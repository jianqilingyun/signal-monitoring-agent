from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from monitor_agent.core.models import MonitorConfig
from monitor_agent.core.storage import Storage
from monitor_agent.inbox_engine import InboxEngine, UserSignalInput
from monitor_agent.user_input_resolver import UserInputResolver
from monitor_agent.ingestion_layer.html_parser import ParsedHtmlPage


def _base_config() -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "domain": "Test Domain",
            "sources": {"rss": [], "playwright": []},
            "api": {
                "host": "127.0.0.1",
                "port": 8080,
                "scheduler_enabled": False,
                "auto_run_on_user_ingest": False,
                "telegram_ingest_enabled": False,
            },
        }
    )


class UserInputResolutionTests(unittest.TestCase):
    def test_resolver_fetches_url_content_before_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            resolver = UserInputResolver(config=_base_config(), storage=storage)
            article = ParsedHtmlPage(
                url="https://example.com/story",
                final_url="https://example.com/story",
                title="Example Story",
                text=(
                    "Example Story body paragraph one with sufficient detail to pass the parser threshold. "
                    "Paragraph two adds concrete facts about the story, the actors involved, and the main change. "
                    "Paragraph three reinforces that this is a meaningful article body rather than a short stub."
                ),
                links=[],
                meta_publish_times=["2026-03-19T00:00:00Z"],
                content_type="text/html",
            )

            with patch("monitor_agent.user_input_resolver.validate_public_http_url", side_effect=lambda url: url):
                with patch("monitor_agent.user_input_resolver.fetch_parsed_html", return_value=article):
                    resolved = resolver.resolve(
                        [
                            UserSignalInput(
                                title="Please track this",
                                context="Please track this:\nhttps://example.com/story",
                            )
                        ]
                    )[0]

            self.assertEqual(resolved.original_context, "Please track this:\nhttps://example.com/story")
            self.assertEqual(resolved.title, "Example Story")
            self.assertEqual(resolved.resolved_title, "Example Story")
            self.assertIn("Example Story body paragraph one", resolved.resolved_context or "")
            self.assertEqual(resolved.source_urls, ["https://example.com/story"])
            self.assertEqual(resolved.resolution_method, "html_parser")

    def test_inbox_uses_resolved_context_and_preserves_original_submission(self) -> None:
        class _FakeResolver:
            def resolve(self, inputs: list[UserSignalInput]) -> list[UserSignalInput]:
                out: list[UserSignalInput] = []
                for item in inputs:
                    out.append(
                        item.model_copy(
                            update={
                                "original_context": item.context,
                                "resolved_title": "Resolved Article Title",
                                "resolved_context": "Resolved article body with concrete facts.",
                                "resolution_method": "html_parser",
                                "resolution_errors": [],
                            }
                        )
                    )
                return out

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage, resolver=_FakeResolver())
            signals = inbox.ingest_user_signals(
                [
                    UserSignalInput(
                        title="Track this link",
                        context="Please track this:\nhttps://example.com/story",
                    )
                ]
            )

            self.assertEqual(len(signals), 1)
            signal = signals[0]
            self.assertEqual(signal.title, "Resolved Article Title")
            self.assertEqual(signal.user_context, "Please track this:\nhttps://example.com/story")
            self.assertIn("Resolved article body with concrete facts.", signal.summary)
            self.assertIn("resolved_via=html_parser", signal.evidence)


if __name__ == "__main__":
    unittest.main()
