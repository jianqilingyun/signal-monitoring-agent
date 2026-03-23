from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from monitor_agent.core.models import PlaywrightRuntimeConfig, PlaywrightSourceConfig, RawItem
from monitor_agent.core.utils import utc_now
from monitor_agent.ingestion_layer.source_cursor import advance_url_cursor, cursor_from_mapping, filter_follow_candidates

logger = logging.getLogger(__name__)


class PlaywrightIngestor:
    def __init__(
        self,
        source: PlaywrightSourceConfig,
        profile_dir: str,
        runtime: PlaywrightRuntimeConfig | None = None,
        cursor_state=None,
    ) -> None:
        self.source = source
        self.profile_dir = profile_dir
        self.runtime = runtime or PlaywrightRuntimeConfig()
        self.cursor_state = cursor_from_mapping(
            cursor_state.model_dump(mode="python") if hasattr(cursor_state, "model_dump") else cursor_state,
            source_type="playwright",
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
        logger.info("Scraping source with Playwright: %s", self.source.url)
        items: list[RawItem] = []
        source_name = self._source_display_name()

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                **self.build_context_options(self.profile_dir, self.runtime)
            )
            root_page = context.new_page()
            root_navigated = self._goto_with_retry(root_page, self.source.url, self.source.timeout_ms)
            root_item = self._capture_item(
                page=root_page,
                item_url=self.source.url,
                source_name=source_name,
                wait_selector=self.source.wait_for_selector,
                content_selector=self.source.content_selector,
                max_chars=self.source.max_chars,
                parent_url=None,
                depth=0,
                navigation_complete=root_navigated,
            )
            items.append(root_item)

            if self.source.follow_links_enabled and self.source.max_depth >= 1:
                follow_candidates = self._collect_follow_candidates(root_page, self.source.url)
                kept_candidates, self.incremental_stats = filter_follow_candidates(follow_candidates, self.cursor_state)
                if self.incremental_stats["dropped_count"] > 0:
                    logger.info(
                        "Playwright source %s incremental cutoff kept %d/%d links (overlap=%d)",
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
                        article_page = context.new_page()
                        child_navigated = self._goto_with_retry(article_page, url, self.source.timeout_ms)
                        child_item = self._capture_item(
                            page=article_page,
                            item_url=url,
                            source_name=f"{source_name}#{idx}",
                            wait_selector=self.source.article_wait_for_selector,
                            content_selector=self.source.article_content_selector or "body",
                            max_chars=self.source.max_chars,
                            parent_url=self.source.url,
                            depth=1,
                            navigation_complete=child_navigated,
                        )
                        items.append(child_item)
                        successful_follow_candidates.append(
                            {
                                "url": child_item.url or url,
                                "publish_time": publish_time,
                            }
                        )
                    except Exception as exc:
                        logger.warning("Playwright follow-link failed for %s: %s", url, exc)
                    finally:
                        try:
                            article_page.close()
                        except Exception:
                            pass
                self.cursor_state = advance_url_cursor(successful_follow_candidates, self.cursor_state)

            context.close()

        return items

    @staticmethod
    def build_context_options(
        profile_dir: str,
        runtime: PlaywrightRuntimeConfig,
        force_headed: bool = False,
    ) -> dict[str, Any]:
        extension_paths = _normalized_extension_paths(runtime.extension_paths)
        launch_args = [arg for arg in runtime.launch_args if arg and arg.strip()]
        if extension_paths:
            joined = ",".join(extension_paths)
            launch_args.extend(
                [
                    f"--disable-extensions-except={joined}",
                    f"--load-extension={joined}",
                ]
            )

        effective_headless = runtime.headless and not extension_paths and not force_headed
        if extension_paths and runtime.headless and not force_headed:
            logger.warning("Playwright extensions require headed mode; forcing headless=False")

        options: dict[str, Any] = {
            "user_data_dir": profile_dir,
            "headless": effective_headless,
        }
        channel = (runtime.channel or "").strip()
        if channel:
            options["channel"] = channel
        if launch_args:
            options["args"] = launch_args
        return options

    @staticmethod
    def _goto_with_retry(page, url: str, timeout_ms: int) -> bool:
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                PlaywrightIngestor._close_extension_tabs(page)
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                if str(getattr(page, "url", "")).lower().startswith("chrome-extension://"):
                    raise RuntimeError("Navigation landed on browser extension page instead of target URL")
                return True
            except Exception as exc:
                last_exc = exc
                if PlaywrightIngestor._is_extension_navigation_issue(page, exc):
                    logger.warning(
                        "Playwright navigation interrupted by extension; retrying %d/%d for %s",
                        attempt,
                        max_attempts,
                        url,
                    )
                    continue
                if attempt >= max_attempts:
                    if "timeout" in str(exc).lower():
                        logger.warning("Playwright navigation timed out for %s; continuing with partial page", url)
                        return False
                    raise
        if last_exc is not None:
            raise last_exc
        return False

    def _source_display_name(self) -> str:
        name = str(getattr(self.source, "name", "") or "").strip()
        if name and name.lower() != "none":
            return name
        host = (urlparse(self.source.url).hostname or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        return host or self.source.url

    @staticmethod
    def _is_extension_navigation_issue(page, exc: Exception) -> bool:
        msg = str(exc).lower()
        current_url = str(getattr(page, "url", "")).lower()
        return (
            "chrome-extension://" in msg
            or current_url.startswith("chrome-extension://")
            or "interrupted by another navigation" in msg
        )

    @staticmethod
    def _close_extension_tabs(page) -> None:
        context = getattr(page, "context", None)
        if context is None:
            return
        pages = getattr(context, "pages", [])
        for tab in list(pages):
            if tab is page:
                continue
            tab_url = str(getattr(tab, "url", "")).lower()
            if not tab_url.startswith("chrome-extension://"):
                continue
            try:
                tab.close()
            except Exception:
                continue

    def _capture_item(
        self,
        page,
        item_url: str,
        source_name: str,
        wait_selector: str | None,
        content_selector: str,
        max_chars: int,
        parent_url: str | None,
        depth: int,
        navigation_complete: bool = True,
    ) -> RawItem:
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=self.source.timeout_ms)
            except PlaywrightTimeoutError:
                logger.warning("Timeout waiting selector %s for %s", wait_selector, item_url)

        content = self._extract_text(page, content_selector, max_chars)
        title = page.title() or f"Snapshot: {source_name}"
        meta_publish_times = self._extract_meta_publish_times(page)

        return RawItem(
            source_type="playwright",
            source_name=source_name,
            title=title,
            url=item_url,
            content=content,
            fetched_at=utc_now(),
            metadata={
                "selector": content_selector,
                "wait_for_selector": wait_selector,
                "meta_publish_times": meta_publish_times,
                "parent_url": parent_url,
                "depth": depth,
                "follow_links_enabled": self.source.follow_links_enabled,
                "navigation_complete": navigation_complete,
            },
        )

    @staticmethod
    def _extract_text(page, selector: str, max_chars: int) -> str:
        try:
            text = page.locator(selector).first.inner_text(timeout=15000)
        except Exception:
            try:
                text = page.locator("body").first.inner_text(timeout=15000)
            except Exception:
                return ""
        return text.strip()[:max_chars]

    def _collect_follow_candidates(self, page, root_url: str) -> list[dict[str, str | None]]:
        selector = self.source.link_selector or "a[href]"
        script = """
(selector) => {
  const rows = [];
  const containerSelector = "article, li, section, main, div";
  document.querySelectorAll(selector).forEach((node) => {
    const href = node.getAttribute('href');
    if (!href) return;
    try {
      const abs = new URL(href, window.location.href).toString();
      let publishTime = null;
      let cursor = node.closest(containerSelector);
      while (cursor && !publishTime) {
        const timeNode = cursor.querySelector("time[datetime]");
        if (timeNode) {
          publishTime = timeNode.getAttribute("datetime");
          break;
        }
        cursor = cursor.parentElement ? cursor.parentElement.closest(containerSelector) : null;
      }
      rows.push({ url: abs, publish_time: publishTime });
    } catch (err) {
      // ignore invalid urls
    }
  });
  return rows;
}
"""
        try:
            raw_rows = page.evaluate(script, selector)
        except Exception:
            return []
        if not isinstance(raw_rows, list):
            return []

        root_host = (urlparse(root_url).hostname or "").lower()
        accepted: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
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

    @staticmethod
    def _extract_meta_publish_times(page) -> list[str]:
        script = """
() => {
  const values = [];
  const include = (value) => {
    if (!value || typeof value !== "string") return;
    const normalized = value.trim();
    if (!normalized) return;
    values.push(normalized);
  };

  const shouldKeep = (token) => {
    const v = (token || "").toLowerCase();
    return (
      v.includes("publish") ||
      v.includes("updated") ||
      v.includes("modified") ||
      v.includes("date") ||
      v.includes("time")
    );
  };

  document.querySelectorAll("meta").forEach((meta) => {
    const key = meta.getAttribute("property") || meta.getAttribute("name") || meta.getAttribute("itemprop") || "";
    if (!shouldKeep(key)) return;
    include(meta.getAttribute("content"));
  });

  document.querySelectorAll("time[datetime]").forEach((node) => include(node.getAttribute("datetime")));
  return values.slice(0, 10);
}
"""
        try:
            values = page.evaluate(script)
        except Exception:
            return []
        if not isinstance(values, list):
            return []
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            token = str(value).strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(token)
        return deduped[:10]


def _normalized_extension_paths(paths: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        token = raw.strip()
        if not token:
            continue
        path = str(Path(token).expanduser().resolve())
        if path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return normalized


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
