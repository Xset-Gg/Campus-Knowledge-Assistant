"""PDF parsing via Docling, normalized into a document-structure representation.

Docling gives us a hierarchical document model (headings, paragraphs, tables,
page provenance). We flatten it into an ordered list of `ParsedBlock`s that
carry both the section path and the originating page number, which is what
makes page-accurate citations possible downstream.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class DocumentParseError(Exception):
    """Raised when a source file cannot be parsed into usable text."""


@dataclass(slots=True)
class ParsedBlock:
    """A single structural element extracted from a document."""

    text: str
    page_number: int | None
    section_path: str
    is_heading: bool = False
    heading_level: int | None = None


@dataclass(slots=True)
class ParsedDocument:
    """The full parse result for one source file."""

    file_name: str
    file_hash: str
    page_count: int | None
    blocks: list[ParsedBlock] = field(default_factory=list)
    detected_title: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any(block.text.strip() for block in self.blocks)


def compute_file_hash(path: Path) -> str:
    """SHA-256 of the file contents, used to deduplicate re-ingested documents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _section_path_from_stack(stack: list[tuple[int, str]]) -> str:
    """Render the current heading stack as a breadcrumb, e.g. "3. Grading > 3.2 Late Work"."""
    return " > ".join(title for _, title in stack)


def _item_page_number(item: object) -> int | None:
    """Best-effort extraction of a 1-based page number from a Docling item."""
    provenance = getattr(item, "prov", None)
    if not provenance:
        return None
    first = provenance[0]
    page_no = getattr(first, "page_no", None)
    return int(page_no) if page_no is not None else None


def parse_pdf(path: Path) -> ParsedDocument:
    """Parse a PDF (or any Docling-supported format) into ordered structural blocks.

    Raises:
        DocumentParseError: if the file is missing, corrupted, or yields no text.
    """
    if not path.exists():
        raise DocumentParseError(f"File not found: {path}")
    if path.stat().st_size == 0:
        raise DocumentParseError(f"File is empty: {path}")

    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise DocumentParseError("Docling is not installed; run `pip install docling`") from exc

    try:
        converter = DocumentConverter()
        result = converter.convert(str(path))
    except Exception as exc:
        # Corrupted / encrypted / unsupported PDFs land here. We surface a typed
        # error so the pipeline can mark the document `failed` and keep going.
        raise DocumentParseError(f"Docling failed to parse {path.name}: {exc}") from exc

    doc = result.document
    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    detected_title: str | None = getattr(doc, "name", None) or None

    for item, _level in doc.iterate_items():
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue

        label = str(getattr(item, "label", "") or "").lower()
        page_number = _item_page_number(item)

        if "title" in label and detected_title is None:
            detected_title = text

        if "section_header" in label or "heading" in label or "title" in label:
            level = int(getattr(item, "level", 1) or 1)
            # Pop any headings at the same or deeper level before pushing this one.
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            blocks.append(
                ParsedBlock(
                    text=text,
                    page_number=page_number,
                    section_path=_section_path_from_stack(heading_stack),
                    is_heading=True,
                    heading_level=level,
                )
            )
            continue

        blocks.append(
            ParsedBlock(
                text=text,
                page_number=page_number,
                section_path=_section_path_from_stack(heading_stack),
            )
        )

    page_count = len(getattr(doc, "pages", []) or []) or None
    parsed = ParsedDocument(
        file_name=path.name,
        file_hash=compute_file_hash(path),
        page_count=page_count,
        blocks=blocks,
        detected_title=detected_title,
    )

    if parsed.is_empty:
        raise DocumentParseError(
            f"{path.name} produced no extractable text (scanned image PDF? try OCR)"
        )
    return parsed
