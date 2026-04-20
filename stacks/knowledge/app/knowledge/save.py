"""Save a URL as a markdown note with images."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

_USER_AGENT = "knowledge-save/1.0"
_TIMEOUT = 30
_MIN_IMAGE_BYTES = 2 * 1024
_STRIP_ELEMENTS = ("nav", "footer", "script", "style", "aside", "noscript", "audio")
_BOILERPLATE_MARKERS = (
    "nav",
    "menu",
    "footer",
    "sidebar",
    "social",
    "share",
    "cookie",
    "banner",
    "toc",
    "table-of-contents",
    "related",
    "recommend",
)
_RELATED_HEADINGS = ("related", "recommended", "more from", "you might also", "further reading")
_DECORATIVE_IMAGE_MARKERS = ("icon", "logo", "avatar", "sprite")


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
    md_path = article_dir / f"{slug}.md"

    if article_dir.exists():
        logger.info("Article already saved: %s (re-saving with updated content)", article_dir)

    # Find the main content area
    content_element = _find_content(soup)
    _strip_boilerplate(content_element)

    if article_dir.exists():
        shutil.rmtree(article_dir)
    article_dir.mkdir(parents=True, exist_ok=True)

    # Download images and rewrite src to local paths
    _download_images(content_element, url, article_dir)

    # Convert to markdown-ish text
    markdown = _to_markdown(content_element, title, url, saved_on=date.today())

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
        element = soup.select_one(selector)
        if element:
            return element
    return soup.body or soup


def _strip_boilerplate(content: Tag | BeautifulSoup) -> None:
    """Remove obvious navigation, footer, and sharing boilerplate."""
    for tag_name in _STRIP_ELEMENTS:
        for tag in content.find_all(tag_name):
            tag.decompose()

    for tag in list(content.find_all(True)):
        if _has_boilerplate_marker(tag):
            tag.decompose()

    _strip_related_sections(content)


def _has_boilerplate_marker(tag: Tag) -> bool:
    """Return True when class or role metadata marks the element as boilerplate."""
    attrs = tag.attrs if isinstance(tag.attrs, dict) else {}
    return _attribute_contains_marker(attrs.get("class")) or _attribute_contains_marker(
        attrs.get("role")
    )


def _attribute_contains_marker(value: object) -> bool:
    """Return True when an attribute value matches known boilerplate markers."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [item for item in value if isinstance(item, str)]
    else:
        return False

    return any(
        marker in raw_value.lower() for raw_value in values for marker in _BOILERPLATE_MARKERS
    )


def _strip_related_sections(content: Tag | BeautifulSoup) -> None:
    """Remove 'Related content' sections identified by heading text."""
    for heading in content.find_all(["h2", "h3", "h4"]):
        heading_text = heading.get_text(strip=True).lower()
        if not any(marker in heading_text for marker in _RELATED_HEADINGS):
            continue

        # Walk up to find a containing section/div to remove whole block
        removed = False
        for ancestor in heading.parents:
            if ancestor is content:
                break
            if ancestor.name in ("section", "aside"):
                ancestor.decompose()
                removed = True
                break

        if not removed:
            # Remove the heading and all following siblings
            for sibling in list(heading.find_next_siblings()):
                sibling.decompose()
            heading.decompose()


def _download_images(
    content: Tag | BeautifulSoup,
    base_url: str,
    article_dir: Path,
) -> None:
    """Download images and rewrite src attributes to local relative paths."""
    saved_image_count = 0
    h1 = content.find("h1")
    title_text = h1.get_text(strip=True).lower() if h1 else ""

    for img in list(content.find_all("img")):
        source = _image_source(img)
        if not source:
            img.decompose()
            continue

        if _is_decorative_image(img, source):
            img.decompose()
            continue

        # Skip hero images that just repeat the title
        alt = img.get("alt", "")
        if (
            isinstance(alt, str)
            and saved_image_count == 0
            and title_text
            and alt.strip().lower() == title_text
        ):
            img.decompose()
            continue

        abs_url = urljoin(base_url, source)
        if abs_url.startswith("data:"):
            img.decompose()
            continue

        try:
            req = urllib.request.Request(abs_url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
                image_bytes = response.read()
                if len(image_bytes) < _MIN_IMAGE_BYTES:
                    img.decompose()
                    continue

                saved_image_count += 1
                ext = _image_extension(abs_url, _content_type(response))
                filename = f"{saved_image_count:03d}{ext}"
                local_path = article_dir / filename
                local_path.write_bytes(image_bytes)
            img["src"] = filename
        except Exception:
            logger.warning("Failed to download image: %s", abs_url)
            img["src"] = abs_url


def _image_source(img: Tag) -> str | None:
    """Return the best available image URL from common attributes."""
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        value = img.get(attr)
        if isinstance(value, str) and value:
            return value

    for attr in ("srcset", "data-srcset"):
        value = img.get(attr)
        if isinstance(value, str) and value:
            return value.split(",", 1)[0].strip().split(" ", 1)[0]

    return None


def _is_decorative_image(img: Tag, source: str) -> bool:
    """Return True when an image is likely decorative boilerplate."""
    lower_source = source.lower()
    if any(marker in lower_source for marker in _DECORATIVE_IMAGE_MARKERS):
        return True

    alt = img.get("alt")
    width = _parse_dimension(img.get("width"))
    height = _parse_dimension(img.get("height"))
    if isinstance(alt, str) and alt.strip():
        return False
    return width is not None and height is not None and width < 100 and height < 100


def _parse_dimension(value: object) -> int | None:
    """Parse an integer width or height attribute."""
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    match = re.search(r"\d+", value)
    if not match:
        return None
    return int(match.group())


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


def _to_markdown(
    content: Tag | BeautifulSoup,
    title: str,
    source_url: str,
    *,
    saved_on: date,
) -> str:
    """Convert HTML content to a readable markdown note."""
    lines: list[str] = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"source: {source_url}",
        f"saved: {saved_on.isoformat()}",
        "---",
        "",
    ]

    heading_prefix = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}

    for element in content.find_all(["h1", "h2", "h3", "h4", "p", "li", "img", "pre"]):
        if element.name in heading_prefix:
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            lines.append(f"{heading_prefix[element.name]} {text}")
            lines.append("")
        elif element.name == "p":
            if element.find_parent("li") is not None:
                continue
            text = element.get_text(" ", strip=True)
            if text:
                lines.append(text)
                lines.append("")
        elif element.name == "li":
            text = element.get_text(" ", strip=True)
            if text:
                lines.append(f"- {text}")
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
    """Derive a stable slug from the URL path segment."""
    path_segment = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "page"
    return _slugify(path_segment) or "page"


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
    _git("commit", "-m", f"Save article: {title}")
    _git("push", "origin", "main")
