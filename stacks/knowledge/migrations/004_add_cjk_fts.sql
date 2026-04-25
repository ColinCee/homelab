-- Add derived Chinese token search for multilingual hybrid retrieval.
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS cjk_tokens TEXT NOT NULL DEFAULT '';

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS tsv_zh tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', cjk_tokens)) STORED;

CREATE INDEX IF NOT EXISTS chunks_tsv_zh_idx ON chunks USING gin (tsv_zh);
