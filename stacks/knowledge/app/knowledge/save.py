"""Save a URL as a markdown note with images."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import trafilatura

logger = logging.getLogger(__name__)

_USER_AGENT = "knowledge-save/1.0"
_TIMEOUT = 30
_MIN_IMAGE_BYTES = 2 * 1024
_DECORATIVE_IMAGE_MARKERS = ("icon", "logo", "avatar", "sprite")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def save_url(url: str, *, notes_dir: Path) -> Path:
    """Fetch a URL, convert to markdown note with images, save to notes_dir.

    Returns the path to the saved markdown file.
    """
    _git_sync(notes_dir)

    html = _fetch(url)
    title, body = _extract_content(html, url)
    slug = _url_slug(url)
    article_dir = notes_dir / "resources" / "articles" / slug
    md_path = article_dir / f"{slug}.md"

    if article_dir.exists():
        logger.info("Article already saved: %s (re-saving with updated content)", article_dir)
        shutil.rmtree(article_dir)
    article_dir.mkdir(parents=True, exist_ok=True)

    body = _download_images(body, url, article_dir)
    markdown = _frontmatter(title, url, saved_on=date.today()) + body + "\n"
    md_path.write_text(markdown, encoding="utf-8")

    _git_commit_and_push(notes_dir, article_dir, title)

    logger.info("Saved: %s → %s", url, md_path)
    return md_path


def _fetch(url: str) -> str:
    """Fetch URL content as text."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def _extract_content(html: str, url: str) -> tuple[str, str]:
    """Extract article title and markdown body from HTML.

    Uses trafilatura for content extraction and boilerplate removal.
    Falls back to a simple <title> parse when trafilatura metadata is empty.
    """
    meta = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_images=True)
    body = trafilatura.extract(
        html, url=url, output_format="markdown", include_images=True, include_links=True
    )
    extracted_title: str | None = None
    if meta:
        raw = getattr(meta, "title", None)
        if isinstance(raw, str):
            extracted_title = raw
    title = extracted_title or _title_from_html(html) or urlparse(url).netloc
    return str(title), body or ""


def _title_from_html(html: str) -> str | None:
    """Extract title from <title> tag without a full HTML parser dependency."""

    class _TitleParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self._in_title = False
            self.title: str | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "title":
                self._in_title = True

        def handle_data(self, data: str) -> None:
            if self._in_title:
                self.title = data.strip()

        def handle_endtag(self, tag: str) -> None:
            if tag == "title":
                self._in_title = False

    parser = _TitleParser()
    parser.feed(html)
    return parser.title


def _frontmatter(title: str, source_url: str, *, saved_on: date) -> str:
    """Build YAML frontmatter block."""
    return (
        "---\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f"source: {source_url}\n"
        f"saved: {saved_on.isoformat()}\n"
        "---\n\n"
    )


def _download_images(markdown: str, base_url: str, article_dir: Path) -> str:
    """Download images referenced in markdown and rewrite URLs to local filenames."""
    saved_count = 0

    def _replace_image(match: re.Match[str]) -> str:
        nonlocal saved_count
        alt, src = match.group(1), match.group(2)
        abs_url = urljoin(base_url, src)

        if abs_url.startswith("data:"):
            return ""
        if any(marker in src.lower() for marker in _DECORATIVE_IMAGE_MARKERS):
            return ""

        try:
            req = urllib.request.Request(abs_url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
                image_bytes = response.read()
                if len(image_bytes) < _MIN_IMAGE_BYTES:
                    return ""
                saved_count += 1
                ext = _image_extension(abs_url, _content_type(response))
                filename = f"{saved_count:03d}{ext}"
                (article_dir / filename).write_bytes(image_bytes)
            return f"![{alt}]({filename})"
        except Exception:
            logger.warning("Failed to download image: %s", abs_url)
            return f"![{alt}]({abs_url})"

    return _IMAGE_RE.sub(_replace_image, markdown)


def _content_type(response: object) -> str | None:
    """Read a content type from a urllib response if available."""
    headers = getattr(response, "headers", None)
    get_content_type = getattr(headers, "get_content_type", None)
    if not callable(get_content_type):
        return None

    content_type = get_content_type()
    if not isinstance(content_type, str):
        return None
    return content_type


def _image_extension(url: str, content_type: str | None = None) -> str:
    """Guess image extension from URL."""
    if content_type:
        content_types = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/svg+xml": ".svg",
            "image/webp": ".webp",
            "image/avif": ".avif",
        }
        if content_type in content_types:
            return content_types[content_type]

    path = urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif"):
        if path.endswith(ext):
            return ext
    return ".png"


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80].strip("-")


def _url_slug(url: str) -> str:
    """Derive a stable slug from the URL path segment."""
    path_segment = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "page"
    return _slugify(path_segment) or "page"


def _git(notes_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=notes_dir,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_sync(notes_dir: Path) -> None:
    """Fetch and reset to origin/main so we're on the latest state before writing."""
    _git(notes_dir, "fetch", "origin", "main")
    _git(notes_dir, "reset", "--hard", "origin/main")


def _git_commit_and_push(notes_dir: Path, article_dir: Path, title: str) -> None:
    """Commit the saved article and push to origin."""
    relative_path = article_dir.relative_to(notes_dir)
    _git(notes_dir, "add", str(relative_path))
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=notes_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode == 0:
        logger.info("Article content unchanged after save: %s", relative_path)
        return
    _git(notes_dir, "commit", "-m", f"Save article: {title}")
    _git(notes_dir, "push", "origin", "main")
