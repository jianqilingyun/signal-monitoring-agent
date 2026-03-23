from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from monitor_agent.core.models import MonitorConfig, RawItem, SourceCursorState
from monitor_agent.core.storage import Storage
from monitor_agent.ingestion_layer.html_ingestor import HtmlIngestor
from monitor_agent.ingestion_layer.playwright_ingestor import PlaywrightIngestor
from monitor_agent.ingestion_layer.source_advisories import build_source_advisories
from monitor_agent.ingestion_layer.rss_ingestor import RSSIngestor
from monitor_agent.ingestion_layer.source_cursor import make_source_key, normalize_url

logger = logging.getLogger(__name__)
_RSS_SOURCE_MAX_WORKERS = 4


class IngestionManager:
    def __init__(self, config: MonitorConfig, playwright_profile_dir: str, storage: Storage | None = None) -> None:
        self.config = config
        self.playwright_profile_dir = playwright_profile_dir
        self.storage = storage
        self.last_incremental_stats: dict[str, dict[str, int]] = {}
        self.last_source_health: dict[str, dict[str, object]] = {}
        self.last_source_advisories: list[dict[str, object]] = []

    def ingest_all(self) -> tuple[list[RawItem], list[str]]:
        all_items: list[RawItem] = []
        errors: list[str] = []
        incremental_stats: dict[str, dict[str, object]] = {}
        source_health: dict[str, dict[str, object]] = {}
        cursor_states = self.storage.load_source_cursors() if self.storage is not None else {}
        strategy_cache = self.storage.load_source_strategy_cache() if self.storage is not None else {}

        rss_sources = self.config.sources.rss
        if rss_sources:
            workers = min(_RSS_SOURCE_MAX_WORKERS, len(rss_sources))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._ingest_rss_source,
                        source,
                        self.playwright_profile_dir,
                        self.config.playwright,
                        cursor_states.get(make_source_key("rss", source.url)),
                    ): source
                    for source in rss_sources
                    if not self._record_skip_if_needed(
                        source_health,
                        source_type="rss",
                        source_name=source.name,
                        source_url=source.url,
                        cursor_state=cursor_states.get(make_source_key("rss", source.url)),
                        strategy_cache=strategy_cache,
                        refresh_interval_hours=source.refresh_interval_hours,
                    )
                }
                for future in as_completed(futures):
                    source = futures[future]
                    source_name = source.name
                    try:
                        items, cursor_state, stats = future.result()
                        all_items.extend(items)
                        cursor_states[cursor_state.source_key] = cursor_state
                        incremental_stats[cursor_state.source_key] = {
                            **stats,
                            "source_name": source_name,
                            "source_type": "rss",
                            "source_url": source.url,
                        }
                        source_health[cursor_state.source_key] = {
                            "source_name": source_name,
                            "source_type": "rss",
                            "source_url": source.url,
                            "status": "success",
                            "items_emitted": len(items),
                            **stats,
                        }
                    except Exception as exc:
                        msg = f"RSS source '{source_name}' failed: {exc}"
                        logger.exception(msg)
                        errors.append(msg)
                        source_health[make_source_key("rss", source.url)] = {
                            "source_name": source_name,
                            "source_type": "rss",
                            "source_url": source.url,
                            "status": "error",
                            "error": str(exc),
                        }

        for source in self.config.sources.playwright:
            source_key = make_source_key("playwright", source.url)
            existing_cursor = cursor_states.get(source_key)
            if self._record_skip_if_needed(
                source_health,
                source_type="playwright",
                source_name=source.name,
                source_url=source.url,
                cursor_state=existing_cursor,
                strategy_cache=strategy_cache,
                refresh_interval_hours=source.refresh_interval_hours,
            ):
                continue
            if source.force_playwright:
                try:
                    ingestor = PlaywrightIngestor(
                        source,
                        profile_dir=self.playwright_profile_dir,
                        runtime=self.config.playwright,
                        cursor_state=existing_cursor,
                    )
                    items = ingestor.ingest()
                    all_items.extend(items)
                    cursor_states[ingestor.cursor_state.source_key] = ingestor.cursor_state
                    incremental_stats[ingestor.cursor_state.source_key] = {
                        **ingestor.incremental_stats,
                        "source_name": source.name,
                        "source_type": "playwright",
                        "source_url": source.url,
                    }
                    source_health[ingestor.cursor_state.source_key] = {
                        "source_name": source.name,
                        "source_type": "playwright",
                        "source_url": source.url,
                        "status": "success",
                        "items_emitted": len(items),
                        **ingestor.incremental_stats,
                    }
                except Exception as exc:
                    msg = f"Playwright source '{source.name}' failed: {exc}"
                    logger.exception(msg)
                    errors.append(msg)
                    source_health[source_key] = {
                        "source_name": source.name,
                        "source_type": "playwright",
                        "source_url": source.url,
                        "status": "error",
                        "error": str(exc),
                    }
                continue

            html_error: Exception | None = None
            try:
                html_ingestor = HtmlIngestor(source, cursor_state=existing_cursor)
                html_items = html_ingestor.ingest()
                if not self._has_useful_content(html_items):
                    raise RuntimeError("HTML parser returned empty content")
                all_items.extend(html_items)
                cursor_states[html_ingestor.cursor_state.source_key] = html_ingestor.cursor_state
                incremental_stats[html_ingestor.cursor_state.source_key] = {
                    **html_ingestor.incremental_stats,
                    "source_name": source.name,
                    "source_type": "html",
                    "source_url": source.url,
                }
                source_health[html_ingestor.cursor_state.source_key] = {
                    "source_name": source.name,
                    "source_type": "html",
                    "source_url": source.url,
                    "status": "success",
                    "items_emitted": len(html_items),
                    **html_ingestor.incremental_stats,
                }
                continue
            except Exception as exc:
                html_error = exc
                logger.warning(
                    "HTML parser source '%s' failed, falling back to Playwright: %s",
                    source.name,
                    exc,
                )

            try:
                pw_ingestor = PlaywrightIngestor(
                    source,
                    profile_dir=self.playwright_profile_dir,
                    runtime=self.config.playwright,
                    cursor_state=existing_cursor,
                )
                pw_items = pw_ingestor.ingest()
                all_items.extend(pw_items)
                cursor_states[pw_ingestor.cursor_state.source_key] = pw_ingestor.cursor_state
                incremental_stats[pw_ingestor.cursor_state.source_key] = {
                    **pw_ingestor.incremental_stats,
                    "source_name": source.name,
                    "source_type": "playwright",
                    "source_url": source.url,
                }
                source_health[pw_ingestor.cursor_state.source_key] = {
                    "source_name": source.name,
                    "source_type": "playwright",
                    "source_url": source.url,
                    "status": "success",
                    "items_emitted": len(pw_items),
                    **pw_ingestor.incremental_stats,
                }
            except Exception as exc:
                msg = (
                    f"Web source '{source.name}' failed: html_parser={html_error}; "
                    f"playwright={exc}"
                )
                logger.exception(msg)
                errors.append(msg)
                source_health[source_key] = {
                    "source_name": source.name,
                    "source_type": "playwright",
                    "source_url": source.url,
                    "status": "error",
                    "error": str(exc),
                }

        if self.storage is not None:
            self.storage.save_source_cursors(cursor_states)
        self.last_incremental_stats = incremental_stats
        self.last_source_health = source_health
        self.last_source_advisories = [
            advisory.model_dump(mode="json")
            for advisory in build_source_advisories(source_health, strategy_cache)
        ]
        return all_items, errors

    @staticmethod
    def _ingest_rss_source(
        source,
        playwright_profile_dir: str,
        playwright_runtime,
        cursor_state: SourceCursorState | None,
    ) -> tuple[list[RawItem], SourceCursorState, dict[str, int]]:
        ingestor = RSSIngestor(
            source,
            playwright_profile_dir=playwright_profile_dir,
            playwright_runtime=playwright_runtime,
            cursor_state=cursor_state,
        )
        items = ingestor.ingest()
        return items, ingestor.cursor_state, ingestor.incremental_stats

    @staticmethod
    def _has_useful_content(items: list[RawItem]) -> bool:
        for item in items:
            if str(item.content or "").strip():
                return True
        return False

    def _record_skip_if_needed(
        self,
        source_health: dict[str, dict[str, object]],
        *,
        source_type: str,
        source_name: str,
        source_url: str,
        cursor_state: SourceCursorState | None,
        strategy_cache: dict[str, object],
        refresh_interval_hours: int | None,
    ) -> bool:
        if cursor_state is None or cursor_state.last_success_at is None:
            return False
        interval_hours = self._effective_refresh_interval_hours(
            source_type=source_type,
            source_url=source_url,
            strategy_cache=strategy_cache,
            explicit_refresh_interval_hours=refresh_interval_hours,
        )
        last_success = cursor_state.last_success_at.astimezone(UTC)
        elapsed_hours = max(0.0, (datetime.now(UTC) - last_success).total_seconds() / 3600.0)
        if elapsed_hours >= float(interval_hours):
            return False

        source_key = make_source_key(source_type, source_url)
        source_health[source_key] = {
            "source_name": source_name,
            "source_type": source_type,
            "source_url": source_url,
            "status": "skipped",
            "skip_reason": "refresh_interval_not_due",
            "refresh_interval_hours": interval_hours,
            "hours_since_last_success": round(elapsed_hours, 3),
        }
        logger.info(
            "Skipping source %s (%s); next fetch due after %.1fh, elapsed=%.2fh",
            source_name,
            source_type,
            float(interval_hours),
            elapsed_hours,
        )
        return True

    @staticmethod
    def _effective_refresh_interval_hours(
        *,
        source_type: str,
        source_url: str,
        strategy_cache: dict[str, object],
        explicit_refresh_interval_hours: int | None,
    ) -> int:
        if explicit_refresh_interval_hours is not None:
            return max(1, int(explicit_refresh_interval_hours))

        cache_key = normalize_url(source_url).lower()
        payload = strategy_cache.get(cache_key)
        analysis = payload.get("analysis", {}) if isinstance(payload, dict) else {}
        if not isinstance(analysis, dict):
            analysis = {}

        if source_type == "rss":
            hint = str(analysis.get("update_frequency_hint") or "").strip().lower()
            if hint == "high":
                return 6
            if hint == "daily":
                return 12
            if hint == "every_few_days":
                return 36
            if hint == "weekly_or_slower":
                return 72
            return 24

        if bool(analysis.get("list_like_page")):
            return 12
        if bool(analysis.get("playwright_needed")):
            return 18
        return 24
