#!/usr/local/bin/homelab-python3 -Es
"""Prove the image's tools run for a job, after building the image:
  sudo ci-runner/tests/test-tools.sh
with the supervisor stopped, since it needs slot 0 free.
Boots a VM in slot 0 and runs each tool as the runner user, with the PATH
ci-boot gives the runner. Exits non-zero if any fails.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from host_commands import start_process

RUN_VM = "/usr/local/lib/homelab-ci/run-vm.sh"
# Expands inside the VM, not here. The Docker check builds with BuildKit (RUN --mount),
# publishes a port and fetches the page.
PROBE = r'''mkdir -p /tmp/dc && cat > /tmp/dc/Dockerfile <<EOF
FROM busybox:1.37
RUN --mount=type=tmpfs,target=/t echo served > /index.html
CMD ["httpd", "-f", "-p", "8090", "-h", "/"]
EOF
chmod -R a+rX /tmp/dc
tools=("node --version" "uv --version" "java -version" "mise --version" "bun --version"
  "pnpm --version" "docker compose version" "docker buildx version" "python3 --version"
  "shellcheck --version" "file --version" "gh --version" "gitleaks version" "bwrap --version" "socat -V"
  "docker build -q -t check /tmp/dc >/dev/null && docker run -d -p 8090:8090 check >/dev/null && curl -fsS --retry 10 --retry-all-errors --retry-delay 1 http://localhost:8090/")
for t in "${tools[@]}"; do
  if out=$(cd /home/runner && sudo -u runner -H env "PATH=$(cat /opt/runner/.path)" bash -c "$t" 2>&1); then
    echo "PASS $t: $(head -n 1 <<<"$out")"
  else
    echo "FAIL $t: $(tail -n 1 <<<"$out")"
  fi
done
echo "TOTAL ${#tools[@]}"'''  # noqa: E501


def boot(command: list[str], log: Path, env: Mapping[str, str]) -> None:
    start_process(command, log, env).wait()


@dataclass
class Host:
    boot: Callable[[list[str], Path, Mapping[str, str]], None] = boot
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    is_root: Callable[[], bool] = lambda: os.geteuid() == 0


def verdict(console: str) -> tuple[list[str], int, str | None]:
    """The probe's result lines, how many passed, and the failure line unless all did."""
    results = [
        m.group(0).replace("\r", "")
        for line in console.split("\n")
        if (m := re.search(r"(PASS|FAIL) .*", line))
    ]
    passes = sum(r.startswith("PASS") for r in results)
    total = re.search(r"TOTAL ([0-9]+)", console)
    if total is None or passes != int(total.group(1)):
        counted = total.group(1) if total else "?"
        return results, passes, f"TOOLS TEST FAILED ({passes} of {counted} passed)"
    return results, passes, None


def last_lines(text: str, count: int) -> str:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines[-count:])


def main(host: Host | None = None) -> int:
    host = host or Host()
    if not host.is_root():
        print("run with sudo", file=sys.stderr)
        return 1
    run_vm = host.env.get("RUN_VM") or RUN_VM
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        metadata = tmp / "metadata.json"
        metadata.write_text(json.dumps({"task": "probe", "probe": PROBE}))
        console = tmp / "console.log"
        host.boot([run_vm, "0", str(metadata), "180"], console, host.env)
        # Raw, as grep and tail saw it: read_text would turn a lone CR into a line break.
        log = console.read_bytes().decode(errors="replace")
    results, passes, failed = verdict(log)
    print("\n".join(results))
    if failed:
        print(failed)
        if tail := last_lines(log, 30):
            print(tail)
        return 1
    print(f"tools test passed ({passes} tools)")
    return 0


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
