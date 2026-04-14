"""Runtime environment validation helpers."""

import os
from collections.abc import Iterable, Mapping


class RequiredEnvironmentError(RuntimeError):
    """Raised when one or more required environment variables are missing."""

    def __init__(self, names: Iterable[str]):
        self.names = tuple(names)
        plural = "variable is" if len(self.names) == 1 else "variables are"
        joined = ", ".join(self.names)
        super().__init__(f"Required environment {plural} missing or empty: {joined}")


def missing_required_env(
    names: Iterable[str], environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Return required env vars whose values are missing or empty."""
    env = os.environ if environ is None else environ
    return tuple(name for name in names if not env.get(name))


def validate_required_env(names: Iterable[str], environ: Mapping[str, str] | None = None) -> None:
    """Raise when any required env vars are missing or empty."""
    missing = missing_required_env(names, environ)
    if missing:
        raise RequiredEnvironmentError(missing)
