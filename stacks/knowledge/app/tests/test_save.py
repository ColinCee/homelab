"""Tests for the save module (URL → markdown note)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from knowledge import __main__ as cli
from knowledge import save


def _task_event(stderr: str) -> dict[str, object]:
    return json.loads(stderr.strip())


class TestTitleFromHtml:
    def test_extracts_from_title_tag(self) -> None:
        html = "<html><head><title>My Article</title></head><body></body></html>"
        assert save._title_from_html(html) == "My Article"

    def test_returns_none_when_no_title(self) -> None:
        html = "<html><body><p>No title</p></body></html>"
        assert save._title_from_html(html) is None

    def test_strips_whitespace(self) -> None:
        html = "<html><head><title>  Padded Title  </title></head></html>"
        assert save._title_from_html(html) == "Padded Title"


class TestExtractContent:
    @patch("trafilatura.extract", return_value="## Heading\n\nBody text")
    @patch("trafilatura.bare_extraction")
    def test_uses_trafilatura_title_and_body(
        self, mock_bare: MagicMock, mock_extract: MagicMock
    ) -> None:
        meta = MagicMock()
        meta.title = "Extracted Title"
        mock_bare.return_value = meta

        title, body = save._extract_content("<html>...</html>", "https://example.com")

        assert title == "Extracted Title"
        assert body == "## Heading\n\nBody text"

    @patch("trafilatura.extract", return_value="Some body")
    @patch("trafilatura.bare_extraction")
    def test_falls_back_to_html_title_tag(
        self, mock_bare: MagicMock, mock_extract: MagicMock
    ) -> None:
        meta = MagicMock()
        meta.title = None
        mock_bare.return_value = meta
        html = "<html><head><title>Fallback Title</title></head><body>content</body></html>"

        title, _ = save._extract_content(html, "https://example.com")

        assert title == "Fallback Title"

    @patch("trafilatura.extract", return_value="Some body")
    @patch("trafilatura.bare_extraction")
    def test_falls_back_to_domain_when_no_title(
        self, mock_bare: MagicMock, mock_extract: MagicMock
    ) -> None:
        meta = MagicMock()
        meta.title = None
        mock_bare.return_value = meta

        title, _ = save._extract_content("<html><body>x</body></html>", "https://example.com/page")

        assert title == "example.com"

    @patch("trafilatura.extract", return_value=None)
    @patch("trafilatura.bare_extraction", return_value=None)
    def test_returns_empty_body_when_extraction_fails(
        self, mock_bare: MagicMock, mock_extract: MagicMock
    ) -> None:
        title, body = save._extract_content("<html></html>", "https://example.com")

        assert title == "example.com"
        assert body == ""


_FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestExtractContentRegression:
    """End-to-end tests with real article HTML snapshots.

    Fixtures are saved snapshots of public articles so tests run offline.
    Source: https://metr.org/blog/2026-02-24-uplift-update/
    """

    def test_metr_article_produces_structured_markdown(self) -> None:
        html = (_FIXTURE_DIR / "metr-uplift-update.html").read_text(encoding="utf-8")

        title, body = save._extract_content(html, "https://metr.org/blog/2026-02-24-uplift-update/")

        assert title
        assert "experiment" in title.lower() or "productivity" in title.lower()
        assert "##" in body, "Expected markdown headings"
        assert "![" in body, "Expected image references"
        assert len(body.splitlines()) > 30, "Expected substantial article content"


class TestDownloadImages:
    def test_downloads_and_rewrites_image_urls(self, tmp_path: Path) -> None:
        markdown = "Text\n\n![Diagram](https://example.com/img.png)\n\nMore text"

        with patch("urllib.request.urlopen") as mock_urlopen:
            response = MagicMock()
            response.read.return_value = b"\x89PNG" + (b"x" * 3000)
            response.headers.get_content_type.return_value = "image/png"
            response.__enter__ = lambda s: response
            response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = response

            result = save._download_images(markdown, "https://example.com", tmp_path)

        assert "![Diagram](001.png)" in result
        assert (tmp_path / "001.png").exists()
        assert len((tmp_path / "001.png").read_bytes()) > 2048

    def test_filters_decorative_images(self, tmp_path: Path) -> None:
        markdown = "![Site logo](/images/logo.png)\n\n![Chart](/images/chart.png)"

        with patch("urllib.request.urlopen") as mock_urlopen:
            response = MagicMock()
            response.read.return_value = b"\x89PNG" + (b"x" * 3000)
            response.headers.get_content_type.return_value = "image/png"
            response.__enter__ = lambda s: response
            response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = response

            result = save._download_images(markdown, "https://example.com", tmp_path)

        assert "logo" not in result
        assert "![Chart](001.png)" in result

    def test_strips_images_below_min_size(self, tmp_path: Path) -> None:
        markdown = "![Tiny](/tiny.png)"

        with patch("urllib.request.urlopen") as mock_urlopen:
            response = MagicMock()
            response.read.return_value = b"\x89PNG" + (b"x" * 100)
            response.headers.get_content_type.return_value = "image/png"
            response.__enter__ = lambda s: response
            response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = response

            result = save._download_images(markdown, "https://example.com", tmp_path)

        assert result == ""
        assert not list(tmp_path.iterdir())

    def test_keeps_url_on_download_failure(self, tmp_path: Path) -> None:
        markdown = "![Chart](https://example.com/chart.png)"

        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = save._download_images(markdown, "https://example.com", tmp_path)

        assert "![Chart](https://example.com/chart.png)" in result

    def test_resolves_relative_urls(self, tmp_path: Path) -> None:
        markdown = "![Diagram](/assets/img.png)"

        with patch("urllib.request.urlopen") as mock_urlopen:
            response = MagicMock()
            response.read.return_value = b"\x89PNG" + (b"x" * 3000)
            response.headers.get_content_type.return_value = "image/png"
            response.__enter__ = lambda s: response
            response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = response

            save._download_images(markdown, "https://example.com/blog/post", tmp_path)

        call_arg = mock_urlopen.call_args[0][0]
        assert call_arg.full_url == "https://example.com/assets/img.png"

    def test_strips_data_uri_images(self, tmp_path: Path) -> None:
        markdown = "![](data:image/png;base64,abc123)"

        result = save._download_images(markdown, "https://example.com", tmp_path)

        assert result == ""


class TestUrlSlug:
    def test_uses_path_segment_only(self) -> None:
        slug = save._url_slug("https://example.com/blog/my-article")
        assert slug == "my-article"

    def test_same_url_same_slug(self) -> None:
        a = save._url_slug("https://example.com/post")
        b = save._url_slug("https://example.com/post")
        assert a == b

    def test_different_urls_different_slugs(self) -> None:
        a = save._url_slug("https://example.com/post-a")
        b = save._url_slug("https://example.com/post-b")
        assert a != b

    def test_handles_trailing_slash(self) -> None:
        slug = save._url_slug("https://example.com/article/")
        assert slug == "article"

    def test_ignores_query_string(self) -> None:
        slug = save._url_slug("https://example.com/article/?utm_source=test")
        assert slug == "article"


class TestSaveUrl:
    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_extract_content")
    @patch.object(save, "_fetch")
    def test_creates_note_with_correct_structure(
        self,
        mock_fetch: MagicMock,
        mock_extract: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = "<html>...</html>"
        mock_extract.return_value = ("Simple Page", "Hello world")

        result = save.save_url("https://example.com/article", notes_dir=tmp_path)

        assert result.exists()
        assert result.suffix == ".md"
        assert result == tmp_path / "resources" / "articles" / "article" / "article.md"
        content = result.read_text()
        assert 'title: "Simple Page"' in content
        assert "source: https://example.com/article" in content
        assert "Hello world" in content

    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_extract_content")
    @patch.object(save, "_fetch")
    def test_downloads_images_from_extracted_markdown(
        self,
        mock_fetch: MagicMock,
        mock_extract: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = "<html>...</html>"
        mock_extract.return_value = (
            "Post Title",
            "Content\n\n![Architecture diagram](https://example.com/images/diagram.png)",
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            response = MagicMock()
            response.read.return_value = b"\x89PNG" + (b"x" * 3000)
            response.headers.get_content_type.return_value = "image/png"
            response.__enter__ = lambda s: response
            response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = response

            result = save.save_url("https://example.com/post", notes_dir=tmp_path)

        article_dir = result.parent
        image_paths = sorted(p.name for p in article_dir.iterdir() if p.suffix == ".png")
        assert image_paths == ["001.png"]
        assert "![Architecture diagram](001.png)" in result.read_text()

    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_extract_content")
    @patch.object(save, "_fetch")
    def test_resaves_existing_article_with_updated_content(
        self,
        mock_fetch: MagicMock,
        mock_extract: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = "<html>...</html>"
        mock_extract.return_value = ("Simple Page", "Hello world")

        slug = save._url_slug("https://example.com/article")
        article_dir = tmp_path / "resources" / "articles" / slug
        assets_dir = article_dir / "assets"
        assets_dir.mkdir(parents=True)
        md_path = article_dir / f"{slug}.md"
        md_path.write_text("old content")
        (assets_dir / "legacy.png").write_bytes(b"old")

        result = save.save_url("https://example.com/article", notes_dir=tmp_path)

        assert result == md_path
        assert "Hello world" in md_path.read_text()
        assert not assets_dir.exists()
        mock_git.assert_called_once()

    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_extract_content")
    @patch.object(save, "_fetch")
    def test_syncs_before_writing(
        self,
        mock_fetch: MagicMock,
        mock_extract: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = "<html>...</html>"
        mock_extract.return_value = ("Title", "Body")

        save.save_url("https://example.com/article", notes_dir=tmp_path)

        mock_sync.assert_called_once_with(tmp_path)
        mock_git.assert_called_once()


class TestGitSync:
    @patch("subprocess.run")
    def test_fetches_and_resets_to_origin_main(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        save._git_sync(tmp_path)

        calls = [call.args[0] for call in mock_run.call_args_list]
        assert calls[0] == ["git", "fetch", "origin", "main"]
        assert calls[1] == ["git", "reset", "--hard", "origin/main"]


class TestGitCommitAndPush:
    @patch("subprocess.run")
    def test_adds_commits_and_pushes(self, mock_run: MagicMock, tmp_path: Path) -> None:
        article_dir = tmp_path / "resources" / "articles" / "test"
        article_dir.mkdir(parents=True)

        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        save._git_commit_and_push(tmp_path, article_dir, "Test Article")

        calls = [call.args[0] for call in mock_run.call_args_list]
        assert calls[0] == ["git", "add", "resources/articles/test"]
        assert calls[1] == ["git", "diff", "--cached", "--quiet"]
        assert calls[2][0:3] == ["git", "commit", "-m"]
        assert "Test Article" in calls[2][3]
        assert calls[3] == ["git", "push", "origin", "main"]

    @patch("subprocess.run")
    def test_skips_commit_and_push_when_article_is_unchanged(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        article_dir = tmp_path / "resources" / "articles" / "test"
        article_dir.mkdir(parents=True)

        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        save._git_commit_and_push(tmp_path, article_dir, "Test Article")

        calls = [call.args[0] for call in mock_run.call_args_list]
        assert calls == [
            ["git", "add", "resources/articles/test"],
            ["git", "diff", "--cached", "--quiet"],
        ]


def test_cli_save_prints_saved_path_and_task_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Arrange
    saved_path = tmp_path / "resources" / "articles" / "saved-note.md"

    monkeypatch.setattr(cli, "connect", lambda: MagicMock())
    monkeypatch.setattr(cli, "run_migrations", lambda conn: None)
    monkeypatch.setattr(cli, "save_url", lambda url, *, notes_dir: saved_path)
    monkeypatch.setattr(
        sys, "argv", ["knowledge", "save", "https://example.com", "--notes-dir", str(tmp_path)]
    )

    # Act
    cli.main()

    # Assert
    captured = capsys.readouterr()
    assert captured.out.strip() == f"Saved: {saved_path}"
    assert _task_event(captured.err) == {
        "command": "save",
        "event": "task_completed",
        "exit_code": 0,
        "saved_path": str(saved_path),
        "status": "succeeded",
        "duration_seconds": pytest.approx(0, abs=1),
    }
