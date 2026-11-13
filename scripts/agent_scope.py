"""Say whether an agent's pull request changes only files that run no code on the Beelink, the
only ones the on-call agent merges itself (docs/architecture.md, "Monitoring and alerts").

Usage: agent_scope.py BASE HEAD. Exits 0 when every changed path, and a rename's old one, is an
alert rule, a dashboard, a doc or a test; exits 1 and names the rest otherwise. CI runs the base
commit's copy, so a pull request cannot change the rule that judges it. Colin merges the rest past
it as admin.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Tests run only in CI. AGENTS.md, skills and workflows steer agents and CI, so they stay out.
DATA = re.compile(
    r"stacks/observability/prometheus/rules\.yml"
    r"|stacks/observability/grafana/dashboards/[\w.-]+\.json"
    r"|docs/[\w./-]+\.(md|svg|png)"
    r"|README\.md"
    r"|tests/[\w./-]+\.py"
)


def outside(base: str, head: str) -> list[str]:
    names = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", base, head],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return [n for n in names if n and (".." in n or not DATA.fullmatch(n))]


if __name__ == "__main__":
    found = outside(sys.argv[1], sys.argv[2])
    for path in found:
        print(f"::error file={path}::An agent's pull request may merge itself only when it changes "
              "alert rules, dashboards, docs or tests; Colin merges this one.")  # fmt: skip
    sys.exit(1 if found else 0)
