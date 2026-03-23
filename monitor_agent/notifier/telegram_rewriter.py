from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI

from monitor_agent.core.models import LLMConfig

logger = logging.getLogger(__name__)
TELEGRAM_REWRITE_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_REWRITE_TIMEOUT_SECONDS", "20"))


class TelegramBriefRewriter:
    def __init__(self, llm_config: LLMConfig | None = None) -> None:
        self.llm_config = llm_config
        self.client: OpenAI | None = None
        self._cache: dict[str, dict[str, Any]] = {}

        if llm_config is None:
            return
        api_key = os.getenv("OPENAI_API_KEY")
        if llm_config.base_url and not api_key:
            api_key = "dummy"
        if api_key:
            self.client = OpenAI(api_key=api_key, base_url=llm_config.base_url)

    def rewrite_cards(self, *, domain: str, cards: list[dict[str, Any]], language: str = "zh") -> list[dict[str, Any]]:
        if not cards:
            return []
        if self.client is None or self.llm_config is None:
            return cards
        lang = "en" if str(language).strip().lower() == "en" else "zh"

        pending: list[dict[str, Any]] = []
        keys: list[str] = []
        results: dict[str, dict[str, Any]] = {}
        for card in cards:
            cache_key = self._cache_key(domain=domain, card=card, language=lang)
            cached = self._cache.get(cache_key)
            if cached is not None:
                results[str(card.get("id") or "")] = self._merge(card, cached)
                continue
            pending.append(card)
            keys.append(cache_key)

        if pending:
            rewritten = self._rewrite_batch(domain=domain, cards=pending, language=lang)
            for card, cache_key in zip(pending, keys):
                sid = str(card.get("id") or "")
                candidate = rewritten.get(sid)
                if candidate and self._is_valid(candidate):
                    self._cache[cache_key] = candidate
                    results[sid] = self._merge(card, candidate)
                else:
                    results[sid] = card

        out: list[dict[str, Any]] = []
        for card in cards:
            sid = str(card.get("id") or "")
            out.append(results.get(sid, card))
        if len(self._cache) > 128:
            self._cache = dict(list(self._cache.items())[-64:])
        return out

    def _rewrite_batch(self, *, domain: str, cards: list[dict[str, Any]], language: str) -> dict[str, dict[str, Any]]:
        instruction = (
            "Rewrite each item for a Telegram mobile briefing in Simplified Chinese. "
            "Do not add new facts. Keep the meaning faithful to the input. "
            "Make the title read like a concise news headline. "
            "Make 'what' 2-3 short sentences, under 140 Chinese characters when possible. "
            "Make 'why' 1-2 short sentences, under 90 Chinese characters when possible. "
            "Keep follow_up to max 2 items, each short and actionable. "
            "Return strict JSON only with key 'items'."
        )
        system_prompt = "You are an editor optimizing executive briefs for Telegram. Return strict JSON only."
        if language == "en":
            instruction = (
                "Rewrite each item for a Telegram mobile briefing in concise English. "
                "Do not add new facts. Keep the meaning faithful to the input. "
                "Make the title read like a short news headline. "
                "Make 'what' 2-3 short sentences, under 220 characters when possible. "
                "Make 'why' 1-2 short sentences, under 140 characters when possible. "
                "Keep follow_up to max 2 items, each short and actionable. "
                "Return strict JSON only with key 'items'."
            )
        payload = {
            "domain": domain,
            "items": [
                {
                    "id": str(card.get("id") or ""),
                    "title": str(card.get("title") or ""),
                    "what": str(card.get("what") or ""),
                    "why": str(card.get("why") or ""),
                    "follow_up": [str(v).strip() for v in card.get("follow_up", []) if str(v).strip()][:2],
                }
                for card in cards
            ],
            "instruction": instruction,
        }
        try:
            response = self.client.chat.completions.create(
                model=self.llm_config.model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                timeout=TELEGRAM_REWRITE_TIMEOUT_SECONDS,
            )
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
            rows = parsed.get("items", [])
            if not isinstance(rows, list):
                return {}
            out: dict[str, dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sid = str(row.get("id") or "").strip()
                if not sid:
                    continue
                out[sid] = {
                    "title": str(row.get("title") or "").strip(),
                    "what": str(row.get("what") or "").strip(),
                    "why": str(row.get("why") or "").strip(),
                    "follow_up": [str(v).strip() for v in row.get("follow_up", []) if str(v).strip()][:2],
                }
            return out
        except Exception as exc:
            logger.warning("Telegram rewrite failed, using original cards: %s", exc)
            return {}

    @staticmethod
    def _merge(original: dict[str, Any], rewritten: dict[str, Any]) -> dict[str, Any]:
        merged = dict(original)
        for key in ("title", "what", "why", "follow_up"):
            value = rewritten.get(key)
            if key == "follow_up":
                if isinstance(value, list) and value:
                    merged[key] = value[:2]
                continue
            token = str(value or "").strip()
            if token:
                merged[key] = token
        return merged

    @staticmethod
    def _is_valid(candidate: dict[str, Any]) -> bool:
        title = str(candidate.get("title") or "").strip()
        what = str(candidate.get("what") or "").strip()
        why = str(candidate.get("why") or "").strip()
        return bool(title and len(title) >= 6 and what and why)

    @staticmethod
    def _cache_key(*, domain: str, card: dict[str, Any], language: str) -> str:
        follow = "|".join(str(v).strip() for v in card.get("follow_up", []) if str(v).strip())
        return "||".join(
            [
                language,
                domain.strip(),
                str(card.get("id") or "").strip(),
                str(card.get("title") or "").strip(),
                str(card.get("what") or "").strip(),
                str(card.get("why") or "").strip(),
                follow,
            ]
        )
