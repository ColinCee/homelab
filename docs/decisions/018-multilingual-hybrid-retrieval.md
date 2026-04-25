# ADR-018: Multilingual hybrid retrieval

**Date:** 2026-04-25
**Status:** Accepted

## Context

ADR-016 requires Chinese language support for the personal knowledge base. The
initial hybrid search implementation fused vector search with PostgreSQL English
full-text search via reciprocal rank fusion (RRF). Live evaluation against the
Mandarin notes showed good final results, but the lexical path was fragile:

- no-space Chinese queries such as `内疚狼狈堕落胆怯` produced zero English FTS
  candidates;
- spaced Chinese compound queries worked only because PostgreSQL treated each
  whitespace-separated compound as a token;
- some longer English natural-language queries produced zero strict FTS
  candidates because `websearch_to_tsquery` ANDs terms together;
- naive OR relaxation recovered English candidates but introduced broad noisy
  candidate sets.

The implementation should improve Chinese and mixed-language retrieval without
replacing the small Postgres-based stack or introducing a dedicated search
engine.

## Options Considered

### Option A: Keep vector + English FTS only

| Pros | Cons |
|------|------|
| No new moving parts | Chinese keyword retrieval silently degrades to vector-only |
| Current rank-1 results are good on the small fixture | Fails the ADR-016 Chinese support requirement for realistic no-space queries |

**Verdict:** Rejected. The current result quality is good, but the keyword
ranker is not actually supporting Chinese retrieval.

### Option B: App-level Jieba tokenization + Postgres simple FTS

| Pros | Cons |
|------|------|
| Small dependency, no custom Postgres image | Tokenization is derived data that must be stored/backfilled |
| Empirically segments the local no-space Chinese fixture correctly | Less sophisticated than a dedicated CJK search extension |
| Keeps Postgres as the only backend | Requires a second tsvector and RRF candidate source |

**Verdict:** Chosen. It is the simplest option that fixes the observed Chinese
lexical gap while preserving the current architecture.

### Option C: PGroonga, zhparser, or pg_jieba

| Pros | Cons |
|------|------|
| Better DB-native CJK search | Custom Postgres image and extension maintenance |
| Stronger language-specific indexing | More operational complexity than the evidence currently justifies |

**Verdict:** Deferred. Reconsider only if the Jieba + simple FTS approach fails
the retrieval fixture or real notes.

### Option D: Replace RRF with weighted score fusion

| Pros | Cons |
|------|------|
| Fine-grained weighting possible | Requires calibrating cosine, `ts_rank_cd`, and CJK lexical scores |
| Can be tuned with enough labelled data | More brittle than rank fusion for heterogeneous retrievers |

**Verdict:** Rejected for now. RRF is robust, simple, and matches pgvector,
Azure AI Search, and Elasticsearch hybrid-search guidance.

## Decision

Keep RRF as the fusion layer and make candidate generation multilingual:

1. vector search remains the recall backbone;
2. English strict FTS remains the high-precision English keyword ranker;
3. bounded English relaxed FTS contributes candidates for long English queries;
4. Chinese lexical FTS uses app-level Jieba tokens stored in `chunks.cjk_tokens`
   and indexed with a generated `tsv_zh` column using PostgreSQL `simple` FTS.

The retrieval fixture in
`stacks/knowledge/app/tests/fixtures/chinese_retrieval_eval_queries.json` is the
regression set for Chinese, English, and mixed-language queries.

## References

- ADR-016: `docs/decisions/016-knowledge-base.md`
- Issue: <https://github.com/ColinCee/homelab/issues/238>
- Retrieval fixture:
  `stacks/knowledge/app/tests/fixtures/chinese_retrieval_eval_queries.json`
