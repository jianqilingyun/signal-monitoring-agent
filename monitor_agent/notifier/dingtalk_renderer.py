from __future__ import annotations

from datetime import datetime
from typing import Any


class DingTalkBriefRenderer:
    def __init__(self, max_signals: int = 3) -> None:
        self.max_signals = max(1, max_signals)

    def render(
        self,
        *,
        domain: str,
        generated_at: datetime,
        signal_cards: list[dict[str, Any]],
        language: str = "zh",
    ) -> tuple[str, str]:
        lang = "en" if str(language).strip().lower() == "en" else "zh"
        cards = [card for card in signal_cards if isinstance(card, dict)][: self.max_signals]
        local = generated_at.astimezone()
        title = f"{domain} Brief" if lang == "en" else f"{domain} 简报"
        lines = [
            f"### {title}",
            f"> {local.month}/{local.day}" if lang == "en" else f"> {local.month}月{local.day}日",
            "",
        ]
        if not cards:
            lines.append("No high-signal updates today." if lang == "en" else "今日暂无高相关更新。")
            return title, "\n".join(lines)

        lines.append(f"**Top items: {len(cards)}**" if lang == "en" else f"**今日重点：{len(cards)}条**")
        lines.append("")
        for idx, card in enumerate(cards, start=1):
            lines.extend(self._render_card(idx=idx, card=card, language=lang))
        return title, "\n".join(lines).strip()

    def _render_card(self, *, idx: int, card: dict[str, Any], language: str) -> list[str]:
        title = str(card.get("title") or "Untitled").strip()
        what = self._truncate(str(card.get("what") or "").strip(), 180)
        why = self._truncate(str(card.get("why") or "").strip(), 120)
        follow_up = [str(v).strip() for v in card.get("follow_up", []) if str(v).strip()][:2]
        source_links = card.get("source_links", [])

        lines = [f"#### {idx}. {title}", ""]
        if what:
            lines.append(f"- What happened: {what}" if language == "en" else f"- 发生了什么：{what}")
        if why:
            lines.append(f"- Why it matters: {why}" if language == "en" else f"- 为什么重要：{why}")
        for item in follow_up:
            lines.append(f"- Follow-up: {item}" if language == "en" else f"- 后续跟踪：{item}")
        if source_links:
            refs: list[str] = []
            for row in source_links[:2]:
                label = str(row.get("label") or "来源").strip()
                url = str(row.get("url") or "").strip()
                if label and url:
                    refs.append(f"[{label}]({url})")
            if refs:
                lines.append(f"- Sources: {' | '.join(refs)}" if language == "en" else f"- 来源：{' | '.join(refs)}")
        lines.append("")
        return lines

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        token = " ".join(text.split())
        if len(token) <= limit:
            return token
        return token[: limit - 1].rstrip("，,;；:：。 ") + "…"
