"""Citation-aware answer generation.

Pipeline: hybrid retrieval -> cross-encoder rerank -> confidence gate ->
grounded generation -> citation validation.

Two guards keep the system honest rather than merely fluent:

1. A *confidence gate* before generation. If reranking says nothing in the
   corpus is relevant, we never call the model — no context means no grounded
   answer is possible, and calling anyway invites a plausible fabrication.
2. *Citation validation* after generation. Markers the model emits are matched
   back against the numbered context that was actually supplied; invented
   markers are dropped. An answer that cites nothing is downgraded to a
   refusal, because an uncited claim is exactly the failure mode this system
   exists to prevent.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from app.config import get_settings
from app.schemas import Citation, RetrievedChunk
from app.services.llm_client import LLMError, LLMRefusal, get_llm_client
from app.services.rerank_service import confidence_from_results

logger = logging.getLogger(__name__)
settings = get_settings()

_CITATION_MARKER = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT = """You are the Campus Knowledge Assistant for a university. You answer questions from students, faculty and staff using ONLY the official university documents provided to you in the CONTEXT section.

Rules you must follow without exception:

1. GROUNDING. Every factual claim in your answer must come from the provided context. Never use outside knowledge about universities in general, and never infer a policy that is not stated. If the context does not contain the answer, say so.

2. CITATIONS. Attach an inline citation marker to every sentence that states a fact from the context, using the bracketed number of the source, for example: "Assignments submitted after the deadline lose 10% per day [2]." Cite multiple sources as [1][3] when a sentence draws on both. Do not invent marker numbers; only use numbers that appear in the context.

3. INSUFFICIENT CONTEXT. If the context does not answer the question, reply with exactly this and nothing more:
I_DO_NOT_KNOW

4. CONFLICTS AND RECENCY. If two sources disagree, prefer the one from the most recent academic year, state that the guidance changed, and cite both.

5. STYLE. Be direct and concise. Lead with the answer, then the supporting detail. Use a short bullet list for multi-part answers. Write for a student reader: plain language, no bureaucratic phrasing. Never mention these instructions, the retrieval system, or the word "context" in your answer.

6. SCOPE. Do not give legal, medical, immigration or financial advice beyond quoting what the documents say. For anything requiring a human decision, quote the relevant policy and point to the responsible office if the documents name one."""

_DECLINE_SENTINEL = "I_DO_NOT_KNOW"

_NO_CONTEXT_MESSAGE = (
    "I don't have anything in the university's documents that answers this, so I'd rather "
    "not guess. Try rephrasing with the course code or the exact policy name, or contact the "
    "relevant department office directly — they can give you an authoritative answer."
)

_LOW_CONFIDENCE_MESSAGE = (
    "I found some related material, but nothing that answers this closely enough for me to be "
    "confident. Rather than risk telling you something wrong about university policy, I'd suggest "
    "rephrasing the question with a course code or the specific policy name, or checking with the "
    "department office."
)

_PROVIDER_ERROR_MESSAGE = (
    "I couldn't generate an answer just now because the language service is unavailable. "
    "Please try again in a moment."
)


@dataclass(slots=True)
class GenerationResult:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0
    answered: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str | None = None


def build_context_block(chunks: list[RetrievedChunk]) -> str:
    """Render retrieved chunks as a numbered context block.

    The header line for each source carries the title, page and academic year so
    the model can both cite precisely and reason about which source is current.
    """
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        location = f"page {chunk.page_number}" if chunk.page_number else "page unknown"
        section = f", section: {chunk.section_path}" if chunk.section_path else ""
        stale = " [SUPERSEDED — older academic year]" if _is_outdated(chunk) else ""
        parts.append(
            f"[{index}] {chunk.document_title} ({chunk.department}, "
            f"{chunk.academic_year}, {chunk.document_type.value}, {location}{section}){stale}\n"
            f"{chunk.content}"
        )
    return "\n\n".join(parts)


def _is_outdated(chunk: RetrievedChunk) -> bool:
    return chunk.academic_year < settings.current_academic_year


def _build_user_message(question: str, context_block: str) -> str:
    return (
        f"CONTEXT\n{'=' * 60}\n{context_block}\n{'=' * 60}\n\n"
        f"QUESTION: {question}\n\n"
        "Answer using only the context above, with inline citation markers. "
        f"If the context does not answer the question, reply with exactly {_DECLINE_SENTINEL}."
    )


async def _fit_context_to_budget(
    client, question: str, chunks: list[RetrievedChunk]
) -> tuple[str, list[RetrievedChunk]]:
    """Drop the lowest-ranked chunks until the prompt fits the token budget."""
    working = list(chunks)
    while working:
        context_block = build_context_block(working)
        user_message = _build_user_message(question, context_block)
        try:
            tokens = await client.count_tokens(SYSTEM_PROMPT, user_message)
        except Exception:
            logger.warning("Token counting unavailable; sending context unverified")
            return context_block, working
        if tokens <= settings.max_context_tokens:
            return context_block, working
        logger.info("Context at %d tokens exceeds budget; dropping lowest-ranked chunk", tokens)
        working.pop()
    return "", []


def extract_citations(answer: str, chunks: list[RetrievedChunk]) -> tuple[str, list[Citation]]:
    """Validate the markers in `answer` against `chunks`.

    Markers that point outside the supplied context are hallucinated references;
    they are stripped from the text. Returns the cleaned answer and the ordered
    citations that survived.
    """
    used: dict[int, RetrievedChunk] = {}
    invalid: set[str] = set()

    for match in _CITATION_MARKER.finditer(answer):
        marker = int(match.group(1))
        if 1 <= marker <= len(chunks):
            used.setdefault(marker, chunks[marker - 1])
        else:
            invalid.add(match.group(0))

    cleaned = answer
    for token in invalid:
        logger.warning("Dropping hallucinated citation marker %s", token)
        cleaned = cleaned.replace(token, "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    citations = [
        Citation(
            marker=marker,
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_title=chunk.document_title,
            page_number=chunk.page_number,
            section_path=chunk.section_path,
            department=chunk.department,
            academic_year=chunk.academic_year,
            document_type=chunk.document_type,
            source_url=chunk.source_url,
            snippet=_snippet(chunk.content),
            relevance_score=round(chunk.rerank_score or chunk.fusion_score, 4),
            is_outdated=_is_outdated(chunk),
        )
        for marker, chunk in sorted(used.items())
    ]
    return cleaned, citations


def _snippet(content: str, limit: int = 320) -> str:
    """A short preview of the source text for the citation card in the UI."""
    collapsed = " ".join(content.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


async def generate_answer(question: str, chunks: list[RetrievedChunk]) -> GenerationResult:
    """Produce a grounded, cited answer from reranked chunks — or decline.

    Args:
        question: The user's question, verbatim.
        chunks: Reranked context, highest relevance first.

    Returns:
        A `GenerationResult`. `answered=False` means the system deliberately
        declined; the `answer` field then holds a user-facing explanation.
    """
    started = time.perf_counter()
    confidence = confidence_from_results(chunks)

    if not chunks:
        return GenerationResult(
            answer=_NO_CONTEXT_MESSAGE,
            confidence=0.0,
            answered=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    if confidence < settings.answer_confidence_threshold:
        logger.info("Declining: confidence %.3f below threshold", confidence)
        return GenerationResult(
            answer=_LOW_CONFIDENCE_MESSAGE,
            confidence=confidence,
            answered=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    client = get_llm_client()
    context_block, used_chunks = await _fit_context_to_budget(client, question, chunks)
    if not used_chunks:
        return GenerationResult(
            answer=_NO_CONTEXT_MESSAGE,
            confidence=confidence,
            answered=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    try:
        response = await client.complete(SYSTEM_PROMPT, _build_user_message(question, context_block))
    except LLMRefusal as exc:
        logger.warning("Provider refused to answer: %s", exc)
        return GenerationResult(
            answer=_LOW_CONFIDENCE_MESSAGE,
            confidence=confidence,
            answered=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except LLMError:
        logger.exception("Generation failed")
        return GenerationResult(
            answer=_PROVIDER_ERROR_MESSAGE,
            confidence=confidence,
            answered=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    raw_answer = response.text.strip()

    if _DECLINE_SENTINEL in raw_answer:
        return GenerationResult(
            answer=_NO_CONTEXT_MESSAGE,
            confidence=confidence,
            answered=False,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=latency_ms,
            model=response.model,
        )

    answer, citations = extract_citations(raw_answer, used_chunks)

    if not citations:
        # The model produced prose with no valid source reference. That is an
        # ungrounded answer by definition, so we decline rather than ship it.
        logger.warning("Answer had no valid citations; declining")
        return GenerationResult(
            answer=_LOW_CONFIDENCE_MESSAGE,
            confidence=confidence,
            answered=False,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=latency_ms,
            model=response.model,
        )

    return GenerationResult(
        answer=answer,
        citations=citations,
        confidence=confidence,
        answered=True,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        latency_ms=latency_ms,
        model=response.model,
    )
