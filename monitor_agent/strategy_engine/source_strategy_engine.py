from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from statistics import median
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

import feedparser
from openai import OpenAI

from monitor_agent.core.models import LLMConfig
from monitor_agent.core.storage import Storage
from monitor_agent.strategy_engine.models import (
    SourceStrategySuggestion,
    SourceStrategySuggestResult,
)

_RSS_HINTS = ("/rss", ".rss", ".xml", "/feed", "atom")
_PAYWALL_HINTS = ("paywall", "subscribe", "subscription", "sign in", "login", "metered")
_DYNAMIC_HINTS = ("__next_data__", "__nuxt", "hydration", "application/json")
_ARTICLE_HINTS = ("/article/", "/news/", "/202", "/20", "/story/")


class SourceStrategyEngine:
    def __init__(
        self,
        storage: Storage,
        llm_config: LLMConfig | None = None,
        use_llm: bool = True,
    ) -> None:
        self.storage = storage
        self.llm_config = llm_config
        self.use_llm = use_llm
        self.client: OpenAI | None = None
        if use_llm and llm_config is not None:
            api_key = os.getenv("OPENAI_API_KEY")
            if llm_config.base_url and not api_key:
                api_key = "dummy"
            if api_key:
                self.client = OpenAI(api_key=api_key, base_url=llm_config.base_url)

    def suggest(
        self,
        urls: list[str],
        *,
        refresh_interval_days: int = 14,
        force_refresh: bool = False,
    ) -> SourceStrategySuggestResult:
        normalized_urls = _dedupe_urls(urls)
        cache = self.storage.load_source_strategy_cache()
        now = datetime.now(UTC)
        reused = 0
        recomputed = 0
        suggestions: list[SourceStrategySuggestion] = []

        for raw in normalized_urls:
            url = _normalize_url(raw)
            if not url:
                continue
            key = url.lower()
            cached = cache.get(key)
            if not force_refresh and isinstance(cached, dict):
                suggestion = _load_cached_suggestion(cached)
                if suggestion is not None:
                    age = now - suggestion.analyzed_at.astimezone(UTC)
                    if age < timedelta(days=refresh_interval_days):
                        suggestion.cache_hit = True
                        suggestion.next_refresh_at = suggestion.analyzed_at.astimezone(UTC) + timedelta(
                            days=refresh_interval_days
                        )
                        suggestions.append(suggestion)
                        reused += 1
                        continue

            suggestion = self._analyze_url(url, refresh_interval_days=refresh_interval_days, analyzed_at=now)
            suggestion.cache_hit = False
            cache[key] = suggestion.model_dump(mode="json")
            suggestions.append(suggestion)
            recomputed += 1

        self.storage.save_source_strategy_cache(cache)
        return SourceStrategySuggestResult(
            refresh_interval_days=refresh_interval_days,
            total_urls=len(normalized_urls),
            reused_cached=reused,
            recomputed=recomputed,
            suggestions=suggestions,
        )

    def _analyze_url(
        self,
        url: str,
        *,
        refresh_interval_days: int,
        analyzed_at: datetime,
    ) -> SourceStrategySuggestion:
        now = analyzed_at.astimezone(UTC)
        host = _host(url)
        html, content_type = self._fetch_text(url)
        lowered = html.lower()
        parsed_hint = self._parse_rss_hint(url)
        if parsed_hint is not None:
            return SourceStrategySuggestion(
                url=url,
                parser_recommendation=parsed_hint["parser_recommendation"],
                configured_type=parsed_hint["configured_type"],
                normalized_source_link=parsed_hint["normalized_source_link"],
                reason=parsed_hint["reason"],
                confidence=float(parsed_hint.get("confidence", 0.8)),
                analysis=dict(parsed_hint.get("analysis", {})),
                probe_status=str(parsed_hint.get("probe_status", "ok")),  # type: ignore[arg-type]
                issues=[str(v) for v in parsed_hint.get("issues", [])],
                fixes=[str(v) for v in parsed_hint.get("fixes", [])],
                analyzed_at=now,
                next_refresh_at=now + timedelta(days=refresh_interval_days),
            )

        discovered_rss = _discover_rss_link(html, url)
        if discovered_rss:
            max_items, freq_hint = _estimate_feed_max_items(discovered_rss)
            reason = "Detected RSS/Atom discovery link; use feed ingestion for low-cost and stable updates."
            return SourceStrategySuggestion(
                url=url,
                parser_recommendation="rss",
                configured_type="rss",
                normalized_source_link={
                    "url": discovered_rss,
                    "type": "rss",
                    "name": f"{host or 'source'} RSS",
                },
                reason=reason,
                confidence=0.9,
                analysis={
                    "content_type": content_type,
                    "discovered_rss": discovered_rss,
                    "update_frequency_hint": freq_hint,
                    "suggested_max_items": max_items,
                    "feed_probe": _probe_feed(discovered_rss),
                },
                probe_status="ok",
                issues=[],
                fixes=[],
                analyzed_at=now,
                next_refresh_at=now + timedelta(days=refresh_interval_days),
            )

        paywall = any(token in lowered for token in _PAYWALL_HINTS)
        dynamic = any(token in lowered for token in _DYNAMIC_HINTS)
        login = "login" in lowered or "sign in" in lowered
        article_paths = _extract_article_patterns(html)
        link_count = len(_extract_links(html, url))
        list_like = link_count >= 8
        analysis = {
            "content_type": content_type,
            "host": host,
            "paywall_marker": paywall,
            "dynamic_marker": dynamic,
            "login_marker": login,
            "link_count": link_count,
            "list_like_page": list_like,
            "article_patterns": article_paths[:5],
            "playwright_needed": bool(paywall or dynamic or login),
        }

        llm_suggestion = self._llm_suggest(
            url=url,
            host=host,
            analysis=analysis,
            html_sample=(html[:3500] if html else ""),
        )
        if llm_suggestion is not None:
            llm_suggestion.analyzed_at = now
            llm_suggestion.next_refresh_at = now + timedelta(days=refresh_interval_days)
            llm_suggestion.analysis = {**analysis, **llm_suggestion.analysis}
            if llm_suggestion.issues is None:
                llm_suggestion.issues = []
            if llm_suggestion.fixes is None:
                llm_suggestion.fixes = []
            return llm_suggestion

        return self._heuristic_web_suggestion(
            url=url,
            host=host,
            analysis=analysis,
            refresh_interval_days=refresh_interval_days,
            analyzed_at=now,
        )

    @staticmethod
    def _parse_rss_hint(url: str) -> dict[str, Any] | None:
        lowered = url.lower()
        if not any(token in lowered for token in _RSS_HINTS):
            return None
        probe = _probe_feed(url)
        max_items, freq_hint = _estimate_feed_max_items(url)
        analysis = {
            "feed_probe": probe,
            "update_frequency_hint": freq_hint,
            "suggested_max_items": max_items,
        }
        if probe["ok"]:
            return {
                "parser_recommendation": "rss",
                "configured_type": "rss",
                "normalized_source_link": {"url": url, "type": "rss", "name": f"{_host(url) or 'source'} RSS"},
                "reason": "URL pattern indicates RSS/Atom feed and live probe returned entries.",
                "confidence": 0.9,
                "analysis": analysis,
                "probe_status": "ok",
                "issues": [],
                "fixes": [],
            }

        fallback_page = _rss_fallback_page(url)
        issues = [f"RSS probe unhealthy: {probe.get('reason', 'unknown')}"]
        if fallback_page and fallback_page != url:
            return {
                "parser_recommendation": "html_parser",
                "configured_type": "playwright",
                "normalized_source_link": {
                    "url": fallback_page,
                    "type": "playwright",
                    "name": f"{_host(fallback_page) or 'source'} web",
                    "force_playwright": False,
                },
                "reason": "RSS-like URL did not return healthy feed; switching to webpage ingestion is safer.",
                "confidence": 0.86,
                "analysis": {**analysis, "fallback_page_url": fallback_page},
                "probe_status": "warning",
                "issues": issues,
                "fixes": [f"Try webpage ingestion instead: {fallback_page}"],
            }

        return {
            "parser_recommendation": "rss",
            "configured_type": "rss",
            "normalized_source_link": {"url": url, "type": "rss", "name": f"{_host(url) or 'source'} RSS"},
            "reason": "URL pattern indicates RSS/Atom feed, but probe is unhealthy; keep RSS and verify source manually.",
            "confidence": 0.65,
            "analysis": analysis,
            "probe_status": "error",
            "issues": issues,
            "fixes": ["Check whether this feed URL has moved or requires a different path."],
        }

    @staticmethod
    def _fetch_text(url: str, timeout: float = 8.0) -> tuple[str, str]:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MonitorSourceStrategyBot/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                content_type = str(resp.headers.get("Content-Type", ""))
                raw = resp.read(250_000)
                text = raw.decode("utf-8", errors="ignore")
                return text, content_type
        except Exception:
            return "", ""

    def _llm_suggest(
        self,
        *,
        url: str,
        host: str,
        analysis: dict[str, Any],
        html_sample: str,
    ) -> SourceStrategySuggestion | None:
        if self.client is None or self.llm_config is None:
            return None

        payload = {
            "url": url,
            "host": host,
            "analysis": analysis,
            "html_sample": html_sample,
            "instruction": (
                "Decide source ingestion strategy for a monitoring pipeline.\n"
                "Output JSON only with keys:\n"
                "parser_recommendation (rss|html_parser|playwright),\n"
                "configured_type (rss|playwright),\n"
                "normalized_source_link(object),\n"
                "reason(string), confidence(0..1), analysis(object optional), "
                "probe_status(ok|warning|error optional), issues(array optional), fixes(array optional).\n"
                "For configured_type=playwright include pragmatic fields like "
                "follow_links_enabled, max_links_per_source, same_domain_only, force_playwright, "
                "link_selector, article_url_patterns, exclude_url_patterns."
            ),
        }
        messages = [
            {"role": "system", "content": "You are a web ingestion strategy planner. Return strict JSON only."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for _attempt in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=self.llm_config.model,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=messages,
                )
                content = response.choices[0].message.content or "{}"
                parsed = json.loads(content)
            except Exception:
                return None

            suggestion, err = self._parse_llm_payload(
                parsed=parsed,
                url=url,
                host=host,
            )
            if suggestion is not None:
                return suggestion

            messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Output format invalid. "
                        f"Error: {err}. "
                        "Re-output strict JSON with required keys only: "
                        "parser_recommendation, configured_type, normalized_source_link, reason, confidence, analysis."
                    ),
                }
            )
        return None

    def _parse_llm_payload(
        self,
        *,
        parsed: dict[str, Any],
        url: str,
        host: str,
    ) -> tuple[SourceStrategySuggestion | None, str]:
        try:
            parser_reco = str(parsed.get("parser_recommendation", "")).strip().lower()
            configured = str(parsed.get("configured_type", "")).strip().lower()
            if parser_reco not in {"rss", "html_parser", "playwright"}:
                return None, "parser_recommendation must be rss|html_parser|playwright"
            if configured not in {"rss", "playwright"}:
                return None, "configured_type must be rss|playwright"

            raw_link = parsed.get("normalized_source_link", {})
            if not isinstance(raw_link, dict):
                return None, "normalized_source_link must be object"
            link_obj = self._normalize_source_link(
                url=url,
                host=host,
                parser_recommendation=parser_reco,
                configured_type=configured,
                link_obj=raw_link,
            )

            reason = str(parsed.get("reason", "")).strip()
            if not reason:
                return None, "reason is required"
            confidence = _bounded_float(parsed.get("confidence"), default=0.75)
            analysis_extra = parsed.get("analysis", {})
            if not isinstance(analysis_extra, dict):
                analysis_extra = {}
            probe_status = str(parsed.get("probe_status", "ok")).strip().lower()
            if probe_status not in {"ok", "warning", "error"}:
                probe_status = "ok"
            issues = _clean_list(parsed.get("issues"))
            fixes = _clean_list(parsed.get("fixes"))
            return (
                SourceStrategySuggestion(
                    url=url,
                    parser_recommendation=parser_reco,  # type: ignore[arg-type]
                    configured_type=configured,  # type: ignore[arg-type]
                    normalized_source_link=link_obj,
                    reason=reason,
                    confidence=confidence,
                    analysis=analysis_extra,
                    probe_status=probe_status,  # type: ignore[arg-type]
                    issues=issues,
                    fixes=fixes,
                    analyzed_at=datetime.now(UTC),
                    next_refresh_at=datetime.now(UTC),
                ),
                "",
            )
        except Exception as exc:
            return None, f"payload parse failure: {exc}"

    @staticmethod
    def _normalize_source_link(
        *,
        url: str,
        host: str,
        parser_recommendation: str,
        configured_type: str,
        link_obj: dict[str, Any],
    ) -> dict[str, Any]:
        out = {
            "url": str(link_obj.get("url", "")).strip() or url,
            "type": configured_type,
            "name": str(link_obj.get("name", "")).strip() or (host or "source"),
        }
        if configured_type == "playwright":
            if "force_playwright" in link_obj:
                out["force_playwright"] = bool(link_obj.get("force_playwright"))
            else:
                out["force_playwright"] = parser_recommendation == "playwright"
            out["follow_links_enabled"] = bool(link_obj.get("follow_links_enabled", False))
            out["max_links_per_source"] = _int_clamp(link_obj.get("max_links_per_source"), 3, 1, 20)
            out["same_domain_only"] = bool(link_obj.get("same_domain_only", True))
            out["link_selector"] = str(link_obj.get("link_selector", "a[href]")).strip() or "a[href]"
            out["article_url_patterns"] = _clean_list(link_obj.get("article_url_patterns"))
            out["exclude_url_patterns"] = _clean_list(link_obj.get("exclude_url_patterns"))
        return out

    @staticmethod
    def _heuristic_web_suggestion(
        *,
        url: str,
        host: str,
        analysis: dict[str, Any],
        refresh_interval_days: int,
        analyzed_at: datetime,
    ) -> SourceStrategySuggestion:
        list_like = bool(analysis.get("list_like_page"))
        article_paths = analysis.get("article_patterns", []) if isinstance(analysis.get("article_patterns"), list) else []
        parser_recommendation = "playwright" if analysis.get("playwright_needed") else "html_parser"
        reason = (
            "Heuristic fallback: dynamic/login/paywall markers suggest Playwright."
            if parser_recommendation == "playwright"
            else "Heuristic fallback: static page suggests HTML parser first with Playwright fallback."
        )
        return SourceStrategySuggestion(
            url=url,
            parser_recommendation=parser_recommendation,  # type: ignore[arg-type]
            configured_type="playwright",
            normalized_source_link={
                "url": url,
                "type": "playwright",
                "name": host or "web_source",
                "force_playwright": parser_recommendation == "playwright",
                "follow_links_enabled": list_like,
                "max_links_per_source": 5 if list_like else 3,
                "same_domain_only": True,
                "link_selector": "a[href]",
                "article_url_patterns": article_paths[:3] if article_paths else (["/article/", "/news/"] if list_like else []),
                "exclude_url_patterns": ["/video/", "/podcast/", "/live/"] if list_like else [],
            },
            reason=reason,
            confidence=0.72,
            analysis={},
            probe_status="ok",
            issues=[],
            fixes=[],
            analyzed_at=analyzed_at,
            next_refresh_at=analyzed_at + timedelta(days=refresh_interval_days),
        )


def _load_cached_suggestion(payload: dict[str, Any]) -> SourceStrategySuggestion | None:
    try:
        return SourceStrategySuggestion.model_validate(payload)
    except Exception:
        return None


def _discover_rss_link(html: str, base_url: str) -> str | None:
    if not html:
        return None
    pattern = re.compile(
        r"<link[^>]+type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*href=[\"']([^\"']+)[\"'][^>]*>",
        flags=re.IGNORECASE,
    )
    match = pattern.search(html)
    if not match:
        # fallback: href appears before type
        pattern = re.compile(
            r"<link[^>]+href=[\"']([^\"']+)[\"'][^>]*type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>",
            flags=re.IGNORECASE,
        )
        match = pattern.search(html)
    if not match:
        return None
    href = match.group(1).strip()
    if not href:
        return None
    return urljoin(base_url, href)


def _extract_links(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    for href in re.findall(r"""href=['"]([^'"#]+)['"]""", html, flags=re.IGNORECASE):
        abs_url = urljoin(base_url, href.strip())
        if abs_url.startswith("http://") or abs_url.startswith("https://"):
            links.append(abs_url)
    return _dedupe_urls(links)


def _extract_article_patterns(html: str) -> list[str]:
    links = re.findall(r"""href=['"]([^'"#]+)['"]""", html, flags=re.IGNORECASE)
    candidates: list[str] = []
    for href in links:
        lowered = href.lower()
        if any(token in lowered for token in _ARTICLE_HINTS):
            if "/article/" in lowered:
                candidates.append("/article/")
            elif "/news/" in lowered:
                candidates.append("/news/")
            else:
                candidates.append("/20")
    out: list[str] = []
    seen: set[str] = set()
    for token in candidates:
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out[:5]


def _estimate_feed_max_items(feed_url: str) -> tuple[int, str]:
    try:
        feed = feedparser.parse(feed_url)
    except Exception:
        return 20, "unknown"
    timestamps: list[datetime] = []
    for entry in feed.entries[:12]:
        dt = _entry_time(entry)
        if dt is not None:
            timestamps.append(dt)
    if len(timestamps) < 2:
        return 20, "unknown"
    timestamps.sort(reverse=True)
    deltas = [
        max(1.0, (timestamps[idx] - timestamps[idx + 1]).total_seconds() / 3600.0)
        for idx in range(len(timestamps) - 1)
    ]
    med = float(median(deltas))
    if med <= 6:
        return 40, "high"
    if med <= 24:
        return 25, "daily"
    if med <= 72:
        return 15, "every_few_days"
    return 8, "weekly_or_slower"


def _probe_feed(feed_url: str) -> dict[str, Any]:
    try:
        feed = feedparser.parse(feed_url)
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "entries": 0,
            "bozo": True,
            "reason": f"feed parse exception: {exc}",
        }

    entries = len(getattr(feed, "entries", []) or [])
    status = getattr(feed, "status", None)
    bozo = bool(getattr(feed, "bozo", False))
    bozo_exc = getattr(feed, "bozo_exception", None)
    bozo_msg = str(bozo_exc) if bozo_exc else ""

    if entries > 0 and not bozo:
        reason = "feed healthy"
        ok = True
    elif entries > 0 and bozo:
        reason = "feed has entries but parser reported bozo"
        ok = True
    elif entries == 0 and status in {301, 302, 307, 308}:
        reason = "feed redirected but no entries in response"
        ok = False
    elif entries == 0:
        reason = "feed returned zero entries"
        ok = False
    else:
        reason = "feed unhealthy"
        ok = False

    if bozo and bozo_msg:
        reason = f"{reason}; bozo={bozo_msg}"

    return {
        "ok": ok,
        "status": status,
        "entries": entries,
        "bozo": bozo,
        "reason": reason,
    }


def _rss_fallback_page(feed_url: str) -> str:
    try:
        parsed = urlparse(feed_url)
    except Exception:
        return feed_url

    path = parsed.path or "/"
    lowered = path.lower()

    if lowered.endswith("/rss/"):
        path = path[:-4]
    elif lowered.endswith("/rss"):
        path = path[:-4] or "/"
    elif lowered.endswith("rss.xml"):
        path = path[:-7] or "/"
    elif lowered.endswith("/feed/"):
        path = path[:-5]
    elif lowered.endswith("/feed"):
        path = path[:-5] or "/"
    elif lowered.endswith(".xml"):
        segs = [seg for seg in path.split("/") if seg]
        if segs:
            segs.pop()
        path = "/" + "/".join(segs)
        if not path.endswith("/"):
            path += "/"

    if not path:
        path = "/"
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"

    rebuilt = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    return rebuilt or feed_url


def _entry_time(entry: Any) -> datetime | None:
    dt_struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if dt_struct is not None:
        return datetime(
            dt_struct.tm_year,
            dt_struct.tm_mon,
            dt_struct.tm_mday,
            dt_struct.tm_hour,
            dt_struct.tm_min,
            dt_struct.tm_sec,
            tzinfo=UTC,
        )
    for key in ("published", "updated"):
        value = getattr(entry, key, None)
        if not value:
            continue
        try:
            parsed = parsedate_to_datetime(str(value))
        except Exception:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _normalize_url(value: str) -> str:
    token = value.strip()
    if not token:
        return ""
    if "://" not in token:
        token = f"https://{token}"
    return token


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in urls:
        token = _normalize_url(value)
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _int_clamp(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))
