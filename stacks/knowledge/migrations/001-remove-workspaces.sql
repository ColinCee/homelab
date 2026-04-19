-- Migration 001: Remove workspace abstraction
-- Run against the live database BEFORE deploying the new code.
--
-- Usage:
--   psql "$KNOWLEDGE_DB_URL" -f stacks/knowledge/migrations/001-remove-workspaces.sql
--
-- Safe to run multiple times (all statements use IF EXISTS / conditional).

BEGIN;

-- Deduplicate source_paths that exist in multiple workspaces by prefixing
-- the workspace name, so the unique index on source_path won't fail.
-- Must run BEFORE dropping the workspace column.
UPDATE documents
SET source_path = workspace || '/' || source_path
WHERE EXISTS (
    SELECT 1 FROM documents d2
    WHERE d2.source_path = documents.source_path
      AND d2.id != documents.id
);

-- Drop the composite unique index that includes workspace
DROP INDEX IF EXISTS documents_workspace_source_idx;

-- Remove workspace column from documents (also drops FK to workspaces)
ALTER TABLE documents DROP COLUMN IF EXISTS workspace;

-- Create the new unique index on source_path alone
CREATE UNIQUE INDEX IF NOT EXISTS documents_source_path_idx ON documents (source_path);

-- Drop the now-unreferenced workspaces table
DROP TABLE IF EXISTS workspaces;

COMMIT;
