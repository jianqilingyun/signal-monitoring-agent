from __future__ import annotations

import unittest
from datetime import UTC, datetime

from monitor_agent.core.models import LLMConfig, RawItem
from monitor_agent.signal_engine.extractor import LLMSignalExtractor


class SignalExtractorPromptTests(unittest.TestCase):
    def test_prompt_allows_single_authoritative_source(self) -> None:
        extractor = LLMSignalExtractor(LLMConfig())
        item = RawItem(
            source_type="rss",
            source_name="unit-test-source",
            title="Example title",
            url="https://example.com/post",
            content="Example content",
            fetched_at=datetime(2026, 3, 18, 0, 0, 0, tzinfo=UTC),
        )

        prompt = extractor._build_prompt(  # noqa: SLF001 - private helper tested intentionally
            domain="AI Infrastructure",
            items=[item],
            strategy_profile=None,
        )

        self.assertIn("A single authoritative source is sufficient.", prompt)
        self.assertIn("Use Simplified Chinese for summary", prompt)

    def test_prompt_clips_item_content_to_6000_chars(self) -> None:
        extractor = LLMSignalExtractor(LLMConfig())
        long_text = "A" * 8000
        item = RawItem(
            source_type="rss",
            source_name="unit-test-source",
            title="Long article",
            url="https://example.com/long",
            content=long_text,
            fetched_at=datetime(2026, 3, 18, 0, 0, 0, tzinfo=UTC),
        )

        prompt = extractor._build_prompt(  # noqa: SLF001 - private helper tested intentionally
            domain="AI Infrastructure",
            items=[item],
            strategy_profile=None,
        )

        self.assertIn('"content": "' + ("A" * 6000) + '"', prompt)
        self.assertNotIn('"content": "' + ("A" * 6100) + '"', prompt)


if __name__ == "__main__":
    unittest.main()
