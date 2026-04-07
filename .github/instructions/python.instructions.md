---
applyTo: "{src,tests}/**/*.py"
---

# Python Conventions

Style is enforced by ruff and ty — see `pyproject.toml` for config. Don't repeat what the tooling already enforces.

## Audit Module Pattern

All check modules (`health.py`, `security.py`) follow the same structure:

1. Check functions that return `CheckResult` from `homelab.models`
2. A `run_*()` function that returns `AuditReport`
3. A `__main__` block: `report.print_report()` then `sys.exit(0 if report.passed else 1)`

Use `run_cmd()` to wrap `subprocess.run()`. Return `Status.SKIP` when a tool isn't available, `Status.FAIL` on timeouts or bad results.

## Testing

- Mock `run_cmd` via `@patch("homelab.<module>.run_cmd")` — never call real system commands
- Use `_mock_result(stdout=, returncode=)` helper to build `subprocess.CompletedProcess`
- Group tests in classes by function under test (e.g., `TestCheckContainerRunning`)
- Assert on `result.status` (the `Status` enum), not string comparisons
