---
applyTo: "{src,tests}/**/*.py"
---

# Python Conventions

Python 3.12+. Managed by uv. Linted by ruff, type-checked by ty.

## Style

- Ruff rules: E, F, I, UP, B, SIM, RUF — line length 100
- Use `from __future__ import annotations` only if needed; prefer `X | None` over `Optional[X]`
- Type-annotate all function signatures
- Docstrings on modules and public functions — one line when possible

## Patterns

- **Models:** Use Pydantic `BaseModel` for structured data. Shared models live in `src/homelab/models.py` (e.g., `CheckResult`, `AuditReport`, `Status`)
- **Audit modules:** Follow the `health.py` / `security.py` pattern — each module defines check functions returning `CheckResult`, a `run_*` function returning `AuditReport`, and a `__main__` block calling `report.print_report()` with `sys.exit(0 if report.passed else 1)`
- **Shell commands:** Use `run_cmd()` wrapper around `subprocess.run()` with `capture_output=True, text=True, timeout=`
- **Error handling:** Return `Status.SKIP` when a tool isn't available (e.g., `FileNotFoundError`), `Status.FAIL` on timeouts or bad results

## Testing

- Mirror source structure: `src/homelab/health.py` → `tests/test_health.py`
- Mock `run_cmd` via `@patch("homelab.<module>.run_cmd")` — never call real system commands
- Use `_mock_result(stdout=, returncode=)` helper for `subprocess.CompletedProcess`
- Group tests in classes by function under test (e.g., `TestCheckContainerRunning`)
- Assert on `result.status` (Status enum), not string values

## Commands

```bash
uv run ruff check src/ tests/     # Lint
uv run ruff format src/ tests/    # Format
uv run ty check src/              # Type check
uv run pytest tests/ -v           # Test
```
