from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin
from typing import Callable

import httpx


_META_DATE_KEYWORDS = ("publish", "updated", "modified", "date", "time")
_TEXT_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas"}
_HREF_PATTERN = re.compile(r"""href=['"]([^'"#]+)['"]""", flags=re.IGNORECASE)
_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", flags=re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class ParsedHtmlPage:
    url: str
    final_url: str
    title: str
    text: str
    links: list[str]
    meta_publish_times: list[str]
    content_type: str
    link_candidates: list[dict[str, str | None]] = field(default_factory=list)


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._article_depth = 0
        self._general_chunks: list[str] = []
        self._article_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        lowered = tag.lower()
        if lowered in _TEXT_SKIP_TAGS:
            self._skip_depth += 1
        if lowered == "article":
            self._article_depth += 1

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        lowered = tag.lower()
        if lowered in _TEXT_SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if lowered == "article" and self._article_depth > 0:
            self._article_depth -= 1

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._skip_depth > 0:
            return
        token = _clean_text(data)
        if not token:
            return
        self._general_chunks.append(token)
        if self._article_depth > 0:
            self._article_chunks.append(token)

    def text(self) -> str:
        preferred = " ".join(self._article_chunks).strip()
        if len(preferred) >= 240:
            return preferred
        return " ".join(self._general_chunks).strip()


def fetch_parsed_html(
    url: str,
    *,
    timeout_seconds: float = 15.0,
    max_bytes: int = 1_500_000,
    max_chars: int = 10_000,
    url_validator: Callable[[str], str] | None = None,
    max_redirects: int = 5,
) -> ParsedHtmlPage:
    current_url = url_validator(url) if url_validator else url
    final_url = current_url
    content_type = ""
    html = ""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MonitorHtmlParser/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            resp = client.get(current_url, headers=headers)
            if resp.is_redirect:
                location = resp.headers.get("Location", "").strip()
                if not location:
                    raise RuntimeError("Redirect response missing Location header")
                next_url = urljoin(str(resp.url), location)
                current_url = url_validator(next_url) if url_validator else next_url
                continue
            resp.raise_for_status()
            raw = resp.content[:max_bytes]
            content_type = str(resp.headers.get("Content-Type", ""))
            charset = _charset_from_content_type(content_type)
            html = raw.decode(charset or "utf-8", errors="ignore")
            final_url = str(resp.url)
            break
        else:
            raise RuntimeError("Too many redirects while fetching HTML")

    title = _extract_title(html)
    collector = _TextCollector()
    collector.feed(html)
    text = _clean_text(collector.text())[:max_chars]
    link_candidates = _extract_link_candidates(html, final_url)
    links = [str(row.get("url") or "") for row in link_candidates if str(row.get("url") or "").strip()]
    meta_publish_times = _extract_meta_publish_times(html)

    return ParsedHtmlPage(
        url=url,
        final_url=final_url,
        title=title,
        text=text,
        links=links,
        link_candidates=link_candidates,
        meta_publish_times=meta_publish_times,
        content_type=content_type,
    )


def _extract_title(html: str) -> str:
    match = _TITLE_PATTERN.search(html or "")
    if not match:
        return ""
    return _clean_text(unescape(match.group(1)))


def _extract_links(html: str, base_url: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for href in _HREF_PATTERN.findall(html or ""):
        candidate = href.strip()
        if not candidate:
            continue
        abs_url = urljoin(base_url, candidate)
        if not abs_url.lower().startswith(("http://", "https://")):
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        out.append(abs_url)
    return out


class _LinkCandidateCollector(HTMLParser):
    _CONTAINER_TAGS = {"article", "section", "li", "div", "main", "aside"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.frames: list[dict[str, object]] = [{"tag": "root", "time_hint": None, "links": []}]
        self.results: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        lowered = tag.lower()
        attrs_map = {str(key).lower(): str(value or "") for key, value in attrs}
        if lowered in self._CONTAINER_TAGS:
            self.frames.append({"tag": lowered, "time_hint": None, "links": []})

        if lowered == "time":
            dt_value = _clean_text(attrs_map.get("datetime", ""))
            if dt_value:
                self._current_frame()["time_hint"] = dt_value

        if lowered == "a":
            href = attrs_map.get("href", "").strip()
            if not href:
                return
            abs_url = urljoin(self.base_url, href)
            abs_url = urldefrag(abs_url)[0]
            if not abs_url.lower().startswith(("http://", "https://")):
                return
            self._current_frame_links().append(abs_url)

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        lowered = tag.lower()
        if lowered not in self._CONTAINER_TAGS or len(self.frames) <= 1:
            return
        frame = self.frames.pop()
        parent_links = self._current_frame_links()
        links = [str(url or "").strip() for url in frame.get("links", [])]
        time_hint = frame.get("time_hint")
        if time_hint:
            for url in links:
                if not url:
                    continue
                self.results.append({"url": url, "publish_time": str(time_hint)})
        else:
            parent_links.extend(links)

    def finalize(self) -> list[dict[str, str | None]]:
        while len(self.frames) > 1:
            self.handle_endtag(str(self.frames[-1].get("tag", "")))
        root = self.frames[0]
        for url in [str(token or "").strip() for token in root.get("links", [])]:
            if not url:
                continue
            self.results.append({"url": url, "publish_time": None})

        deduped: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for row in self.results:
            url = str(row.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append({"url": url, "publish_time": row.get("publish_time")})
        return deduped

    def _current_frame(self) -> dict[str, object]:
        return self.frames[-1]

    def _current_frame_links(self) -> list[str]:
        links = self._current_frame().setdefault("links", [])
        assert isinstance(links, list)
        return links


def _extract_link_candidates(html: str, base_url: str) -> list[dict[str, str | None]]:
    collector = _LinkCandidateCollector(base_url)
    collector.feed(html or "")
    return collector.finalize()


def _extract_meta_publish_times(html: str) -> list[str]:
    values: list[str] = []
    meta_pattern = re.compile(
        r"<meta[^>]+(?:property|name|itemprop)=['\"]([^'\"]+)['\"][^>]*content=['\"]([^'\"]+)['\"][^>]*>",
        flags=re.IGNORECASE,
    )
    for key, content in meta_pattern.findall(html or ""):
        lowered = key.strip().lower()
        if not any(token in lowered for token in _META_DATE_KEYWORDS):
            continue
        token = _clean_text(unescape(content))
        if token:
            values.append(token)

    time_pattern = re.compile(r"<time[^>]+datetime=['\"]([^'\"]+)['\"][^>]*>", flags=re.IGNORECASE)
    values.extend(_clean_text(unescape(v)) for v in time_pattern.findall(html or ""))

    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        token = raw.strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out[:10]


def _charset_from_content_type(content_type: str) -> str | None:
    lowered = content_type.lower()
    match = re.search(r"charset=([a-z0-9_-]+)", lowered)
    if not match:
        return None
    token = match.group(1).strip()
    return token or None


def _clean_text(text: str) -> str:
    token = str(text or "")
    token = unescape(token)
    token = token.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    token = re.sub(r"\s+", " ", token)
    return token.strip()
