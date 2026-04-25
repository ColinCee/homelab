# ADR-016: Personal knowledge base — Postgres + pgvector

**Date:** 2026-04-19
**Status:** Accepted

## Context

Personal notes (~195 markdown files) live in a private GitHub repo
(`ColinCee/notes`). Finding information requires remembering which file
something is in and searching manually. The goal: semantic search over all notes
so the Copilot CLI agent (and eventually any Tailscale device) can retrieve
relevant context from personal writing.

Requirements:

- **Semantic search** — find notes by meaning, not just keywords
- **Chinese language support** — user is learning Chinese, notes contain mixed languages
- **Security** — notes contain personal and work-sensitive information, Tailscale-only access
- **Low resource usage** — Beelink has 16 GB RAM shared across all services
- **Rebuildable** — the vector index is derived data; the notes repo is the source of truth

## Options Considered

### Option A: Full knowledge management app (Khoj, AnythingLLM, Open WebUI)

Evaluated Khoj (agentic, code execution, automations), AnythingLLM (~2 GB RAM),
and Open WebUI (chat-focused). Khoj was the top choice until discovering
app.khoj.dev had been sunset.

All three bundle their own storage, embedding pipeline, and UI — most of which
we don't need. The Copilot CLI is already the chat interface. Adding another app
means maintaining a second set of auth, updates, and resource usage for features
that overlap with what we have.

**Verdict:** Rejected. Too much surface area for the actual need (vector
storage + search).

### Option B: Dedicated vector database (Qdrant, Chroma, LanceDB)

Purpose-built for embeddings. Lightweight, good APIs. But they're a
single-purpose service — if we later want relational queries, full-text search,
or metadata filtering alongside vectors, we'd need a second database.

**Verdict:** Rejected. Postgres does vectors well enough via pgvector, and we
avoid running a separate service.

### Option C: Postgres 17 + pgvector (chosen)

Postgres with the pgvector extension. One container handles both relational
storage (documents, metadata, content hashes) and vector similarity search
(HNSW index, cosine distance, halfvec for 3072-dim embeddings). Every knowledge management tool evaluated
(Khoj, AnythingLLM, Open WebUI) supports Postgres as a backend — so if we
ever add a UI, it can plug into the existing database.

| Feature | pgvector | Qdrant | Chroma |
|---------|----------|--------|--------|
| Vector search | ✅ HNSW + cosine (halfvec) | ✅ native | ✅ native |
| Relational queries | ✅ full SQL | ❌ | ❌ |
| Metadata filtering | ✅ SQL WHERE | ✅ payload filters | ✅ where filters |
| RAM usage | ~100 MB | ~200 MB | ~150 MB |
| Ecosystem compat | ✅ universal | ⚠ growing | ⚠ Python-focused |

**Verdict:** Chosen. Universal backend, relational + vector in one container,
minimal RAM.

### Embedding model: text-embedding-3-large

Investigated Cohere embed-v4 (better multilingual, MTEB 65.2 vs 64.6) but
discovered it's **not available on GitHub Models API** — confirmed by querying
the actual catalog (43 models, only 2 embeddings). The only options via GitHub
Models are `openai/text-embedding-3-large` (3072 dims) and
`openai/text-embedding-3-small` (1536 dims).

Chose `text-embedding-3-large` for higher quality. Free via GitHub Models API
with a fine-grained PAT (`models:read` permission required since March 2025).
Rate-limited on the free tier (~10-50 RPM), which is fine for incremental
updates but slow for bulk ingestion (~12 min for 195 files). Content hashing
means re-runs skip unchanged files.

**Dimension constraint:** pgvector HNSW indexes support up to 2000 dimensions
for the `vector` type, but 3072 exceeds that limit. Using `halfvec(3072)`
(half-precision float16) instead — HNSW supports up to 4000 dimensions on
`halfvec`. The precision loss is negligible for cosine similarity ranking.

### MCP vs Copilot CLI skill for integration

Investigated MCP (Model Context Protocol) servers as the integration path
between the CLI agent and the knowledge base. Built a test MCP server and
configured `~/.copilot/mcp.json`. **Enterprise policy blocks custom MCP
servers** — confirmed by testing: "Third-party MCP servers are disabled by your
organization's Copilot policy."

Pivoted to a Copilot CLI skill that SSHes to beelink and runs the search
container. Works today, no policy restrictions. Future path: HTTP search API
bound to Tailscale IP (issue #199).

### Containerized ingest vs bare-metal scripts

Initially built the ingest as bare-metal Python scripts run by the GitHub
Actions runner. E2E testing exposed 3 bugs caused by host environment
assumptions:

1. Runner PATH doesn't include mise shims (`uv: command not found`)
2. DB password special characters break URL parsing (`invalid percent-encoded token`)
3. DB bound to Tailscale IP, not localhost (`connection refused`)

Containerized the ingest as a Docker Compose profile service. The container
connects to Postgres via Docker service name, receives the COPILOT_GITHUB_TOKEN
as an env var, and mounts the notes directory as a read-only volume. All three
bugs became irrelevant — the container owns its own environment.

## Decision

- **Postgres 17 + pgvector** as the storage layer (`stacks/knowledge/`)
- **text-embedding-3-large** (3072 dims) via GitHub Models API
- **Containerized ingest** as a Docker Compose profile service — runs on push to
  notes repo via GitHub Actions, skips unchanged files via content hash
- **Copilot CLI skill** (`knowledge-search`) for agent access via SSH + Docker
- **Trust boundary**: only the human CLI session and the beelink runner have
  access. The homelab review/implement agent has no access to notes.

### Data flow

```
push to ColinCee/notes
  → GitHub Actions (beelink-notes runner)
  → scripts/ingest-notes.sh
  → docker compose --profile ingest run
  → chunk markdown → embed via GitHub Models API → store in pgvector

search query (CLI skill or direct)
  → ssh beelink → docker compose run ingest search "query"
  → embed query → pgvector cosine similarity → ranked results
```

## References

- Epic: #139 (sub-issues #140–#144)
- Containerization: PR #196
- Circuit breaker: PR #197
- Search skill: PR #198
- HTTP API (future): #199
- Runbook: `docs/runbooks/knowledge-base.md`
