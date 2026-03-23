from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from openai import OpenAI

from monitor_agent.core.models import LLMConfig, RawItem, Signal, StrategyProfileConfig
from monitor_agent.core.utils import make_fingerprint, utc_now

logger = logging.getLogger(__name__)
PROMPT_ITEM_CONTENT_MAX_CHARS = 6000


class LLMSignalExtractor:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        api_key = os.getenv("OPENAI_API_KEY")
        if config.base_url and not api_key:
            # Some OpenAI-compatible endpoints accept placeholder keys.
            api_key = "dummy"

        self.client = OpenAI(api_key=api_key, base_url=config.base_url) if api_key else None

    def extract(
        self,
        domain: str,
        raw_items: list[RawItem],
        strategy_profile: StrategyProfileConfig | None = None,
    ) -> tuple[list[Signal], list[str]]:
        if not raw_items:
            return [], []

        limited_items = raw_items[: self.config.max_input_items]
        prompt = self._build_prompt(domain, limited_items, strategy_profile)
        errors: list[str] = []

        if self.client is None:
            msg = "OPENAI_API_KEY is missing; using fallback extraction"
            logger.warning(msg)
            errors.append(msg)
            return self._fallback_extract(limited_items), errors

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                temperature=self.config.temperature,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a monitoring analyst. Return JSON only with key 'signals'. "
                            "Each signal must include: title, summary, importance(0..1), category, "
                            "source_urls(array), evidence(array), tags(array), "
                            "publish_time(optional ISO8601), age_hours(optional nullable), "
                            "freshness(optional fresh/recent/stale/unknown). "
                            "Write summary in concise Simplified Chinese with concrete facts (who did what, key numbers/partners/products). "
                            "Single-source updates from trusted publishers are valid signals; "
                            "do not require multi-source corroboration."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            content = response.choices[0].message.content or "{}"
            payload = json.loads(content)
            candidates = payload.get("signals", [])
            signals = self._to_signals(candidates)
            logger.info("LLM extracted %d signals", len(signals))
            return signals, errors
        except Exception as exc:
            msg = f"LLM extraction failed; using fallback extraction: {exc}"
            logger.exception(msg)
            errors.append(msg)
            return self._fallback_extract(limited_items), errors

    def _build_prompt(
        self,
        domain: str,
        items: list[RawItem],
        strategy_profile: StrategyProfileConfig | None,
    ) -> str:
        serialized: list[dict[str, Any]] = []
        for item in items:
            serialized.append(
                {
                    "id": item.id,
                    "source_type": item.source_type,
                    "source_name": item.source_name,
                    "title": item.title,
                    "url": item.url,
                    "content": self._clip_prompt_content(item.content),
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                    "publish_time": item.publish_time.isoformat() if item.publish_time else None,
                    "age_hours": item.age_hours,
                    "freshness": item.freshness,
                    "fetched_at": item.fetched_at.isoformat(),
                }
            )

        focus_text = ", ".join((strategy_profile.focus_areas if strategy_profile else [])[:8]) or "not specified"
        entities_text = ", ".join((strategy_profile.entities if strategy_profile else [])[:12]) or "not specified"
        keywords_text = ", ".join((strategy_profile.keywords if strategy_profile else [])[:12]) or "not specified"

        return (
            f"Domain: {domain}\n"
            f"Focus Areas: {focus_text}\n"
            f"Tracked Entities: {entities_text}\n"
            f"Priority Keywords: {keywords_text}\n"
            "Task: Identify high-value monitoring signals from these items. "
            "Merge overlapping facts into single signals and avoid duplicates. "
            "Favor relevance and freshness. A single authoritative source is sufficient. "
            "Use Simplified Chinese for summary and keep it specific.\n"
            "Return JSON object with key 'signals'.\n"
            f"Items: {json.dumps(serialized, ensure_ascii=False)}"
        )

    @staticmethod
    def _clip_prompt_content(content: str) -> str:
        token = str(content or "").strip()
        if len(token) <= PROMPT_ITEM_CONTENT_MAX_CHARS:
            return token
        return token[:PROMPT_ITEM_CONTENT_MAX_CHARS]

    def _to_signals(self, rows: list[dict[str, Any]]) -> list[Signal]:
        signals: list[Signal] = []
        extracted_at = utc_now()

        for row in rows:
            try:
                title = str(row.get("title", "Untitled")).strip()
                summary = str(row.get("summary", "")).strip()
                importance = float(row.get("importance", 0.5))
                importance = min(max(importance, 0.0), 1.0)
                category = str(row.get("category", "general")).strip() or "general"
                source_urls = [str(v) for v in row.get("source_urls", []) if v]
                evidence = [str(v) for v in row.get("evidence", []) if v]
                tags = [str(v) for v in row.get("tags", []) if v]

                publish_time = self._parse_datetime(row.get("publish_time") or row.get("published_at"))
                published_at = publish_time or self._parse_datetime(row.get("published_at"))
                age_hours = self._to_optional_float(row.get("age_hours"))
                if publish_time and age_hours is None:
                    age_hours = max(0.0, (extracted_at - publish_time).total_seconds() / 3600.0)
                if age_hours is not None:
                    age_hours = round(max(age_hours, 0.0), 3)

                freshness = str(row.get("freshness", "")).strip().lower()
                if freshness not in {"fresh", "recent", "stale", "unknown"}:
                    freshness = self._classify_freshness(age_hours)
                fingerprint = make_fingerprint(title, " ".join(source_urls), summary[:160])

                signals.append(
                    Signal(
                        title=title,
                        summary=summary,
                        importance=importance,
                        category=category,
                        source_urls=source_urls,
                        evidence=evidence,
                        tags=tags,
                        published_at=published_at,
                        publish_time=publish_time,
                        age_hours=age_hours,
                        freshness=freshness,
                        extracted_at=extracted_at,
                        fingerprint=fingerprint,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping malformed signal row %s due to %s", row, exc)

        return signals

    def _fallback_extract(self, items: list[RawItem]) -> list[Signal]:
        extracted_at = utc_now()
        signals: list[Signal] = []

        for item in items:
            title = item.title.strip() or "Untitled"
            summary = item.content.strip().replace("\n", " ")[:400]
            category = item.source_type
            source_urls = [item.url] if item.url else []
            importance = self._fallback_importance(title, summary)
            fingerprint = make_fingerprint(title, item.source_name, summary[:160])

            signals.append(
                Signal(
                    title=title,
                    summary=summary,
                    importance=importance,
                    category=category,
                    source_urls=source_urls,
                    evidence=[f"source={item.source_name}"],
                    tags=[item.source_type],
                    published_at=item.published_at or item.publish_time,
                    publish_time=item.publish_time or item.published_at,
                    age_hours=item.age_hours,
                    freshness=item.freshness
                    if item.freshness in {"fresh", "recent", "stale", "unknown"}
                    else self._classify_freshness(item.age_hours),
                    extracted_at=extracted_at,
                    fingerprint=fingerprint,
                )
            )

        return signals

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except ValueError:
                return None
        return None

    @staticmethod
    def _fallback_importance(title: str, summary: str) -> float:
        text = f"{title} {summary}".lower()
        score = 0.62
        boosted_terms = ("breaking", "launch", "security", "outage", "funding", "acquire", "release")
        for term in boosted_terms:
            if term in text:
                score += 0.05
        return min(score, 0.9)

    @staticmethod
    def _to_optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _classify_freshness(age_hours: float | None) -> str:
        if age_hours is None:
            return "unknown"
        if age_hours <= 24:
            return "fresh"
        if age_hours <= 72:
            return "recent"
        return "stale"
