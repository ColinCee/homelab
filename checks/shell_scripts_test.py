"""Every shell script in git passes shellcheck: each `.sh`, and each file whose first line runs a
shell, so a new script is checked without being named."""

import re
import subprocess

from conftest import ROOT, tool, tracked

SHEBANG = re.compile(rb"#!\s*/(?:usr/)?bin/(?:env\s+)?(?:ba)?sh\b")


def scripts() -> list[str]:
    found = []
    for path in tracked():
        if path.suffix in (".sh", "") and path.is_file():
            with path.open("rb") as f:
                first = f.readline()
            if path.suffix == ".sh" or SHEBANG.match(first):
                found.append(path.relative_to(ROOT).as_posix())
    return found


def test_every_shell_script_passes_shellcheck() -> None:
    files = scripts()
    assert "ci-runner/rootfs/ci-boot" in files  # an extensionless one is found too
    done = subprocess.run(
        [tool("shellcheck"), *files], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stdout + done.stderr
