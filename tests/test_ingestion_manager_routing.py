from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from monitor_agent.core.models import MonitorConfig, RawItem
from monitor_agent.ingestion_layer.manager import IngestionManager


def _item(source_type: str) -> RawItem:
    return RawItem(
        source_type=source_type,  # type: ignore[arg-type]
        source_name="unit-source",
        title="Test Title",
        url="https://example.com/a",
        content="Test content",
        fetched_at=datetime.now(UTC),
    )


class IngestionManagerRoutingTests(unittest.TestCase):
    def test_web_source_prefers_html_parser_when_available(self) -> None:
        config = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [],
                    "playwright": [{"name": "Example Web", "url": "https://example.com/news"}],
                },
            }
        )
        manager = IngestionManager(config=config, playwright_profile_dir="/tmp/profile")

        with (
            patch("monitor_agent.ingestion_layer.manager.HtmlIngestor") as html_cls,
            patch("monitor_agent.ingestion_layer.manager.PlaywrightIngestor") as pw_cls,
        ):
            html_cls.return_value.ingest.return_value = [_item("html")]
            items, errors = manager.ingest_all()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_type, "html")
        self.assertEqual(errors, [])
        pw_cls.assert_not_called()

    def test_web_source_falls_back_to_playwright_when_html_fails(self) -> None:
        config = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [],
                    "playwright": [{"name": "Example Web", "url": "https://example.com/news"}],
                },
            }
        )
        manager = IngestionManager(config=config, playwright_profile_dir="/tmp/profile")

        with (
            patch("monitor_agent.ingestion_layer.manager.HtmlIngestor") as html_cls,
            patch("monitor_agent.ingestion_layer.manager.PlaywrightIngestor") as pw_cls,
        ):
            html_cls.return_value.ingest.side_effect = RuntimeError("html failed")
            pw_cls.return_value.ingest.return_value = [_item("playwright")]
            items, errors = manager.ingest_all()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_type, "playwright")
        self.assertEqual(errors, [])
        pw_cls.assert_called_once()

    def test_force_playwright_skips_html_parser(self) -> None:
        config = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [],
                    "playwright": [
                        {"name": "WSJ", "url": "https://www.wsj.com/tech", "force_playwright": True}
                    ],
                },
            }
        )
        manager = IngestionManager(config=config, playwright_profile_dir="/tmp/profile")

        with (
            patch("monitor_agent.ingestion_layer.manager.HtmlIngestor") as html_cls,
            patch("monitor_agent.ingestion_layer.manager.PlaywrightIngestor") as pw_cls,
        ):
            pw_cls.return_value.ingest.return_value = [_item("playwright")]
            items, errors = manager.ingest_all()

        self.assertEqual(len(items), 1)
        self.assertEqual(errors, [])
        html_cls.assert_not_called()
        pw_cls.assert_called_once()

    def test_rss_sources_are_ingested_in_parallel(self) -> None:
        config = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [
                        {"name": "Feed A", "url": "https://a.example.com/rss.xml"},
                        {"name": "Feed B", "url": "https://b.example.com/rss.xml"},
                    ],
                    "playwright": [],
                },
            }
        )
        manager = IngestionManager(config=config, playwright_profile_dir="/tmp/profile")

        def _slow_ingest(self):  # type: ignore[no-untyped-def]
            time.sleep(0.2)
            return [_item("rss")]

        with patch("monitor_agent.ingestion_layer.manager.RSSIngestor.ingest", new=_slow_ingest):
            start = time.perf_counter()
            items, errors = manager.ingest_all()
            elapsed = time.perf_counter() - start

        self.assertEqual(len(items), 2)
        self.assertEqual(errors, [])
        # Parallel run should be noticeably less than sequential (~0.4s).
        self.assertLess(elapsed, 0.35)


if __name__ == "__main__":
    unittest.main()
