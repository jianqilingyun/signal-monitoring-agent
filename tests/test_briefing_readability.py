from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from monitor_agent.briefing.generator import BriefingGenerator
from monitor_agent.storage_engine import StorageEngine


class BriefingReadabilityTests(unittest.TestCase):
    def test_generate_human_readable_brief_without_debug_style_lines(self) -> None:
        generator = BriefingGenerator()
        signal = SimpleNamespace(
            source="system",
            event_type="new",
            title="Example Signal",
            freshness="fresh",
            summary="Example summary.",
            evidence=["Example evidence line."],
            tags=["gpu"],
            source_urls=["https://example.com/a", "https://example.com/b"],
        )
        text = generator.generate(
            domain="AI Infrastructure",
            signals=[signal],  # type: ignore[arg-type]
            trends=[
                {
                    "title": "Example Trend",
                    "direction": "increasing",
                    "recent_count": 2,
                    "previous_count": 0,
                }
            ],
            watchlist=[],
            generated_at=datetime(2026, 3, 18, 1, 2, 3, tzinfo=UTC),
        )

        self.assertIn("# 监控简报 / Monitoring Brief - AI Infrastructure", text)
        self.assertNotIn("## Section 1: 收件箱重点 / Inbox", text)
        self.assertIn("## Section 2: 系统重点信号 / System Signals", text)
        self.assertNotIn("## Section 3: 趋势 / Trends", text)
        self.assertNotIn("## Section 4: 追踪列表 / Watchlist", text)
        self.assertNotIn("Example Trend：趋势上升，近窗 2，前窗 0。", text)
        self.assertNotIn("direction=increasing", text)
        self.assertNotIn("Importance:", text)
        self.assertIn("**发生了什么**", text)
        self.assertIn("**为什么重要**", text)
        self.assertIn("**后续跟踪**", text)
        self.assertNotIn("**English Insight**", text)
        self.assertNotIn("## Sources / 来源索引", text)
        self.assertNotIn("当前判定为新事件", text)
        self.assertNotIn("时间标记为", text)
        self.assertIn("**来源** [Example·a](https://example.com/a) [Example·b](https://example.com/b)", text)

    def test_storage_markdown_no_run_metadata_wrapper(self) -> None:
        brief_text = "# Monitoring Brief - AI Infrastructure\n\nBody"
        rendered = StorageEngine._render_markdown_brief(
            run_id="20260318T000000Z_test",
            domain="AI Infrastructure",
            generated_at=datetime(2026, 3, 18, 0, 0, 0, tzinfo=UTC),
            brief_text=brief_text,
        )
        self.assertEqual(rendered, brief_text + "\n")
        self.assertNotIn("Run ID:", rendered)
        self.assertNotIn("Generated At (UTC):", rendered)

    def test_empty_brief_includes_diagnostics(self) -> None:
        generator = BriefingGenerator()
        text = generator.generate(
            domain="AI Infrastructure",
            signals=[],
            trends=[],
            watchlist=[],
            generated_at=datetime(2026, 3, 18, 1, 2, 3, tzinfo=UTC),
            diagnostics={
                "ingested_items": 120,
                "stale_dropped": 61,
                "dedup_duplicates": 9,
                "final_signals": 0,
            },
        )
        self.assertIn("本轮暂无与关注策略高度相关的更新。", text)
        self.assertNotIn("抓取条目 / Ingested items:", text)
        self.assertNotIn("去重丢弃 / Dedup discarded:", text)

    def test_generate_english_brief(self) -> None:
        generator = BriefingGenerator()
        signal = SimpleNamespace(
            source="system",
            event_type="new",
            title="Example Signal",
            freshness="fresh",
            summary="OpenAI released a new evaluation framework for agent reliability.",
            evidence=["Framework supports online and offline evaluation modes."],
            tags=["agent eval"],
            source_urls=["https://example.com/a"],
        )
        text = generator.generate(
            domain="AI Infrastructure",
            signals=[signal],  # type: ignore[arg-type]
            language="en",
            generated_at=datetime(2026, 3, 18, 1, 2, 3, tzinfo=UTC),
        )
        self.assertIn("# Monitoring Brief - AI Infrastructure", text)
        self.assertIn("## Section 2: System Signals", text)
        self.assertIn("**What Happened**", text)
        self.assertIn("**Why It Matters**", text)
        self.assertIn("**Follow-Up**", text)
        self.assertIn("**Sources** [Example](https://example.com/a)", text)


if __name__ == "__main__":
    unittest.main()
