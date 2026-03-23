from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from monitor_agent.core.models import Signal
from monitor_agent.core.utils import make_fingerprint
from monitor_agent.trend_engine import TrendEngine


def _signal(
    *,
    title: str,
    extracted_at: datetime,
    event_id: str | None = None,
) -> Signal:
    return Signal(
        title=title,
        summary="summary",
        importance=0.8,
        category="test",
        source_urls=["https://example.com/a"],
        extracted_at=extracted_at,
        fingerprint=make_fingerprint(title),
        event_id=event_id,
    )


class TrendEngineTests(unittest.TestCase):
    def test_detect_limits_trends_to_current_signal_keys(self) -> None:
        now = datetime.now(UTC)
        current = [_signal(title="Current Event", extracted_at=now, event_id="evt_current")]
        history = [
            _signal(title="Current Event Older", extracted_at=now - timedelta(days=1), event_id="evt_current"),
            _signal(title="Other Event", extracted_at=now - timedelta(days=1), event_id="evt_other"),
            _signal(title="Other Event 2", extracted_at=now - timedelta(days=2), event_id="evt_other"),
        ]

        trends = TrendEngine().detect(current, history)
        self.assertEqual(len(trends), 1)
        self.assertEqual(trends[0]["key"], "event:evt_current")

    def test_detect_requires_at_least_two_recent_hits(self) -> None:
        now = datetime.now(UTC)
        current = [_signal(title="Single Hit", extracted_at=now, event_id="evt_single")]
        history = []
        trends = TrendEngine().detect(current, history)
        self.assertEqual(trends, [])


if __name__ == "__main__":
    unittest.main()
