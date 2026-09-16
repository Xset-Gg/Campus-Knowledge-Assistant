# Campus Knowledge Assistant

A production-oriented RAG application that answers student and faculty questions
from university policies and syllabi, with citations down to the page, and
role-based access control enforced in the database query itself.

The design goal is not "a chatbot that talks about policy" but **an assistant
that is either correct and cited, or silent**. Two properties follow from that:

- **It declines.** If reranking says nothing relevant was retrieved, or the
  generated answer carries no valid citation, the system returns a refusal
  instead of an answer. A confident wrong answer about a withdrawal deadline is
  worse than no answer.
- **It cannot over-share.** Access filtering is part of the retrieval SQL, not a
  post-filter. A student's query never scores an admin-only policy draft as a
  candidate, so there is no code path in which one can leak.

---

## Table of contents

- [Architecture](#architecture)
- [Project structure](#project-structure)
- [Quick start (Docker)](#quick-start-docker)
- [Local development](#local-development)
- [Ingesting documents](#ingesting-documents)
- [How retrieval works](#how-retrieval-works)
- [Role-based access control](#role-based-access-control)
- [Evaluation](#evaluation)
- [Observability](#observability)
- [API reference](#api-reference)
- [Configuration](#configuration)
- [Extending the system](#extending-the-system)
- [Production checklist](#production-checklist)

---

## Architecture

```
                 ┌──────────────────────────────────────────┐
  PDFs  ────────▶│  Ingestion (Docling → chunk → embed)     │
                 └───────────────────┬──────────────────────┘
                                     ▼
                 ┌──────────────────────────────────────────┐
                 │  PostgreSQL 16 + pgvector                │
                 │  chunks(embedding vector, content_tsv)   │
                 └───────────────────┬──────────────────────┘
                                     ▼
  Question ─▶ ┌──────────────────────────────────────────────────┐
              │  Hybrid retrieval  (RBAC filter applied in SQL)  │
              │    • semantic:  embedding <=> query  (cosine)    │
              │    • lexical:   content_tsv @@ websearch_query   │
              │    • fusion:    reciprocal rank fusion           │
              │    • temporal:  boost current academic year      │
              └───────────────────────┬──────────────────────────┘
                                      ▼  top 25
              ┌──────────────────────────────────────────────────┐
              │  Cross-encoder reranker  →  top 5                │
              └───────────────────────┬──────────────────────────┘
                                      ▼
                          confidence < threshold ? ──▶ "I don't know"
                                      ▼
              ┌──────────────────────────────────────────────────┐
              │  Generation (Claude / GPT) — grounded, cited     │
              │  → validate every [n] against supplied context   │
              │  → zero valid citations ⇒ downgrade to refusal   │
              └───────────────────────┬──────────────────────────┘
                                      ▼
                        Answer + citation cards (Next.js)
```

Every stage emits a Langfuse span on one trace, so a bad answer can be traced
from the question to the exact chunks the model saw.

### Stack

| Layer | Choice |
|---|---|
| PDF parsing | Docling (layout-aware, preserves headings + page provenance) |
| Store | PostgreSQL 16 + pgvector (HNSW) + native full-text search |
| Embeddings | `all-MiniLM-L6-v2` (384-dim, normalized) |
| Reranking | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| LLM | Anthropic Claude (default) or OpenAI — pluggable adapter |
| Backend | FastAPI, async SQLAlchemy 2.0, JWT auth |
| Frontend | Next.js 14 App Router + Tailwind |
| Evaluation | Ragas + a deterministic RBAC/abstention suite |
| Tracing | Langfuse |

**One database, not two.** Vectors and full-text indexes live in the same
Postgres instance as the metadata, which means RBAC filters, recency filters and
both retrieval modes compose in a single query. A separate vector store would
force filtering to happen in application code after retrieval — slower, and a
place for access-control bugs to hide.

---

## Project structure

```
Campus-Knowledge-Assistant/
├── docker-compose.yml
├── .env.example
├── sql/
│   └── schema.sql                  # pgvector setup, tables, indexes, triggers
├── data/
│   ├── raw_pdfs/                   # source documents (gitignored)
│   └── sample_docs/
│       └── manifest.example.json   # ingestion manifest format
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py                 # FastAPI app, middleware, health probes
│   │   ├── config.py               # typed settings from environment
│   │   ├── db.py                   # async engine + session dependency
│   │   ├── models.py               # SQLAlchemy ORM
│   │   ├── schemas.py              # Pydantic request/response models
│   │   ├── auth.py                 # JWT, password hashing, RBAC dependencies
│   │   ├── permissions.py          # role → access-level / capability matrix
│   │   ├── routers/
│   │   │   ├── auth_router.py
│   │   │   ├── chat.py             # /chat/ask, /chat/search, history
│   │   │   ├── documents.py        # catalogue, upload, metadata management
│   │   │   ├── feedback.py         # thumbs up/down
│   │   │   └── admin.py            # analytics, failed searches, users
│   │   ├── services/
│   │   │   ├── search_service.py   # hybrid search + RRF + RBAC SQL
│   │   │   ├── rerank_service.py   # cross-encoder + confidence scoring
│   │   │   ├── generation_service.py  # grounded generation + citation checks
│   │   │   ├── llm_client.py       # Anthropic / OpenAI adapters
│   │   │   ├── embedding_service.py
│   │   │   └── rag_pipeline.py     # the single orchestrator
│   │   └── observability/
│   │       └── tracing.py          # Langfuse spans, scores, no-op fallback
│   ├── ingestion/
│   │   ├── ingest.py               # CLI: file / directory / manifest
│   │   ├── parsers.py              # Docling → structured blocks
│   │   └── chunking.py             # section-aware chunking
│   ├── evaluation/
│   │   ├── ragas_evaluation.py     # Ragas + deterministic metrics
│   │   └── golden_dataset.json     # 64 QA pairs across 11 departments
│   ├── scripts/
│   │   └── seed_users.py
│   └── tests/
│       ├── test_rbac.py
│       └── test_generation.py
└── frontend/
    ├── Dockerfile
    ├── package.json
    ├── app/
    │   ├── layout.tsx
    │   ├── page.tsx                # chat
    │   ├── login/page.tsx
    │   ├── documents/page.tsx
    │   └── admin/page.tsx
    ├── components/
    │   ├── AppShell.tsx            # nav + role-aware links
    │   ├── ChatWindow.tsx
    │   ├── MessageBubble.tsx
    │   ├── CitationCard.tsx        # clickable, deep-links to the PDF page
    │   └── FeedbackButtons.tsx
    ├── lib/
    │   ├── api.ts
    │   └── auth-context.tsx
    └── types/api.ts
```

---

## Quick start (Docker)

**Prerequisites:** Docker with Compose v2, an API key for Anthropic or OpenAI,
and ~4 GB free disk (the backend image bakes in the embedding + reranker
weights).

```bash
git clone <your-repo-url> Campus-Knowledge-Assistant
cd Campus-Knowledge-Assistant

cp .env.example .env
```

Edit `.env` — at minimum set these three:

```bash
POSTGRES_PASSWORD=<a strong password>
JWT_SECRET_KEY=<paste: openssl rand -hex 32>
ANTHROPIC_API_KEY=<your key>          # or OPENAI_API_KEY with LLM_PROVIDER=openai
```

Then:

```bash
docker compose up --build -d      # first build takes ~5 min (model download)
docker compose logs -f backend    # wait for "Models ready"
```

Create the demo accounts:

```bash
docker compose exec backend python -m scripts.seed_users
```

Add documents and ingest them:

```bash
cp /path/to/your/*.pdf data/raw_pdfs/

docker compose exec backend python -m ingestion.ingest \
  --directory data/raw_pdfs \
  --department "Computer Science" \
  --academic-year 2026 \
  --access-level student
```

Open **http://localhost:3000** and sign in as
`student@university.edu` / `student-change-me`.

- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health/ready

> The seeded passwords are development defaults. `seed_users.py` refuses to use
> them when `ENVIRONMENT=production`; set `SEED_*_PASSWORD` instead.

---

## Local development

Run Postgres in Docker and the two apps on the host.

```bash
# 1. Database only
docker compose up -d db

# 2. Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL=postgresql://ckadmin:<password>@localhost:5432/campus_knowledge
export JWT_SECRET_KEY=$(openssl rand -hex 32)
export ANTHROPIC_API_KEY=<your key>

python -m scripts.seed_users
uvicorn app.main:app --reload --port 8000

# 3. Frontend (new terminal)
cd frontend
npm install
echo "NEXT_PUBLIC_API_BASE_URL=http://localhost:8000" > .env.local
npm run dev
```

Tests and checks:

```bash
cd backend  && python -m pytest tests/ -v
cd frontend && npm run typecheck && npm run build
```

If you are not using Docker for the database, apply the schema by hand:

```bash
psql "$DATABASE_URL" -f sql/schema.sql
```

---

## Ingesting documents

Three input modes. Metadata absent from the command line is inferred from the
filename (year via a `20xx` match, type via keywords such as `syllabus` or
`policy`), and `access_level` defaults to `student` — the safe default, never
`public`.

```bash
# One file, fully specified
python -m ingestion.ingest \
  --file data/raw_pdfs/cs101_syllabus.pdf \
  --title "CS101 Introduction to Computer Science" \
  --department "Computer Science" \
  --academic-year 2026 \
  --document-type syllabus \
  --access-level student \
  --source-url "https://university.edu/docs/cs101.pdf"

# A directory, shared metadata
python -m ingestion.ingest --directory data/raw_pdfs --department "Registrar" --academic-year 2026

# A manifest, per-file metadata (recommended for a real corpus)
python -m ingestion.ingest --manifest data/sample_docs/manifest.json
```

See `data/sample_docs/manifest.example.json` for the manifest format.

**Behaviour worth knowing:**

- **Deduplication** is by SHA-256 of the file. Re-running is a no-op; pass
  `--force` to re-ingest a changed file, which replaces its chunks atomically.
- **Failures are isolated.** A corrupt, encrypted, or scanned-image PDF marks
  that one document `failed` with the reason stored in `ingestion_error`; the
  rest of the batch continues. The run exits non-zero if anything failed.
- **`source_url` is what makes citations clickable** — the UI deep-links to
  `#page=N`. Without it you still get the title and page number, just no link.

### Chunking

Fixed-size chunking splits mid-sentence and mixes unrelated policy sections into
one chunk, which produces retrieval hits that cite the wrong rule. Instead,
chunks are grouped by heading path and only split when a section exceeds the
token budget:

- Each chunk is prefixed with its heading breadcrumb (`3. Grading > 3.2 Late
  Work`), so it is self-describing to the embedder and to the model.
- Splits happen on sentence boundaries with a ~64-token overlap.
- Chunks below the minimum size are merged forward, so a stray one-line heading
  never becomes a standalone retrieval result.
- Page numbers ride along from Docling's provenance data — this is what makes
  page-accurate citation possible.

---

## How retrieval works

**Why hybrid.** Semantic search alone misses exact identifiers: a student asking
about "CS101" gets generic intro-computing chunks, because the embedding of a
course code carries little meaning. Lexical search alone misses paraphrase:
"can I hand in work late" never matches a section titled "Assignment Submission
Deadlines". Running both and fusing the rankings recovers each one's blind spot.

Both retrievers run as CTEs in one query and are combined by **Reciprocal Rank
Fusion**:

```
score(chunk) = w_sem · 1/(k + rank_semantic) + w_kw · 1/(k + rank_keyword)
```

RRF combines *ranks*, not scores, which avoids having to normalize two
incomparable scales (cosine distance vs. `ts_rank_cd`). `k = 60` damps the
influence of the very top ranks so one retriever cannot dominate. Set
`HYBRID_SEMANTIC_WEIGHT` / `HYBRID_KEYWORD_WEIGHT` to tune the balance, or
switch to weighted score fusion via the `strategy` argument.

**Temporal relevance** is applied as a multiplier on the fused score, so that on
an otherwise equal match the current year wins:

| Document year | Multiplier |
|---|---|
| current (`CURRENT_ACADEMIC_YEAR`) or later | 1.00 |
| one year old | 0.85 |
| two years old | 0.70 |
| older | 0.55 |

Documents marked `is_current = false` are excluded entirely unless the caller
opts in with `include_outdated`. Anything cited from a previous year is flagged
in the answer's citation card, and `GET /admin/outdated-documents` lists current
documents that have fallen behind the academic year.

**Reranking.** The bi-encoder used for retrieval embeds query and passage
independently, so it measures topical similarity only. The cross-encoder reads
the pair jointly and scores real relevance, which reliably reorders the top-k:
the passage that merely mentions "attendance" sinks below the one that states
the attendance rule. Retrieve 25, rerank to 5.

**Abstention.** Confidence is `0.7 · best + 0.3 · mean(top 3)` over the
cross-encoder scores — weighted toward the best passage, because one strongly
relevant source is enough to answer whereas several weak ones usually mean the
corpus does not cover the question. Below `ANSWER_CONFIDENCE_THRESHOLD` the LLM
is never called. After generation, every `[n]` marker is checked against the
context actually supplied; invented markers are stripped, and an answer left
with zero valid citations is downgraded to a refusal.

---

## Role-based access control

Three roles, four document access levels:

| Role | public | student | faculty | admin |
|---|:---:|:---:|:---:|:---:|
| `student` | ✅ | ✅ | ❌ | ❌ |
| `professor` | ✅ | ✅ | ✅ | ❌ |
| `admin` | ✅ | ✅ | ✅ | ✅ |

Enforcement rules the codebase holds to:

1. **Filtering happens in SQL.** `_build_access_filter` injects
   `c.access_level = ANY(:allowed_levels)` into *both* retrieval CTEs. A
   restricted chunk is never a fusion candidate, never reaches the reranker, and
   never reaches the model.
2. **One pipeline.** Chat, search, the admin debug endpoint and the evaluation
   harness all call `answer_question`. There is no second code path that could
   drift out of sync with the filter.
3. **The token is not the authority.** `get_current_user` re-loads the user from
   the database on every request, so a role change or a deactivation takes
   effect immediately rather than when the token happens to expire.
4. **No privilege escalation on write.** Self-registration is hard-coded to
   `student`. An uploader cannot publish a document at an access level they
   could not themselves read. An admin cannot demote or deactivate themselves.
5. **404, not 403,** for a document the caller may not read — a 403 would
   confirm the document exists.
6. **The UI hides, the API enforces.** `GET /auth/me/capabilities` drives which
   nav links render, but every endpoint independently re-checks.

Capabilities by role:

| Capability | student | professor | admin |
|---|:---:|:---:|:---:|
| `ask`, `feedback`, `view_history` | ✅ | ✅ | ✅ |
| `view_faculty_docs`, `upload_documents` | | ✅ | ✅ |
| `manage_documents`, `view_analytics`, `view_failed_searches`, `manage_users`, `debug_retrieval` | | | ✅ |

---

## Evaluation

```bash
cd backend

python -m evaluation.ragas_evaluation                      # full suite
python -m evaluation.ragas_evaluation --skip-ragas         # deterministic only, no LLM cost
python -m evaluation.ragas_evaluation --category rbac_probe
python -m evaluation.ragas_evaluation --department "Computer Science"
python -m evaluation.ragas_evaluation --output results.json
```

The golden set (`evaluation/golden_dataset.json`) has **64 cases** across 11
departments and five document types, including three adversarial categories most
RAG evaluations omit:

| Category | Count | What it catches |
|---|---:|---|
| `factual`, `procedural`, `policy_interpretation` | 40 | ordinary correctness |
| `exact_match_code` | 3 | course/policy codes — lexical retrieval |
| `temporal`, `temporal_conflict` | 7 | answering from the current year's document |
| `multi_hop` | 2 | combining two departments' policies |
| `abstention` | 5 | **should refuse** — not in the corpus |
| `rbac_probe` | 4 | **must not retrieve** restricted content |
| `faculty_only` | 3 | professor-visible content |

**Metrics.** Ragas scores only the cases the system chose to answer —
faithfulness and relevancy are undefined for a deliberate refusal, and including
refusals would penalize the behaviour we want:

- **faithfulness** — is every claim supported by the retrieved context? The
  hallucination detector, and the metric that matters most here.
- **answer_relevancy** — does the answer address the question asked?
- **context_precision** — are retrieved chunks relevant and well ranked? Low
  precision indicts the reranker.
- **context_recall** — did retrieval find everything the answer needed? Low
  recall indicts chunking or the corpus.

Alongside those, deterministic checks run on **every** case with no LLM judge:
abstention accuracy (reported separately, because a system that answers
everything scores well on relevancy and is still dangerous), RBAC leakage,
citation validity, and p50/p95 latency.

**Exit codes for CI:** `0` pass, `2` an RBAC violation, `3` too many cases
answered that should have been declined. A single RBAC leak fails the run
regardless of every other score.

Suggested gates once you have a real corpus: faithfulness ≥ 0.90, answer
relevancy ≥ 0.85, context precision ≥ 0.75, context recall ≥ 0.80, abstention
accuracy ≥ 0.95, RBAC violations = 0.

> The shipped ground truths are written against the sample corpus described in
> `data/sample_docs/`. Replace them with answers from your own documents before
> reading the scores as meaningful.

---

## Observability

Set these to enable Langfuse:

```bash
ENABLE_TRACING=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

Each request produces one trace with `retrieval`, `rerank` and `generation`
spans, plus token counts, model name and latency. Thumbs up/down are attached to
the originating trace as scores, so answer quality lines up with the retrieval
that produced it.

Tracing is fully optional: with keys absent every call becomes a no-op and the
request path is unaffected. Observability must never be able to take down the
API.

Independently of Langfuse, `search_logs` records every question with its
confidence and whether it was answered. Unanswered questions are the most
valuable signal the system produces — each is either a retrieval bug or a
genuine gap in the published corpus — and `GET /admin/failed-searches/top`
ranks them by frequency.

---

## API reference

Interactive docs at `/docs` (disabled when `ENVIRONMENT=production`).

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/auth/register` | — | Self-registration (student only) |
| POST | `/auth/login` | — | Exchange credentials for a JWT |
| GET | `/auth/me` | any | Current user |
| GET | `/auth/me/capabilities` | any | Capabilities for the UI |
| POST | `/chat/ask` | any | **Ask a question** → answer + citations |
| POST | `/chat/search` | any | Hybrid search, no generation |
| GET | `/chat/sessions` | any | Own chat sessions |
| GET | `/chat/sessions/{id}/messages` | owner | Conversation history |
| POST | `/chat/debug/retrieve` | admin | Fusion ranks + rerank scores |
| GET | `/documents` | any | Catalogue, filtered by role |
| GET | `/documents/departments` | any | Departments visible to caller |
| POST | `/documents/upload` | professor+ | Upload and queue ingestion |
| PATCH | `/documents/{id}` | admin | Edit metadata (propagates to chunks) |
| DELETE | `/documents/{id}` | admin | Delete document and chunks |
| POST | `/feedback` | any | Thumbs up/down on an answer |
| GET | `/admin/analytics` | admin | Usage and quality summary |
| GET | `/admin/failed-searches` | admin | Unanswered questions |
| GET | `/admin/failed-searches/top` | admin | Ranked content gaps |
| GET | `/admin/outdated-documents` | admin | Current docs from older years |
| GET | `/admin/users` | admin | List users |
| PATCH | `/admin/users/{id}/role` | admin | Change a role |
| GET | `/health`, `/health/ready` | — | Liveness / readiness |

Example:

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"student@university.edu","password":"student-change-me"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -s -X POST localhost:8000/chat/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is the late submission penalty?"}' | python3 -m json.tool
```

---

## Configuration

All settings come from the environment; see `.env.example` for the full list.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | Postgres connection string |
| `JWT_SECRET_KEY` | — | **Required.** `openssl rand -hex 32` |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Token lifetime |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `LLM_EFFORT` | `medium` | Thinking depth: `low`…`max` |
| `LLM_MAX_TOKENS` | `2048` | Answers are deliberately short |
| `MAX_CONTEXT_TOKENS` | `24000` | Context budget, enforced by token counting |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Must match `EMBEDDING_DIM` |
| `EMBEDDING_DIM` | `384` | **Must match `vector(N)` in `schema.sql`** |
| `RERANKER_MODEL` | `ms-marco-MiniLM-L-6-v2` | Cross-encoder |
| `RETRIEVAL_TOP_K` | `25` | Candidates before reranking |
| `RERANK_TOP_K` | `5` | Chunks given to the model |
| `ANSWER_CONFIDENCE_THRESHOLD` | `0.35` | Below this, refuse to answer |
| `HYBRID_SEMANTIC_WEIGHT` / `HYBRID_KEYWORD_WEIGHT` | `0.6` / `0.4` | Fusion balance |
| `RRF_K` | `60` | RRF damping constant |
| `CURRENT_ACADEMIC_YEAR` | `2026` | Drives temporal boosting |
| `ENABLE_TRACING` | `false` | Langfuse on/off |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated |
| `ENVIRONMENT` | `development` | `production` disables `/docs` |

**Tuning notes.** Raise `ANSWER_CONFIDENCE_THRESHOLD` if the assistant answers
questions it should decline; lower it if it refuses questions the corpus clearly
covers — use `--category abstention` to measure rather than guess. Raise
`RETRIEVAL_TOP_K` for recall at some latency cost. Raise `HYBRID_KEYWORD_WEIGHT`
if your corpus is dense with course and policy codes.

### Changing the embedding model

`EMBEDDING_DIM` must match the `vector(N)` column in `sql/schema.sql`, and
changing models invalidates every stored embedding:

```sql
ALTER TABLE chunks DROP COLUMN embedding;
ALTER TABLE chunks ADD COLUMN embedding vector(768);   -- your model's dimension
CREATE INDEX idx_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
```

Then re-ingest with `--force`.

---

## Extending the system

**A new document type** — add a value to the `document_type` enum in
`sql/schema.sql` and to `DocumentType` in `app/models.py`. Nothing else reads
the type exhaustively, so ingestion, filtering and the UI pick it up
automatically.

**A new access level** — add it to the `access_level` enum and to
`ROLE_ACCESS_LEVELS` in `app/permissions.py`. That table is the single
definition of who may read what; the SQL filter is generated from it.

**A different LLM** — implement the `LLMClient` protocol in
`app/services/llm_client.py` (two methods: `complete`, `count_tokens`) and add a
branch to `get_llm_client`. Nothing above that layer depends on a vendor SDK.

**A different file format** — Docling already handles DOCX, HTML, Markdown and
PPTX. Add the suffix to `ALLOWED_SUFFIXES` in `app/routers/documents.py`;
`parse_pdf` handles any Docling-supported input.

**A new retrieval signal** — add it as a CTE in `_HYBRID_SQL_TEMPLATE` and
extend the fusion expression. Keep the access filter in the new CTE.

---

## Production checklist

Before exposing this to real students:

- [ ] `JWT_SECRET_KEY` is a fresh 32-byte random value held in a secret manager
- [ ] `ENVIRONMENT=production` (disables `/docs`)
- [ ] Demo accounts removed or given real passwords via `SEED_*_PASSWORD`
- [ ] `CORS_ORIGINS` restricted to your actual frontend origin
- [ ] TLS terminated at a reverse proxy; the API is not exposed directly
- [ ] Rate limiting on `/auth/login` and `/chat/ask`
- [ ] Postgres backups configured, and `pgdata` on durable storage
- [ ] `python -m evaluation.ragas_evaluation` green against your real corpus
- [ ] Every document's `access_level` reviewed — this is the setting that
      decides who can read what, and the ingestion default is `student`
- [ ] Alerting on the unanswered-question rate from `/admin/analytics`

**Known limitations.** Answers are non-streaming, so a long answer appears all
at once. Uploaded documents are ingested in a FastAPI background task, which
does not survive a restart — for a large corpus, move ingestion to a proper
queue. Scanned image PDFs need an OCR pass before Docling can extract text.
