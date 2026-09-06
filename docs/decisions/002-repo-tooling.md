# ADR-002: Repository tooling

**Status:** Accepted

Use mise for tool versions and common checks, uv for the knowledge application's
Python dependencies, and Docker Compose for services. Shell scripts are limited
to deployment, secret rendering, validation, ingestion, and backups.

`mise.toml` owns the commands. CI runs them; the Git hook only prevents plaintext
private-document commits. Do not maintain a second check runner in the hook or
per-service aliases for identical tasks.
