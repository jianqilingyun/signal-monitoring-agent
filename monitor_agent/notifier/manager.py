from __future__ import annotations

import logging
from typing import Any
from datetime import datetime

from monitor_agent.core.exceptions import NotificationError
from monitor_agent.core.models import BriefingConfig, LLMConfig, NotificationsConfig
from monitor_agent.notifier.dingtalk import DingTalkNotifier
from monitor_agent.notifier.dingtalk_renderer import DingTalkBriefRenderer
from monitor_agent.notifier.telegram import TelegramNotifier
from monitor_agent.notifier.telegram_renderer import TelegramBriefRenderer, TelegramRenderedMessage
from monitor_agent.notifier.telegram_rewriter import TelegramBriefRewriter

logger = logging.getLogger(__name__)


class NotificationManager:
    def __init__(
        self,
        config: NotificationsConfig,
        llm_config: LLMConfig | None = None,
        briefing_config: BriefingConfig | None = None,
    ) -> None:
        self.config = config
        self.briefing_config = briefing_config or BriefingConfig()
        self.telegram = TelegramNotifier()
        self.telegram_renderer = TelegramBriefRenderer()
        self.telegram_rewriter = TelegramBriefRewriter(llm_config)
        self.dingtalk = DingTalkNotifier()
        self.dingtalk_renderer = DingTalkBriefRenderer()

    def notify(
        self,
        brief_text: str,
        run_id: str,
        *,
        domain: str | None = None,
        generated_at: datetime | None = None,
        signal_cards: list[dict[str, Any]] | None = None,
        audio_path: str | None = None,
    ) -> tuple[bool, list[str]]:
        channels = list(self.config.channels or [])
        if not channels:
            return False, []
        sent = False
        errors: list[str] = []
        for channel in channels:
            try:
                if channel == "telegram":
                    messages = self._build_telegram_messages(
                        brief_text=brief_text,
                        domain=domain,
                        generated_at=generated_at,
                        signal_cards=signal_cards,
                        language=self.briefing_config.language,
                    )
                    self.telegram.send_messages(messages, audio_path=audio_path)
                    sent = True
                    continue
                if channel == "dingtalk":
                    title, body = self._build_dingtalk_message(
                        brief_text=brief_text,
                        domain=domain,
                        generated_at=generated_at,
                        signal_cards=signal_cards,
                        language=self.briefing_config.language,
                    )
                    self.dingtalk.send_markdown(title=title, text=body)
                    sent = True
                    continue
                errors.append(f"Unsupported notification channel: {channel}")
            except Exception as exc:
                mapping = {"telegram": "Telegram", "dingtalk": "DingTalk"}
                label = mapping.get(channel, channel)
                msg = f"{label} notification failed: {exc}"
                logger.exception(msg)
                errors.append(msg)
        if errors and not sent:
            raise NotificationError("; ".join(errors))
        return sent, errors

    def _build_telegram_messages(
        self,
        *,
        brief_text: str,
        domain: str | None,
        generated_at: datetime | None,
        signal_cards: list[dict[str, Any]] | None,
        language: str,
    ) -> list[TelegramRenderedMessage]:
        if domain and generated_at and signal_cards is not None:
            rewritten_cards = self.telegram_rewriter.rewrite_cards(domain=domain, cards=signal_cards, language=language)
            return self.telegram_renderer.render(
                domain=domain,
                generated_at=generated_at,
                signal_cards=rewritten_cards,
                language=language,
            )
        return [TelegramRenderedMessage(text=brief_text[:3900], parse_mode=None, disable_web_page_preview=False)]

    def _build_dingtalk_message(
        self,
        *,
        brief_text: str,
        domain: str | None,
        generated_at: datetime | None,
        signal_cards: list[dict[str, Any]] | None,
        language: str,
    ) -> tuple[str, str]:
        if domain and generated_at and signal_cards is not None:
            rewritten_cards = self.telegram_rewriter.rewrite_cards(domain=domain, cards=signal_cards, language=language)
            return self.dingtalk_renderer.render(
                domain=domain,
                generated_at=generated_at,
                signal_cards=rewritten_cards,
                language=language,
            )
        title = "Monitoring Brief"
        return title, brief_text[:3500]
