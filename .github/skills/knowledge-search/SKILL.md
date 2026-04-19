---
name: knowledge-search
description: Search the personal knowledge base for notes, documents, and reference material. Use when you need context from personal notes to answer questions or inform decisions.
allowed-tools: shell
user-invocable: true
---

# Knowledge Base Search

You have access to a personal knowledge base containing markdown documents and PDFs — work notes, coding references, system design studies, side project docs, and more. Use it to ground your answers in the user's own writing.

## How to search

Run the search container on beelink via SSH:

```bash
ssh beelink "cd /home/colin/code/homelab/stacks/knowledge && docker compose --profile ingest run --rm ingest search \"<query>\" --limit <N>"
```

- `<query>` — natural language search text (semantic similarity, not keyword match)
- `--limit` — number of results (default: 5, increase for broad topics)
- The query is wrapped in escaped double quotes (`\"...\"`), which correctly handles apostrophes and special characters

The command takes ~5 seconds (container startup + embedding + vector search).

## Reading the output

Results are ranked by relevance using hybrid search (combines semantic similarity with keyword matching). Higher scores = more relevant:

```
1. score=0.031 source=/notes/Coding/System Design/Practice/Design a rate limiter.md chunk=0
   --- title: Scalable Rate Limiter Design tags: ...
```

- `source` — the note's file path (under `/notes/`)
- `chunk` — which section of the document matched
- Excerpts show up to ~2000 chars of chunk content

## Reading full note content

If a search result looks relevant but the excerpt is too short, read the full file:

```bash
ssh beelink "cat '/home/colin/code/notes/<path-after-/notes/>'"
```

For example, if source is `/notes/Coding/System Design/Practice/Design a rate limiter.md`:
```bash
ssh beelink "cat '/home/colin/code/notes/Coding/System Design/Practice/Design a rate limiter.md'"
```

## When to search

- User asks about something they've written about before (work processes, side projects, coding topics)
- You need context from their notes to make a decision or recommendation
- User explicitly asks to look something up in their notes
- You want to check if there's existing documentation before creating new content

## When NOT to search

- General programming questions answerable from public knowledge
- Questions about the homelab codebase itself (use grep/view on the repo instead)
- The user hasn't indicated they want to reference their personal notes

## Tips

- Search queries work best as natural language questions or topic descriptions, not keywords
- Try multiple searches with different phrasings if the first doesn't return good results
- Results are ranked by Reciprocal Rank Fusion — absolute score values are small (~0.01–0.03) but ordering is meaningful
- The knowledge base is updated on every push to the notes repo (automated pipeline)

## Saving articles

To save a web page as a note (with images) for future search:

```bash
ssh beelink "cd /home/colin/code/homelab/stacks/knowledge && docker compose --profile save run --rm save save \"<URL>\""
```

This fetches the page, downloads images, converts to markdown, and commits to the notes repo. The ingest pipeline picks it up automatically on the next push.
