from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from knowledge.__main__ import main
from knowledge.models import DirectoryIngestResult


def _make_result(*, processed: int, failed: int) -> DirectoryIngestResult:
    return DirectoryIngestResult(
        files_found=processed + failed,
        files_failed=failed,
        documents_processed=processed,
        chunks_created=processed,
        documents_skipped=0,
        documents_deleted=0,
    )


@patch("knowledge.__main__.run_migrations")
@patch("knowledge.__main__.connect")
@patch("knowledge.__main__.ingest_directory")
def test_directory_ingest_exits_nonzero_when_all_files_fail(
    mock_ingest: MagicMock,
    _mock_connect: MagicMock,
    _mock_migrations: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Arrange — every file failed (e.g. systemic DB error)
    mock_ingest.return_value = _make_result(processed=0, failed=5)
    monkeypatch.setattr("sys.argv", ["knowledge", "ingest", "--dir", str(tmp_path)])

    # Act / Assert
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code != 0


@patch("knowledge.__main__.run_migrations")
@patch("knowledge.__main__.connect")
@patch("knowledge.__main__.ingest_directory")
def test_directory_ingest_exits_zero_on_partial_success(
    mock_ingest: MagicMock,
    _mock_connect: MagicMock,
    _mock_migrations: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Arrange — some files failed but at least one succeeded
    mock_ingest.return_value = _make_result(processed=3, failed=2)
    monkeypatch.setattr("sys.argv", ["knowledge", "ingest", "--dir", str(tmp_path)])

    # Act / Assert — partial success is not a failure
    main()
