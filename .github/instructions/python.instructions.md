---
applyTo: "stacks/agents/app/**/*.py"
---

# Python Conventions

Style is enforced by ruff and ty — see `stacks/agents/app/pyproject.toml` for config. Don't repeat what the tooling already enforces.

## Agent Service Pattern

The agent service (`stacks/agents/app/`) is a FastAPI app that:

1. Receives requests (e.g., `/review`) with minimal input (repo + PR number)
2. Runs Copilot CLI in headless mode to perform the work (review, implement)
3. Copilot CLI interacts with GitHub directly — posting reviews, reading files, etc.

### Key files

- `main.py` — FastAPI endpoints, request/response models
- `review.py` — PR review orchestrator (deep module, narrow interface)
- `implement.py` — Issue implementation + fix orchestrator
- `copilot.py` — Copilot CLI subprocess wrapper, returns `CLIResult` with parsed stats
- `git.py` — Git operations: bare clone, worktrees, branches, commit and push
- `github.py` — GitHub API: App auth (JWT → installation token), REST, GraphQL helpers
- `tests/` — unit tests, one per module (mock external calls at boundaries)

## Testing

- Mock external calls at boundaries: `@patch("main.review_pr", new_callable=AsyncMock)`
- Use `TestClient(app)` from FastAPI for endpoint tests
- Run from the agent directory: `cd stacks/agents/app && uv run pytest`
- See `.github/instructions/testing.instructions.md` for full testing guidelines
