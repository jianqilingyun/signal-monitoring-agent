from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from monitor_agent.notifier.telegram_renderer import TelegramRenderedMessage

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self) -> None:
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str, audio_path: str | None = None) -> None:
        if not self.is_configured:
            raise RuntimeError("Telegram credentials are not configured")

        message = TelegramRenderedMessage(text=text[:3900], parse_mode=None, disable_web_page_preview=False)
        self.send_messages([message], audio_path=audio_path)

    def send_message(
        self,
        *,
        text: str,
        chat_id: str | int | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        if not self.bot_token:
            raise RuntimeError("Telegram credentials are not configured")
        target_chat = str(chat_id or self.chat_id or "").strip()
        if not target_chat:
            raise RuntimeError("Telegram chat_id is not configured")

        base = f"https://api.telegram.org/bot{self.bot_token}"
        payload = {
            "chat_id": target_chat,
            "text": text[:3900],
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{base}/sendMessage", json=payload)
            response.raise_for_status()

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        if not self.bot_token:
            raise RuntimeError("Telegram credentials are not configured")
        if not commands:
            return

        base = f"https://api.telegram.org/bot{self.bot_token}"
        payload = {"commands": commands}
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{base}/setMyCommands", json=payload)
            response.raise_for_status()

    def clear_commands(self) -> None:
        if not self.bot_token:
            raise RuntimeError("Telegram credentials are not configured")

        base = f"https://api.telegram.org/bot{self.bot_token}"
        scopes: list[dict[str, str]] = [
            {"type": "default"},
            {"type": "all_private_chats"},
            {"type": "all_group_chats"},
            {"type": "all_chat_administrators"},
        ]
        with httpx.Client(timeout=30.0) as client:
            for scope in scopes:
                payload = {"scope": scope}
                response = client.post(f"{base}/deleteMyCommands", json=payload)
                response.raise_for_status()

    def send_messages(self, messages: list[TelegramRenderedMessage], audio_path: str | None = None) -> None:
        if not self.is_configured:
            raise RuntimeError("Telegram credentials are not configured")

        base = f"https://api.telegram.org/bot{self.bot_token}"
        text_url = f"{base}/sendMessage"

        with httpx.Client(timeout=30.0) as client:
            for message in messages:
                payload = {
                    "chat_id": self.chat_id,
                    "text": message.text[:3900],
                    "disable_web_page_preview": message.disable_web_page_preview,
                }
                if message.parse_mode:
                    payload["parse_mode"] = message.parse_mode
                res = client.post(text_url, json=payload)
                res.raise_for_status()

            if audio_path:
                audio_file = Path(audio_path)
                if audio_file.exists():
                    with audio_file.open("rb") as f:
                        files = {"audio": (audio_file.name, f, "audio/mpeg")}
                        form = {"chat_id": self.chat_id}
                        audio_res = client.post(f"{base}/sendAudio", data=form, files=files)
                        audio_res.raise_for_status()

        logger.info("Telegram notification sent")
