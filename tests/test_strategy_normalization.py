from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from monitor_agent.core.models import MonitorConfig
from monitor_agent.core.storage import Storage
from monitor_agent.strategy_engine.models import (
    SourceStrategySuggestResult,
    SourceStrategySuggestion,
    StrategyDeployRequest,
    StrategyGenerateRequest,
    StrategyGetRequest,
    StrategyPreviewRequest,
)
from monitor_agent.strategy_engine.service import StrategyEngine


def _base_config() -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "domain": "Technology",
            "sources": {"rss": [], "playwright": []},
        }
    )


class StrategyNormalizationTests(unittest.TestCase):
    def test_generate_supports_simple_ui_payload_and_internal_strategy(self) -> None:
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                storage = Storage(tmpdir)
                engine = StrategyEngine(base_config=_base_config(), storage=storage)

                result = engine.generate(
                    StrategyGenerateRequest(
                        domain="AI Infrastructure",
                        focus_areas=["GPU supply chain", "Inference cost"],
                        entities=["NVIDIA", "OpenAI"],
                        keywords=["Blackwell", "H200"],
                        source_links=["https://hnrss.org/frontpage", "https://openai.com/news/rss.xml"],
                        timezone="Asia/Shanghai",
                        schedule_times=["07:00", "22:00"],
                    )
                )
                self.assertIsNotNone(result.ui_input)
                self.assertIsNotNone(result.internal_strategy)
                assert result.internal_strategy is not None
                self.assertIn("signal_categories", result.config_object["internal_strategy"])
                self.assertEqual(
                    result.config_object["domain_profiles"][0]["source_links"],
                    ["https://hnrss.org/frontpage", "https://openai.com/news/rss.xml"],
                )
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous

    def test_preview_returns_summary_and_normalized_machine_config(self) -> None:
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                storage = Storage(tmpdir)
                engine = StrategyEngine(base_config=_base_config(), storage=storage)

                preview = engine.preview(
                    StrategyPreviewRequest(
                        domain="Cybersecurity",
                        focus_areas=["vulnerability intelligence"],
                        entities=["CISA"],
                        keywords=["CVE"],
                        source_links=["https://www.cisa.gov/cybersecurity-advisories/all.xml"],
                    )
                )
                self.assertTrue(preview.summary)
                self.assertIn("internal_strategy", preview.normalized_config)
                self.assertIn("signal_categories", preview.normalized_config["internal_strategy"])
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous

    def test_generate_accepts_inline_source_type_hint_in_ui_links(self) -> None:
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                storage = Storage(tmpdir)
                engine = StrategyEngine(base_config=_base_config(), storage=storage)

                result = engine.generate(
                    StrategyGenerateRequest(
                        domain="AI Infrastructure",
                        source_links=["https://www.wsj.com/tech | playwright"],
                    )
                )
                assert result.internal_strategy is not None
                self.assertIn("www.wsj.com", result.internal_strategy.source_weights)
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous

    def test_deploy_modification_request_applies_patch_incrementally(self) -> None:
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                storage = Storage(tmpdir)
                engine = StrategyEngine(base_config=_base_config(), storage=storage)

                engine.generate(
                    StrategyGenerateRequest(
                        domain="AI Infrastructure",
                        focus_areas=["GPU supply chain"],
                        entities=["NVIDIA"],
                        keywords=["Blackwell"],
                        source_links=["https://hnrss.org/frontpage"],
                    )
                )
                deployed_path = str(Path(tmpdir) / "config.generated.yaml")
                deployed = engine.deploy(
                    StrategyDeployRequest(
                        confirm=True,
                        modification_request="add keyword H200",
                        target_config_path=deployed_path,
                    )
                )
                self.assertTrue(deployed.deployed)
                self.assertTrue(Path(deployed.deployed_path).exists())
                state = engine.get(StrategyGetRequest()).strategy
                assert state is not None
                self.assertEqual(state.version, 2)
                self.assertEqual(state.deployed_version, 2)
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous

    @patch("monitor_agent.strategy_engine.service.SourceStrategyEngine.suggest")
    def test_generate_can_auto_diagnose_new_source_links(self, suggest_mock) -> None:
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                storage = Storage(tmpdir)
                engine = StrategyEngine(base_config=_base_config(), storage=storage)
                now = datetime.now(UTC)
                suggest_mock.return_value = SourceStrategySuggestResult(
                    refresh_interval_days=14,
                    total_urls=1,
                    reused_cached=0,
                    recomputed=1,
                    suggestions=[
                        SourceStrategySuggestion(
                            url="https://cloud.google.com/blog/rss/",
                            parser_recommendation="html_parser",
                            configured_type="playwright",
                            normalized_source_link={
                                "url": "https://cloud.google.com/blog/",
                                "type": "playwright",
                                "force_playwright": False,
                            },
                            reason="fallback",
                            confidence=0.9,
                            next_refresh_at=now + timedelta(days=14),
                            analyzed_at=now,
                            probe_status="warning",
                            issues=["rss invalid"],
                            fixes=["switch to web page"],
                        )
                    ],
                )

                result = engine.generate(
                    StrategyGenerateRequest(
                        domain="AI Infrastructure",
                        source_links=["https://cloud.google.com/blog/rss/"],
                        advanced_settings={"auto_source_diagnosis": True},
                    )
                )

                links = result.config_object["domain_profiles"][0]["source_links"]
                self.assertIsInstance(links, list)
                self.assertTrue(links)
                self.assertIsInstance(links[0], dict)
                self.assertEqual(links[0]["url"], "https://cloud.google.com/blog/")
                self.assertEqual(links[0]["type"], "playwright")
                self.assertFalse(links[0]["force_playwright"])
                suggest_mock.assert_called_once()
                self.assertEqual(
                    suggest_mock.call_args.kwargs.get("urls"),
                    ["https://cloud.google.com/blog/rss/"],
                )
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous

    @patch("monitor_agent.strategy_engine.service.SourceStrategyEngine.suggest")
    def test_generate_auto_diagnosis_only_checks_new_links(self, suggest_mock) -> None:
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                storage = Storage(tmpdir)
                engine = StrategyEngine(base_config=_base_config(), storage=storage)
                now = datetime.now(UTC)
                suggest_mock.side_effect = [
                    SourceStrategySuggestResult(
                        refresh_interval_days=14,
                        total_urls=1,
                        reused_cached=0,
                        recomputed=1,
                        suggestions=[
                            SourceStrategySuggestion(
                                url="https://a.example.com/feed.xml",
                                parser_recommendation="rss",
                                configured_type="rss",
                                normalized_source_link={"url": "https://a.example.com/feed.xml", "type": "rss"},
                                reason="rss",
                                confidence=0.9,
                                next_refresh_at=now + timedelta(days=14),
                                analyzed_at=now,
                            )
                        ],
                    ),
                    SourceStrategySuggestResult(
                        refresh_interval_days=14,
                        total_urls=1,
                        reused_cached=0,
                        recomputed=1,
                        suggestions=[
                            SourceStrategySuggestion(
                                url="https://b.example.com/news/",
                                parser_recommendation="html_parser",
                                configured_type="playwright",
                                normalized_source_link={
                                    "url": "https://b.example.com/news/",
                                    "type": "playwright",
                                    "force_playwright": False,
                                },
                                reason="web",
                                confidence=0.8,
                                next_refresh_at=now + timedelta(days=14),
                                analyzed_at=now,
                            )
                        ],
                    ),
                ]

                engine.generate(
                    StrategyGenerateRequest(
                        domain="AI Infrastructure",
                        source_links=["https://a.example.com/feed.xml"],
                        advanced_settings={"auto_source_diagnosis": True},
                    )
                )
                engine.generate(
                    StrategyGenerateRequest(
                        domain="AI Infrastructure",
                        source_links=["https://a.example.com/feed.xml", "https://b.example.com/news/"],
                        advanced_settings={"auto_source_diagnosis": True},
                    )
                )

                self.assertEqual(suggest_mock.call_count, 2)
                first_urls = suggest_mock.call_args_list[0].kwargs.get("urls")
                second_urls = suggest_mock.call_args_list[1].kwargs.get("urls")
                self.assertEqual(first_urls, ["https://a.example.com/feed.xml"])
                self.assertEqual(second_urls, ["https://b.example.com/news/"])
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
