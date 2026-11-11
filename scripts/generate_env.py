#!/usr/bin/env python3
"""Render stack .env files and secret files without evaluating them as shell input.

A stack's `.env.example` renders to `.env`, which Compose reads. Each `secrets/<name>.example`
renders to `secrets/<name>`, which Compose mounts at /run/secrets/<name>: a secret never goes in a
container's environment, which anything allowed to inspect containers can read.
"""

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
    _check_present(template, environment)

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


def render_secret(template: str, environment: Mapping[str, str]) -> str:
    """Render a secret file as raw text, with no trailing newline: not every reader trims one."""
    _check_present(template, environment)
    return _PLACEHOLDER.sub(lambda match: environment[match.group(1)], template).rstrip("\r\n")


def secret_templates(stack_dir: Path) -> tuple[Path, ...]:
    """A stack's secret file templates, `secrets/<name>.example`."""
    return tuple(sorted((stack_dir / "secrets").glob("*.example")))


def generate_env_files(
    repo_root: Path,
    stack_names: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Validate every selected template, then atomically render its .env and secret files."""
    environment = os.environ if environment is None else environment
    names = _select_stack_names(repo_root, stack_names)
    env_files: dict[Path, str] = {}
    secrets: dict[Path, str] = {}
    errors: list[str] = []

    for name in names:
        _validate_stack_name(name)
        stack_dir = repo_root / "stacks" / name
        if not stack_dir.is_dir():
            raise EnvGenerationError(f"Stack directory not found: {stack_dir}")
        example = stack_dir / ".env.example"
        templates = secret_templates(stack_dir)
        if not example.is_file() and not templates:
            if stack_names is not None:
                print(f"No templates for {name}; nothing to generate", file=sys.stderr)
            continue

        try:
            if example.is_file():
                env_files[stack_dir / ".env"] = render_template(
                    example.read_text(encoding="utf-8"), environment
                )
            for template in templates:
                secrets[template.with_suffix("")] = render_secret(
                    template.read_text(encoding="utf-8"), environment
                )
        except EnvGenerationError as exc:
            errors.append(f"{name}: {exc}")

    if errors:
        raise EnvGenerationError("\n".join(errors))

    for output, content in env_files.items():
        _atomic_write(output, content)
        print(f"Generated {output}")
    # A stack whose settings all became secret files keeps no old .env, which held them.
    for name in names:
        stack_dir = repo_root / "stacks" / name
        old = not (stack_dir / ".env.example").is_file() and (stack_dir / ".env").is_file()
        if old and secret_templates(stack_dir):
            (stack_dir / ".env").unlink()
            print(f"Removed {stack_dir / '.env'}")
    for output, content in secrets.items():
        _write_secret(output, content)
    return (*env_files, *secrets)


def _check_present(template: str, environment: Mapping[str, str]) -> None:
    missing = [name for name in required_variables(template) if not environment.get(name)]
    if missing:
        raise EnvGenerationError("Missing required environment variable(s): " + ", ".join(missing))


def _write_secret(path: Path, content: str) -> None:
    """Containers read it as different users, so the file is open to all and its folder to the
    owner alone. It is rewritten only when it changes, so its time marks a new value, which
    recreates the containers reading it (deploy.py, stack_config_hash)."""
    path.parent.chmod(0o700)
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    _atomic_write(path, content, mode=0o444)
    print(f"Generated {path}")


def _select_stack_names(
    repo_root: Path,
    stack_names: Sequence[str] | None,
) -> tuple[str, ...]:
    if stack_names is not None:
        return tuple(stack_names)

    stacks_dir = repo_root / "stacks"
    with_env = {path.parent.name for path in stacks_dir.glob("*/.env.example") if path.is_file()}
    with_secrets = {path.parent.parent.name for path in stacks_dir.glob("*/secrets/*.example")}
    return tuple(sorted(with_env | with_secrets))


def _validate_stack_name(name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise EnvGenerationError(f"Invalid stack name: {name!r}")


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stacks", nargs="*", help="stack names (default: all with templates)")
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
