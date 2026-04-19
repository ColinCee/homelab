-- Migration 001: Remove workspace abstraction
-- Run against the live database BEFORE deploying the new code.
--
-- Usage:
--   psql "$KNOWLEDGE_DB_URL" -f stacks/knowledge/migrations/001-remove-workspaces.sql
--
-- Safe to run multiple times (all statements use IF EXISTS).

BEGIN;

-- Drop the composite unique index that includes workspace
DROP INDEX IF EXISTS documents_workspace_source_idx;

-- Remove workspace column from documents (also drops FK to workspaces)
ALTER TABLE documents DROP COLUMN IF EXISTS workspace;

-- Create the new unique index on source_path alone
CREATE UNIQUE INDEX IF NOT EXISTS documents_source_path_idx ON documents (source_path);

-- Drop the now-unreferenced workspaces table
DROP TABLE IF EXISTS workspaces;

COMMIT;
