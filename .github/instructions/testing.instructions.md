---
applyTo: "**/tests/**,**/*test*"
---

# Testing Conventions

## Philosophy

Write tests that catch real bugs, not tests that exercise implementation details. Every test should justify its existence — if it can't break when behaviour changes, it's not earning its keep.

## Principles

- **Test behaviour, not implementation** — assert what the code does, not how it does it. If you refactor internals and tests break, the tests were wrong.
- **High-value tests over coverage targets** — 5 tests that catch real bugs beat 50 that test getters. Focus on: edge cases, error paths, integration boundaries.
- **Mock at boundaries, not everywhere** — mock external services (GitHub API, Copilot CLI, filesystem). Don't mock internal modules — that's testing wiring, not behaviour.
- **One assertion per concept** — a test can have multiple `assert` lines, but they should all verify one logical thing. If a test name needs "and", split it.
- **Tests are documentation** — a new reader should understand the system's contract by reading test names alone.

## Structure

Use **Arrange-Act-Assert** (AAA):

```python
def test_rejects_duplicate_review():
    # Arrange
    client = TestClient(app)
    _review_status["repo#1"] = {"status": "in_progress"}

    # Act
    response = client.post("/review", json={"repo": "repo", "pr_number": 1})

    # Assert
    assert response.json()["status"] == "already_in_progress"
```

## Naming

- Test functions: `test_<what_it_does>` — describe the behaviour, not the method
- Test classes (optional): group related tests with `class TestFeatureName`
- Bad: `test_review_pr_method`, `test_function_returns_value`
- Good: `test_rejects_duplicate_review`, `test_parses_timing_from_cli_output`

## What to Test

| Priority | What | Example |
|----------|------|---------|
| **High** | Error paths and edge cases | Invalid input, API failures, timeouts |
| **High** | Integration boundaries | HTTP endpoints, subprocess calls |
| **Medium** | Business logic with branching | Prompt building, status transitions |
| **Low** | Pure data transformations | Parsing, formatting (only if complex) |
| **Skip** | Framework glue, config, constants | FastAPI decorator wiring, env var reads |

## FastAPI Testing

- Use `TestClient(app)` for endpoint tests — it handles ASGI lifecycle
- Mock the orchestrator function, not its internals: `@patch("main.review_pr")`
- Test HTTP contracts: status codes, response shapes, error responses

## Async Testing

- Use `pytest-asyncio` for async tests when needed
- Prefer `TestClient` over raw async tests for endpoints — it's simpler and tests the full stack
