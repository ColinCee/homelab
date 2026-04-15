---
applyTo: "stacks/agents/app/**/*.py"
---

# Python Conventions

Style is enforced by ruff and ty — see `stacks/agents/app/pyproject.toml` for config. Don't repeat what the tooling already enforces.

**ty quirk:** `ty` does not support `# type: ignore` comments. If you need to suppress a type error, fix the types instead of adding ignore comments.

## Agent Service Pattern

The agent service (`stacks/agents/app/`) is a FastAPI app that:

1. Receives requests (e.g., `/review`) with minimal input (repo + PR number)
2. Sets up a worktree and generates a GitHub App token
3. Dispatches to Copilot CLI with full repo access (`GH_TOKEN`) — the CLI owns the full lifecycle

### Key files

- `main.py` — FastAPI endpoints, request/response models
- `review/orchestrator.py` — PR review orchestrator (thin dispatcher)
- `implement/orchestrator.py` — Issue implementation orchestrator (thin dispatcher)
- `services/copilot.py` — Copilot CLI subprocess wrapper, returns `CLIResult` with parsed stats
- `services/git.py` — Git operations: bare clone, worktrees, branches
- `services/github.py` — GitHub API: App auth (JWT → installation token), REST helpers
- `stats.py` — shared stats formatting for lifecycle stage comments
- `tests/` — unit tests, one per module (mock external calls at boundaries)

## Testing

- Mock external calls at boundaries: `@patch("main.review_pr", new_callable=AsyncMock)`
- Use `TestClient(app)` from FastAPI for endpoint tests
- Run from the agent directory: `cd stacks/agents/app && uv run pytest`
- See `.github/instructions/testing.instructions.md` for full testing guidelines
