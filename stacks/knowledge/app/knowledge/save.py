"""Save a URL as a markdown note with images."""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

_USER_AGENT = "knowledge-save/1.0"
_TIMEOUT = 30
_STRIP_ELEMENTS = ("nav", "header", "footer", "script", "style", "aside", "noscript")


def save_url(url: str, *, notes_dir: Path) -> Path:
    """Fetch a URL, convert to markdown note with images, save to notes_dir.

    Returns the path to the saved markdown file.
    """
    # Sync to latest remote state before writing anything
    _git_sync(notes_dir)

    html = _fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    title = _extract_title(soup, url)
    slug = _url_slug(url)
    article_dir = notes_dir / "resources" / "articles" / slug
    assets_dir = article_dir / "assets"
    md_path = article_dir / f"{slug}.md"

    if article_dir.exists():
        logger.info("Article already saved: %s (re-saving with updated content)", article_dir)

    assets_dir.mkdir(parents=True, exist_ok=True)

    # Strip boilerplate elements
    for tag_name in _STRIP_ELEMENTS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Find the main content area
    content_element = _find_content(soup)

    # Download images and rewrite src to local paths
    _download_images(content_element, url, assets_dir, slug)

    # Convert to markdown-ish text
    markdown = _to_markdown(content_element, title, url)

    # Write the note
    md_path.write_text(markdown, encoding="utf-8")

    # Git commit and push
    _git_commit_and_push(notes_dir, article_dir, title)

    logger.info("Saved: %s → %s", url, md_path)
    return md_path


def _fetch(url: str) -> str:
    """Fetch URL content as text."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def _extract_title(soup: BeautifulSoup, fallback_url: str) -> str:
    """Extract page title from <title> or <h1>."""
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return urlparse(fallback_url).netloc


def _find_content(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    """Find the main content element, falling back to body."""
    for selector in ("article", "main", '[role="main"]'):
        element = soup.find(selector)
        if element:
            return element
    return soup.body or soup


def _download_images(
    content: Tag | BeautifulSoup,
    base_url: str,
    assets_dir: Path,
    slug: str,
) -> None:
    """Download images and rewrite src attributes to local relative paths."""
    for i, img in enumerate(content.find_all("img")):
        src = img.get("src")
        if not isinstance(src, str) or not src:
            continue

        abs_url = urljoin(base_url, src)
        ext = _image_extension(abs_url)
        filename = f"{slug}-{i:03d}{ext}"
        local_path = assets_dir / filename

        try:
            req = urllib.request.Request(abs_url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
                local_path.write_bytes(response.read())
            img["src"] = f"assets/{filename}"
        except Exception:
            logger.warning("Failed to download image: %s", abs_url)
            img["src"] = abs_url


def _image_extension(url: str) -> str:
    """Guess image extension from URL."""
    path = urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif"):
        if path.endswith(ext):
            return ext
    return ".png"


def _to_markdown(content: Tag | BeautifulSoup, title: str, source_url: str) -> str:
    """Convert HTML content to a readable markdown note."""
    lines: list[str] = [f"# {title}", "", f"Source: {source_url}", ""]

    heading_prefix = {"h1": "##", "h2": "##", "h3": "###", "h4": "####"}

    for element in content.find_all(["h1", "h2", "h3", "h4", "p", "li", "img", "pre", "code"]):
        if element.name in heading_prefix:
            lines.append(f"{heading_prefix[element.name]} {element.get_text(strip=True)}")
            lines.append("")
        elif element.name == "p":
            text = element.get_text(strip=True)
            if text:
                lines.append(text)
                lines.append("")
        elif element.name == "li":
            lines.append(f"- {element.get_text(strip=True)}")
        elif element.name == "img":
            alt = element.get("alt", "")
            src = element.get("src", "")
            lines.append(f"![{alt}]({src})")
            lines.append("")
        elif element.name == "pre":
            code = element.get_text()
            lines.append(f"```\n{code}\n```")
            lines.append("")

    return "\n".join(lines) + "\n"


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80].strip("-")


def _url_slug(url: str) -> str:
    """Derive a stable, unique slug from a URL (not title-dependent)."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:10]
    # Use the last path segment for readability, plus hash for uniqueness
    path_segment = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "page"
    readable = _slugify(path_segment)[:60]
    return f"{readable}-{url_hash}"


def _git_sync(notes_dir: Path) -> None:
    """Fetch and reset to origin/main so we're on the latest state before writing."""
    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=notes_dir,
            capture_output=True,
            text=True,
            check=True,
        )

    _git("fetch", "origin", "main")
    _git("reset", "--hard", "origin/main")


def _git_commit_and_push(notes_dir: Path, article_dir: Path, title: str) -> None:
    """Commit the saved article and push to origin."""
    relative_path = article_dir.relative_to(notes_dir)

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=notes_dir,
            capture_output=True,
            text=True,
            check=True,
        )

    _git("add", str(relative_path))
    _git("commit", "-m", f"Save article: {title}")
    _git("push", "origin", "main")
