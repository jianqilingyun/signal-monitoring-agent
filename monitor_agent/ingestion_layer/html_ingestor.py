from __future__ import annotations

import logging
import re
from urllib.parse import urldefrag, urlparse

from monitor_agent.core.models import PlaywrightSourceConfig, RawItem
from monitor_agent.core.utils import utc_now
from monitor_agent.ingestion_layer.html_parser import ParsedHtmlPage, fetch_parsed_html
from monitor_agent.ingestion_layer.source_cursor import advance_url_cursor, cursor_from_mapping, filter_follow_candidates

logger = logging.getLogger(__name__)


class HtmlIngestor:
    def __init__(self, source: PlaywrightSourceConfig, cursor_state=None) -> None:
        self.source = source
        self.cursor_state = cursor_from_mapping(
            cursor_state.model_dump(mode="python") if hasattr(cursor_state, "model_dump") else cursor_state,
            source_type="html",
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
        logger.info("Scraping source with HTML parser: %s", self.source.url)
        source_name = self._source_display_name()
        items: list[RawItem] = []

        root_page = fetch_parsed_html(
            self.source.url,
            timeout_seconds=self._timeout_seconds(),
            max_chars=self.source.max_chars,
        )
        items.append(
            self._capture_item(
                page=root_page,
                item_url=root_page.final_url or self.source.url,
                source_name=source_name,
                parent_url=None,
                depth=0,
            )
        )

        if self.source.follow_links_enabled and self.source.max_depth >= 1:
            follow_candidates = self._collect_follow_candidates(
                root_page.link_candidates or [{"url": url, "publish_time": None} for url in root_page.links],
                root_url=root_page.final_url or self.source.url,
            )
            kept_candidates, self.incremental_stats = filter_follow_candidates(follow_candidates, self.cursor_state)
            if self.incremental_stats["dropped_count"] > 0:
                logger.info(
                    "HTML source %s incremental cutoff kept %d/%d links (overlap=%d)",
                    self.source.name,
                    self.incremental_stats["kept_count"],
                    self.incremental_stats["candidate_count"],
                    self.incremental_stats["overlap_kept"],
                )
            successful_follow_candidates: list[dict[str, object]] = []
            for idx, candidate in enumerate(kept_candidates, start=1):
                url = str(candidate.get("url") or "").strip()
                publish_time = candidate.get("publish_time")
                if not url:
                    continue
                try:
                    child_page = fetch_parsed_html(
                        url,
                        timeout_seconds=self._timeout_seconds(),
                        max_chars=self.source.max_chars,
                    )
                    items.append(
                        self._capture_item(
                            page=child_page,
                            item_url=child_page.final_url or url,
                            source_name=f"{source_name}#{idx}",
                            parent_url=self.source.url,
                            depth=1,
                        )
                    )
                    successful_follow_candidates.append(
                        {
                            "url": child_page.final_url or url,
                            "publish_time": publish_time,
                        }
                    )
                except Exception as exc:
                    logger.warning("HTML parser follow-link failed for %s: %s", url, exc)
            self.cursor_state = advance_url_cursor(successful_follow_candidates, self.cursor_state)

        return items

    def _capture_item(
        self,
        *,
        page: ParsedHtmlPage,
        item_url: str,
        source_name: str,
        parent_url: str | None,
        depth: int,
    ) -> RawItem:
        title = page.title.strip() or f"Snapshot: {source_name}"
        return RawItem(
            source_type="html",
            source_name=source_name,
            title=title,
            url=item_url,
            content=page.text,
            fetched_at=utc_now(),
            metadata={
                "parser_engine": "html_parser",
                "content_type": page.content_type,
                "meta_publish_times": page.meta_publish_times,
                "parent_url": parent_url,
                "depth": depth,
                "follow_links_enabled": self.source.follow_links_enabled,
            },
        )

    def _collect_follow_candidates(
        self,
        raw_candidates: list[dict[str, str | None]],
        *,
        root_url: str,
    ) -> list[dict[str, str | None]]:
        root_host = (urlparse(root_url).hostname or "").lower()
        accepted: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for raw in raw_candidates:
            token = str(raw.get("url") or "").strip()
            if not token:
                continue
            token = urldefrag(token)[0]
            if not token:
                continue
            if token in seen:
                continue
            seen.add(token)

            if self.source.same_domain_only:
                host = (urlparse(token).hostname or "").lower()
                if not host or host != root_host:
                    continue

            if self.source.exclude_url_patterns and _matches_any_pattern(token, self.source.exclude_url_patterns):
                continue

            if self.source.article_url_patterns and not _matches_any_pattern(token, self.source.article_url_patterns):
                continue

            if token == root_url:
                continue

            accepted.append(
                {
                    "url": token,
                    "publish_time": str(raw.get("publish_time") or "").strip() or None,
                }
            )
            if len(accepted) >= self.source.max_links_per_source:
                break
        return accepted

    def _source_display_name(self) -> str:
        name = str(getattr(self.source, "name", "") or "").strip()
        if name and name.lower() != "none":
            return name
        host = (urlparse(self.source.url).hostname or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        return host or self.source.url

    def _timeout_seconds(self) -> float:
        timeout_ms = int(getattr(self.source, "timeout_ms", 30_000) or 30_000)
        timeout_seconds = timeout_ms / 1000.0
        return max(5.0, timeout_seconds)


def _matches_any_pattern(url: str, patterns: list[str]) -> bool:
    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        try:
            if re.search(pattern, url, flags=re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in url.lower():
                return True
    return False
