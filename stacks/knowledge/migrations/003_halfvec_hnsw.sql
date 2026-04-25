-- Switch embeddings from vector(3072) to halfvec(3072) and add HNSW index.
--
-- pgvector HNSW supports up to 2000 dims for vector, but 4000 for halfvec.
-- text-embedding-3-large produces 3072-dim embeddings, so halfvec is required.
-- Half-precision (float16) has negligible impact on cosine similarity ranking.

ALTER TABLE chunks
    ALTER COLUMN embedding TYPE halfvec(3072) USING embedding::halfvec(3072);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding halfvec_cosine_ops);
