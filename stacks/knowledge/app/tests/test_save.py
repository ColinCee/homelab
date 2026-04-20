"""Tests for the save module (URL → markdown note)."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from datetime import date
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
    def test_includes_frontmatter(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<article><p>Hello</p></article>", "html.parser")
        md = save._to_markdown(
            soup,
            "My Title",
            "https://example.com",
            saved_on=date(2026, 4, 20),
        )
        assert md.startswith(
            '---\ntitle: "My Title"\nsource: https://example.com\nsaved: 2026-04-20\n---\n'
        )

    def test_includes_paragraphs(self) -> None:
        from bs4 import BeautifulSoup

        html = "<article><p>First para</p><p>Second para</p></article>"
        soup = BeautifulSoup(html, "html.parser")
        md = save._to_markdown(soup, "Title", "https://x.com", saved_on=date(2026, 4, 20))
        assert "First para" in md
        assert "Second para" in md

    def test_includes_images(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup('<article><img src="001.png" alt="Diagram"></article>', "html.parser")
        md = save._to_markdown(soup, "Title", "https://x.com", saved_on=date(2026, 4, 20))
        assert "![Diagram](001.png)" in md

    def test_does_not_duplicate_paragraphs_inside_list_items(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<article><ul><li><p>One item</p></li></ul></article>", "html.parser")
        md = save._to_markdown(soup, "Title", "https://x.com", saved_on=date(2026, 4, 20))
        assert md.count("One item") == 1


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
        assert result == tmp_path / "resources" / "articles" / "article" / "article.md"
        content = result.read_text()
        assert 'title: "Simple Page"' in content
        assert "source: https://example.com/article" in content

    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_fetch")
    def test_downloads_meaningful_images_next_to_markdown(
        self,
        mock_fetch: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = SAMPLE_HTML

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b"\x89PNG" + (b"x" * 3000)
            mock_response.headers.get_content_type.return_value = "image/png"
            mock_response.__enter__ = lambda s: mock_response
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            result = save.save_url("https://example.com/post", notes_dir=tmp_path)

        article_dir = result.parent
        image_paths = sorted(path.name for path in article_dir.iterdir() if path.suffix == ".png")
        assert image_paths == ["001.png"]
        assert "![Architecture diagram](001.png)" in result.read_text()

    @patch.object(save, "_git_commit_and_push")
    @patch.object(save, "_git_sync")
    @patch.object(save, "_fetch")
    def test_filters_boilerplate_and_junk_images(
        self,
        mock_fetch: MagicMock,
        mock_sync: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = """<html><body>
        <main>
            <div class="top-nav">Products</div>
            <article>
                <p>Real content.</p>
                <div class="share-menu">Share this</div>
                <img src="/images/logo.png" alt="Site logo">
                <img src="/images/icon.png" width="32" height="32" alt="">
                <img src="/images/real-image.png" alt="Real diagram">
                <audio>Play me</audio>
                <div role="navigation">The Latest</div>
            </article>
            <div class="footer-links">Meta © 2026</div>
        </main>
        </body></html>"""

        def fake_urlopen(request: object, timeout: int) -> MagicMock:
            url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
            response = MagicMock()
            response.__enter__ = lambda s: response
            response.__exit__ = MagicMock(return_value=False)
            response.headers.get_content_type.return_value = "image/png"
            if url.endswith("real-image.png"):
                response.read.return_value = b"\x89PNG" + (b"x" * 3000)
            else:
                response.read.return_value = b"\x89PNG" + (b"x" * 100)
            return response

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save.save_url("https://example.com/post", notes_dir=tmp_path)

        markdown = result.read_text()
        article_dir = result.parent
        assert "Real content." in markdown
        assert "Products" not in markdown
        assert "Share this" not in markdown
        assert "The Latest" not in markdown
        assert "Meta © 2026" not in markdown
        assert "Play me" not in markdown
        assert "logo.png" not in markdown
        assert "icon.png" not in markdown
        assert "![Real diagram](001.png)" in markdown
        assert sorted(path.name for path in article_dir.iterdir()) == ["001.png", "post.md"]

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
