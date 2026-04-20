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

SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head><title>Test Article Title</title></head>
<body>
<nav><a href="/">Home</a></nav>
<header><h1>Site Header</h1></header>
<article>
    <h1>Test Article Title</h1>
    <p>This is the first paragraph with important content.</p>
    <h2>Section Two</h2>
    <p>More content here about the topic.</p>
    <img src="/images/diagram.png" alt="Architecture diagram">
    <p>Final paragraph.</p>
</article>
<footer>Copyright 2026</footer>
</body>
</html>"""

SIMPLE_HTML = """<html>
<head><title>Simple Page</title></head>
<body><p>Hello world</p></body>
</html>"""


def _task_event(stderr: str) -> dict[str, object]:
    return json.loads(stderr.strip())


class TestExtractTitle:
    def test_extracts_from_title_tag(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        assert save._extract_title(soup, "https://example.com") == "Test Article Title"

    def test_falls_back_to_h1(self) -> None:
        from bs4 import BeautifulSoup

        html = "<html><body><h1>Heading Title</h1></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        assert save._extract_title(soup, "https://example.com") == "Heading Title"

    def test_falls_back_to_domain(self) -> None:
        from bs4 import BeautifulSoup

        html = "<html><body><p>No title</p></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        assert save._extract_title(soup, "https://example.com/page") == "example.com"


class TestUrlSlug:
    def test_uses_path_segment_plus_hash(self) -> None:
        slug = save._url_slug("https://example.com/blog/my-article")
        assert slug.startswith("my-article-")
        assert len(slug) > len("my-article-")

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
        assert "article" in slug


class TestFindContent:
    def test_finds_article_element(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        content = save._find_content(soup)
        assert content.name == "article"

    def test_falls_back_to_main(self) -> None:
        from bs4 import BeautifulSoup

        html = "<html><body><main><p>Content</p></main></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        content = save._find_content(soup)
        assert content.name == "main"

    def test_falls_back_to_body(self) -> None:
        from bs4 import BeautifulSoup

        html = "<html><body><p>Content</p></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        content = save._find_content(soup)
        assert content.name == "body"


class TestToMarkdown:
    def test_includes_title_and_source(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<article><p>Hello</p></article>", "html.parser")
        md = save._to_markdown(soup, "My Title", "https://example.com")
        assert md.startswith("# My Title\n")
        assert "Source: https://example.com" in md

    def test_includes_paragraphs(self) -> None:
        from bs4 import BeautifulSoup

        html = "<article><p>First para</p><p>Second para</p></article>"
        soup = BeautifulSoup(html, "html.parser")
        md = save._to_markdown(soup, "Title", "https://x.com")
        assert "First para" in md
        assert "Second para" in md

    def test_includes_images(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<article><img src="assets/img.png" alt="Diagram"></article>', "html.parser"
        )
        md = save._to_markdown(soup, "Title", "https://x.com")
        assert "![Diagram](assets/img.png)" in md


class TestSaveUrl:
    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_fetch")
    def test_creates_note_with_correct_structure(
        self,
        mock_fetch: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = SIMPLE_HTML

        result = save.save_url("https://example.com/article", notes_dir=tmp_path)

        assert result.exists()
        assert result.suffix == ".md"
        assert "resources/articles/" in str(result)
        content = result.read_text()
        assert "# Simple Page" in content
        assert "Source: https://example.com/article" in content

    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_fetch")
    def test_downloads_images(
        self,
        mock_fetch: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = SAMPLE_HTML

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b"\x89PNG fake image data"
            mock_response.__enter__ = lambda s: mock_response
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            result = save.save_url("https://example.com/post", notes_dir=tmp_path)

        article_dir = result.parent
        assets_dir = article_dir / "assets"
        assert assets_dir.exists()
        assert any(assets_dir.iterdir())

    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_fetch")
    def test_resaves_existing_article_with_updated_content(
        self,
        mock_fetch: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = SIMPLE_HTML

        # Pre-create the article directory with old content
        slug = save._url_slug("https://example.com/article")
        article_dir = tmp_path / "resources" / "articles" / slug
        article_dir.mkdir(parents=True)
        md_path = article_dir / f"{slug}.md"
        md_path.write_text("old content")

        result = save.save_url("https://example.com/article", notes_dir=tmp_path)

        # Should overwrite with new content
        assert result == md_path
        assert "Hello world" in md_path.read_text()
        mock_git.assert_called_once()

    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_fetch")
    def test_syncs_before_writing(
        self,
        mock_fetch: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = SIMPLE_HTML

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

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        save._git_commit_and_push(tmp_path, article_dir, "Test Article")

        calls = [call.args[0] for call in mock_run.call_args_list]
        assert calls[0] == ["git", "add", "resources/articles/test"]
        assert calls[1][0:3] == ["git", "commit", "-m"]
        assert "Test Article" in calls[1][3]
        assert calls[2] == ["git", "push", "origin", "main"]


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
