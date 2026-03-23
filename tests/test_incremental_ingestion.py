from __future__ import annotations

import types
import unittest
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from unittest.mock import patch

from monitor_agent.core.models import MonitorConfig, RawItem, SourceCursorState
from monitor_agent.core.storage import Storage
from monitor_agent.ingestion_layer.html_ingestor import HtmlIngestor
from monitor_agent.ingestion_layer.html_parser import ParsedHtmlPage
from monitor_agent.ingestion_layer.manager import IngestionManager
from monitor_agent.ingestion_layer.rss_ingestor import RSSIngestor
from monitor_agent.ingestion_layer.source_cursor import make_source_key


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0):
    return types.SimpleNamespace(
        tm_year=year,
        tm_mon=month,
        tm_mday=day,
        tm_hour=hour,
        tm_min=minute,
        tm_sec=second,
    )


class IncrementalIngestionTests(unittest.TestCase):
    def test_rss_incremental_cutoff_uses_time_and_overlap(self) -> None:
        source = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [
                        {
                            "name": "Example RSS",
                            "url": "https://example.com/feed.xml",
                            "fetch_full_text": False,
                            "incremental_overlap_count": 1,
                        }
                    ],
                    "playwright": [],
                },
            }
        ).sources.rss[0]
        fake_feed = types.SimpleNamespace(
            entries=[
                types.SimpleNamespace(
                    id="old-a",
                    title="Old A",
                    link="https://example.com/a",
                    summary="Old A summary",
                    published_parsed=_ts(2026, 3, 20, 11),
                    updated_parsed=None,
                ),
                types.SimpleNamespace(
                    id="new-b",
                    title="New B",
                    link="https://example.com/b",
                    summary="New B summary",
                    published_parsed=_ts(2026, 3, 20, 13),
                    updated_parsed=None,
                ),
                types.SimpleNamespace(
                    id="old-c",
                    title="Old C",
                    link="https://example.com/c",
                    summary="Old C summary",
                    published_parsed=_ts(2026, 3, 20, 10),
                    updated_parsed=None,
                ),
            ]
        )
        cursor = SourceCursorState(
            source_key=make_source_key("rss", source.url),
            source_type="rss",
            source_url=source.url,
            last_seen_published_at=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
            last_seen_ids=["old-a", "old-c"],
            last_seen_urls=["https://example.com/a", "https://example.com/c"],
            overlap_count=1,
            incremental_mode="mixed",
        )

        with patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed):
            items = RSSIngestor(source, cursor_state=cursor).ingest()

        self.assertEqual([item.title for item in items], ["Old A", "New B"])

    def test_rss_incremental_cutoff_falls_back_to_entry_identity(self) -> None:
        source = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [
                        {
                            "name": "Example RSS",
                            "url": "https://example.com/feed.xml",
                            "fetch_full_text": False,
                            "incremental_overlap_count": 0,
                        }
                    ],
                    "playwright": [],
                },
            }
        ).sources.rss[0]
        fake_feed = types.SimpleNamespace(
            entries=[
                types.SimpleNamespace(
                    id="known-a",
                    title="Known A",
                    link="https://example.com/a",
                    summary="Known A summary",
                    published_parsed=None,
                    updated_parsed=None,
                ),
                types.SimpleNamespace(
                    id="new-b",
                    title="New B",
                    link="https://example.com/b",
                    summary="New B summary",
                    published_parsed=None,
                    updated_parsed=None,
                ),
            ]
        )
        cursor = SourceCursorState(
            source_key=make_source_key("rss", source.url),
            source_type="rss",
            source_url=source.url,
            last_seen_ids=["known-a"],
            last_seen_urls=["https://example.com/a"],
            overlap_count=0,
            incremental_mode="id",
        )

        with patch("monitor_agent.ingestion_layer.rss_ingestor.feedparser.parse", return_value=fake_feed):
            items = RSSIngestor(source, cursor_state=cursor).ingest()

        self.assertEqual([item.title for item in items], ["New B"])

    def test_html_follow_links_only_fetches_incremental_urls(self) -> None:
        source = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [],
                    "playwright": [
                        {
                            "name": "Example Web",
                            "url": "https://example.com/news",
                            "follow_links_enabled": True,
                            "max_links_per_source": 10,
                            "incremental_overlap_count": 1,
                        }
                    ],
                },
            }
        ).sources.playwright[0]
        cursor = SourceCursorState(
            source_key=make_source_key("html", source.url),
            source_type="html",
            source_url=source.url,
            last_seen_urls=["https://example.com/a", "https://example.com/b"],
            overlap_count=1,
            incremental_mode="url",
        )
        root = ParsedHtmlPage(
            url=source.url,
            final_url=source.url,
            title="Root",
            text="Root body",
            links=["https://example.com/a", "https://example.com/b", "https://example.com/c"],
            meta_publish_times=[],
            content_type="text/html",
            link_candidates=[
                {"url": "https://example.com/a", "publish_time": None},
                {"url": "https://example.com/b", "publish_time": None},
                {"url": "https://example.com/c", "publish_time": None},
            ],
        )

        def _fetch(url: str, **_: object) -> ParsedHtmlPage:
            if url == source.url:
                return root
            suffix = url.rsplit("/", 1)[-1]
            return ParsedHtmlPage(
                url=url,
                final_url=url,
                title=f"Article {suffix.upper()}",
                text=f"Body {suffix}",
                links=[],
                meta_publish_times=[],
                content_type="text/html",
            )

        with patch("monitor_agent.ingestion_layer.html_ingestor.fetch_parsed_html", side_effect=_fetch) as fetch_mock:
            items = HtmlIngestor(source, cursor_state=cursor).ingest()

        self.assertEqual([item.title for item in items], ["Root", "Article A", "Article C"])
        self.assertEqual(fetch_mock.call_count, 3)

    def test_html_follow_links_prefers_time_hint_for_incremental_pruning(self) -> None:
        source = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [],
                    "playwright": [
                        {
                            "name": "Example Web",
                            "url": "https://example.com/news",
                            "follow_links_enabled": True,
                            "max_links_per_source": 10,
                            "incremental_overlap_count": 0,
                        }
                    ],
                },
            }
        ).sources.playwright[0]
        cursor = SourceCursorState(
            source_key=make_source_key("html", source.url),
            source_type="html",
            source_url=source.url,
            last_seen_published_at=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
            last_seen_urls=["https://example.com/a"],
            overlap_count=0,
            incremental_mode="mixed",
        )
        root = ParsedHtmlPage(
            url=source.url,
            final_url=source.url,
            title="Root",
            text="Root body",
            links=["https://example.com/a", "https://example.com/b"],
            meta_publish_times=[],
            content_type="text/html",
            link_candidates=[
                {"url": "https://example.com/a", "publish_time": "2026-03-20T10:00:00Z"},
                {"url": "https://example.com/b", "publish_time": "2026-03-20T13:00:00Z"},
            ],
        )

        def _fetch(url: str, **_: object) -> ParsedHtmlPage:
            if url == source.url:
                return root
            return ParsedHtmlPage(
                url=url,
                final_url=url,
                title="Article B",
                text="Body b",
                links=[],
                meta_publish_times=[],
                content_type="text/html",
            )

        with patch("monitor_agent.ingestion_layer.html_ingestor.fetch_parsed_html", side_effect=_fetch) as fetch_mock:
            items = HtmlIngestor(source, cursor_state=cursor).ingest()

        self.assertEqual([item.title for item in items], ["Root", "Article B"])
        self.assertEqual(fetch_mock.call_count, 2)

    def test_ingestion_manager_persists_source_cursors(self) -> None:
        config = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [{"name": "Feed A", "url": "https://a.example.com/rss.xml"}],
                    "playwright": [],
                },
            }
        )
        cursor = SourceCursorState(
            source_key=make_source_key("rss", "https://a.example.com/rss.xml"),
            source_type="rss",
            source_url="https://a.example.com/rss.xml",
            last_seen_published_at=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
        )
        item = RawItem(
            source_type="rss",
            source_name="Feed A",
            title="Test Title",
            url="https://a.example.com/post",
            content="Test content",
            fetched_at=datetime.now(UTC),
        )
        with TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            manager = IngestionManager(config=config, playwright_profile_dir="/tmp/profile", storage=storage)
            with patch.object(
                IngestionManager,
                "_ingest_rss_source",
                return_value=([item], cursor, {"candidate_count": 3, "kept_count": 1, "overlap_kept": 0, "dropped_count": 2}),
            ):
                items, errors = manager.ingest_all()

            persisted = storage.load_source_cursors()

        self.assertEqual(len(items), 1)
        self.assertEqual(errors, [])
        self.assertIn(cursor.source_key, persisted)
        self.assertEqual(persisted[cursor.source_key].last_seen_published_at, cursor.last_seen_published_at)

    def test_ingestion_manager_skips_source_when_refresh_not_due(self) -> None:
        config = MonitorConfig.model_validate(
            {
                "domain": "AI Infra",
                "sources": {
                    "rss": [
                        {
                            "name": "Feed A",
                            "url": "https://a.example.com/rss.xml",
                            "refresh_interval_hours": 24,
                        }
                    ],
                    "playwright": [],
                },
            }
        )
        with TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            storage.save_source_cursors(
                {
                    make_source_key("rss", "https://a.example.com/rss.xml"): SourceCursorState(
                        source_key=make_source_key("rss", "https://a.example.com/rss.xml"),
                        source_type="rss",
                        source_url="https://a.example.com/rss.xml",
                        last_success_at=datetime.now(UTC),
                    )
                }
            )
            manager = IngestionManager(config=config, playwright_profile_dir="/tmp/profile", storage=storage)
            with patch.object(IngestionManager, "_ingest_rss_source") as ingest_mock:
                items, errors = manager.ingest_all()

        self.assertEqual(items, [])
        self.assertEqual(errors, [])
        ingest_mock.assert_not_called()
        health = manager.last_source_health[make_source_key("rss", "https://a.example.com/rss.xml")]
        self.assertEqual(health["status"], "skipped")
        self.assertEqual(health["skip_reason"], "refresh_interval_not_due")


if __name__ == "__main__":
    unittest.main()
