"""Structure-aware chunking.

Naive fixed-size chunking splits mid-sentence and mixes unrelated policy
sections into one chunk, which produces retrieval hits that cite the wrong
section. Instead we group blocks by their heading path and only split a
section when it exceeds the token budget — and when we do split, we carry the
heading breadcrumb and an overlap window into the next chunk so each chunk
stands alone.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache

from ingestion.parsers import ParsedBlock

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 512
DEFAULT_MIN_TOKENS = 40
DEFAULT_OVERLAP_TOKENS = 64

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


@dataclass(slots=True)
class Chunk:
    """A retrieval-sized piece of a document, with its provenance intact."""

    content: str
    chunk_index: int
    page_number: int | None
    section_path: str | None
    token_count: int


@lru_cache(maxsize=1)
def _encoder():
    """tiktoken encoder, with a whitespace fallback when tiktoken is unavailable."""
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # pragma: no cover - optional dependency
        logger.warning("tiktoken unavailable; falling back to whitespace token counting")
        return None


def count_tokens(text: str) -> int:
    encoder = _encoder()
    if encoder is None:
        return len(text.split())
    return len(encoder.encode(text))


def _split_long_text(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split an over-long section on sentence boundaries with a sliding overlap."""
    sentences = _SENTENCE_BOUNDARY.split(text)
    parts: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for sentence in sentences:
        sentence_tokens = count_tokens(sentence)

        # A single sentence longer than the budget is split on words as a last resort.
        if sentence_tokens > max_tokens:
            if current:
                parts.append(" ".join(current))
                current, current_tokens = [], 0
            words = sentence.split()
            stride = max(1, max_tokens // 2)
            for start in range(0, len(words), stride):
                parts.append(" ".join(words[start : start + max_tokens]))
            continue

        if current_tokens + sentence_tokens > max_tokens and current:
            parts.append(" ".join(current))
            # Carry the tail of the previous chunk forward as overlap context.
            overlap: list[str] = []
            overlap_count = 0
            for prev in reversed(current):
                prev_tokens = count_tokens(prev)
                if overlap_count + prev_tokens > overlap_tokens:
                    break
                overlap.insert(0, prev)
                overlap_count += prev_tokens
            current = overlap
            current_tokens = overlap_count

        current.append(sentence)
        current_tokens += sentence_tokens

    if current:
        parts.append(" ".join(current))
    return [part.strip() for part in parts if part.strip()]


def chunk_blocks(
    blocks: list[ParsedBlock],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Group parsed blocks into section-respecting chunks.

    Args:
        blocks: Ordered structural blocks from the parser.
        max_tokens: Hard upper bound on chunk size.
        min_tokens: Chunks below this are merged into the following chunk so
            that stray one-line headings never become standalone results.
        overlap_tokens: Sentence overlap carried between splits of one section.
    """
    # 1. Group consecutive blocks that share a heading path.
    groups: list[tuple[str, list[ParsedBlock]]] = []
    for block in blocks:
        section = block.section_path or ""
        if groups and groups[-1][0] == section:
            groups[-1][1].append(block)
        else:
            groups.append((section, [block]))

    # 2. Turn each group into one or more chunks.
    raw_chunks: list[Chunk] = []
    for section_path, group in groups:
        body_blocks = [b for b in group if not b.is_heading]
        heading_text = " ".join(b.text for b in group if b.is_heading)
        body = "\n".join(b.text for b in body_blocks).strip()

        if not body:
            # Heading with no body of its own — it will prefix the next chunk
            # via section_path, so nothing to emit here.
            continue

        page_number = next((b.page_number for b in group if b.page_number is not None), None)
        # Prefixing the heading makes each chunk self-describing for the embedder.
        prefix = f"{section_path}\n" if section_path else (f"{heading_text}\n" if heading_text else "")
        full_text = f"{prefix}{body}".strip()

        if count_tokens(full_text) <= max_tokens:
            raw_chunks.append(
                Chunk(
                    content=full_text,
                    chunk_index=0,
                    page_number=page_number,
                    section_path=section_path or None,
                    token_count=count_tokens(full_text),
                )
            )
            continue

        for part in _split_long_text(body, max_tokens - count_tokens(prefix), overlap_tokens):
            part_text = f"{prefix}{part}".strip()
            raw_chunks.append(
                Chunk(
                    content=part_text,
                    chunk_index=0,
                    page_number=page_number,
                    section_path=section_path or None,
                    token_count=count_tokens(part_text),
                )
            )

    # 3. Merge undersized chunks forward, then renumber.
    merged: list[Chunk] = []
    for chunk in raw_chunks:
        if merged and chunk.token_count < min_tokens:
            previous = merged[-1]
            combined = f"{previous.content}\n{chunk.content}"
            if count_tokens(combined) <= max_tokens:
                merged[-1] = Chunk(
                    content=combined,
                    chunk_index=0,
                    page_number=previous.page_number,
                    section_path=previous.section_path,
                    token_count=count_tokens(combined),
                )
                continue
        merged.append(chunk)

    for index, chunk in enumerate(merged):
        chunk.chunk_index = index
    return merged
