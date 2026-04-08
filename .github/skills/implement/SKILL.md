---
name: implement
description: Implement a GitHub issue. Make code changes, run tests, and ensure quality.
allowed-tools: shell
---

# Implementation Skill

You are implementing a GitHub issue. The issue details are provided in the prompt.

## Process

1. Read and understand the issue requirements
2. Explore the codebase to understand the relevant code and conventions
3. Make the necessary changes — follow existing patterns
4. Run tests and linting if you changed code:
   - Python: `uv run pytest tests/ -v && uv run ruff check .`
   - YAML: `yamllint -c .yamllint.yaml stacks/`
5. Fix any test/lint failures before finishing

## Rules

- **Do NOT commit, push, or create pull requests** — the orchestrator handles all git operations
- **Do NOT run `git add`, `git commit`, `git push`, or `gh pr create`**
- Focus on making correct, complete code changes in the working directory
- Follow existing code patterns and conventions
- Read `.github/copilot-instructions.md` for project conventions
- Keep changes minimal — solve the issue, don't refactor unrelated code

## Quality

- Every change should be tested if test infrastructure exists
- Prefer modifying existing tests over creating new test files
- If you add a new module, add a corresponding test file
