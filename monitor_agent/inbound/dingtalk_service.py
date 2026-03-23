from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from urllib.parse import quote_plus

import websockets
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage, Credential, DingTalkStreamClient

from monitor_agent.core.models import DingTalkConfig
from monitor_agent.core.storage import Storage
from monitor_agent.inbound.common import (
    derive_title,
    derive_tracking_id,
    extract_ingest_mode,
    extract_urls,
    help_text,
    parse_id_list,
)
from monitor_agent.inbox_engine import InboxEngine, UserSignalInput
from monitor_agent.core.pipeline import MonitoringPipeline

logger = logging.getLogger(__name__)


@dataclass
class DingTalkInboundResult:
    tracked_count: int
    replied_count: int


class DingTalkInboundService:
    def __init__(
        self,
        *,
        storage: Storage,
        inbox_engine: InboxEngine,
        dingtalk_config: DingTalkConfig,
        pipeline: MonitoringPipeline | None = None,
    ) -> None:
        self.storage = storage
        self.inbox_engine = inbox_engine
        self.dingtalk_config = dingtalk_config
        self.pipeline = pipeline
        self._app_key = self._read_env("DINGTALK_APP_KEY", "DINGTALK_CLIENT_ID", "DINGTALK_BOT_CLIENT_ID")
        self._app_secret = self._read_env(
            "DINGTALK_APP_SECRET",
            "DINGTALK_CLIENT_SECRET",
            "DINGTALK_BOT_CLIENT_SECRET",
        )
        self._client: DingTalkStreamClient | None = None
        self._handler: _DingTalkChatbotHandler | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._allowed_sender_ids = parse_id_list(os.getenv("DINGTALK_ALLOWED_SENDER_IDS", ""))
        self._allowed_conversation_ids = parse_id_list(os.getenv("DINGTALK_ALLOWED_CONVERSATION_IDS", ""))

    @property
    def enabled(self) -> bool:
        return bool(
            self.dingtalk_config.ingest_enabled
            and self._app_key
            and self._app_secret
            and (self._allowed_sender_ids or self._allowed_conversation_ids)
        )

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="dingtalk-inbound", daemon=True)
        self._thread.start()
        logger.info("DingTalk inbound listener started")

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._loop and self._client and getattr(self._client, "websocket", None) is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(self._client.websocket.close(), self._loop)
                future.result(timeout=5.0)
            except Exception as exc:  # pragma: no cover - best-effort shutdown
                logger.debug("DingTalk websocket close during shutdown failed: %s", exc)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("DingTalk inbound listener stopped")

    def _run_loop(self) -> None:
        asyncio.run(self._async_run_loop())

    async def _async_run_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ensure_client()
        assert self._client is not None

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                connection = self._client.open_connection()
                if not connection:
                    logger.error("DingTalk open connection failed")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                    continue

                endpoint = str(connection.get("endpoint") or "").strip()
                ticket = str(connection.get("ticket") or "").strip()
                if not endpoint or not ticket:
                    logger.error("DingTalk open connection returned invalid payload: %s", connection)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                    continue

                uri = f"{endpoint}?ticket={quote_plus(ticket)}"
                logger.info("DingTalk stream endpoint acquired")
                async with websockets.connect(uri) as websocket:
                    self._client.websocket = websocket
                    ping_task = asyncio.create_task(self._keepalive(websocket))
                    try:
                        async for raw_message in websocket:
                            if self._stop_event.is_set():
                                break
                            try:
                                json_message = json.loads(raw_message)
                            except json.JSONDecodeError:
                                logger.debug("Skipping malformed DingTalk stream payload")
                                continue
                            route_result = await self._client.route_message(json_message)
                            if route_result == DingTalkStreamClient.TAG_DISCONNECT:
                                break
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                backoff = 1.0
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                break
            except Exception as exc:
                logger.exception("DingTalk inbound loop failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _keepalive(self, websocket, ping_interval: int = 60) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(ping_interval)
            try:
                await websocket.ping()
            except websockets.exceptions.ConnectionClosed:
                break

    def _ensure_client(self) -> None:
        if self._client is not None and self._handler is not None:
            return
        assert self._app_key and self._app_secret
        self._client = DingTalkStreamClient(Credential(self._app_key, self._app_secret))
        self._handler = _DingTalkChatbotHandler(self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, self._handler)
        self._client.register_callback_handler(ChatbotMessage.DELEGATE_TOPIC, self._handler)

    def _handle_chatbot_message(self, incoming_message: ChatbotMessage, responder: _DingTalkChatbotHandler) -> DingTalkInboundResult:
        sender_staff_id = str(getattr(incoming_message, "sender_staff_id", "") or "").strip()
        conversation_id = str(getattr(incoming_message, "conversation_id", "") or "").strip()
        if not self._is_allowed_sender(sender_staff_id, conversation_id):
            logger.warning(
                "Ignoring DingTalk message from unauthorized sender=%s conversation=%s",
                sender_staff_id,
                conversation_id,
            )
            return DingTalkInboundResult(tracked_count=0, replied_count=0)
        text = self._extract_message_text(incoming_message)
        if not text:
            return DingTalkInboundResult(tracked_count=0, replied_count=0)

        normalized = text.strip()
        if normalized.startswith("/start") or normalized.startswith("/help"):
            responder.reply_text(help_text(), incoming_message)
            return DingTalkInboundResult(tracked_count=0, replied_count=1)

        ingest_mode, content = extract_ingest_mode(normalized)
        if not content.strip():
            responder.reply_text(help_text(), incoming_message)
            return DingTalkInboundResult(tracked_count=0, replied_count=1)

        urls = extract_urls(content)
        title = derive_title(content, urls, fallback_prefix="DingTalk shared signal")
        tracking_id = derive_tracking_id(urls, content, prefix="dt")

        user_signal = UserSignalInput(
            title=title,
            context=content,
            original_context=content,
            ingest_mode=ingest_mode,
            tracking_id=tracking_id,
            tags=["dingtalk", "inbound"],
            entities=[],
            source_urls=urls,
            user_interest=1.0,
        )
        resolved_inputs = self.inbox_engine.resolve_user_signals([user_signal])
        if ingest_mode == "brief_now":
            if self.pipeline is None:
                responder.reply_text("单篇简报暂不可用。", incoming_message)
                return DingTalkInboundResult(tracked_count=0, replied_count=1)
            try:
                brief = self.pipeline.brief_user_signal(resolved_inputs[0])
                brief_text = str(brief.get("brief_text") or "").strip()
                if brief_text:
                    brief_title = resolved_inputs[0].resolved_title or resolved_inputs[0].title or "单篇简报"
                    responder.reply_markdown(brief_title, brief_text, incoming_message)
                else:
                    responder.reply_text("单篇简报暂时没有可展示内容，已保留原始链接。", incoming_message)
            except RuntimeError as exc:
                logger.warning("DingTalk brief_now failed: %s", exc)
                responder.reply_text("单篇简报暂不可用，已保留原始链接。", incoming_message)
            return DingTalkInboundResult(tracked_count=0, replied_count=1)

        upserted = self.inbox_engine.ingest_user_signals(resolved_inputs)
        if not upserted:
            responder.reply_text("已收到，但这条消息未能写入收件箱。", incoming_message)
            return DingTalkInboundResult(tracked_count=0, replied_count=1)

        signal = upserted[0]
        ack = f"已加入收件箱。\n标题：{signal.title}"
        if urls:
            ack += f"\n链接数：{len(urls)}"
        responder.reply_text(ack, incoming_message)
        return DingTalkInboundResult(tracked_count=1, replied_count=1)

    def _is_allowed_sender(self, sender_staff_id: str, conversation_id: str) -> bool:
        sender_allowed = not self._allowed_sender_ids or sender_staff_id in self._allowed_sender_ids
        conversation_allowed = not self._allowed_conversation_ids or conversation_id in self._allowed_conversation_ids
        return sender_allowed and conversation_allowed

    @staticmethod
    def _extract_message_text(message: ChatbotMessage) -> str:
        texts = message.get_text_list() or []
        if texts:
            return "\n".join(str(item).strip() for item in texts if str(item).strip())
        if getattr(message, "text", None) is not None:
            content = getattr(message.text, "content", "")
            if isinstance(content, str) and content.strip():
                return content
        return ""

    @staticmethod
    def _read_env(*names: str) -> str:
        for name in names:
            value = os.getenv(name, "").strip()
            if value:
                return value
        return ""


class _DingTalkChatbotHandler(ChatbotHandler):
    def __init__(self, service: DingTalkInboundService) -> None:
        super().__init__()
        self.service = service

    async def process(self, message) -> tuple[int, str]:
        try:
            incoming_message = ChatbotMessage.from_dict(message.data or {})
            self.service._handle_chatbot_message(incoming_message, self)  # pylint: disable=protected-access
            return AckMessage.STATUS_OK, "ok"
        except Exception as exc:  # pragma: no cover - handled by stream runtime
            logger.exception("DingTalk callback handling failed: %s", exc)
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "callback handling failed"
