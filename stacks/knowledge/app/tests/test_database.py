import pytest

from knowledge.database import DATABASE_URL_ENV, resolve_database_url


def test_resolve_database_url_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    database_url = "postgresql://knowledge:secret@100.100.146.119:5432/knowledge"
    monkeypatch.setenv(DATABASE_URL_ENV, database_url)

    # Act
    resolved = resolve_database_url()

    # Assert
    assert resolved == database_url


def test_resolve_database_url_requires_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    # Act / Assert
    with pytest.raises(RuntimeError, match=DATABASE_URL_ENV):
        resolve_database_url()
