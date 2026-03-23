from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from monitor_agent.core.models import ApiConfig
from monitor_agent.core.storage import Storage
from monitor_agent.core.utils import make_fingerprint
from monitor_agent.inbound.common import parse_id_list
from monitor_agent.inbox_engine import IngestRequest, InboxEngine, UserSignalInput
from monitor_agent.notifier.telegram import TelegramNotifier
from monitor_agent.core.pipeline import MonitoringPipeline

logger = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+")


@dataclass
class TelegramInboundResult:
    tracked_count: int
    replied_count: int


class TelegramInboundService:
    def __init__(
        self,
        *,
        storage: Storage,
        inbox_engine: InboxEngine,
        api_config: ApiConfig,
        pipeline: MonitoringPipeline | None = None,
    ) -> None:
        self.storage = storage
        self.inbox_engine = inbox_engine
        self.api_config = api_config
        self.pipeline = pipeline
        self.notifier = TelegramNotifier()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._offset = self._load_offset()
        self._allowed_chat_ids = self._load_allowed_chat_ids()

    @property
    def enabled(self) -> bool:
        return bool(
            self.api_config.telegram_ingest_enabled
            and self.notifier.bot_token
            and self._allowed_chat_ids
        )

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._configure_commands()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="telegram-inbound", daemon=True)
        self._thread.start()
        logger.info("Telegram inbound listener started")

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("Telegram inbound listener stopped")

    def _run_loop(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                updates = self._fetch_updates()
                if updates:
                    self._handle_updates(updates)
                    backoff = 1.0
                else:
                    time.sleep(self.api_config.telegram_ingest_poll_interval_seconds)
            except Exception as exc:
                logger.exception("Telegram inbound poll failed: %s", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    def _fetch_updates(self) -> list[dict[str, Any]]:
        params = {
            "timeout": 20,
            "offset": self._offset,
            "allowed_updates": ["message", "edited_message"],
        }
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/getUpdates"
        with httpx.Client(timeout=25.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        result = payload.get("result", [])
        if not isinstance(result, list):
            return []
        return [row for row in result if isinstance(row, dict)]

    def _handle_updates(self, updates: list[dict[str, Any]]) -> None:
        latest_offset = self._offset
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                latest_offset = max(latest_offset, update_id + 1)

            message = self._extract_message(update)
            if message is None:
                continue
            try:
                self._handle_message(message)
            except Exception as exc:
                logger.exception("Failed to handle Telegram message: %s", exc)

        if latest_offset != self._offset:
            self._offset = latest_offset
            self._save_offset(latest_offset)

    def _handle_message(self, message: dict[str, Any]) -> TelegramInboundResult:
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        text = self._extract_message_text(message)

        if not chat_id or not text:
            return TelegramInboundResult(tracked_count=0, replied_count=0)
        if chat_id not in self._allowed_chat_ids:
            logger.warning("Ignoring Telegram message from unauthorized chat_id=%s", chat_id)
            return TelegramInboundResult(tracked_count=0, replied_count=0)

        normalized = text.strip()
        if normalized.startswith("/start") or normalized.startswith("/help"):
            self._reply(chat_id, self._help_text())
            return TelegramInboundResult(tracked_count=0, replied_count=1)

        ingest_mode, content = self._extract_ingest_mode(normalized)
        if not content.strip():
            self._reply(chat_id, self._help_text())
            return TelegramInboundResult(tracked_count=0, replied_count=1)
        urls = self._extract_urls(content)
        title = self._derive_title(content, urls)
        tracking_id = self._derive_tracking_id(urls, content)

        user_signal = UserSignalInput(
            title=title,
            context=content,
            original_context=content,
            ingest_mode=ingest_mode,
            tracking_id=tracking_id,
            tags=["telegram", "inbound"],
            entities=[],
            source_urls=urls,
            user_interest=1.0,
        )
        resolved_inputs = self.inbox_engine.resolve_user_signals([user_signal])
        if ingest_mode == "brief_now":
            if self.pipeline is None:
                self._reply(chat_id, "单篇简报暂不可用。")
                return TelegramInboundResult(tracked_count=0, replied_count=1)
            try:
                brief = self.pipeline.brief_user_signal(resolved_inputs[0])
                brief_text = str(brief.get("brief_text") or "").strip()
                if brief_text:
                    self._reply(chat_id, brief_text)
                else:
                    self._reply(chat_id, "单篇简报暂时没有可展示内容，已保留原始链接。")
            except RuntimeError as exc:
                logger.warning("Telegram brief_now failed: %s", exc)
                self._reply(chat_id, "单篇简报暂不可用，已保留原始链接。")
            return TelegramInboundResult(tracked_count=0, replied_count=1)

        upserted = self.inbox_engine.ingest_user_signals(resolved_inputs)
        if not upserted:
            self._reply(chat_id, "已收到，但这条消息未能写入收件箱。")
            return TelegramInboundResult(tracked_count=0, replied_count=1)

        signal = upserted[0]
        ack = f"已加入收件箱。\n标题：{signal.title}"
        if urls:
            ack += f"\n链接数：{len(urls)}"
        self._reply(chat_id, ack)
        return TelegramInboundResult(tracked_count=1, replied_count=1)

    def _reply(self, chat_id: str, text: str) -> None:
        try:
            self.notifier.send_message(text=text, chat_id=chat_id, disable_web_page_preview=True)
        except Exception as exc:
            logger.warning("Telegram reply failed: %s", exc)

    @staticmethod
    def _extract_message(update: dict[str, Any]) -> dict[str, Any] | None:
        message = update.get("message")
        if isinstance(message, dict):
            return message
        edited = update.get("edited_message")
        if isinstance(edited, dict):
            return edited
        return None

    @staticmethod
    def _extract_message_text(message: dict[str, Any]) -> str:
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text
        caption = message.get("caption")
        if isinstance(caption, str) and caption.strip():
            return caption
        return ""

    @staticmethod
    def _extract_urls(text: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for raw in _URL_RE.findall(text):
            url = raw.strip().rstrip(".,;，。；)")
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls

    @staticmethod
    def _derive_title(text: str, urls: list[str]) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        first = lines[0] if lines else ""
        if first and not _URL_RE.fullmatch(first):
            if len(first) > 60:
                return first[:57].rstrip() + "..."
            return first
        if urls:
            domain = urls[0].split("//", 1)[-1].split("/", 1)[0]
            domain = domain.replace("www.", "")
            return f"Telegram shared link - {domain}"
        return "Telegram inbound signal"

    @staticmethod
    def _derive_tracking_id(urls: list[str], text: str) -> str | None:
        if urls:
            return f"tg_{make_fingerprint(urls[0])}"
        cleaned = text.strip()
        if cleaned:
            return f"tg_{make_fingerprint(cleaned)[:12]}"
        return None

    @staticmethod
    def _help_text() -> str:
        return (
            "把网页链接或相关文本直接发给我，我会帮你加入监控。\n"
            "默认是保存到收件箱，等下一轮整理。\n"
            "快捷命令：/save 保存，/brief 立即单篇简报，/help 查看说明。"
        )

    @staticmethod
    def _extract_ingest_mode(text: str) -> tuple[str, str]:
        normalized = text.strip()
        if not normalized.startswith("/"):
            return "save_only", normalized

        lines = normalized.splitlines()
        first_line = lines[0].strip()
        remainder_lines = lines[1:]
        parts = first_line.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if remainder_lines:
            tail = "\n".join(line for line in remainder_lines if line.strip()).strip()
            if tail:
                rest = f"{rest}\n{tail}".strip() if rest else tail

        if command == "/brief":
            return "brief_now", rest
        if command == "/save":
            return "save_only", rest
        return "save_only", normalized

    def _configure_commands(self) -> None:
        try:
            self.notifier.clear_commands()
            self.notifier.set_commands(
                [
                    {"command": "save", "description": "保存到收件箱"},
                    {"command": "brief", "description": "立即生成单篇简报"},
                    {"command": "help", "description": "查看使用说明"},
                ]
            )
        except Exception as exc:
            logger.warning("Failed to configure Telegram commands: %s", exc)

    def _load_offset(self) -> int:
        state = self.storage.load_telegram_ingest_state()
        offset = state.get("last_update_id")
        if isinstance(offset, int) and offset >= 0:
            return offset
        if isinstance(offset, str) and offset.isdigit():
            return int(offset)
        return 0

    def _save_offset(self, offset: int) -> None:
        with self._state_lock:
            self.storage.save_telegram_ingest_state({"last_update_id": offset})

    def _load_allowed_chat_ids(self) -> set[str]:
        configured = parse_id_list(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
        default_chat = str(self.notifier.chat_id or "").strip()
        if default_chat:
            configured.add(default_chat)
        return configured
