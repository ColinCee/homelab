"""Back up /data to the restic repository, nightly or once.

usage: backup.py [loop|once|restore-test]
  loop           wait for BACKUP_TIME (UTC) each day, then back up
  once           back up now
  restore-test   restore one file from the latest snapshot (systemd/backup-upkeep.service)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import telemetry

HOST = "beelink"
# The live recorder database, logs, and re-downloadable plugins are left out: a live copy of the
# database would be inconsistent, so history-snapshot.db (stacks/home-assistant/systemd) is backed
# up instead.
EXCLUDES = (
    "/data/home-assistant/home-assistant_v2.db*",
    "/data/home-assistant/*.log*",
    "/data/home-assistant/.cache",
    "/data/home-assistant/deps",
    "/data/home-assistant/tts",
)
# A run's numbers from restic's summary, logged as fields of "restic backup ran".
SUMMARY_FIELDS = (
    "files_new",
    "files_changed",
    "files_unmodified",
    "data_added",
    "data_added_packed",
    "total_files_processed",
    "total_bytes_processed",
    "total_duration",
    "snapshot_id",
)
FORGET = ("--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6")
# R2 bills past 10 GB and has no server-side quota, so the cap is enforced here: a run that would
# take the repository over this size uploads nothing.
MAX_REPO_BYTES = 8_000_000_000

# Alloy reads this file into Prometheus for the Nightly backup didn't run alert. A metric, not the
# "backup ok" log line: a log store missed that line once and the alert fired with backups
# succeeding.
METRICS_FILE = "/metrics/backup.prom"
# The last run's failing step, for the alert to name; removed by the next good run.
FAILURE_FILE = "/metrics/backup-failure.prom"
# The Server page's Backups row reads this, not the log, for the same reason.
DEEP_CHECK_FILE = "/metrics/backup-deep-check.prom"
# The Backup storage nearly full alert reads it.
SIZE_FILE = "/metrics/backup-size.prom"

# Named, as ruff formats `except (A, B):` to the bare form only Python 3.14 and later parse.
UNREADABLE = (ValueError, LookupError, TypeError)

RESTORE_FILE = "/data/home-assistant/configuration.yaml"
RESTORE_TARGET = "/tmp/restore-test"


class Run(Protocol):
    def __call__(
        self, command: list[str], *, stdout: int | None = None, stderr: int | None = None
    ) -> subprocess.CompletedProcess[str]: ...


log = logging.getLogger("backup")


def run(
    command: list[str], *, stdout: int | None = None, stderr: int | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, stdout=stdout, stderr=stderr, text=True, check=False)


def json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Each JSON object line of restic's `--json` output, skipping any other line."""
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def json_number(text: str, key: str) -> int | None:
    """The first integer `key` in any JSON object line of restic's output."""
    numbers = (n for o in json_objects(text) if isinstance(n := o.get(key), int))
    return next(numbers, None)


def summary(text: str) -> dict[str, Any]:
    """The summary object `restic backup --json` prints last, or {} when it printed none."""
    found = [o for o in json_objects(text) if o.get("message_type") == "summary"]
    return found[-1] if found else {}


def seconds_until(clock: str, now: datetime) -> int:
    """Seconds from `now` to the next `clock` (HH:MM, UTC); a time already passed is tomorrow's."""
    hour, minute = clock.split(":")
    target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int(target.timestamp()) - int(now.timestamp())


@dataclass
class Backup:
    run: Run = run
    root: Path = Path("/")
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def path(self, name: str) -> Path:
        return self.root / name.lstrip("/")

    def setting(self, name: str, default: str) -> str:
        return self.env.get(name) or default

    def restic(self, *args: str, stdout: int | None = None, stderr: int | None = None) -> int:
        return self.run(["restic", *args], stdout=stdout, stderr=stderr).returncode

    def output(self, *args: str, stderr: int | None = None) -> str:
        return self.run(["restic", *args], stdout=subprocess.PIPE, stderr=stderr).stdout or ""

    def write_login(self) -> None:
        """The repository and its S3 login, in the files restic reads (compose.yaml), from the R2
        secrets. Owner-only, as they hold the keys."""
        account, key, secret = (
            telemetry.secret(self.path(f"/run/secrets/{name}"))
            for name in ("r2_account_id", "r2_access_key_id", "r2_secret_access_key")
        )
        self._private(
            self.env["RESTIC_REPOSITORY_FILE"],
            f"s3:https://{account}.r2.cloudflarestorage.com/beelink-backups\n",
        )
        self._private(
            self.env["AWS_SHARED_CREDENTIALS_FILE"],
            f"[default]\naws_access_key_id = {key}\naws_secret_access_key = {secret}\n",
        )

    def _private(self, name: str, text: str) -> None:
        # Replaced whole, so a `docker exec` run never leaves the loop's restic an empty file.
        telemetry.replace_file(self.path(name), text, 0o600)

    def write_metric(self, name: str, metric: str, value: int) -> None:
        try:
            telemetry.replace_file(self.path(name), f"{metric} {value}\n")
        except OSError:
            log.error("could not write a metrics file", extra={"file": name})

    def timestamp(self) -> int:
        return int(self.now().timestamp())

    def backup_args(self, *extra: str) -> list[str]:
        excludes = [arg for pattern in EXCLUDES for arg in ("--exclude", pattern)]
        # --quiet leaves only the summary on stdout; errors still reach stderr, as JSON.
        return ["backup", "/data", "--host", HOST, *excludes, "--json", "--quiet", *extra]

    def repo_bytes(self) -> int | None:
        return json_number(self.output("stats", "--mode", "raw-data", "--json"), "total_size")

    def max_bytes(self) -> int:
        return int(self.setting("MAX_REPO_BYTES", str(MAX_REPO_BYTES)))

    def within_cap(self) -> bool:
        stored = self.repo_bytes()
        dry_run = self.output(*self.backup_args("--dry-run"), stderr=subprocess.DEVNULL)
        pending = summary(dry_run).get("data_added")
        if stored is None or not isinstance(pending, int):
            log.error("backup skipped: could not measure the repository or upload size")
            return False
        limit = self.max_bytes()
        if stored + pending > limit:
            log.error(
                "backup skipped: it would exceed the byte cap",
                extra={"stored_bytes": stored, "new_bytes": pending, "cap_bytes": limit},
            )
            return False
        return True

    def verify(self) -> bool:
        # A plain check only reads metadata. Once a week, download and verify every stored byte
        # so silent corruption in R2 is found before a restore needs it.
        today = str(self.now().isoweekday())
        if today == self.setting("DEEP_CHECK_DAY", "7") or self.env.get("DEEP_CHECK") == "1":
            if self.restic("check", "--read-data") != 0:
                return False
            log.info("deep check ok")
            self.write_metric(
                DEEP_CHECK_FILE,
                "homelab_backup_last_deep_check_timestamp_seconds",
                self.timestamp(),
            )
            return True
        return self.restic("check") == 0

    def record_size(self) -> None:
        size = self.repo_bytes()
        if size is None:
            return
        log.info("repository measured", extra={"size_bytes": size, "cap_bytes": self.max_bytes()})
        self.write_metric(SIZE_FILE, "homelab_backup_repository_bytes", size)

    def back_up(self) -> bool:
        finished = self.run(["restic", *self.backup_args()], stdout=subprocess.PIPE)
        numbers = summary(finished.stdout or "")
        fields = {name: numbers[name] for name in SUMMARY_FIELDS if name in numbers}
        level = logging.INFO if finished.returncode == 0 else logging.WARNING
        log.log(level, "restic backup ran", extra=fields | {"exit_code": finished.returncode})
        return finished.returncode == 0

    def run_backup(self) -> str | None:
        """The step that failed, so a failed run records where it stopped; None when all pass."""
        steps: Sequence[tuple[str, Callable[[], bool]]] = (
            ("cap", self.within_cap),
            ("backup", self.back_up),
            ("forget", lambda: self.restic("forget", "--host", HOST, "--prune", *FORGET) == 0),
            ("check", self.verify),
        )
        for step, passed in steps:
            if not passed():
                return step
        self.record_size()
        return None

    def report(self) -> int:
        step = self.run_backup()
        if step is not None:
            log.error("backup failed", extra={"step": step})
            self.record_failure(step)
            return 1
        log.info("backup ok")
        self.write_metric(
            METRICS_FILE, "homelab_backup_last_success_timestamp_seconds", self.timestamp()
        )
        try:
            self.path(FAILURE_FILE).unlink(missing_ok=True)
        except OSError as error:
            log.error(
                "could not remove the failure file",
                extra={"file": str(FAILURE_FILE), "error": str(error)},
            )
        return 0

    def record_failure(self, step: str) -> None:
        self.write_metric(
            FAILURE_FILE,
            f'homelab_backup_last_failure_timestamp_seconds{{step="{step}"}}',
            self.timestamp(),
        )

    def seed_metric(self) -> bool:
        """On a fresh host, start from the newest snapshot so the alert has a value before the
        first nightly run."""
        if self.path(METRICS_FILE).is_file():
            return True
        try:
            snapshots = json.loads(
                self.output("snapshots", "--host", HOST, "--latest", "1", "--json")
            )
            latest = datetime.strptime(snapshots[0]["time"][:19], "%Y-%m-%dT%H:%M:%S")
        except UNREADABLE:
            return False
        seconds = int(latest.replace(tzinfo=UTC).timestamp())
        self.write_metric(METRICS_FILE, "homelab_backup_last_success_timestamp_seconds", seconds)
        return self.path(METRICS_FILE).is_file()

    def restore_test(self) -> int:
        target = self.path(RESTORE_TARGET)
        shutil.rmtree(target, ignore_errors=True)
        # restic exits non-zero because the container cannot chown restored files, so the restored
        # file is the test.
        self.restic(
            "restore", "latest", "--target", str(target), "--include", RESTORE_FILE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        restored = target / RESTORE_FILE.lstrip("/")
        passed = restored.is_file() and restored.stat().st_size > 0
        if passed:
            log.info("restore test ok", extra={"file": str(RESTORE_FILE)})
        else:
            log.error(
                "restore test failed: could not restore the file", extra={"file": str(RESTORE_FILE)}
            )
        shutil.rmtree(target, ignore_errors=True)
        return 0 if passed else 1

    def loop(self, sleep: Callable[[float], None] = time.sleep) -> None:
        if not self.seed_metric():
            log.error("could not seed the metrics file", extra={"file": str(METRICS_FILE)})
        self.path("/tmp/ready").touch()
        if not self.path(SIZE_FILE).is_file():
            self.record_size()
        while True:
            sleep(seconds_until(self.setting("BACKUP_TIME", "02:30"), self.now()))
            # A crash records a failure and waits for the next night, rather than ending the loop.
            try:
                self.report()
            except Exception:
                log.exception("backup crashed")
                self.record_failure("crash")

    def main(self, argv: Sequence[str]) -> int:
        self.write_login()
        # Create the repository on first use; fail fast on bad credentials.
        quiet = subprocess.DEVNULL
        if self.restic("cat", "config", stdout=quiet, stderr=quiet) != 0:
            initialised = self.restic("init")
            if initialised != 0:
                return initialised
        match (argv[0] if argv else "") or "loop":
            case "once":
                return self.report()
            case "restore-test":
                return self.restore_test()
            case "loop":
                self.loop()
                return 0
            case _:
                log.error("usage: backup.py [loop|once|restore-test]")
                return 2


if __name__ == "__main__":
    telemetry.start()
    sys.exit(Backup().main(sys.argv[1:]))
