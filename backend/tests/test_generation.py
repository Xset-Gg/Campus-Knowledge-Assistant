"""Tests for citation validation, chunking, and the abstention path."""
from __future__ import annotations

import uuid

import pytest

from app.models import AccessLevel, DocumentType
from app.schemas import RetrievedChunk
from app.services.generation_service import build_context_block, extract_citations
from app.services.rerank_service import confidence_from_results
from ingestion.chunking import chunk_blocks
from ingestion.parsers import ParsedBlock


def make_chunk(title: str = "CS101 Syllabus", page: int = 4, year: int = 2026) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title=title,
        content="Late assignments lose 10 percent of the total marks per calendar day.",
        page_number=page,
        section_path="3. Grading > 3.2 Late Work",
        department="Computer Science",
        academic_year=year,
        document_type=DocumentType.syllabus,
        access_level=AccessLevel.student,
        fusion_score=0.42,
        rerank_score=0.88,
    )


class TestCitationExtraction:
    def test_valid_markers_become_citations(self):
        chunks = [make_chunk("A"), make_chunk("B")]
        answer, citations = extract_citations("Rule one [1]. Rule two [2].", chunks)
        assert [c.marker for c in citations] == [1, 2]
        assert answer == "Rule one [1]. Rule two [2]."

    def test_hallucinated_marker_is_stripped(self):
        chunks = [make_chunk("A")]
        answer, citations = extract_citations("Says so [1] and also [7].", chunks)
        assert [c.marker for c in citations] == [1]
        assert "[7]" not in answer

    def test_marker_zero_is_rejected(self):
        chunks = [make_chunk("A")]
        answer, citations = extract_citations("Claim [0].", chunks)
        assert citations == []
        assert "[0]" not in answer

    def test_repeated_marker_yields_one_citation(self):
        chunks = [make_chunk("A")]
        _, citations = extract_citations("One [1]. Two [1]. Three [1].", chunks)
        assert len(citations) == 1

    def test_uncited_answer_produces_no_citations(self):
        chunks = [make_chunk("A")]
        _, citations = extract_citations("There is a policy about this.", chunks)
        assert citations == []

    def test_outdated_source_is_flagged(self):
        chunks = [make_chunk("Old Policy", year=2023)]
        _, citations = extract_citations("Per the policy [1].", chunks)
        assert citations[0].is_outdated is True

    def test_current_source_is_not_flagged(self):
        chunks = [make_chunk("New Policy", year=2026)]
        _, citations = extract_citations("Per the policy [1].", chunks)
        assert citations[0].is_outdated is False

    def test_citation_carries_page_and_title(self):
        chunks = [make_chunk("Enrollment Policy", page=12)]
        _, citations = extract_citations("See the rule [1].", chunks)
        assert citations[0].document_title == "Enrollment Policy"
        assert citations[0].page_number == 12


class TestContextBlock:
    def test_sources_are_numbered_from_one(self):
        block = build_context_block([make_chunk("A"), make_chunk("B")])
        assert block.startswith("[1] A")
        assert "[2] B" in block

    def test_outdated_source_is_marked_for_the_model(self):
        block = build_context_block([make_chunk("Old", year=2022)])
        assert "SUPERSEDED" in block

    def test_page_and_section_are_included(self):
        block = build_context_block([make_chunk(page=7)])
        assert "page 7" in block
        assert "3.2 Late Work" in block

    def test_empty_input_yields_empty_block(self):
        assert build_context_block([]) == ""


class TestConfidence:
    def test_no_results_is_zero_confidence(self):
        assert confidence_from_results([]) == 0.0

    def test_strong_single_hit_is_high_confidence(self):
        chunk = make_chunk()
        chunk.rerank_score = 0.95
        assert confidence_from_results([chunk]) > 0.9

    def test_weak_hits_produce_low_confidence(self):
        chunks = []
        for score in (0.05, 0.04, 0.02):
            chunk = make_chunk()
            chunk.rerank_score = score
            chunks.append(chunk)
        assert confidence_from_results(chunks) < 0.2

    def test_falls_back_to_fusion_score_without_reranker(self):
        chunk = make_chunk()
        chunk.rerank_score = None
        chunk.fusion_score = 0.6
        assert confidence_from_results([chunk]) == pytest.approx(0.6)


class TestChunking:
    def test_empty_input_produces_no_chunks(self):
        assert chunk_blocks([]) == []

    def test_chunks_respect_the_token_budget(self):
        blocks = [
            ParsedBlock("1. Rules", 1, "1. Rules", True, 1),
            ParsedBlock("This sentence repeats. " * 200, 1, "1. Rules"),
        ]
        chunks = chunk_blocks(blocks, max_tokens=100, min_tokens=10, overlap_tokens=10)
        assert len(chunks) > 1
        assert all(c.token_count <= 120 for c in chunks)

    def test_every_chunk_keeps_its_section_path(self):
        blocks = [
            ParsedBlock("2. Penalties", 3, "Policy > 2. Penalties", True, 2),
            ParsedBlock("A warning is issued. " * 30, 3, "Policy > 2. Penalties"),
        ]
        chunks = chunk_blocks(blocks, max_tokens=60, min_tokens=5, overlap_tokens=5)
        assert all(c.section_path == "Policy > 2. Penalties" for c in chunks)

    def test_page_number_is_preserved_for_citations(self):
        blocks = [
            ParsedBlock("1. Scope", 9, "1. Scope", True, 1),
            ParsedBlock("Applies to all students. " * 5, 9, "1. Scope"),
        ]
        chunks = chunk_blocks(blocks)
        assert chunks[0].page_number == 9

    def test_chunk_indices_are_sequential(self):
        blocks = [
            ParsedBlock(f"{n}. Section", n, f"{n}. Section", True, 1)
            for n in range(1, 4)
        ] + [ParsedBlock("Body text here. " * 20, 1, "1. Section")]
        chunks = chunk_blocks(blocks, max_tokens=50, min_tokens=5)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_headings_without_a_body_do_not_become_chunks(self):
        blocks = [ParsedBlock("Table of Contents", 1, "Table of Contents", True, 1)]
        assert chunk_blocks(blocks) == []
