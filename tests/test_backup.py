import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stacks.backup.backup import SUMMARY_FIELDS, Backup, seconds_until

SUNDAY = datetime(2026, 11, 15, 2, 30, 5, tzinfo=UTC)
MONDAY = datetime(2026, 11, 16, 2, 30, 5, tzinfo=UTC)
STATS = '{"total_size":1000,"snapshots_count":3}\n'
DRY_RUN = (
    '{"message_type":"status","percent_done":1}\n'
    '{"message_type":"summary","files_new":2,"data_added":500,"total_duration":1.2}\n'
)


class Restic:
    """Records restic's argv and answers by subcommand: (exit code, stdout)."""

    def __init__(self, **answers: tuple[int, str]) -> None:
        self.calls: list[list[str]] = []
        self.answers = {
            "stats": (0, STATS),
            "dry-run": (0, DRY_RUN),
            "backup": (0, DRY_RUN.replace("1.2", "61.5")),
        } | answers
        self.on_restore: Path | None = None

    def __call__(
        self, command: list[str], *, stdout: int | None = None, stderr: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        key = "dry-run" if "--dry-run" in command else command[1]
        if key == "restore" and self.on_restore is not None:
            self.on_restore.parent.mkdir(parents=True)
            self.on_restore.write_text("homeassistant:\n")
        code, out = self.answers.get(key, (0, ""))
        return subprocess.CompletedProcess(
            command, code, out if stdout == subprocess.PIPE else None
        )

    def ran(self, subcommand: str) -> list[list[str]]:
        return [c for c in self.calls if c[1] == subcommand and "--dry-run" not in c]


def make(
    tmp_path: Path, restic: Restic, when: datetime = MONDAY, env: dict[str, str] | None = None
) -> Backup:
    for name, value in {
        "r2_account_id": "acct\n",
        "r2_access_key_id": "key\n",
        "r2_secret_access_key": "secret\n",
    }.items():
        (tmp_path / "run/secrets").mkdir(parents=True, exist_ok=True)
        (tmp_path / "run/secrets" / name).write_text(value)
    for folder in ("tmp", "metrics"):
        (tmp_path / folder).mkdir(exist_ok=True)
    environment = {
        "RESTIC_REPOSITORY_FILE": "/tmp/restic_repository",
        "AWS_SHARED_CREDENTIALS_FILE": "/tmp/r2_credentials",
    } | (env or {})
    return Backup(run=restic, root=tmp_path, env=environment, now=lambda: when)


def metric(tmp_path: Path, name: str) -> str:
    return (tmp_path / "metrics" / name).read_text()


def test_start_writes_the_login_owner_only_and_creates_a_missing_repository(
    tmp_path: Path,
) -> None:
    restic = Restic(cat=(1, ""), init=(3, ""))
    assert make(tmp_path, restic).main(["once"]) == 3  # a failed init stops before backing up
    assert [c[1] for c in restic.calls] == ["cat", "init"]
    repository = tmp_path / "tmp/restic_repository"
    assert repository.read_text() == "s3:https://acct.r2.cloudflarestorage.com/beelink-backups\n"
    credentials = tmp_path / "tmp/r2_credentials"
    assert credentials.read_text() == (
        "[default]\naws_access_key_id = key\naws_secret_access_key = secret\n"
    )
    assert {repository.stat().st_mode & 0o777, credentials.stat().st_mode & 0o777} == {0o600}


def test_a_good_run_backs_up_without_the_live_database_and_records_success(
    tmp_path: Path,
) -> None:
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics/backup-failure.prom").write_text("old\n")
    restic = Restic()
    assert make(tmp_path, restic).main(["once"]) == 0

    [run] = restic.ran("backup")
    assert run[:5] == ["restic", "backup", "/data", "--host", "beelink"]
    flags = run[5:]
    excluded = [flags[i + 1] for i, flag in enumerate(flags) if flag == "--exclude"]
    assert "/data/home-assistant/home-assistant_v2.db*" in excluded
    assert "/data/home-assistant/*.log*" in excluded
    [dry_run] = [c for c in restic.calls if "--dry-run" in c]
    assert dry_run[: len(run)] == run  # the cap measures exactly what the backup would send
    assert restic.ran("forget") == [
        ["restic", "forget", "--host", "beelink", "--prune",
         "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6"]
    ]  # fmt: skip
    assert restic.ran("check") == [["restic", "check"]]  # Monday: metadata only

    now = int(MONDAY.timestamp())
    assert metric(tmp_path, "backup.prom") == (
        f"homelab_backup_last_success_timestamp_seconds {now}\n"
    )
    assert metric(tmp_path, "backup-size.prom") == "homelab_backup_repository_bytes 1000\n"
    assert not (tmp_path / "metrics/backup-failure.prom").exists()
    assert not list((tmp_path / "metrics").glob("*.tmp"))


@pytest.mark.parametrize(
    ("answers", "env", "step"),
    [
        pytest.param({}, {"MAX_REPO_BYTES": "1499"}, "cap", id="over-cap"),
        pytest.param({"stats": (1, "")}, {}, "cap", id="unmeasured"),
        pytest.param({"backup": (1, "")}, {}, "backup", id="backup"),
        pytest.param({"forget": (1, "")}, {}, "forget", id="forget"),
        pytest.param({"check": (1, "")}, {}, "check", id="check"),
    ],
)
def test_a_failed_step_is_recorded_and_fails_the_run(
    tmp_path: Path, answers: dict[str, tuple[int, str]], env: dict[str, str], step: str
) -> None:
    restic = Restic(**answers)
    assert make(tmp_path, restic, env=env).main(["once"]) == 1
    now = int(MONDAY.timestamp())
    assert metric(tmp_path, "backup-failure.prom") == (
        f'homelab_backup_last_failure_timestamp_seconds{{step="{step}"}} {now}\n'
    )
    assert not (tmp_path / "metrics/backup.prom").exists()
    if step == "cap":
        assert not restic.ran("backup")  # nothing is uploaded past the cap


@pytest.mark.parametrize(
    ("answer", "fields"),
    [
        pytest.param(
            None,
            {"files_new": 2, "data_added": 500, "total_duration": 61.5, "exit_code": 0},
            id="summary",
        ),
        pytest.param((1, "Fatal: unable to open repository\n"), {"exit_code": 1}, id="none"),
    ],
)
def test_a_runs_bytes_and_files_are_fields_of_its_log_line(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    answer: tuple[int, str] | None,
    fields: dict[str, object],
) -> None:
    caplog.set_level(logging.INFO)
    restic = Restic(**({"backup": answer} if answer else {}))
    make(tmp_path, restic).main(["once"])
    assert restic.ran("backup")[0][-2:] == ["--json", "--quiet"]
    [record] = [r for r in caplog.records if r.getMessage() == "backup uploaded"]
    keys = {*SUMMARY_FIELDS, "exit_code", "message_type"}
    logged = {k: v for k, v in vars(record).items() if k in keys}
    assert logged == fields  # restic's own keys, never its message_type


def test_the_cap_allows_a_run_that_reaches_it_exactly(tmp_path: Path) -> None:
    assert make(tmp_path, Restic(), env={"MAX_REPO_BYTES": "1500"}).main(["once"]) == 0


@pytest.mark.parametrize(
    ("when", "env"),
    [
        pytest.param(SUNDAY, {}, id="sunday"),
        pytest.param(MONDAY, {"DEEP_CHECK": "1"}, id="asked"),
        pytest.param(MONDAY, {"DEEP_CHECK_DAY": "1"}, id="moved-day"),
        pytest.param(SUNDAY, {"DEEP_CHECK_DAY": ""}, id="empty-day-is-sunday"),
    ],
)
def test_the_weekly_check_reads_every_byte_and_records_it(
    tmp_path: Path, when: datetime, env: dict[str, str]
) -> None:
    restic = Restic()
    assert make(tmp_path, restic, when, env).main(["once"]) == 0
    assert restic.ran("check") == [["restic", "check", "--read-data"]]
    assert metric(tmp_path, "backup-deep-check.prom") == (
        f"homelab_backup_last_deep_check_timestamp_seconds {int(when.timestamp())}\n"
    )


@pytest.mark.parametrize(
    ("now", "clock", "wait"),
    [
        (datetime(2026, 11, 12, 1, 0, 0, tzinfo=UTC), "02:30", 5400),
        (datetime(2026, 11, 12, 2, 30, 0, tzinfo=UTC), "02:30", 86400),  # just ran: tomorrow
        (datetime(2026, 11, 12, 23, 0, 0, tzinfo=UTC), "02:30", 12600),
        (datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), "00:00", 1),
    ],
)
def test_the_loop_waits_until_the_next_backup_time(now: datetime, clock: str, wait: int) -> None:
    assert seconds_until(clock, now) == wait


class Stop(Exception):
    pass


def test_the_loop_seeds_the_alert_then_backs_up_at_backup_time(tmp_path: Path) -> None:
    snapshots = '[{"time":"2026-11-10T02:30:41.123456789Z","hostname":"beelink"}]'
    restic = Restic(snapshots=(0, snapshots))
    job = make(tmp_path, restic, env={"BACKUP_TIME": "03:00"})
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 2:
            raise Stop

    with pytest.raises(Stop):
        job.loop(sleep)
    assert waits == [1795, 1795]
    assert len(restic.ran("backup")) == 1
    assert (tmp_path / "tmp/ready").exists()
    # The seed was replaced by the run's own success time.
    assert metric(tmp_path, "backup.prom") == (
        f"homelab_backup_last_success_timestamp_seconds {int(MONDAY.timestamp())}\n"
    )


def test_seeding_takes_the_newest_snapshot_as_utc(tmp_path: Path) -> None:
    snapshots = '[{"time":"2026-11-10T02:30:41.123456789+01:00","hostname":"beelink"}]'
    assert make(tmp_path, Restic(snapshots=(0, snapshots))).seed_metric()
    seeded = int(datetime(2026, 11, 10, 2, 30, 41, tzinfo=UTC).timestamp())
    assert metric(tmp_path, "backup.prom") == (
        f"homelab_backup_last_success_timestamp_seconds {seeded}\n"
    )


def test_seeding_with_no_snapshot_writes_nothing(tmp_path: Path) -> None:
    assert not make(tmp_path, Restic(snapshots=(0, "[]"))).seed_metric()
    assert not (tmp_path / "metrics/backup.prom").exists()


@pytest.mark.parametrize("restored", [True, False])
def test_restore_test_passes_only_when_the_file_comes_back(
    tmp_path: Path, restored: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    restic = Restic(restore=(1, ""))  # restic fails to chown even when the file is restored
    if restored:
        restic.on_restore = tmp_path / "tmp/restore-test/data/home-assistant/configuration.yaml"
    assert make(tmp_path, restic).main(["restore-test"]) == (0 if restored else 1)
    [call] = restic.ran("restore")
    assert call == [
        "restic", "restore", "latest", "--target", str(tmp_path / "tmp/restore-test"),
        "--include", "/data/home-assistant/configuration.yaml",
    ]  # fmt: skip
    assert not (tmp_path / "tmp/restore-test").exists()
    [record] = [r for r in caplog.records if r.getMessage().startswith("restore test")]
    assert record.levelno == (logging.INFO if restored else logging.ERROR)


def test_an_unknown_mode_is_a_usage_error(tmp_path: Path) -> None:
    assert make(tmp_path, Restic()).main(["nightly"]) == 2


def test_a_crashed_run_is_recorded_and_the_loop_keeps_going(tmp_path: Path) -> None:
    restic = Restic()
    job = make(tmp_path, restic)

    def crashing(
        command: list[str], *, stdout: int | None = None, stderr: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        if command[1] == "forget":
            raise OSError("restic vanished")
        return restic(command, stdout=stdout, stderr=stderr)

    job.run = crashing
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            raise Stop

    with pytest.raises(Stop):
        job.loop(sleep)
    assert len(restic.ran("backup")) == 2  # the second night ran after the first crashed
    assert metric(tmp_path, "backup-failure.prom") == (
        f'homelab_backup_last_failure_timestamp_seconds{{step="crash"}} {int(MONDAY.timestamp())}\n'
    )


def test_a_good_run_passes_when_the_old_failure_cannot_be_removed(tmp_path: Path) -> None:
    (tmp_path / "metrics/backup-failure.prom").mkdir(parents=True)  # unlink fails on a folder
    assert make(tmp_path, Restic()).main(["once"]) == 0
    assert metric(tmp_path, "backup.prom").startswith("homelab_backup_last_success")


def test_the_login_files_are_replaced_whole_and_stay_owner_only(tmp_path: Path) -> None:
    job = make(tmp_path, Restic())
    job.write_login()
    repository = tmp_path / "tmp/restic_repository"
    before = repository.stat().st_ino
    job.write_login()
    assert repository.stat().st_ino != before  # renamed over, never truncated in place
    assert repository.stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "tmp").glob("*.tmp"))
