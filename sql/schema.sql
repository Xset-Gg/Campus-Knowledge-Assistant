-- Campus Knowledge Assistant — PostgreSQL + pgvector schema
-- Run once against a fresh database: psql -f sql/schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Enumerated domains
--
-- These are TEXT + CHECK rather than native ENUM types. Two reasons:
--   1. The RBAC filter binds a text[] parameter (`access_level = ANY(:levels)`).
--      Against a native enum column that comparison needs an explicit cast and
--      fails as "operator does not exist: access_level = text".
--   2. Adding a document type becomes a CHECK change instead of ALTER TYPE,
--      which cannot run inside a transaction with other DDL on older servers.
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    CREATE DOMAIN access_level AS TEXT
        CHECK (VALUE IN ('public', 'student', 'faculty', 'admin'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE DOMAIN user_role AS TEXT
        CHECK (VALUE IN ('student', 'professor', 'admin'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE DOMAIN document_type AS TEXT
        CHECK (VALUE IN ('syllabus', 'policy', 'handbook', 'form', 'announcement', 'other'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE DOMAIN ingestion_status AS TEXT
        CHECK (VALUE IN ('pending', 'processing', 'completed', 'failed'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- Users (RBAC principals)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    full_name       TEXT NOT NULL,
    role            user_role NOT NULL DEFAULT 'student',
    department      TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Source documents (one row per ingested PDF)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    file_hash       TEXT NOT NULL UNIQUE,          -- sha256, dedup guard
    department      TEXT NOT NULL,
    academic_year   INTEGER NOT NULL,               -- e.g. 2026
    document_type   document_type NOT NULL DEFAULT 'other',
    access_level    access_level NOT NULL DEFAULT 'student',
    is_current      BOOLEAN NOT NULL DEFAULT TRUE,   -- superseded docs set to FALSE
    source_url      TEXT,                            -- link back to the original PDF
    page_count      INTEGER,
    ingestion_status ingestion_status NOT NULL DEFAULT 'pending',
    ingestion_error TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_department ON documents (department);
CREATE INDEX IF NOT EXISTS idx_documents_academic_year ON documents (academic_year);
CREATE INDEX IF NOT EXISTS idx_documents_access_level ON documents (access_level);

-- ---------------------------------------------------------------------------
-- Chunks (retrieval unit). One row per chunk, with embedding + tsvector.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,               -- ordinal position within the document
    section_path    TEXT,                            -- e.g. "3. Grading > 3.2 Late Work"
    page_number     INTEGER,
    content         TEXT NOT NULL,
    token_count     INTEGER,
    embedding       vector(384),                     -- all-MiniLM-L6-v2 dimension; adjust to your model
    content_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    -- Denormalized filter/boost fields, copied from `documents` at ingest time so
    -- RBAC + temporal filtering never requires a join on the hot query path.
    department      TEXT NOT NULL,
    academic_year   INTEGER NOT NULL,
    document_type   document_type NOT NULL,
    access_level    access_level NOT NULL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Vector index (HNSW; requires pgvector >= 0.5.0). Falls back to ivfflat if unavailable.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- Full-text search index
CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv ON chunks USING GIN (content_tsv);

-- RBAC / temporal filter indexes
CREATE INDEX IF NOT EXISTS idx_chunks_access_level ON chunks (access_level);
CREATE INDEX IF NOT EXISTS idx_chunks_department ON chunks (department);
CREATE INDEX IF NOT EXISTS idx_chunks_academic_year ON chunks (academic_year);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks (document_id);

-- ---------------------------------------------------------------------------
-- Chat sessions / messages (for history + Langfuse correlation)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    citations       JSONB,                           -- [{document_id, title, page_number, chunk_id}, ...]
    confidence      REAL,
    trace_id        TEXT,                             -- Langfuse/LangSmith trace correlation id
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Feedback (RLHF signal collection)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedback (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id  UUID NOT NULL REFERENCES chat_messages (id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    rating      SMALLINT NOT NULL CHECK (rating IN (-1, 1)),  -- thumbs down / thumbs up
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id, user_id)
);

-- ---------------------------------------------------------------------------
-- Failed / low-confidence search log (observability)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users (id) ON DELETE SET NULL,
    query           TEXT NOT NULL,
    top_score       REAL,
    result_count    INTEGER,
    was_answered    BOOLEAN NOT NULL,
    trace_id        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_search_logs_was_answered ON search_logs (was_answered);

-- ---------------------------------------------------------------------------
-- updated_at trigger for documents
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_documents_updated_at ON documents;
CREATE TRIGGER trg_documents_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
