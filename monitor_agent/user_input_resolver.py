from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urldefrag, urlparse

from playwright.sync_api import sync_playwright

from monitor_agent.core.models import MonitorConfig
from monitor_agent.core.storage import Storage
from monitor_agent.core.url_safety import UnsafeUrlError, validate_public_http_url
from monitor_agent.ingestion_layer.html_parser import ParsedHtmlPage, fetch_parsed_html
from monitor_agent.ingestion_layer.playwright_ingestor import PlaywrightIngestor

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+")
_MIN_HTML_CHARS = 240
_MAX_CONTEXT_CHARS = 8_000
_MAX_PAGE_CHARS = 6_000


@dataclass(slots=True)
class ResolvedUserInput:
    title: str
    context: str
    source_urls: list[str]
    resolution_method: str
    resolution_errors: list[str]


class UserInputResolver:
    """Resolve user-submitted links into content-rich context before inbox ingestion."""

    def __init__(self, *, config: MonitorConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        flag = os.getenv("MONITOR_ALLOW_USER_INPUT_PLAYWRIGHT", "").strip().lower()
        self._allow_playwright_fallback = flag not in {"0", "false", "no"}

    def resolve(self, inputs: list[Any]) -> list[Any]:
        resolved: list[Any] = []
        for item in inputs:
            try:
                resolved.append(self._resolve_single(item))
            except Exception as exc:
                logger.warning("User input resolution failed; keeping original payload: %s", exc)
                resolved.append(item)
        return resolved

    def _resolve_single(self, item: Any) -> Any:
        title = self._get_text(item, "title", default="").strip()
        context = self._get_text(item, "context", default="").strip()
        original_context = self._get_text(item, "original_context", default="").strip() or context
        resolved_context = self._get_text(item, "resolved_context", default="").strip()
        resolved_title = self._get_text(item, "resolved_title", default="").strip()
        resolution_method = self._get_text(item, "resolution_method", default="").strip()
        resolution_errors = self._get_list(item, "resolution_errors")

        source_urls = self._dedupe_urls(
            self._get_list(item, "source_urls") + self._extract_urls(original_context) + self._extract_urls(context)
        )
        blocked_errors = resolution_errors[:]
        safe_source_urls: list[str] = []
        for url in source_urls:
            try:
                safe_source_urls.append(validate_public_http_url(url))
            except UnsafeUrlError as exc:
                blocked_errors.append(f"Blocked unsafe URL {url}: {exc}")
        source_urls = safe_source_urls

        if resolved_context:
            return self._update_item(
                item,
                original_context=original_context,
                context=context or original_context,
                title=resolved_title or title or self._derive_title(original_context, source_urls),
                resolved_context=self._clip_text(resolved_context, _MAX_CONTEXT_CHARS),
                resolved_title=resolved_title or title,
                source_urls=source_urls,
                resolution_method=resolution_method or "provided",
                resolution_errors=self._dedupe_preserve(blocked_errors),
            )

        if not source_urls:
            return self._update_item(
                item,
                original_context=original_context,
                context=context or original_context,
                title=resolved_title or title or self._derive_title(original_context, source_urls),
                resolved_title=resolved_title or title,
                resolved_context=self._clip_text(original_context or context, _MAX_CONTEXT_CHARS),
                source_urls=source_urls,
                resolution_method=resolution_method or "text",
                resolution_errors=self._dedupe_preserve(blocked_errors),
            )

        resolved_pages: list[ResolvedUserInput] = []
        for url in source_urls[:3]:
            page = self._resolve_url(url)
            if page is not None:
                resolved_pages.append(page)

        if not resolved_pages:
            return self._update_item(
                item,
                original_context=original_context,
                context=context or original_context,
                title=resolved_title or title or self._derive_title(original_context, source_urls),
                resolved_title=resolved_title or title or self._derive_title(original_context, source_urls),
                resolved_context=self._build_context(original_context, []),
                source_urls=source_urls,
                resolution_method="unresolved",
                resolution_errors=self._dedupe_preserve(blocked_errors or ["Failed to resolve linked content."]),
            )

        composed_context = self._build_context(original_context, resolved_pages)
        method_names = self._dedupe_preserve([page.resolution_method for page in resolved_pages])
        merged_errors = self._dedupe_preserve(
            blocked_errors
            + [error for page in resolved_pages for error in page.resolution_errors]
        )
        merged_title = resolved_pages[0].title or resolved_title or title or self._derive_title(original_context, source_urls)
        merged_urls = self._dedupe_urls([url for page in resolved_pages for url in page.source_urls])

        return self._update_item(
            item,
            original_context=original_context,
            context=context or original_context,
            title=merged_title,
            resolved_title=merged_title,
            resolved_context=composed_context,
            source_urls=merged_urls or source_urls,
            resolution_method="+".join(method_names) if method_names else "html_parser",
            resolution_errors=merged_errors,
        )

    def _resolve_url(self, url: str) -> ResolvedUserInput | None:
        errors: list[str] = []
        html_result: ResolvedUserInput | None = None
        try:
            page = fetch_parsed_html(
                url,
                timeout_seconds=self._html_timeout_seconds(),
                max_chars=_MAX_PAGE_CHARS,
                url_validator=validate_public_http_url,
            )
            resolved = self._page_to_result(page, method="html_parser")
            html_result = resolved
            if len(resolved.context.strip()) >= _MIN_HTML_CHARS:
                return resolved
            errors.append("HTML parser content too short; attempting Playwright fallback.")
        except Exception as exc:
            errors.append(f"HTML parser failed: {exc}")

        if not self._allow_playwright_fallback:
            if html_result is not None:
                return ResolvedUserInput(
                    title=html_result.title,
                    context=html_result.context,
                    source_urls=html_result.source_urls,
                    resolution_method="html_parser",
                    resolution_errors=errors + ["Playwright fallback disabled for user-submitted links."],
                )
            return ResolvedUserInput(
                title=self._derive_title("", [url]),
                context="",
                source_urls=[url],
                resolution_method="unresolved",
                resolution_errors=errors + ["Playwright fallback disabled for user-submitted links."],
            )

        try:
            playwright_result = self._fetch_with_playwright(url, errors=errors)
            if playwright_result.context.strip():
                return playwright_result
            if html_result is not None and html_result.context.strip():
                return ResolvedUserInput(
                    title=html_result.title,
                    context=html_result.context,
                    source_urls=html_result.source_urls,
                    resolution_method="html_parser",
                    resolution_errors=errors + ["Playwright returned empty content; kept HTML parser result."],
                )
            return playwright_result
        except Exception as exc:
            errors.append(f"Playwright failed: {exc}")
            logger.warning("User input resolution failed for %s: %s", url, exc)
            if html_result is not None:
                return ResolvedUserInput(
                    title=html_result.title,
                    context=html_result.context,
                    source_urls=html_result.source_urls,
                    resolution_method="html_parser",
                    resolution_errors=errors,
                )
            return ResolvedUserInput(
                title=self._derive_title("", [url]),
                context="",
                source_urls=[url],
                resolution_method="unresolved",
                resolution_errors=errors,
            )

    def _fetch_with_playwright(self, url: str, *, errors: list[str]) -> ResolvedUserInput:
        validated_url = validate_public_http_url(url)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                **PlaywrightIngestor.build_context_options(
                    profile_dir=str(self.storage.playwright_profile_dir),
                    runtime=self.config.playwright,
                )
            )
            page = context.new_page()
            try:
                self._goto_with_retry(page, validated_url, timeout_ms=max(5_000, int(self._playwright_timeout_ms())))
                title = self._clean_text(page.title() or "")
                content = self._extract_body_text(page)
                final_url = validate_public_http_url(str(getattr(page, "url", "") or validated_url))
                content = self._clip_text(content, _MAX_PAGE_CHARS)
                if not content:
                    errors.append("Playwright body text empty.")
                result = ResolvedUserInput(
                    title=title or self._derive_title("", [final_url]),
                    context=content,
                    source_urls=[final_url],
                    resolution_method="playwright",
                    resolution_errors=errors[:],
                )
                return result
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass

    @staticmethod
    def _page_to_result(page: ParsedHtmlPage, *, method: str) -> ResolvedUserInput:
        title = page.title.strip() or page.final_url
        content = page.text.strip()
        source_urls = [page.final_url or page.url]
        return ResolvedUserInput(
            title=title,
            context=content,
            source_urls=source_urls,
            resolution_method=method,
            resolution_errors=[],
        )

    def _build_context(self, original_context: str, pages: list[ResolvedUserInput]) -> str:
        parts: list[str] = []
        if original_context.strip():
            parts.append("Original submission:")
            parts.append(self._clip_text(original_context.strip(), 2_000))
        for idx, page in enumerate(pages, start=1):
            if not page.context.strip():
                continue
            url = page.source_urls[0] if page.source_urls else ""
            parts.append("")
            parts.append(f"Resolved content {idx}:")
            if page.title.strip():
                parts.append(f"Title: {page.title.strip()}")
            if url:
                parts.append(f"URL: {url}")
            parts.append(self._clip_text(page.context.strip(), 3_500))
        return self._clip_text("\n".join(parts).strip(), _MAX_CONTEXT_CHARS)

    def _update_item(self, item: Any, **updates: Any) -> Any:
        if hasattr(item, "model_copy"):
            return item.model_copy(update=updates)
        if isinstance(item, dict):
            updated = dict(item)
            updated.update(updates)
            return updated
        for key, value in updates.items():
            setattr(item, key, value)
        return item

    def _get_text(self, item: Any, key: str, default: str = "") -> str:
        value = self._get_value(item, key, default)
        return str(value or default)

    def _get_list(self, item: Any, key: str) -> list[str]:
        value = self._get_value(item, key, [])
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for entry in value:
            token = str(entry or "").strip()
            if token:
                out.append(token)
        return self._dedupe_urls(out)

    @staticmethod
    def _get_value(item: Any, key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @staticmethod
    def _extract_urls(text: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for raw in _URL_RE.findall(text or ""):
            url = raw.strip().rstrip(".,;，。；)")
            if not url:
                continue
            key = urldefrag(url)[0]
            if key in seen:
                continue
            seen.add(key)
            urls.append(key)
        return urls

    @staticmethod
    def _dedupe_urls(urls: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in urls:
            url = str(raw or "").strip()
            if not url:
                continue
            token = urldefrag(url)[0]
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out

    @staticmethod
    def _dedupe_preserve(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            token = str(item or "").strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(token)
        return out

    @staticmethod
    def _derive_title(text: str, urls: list[str]) -> str:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if lines:
            first = lines[0]
            if not _URL_RE.fullmatch(first):
                return first[:80].rstrip() if len(first) > 80 else first
        if urls:
            host = (urlparse(urls[0]).hostname or "").replace("www.", "")
            if host:
                return f"Shared link - {host}"
        return "User submitted signal"

    def _html_timeout_seconds(self) -> float:
        return 12.0

    def _playwright_timeout_ms(self) -> int:
        return 18_000

    @staticmethod
    def _extract_body_text(page) -> str:
        try:
            text = page.locator("article").first.inner_text(timeout=12_000)
        except Exception:
            try:
                text = page.locator("body").first.inner_text(timeout=12_000)
            except Exception:
                return ""
        return UserInputResolver._clean_text(text)

    @staticmethod
    def _goto_with_retry(page, url: str, timeout_ms: int) -> None:
        max_attempts = 2
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                return
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    raise
                logger.warning("User input Playwright navigation retry %d/%d for %s", attempt, max_attempts, url)
        if last_exc is not None:
            raise last_exc

    @staticmethod
    def _clean_text(text: str) -> str:
        token = str(text or "")
        token = token.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        token = re.sub(r"\s+", " ", token)
        return token.strip()

    @staticmethod
    def _clip_text(text: str, limit: int) -> str:
        token = UserInputResolver._clean_text(text)
        if len(token) <= limit:
            return token
        return token[: limit - 1].rstrip() + "…"
