from __future__ import annotations

import time
import types
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from monitor_agent.core.models import PlaywrightRuntimeConfig, RssSourceConfig, RawItem
from monitor_agent.ingestion_layer.html_parser import ParsedHtmlPage
from monitor_agent.ingestion_layer.rss_ingestor import RSSIngestor


class RssSecondHopTests(unittest.TestCase):
    def test_rss_ingestor_prefers_article_full_text_when_available(self) -> None:
        feed_entry = types.SimpleNamespace(
            title="Feed title",
            link="https://example.com/post/1",
            summary="Short feed summary",
            published_parsed=None,
            updated_parsed=None,
        )
        fake_feed = types.SimpleNamespace(entries=[feed_entry])
        parsed_article = ParsedHtmlPage(
            url="https://example.com/post/1",
            final_url="https://example.com/post/1",
            title="Article title",
            text="This is a much longer full article body. " * 20,
            links=[],
            meta_publish_times=["2026-03-18T00:10:00Z"],
            content_type="text/html; charset=utf-8",
        )
        source = RssSourceConfig(name="Example RSS", url="https://example.com/feed.xml", max_items=5)

        with (
            patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed),
            patch("monitor_agent.ingestion_layer.rss_ingestor.fetch_parsed_html", return_value=parsed_article),
        ):
            items = RSSIngestor(source).ingest()

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("much longer full article body", item.content)
        self.assertEqual(item.metadata.get("content_source"), "article_full_text")
        self.assertEqual(item.metadata.get("meta_publish_times"), ["2026-03-18T00:10:00Z"])

    def test_rss_ingestor_can_disable_second_hop(self) -> None:
        feed_entry = types.SimpleNamespace(
            title="Feed title",
            link="https://example.com/post/1",
            summary="Short feed summary",
            published_parsed=None,
            updated_parsed=None,
        )
        fake_feed = types.SimpleNamespace(entries=[feed_entry])
        source = RssSourceConfig(
            name="Example RSS",
            url="https://example.com/feed.xml",
            max_items=5,
            fetch_full_text=False,
        )

        with (
            patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed),
            patch("monitor_agent.ingestion_layer.rss_ingestor.fetch_parsed_html") as fetch_mock,
        ):
            items = RSSIngestor(source).ingest()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content, "Short feed summary")
        fetch_mock.assert_not_called()

    def test_rss_ingestor_prefers_feed_content_field_when_more_informative(self) -> None:
        feed_entry = types.SimpleNamespace(
            title="Feed title",
            link="https://example.com/post/1",
            summary="Brief summary",
            content=[{"value": "<p>" + ("Detailed feed content. " * 20) + "</p>"}],
            published_parsed=None,
            updated_parsed=None,
        )
        fake_feed = types.SimpleNamespace(entries=[feed_entry])
        source = RssSourceConfig(
            name="Example RSS",
            url="https://example.com/feed.xml",
            max_items=5,
            fetch_full_text=False,
        )

        with (
            patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed),
            patch("monitor_agent.ingestion_layer.rss_ingestor.fetch_parsed_html") as fetch_mock,
        ):
            items = RSSIngestor(source).ingest()

        self.assertEqual(len(items), 1)
        self.assertIn("Detailed feed content", items[0].content)
        self.assertEqual(items[0].metadata.get("content_source"), "rss_content")
        fetch_mock.assert_not_called()

    def test_rss_ingestor_records_forbidden_second_hop_errors(self) -> None:
        feed_entries = [
            types.SimpleNamespace(
                title="Post A",
                link="https://example.com/post/a",
                summary="Summary A",
                published_parsed=None,
                updated_parsed=None,
            ),
            types.SimpleNamespace(
                title="Post B",
                link="https://example.com/post/b",
                summary="Summary B",
                published_parsed=None,
                updated_parsed=None,
            ),
        ]
        fake_feed = types.SimpleNamespace(entries=feed_entries)
        source = RssSourceConfig(name="Example RSS", url="https://example.com/feed.xml", max_items=5)

        with (
            patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed),
            patch(
                "monitor_agent.ingestion_layer.rss_ingestor.fetch_parsed_html",
                side_effect=RuntimeError("HTTP Error 403: Forbidden"),
            ) as fetch_mock,
        ):
            items = RSSIngestor(source).ingest()

        self.assertEqual(len(items), 2)
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertIn("403", str(items[0].metadata.get("second_hop_error", "")))
        self.assertIn("403", str(items[1].metadata.get("second_hop_error", "")))
        self.assertEqual(items[0].metadata.get("content_source"), "rss_summary")
        self.assertEqual(items[1].metadata.get("content_source"), "rss_summary")

    def test_rss_ingestor_uses_browser_fallback_on_forbidden_second_hop(self) -> None:
        feed_entry = types.SimpleNamespace(
            title="Post A",
            link="https://example.com/post/a",
            summary="Summary A",
            published_parsed=None,
            updated_parsed=None,
        )
        fake_feed = types.SimpleNamespace(entries=[feed_entry])
        source = RssSourceConfig(name="Example RSS", url="https://example.com/feed.xml", max_items=5)
        browser_item = RawItem(
            source_type="playwright",
            source_name="Example RSS fallback",
            title="Browser title",
            url="https://example.com/post/a",
            content="Browser full text that should replace the blocked RSS second hop.",
            fetched_at=datetime.now(UTC),
            metadata={"meta_publish_times": ["2026-03-18T00:10:00Z"]},
        )

        with (
            patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed),
            patch("monitor_agent.ingestion_layer.rss_ingestor.fetch_parsed_html", side_effect=RuntimeError("HTTP Error 403: Forbidden")),
            patch("monitor_agent.ingestion_layer.rss_ingestor.PlaywrightIngestor") as pw_cls,
        ):
            pw_cls.return_value.ingest.return_value = [browser_item]
            items = RSSIngestor(
                source,
                playwright_profile_dir="/tmp/profile",
                playwright_runtime=PlaywrightRuntimeConfig(),
            ).ingest()

        self.assertEqual(len(items), 1)
        self.assertIn("Browser full text", items[0].content)
        self.assertEqual(items[0].metadata.get("content_source"), "article_full_text")
        self.assertEqual(items[0].metadata.get("meta_publish_times"), ["2026-03-18T00:10:00Z"])

    def test_rss_ingestor_skips_second_hop_for_aggregator_feeds(self) -> None:
        feed_entry = types.SimpleNamespace(
            title="HN item",
            link="https://example.com/post/1",
            summary="Short feed summary",
            published_parsed=None,
            updated_parsed=None,
        )
        fake_feed = types.SimpleNamespace(entries=[feed_entry])
        source = RssSourceConfig(name="HN RSS", url="https://hnrss.org/frontpage", max_items=5)

        with (
            patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed),
            patch("monitor_agent.ingestion_layer.rss_ingestor.fetch_parsed_html") as fetch_mock,
        ):
            items = RSSIngestor(source).ingest()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].metadata.get("second_hop_skip_reason"), "aggregator_feed")
        self.assertTrue(bool(items[0].metadata.get("second_hop_skipped")))
        fetch_mock.assert_not_called()

    def test_rss_second_hop_uses_bounded_parallel_workers(self) -> None:
        feed_entries = []
        for idx in range(3):
            feed_entries.append(
                types.SimpleNamespace(
                    title=f"Post {idx}",
                    link=f"https://example.com/post/{idx}",
                    summary="Summary",
                    published_parsed=None,
                    updated_parsed=None,
                )
            )
        fake_feed = types.SimpleNamespace(entries=feed_entries)
        source = RssSourceConfig(name="Example RSS", url="https://example.com/feed.xml", max_items=5)
        parsed_article = ParsedHtmlPage(
            url="https://example.com/post/0",
            final_url="https://example.com/post/0",
            title="Article title",
            text="Full body " * 120,
            links=[],
            meta_publish_times=[],
            content_type="text/html; charset=utf-8",
        )

        def _slow_fetch(*args, **kwargs):  # type: ignore[no-untyped-def]
            time.sleep(0.2)
            return parsed_article

        with (
            patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed),
            patch("monitor_agent.ingestion_layer.rss_ingestor.fetch_parsed_html", side_effect=_slow_fetch),
        ):
            start = time.perf_counter()
            items = RSSIngestor(source).ingest()
            elapsed = time.perf_counter() - start

        self.assertEqual(len(items), 3)
        # Sequential would be ~0.6s for three second-hop fetches.
        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
