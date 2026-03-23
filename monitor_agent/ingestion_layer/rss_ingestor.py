from __future__ import annotations

import re
import logging
import time
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from urllib.parse import urlparse

import feedparser

from monitor_agent.core.models import PlaywrightRuntimeConfig, PlaywrightSourceConfig, RawItem, RssSourceConfig
from monitor_agent.core.utils import utc_now
from monitor_agent.ingestion_layer.html_parser import ParsedHtmlPage, fetch_parsed_html
from monitor_agent.ingestion_layer.playwright_ingestor import PlaywrightIngestor
from monitor_agent.ingestion_layer.source_cursor import advance_rss_cursor, cursor_from_mapping, filter_rss_rows

logger = logging.getLogger(__name__)
_SECOND_HOP_SKIP_FEED_HOSTS = {"hnrss.org", "news.google.com"}
_SECOND_HOP_MAX_WORKERS = 4


class RSSIngestor:
    def __init__(
        self,
        source: RssSourceConfig,
        *,
        playwright_profile_dir: str | None = None,
        playwright_runtime: PlaywrightRuntimeConfig | None = None,
        cursor_state=None,
    ) -> None:
        self.source = source
        self._blocked_domains: set[str] = set()
        self._domain_failures: dict[str, int] = {}
        self._feed_host = self._host(self.source.url)
        self._skip_second_hop_for_feed = self._feed_host in _SECOND_HOP_SKIP_FEED_HOSTS
        self._playwright_profile_dir = playwright_profile_dir
        self._playwright_runtime = playwright_runtime
        self.cursor_state = cursor_from_mapping(
            cursor_state.model_dump(mode="python") if hasattr(cursor_state, "model_dump") else cursor_state,
            source_type="rss",
            source_url=self.source.url,
            overlap_count=self.source.incremental_overlap_count,
        )
        self.incremental_stats: dict[str, int] = {
            "candidate_count": 0,
            "kept_count": 0,
            "overlap_kept": 0,
            "dropped_count": 0,
        }

    def ingest(self) -> list[RawItem]:
        logger.info("Fetching RSS feed: %s", self.source.url)
        feed = self._parse_feed_with_retry(self.source.url)

        rows: list[dict[str, object]] = []
        for entry in feed.entries[: self.source.max_items]:
            title = getattr(entry, "title", "Untitled")
            link = getattr(entry, "link", None)
            feed_text, feed_text_source = self._extract_feed_text(entry)
            published_at = self._parse_published(entry)
            entry_id = self._entry_identity(entry, link=link, title=title)
            metadata: dict[str, object] = {
                "feed_url": self.source.url,
                "rss_timestamp": published_at.isoformat() if published_at else None,
                "content_source": feed_text_source,
            }
            rows.append(
                {
                    "title": title,
                    "link": str(link) if link else None,
                    "entry_id": entry_id,
                    "published_at": published_at,
                    "feed_text": feed_text,
                    "content": feed_text,
                    "metadata": metadata,
                }
            )

        rows, self.incremental_stats = filter_rss_rows(rows, self.cursor_state)
        if self.incremental_stats["dropped_count"] > 0:
            logger.info(
                "RSS source %s incremental cutoff kept %d/%d rows (overlap=%d)",
                self.source.name,
                self.incremental_stats["kept_count"],
                self.incremental_stats["candidate_count"],
                self.incremental_stats["overlap_kept"],
            )

        tasks: list[tuple[int, str, str]] = []
        for idx, row in enumerate(rows):
            link = str(row.get("link") or "").strip()
            if not self.source.fetch_full_text or not link:
                continue

            metadata = row["metadata"]
            assert isinstance(metadata, dict)
            if self._skip_second_hop_for_feed:
                metadata["second_hop_skipped"] = True
                metadata["second_hop_skip_reason"] = "aggregator_feed"
                continue

            host = self._host(link)
            if host and host in self._blocked_domains:
                metadata["second_hop_skipped"] = True
                metadata["second_hop_skip_reason"] = "domain_blocked_in_run"
                continue
            tasks.append((idx, link, host))

        if tasks:
            workers = min(_SECOND_HOP_MAX_WORKERS, len(tasks))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(self._fetch_article_with_retry, link): (idx, link, host)
                    for idx, link, host in tasks
                }
                for future in as_completed(future_map):
                    idx, link, host = future_map[future]
                    row = rows[idx]
                    metadata = row["metadata"]
                    assert isinstance(metadata, dict)
                    title = str(row.get("title") or "")
                    feed_text = str(row.get("feed_text") or "")
                    feed_text_source = str(metadata.get("content_source") or "rss_summary")
                    try:
                        parsed, error = future.result()
                    except Exception as exc:
                        parsed, error = None, exc

                    if parsed is not None:
                        self._apply_second_hop_success(row, parsed, title, feed_text)
                        if host:
                            self._domain_failures.pop(host, None)
                        continue

                    if host:
                        self._record_second_hop_failure(host, error)
                    logger.warning("RSS second-hop article fetch failed for %s: %s", link, error)
                    metadata["second_hop_error"] = self._truncate_error(error)
                    metadata["content_source"] = feed_text_source

        items: list[RawItem] = []
        for row in rows:
            metadata = row["metadata"]
            assert isinstance(metadata, dict)
            items.append(
                RawItem(
                    source_type="rss",
                    source_name=self.source.name,
                    title=str(row.get("title") or "Untitled"),
                    url=str(row.get("link")) if row.get("link") else None,
                    content=str(row.get("content") or ""),
                    published_at=row.get("published_at"),
                    fetched_at=utc_now(),
                    metadata=metadata,
                )
            )

        self.cursor_state = advance_rss_cursor(rows, self.cursor_state)
        logger.info("RSS source %s yielded %d items", self.source.name, len(items))
        return items

    def _fetch_article_with_retry(self, link: str) -> tuple[ParsedHtmlPage | None, Exception | None]:
        attempts = 2
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                parsed = fetch_parsed_html(
                    str(link),
                    timeout_seconds=float(self.source.article_timeout_seconds),
                    max_chars=int(self.source.article_max_chars),
                )
                return parsed, None
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                if self._is_hard_block_error(exc):
                    break
                time.sleep(0.5 * attempt)
        browser_result = self._fetch_article_with_browser(link, last_error)
        if browser_result is not None:
            return browser_result, None
        return None, last_error

    def _fetch_article_with_browser(self, link: str, prior_error: Exception | None) -> ParsedHtmlPage | None:
        if not self._playwright_profile_dir or self._playwright_runtime is None:
            return None
        try:
            ingestor = PlaywrightIngestor(
                PlaywrightSourceConfig(
                    name=f"{self.source.name} article fallback",
                    url=link,
                    content_selector="body",
                    article_content_selector="body",
                    article_wait_for_selector=None,
                    max_chars=self.source.article_max_chars,
                    timeout_ms=max(5_000, int(self.source.article_timeout_seconds * 1000)),
                    follow_links_enabled=False,
                    force_playwright=True,
                ),
                profile_dir=self._playwright_profile_dir,
                runtime=self._playwright_runtime,
            )
            items = ingestor.ingest()
            if not items:
                return None
            item = items[0]
            return ParsedHtmlPage(
                url=str(item.url or link),
                final_url=str(item.url or link),
                title=str(item.title or ""),
                text=str(item.content or ""),
                links=[],
                meta_publish_times=[str(v) for v in item.metadata.get("meta_publish_times", []) if str(v).strip()]
                if isinstance(item.metadata, dict)
                else [],
                content_type="text/html",
            )
        except Exception as exc:
            logger.warning("RSS browser fallback failed for %s after %s: %s", link, prior_error, exc)
            return None

    @staticmethod
    def _apply_second_hop_success(
        row: dict[str, object],
        parsed: ParsedHtmlPage,
        title: str,
        feed_text: str,
    ) -> None:
        metadata = row["metadata"]
        assert isinstance(metadata, dict)
        if parsed.text:
            if len(parsed.text) >= 200 or len(parsed.text) > len(feed_text):
                row["content"] = parsed.text
                metadata["content_source"] = "article_full_text"
            else:
                metadata["second_hop_note"] = "article_text_shorter_than_feed"
        if parsed.title and (not title or title == "Untitled"):
            row["title"] = parsed.title
        if parsed.meta_publish_times:
            metadata["meta_publish_times"] = parsed.meta_publish_times
        link = str(row.get("link") or "")
        if parsed.final_url and parsed.final_url != link:
            metadata["resolved_url"] = parsed.final_url

    def _record_second_hop_failure(self, host: str, error: Exception | None) -> None:
        if self._is_hard_block_error(error):
            self._blocked_domains.add(host)
            return
        failed = self._domain_failures.get(host, 0) + 1
        self._domain_failures[host] = failed
        if failed >= 2:
            self._blocked_domains.add(host)

    @staticmethod
    def _is_hard_block_error(error: Exception | None) -> bool:
        if error is None:
            return False
        token = str(error).lower()
        return any(
            marker in token
            for marker in (
                "http error 401",
                "http error 403",
                "http error 429",
                "forbidden",
                "unauthorized",
                "too many requests",
                "captcha",
                "access denied",
            )
        )

    @staticmethod
    def _truncate_error(error: Exception | None, limit: int = 220) -> str:
        if error is None:
            return "unknown"
        text = str(error).strip() or error.__class__.__name__
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    def _extract_feed_text(entry: object) -> tuple[str, str]:
        candidates: list[tuple[str, str]] = []
        summary = _to_plain_text(getattr(entry, "summary", ""))
        if summary:
            candidates.append((summary, "rss_summary"))
        description = _to_plain_text(getattr(entry, "description", ""))
        if description:
            candidates.append((description, "rss_description"))

        content_rows = getattr(entry, "content", None)
        if isinstance(content_rows, list):
            for row in content_rows:
                value = ""
                if isinstance(row, dict):
                    value = str(row.get("value", "") or "")
                else:
                    value = str(getattr(row, "value", "") or "")
                token = _to_plain_text(value)
                if token:
                    candidates.append((token, "rss_content"))
                    break

        if not candidates:
            return "", "rss_summary"
        # Prefer the most informative field from feed payload.
        candidates.sort(key=lambda x: len(x[0]), reverse=True)
        return candidates[0]

    @staticmethod
    def _host(url: str) -> str:
        try:
            host = (urlparse(url).hostname or "").strip().lower()
        except Exception:
            return ""
        if host.startswith("www."):
            host = host[4:]
        return host

    @staticmethod
    def _parse_feed_with_retry(url: str):
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return feedparser.parse(
                    url,
                    agent="Mozilla/5.0 (compatible; MonitorRSSBot/1.0)",
                    request_headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"},
                )
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    raise
                logger.warning("RSS fetch failed (%d/%d) for %s: %s", attempt, max_attempts, url, exc)
                time.sleep(0.8 * attempt)
        if last_exc is not None:
            raise last_exc
        return feedparser.parse(url)

    @staticmethod
    def _parse_published(entry: object) -> datetime | None:
        dt_struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if dt_struct is None:
            return None
        return datetime(
            dt_struct.tm_year,
            dt_struct.tm_mon,
            dt_struct.tm_mday,
            dt_struct.tm_hour,
            dt_struct.tm_min,
            dt_struct.tm_sec,
            tzinfo=UTC,
        )

    @staticmethod
    def _entry_identity(entry: object, *, link: object, title: str) -> str:
        for attr in ("id", "guid"):
            value = getattr(entry, attr, None)
            token = str(value or "").strip()
            if token:
                return token
        link_token = str(link or "").strip()
        if link_token:
            return link_token
        return title.strip()


def _to_plain_text(value: object) -> str:
    token = str(value or "")
    if token == "":
        return ""
    token = re.sub(r"<[^>]+>", " ", token)
    token = unescape(token)
    token = token.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    token = re.sub(r"\s+", " ", token)
    return token.strip()
