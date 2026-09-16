"""Ingestion CLI: parse university PDFs and load them into PostgreSQL + pgvector.

Usage
-----
Single file with explicit metadata:

    python -m ingestion.ingest \
        --file data/raw_pdfs/cs101_syllabus.pdf \
        --title "CS101 Introduction to Computer Science" \
        --department "Computer Science" \
        --academic-year 2026 \
        --document-type syllabus \
        --access-level student

Whole directory driven by a manifest (see data/sample_docs/manifest.example.json):

    python -m ingestion.ingest --manifest data/sample_docs/manifest.json

Metadata that is not supplied is inferred from the filename where possible,
and the run reports every file it skipped and why.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

# Allow `python -m ingestion.ingest` from the backend/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    AccessLevel,
    Chunk as ChunkModel,
    Document,
    DocumentType,
    IngestionStatus,
)
from app.services.embedding_service import embed_texts  # noqa: E402
from ingestion.chunking import chunk_blocks  # noqa: E402
from ingestion.parsers import DocumentParseError, compute_file_hash, parse_pdf  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingest")

settings = get_settings()

# Filename heuristics used only to fill gaps in supplied metadata.
_YEAR_PATTERN = re.compile(r"(20\d{2})")
_TYPE_KEYWORDS = {
    "syllabus": DocumentType.syllabus,
    "syllabi": DocumentType.syllabus,
    "policy": DocumentType.policy,
    "policies": DocumentType.policy,
    "handbook": DocumentType.handbook,
    "form": DocumentType.form,
    "announcement": DocumentType.announcement,
}


@dataclass(slots=True)
class DocumentMetadata:
    """Metadata attached to every chunk of a document."""

    title: str
    department: str
    academic_year: int
    document_type: DocumentType
    access_level: AccessLevel
    source_url: str | None = None
    is_current: bool = True


@dataclass(slots=True)
class IngestResult:
    file_name: str
    status: str
    chunk_count: int = 0
    error: str | None = None


def infer_metadata(path: Path, overrides: dict[str, Any]) -> DocumentMetadata:
    """Build document metadata from explicit overrides, falling back to filename hints."""
    stem = path.stem
    lowered = stem.lower()

    year_match = _YEAR_PATTERN.search(stem)
    inferred_year = int(year_match.group(1)) if year_match else settings.current_academic_year

    inferred_type = DocumentType.other
    for keyword, doc_type in _TYPE_KEYWORDS.items():
        if keyword in lowered:
            inferred_type = doc_type
            break

    title = overrides.get("title") or stem.replace("_", " ").replace("-", " ").strip().title()
    department = overrides.get("department") or "General"

    document_type = overrides.get("document_type")
    if isinstance(document_type, str):
        document_type = DocumentType(document_type)

    access_level = overrides.get("access_level")
    if isinstance(access_level, str):
        access_level = AccessLevel(access_level)

    return DocumentMetadata(
        title=title,
        department=department,
        academic_year=int(overrides.get("academic_year") or inferred_year),
        document_type=document_type or inferred_type,
        # Default to the most restrictive level a student can still read, so a
        # missing access_level never silently publishes a document to everyone.
        access_level=access_level or AccessLevel.student,
        source_url=overrides.get("source_url"),
        is_current=bool(overrides.get("is_current", True)),
    )


def ingest_file(
    session: Session,
    path: Path,
    metadata: DocumentMetadata,
    *,
    force: bool = False,
) -> IngestResult:
    """Parse, chunk, embed and persist a single document.

    Re-ingesting an unchanged file is a no-op unless `force` is set; re-ingesting
    a changed file replaces its chunks atomically within one transaction.
    """
    try:
        file_hash = compute_file_hash(path)
    except OSError as exc:
        return IngestResult(path.name, "failed", error=f"unreadable file: {exc}")

    existing = session.execute(select(Document).where(Document.file_hash == file_hash)).scalar_one_or_none()
    if existing and not force:
        logger.info("Skipping %s — already ingested (hash match)", path.name)
        return IngestResult(path.name, "skipped", error="duplicate file hash")

    document = existing or Document(file_hash=file_hash, file_name=path.name)
    document.title = metadata.title
    document.department = metadata.department
    document.academic_year = metadata.academic_year
    document.document_type = metadata.document_type
    document.access_level = metadata.access_level
    document.source_url = metadata.source_url
    document.is_current = metadata.is_current
    document.ingestion_status = IngestionStatus.processing
    document.ingestion_error = None
    session.add(document)
    session.flush()

    try:
        parsed = parse_pdf(path)
    except DocumentParseError as exc:
        document.ingestion_status = IngestionStatus.failed
        document.ingestion_error = str(exc)
        session.commit()
        logger.error("Parse failed for %s: %s", path.name, exc)
        return IngestResult(path.name, "failed", error=str(exc))

    if document.title == path.stem.replace("_", " ").title() and parsed.detected_title:
        document.title = parsed.detected_title
    document.page_count = parsed.page_count

    chunks = chunk_blocks(parsed.blocks)
    if not chunks:
        document.ingestion_status = IngestionStatus.failed
        document.ingestion_error = "no chunks produced"
        session.commit()
        return IngestResult(path.name, "failed", error="no chunks produced")

    try:
        embeddings = embed_texts([chunk.content for chunk in chunks])
    except Exception as exc:
        document.ingestion_status = IngestionStatus.failed
        document.ingestion_error = f"embedding failed: {exc}"
        session.commit()
        logger.exception("Embedding failed for %s", path.name)
        return IngestResult(path.name, "failed", error=str(exc))

    # Replace any previous chunks for this document.
    session.execute(delete(ChunkModel).where(ChunkModel.document_id == document.id))

    for chunk, embedding in zip(chunks, embeddings, strict=True):
        session.add(
            ChunkModel(
                document_id=document.id,
                chunk_index=chunk.chunk_index,
                section_path=chunk.section_path,
                page_number=chunk.page_number,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=embedding,
                department=metadata.department,
                academic_year=metadata.academic_year,
                document_type=metadata.document_type,
                access_level=metadata.access_level,
            )
        )

    document.ingestion_status = IngestionStatus.completed
    session.commit()
    logger.info("Ingested %s — %d chunks", path.name, len(chunks))
    return IngestResult(path.name, "completed", chunk_count=len(chunks))


def ingest_manifest(session: Session, manifest_path: Path, *, force: bool = False) -> list[IngestResult]:
    """Ingest every entry of a JSON manifest.

    Manifest format:
        [
          {
            "file": "data/raw_pdfs/cs101_syllabus.pdf",
            "title": "CS101 Introduction to Computer Science",
            "department": "Computer Science",
            "academic_year": 2026,
            "document_type": "syllabus",
            "access_level": "student",
            "source_url": "https://university.edu/docs/cs101.pdf"
          }
        ]
    """
    try:
        entries = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Cannot read manifest %s: %s", manifest_path, exc)
        return [IngestResult(manifest_path.name, "failed", error=str(exc))]

    results: list[IngestResult] = []
    base_dir = manifest_path.parent
    for entry in entries:
        raw_path = entry.get("file")
        if not raw_path:
            results.append(IngestResult("<missing file key>", "failed", error="manifest entry has no 'file'"))
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = (base_dir / path).resolve() if not path.exists() else path
        if not path.exists():
            results.append(IngestResult(str(raw_path), "failed", error="file not found"))
            continue
        try:
            metadata = infer_metadata(path, entry)
        except ValueError as exc:
            results.append(IngestResult(path.name, "failed", error=f"invalid metadata: {exc}"))
            continue
        results.append(ingest_file(session, path, metadata, force=force))
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest university documents into the RAG store.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Single file to ingest")
    source.add_argument("--directory", type=Path, help="Directory of PDFs to ingest")
    source.add_argument("--manifest", type=Path, help="JSON manifest of files + metadata")

    parser.add_argument("--title")
    parser.add_argument("--department")
    parser.add_argument("--academic-year", type=int)
    parser.add_argument("--document-type", choices=[t.value for t in DocumentType])
    parser.add_argument("--access-level", choices=[a.value for a in AccessLevel])
    parser.add_argument("--source-url")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if the file hash is unchanged")
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine)

    overrides = {
        "title": args.title,
        "department": args.department,
        "academic_year": args.academic_year,
        "document_type": args.document_type,
        "access_level": args.access_level,
        "source_url": args.source_url,
    }
    overrides = {key: value for key, value in overrides.items() if value is not None}

    results: list[IngestResult] = []
    with SessionLocal() as session:
        if args.manifest:
            results = ingest_manifest(session, args.manifest, force=args.force)
        elif args.file:
            if not args.file.exists():
                logger.error("File not found: %s", args.file)
                return 1
            results = [ingest_file(session, args.file, infer_metadata(args.file, overrides), force=args.force)]
        else:
            pdfs = sorted(args.directory.glob("**/*.pdf"))
            if not pdfs:
                logger.warning("No PDFs found under %s", args.directory)
            for path in pdfs:
                results.append(ingest_file(session, path, infer_metadata(path, overrides), force=args.force))

    completed = [r for r in results if r.status == "completed"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]

    logger.info(
        "Done — %d ingested (%d chunks), %d skipped, %d failed",
        len(completed),
        sum(r.chunk_count for r in completed),
        len(skipped),
        len(failed),
    )
    for result in failed:
        logger.error("  FAILED %s: %s", result.file_name, result.error)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
