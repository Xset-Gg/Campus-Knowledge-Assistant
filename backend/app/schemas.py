"""Pydantic request/response models for the public API."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import AccessLevel, DocumentType, UserRole


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    role: UserRole = UserRole.student
    department: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: UserRole
    department: str | None
    is_active: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# --------------------------------------------------------------------------
# Retrieval / chat
# --------------------------------------------------------------------------
class Citation(BaseModel):
    """A single source reference attached to a generated answer."""

    marker: int = Field(description="Inline citation number used in the answer text, e.g. [1]")
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    page_number: int | None
    section_path: str | None
    department: str
    academic_year: int
    document_type: DocumentType
    source_url: str | None = None
    snippet: str
    relevance_score: float
    is_outdated: bool = Field(
        default=False,
        description="True when the source predates the current academic year.",
    )


class RetrievedChunk(BaseModel):
    """Internal retrieval result, also exposed on debug endpoints for admins."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    content: str
    page_number: int | None
    section_path: str | None
    department: str
    academic_year: int
    document_type: DocumentType
    access_level: AccessLevel
    source_url: str | None = None
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    fusion_score: float = 0.0
    rerank_score: float | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    session_id: uuid.UUID | None = None
    department: str | None = Field(default=None, description="Optional department filter")
    document_type: DocumentType | None = None
    include_outdated: bool = Field(
        default=False, description="Include documents from previous academic years"
    )


class AskResponse(BaseModel):
    message_id: uuid.UUID
    session_id: uuid.UUID
    answer: str
    citations: list[Citation]
    confidence: float
    answered: bool = Field(description="False when the system declined to answer")
    latency_ms: int
    trace_id: str | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=10, ge=1, le=50)
    department: str | None = None
    document_type: DocumentType | None = None
    include_outdated: bool = False


class SearchResponse(BaseModel):
    query: str
    results: list[RetrievedChunk]
    total: int


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    file_name: str
    department: str
    academic_year: int
    document_type: DocumentType
    access_level: AccessLevel
    is_current: bool
    source_url: str | None
    page_count: int | None
    ingestion_status: str
    created_at: datetime


class DocumentUpdate(BaseModel):
    title: str | None = None
    department: str | None = None
    academic_year: int | None = None
    document_type: DocumentType | None = None
    access_level: AccessLevel | None = None
    is_current: bool | None = None


# --------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------
class FeedbackCreate(BaseModel):
    message_id: uuid.UUID
    rating: int = Field(description="1 for thumbs up, -1 for thumbs down")
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_id: uuid.UUID
    rating: int
    comment: str | None
    created_at: datetime


# --------------------------------------------------------------------------
# Chat history
# --------------------------------------------------------------------------
class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    citations: list | None
    confidence: float | None
    created_at: datetime


class ChatSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    created_at: datetime


# --------------------------------------------------------------------------
# Admin analytics
# --------------------------------------------------------------------------
class FailedSearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    query: str
    top_score: float | None
    result_count: int | None
    created_at: datetime


class AnalyticsSummary(BaseModel):
    total_questions: int
    unanswered_questions: int
    answer_rate: float
    thumbs_up: int
    thumbs_down: int
    avg_latency_ms: float | None
    document_count: int
    chunk_count: int
