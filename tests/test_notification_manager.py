from __future__ import annotations

import unittest

from monitor_agent.core.exceptions import NotificationError
from monitor_agent.core.models import BriefingConfig, NotificationsConfig
from monitor_agent.notifier.manager import NotificationManager


class _TelegramOK:
    def __init__(self) -> None:
        self.calls = 0
        self.messages = None

    def send_messages(self, messages, audio_path: str | None = None) -> None:
        _ = audio_path
        self.calls += 1
        self.messages = messages


class _DingTalkOK:
    def __init__(self) -> None:
        self.calls = 0
        self.payload = None

    def send_markdown(self, *, title: str, text: str) -> None:
        self.calls += 1
        self.payload = {"title": title, "text": text}


class _TelegramFail:
    def send_messages(self, messages, audio_path: str | None = None) -> None:
        _ = messages, audio_path
        raise RuntimeError("telegram down")


class _RewriterOK:
    def rewrite_cards(self, *, domain, cards, language="zh"):
        _ = domain, language
        updated = [dict(card) for card in cards]
        updated[0]["title"] = "重写后的标题"
        return updated


class NotificationManagerTests(unittest.TestCase):
    def test_multi_channel_sends_all_selected_channels(self) -> None:
        manager = NotificationManager(
            NotificationsConfig(channels=["telegram", "dingtalk"])
        )
        manager.telegram = _TelegramOK()  # type: ignore[assignment]
        manager.dingtalk = _DingTalkOK()  # type: ignore[assignment]
        manager.telegram_rewriter = _RewriterOK()  # type: ignore[assignment]

        sent, errors = manager.notify(
            "brief",
            "run-multi",
            domain="AI Infrastructure",
            generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            signal_cards=[{"title": "A", "what": "B", "why": "C", "follow_up": [], "source_links": []}],
        )
        self.assertTrue(sent)
        self.assertEqual(errors, [])
        self.assertEqual(manager.telegram.calls, 1)
        self.assertEqual(manager.dingtalk.calls, 1)

    def test_multi_channel_keeps_successful_channels_when_one_fails(self) -> None:
        manager = NotificationManager(
            NotificationsConfig(channels=["telegram", "dingtalk"])
        )
        manager.telegram = _TelegramFail()  # type: ignore[assignment]
        manager.dingtalk = _DingTalkOK()  # type: ignore[assignment]

        sent, errors = manager.notify(
            "brief",
            "run-partial",
            domain="AI Infrastructure",
            generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            signal_cards=[{"title": "A", "what": "B", "why": "C", "follow_up": [], "source_links": []}],
        )
        self.assertTrue(sent)
        self.assertEqual(manager.dingtalk.calls, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Telegram notification failed", errors[0])

    def test_telegram_channel_only_sends_telegram(self) -> None:
        manager = NotificationManager(
            NotificationsConfig(channel="telegram", telegram={"enabled": True})
        )
        manager.telegram = _TelegramOK()  # type: ignore[assignment]
        manager.telegram_rewriter = _RewriterOK()  # type: ignore[assignment]

        sent, errors = manager.notify(
            "brief",
            "run-1",
            domain="AI Infrastructure",
            generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            signal_cards=[{"title": "A", "what": "B", "why": "C", "follow_up": [], "source_links": []}],
        )
        self.assertTrue(sent)
        self.assertEqual(errors, [])
        self.assertEqual(manager.telegram.calls, 1)
        self.assertEqual(len(manager.telegram.messages), 2)
        self.assertIn("重写后的标题", manager.telegram.messages[0].text)

    def test_telegram_failure_raises_when_no_other_channel_selected(self) -> None:
        manager = NotificationManager(
            NotificationsConfig(channel="telegram", telegram={"enabled": True})
        )
        manager.telegram = _TelegramFail()  # type: ignore[assignment]

        with self.assertRaises(NotificationError):
            manager.notify(
                "brief",
                "run-1",
                domain="AI Infrastructure",
                generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                signal_cards=[{"title": "A", "what": "B", "why": "C", "follow_up": [], "source_links": []}],
            )

    def test_dingtalk_channel_only_sends_dingtalk(self) -> None:
        manager = NotificationManager(
            NotificationsConfig(channel="dingtalk", dingtalk={"enabled": True})
        )
        manager.dingtalk = _DingTalkOK()  # type: ignore[assignment]
        manager.telegram_rewriter = _RewriterOK()  # type: ignore[assignment]

        sent, errors = manager.notify(
            "brief",
            "run-2",
            domain="AI Infrastructure",
            generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            signal_cards=[{"title": "A", "what": "B", "why": "C", "follow_up": [], "source_links": []}],
        )
        self.assertTrue(sent)
        self.assertEqual(errors, [])
        self.assertEqual(manager.dingtalk.calls, 1)
        self.assertIn("重写后的标题", manager.dingtalk.payload["text"])

    def test_telegram_channel_can_render_english(self) -> None:
        manager = NotificationManager(
            NotificationsConfig(channel="telegram", telegram={"enabled": True}),
            briefing_config=BriefingConfig(language="en"),
        )
        manager.telegram = _TelegramOK()  # type: ignore[assignment]

        sent, errors = manager.notify(
            "brief",
            "run-3",
            domain="AI Infrastructure",
            generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            signal_cards=[{"title": "Agent eval framework", "what": "OpenAI released a framework.", "why": "This could standardize evaluation.", "follow_up": ["Watch adoption by cloud vendors."], "source_links": []}],
        )
        self.assertTrue(sent)
        self.assertEqual(errors, [])
        self.assertEqual(manager.telegram.calls, 1)
        self.assertIn("Brief", manager.telegram.messages[0].text)
        self.assertIn("What Happened", manager.telegram.messages[1].text)


if __name__ == "__main__":
    unittest.main()
