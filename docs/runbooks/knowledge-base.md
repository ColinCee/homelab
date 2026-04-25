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

## Credential Lifecycle

The `save` profile uses a write deploy key at `~/.ssh/notes_deploy_key` on
beelink. The key is scoped to `ColinCee/notes` and mounted read-only into the
container; do not mount the whole host `~/.ssh` directory.

### Check deploy key metadata

Use this during routine maintenance or before deleting an old key. `last_used_at`
is a sanity check, not a complete compromise detector.

```bash
gh api repos/ColinCee/notes/keys \
  --jq '.[] | [.id, .title, .read_only, .created_at, (.last_used_at // "never")] | @tsv'
```

### Rotate the notes deploy key

Generate the replacement key on beelink:

```bash
ssh beelink 'ssh-keygen -t ed25519 -N "" -C "knowledge-save-$(date +%Y-%m-%d)" -f ~/.ssh/notes_deploy_key.next && chmod 600 ~/.ssh/notes_deploy_key.next'
ssh beelink 'cat ~/.ssh/notes_deploy_key.next.pub'
```

Add the printed public key to `ColinCee/notes` as a **write** deploy key in
GitHub: **Settings -> Deploy keys -> Add deploy key -> Allow write access**.

Replace the active key on beelink after the new key exists in GitHub:

```bash
ssh beelink 'mv ~/.ssh/notes_deploy_key ~/.ssh/notes_deploy_key.old.$(date +%Y%m%d) && mv ~/.ssh/notes_deploy_key.next ~/.ssh/notes_deploy_key && chmod 600 ~/.ssh/notes_deploy_key'
```

Verify the container can authenticate without writing to the notes repo:

```bash
ssh beelink "cd /home/colin/code/homelab/stacks/knowledge && docker compose --profile save run --rm --entrypoint git save ls-remote git@github.com:ColinCee/notes.git HEAD"
```

After verification, delete the old deploy key from GitHub. Use the metadata
command above to find the old key id, then:

```bash
gh api -X DELETE repos/ColinCee/notes/keys/<old-key-id>
ssh beelink 'rm -f ~/.ssh/notes_deploy_key.old.* ~/.ssh/notes_deploy_key.next.pub'
```

### Respond to a suspected deploy key leak

1. Delete the deploy key from `ColinCee/notes` immediately.
2. Move the old private key aside on beelink and generate a replacement using
   the rotation procedure above.
3. Review recent notes repo commits and deploy-key `last_used_at` metadata.
4. Restore the notes repo from git history or backup if unexpected commits,
   branch deletions, or force-pushes occurred.

### Refresh GitHub SSH host keys

The knowledge image writes `/home/user/.ssh/known_hosts` at build time with
`ssh-keyscan github.com`. If GitHub rotates SSH host keys, first confirm the new
fingerprints against GitHub's published documentation, then rebuild the save
image and verify SSH authentication:

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
