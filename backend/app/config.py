"""Centralized application settings, loaded from environment variables."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql://ckadmin:change_me@localhost:5432/campus_knowledge"

    # Auth
    jwt_secret_key: str = "insecure-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60

    # LLM
    llm_provider: Literal["openai", "anthropic"] = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    # Thinking depth / token spend for the answer call: low | medium | high | xhigh | max.
    # "medium" balances grounding quality against the latency a chat UI can absorb.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 2048
    # Upper bound on retrieved context handed to the model, enforced with the
    # provider's own token counter so a long syllabus cannot blow the budget.
    max_context_tokens: int = 24000

    # Embeddings & reranking
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # Retrieval tuning
    hybrid_semantic_weight: float = 0.6
    hybrid_keyword_weight: float = 0.4
    rrf_k: int = 60
    retrieval_top_k: int = 25
    rerank_top_k: int = 5
    answer_confidence_threshold: float = 0.35
    current_academic_year: int = 2026

    # Observability
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    enable_tracing: bool = False

    # App
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000"
    environment: str = "development"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
