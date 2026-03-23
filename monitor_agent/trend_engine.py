from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from monitor_agent.core.models import Signal
from monitor_agent.core.utils import utc_now


class TrendEngine:
    """Detects repeated/increasing signal patterns across runs."""

    def detect(self, current_signals: list[Signal], history: list[Signal]) -> list[dict[str, Any]]:
        now = utc_now()
        recent_start = now - timedelta(days=7)
        prev_start = now - timedelta(days=14)
        current_keys = {self._key(signal) for signal in current_signals}

        grouped_recent: dict[str, list[Signal]] = defaultdict(list)
        grouped_prev: dict[str, list[Signal]] = defaultdict(list)

        all_signals = history + current_signals
        for signal in all_signals:
            key = self._key(signal)
            if signal.extracted_at >= recent_start:
                grouped_recent[key].append(signal)
            elif signal.extracted_at >= prev_start:
                grouped_prev[key].append(signal)

        trends: list[dict[str, Any]] = []
        for key, recent_items in grouped_recent.items():
            if key not in current_keys:
                # Keep trends aligned with this cycle's selected signals for readability.
                continue
            recent_count = len(recent_items)
            prev_count = len(grouped_prev.get(key, []))
            if recent_count < 2:
                continue

            top = sorted(recent_items, key=lambda s: s.extracted_at, reverse=True)[0]
            direction = "increasing" if recent_count > prev_count else "stable"
            trends.append(
                {
                    "key": key,
                    "title": top.title,
                    "direction": direction,
                    "recent_count": recent_count,
                    "previous_count": prev_count,
                    "source": top.source,
                    "tracking_id": top.tracking_id,
                }
            )

        trends.sort(key=lambda row: (row["recent_count"] - row["previous_count"], row["recent_count"]), reverse=True)
        return trends[:5]

    @staticmethod
    def _key(signal: Signal) -> str:
        if signal.tracking_id:
            return f"tracking:{signal.tracking_id}"
        if signal.event_id:
            return f"event:{signal.event_id}"
        return f"fingerprint:{signal.fingerprint}"
