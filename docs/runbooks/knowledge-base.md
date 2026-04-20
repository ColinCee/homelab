# Knowledge Base Operations

Personal knowledge base backed by Postgres + pgvector. Notes are ingested from the [notes repo](https://github.com/ColinCee/notes) and searchable via semantic similarity plus computed note links.

## Architecture

```
push to notes repo → GitHub Actions (beelink-notes runner) → ingest-notes.sh
    → docker compose --profile ingest run → embeds via GitHub Models API → pgvector
```

- **Postgres** runs permanently in `stacks/knowledge/compose.yaml`
- **Ingest** is an on-demand container (compose profile), not always running
- **Embeddings** use `openai/text-embedding-3-large` via `models.github.ai` (COPILOT_GITHUB_TOKEN)

## Common Operations

### Search the knowledge base

```bash
ssh beelink "cd /home/colin/code/homelab/stacks/knowledge && docker compose --profile ingest run --rm ingest search \"<query>\" --limit 5"
```

### Show related notes for a document

Returns both resolved `[[wikilinks]]` and embedding-similar documents.

```bash
ssh beelink "cd /home/colin/code/homelab/stacks/knowledge && docker compose --profile ingest run --rm ingest related \"/notes/path/to/note.md\""
```

### Trigger a manual ingest

From the notes repo GitHub Actions tab, or:

```bash
gh workflow run ingest.yaml --repo ColinCee/notes
```

Or directly on beelink:

```bash
ssh beelink "cd /home/colin/code/homelab && bash scripts/ingest-notes.sh"
```

### Inspect recent task runs

- **Grafana:** `Container Overview` → `Knowledge Task Runs`
- **Loki query:**

  ```logql
  {job="knowledge"} | json | event = `task_completed`
  ```
- **Alerts:** failed runs page the existing Discord Private contact point via Grafana alerting

### Re-ingest everything from scratch

Wipe the database and re-ingest all files. Useful if the schema changes or embeddings need regenerating.

```bash
# Drop and recreate the database
ssh beelink "docker exec knowledge-postgres-1 psql -U knowledge -d knowledge -c 'DELETE FROM note_links; DELETE FROM chunks; DELETE FROM documents;'"

# Re-ingest
ssh beelink "cd /home/colin/code/homelab && bash scripts/ingest-notes.sh"
```

Full re-ingest takes ~12 minutes (rate-limited by the embedding API). Incremental runs skip unchanged files via content hash and finish in ~30 seconds.

### Check database health

```bash
# Document and chunk counts
ssh beelink "docker exec knowledge-postgres-1 psql -U knowledge -d knowledge -c 'SELECT count(*) as docs, (SELECT count(*) FROM chunks) as chunks FROM documents;'"

# Most recently ingested documents
ssh beelink "docker exec knowledge-postgres-1 psql -U knowledge -d knowledge -c 'SELECT source_path, ingested_at FROM documents ORDER BY ingested_at DESC LIMIT 10;'"

# Postgres container status
ssh beelink "docker ps --filter name=knowledge-postgres"
```

## Troubleshooting

### Ingest fails with 403 Forbidden

The `COPILOT_GITHUB_TOKEN` PAT needs the **Models: Read** permission under Account permissions. Edit the token at https://github.com/settings/tokens.

### Ingest fails with 429 Too Many Requests

Rate-limited by the embedding API. The ingest retries 3 times per file with exponential backoff. A circuit breaker aborts after 5 consecutive failures. Re-running the ingest picks up where it left off (unchanged files are skipped).

### Ingest container can't connect to Postgres

The ingest container connects via Docker service name (`postgres`), not Tailscale IP. Check that the postgres container is running and healthy:

```bash
ssh beelink "docker ps --filter name=knowledge-postgres"
```

### Search returns duplicates

Old bare-metal ingest paths (`/home/colin/code/notes/...`) may coexist with container paths (`/notes/...`). Clean up:

```bash
ssh beelink "docker exec knowledge-postgres-1 psql -U knowledge -d knowledge -c \"DELETE FROM documents WHERE source_path LIKE '/home/colin%';\""
```
