"""Langfuse tracing.

Every stage of the RAG pipeline is recorded as a span on one trace so that a
bad answer can be debugged end to end: what was retrieved, how reranking
reordered it, what context the model actually saw, and what it cost.

Tracing is entirely optional — when Langfuse is not configured, every call
here becomes a no-op and the request path is unaffected. Observability must
never be able to take down the API.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@lru_cache(maxsize=1)
def _get_client() -> Any | None:
    if not settings.enable_tracing:
        return None
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        logger.warning("ENABLE_TRACING is set but Langfuse keys are missing; tracing disabled")
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:
        logger.exception("Langfuse initialization failed; continuing without tracing")
        return None


class _NoOpSpan:
    """Stand-in span used when tracing is disabled."""

    def update(self, **_: Any) -> None:
        return None

    def end(self, **_: Any) -> None:
        return None

    def span(self, **_: Any) -> "_NoOpSpan":
        return self

    def generation(self, **_: Any) -> "_NoOpSpan":
        return self

    def score(self, **_: Any) -> None:
        return None


class Trace:
    """A single request's trace. Wraps Langfuse, degrading to no-ops on failure."""

    def __init__(self, name: str, user_id: str | None = None, metadata: dict | None = None) -> None:
        self.id = str(uuid.uuid4())
        self._trace: Any = None
        client = _get_client()
        if client is None:
            return
        try:
            self._trace = client.trace(
                id=self.id, name=name, user_id=user_id, metadata=metadata or {}
            )
        except Exception:
            logger.exception("Failed to open Langfuse trace")
            self._trace = None

    @contextmanager
    def span(self, name: str, input: Any = None, metadata: dict | None = None):
        """Record a pipeline stage. Yields a span handle you can `.update(output=...)`."""
        if self._trace is None:
            yield _NoOpSpan()
            return
        span: Any = _NoOpSpan()
        try:
            span = self._trace.span(name=name, input=input, metadata=metadata or {})
        except Exception:
            logger.debug("Failed to open span %s", name, exc_info=True)
        try:
            yield span
        finally:
            try:
                span.end()
            except Exception:
                logger.debug("Failed to close span %s", name, exc_info=True)

    def record_generation(
        self,
        *,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        answered: bool,
    ) -> None:
        """Log the model call so cost and latency roll up in the Langfuse dashboard."""
        if self._trace is None:
            return
        try:
            self._trace.generation(
                name="answer-generation",
                model=model,
                usage={"input": input_tokens, "output": output_tokens, "unit": "TOKENS"},
                metadata={"latency_ms": latency_ms, "answered": answered},
            )
        except Exception:
            logger.debug("Failed to record generation", exc_info=True)

    def score(self, name: str, value: float, comment: str | None = None) -> None:
        """Attach a score — used for thumbs up/down and offline eval results."""
        client = _get_client()
        if client is None:
            return
        try:
            client.score(trace_id=self.id, name=name, value=value, comment=comment)
        except Exception:
            logger.debug("Failed to record score %s", name, exc_info=True)

    def finalize(self, output: Any = None, metadata: dict | None = None) -> None:
        if self._trace is None:
            return
        try:
            self._trace.update(output=output, metadata=metadata or {})
        except Exception:
            logger.debug("Failed to finalize trace", exc_info=True)


def score_trace(trace_id: str, name: str, value: float, comment: str | None = None) -> None:
    """Score an existing trace by id — used when feedback arrives after the request."""
    client = _get_client()
    if client is None:
        return
    try:
        client.score(trace_id=trace_id, name=name, value=value, comment=comment)
    except Exception:
        logger.debug("Failed to score trace %s", trace_id, exc_info=True)


def flush() -> None:
    """Flush buffered events. Call on application shutdown."""
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        logger.debug("Langfuse flush failed", exc_info=True)
