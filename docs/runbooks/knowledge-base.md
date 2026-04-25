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
- **Backups** run nightly via `knowledge-backup.timer` to `/home/colin/backups/knowledge` with 14-day retention

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

### Save a web page to notes

The save profile fetches the page, writes it into the notes repo, commits, and
pushes to `main` using the repo-scoped deploy key from ADR-017.

```bash
ssh beelink "cd /home/colin/code/homelab/stacks/knowledge && docker compose --profile save run --rm save save \"<URL>\""
```

### Inspect recent task runs

- **Grafana:** `Container Overview` → `Knowledge Task Runs`
- **Loki query:**

  ```logql
  {job="knowledge"} | json | event = `task_completed`
  ```
- **Alerts:** failed runs page the existing Discord Private contact point via Grafana alerting

### Inspect database backups

```bash
# Timer status on beelink
ssh beelink "systemctl --user list-timers knowledge-backup.timer"
ssh beelink "journalctl --user -u knowledge-backup.service -n 50"
ssh beelink "ls -lh /home/colin/backups/knowledge/knowledge-*.dump"
```

### Back up the database now

Nightly backups are installed with the knowledge stack. Dumps are stored outside
the git repo and Docker volume at `/home/colin/backups/knowledge`, retained for
14 days, and validated with `pg_restore --list` before being kept. Run one
manually before schema work, embedding changes, or risky ingest fixes:

```bash
ssh beelink "cd /home/colin/code/homelab && scripts/backup-knowledge-db.sh"
```

Override the location or retention for one-off runs if needed:

```bash
ssh beelink "cd /home/colin/code/homelab && KNOWLEDGE_BACKUP_DIR=/path/to/backups KNOWLEDGE_BACKUP_RETENTION_DAYS=30 scripts/backup-knowledge-db.sh"
```

### Restore from a database backup

Use this when the database volume is corrupted, a bad ingest needs rollback, or
GitHub Models is unavailable and re-ingest would not work. Pick the backup file
first:

```bash
ssh beelink "ls -1t /home/colin/backups/knowledge/knowledge-*.dump | head"
```

Then restore it:

```bash
ssh beelink
cd /home/colin/code/homelab
BACKUP=/home/colin/backups/knowledge/knowledge-<timestamp>.dump
docker compose -f stacks/knowledge/compose.yaml exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --maintenance-db=postgres --force --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose -f stacks/knowledge/compose.yaml exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl' < "$BACKUP"
```

### Re-ingest everything from scratch

Wipe derived data and re-ingest all files. This depends on the notes repo,
GitHub Models API, and the configured embedding model all being available.
Prefer restoring a backup when recovering from a bad ingest and the old vectors
are still valid.

```bash
# Drop and recreate the database
ssh beelink "docker exec knowledge-postgres-1 psql -U knowledge -d knowledge -c 'DELETE FROM note_links; DELETE FROM chunks; DELETE FROM documents;'"

# Re-ingest
ssh beelink "cd /home/colin/code/homelab && bash scripts/ingest-notes.sh"
```

Full re-ingest takes ~12 minutes under current GitHub Models limits. Tighter
rate limits or model outages make this slower or unavailable. Incremental runs
skip unchanged files via content hash and finish in ~30 seconds.

If `openai/text-embedding-3-large` is retired, backups preserve the existing
search index while a separate model/schema migration is planned.

### Check database health

```bash
# Document and chunk counts
ssh beelink "docker exec knowledge-postgres-1 psql -U knowledge -d knowledge -c 'SELECT count(*) as docs, (SELECT count(*) FROM chunks) as chunks FROM documents;'"

# Most recently ingested documents
ssh beelink "docker exec knowledge-postgres-1 psql -U knowledge -d knowledge -c 'SELECT source_path, ingested_at FROM documents ORDER BY ingested_at DESC LIMIT 10;'"

# Postgres container status
ssh beelink "docker ps --filter name=knowledge-postgres"
```

## Credential Lifecycle

The `save` profile uses a write deploy key at `~/.ssh/notes_deploy_key` on
beelink. The key is scoped to `ColinCee/notes` and mounted read-only into the
container; do not mount the whole host `~/.ssh` directory. Rotate it after host
compromise, accidental disclosure, or moving the save workflow to a new machine.

### Rotate the notes deploy key

Generate a replacement key on beelink and print the public key:

```bash
ssh beelink 'ssh-keygen -t ed25519 -N "" -C "knowledge-save-$(date +%Y-%m-%d)" -f ~/.ssh/notes_deploy_key.next && chmod 600 ~/.ssh/notes_deploy_key.next'
ssh beelink 'cat ~/.ssh/notes_deploy_key.next.pub'
```

Add the printed public key to `ColinCee/notes` as a **write** deploy key in
GitHub: **Settings -> Deploy keys -> Add deploy key -> Allow write access**.
Then swap the host key and verify that the container can authenticate without
writing to the notes repo:

```bash
ssh beelink 'mv ~/.ssh/notes_deploy_key ~/.ssh/notes_deploy_key.old.$(date +%Y%m%d) && mv ~/.ssh/notes_deploy_key.next ~/.ssh/notes_deploy_key && chmod 600 ~/.ssh/notes_deploy_key'
ssh beelink "cd /home/colin/code/homelab/stacks/knowledge && docker compose --profile save run --rm --entrypoint git save ls-remote git@github.com:ColinCee/notes.git HEAD"
```

After verification, delete the old deploy key from GitHub and clean up the
temporary files:

```bash
gh api repos/ColinCee/notes/keys --jq '.[] | [.id, .title, .read_only, (.last_used_at // "never")] | @tsv'
gh api -X DELETE repos/ColinCee/notes/keys/<old-key-id>
ssh beelink 'rm -f ~/.ssh/notes_deploy_key.old.* ~/.ssh/notes_deploy_key.next.pub'
```

### Refresh GitHub SSH host keys

The knowledge image writes `/home/user/.ssh/known_hosts` at build time with
`ssh-keyscan github.com`. If GitHub rotates SSH host keys, confirm the new
fingerprints against GitHub's published documentation, then rebuild and verify:

```bash
ssh beelink "cd /home/colin/code/homelab/stacks/knowledge && docker compose build --no-cache save && docker compose --profile save run --rm --entrypoint git save ls-remote git@github.com:ColinCee/notes.git HEAD"
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
