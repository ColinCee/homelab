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

## Code Quality

The design philosophy says: deep modules, narrow interfaces; readability first. Here's how to apply that to Python code in this repo.

### Services own their error handling

If a service function is best-effort (its failure doesn't change the caller's control flow), it must handle errors internally and return a safe default. The caller should not need `try`/`except` around it.

Pattern already in use: `safe_comment()` wraps `comment_on_issue()` — logs a warning on failure, never raises. Follow this for all fire-and-forget service calls (lock, close, mark ready). For data-fetching calls that can degrade gracefully, return the empty value (e.g., `[]`).

Anti-pattern: wrapping every service call in `try`/`except` at the orchestrator level. That's error handling at the wrong abstraction level.

### Complexity signals — stop and extract

When a function hits any of these, refactor before continuing:

- **~80 lines** — extract coherent blocks into named helpers
- **3+ `try`/`except` blocks** — error handling is probably at the wrong level
- **6+ parameters** — bundle related params into a context dataclass
- **Same structure built 3+ times** — extract common fields, branch only on what varies

These are signals, not hard rules. The goal: a new reader understands the function's flow in one screen.

### One abstraction level per function

A function sequences high-level steps OR does low-level work (URL formatting, dict construction), not both. If an orchestrator builds URLs inline, extract those.

## Testing

- Mock external calls at boundaries: `@patch("main.review_pr", new_callable=AsyncMock)`
- Use `TestClient(app)` from FastAPI for endpoint tests
- Run from the agent directory: `cd stacks/agents/app && uv run pytest`
- See `.github/instructions/testing.instructions.md` for full testing guidelines
