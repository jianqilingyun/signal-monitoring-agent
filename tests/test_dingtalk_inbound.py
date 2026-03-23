from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from dingtalk_stream import ChatbotMessage

from monitor_agent.core.models import DingTalkConfig
from monitor_agent.core.storage import Storage
from monitor_agent.inbox_engine import InboxEngine
from monitor_agent.inbound.dingtalk_service import DingTalkInboundService


class _FakeResponder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, tuple[object, ...]]] = []

    def reply_text(self, text: str, incoming_message) -> None:
        self.messages.append(("text", (text, incoming_message.message_id)))

    def reply_markdown(self, title: str, text: str, incoming_message) -> None:
        self.messages.append(("markdown", (title, text, incoming_message.message_id)))


class _BriefPipeline:
    def __init__(self) -> None:
        self.calls = 0

    def brief_user_signal(self, user_signal):
        self.calls += 1
        return {
            "brief_text": f"单篇简报：{getattr(user_signal, 'title', 'Untitled')}\n发生了什么\nfake\n为什么重要\nfake",
            "card": {},
            "errors": [],
        }


class DingTalkInboundTests(unittest.TestCase):
    def test_url_message_is_ingested_into_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"DINGTALK_ALLOWED_SENDER_IDS": "staff-1"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            service = DingTalkInboundService(
                storage=storage,
                inbox_engine=inbox,
                dingtalk_config=DingTalkConfig(ingest_enabled=True),
                pipeline=None,
            )
            inbox.resolve_user_signals = lambda items: items  # type: ignore[assignment]
            responder = _FakeResponder()

            incoming = ChatbotMessage.from_dict(
                {
                    "msgtype": "text",
                    "text": {"content": "请跟踪这条：\nhttps://example.com/news/article-1?ref=share"},
                    "sessionWebhook": "https://example.com/webhook",
                    "senderStaffId": "staff-1",
                    "msgId": "msg-1",
                }
            )

            result = service._handle_chatbot_message(incoming, responder)  # pylint: disable=protected-access

            self.assertEqual(result.tracked_count, 1)
            self.assertEqual(result.replied_count, 1)
            self.assertTrue(responder.messages)
            self.assertEqual(responder.messages[0][0], "text")
            self.assertIn("已加入收件箱", responder.messages[0][1][0])
            self.assertIn("链接数：1", responder.messages[0][1][0])

            tracked = inbox.get_tracked_signals()
            self.assertEqual(len(tracked), 1)
            signal = tracked[0]
            self.assertEqual(signal.source, "user")
            self.assertTrue(signal.tracking_id.startswith("dt_"))
            self.assertEqual(signal.source_urls, ["https://example.com/news/article-1?ref=share"])
            self.assertIn("请跟踪这条", signal.user_context)

    def test_help_message_replies_without_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"DINGTALK_ALLOWED_SENDER_IDS": "staff-1"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            service = DingTalkInboundService(
                storage=storage,
                inbox_engine=inbox,
                dingtalk_config=DingTalkConfig(ingest_enabled=True),
                pipeline=None,
            )
            responder = _FakeResponder()

            incoming = ChatbotMessage.from_dict(
                {
                    "msgtype": "text",
                    "text": {"content": "/help"},
                    "sessionWebhook": "https://example.com/webhook",
                    "senderStaffId": "staff-1",
                    "msgId": "msg-2",
                }
            )

            result = service._handle_chatbot_message(incoming, responder)  # pylint: disable=protected-access

            self.assertEqual(result.tracked_count, 0)
            self.assertEqual(result.replied_count, 1)
            self.assertEqual(len(inbox.get_tracked_signals()), 0)
            self.assertTrue(responder.messages)
            self.assertIn("把网页链接或相关文本直接发给我", str(responder.messages[0][1][0]))

    def test_brief_now_reply_is_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"DINGTALK_ALLOWED_SENDER_IDS": "staff-1"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            pipeline = _BriefPipeline()
            service = DingTalkInboundService(
                storage=storage,
                inbox_engine=inbox,
                dingtalk_config=DingTalkConfig(ingest_enabled=True),
                pipeline=pipeline,  # type: ignore[arg-type]
            )
            inbox.resolve_user_signals = lambda items: items  # type: ignore[assignment]
            responder = _FakeResponder()

            incoming = ChatbotMessage.from_dict(
                {
                    "msgtype": "text",
                    "text": {"content": "/brief https://example.com/news/article-1?ref=share"},
                    "sessionWebhook": "https://example.com/webhook",
                    "senderStaffId": "staff-1",
                    "msgId": "msg-3",
                }
            )

            result = service._handle_chatbot_message(incoming, responder)  # pylint: disable=protected-access

            self.assertEqual(result.tracked_count, 0)
            self.assertEqual(result.replied_count, 1)
            self.assertEqual(pipeline.calls, 1)
            self.assertEqual(len(responder.messages), 1)
            self.assertEqual(responder.messages[0][0], "markdown")
            self.assertIn("单篇简报", str(responder.messages[0][1][1]))
            self.assertEqual(len(inbox.get_tracked_signals()), 0)

    def test_unauthorized_sender_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"DINGTALK_ALLOWED_SENDER_IDS": "staff-1"}, clear=False):
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            service = DingTalkInboundService(
                storage=storage,
                inbox_engine=inbox,
                dingtalk_config=DingTalkConfig(ingest_enabled=True),
                pipeline=None,
            )
            responder = _FakeResponder()

            incoming = ChatbotMessage.from_dict(
                {
                    "msgtype": "text",
                    "text": {"content": "https://example.com/news/article-1?ref=share"},
                    "sessionWebhook": "https://example.com/webhook",
                    "senderStaffId": "staff-2",
                    "msgId": "msg-4",
                }
            )

            result = service._handle_chatbot_message(incoming, responder)  # pylint: disable=protected-access

            self.assertEqual(result.tracked_count, 0)
            self.assertEqual(result.replied_count, 0)
            self.assertFalse(responder.messages)
            self.assertEqual(len(inbox.get_tracked_signals()), 0)


if __name__ == "__main__":
    unittest.main()
