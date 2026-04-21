# Personal Finances Test Fixture

This is a test note used by the integration test to verify the full
ingest → search round-trip works against a real Postgres + pgvector instance.

The note mentions some distinctive keywords so the full-text search
component of hybrid search can match reliably without depending on
real embeddings: **quokka habitat budget allocation 2026**.
