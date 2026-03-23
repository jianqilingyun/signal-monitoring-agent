from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from monitor_agent.core.models import MonitorConfig
from monitor_agent.core.storage import Storage
from monitor_agent.user_input_resolver import UserInputResolver


def _config(root_dir: str) -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "domain": "AI Infrastructure",
            "domain_profiles": [{"domain": "AI Infrastructure"}],
            "sources": {"rss": [], "playwright": []},
            "llm": {"provider": "openai", "model": "gpt-5-mini", "embedding_model": "text-embedding-3-small"},
            "tts": {"enabled": False, "provider": "gtts", "model": "gpt-4o-mini-tts", "voice": "alloy"},
            "notifications": {
                "channel": "none",
                "channels": [],
                "telegram": {"enabled": False},
                "dingtalk": {"enabled": False, "ingest_enabled": False},
            },
            "storage": {"root_dir": root_dir, "base_path": root_dir},
            "api": {"host": "127.0.0.1", "port": 8080, "scheduler_enabled": False},
        }
    )


class UserInputSecurityTests(unittest.TestCase):
    def test_private_url_is_blocked_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            resolver = UserInputResolver(config=_config(tmpdir), storage=Storage(tmpdir))
            with patch("monitor_agent.user_input_resolver.fetch_parsed_html", side_effect=AssertionError("should not fetch")):
                resolved = resolver.resolve(
                    [
                        {
                            "title": "private",
                            "context": "http://127.0.0.1/private",
                            "source_urls": ["http://127.0.0.1/private"],
                        }
                    ]
                )[0]

            self.assertEqual(resolved["source_urls"], [])
            self.assertTrue(any("Blocked unsafe URL" in row for row in resolved["resolution_errors"]))

    def test_playwright_fallback_is_disabled_for_user_links_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"MONITOR_ALLOW_USER_INPUT_PLAYWRIGHT": "false"}, clear=False):
            resolver = UserInputResolver(config=_config(tmpdir), storage=Storage(tmpdir))
            with patch("monitor_agent.user_input_resolver.validate_public_http_url", side_effect=lambda url: url):
                with patch("monitor_agent.user_input_resolver.fetch_parsed_html", side_effect=RuntimeError("html failed")):
                    with patch.object(resolver, "_fetch_with_playwright", side_effect=AssertionError("playwright should stay disabled")):
                        resolved = resolver.resolve(
                            [
                                {
                                    "title": "public",
                                    "context": "https://openai.com/index/hello",
                                    "source_urls": ["https://openai.com/index/hello"],
                                }
                            ]
                        )[0]

            self.assertTrue(any("Playwright fallback disabled" in row for row in resolved["resolution_errors"]))


if __name__ == "__main__":
    unittest.main()
