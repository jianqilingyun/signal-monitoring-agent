from __future__ import annotations

import unittest

from monitor_agent.notifier.telegram_rewriter import TelegramBriefRewriter


class TelegramBriefRewriterTests(unittest.TestCase):
    def test_missing_llm_client_falls_back_to_original_cards(self) -> None:
        rewriter = TelegramBriefRewriter(None)
        cards = [
            {
                "id": "sig-1",
                "title": "原始标题",
                "what": "原始发生了什么",
                "why": "原始为什么重要",
                "follow_up": ["跟踪点1"],
            }
        ]
        self.assertEqual(rewriter.rewrite_cards(domain="AI Infrastructure", cards=cards), cards)


if __name__ == "__main__":
    unittest.main()
