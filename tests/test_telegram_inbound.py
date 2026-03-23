from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from monitor_agent.core.models import ApiConfig
from monitor_agent.core.storage import Storage
from monitor_agent.inbox_engine import InboxEngine
from monitor_agent.inbound.telegram_service import TelegramInboundService


class _FakeNotifier:
    def __init__(self) -> None:
        self.bot_token = "bot-token"
        self.chat_id = "default-chat"
        self.messages: list[dict[str, object]] = []
        self.commands: list[dict[str, str]] = []
        self.cleared = 0

    def send_message(
        self,
        *,
        text: str,
        chat_id: str | int | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        self.messages.append(
            {
                "text": text,
                "chat_id": chat_id,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
            }
        )

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self.commands = commands

    def clear_commands(self) -> None:
        self.cleared += 1


class _AutoRunPipeline:
    def __init__(self) -> None:
        self.brief_user_signal_calls = 0

    def brief_user_signal(self, user_signal):
        self.brief_user_signal_calls += 1
        return {
            "brief_text": f"单篇简报：{getattr(user_signal, 'title', 'Untitled')}\n发生了什么\nfake\n为什么重要\nfake",
            "card": {},
            "errors": [],
        }


class TelegramInboundTests(unittest.TestCase):
    def test_url_message_is_ingested_into_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "123456"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            service = TelegramInboundService(
                storage=storage,
                inbox_engine=inbox,
                api_config=ApiConfig(
                    telegram_ingest_enabled=True,
                    auto_run_on_user_ingest=False,
                ),
                pipeline=None,
            )
            fake_notifier = _FakeNotifier()
            service.notifier = fake_notifier  # type: ignore[assignment]

            result = service._handle_message(  # pylint: disable=protected-access
                {
                    "chat": {"id": 123456},
                    "text": "Please track this:\nhttps://example.com/news/article-1?ref=share",
                }
            )

            self.assertEqual(result.tracked_count, 1)
            self.assertEqual(result.replied_count, 1)
            self.assertGreaterEqual(len(fake_notifier.messages), 1)
            ack = str(fake_notifier.messages[0]["text"])
            self.assertIn("已加入收件箱", ack)
            self.assertIn("链接数：1", ack)

            tracked = inbox.get_tracked_signals()
            self.assertEqual(len(tracked), 1)
            signal = tracked[0]
            self.assertEqual(signal.source, "user")
            self.assertTrue(signal.tracking_id.startswith("tg_"))
            self.assertEqual(signal.source_urls, ["https://example.com/news/article-1?ref=share"])
            self.assertIn("Please track this:", signal.user_context)

    def test_help_message_replies_without_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "123456"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            service = TelegramInboundService(
                storage=storage,
                inbox_engine=inbox,
                api_config=ApiConfig(
                    telegram_ingest_enabled=True,
                    auto_run_on_user_ingest=False,
                ),
                pipeline=None,
            )
            fake_notifier = _FakeNotifier()
            service.notifier = fake_notifier  # type: ignore[assignment]

            result = service._handle_message(  # pylint: disable=protected-access
                {
                    "chat": {"id": 123456},
                    "text": "/help",
                }
            )

            self.assertEqual(result.tracked_count, 0)
            self.assertEqual(result.replied_count, 1)
            self.assertEqual(len(inbox.get_tracked_signals()), 0)
            self.assertTrue(fake_notifier.messages)
            self.assertIn("把网页链接或相关文本直接发给我", str(fake_notifier.messages[0]["text"]))

    def test_brief_now_reply_is_concise(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "123456"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            pipeline = _AutoRunPipeline()
            service = TelegramInboundService(
                storage=storage,
                inbox_engine=inbox,
                api_config=ApiConfig(
                    telegram_ingest_enabled=True,
                    auto_run_on_user_ingest=True,
                ),
                pipeline=pipeline,  # type: ignore[arg-type]
            )
            fake_notifier = _FakeNotifier()
            service.notifier = fake_notifier  # type: ignore[assignment]

            result = service._handle_message(  # pylint: disable=protected-access
                {
                    "chat": {"id": 123456},
                    "text": "/brief Please track this:\nhttps://example.com/news/article-1?ref=share",
                }
            )

            self.assertEqual(result.tracked_count, 0)
            self.assertEqual(result.replied_count, 1)
            self.assertEqual(pipeline.brief_user_signal_calls, 1)
            self.assertEqual(len(fake_notifier.messages), 1)
            brief = str(fake_notifier.messages[0]["text"])
            self.assertIn("单篇简报", brief)
            self.assertNotIn("已加入收件箱", brief)
            self.assertNotIn("run_id", brief)
            self.assertNotIn("final_signals", brief)
            self.assertEqual(len(inbox.get_tracked_signals()), 0)

    def test_track_command_is_legacy_alias_for_save_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "123456"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            service = TelegramInboundService(
                storage=storage,
                inbox_engine=inbox,
                api_config=ApiConfig(
                    telegram_ingest_enabled=True,
                    auto_run_on_user_ingest=False,
                ),
                pipeline=None,
            )
            fake_notifier = _FakeNotifier()
            service.notifier = fake_notifier  # type: ignore[assignment]

            result = service._handle_message(  # pylint: disable=protected-access
                {
                    "chat": {"id": 123456},
                    "text": "/track https://example.com/news/article-1?ref=share",
                }
            )

            self.assertEqual(result.tracked_count, 1)
            self.assertEqual(result.replied_count, 1)
            self.assertIn("已加入收件箱", str(fake_notifier.messages[0]["text"]))
            signal = inbox.get_tracked_signals()[0]
            self.assertTrue(signal.source_urls)
            self.assertEqual(signal.source_urls[0], "https://example.com/news/article-1?ref=share")

    def test_start_registers_telegram_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "default-chat"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            service = TelegramInboundService(
                storage=storage,
                inbox_engine=inbox,
                api_config=ApiConfig(
                    telegram_ingest_enabled=True,
                    auto_run_on_user_ingest=False,
                ),
                pipeline=None,
            )
            fake_notifier = _FakeNotifier()
            service.notifier = fake_notifier  # type: ignore[assignment]

            with patch.object(service, "_fetch_updates", return_value=[]):
                service.start()
                service.shutdown()

            self.assertEqual(fake_notifier.cleared, 1)
            self.assertTrue(fake_notifier.commands)
            self.assertEqual([row["command"] for row in fake_notifier.commands], ["save", "brief", "help"])

    def test_offset_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "123456"}, clear=False):
            storage = Storage(tmpdir)
            storage.save_telegram_ingest_state({"last_update_id": 42})

            inbox = InboxEngine(storage)
            service = TelegramInboundService(
                storage=storage,
                inbox_engine=inbox,
                api_config=ApiConfig(
                    telegram_ingest_enabled=True,
                    auto_run_on_user_ingest=False,
                ),
                pipeline=None,
            )
            self.assertEqual(service._load_offset(), 42)  # pylint: disable=protected-access

    def test_unauthorized_chat_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"TELEGRAM_ALLOWED_CHAT_IDS": "123456"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            service = TelegramInboundService(
                storage=storage,
                inbox_engine=inbox,
                api_config=ApiConfig(
                    telegram_ingest_enabled=True,
                    auto_run_on_user_ingest=False,
                ),
                pipeline=None,
            )
            fake_notifier = _FakeNotifier()
            service.notifier = fake_notifier  # type: ignore[assignment]

            result = service._handle_message(  # pylint: disable=protected-access
                {
                    "chat": {"id": 999999},
                    "text": "https://example.com/news/article-1?ref=share",
                }
            )

            self.assertEqual(result.tracked_count, 0)
            self.assertEqual(result.replied_count, 0)
            self.assertFalse(fake_notifier.messages)
            self.assertEqual(len(inbox.get_tracked_signals()), 0)


if __name__ == "__main__":
    unittest.main()
