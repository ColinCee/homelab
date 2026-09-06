#!/usr/bin/env python3
"""Render stack .env files without evaluating them as shell input."""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

_PLACEHOLDER = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_ASSIGNMENT = re.compile(r"^(?P<prefix>[ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*=[ \t]*)(?P<value>.*)$")


class EnvGenerationError(ValueError):
    """Raised when templates cannot be rendered safely."""


def required_variables(template: str) -> tuple[str, ...]:
    """Return the explicit environment placeholders used by a template."""
    return tuple(sorted({match.group(1) for match in _PLACEHOLDER.finditer(template)}))


def quote_env_value(value: str) -> str:
    """Quote a value using Docker Compose's escaped double-quoted syntax."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "$$")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return '"' + escaped + '"'


def render_template(template: str, environment: Mapping[str, str]) -> str:
    """Render placeholders while keeping secret values as dotenv data."""
    variables = required_variables(template)
    missing = [name for name in variables if not environment.get(name)]
    if missing:
        raise EnvGenerationError("Missing required environment variable(s): " + ", ".join(missing))

    rendered_lines: list[str] = []
    for line in template.splitlines(keepends=True):
        body, ending = _split_line_ending(line)
        assignment = _ASSIGNMENT.match(body)
        if assignment is None:
            rendered_lines.append(line)
            continue

        value = _PLACEHOLDER.sub(
            lambda match: environment[match.group(1)], assignment.group("value")
        )
        rendered_lines.append(assignment.group("prefix") + quote_env_value(value) + ending)

    return "".join(rendered_lines)


def generate_env_files(
    repo_root: Path,
    stack_names: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Validate every selected template, then atomically render its .env file."""
    environment = os.environ if environment is None else environment
    names = _select_stack_names(repo_root, stack_names)
    pending: dict[Path, str] = {}
    errors: list[str] = []

    for name in names:
        _validate_stack_name(name)
        stack_dir = repo_root / "stacks" / name
        if not stack_dir.is_dir():
            raise EnvGenerationError(f"Stack directory not found: {stack_dir}")
        example = stack_dir / ".env.example"
        if not example.is_file():
            if stack_names is not None:
                print(f"No .env.example for {name}; nothing to generate", file=sys.stderr)
            continue

        template = example.read_text(encoding="utf-8")
        try:
            pending[stack_dir / ".env"] = render_template(template, environment)
        except EnvGenerationError as exc:
            errors.append(f"{name}: {exc}")

    if errors:
        raise EnvGenerationError("\n".join(errors))

    for output, content in pending.items():
        _atomic_write(output, content)
        print(f"Generated {output}")
    return tuple(pending)


def _select_stack_names(
    repo_root: Path,
    stack_names: Sequence[str] | None,
) -> tuple[str, ...]:
    if stack_names is not None:
        return tuple(stack_names)

    stacks_dir = repo_root / "stacks"
    return tuple(
        sorted(path.parent.name for path in stacks_dir.glob("*/.env.example") if path.is_file())
    )


def _validate_stack_name(name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise EnvGenerationError(f"Invalid stack name: {name!r}")


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stacks", nargs="*", help="stack names (default: all .env.example files)")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    selected = tuple(args.stacks) if args.stacks else None
    try:
        generate_env_files(repo_root, selected)
    except (EnvGenerationError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
