from __future__ import annotations

from urllib.parse import quote_plus

import yaml

from monitor_agent.core.models import MonitorConfig
from monitor_agent.strategy_engine.models import DomainMapping, ParsedIntent, StrategyGenerateRequest


class ConfigGenerator:
    """Deterministically generate monitor config YAML from parsed strategy intent."""

    def __init__(self, base_config: MonitorConfig | None = None) -> None:
        self.base_config = base_config

    def generate(
        self,
        request: StrategyGenerateRequest,
        intent: ParsedIntent,
        mapping: DomainMapping,
    ) -> tuple[dict, str]:
        schedule_timezone = request.timezone or self._base("schedule.timezone", "UTC")
        schedule_times_raw = request.schedule_times or self._base("schedule.times", ["07:00"])
        schedule_times = _normalize_times(schedule_times_raw)

        filtering = {
            "importance_threshold": request.importance_threshold
            if request.importance_threshold is not None
            else self._base("filtering.importance_threshold", 0.6),
            "dedup_window_days": self._base("filtering.dedup_window_days", 14),
            "novelty_window_days": self._base("filtering.novelty_window_days", 30),
            "max_signals": request.max_signals
            if request.max_signals is not None
            else self._base("filtering.max_signals", 10),
            "max_system_signals": self._base("filtering.max_system_signals", 5),
            "event_similarity_threshold": self._base("filtering.event_similarity_threshold", 0.75),
            "event_candidate_top_k": self._base("filtering.event_candidate_top_k", 5),
            "event_candidate_lookback_days": self._base("filtering.event_candidate_lookback_days", 3),
            "inbox_match_threshold": self._base("filtering.inbox_match_threshold", 0.2),
            "unknown_freshness_importance_threshold": self._base(
                "filtering.unknown_freshness_importance_threshold",
                0.9,
            ),
        }

        rss_sources = self._build_rss_sources(intent, mapping)
        playwright_sources = self._build_playwright_sources(intent)

        config_object = {
            "domain": mapping.canonical_domain,
            "domains": [mapping.canonical_domain],
            "domain_profiles": [
                {
                    "domain": mapping.canonical_domain,
                    "focus_areas": _dedupe(intent.focus_areas),
                    "entities": _dedupe(intent.entities),
                    "keywords": _dedupe(mapping.recommended_tags),
                    "source_links": _dedupe(
                        [row.get("url", "") for row in rss_sources + playwright_sources if row.get("url")]
                    ),
                }
            ],
            "sources": {
                "rss": rss_sources,
                "playwright": playwright_sources,
            },
            "strategy_profile": {
                "focus_areas": _dedupe(intent.focus_areas),
                "entities": _dedupe(intent.entities),
                "keywords": _dedupe(mapping.recommended_tags),
            },
            "schedule": {
                "timezone": schedule_timezone,
                "times": schedule_times,
                "enabled": self._base("schedule.enabled", True),
            },
            "filtering": filtering,
            "llm": self._base_section(
                "llm",
                default={
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "dedup_model": None,
                    "embedding_model": "text-embedding-3-small",
                    "embedding_base_url": None,
                    "base_url": None,
                    "temperature": 0.1,
                    "dedup_temperature": 0.0,
                    "max_input_items": 40,
                },
            ),
            "tts": self._base_section(
                "tts",
                default={"enabled": False, "provider": "openai", "model": "gpt-4o-mini-tts", "voice": "alloy"},
            ),
            "notifications": self._base_section(
                "notifications",
                default={
                    "channel": "none",
                    "channels": [],
                    "telegram": {"enabled": False},
                    "dingtalk": {"enabled": False},
                },
            ),
            "storage": self._base_section("storage", default={"root_dir": "./data", "base_path": "./data"}),
            "api": self._base_section(
                "api",
                default={
                    "host": "127.0.0.1",
                    "port": 8080,
                    "scheduler_enabled": False,
                    "auto_run_on_user_ingest": True,
                },
            ),
        }

        validated = MonitorConfig.model_validate(config_object)
        normalized = validated.model_dump(mode="json")
        yaml_text = yaml.safe_dump(normalized, sort_keys=False, allow_unicode=False)
        return normalized, yaml_text

    def _build_rss_sources(self, intent: ParsedIntent, mapping: DomainMapping) -> list[dict]:
        feeds = [f.model_dump(mode="json") for f in mapping.baseline_rss_feeds]

        queries: list[str] = []
        queries.append(mapping.canonical_domain)
        queries.extend(mapping.source_queries)
        queries.extend(intent.focus_areas[:4])
        queries.extend(intent.entities[:3])

        for query in _dedupe([q for q in queries if q]):
            url = f"https://news.google.com/rss/search?q={quote_plus(query)}"
            feeds.append({"name": f"Google News - {query[:40]}", "url": url, "max_items": 20})

        unique: list[dict] = []
        seen_urls: set[str] = set()
        for feed in feeds:
            url = str(feed.get("url", "")).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            unique.append(
                {
                    "name": str(feed.get("name") or "RSS Source"),
                    "url": url,
                    "max_items": int(feed.get("max_items", 20)),
                }
            )

        return unique[:12]

    def _build_playwright_sources(self, intent: ParsedIntent) -> list[dict]:
        sources: list[dict] = []
        for idx, url in enumerate(intent.source_urls[:6], start=1):
            sources.append(
                {
                    "name": f"Web Source {idx}",
                    "url": url,
                    "wait_for_selector": "body",
                    "content_selector": "body",
                    "max_chars": 6000,
                }
            )
        return sources

    def _base(self, path: str, default):
        if self.base_config is None:
            return default
        node = self.base_config.model_dump(mode="json")
        for part in path.split("."):
            if part not in node:
                return default
            node = node[part]
        return node

    def _base_section(self, path: str, default: dict) -> dict:
        value = self._base(path, default)
        if not isinstance(value, dict):
            return default
        return value


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def _normalize_times(times: list[str]) -> list[str]:
    if not times:
        raise ValueError("schedule_times must include at least one HH:MM value")

    normalized: list[str] = []
    for value in times:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid schedule time format: {value}")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid schedule time value: {value}")
        normalized.append(f"{hour:02d}:{minute:02d}")

    return _dedupe(normalized)
