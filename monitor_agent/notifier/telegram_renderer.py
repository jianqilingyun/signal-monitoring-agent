from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any


@dataclass
class TelegramRenderedMessage:
    text: str
    parse_mode: str | None = "HTML"
    disable_web_page_preview: bool = True


class TelegramBriefRenderer:
    def __init__(self, max_signals: int = 3) -> None:
        self.max_signals = max(1, max_signals)

    def render(
        self,
        *,
        domain: str,
        generated_at: datetime,
        signal_cards: list[dict[str, Any]],
        language: str = "zh",
    ) -> list[TelegramRenderedMessage]:
        lang = "en" if str(language).strip().lower() == "en" else "zh"
        cards = [card for card in signal_cards if isinstance(card, dict)][: self.max_signals]
        if not cards:
            return [
                TelegramRenderedMessage(
                    text=self._overview_text(domain=domain, generated_at=generated_at, signal_cards=[], language=lang),
                )
            ]

        messages = [
            TelegramRenderedMessage(
                text=self._overview_text(domain=domain, generated_at=generated_at, signal_cards=cards, language=lang),
            )
        ]
        for idx, card in enumerate(cards, start=1):
            messages.append(TelegramRenderedMessage(text=self._signal_text(idx=idx, card=card, language=lang)))
        return messages

    def _overview_text(self, *, domain: str, generated_at: datetime, signal_cards: list[dict[str, Any]], language: str) -> str:
        dt = generated_at.astimezone()
        if language == "en":
            lines = [f"<b>{escape(domain)} Brief</b>", f"{dt.strftime('%b')} {dt.day}"]
        else:
            lines = [f"<b>{escape(domain)} 简报</b>", f"{dt.month}月{dt.day}日"]
        if not signal_cards:
            lines.extend(["", "No high-signal updates today." if language == "en" else "今日暂无高相关更新。"])
            return "\n".join(lines)

        lines.extend(["", f"Top items: {len(signal_cards)}" if language == "en" else f"今日重点：{len(signal_cards)} 条", ""])
        for idx, card in enumerate(signal_cards, start=1):
            title = escape(str(card.get("title") or "Untitled"))
            lines.append(f"{idx}. {title}")
        return "\n".join(lines)

    def _signal_text(self, *, idx: int, card: dict[str, Any], language: str) -> str:
        title = escape(str(card.get("title") or "Untitled"))
        what = self._truncate(str(card.get("what") or "").strip(), 420)
        why = self._truncate(str(card.get("why") or "").strip(), 300)
        follow_up = [str(v).strip() for v in card.get("follow_up", []) if str(v).strip()][:2]
        source_links = card.get("source_links", [])

        lines = [f"<b>{idx}. {title}</b>"]
        if what:
            lines.extend(["", "<b>What Happened</b>" if language == "en" else "<b>发生了什么</b>", escape(what)])
        if why:
            lines.extend(["", "<b>Why It Matters</b>" if language == "en" else "<b>为什么重要</b>", escape(why)])
        if follow_up:
            lines.extend(["", "<b>Follow-Up</b>" if language == "en" else "<b>后续跟踪</b>"])
            for item in follow_up:
                lines.append(f"• {escape(item)}")
        if source_links:
            link_bits: list[str] = []
            for row in source_links[:2]:
                label = escape(str(row.get("label") or "来源"))
                url = escape(str(row.get("url") or "").strip(), quote=True)
                if not url:
                    continue
                link_bits.append(f'<a href="{url}">{label}</a>')
            if link_bits:
                lines.extend(["", f"Sources: {' | '.join(link_bits)}" if language == "en" else f"来源：{' | '.join(link_bits)}"])
        return "\n".join(lines)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        token = " ".join(text.split())
        if len(token) <= limit:
            return token
        return token[: limit - 1].rstrip("，,;；:：。 ") + "…"
