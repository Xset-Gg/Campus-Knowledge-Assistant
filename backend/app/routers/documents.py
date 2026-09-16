"""Document catalogue and upload endpoints."""
from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_capability
from app.db import get_db
from app.models import AccessLevel, Document, DocumentType, UserRole
from app.permissions import allowed_access_levels
from app.schemas import DocumentOut, DocumentUpdate
from ingestion.parsers import compute_file_hash

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".html"}
UPLOAD_DIR = Path("data/raw_pdfs")


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    user: CurrentUser,
    db: DbSession,
    department: str | None = None,
    document_type: DocumentType | None = None,
    academic_year: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Document]:
    """List documents visible to the caller's role."""
    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    levels = [level.value for level in allowed_access_levels(role)]

    query = select(Document).where(Document.access_level.in_(levels))
    if department:
        query = query.where(Document.department == department)
    if document_type:
        query = query.where(Document.document_type == document_type)
    if academic_year:
        query = query.where(Document.academic_year == academic_year)

    query = query.order_by(Document.academic_year.desc(), Document.title.asc())
    query = query.limit(min(limit, 500)).offset(offset)

    return list((await db.execute(query)).scalars().all())


@router.get("/departments", response_model=list[str])
async def list_departments(user: CurrentUser, db: DbSession) -> list[str]:
    """Distinct departments the caller can see — populates the filter dropdown."""
    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    levels = [level.value for level in allowed_access_levels(role)]
    result = await db.execute(
        select(Document.department)
        .where(Document.access_level.in_(levels))
        .distinct()
        .order_by(Document.department)
    )
    return [row[0] for row in result.all()]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Document:
    """Fetch one document's metadata, subject to the caller's access level."""
    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    levels = [level.value for level in allowed_access_levels(role)]

    document = (
        await db.execute(
            select(Document).where(Document.id == document_id, Document.access_level.in_(levels))
        )
    ).scalar_one_or_none()

    # 404 rather than 403 for a document the caller may not read: a 403 would
    # confirm the document exists, which is itself a disclosure.
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


@router.post(
    "/upload",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_capability("upload_documents"))],
)
async def upload_document(
    background_tasks: BackgroundTasks,
    user: CurrentUser,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    department: Annotated[str, Form()],
    academic_year: Annotated[int, Form()],
    document_type: Annotated[DocumentType, Form()] = DocumentType.other,
    access_level: Annotated[AccessLevel, Form()] = AccessLevel.student,
    source_url: Annotated[str | None, Form()] = None,
) -> Document:
    """Upload a document and queue it for ingestion.

    A user cannot publish a document at an access level they themselves could
    not read — otherwise a professor could create admin-only content and an
    ordinary uploader could quietly widen the audience of a restricted policy.
    """
    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    if access_level not in allowed_access_levels(role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your role cannot publish documents at access level '{access_level.value}'",
        )

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{suffix}'. Allowed: {sorted(ALLOWED_SUFFIXES)}",
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Never trust the client-supplied filename for the path on disk.
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    destination = UPLOAD_DIR / safe_name

    size = 0
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as staging:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit",
                    )
                staging.write(block)
            staging_path = Path(staging.name)
        shutil.move(str(staging_path), destination)
    except HTTPException:
        raise
    except OSError as exc:
        logger.exception("Upload failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not store the upload"
        ) from exc
    finally:
        await file.close()

    # Hash now, on the file already written to disk. The placeholder row and the
    # row the background ingest resolves must share a file_hash, or ingestion
    # creates a second document instead of completing this one.
    file_hash = compute_file_hash(destination)

    existing = (
        await db.execute(select(Document).where(Document.file_hash == file_hash))
    ).scalar_one_or_none()
    if existing is not None:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This file has already been ingested as '{existing.title}'",
        )

    document = Document(
        title=title,
        file_name=safe_name,
        file_hash=file_hash,
        department=department,
        academic_year=academic_year,
        document_type=document_type,
        access_level=access_level,
        source_url=source_url,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    background_tasks.add_task(
        _ingest_uploaded_document,
        destination,
        {
            "title": title,
            "department": department,
            "academic_year": academic_year,
            "document_type": document_type.value,
            "access_level": access_level.value,
            "source_url": source_url,
        },
    )

    return document


def _ingest_uploaded_document(path: Path, metadata: dict) -> None:
    """Run the ingestion pipeline for an uploaded file, off the request path."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from ingestion.ingest import infer_metadata, ingest_file

    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with sessionmaker(bind=engine)() as session:
            result = ingest_file(session, path, infer_metadata(path, metadata), force=True)
            if result.status == "failed":
                logger.error("Background ingestion failed for %s: %s", path.name, result.error)
    except Exception:
        logger.exception("Background ingestion crashed for %s", path.name)
    finally:
        engine.dispose()


@router.patch(
    "/{document_id}",
    response_model=DocumentOut,
    dependencies=[Depends(require_capability("manage_documents"))],
)
async def update_document(
    document_id: uuid.UUID, payload: DocumentUpdate, db: DbSession
) -> Document:
    """Update document metadata. Changes propagate to the denormalized chunk rows."""
    document = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    updates = payload.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(document, field_name, value)

    # Chunks carry copies of these fields for fast filtering, so they must be
    # kept in step — a stale access_level on a chunk is a security hole.
    propagated = {
        key: value
        for key, value in updates.items()
        if key in {"department", "academic_year", "document_type", "access_level"}
    }
    if propagated:
        from app.models import Chunk

        await db.execute(
            Chunk.__table__.update().where(Chunk.document_id == document_id).values(**propagated)
        )

    await db.commit()
    await db.refresh(document)
    return document


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_capability("manage_documents"))],
)
async def delete_document(document_id: uuid.UUID, db: DbSession) -> None:
    """Delete a document and its chunks."""
    document = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await db.delete(document)
    await db.commit()


@router.get(
    "/stats/summary",
    dependencies=[Depends(require_capability("view_analytics"))],
)
async def document_stats(db: DbSession) -> dict[str, object]:
    """Corpus composition, for the admin dashboard."""
    from app.models import Chunk

    by_department = (
        await db.execute(
            select(Document.department, func.count(Document.id)).group_by(Document.department)
        )
    ).all()
    by_year = (
        await db.execute(
            select(Document.academic_year, func.count(Document.id))
            .group_by(Document.academic_year)
            .order_by(Document.academic_year.desc())
        )
    ).all()
    by_status = (
        await db.execute(
            select(Document.ingestion_status, func.count(Document.id)).group_by(
                Document.ingestion_status
            )
        )
    ).all()
    chunk_count = (await db.execute(select(func.count(Chunk.id)))).scalar_one()

    return {
        "documents_by_department": {row[0]: row[1] for row in by_department},
        "documents_by_year": {str(row[0]): row[1] for row in by_year},
        "documents_by_status": {str(row[0]): row[1] for row in by_status},
        "total_chunks": chunk_count,
    }
