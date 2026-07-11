"""B61. Content Extraction Pipeline (Trafilatura Pattern).

HTML → clean text/markdown with metadata, sitemap/feed discovery, URL dedup.
Source: trafilatura pattern.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterable


class ContentType(Enum):
    HTML = auto()
    MARKDOWN = auto()
    PLAINTEXT = auto()
    PDF = auto()
    JSON = auto()
    UNKNOWN = auto()


@dataclass
class ExtractedContent:
    title: str = ""
    text: str = ""
    author: str = ""
    date: str = ""
    url: str = ""
    content_type: ContentType = ContentType.PLAINTEXT
    paragraphs: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    word_count: int = 0


class ContentExtractor:
    """Extract clean text and metadata from HTML."""

    # Tags to strip entirely
    STRIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"}
    # Tags that contain main content
    CONTENT_TAGS = {"article", "main", "section", "div"}
    # Block-level tags
    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "td", "th"}

    def extract(self, html: str, url: str = "") -> ExtractedContent:
        result = ExtractedContent(url=url, content_type=ContentType.HTML)

        # Extract title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if title_match:
            result.title = title_match.group(1).strip()

        # Extract metadata
        for match in re.finditer(r'<meta\s+(?:name|property)=["\']([^"\']+)["\']\s+content=["\']([^"\']*)["\']', html, re.IGNORECASE):
            result.metadata[match.group(1)] = match.group(2)
            if match.group(1).lower() == "author":
                result.author = match.group(2)
            if match.group(1).lower() in ("date", "article:published_time", "publish_date"):
                result.date = match.group(2)

        # Strip unwanted tags
        cleaned = html
        for tag in self.STRIP_TAGS:
            cleaned = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)

        # Extract links
        for match in re.finditer(r'href=["\']([^"\']+)["\']', cleaned, re.IGNORECASE):
            result.links.append(match.group(1))

        # Convert to text
        text = cleaned
        # Replace block tags with newlines
        for tag in self.BLOCK_TAGS:
            text = re.sub(rf"</?{tag}[^>]*>", "\n", text, flags=re.IGNORECASE)
        # Replace <br> with newline
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        # Strip all remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Decode entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
        # Normalize whitespace
        text = re.sub(r"\n\s*\n", "\n\n", text).strip()

        result.text = text
        result.paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        result.word_count = len(text.split())
        return result

    def to_markdown(self, content: ExtractedContent) -> str:
        """Convert extracted content to markdown."""
        lines: list[str] = []
        if content.title:
            lines.append(f"# {content.title}")
            lines.append("")
        if content.author:
            lines.append(f"*By {content.author}*")
        if content.date:
            lines.append(f"*{content.date}*")
        if content.url:
            lines.append(f"Source: {content.url}")
        lines.append("")
        for para in content.paragraphs:
            lines.append(para)
            lines.append("")
        return "\n".join(lines).strip()


class URLDeduplicator:
    """Track seen URLs to avoid re-fetching."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_new(self, url: str) -> bool:
        normalized = self._normalize(url)
        if normalized in self._seen:
            return False
        self._seen.add(normalized)
        return True

    def filter_new(self, urls: Iterable[str]) -> list[str]:
        return [u for u in urls if self.is_new(u)]

    @staticmethod
    def _normalize(url: str) -> str:
        url = url.lower().rstrip("/")
        url = re.sub(r"^https?://", "", url)
        url = re.sub(r"www\.", "", url)
        url = url.split("#")[0]
        return url