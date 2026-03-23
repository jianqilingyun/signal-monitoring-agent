from __future__ import annotations

import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from monitor_agent.core.storage import Storage
from monitor_agent.strategy_engine import source_strategy_engine
from monitor_agent.strategy_engine.source_strategy_engine import SourceStrategyEngine


class SourceStrategyEngineTests(unittest.TestCase):
    def test_suggest_reuses_cache_for_unchanged_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            engine = SourceStrategyEngine(storage=storage, llm_config=None, use_llm=False)
            counter = {"calls": 0}

            def _fake_analyze(self, url: str, *, refresh_interval_days: int, analyzed_at: datetime):
                counter["calls"] += 1
                return self._heuristic_web_suggestion(
                    url=url,
                    host="example.com",
                    analysis={"list_like_page": True, "article_patterns": ["/article/"], "playwright_needed": True},
                    refresh_interval_days=refresh_interval_days,
                    analyzed_at=analyzed_at,
                )

            engine._analyze_url = types.MethodType(_fake_analyze, engine)  # type: ignore[assignment]

            first = engine.suggest(["https://example.com/news"], refresh_interval_days=14, force_refresh=False)
            second = engine.suggest(["https://example.com/news"], refresh_interval_days=14, force_refresh=False)

            self.assertEqual(first.recomputed, 1)
            self.assertEqual(second.reused_cached, 1)
            self.assertEqual(counter["calls"], 1)

    def test_cache_refresh_triggers_recompute_after_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            engine = SourceStrategyEngine(storage=storage, llm_config=None, use_llm=False)
            now = datetime.now(UTC)

            old_suggestion = engine._heuristic_web_suggestion(
                url="https://example.com/news",
                host="example.com",
                analysis={"list_like_page": False, "article_patterns": [], "playwright_needed": False},
                refresh_interval_days=14,
                analyzed_at=now - timedelta(days=30),
            )
            cache = {"https://example.com/news": old_suggestion.model_dump(mode="json")}
            storage.save_source_strategy_cache(cache)

            counter = {"calls": 0}

            def _fake_analyze(self, url: str, *, refresh_interval_days: int, analyzed_at: datetime):
                counter["calls"] += 1
                return self._heuristic_web_suggestion(
                    url=url,
                    host="example.com",
                    analysis={"list_like_page": True, "article_patterns": ["/article/"], "playwright_needed": True},
                    refresh_interval_days=refresh_interval_days,
                    analyzed_at=analyzed_at,
                )

            engine._analyze_url = types.MethodType(_fake_analyze, engine)  # type: ignore[assignment]
            result = engine.suggest(["https://example.com/news"], refresh_interval_days=14, force_refresh=False)
            self.assertEqual(result.recomputed, 1)
            self.assertEqual(counter["calls"], 1)

    def test_parse_llm_payload_rejects_invalid_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            engine = SourceStrategyEngine(storage=storage, llm_config=None, use_llm=False)

            suggestion, err = engine._parse_llm_payload(  # type: ignore[arg-type]
                parsed={"configured_type": "playwright"},
                url="https://example.com",
                host="example.com",
            )
            self.assertIsNone(suggestion)
            self.assertIn("parser_recommendation", err)

    def test_estimate_feed_max_items_handles_network_errors(self) -> None:
        with patch.object(source_strategy_engine.feedparser, "parse", side_effect=RuntimeError("boom")):
            max_items, freq = source_strategy_engine._estimate_feed_max_items("https://example.com/rss.xml")
            self.assertEqual(max_items, 20)
            self.assertEqual(freq, "unknown")

    def test_heuristic_html_parser_recommendation_sets_non_forced_playwright(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            engine = SourceStrategyEngine(storage=storage, llm_config=None, use_llm=False)

            suggestion = engine._heuristic_web_suggestion(
                url="https://example.com/news",
                host="example.com",
                analysis={"list_like_page": False, "article_patterns": [], "playwright_needed": False},
                refresh_interval_days=14,
                analyzed_at=datetime.now(UTC),
            )
            self.assertEqual(suggestion.parser_recommendation, "html_parser")
            self.assertEqual(suggestion.configured_type, "playwright")
            self.assertFalse(bool(suggestion.normalized_source_link.get("force_playwright")))

    def test_parse_rss_hint_recommends_webpage_when_feed_probe_unhealthy(self) -> None:
        with patch.object(source_strategy_engine, "_probe_feed", return_value={"ok": False, "reason": "zero entries"}):
            hint = SourceStrategyEngine._parse_rss_hint("https://cloud.google.com/blog/rss/")
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual(hint["parser_recommendation"], "html_parser")
        self.assertEqual(hint["configured_type"], "playwright")
        self.assertIn("/blog/", str(hint["normalized_source_link"].get("url", "")))
        self.assertEqual(hint["probe_status"], "warning")
        self.assertTrue(hint["issues"])
        self.assertTrue(hint["fixes"])


if __name__ == "__main__":
    unittest.main()
