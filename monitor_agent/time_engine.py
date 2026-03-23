from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from monitor_agent.core.models import RawItem
from monitor_agent.core.utils import utc_now

logger = logging.getLogger(__name__)


class TimeEngine:
    """Resolve publish time and freshness for ingested raw items."""

    _CONTENT_PATTERNS = (
        re.compile(r"\b\d{4}-\d{2}-\d{2}[tT\s]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
        re.compile(r"\b\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}(?::\d{2})?\b"),
        re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\b"),
        re.compile(r"\b[A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}(?::\d{2})?\s+[A-Z+-0-9:]+\b"),
    )

    def annotate_items(self, items: list[RawItem]) -> tuple[list[RawItem], list[RawItem]]:
        fresh_or_recent: list[RawItem] = []
        stale_items: list[RawItem] = []

        for item in items:
            annotated = self.annotate_item(item)
            if annotated.freshness == "stale":
                stale_items.append(annotated)
            else:
                fresh_or_recent.append(annotated)

        if stale_items:
            logger.info(
                "Time engine classified %d/%d items as stale (>72h)",
                len(stale_items),
                len(items),
            )
        return fresh_or_recent, stale_items

    def annotate_item(self, item: RawItem) -> RawItem:
        crawl_time = item.fetched_at or utc_now()
        publish_time, source = self._resolve_publish_time(item, crawl_time)
        age_hours = (
            max(0.0, (crawl_time - publish_time).total_seconds() / 3600.0)
            if publish_time is not None
            else None
        )
        freshness = self._classify_freshness(age_hours)

        item.publish_time = publish_time
        item.age_hours = round(age_hours, 3) if age_hours is not None else None
        item.freshness = freshness
        item.metadata["publish_time_source"] = source
        item.metadata["publish_time"] = publish_time.astimezone(UTC).isoformat() if publish_time else None

        return item

    @staticmethod
    def to_payload(item: RawItem) -> dict[str, Any]:
        return {
            "publish_time": item.publish_time.astimezone(UTC).isoformat() if item.publish_time else None,
            "age_hours": item.age_hours,
            "freshness": item.freshness,
        }

    def _resolve_publish_time(self, item: RawItem, crawl_time: datetime) -> tuple[datetime | None, str]:
        # 1) HTML meta tags
        for value in self._meta_time_candidates(item.metadata):
            parsed = self._parse_datetime(value, crawl_time)
            if parsed is not None:
                return parsed, "html_meta"

        # 2) RSS timestamp
        if item.published_at is not None:
            parsed_rss = self._parse_datetime(item.published_at, crawl_time)
            if parsed_rss is not None:
                return parsed_rss, "rss_timestamp"

        # 3) Content parsing
        parsed_content = self._parse_from_content(item.content, crawl_time)
        if parsed_content is not None:
            return parsed_content, "content_parsed"

        # 4) Unresolved
        return None, "unresolved"

    def _meta_time_candidates(self, metadata: dict[str, Any]) -> list[str]:
        candidates: list[str] = []

        raw_meta = metadata.get("meta_publish_times")
        if isinstance(raw_meta, list):
            candidates.extend(str(v).strip() for v in raw_meta if str(v).strip())
        elif raw_meta:
            candidates.append(str(raw_meta).strip())

        raw_single = metadata.get("meta_publish_time")
        if raw_single:
            candidates.append(str(raw_single).strip())

        return candidates

    def _parse_from_content(self, text: str, crawl_time: datetime) -> datetime | None:
        if not text:
            return None

        clipped = text[:8000]
        for pattern in self._CONTENT_PATTERNS:
            for match in pattern.findall(clipped):
                parsed = self._parse_datetime(match, crawl_time)
                if parsed is not None:
                    return parsed
        return None

    def _parse_datetime(self, value: Any, crawl_time: datetime) -> datetime | None:
        parsed: datetime | None = None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            try:
                parsed = datetime.fromtimestamp(float(value), tz=UTC)
            except Exception:
                parsed = None
        elif isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            candidate = candidate.replace(" UTC", " +00:00")
            candidate = candidate.replace(" GMT", " +00:00")

            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            except ValueError:
                parsed = None

            if parsed is None:
                try:
                    parsed = parsedate_to_datetime(candidate)
                except Exception:
                    parsed = None

            if parsed is None:
                for fmt in (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d %H:%M",
                    "%d %b %Y %H:%M:%S",
                    "%d %b %Y %H:%M",
                    "%d %b %Y",
                    "%Y-%m-%d",
                ):
                    try:
                        parsed = datetime.strptime(candidate, fmt)
                        break
                    except ValueError:
                        continue

        if parsed is None:
            return None

        normalized = self._normalize_datetime(parsed)

        # Skip impossible future publish times caused by parsing noise.
        if normalized > crawl_time.astimezone(UTC):
            return None
        return normalized

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _classify_freshness(age_hours: float | None) -> str:
        if age_hours is None:
            return "unknown"
        if age_hours <= 24:
            return "fresh"
        if age_hours <= 72:
            return "recent"
        return "stale"
