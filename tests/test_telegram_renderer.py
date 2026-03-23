from __future__ import annotations

import unittest
from datetime import UTC, datetime

from monitor_agent.notifier.telegram_renderer import TelegramBriefRenderer


class TelegramBriefRendererTests(unittest.TestCase):
    def test_render_outputs_overview_and_signal_messages(self) -> None:
        renderer = TelegramBriefRenderer()
        messages = renderer.render(
            domain="AI Infrastructure",
            generated_at=datetime(2026, 3, 19, 1, 11, tzinfo=UTC),
            signal_cards=[
                {
                    "title": "AWS推出 Strands Evals 框架",
                    "what": "AWS 发布新框架，用于系统化评估 AI 代理。",
                    "why": "这会降低企业部署 AI 代理的测试门槛。",
                    "follow_up": ["关注企业落地案例。"],
                    "source_links": [{"label": "AWS", "url": "https://example.com/aws"}],
                }
            ],
        )
        self.assertEqual(len(messages), 2)
        self.assertIn("<b>AI Infrastructure 简报</b>", messages[0].text)
        self.assertIn("今日重点：1 条", messages[0].text)
        self.assertIn("<b>1. AWS推出 Strands Evals 框架</b>", messages[1].text)
        self.assertIn("<b>发生了什么</b>", messages[1].text)
        self.assertIn('<a href="https://example.com/aws">AWS</a>', messages[1].text)
        self.assertTrue(messages[1].disable_web_page_preview)


if __name__ == "__main__":
    unittest.main()
