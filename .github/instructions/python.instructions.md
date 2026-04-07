---
applyTo: "stacks/agents/app/**/*.py"
---

# Python Conventions

Style is enforced by ruff and ty — see `stacks/agents/app/pyproject.toml` for config. Don't repeat what the tooling already enforces.

## Agent Service Pattern

The agent service (`stacks/agents/app/`) is a FastAPI app that:

1. Receives requests (e.g., `/review`) with minimal input (repo + PR number)
2. Calls external APIs (GitHub, Copilot) using local credentials (`gh auth token`)
3. Returns structured JSON responses — the agent never writes to GitHub directly
4. GitHub Actions workflows handle posting reviews, comments, etc.

### Key files

- `main.py` — FastAPI endpoints, request/response models
- `review.py` — PR review logic, system prompt, structured output parsing
- `copilot.py` — Copilot API client, returns `ChatResult` dataclass
- `tests/test_main.py` — unit tests (mock external calls)

## Testing

- Mock external calls via `@patch("main.review_pr", new_callable=AsyncMock)`
- Use `TestClient(app)` from FastAPI for endpoint tests
- Run from the agent directory: `cd stacks/agents/app && uv run pytest`
