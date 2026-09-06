# Knowledge search

The private notes Git repository is the source of truth. Postgres + pgvector
stores a derived search index: relational metadata, keyword search, and vectors
in one service rather than a separate search platform.

## Run commands

Open a server shell first. Examples below run on Beelink, avoiding nested local
and remote shell quoting:

```bash
ssh beelink
cd /home/colin/code/homelab
```

Postgres runs continuously; ingest and save are on-demand Compose profiles.
The notes repository's workflow calls `scripts/ingest-notes.sh` on push.
That script pulls the notes checkout with `--ff-only`, then ingests its
read-only `/notes` mount. Embeddings are sent to GitHub Models.

```bash
# Search or follow related notes; quote queries as shell data.
docker compose -f stacks/knowledge/compose.yaml --profile ingest run --rm ingest search 'query' --limit 5
docker compose -f stacks/knowledge/compose.yaml --profile ingest run --rm ingest related '/notes/path/to/note.md'

# Pull and ingest the notes checkout; unchanged content hashes are skipped.
scripts/ingest-notes.sh

# Save a web page: writes a note, commits, and pushes to notes/main.
docker compose -f stacks/knowledge/compose.yaml --profile save run --rm save save 'https://example.com/page'
```

Only save or ingest when intended: these are write operations, not search.
For other CLI options, append `--help` to the container command.

## Privacy and access

Place `.noindex` in a directory to exclude its subtree from bulk ingestion.
Explicit single-file ingestion also refuses excluded files before reading
content or calling embeddings. A completed full-directory ingest removes
previously indexed documents from excluded subtrees; adding the marker alone
does not erase existing database records or backups.

Private homelab records use `notes/areas/homelab/.noindex`. This is not encryption
or filesystem access control. A private Git repository and Tailscale-only
database access do not prevent non-excluded content being sent to the embedding
provider. Keep actual credentials in Bitwarden, not tracked notes.

The save container mounts only `~/.ssh/notes_deploy_key`, scoped to the notes
repository, rather than all host SSH identities. Read-only mounting prevents
file modification, not key theft. This key has no automatic expiry and can
perform destructive pushes unless repository rules prohibit them. Restrict
access to the host and keep recoverable copies of the notes.

## Change retrieval or schema

| Surface | Where and why |
|---------|---------------|
| CLI and ingestion | `stacks/knowledge/app/knowledge/`: container execution avoids host Python/PATH dependencies; Docker DNS connects to Postgres. |
| Embeddings | `embeddings.py` and `models.py` own provider, model, and dimensions. The configured model is `openai/text-embedding-3-large`. |
| Storage | `stacks/knowledge/init.sql` bootstraps new databases; `migrations/` upgrades existing ones. CLI commands rerun migration SQL, so changes must be idempotent. |
| Ranking | `database.py` combines vector, strict/relaxed English FTS, and Chinese FTS using reciprocal rank fusion, avoiding calibration of incompatible raw scores. |
| Chinese text | `tokenize.py` uses Jieba with Postgres `simple` FTS. English FTS alone misses unspaced Chinese; preserve this lexical path. |

The 3072-dimension embeddings use `halfvec` because they exceed pgvector's
2000-dimension HNSW limit for `vector`. Changing models requires a deliberate
schema and full re-embedding plan; equal dimensions do not make models
interchangeable.

Before changing retrieval, use the existing app tests and
`tests/fixtures/chinese_retrieval_eval_queries.json` under the app directory.
Preserve English, Chinese, and mixed-language behavior rather than adding a
new search engine without evidence. Root `mise.toml` owns test commands;
integration tests require an explicitly configured live Postgres database.

## Back up and recover

The user-level `knowledge-backup.timer` runs nightly. Dumps live outside the
Docker volume at `/home/colin/backups/knowledge`, with 14-day retention.
These host-local backups help with bad ingestion or volume loss, not loss of
the whole host. Treat dumps as private note content.

```bash
systemctl --user list-timers knowledge-backup.timer
journalctl --user -u knowledge-backup.service -n 50
scripts/backup-knowledge-db.sh
ls -lt /home/colin/backups/knowledge/
```

The script checks dump readability with `pg_restore --list` before keeping it;
that is not a full restore test. `KNOWLEDGE_BACKUP_DIR` and
`KNOWLEDGE_BACKUP_RETENTION_DAYS` override location and retention.

### Restore a dump

This replaces the database. Pause ingestion/save jobs and the backup timer,
stop active ingest/save containers, and take a pre-change dump if possible.
Choose an exact existing backup and inspect it **before** dropping anything:

```bash
BACKUP='/home/colin/backups/knowledge/knowledge-<timestamp>.dump'
test -s "$BACKUP" &&
  docker compose -f stacks/knowledge/compose.yaml exec -T postgres pg_restore --list < "$BACKUP"
```

Proceed only if that succeeds and the selected backup is the intended one:

```bash
docker compose -f stacks/knowledge/compose.yaml exec -T postgres sh -c \
  'dropdb -U "$POSTGRES_USER" --maintenance-db=postgres --force --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"' &&
docker compose -f stacks/knowledge/compose.yaml exec -T postgres sh -c \
  'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl' < "$BACKUP"
```

Inspect document counts and command behavior before resuming paused jobs.
Re-enable the backup timer afterward.

### Rebuild the index

Prefer a backup for recovery. Re-ingestion requires the notes, embedding API,
and configured model to be available. A backup preserves vectors during an API
outage, but normal search still needs the API to embed the query.

Only after backing up and confirming a rebuild is possible, pause writers and
clear derived records in one transaction:

```bash
docker compose -f stacks/knowledge/compose.yaml exec -T postgres sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "BEGIN; DELETE FROM note_links; DELETE FROM chunks; DELETE FROM documents; COMMIT;"'
scripts/ingest-notes.sh
```

Resume normal jobs after successful ingestion. Do not clear the index to fix
an unavailable API.

## Rotate save credentials

Rotate after suspected disclosure/compromise or moving hosts. For suspected
compromise, revoke the old key immediately; do not wait for a replacement.
For routine rotation, generate a new key on Beelink:

```bash
ssh-keygen -t ed25519 -N "" -C "knowledge-save-$(date +%Y-%m-%d)" -f ~/.ssh/notes_deploy_key.next
chmod 600 ~/.ssh/notes_deploy_key.next
cat ~/.ssh/notes_deploy_key.next.pub
```

Add the public key under the notes repository's **Settings -> Deploy keys**,
with write access. Pause save operations while replacing the key; do not
overwrite an existing `.old` backup:

```bash
test ! -e ~/.ssh/notes_deploy_key.old &&
  mv ~/.ssh/notes_deploy_key ~/.ssh/notes_deploy_key.old &&
  mv ~/.ssh/notes_deploy_key.next ~/.ssh/notes_deploy_key &&
  chmod 600 ~/.ssh/notes_deploy_key
docker compose -f stacks/knowledge/compose.yaml --profile save run --rm --entrypoint git save ls-remote git@github.com:ColinCee/notes.git HEAD
```

After authentication succeeds, remove the old deploy key in GitHub and delete
only `~/.ssh/notes_deploy_key.old` and `~/.ssh/notes_deploy_key.next.pub`.
Resume save operations. Deploy keys need manual lifecycle management; consider
short-lived App tokens only if that trade-off changes.

The image bakes in GitHub SSH host keys. If those change, confirm fingerprints
against GitHub's published documentation before rebuilding:

```bash
docker compose -f stacks/knowledge/compose.yaml build --no-cache save
docker compose -f stacks/knowledge/compose.yaml --profile save run --rm --entrypoint git save ls-remote git@github.com:ColinCee/notes.git HEAD
```

## Troubleshoot

Use [observability](../observability.md) for task logs and alerts.

| Symptom | Action |
|---------|--------|
| HTTP 403 from embeddings | Check the PAT's **Models: Read** account permission and provider access. |
| HTTP 410 from embeddings | The configured endpoint previously returned Gone. Preserve the index and investigate provider/model availability; retries or a token rotation do not resolve a retired endpoint. |
| HTTP 429 or transient failures | The client backs off; ingestion aborts after repeated consecutive file failures. Rerun once the provider recovers; unchanged files are skipped. |
| Postgres unavailable | Check Compose status and logs. Ingest uses Docker DNS `postgres`, not localhost or the Tailscale address. |
| Duplicate results | Inspect source paths before deleting anything. Historical host paths and `/notes/` paths can describe the same file. |

```bash
docker compose -f stacks/knowledge/compose.yaml ps
docker compose -f stacks/knowledge/compose.yaml logs --tail 50 postgres
docker compose -f stacks/knowledge/compose.yaml exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) AS docs, (SELECT count(*) FROM chunks) AS chunks FROM documents;"'
docker compose -f stacks/knowledge/compose.yaml exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT source_path, ingested_at FROM documents ORDER BY ingested_at DESC LIMIT 10;"'
```
