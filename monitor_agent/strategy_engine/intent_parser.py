from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from openai import OpenAI

from monitor_agent.strategy_engine.models import ParsedIntent

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s)\]}>\"']+")
_ENTITY_RE = re.compile(r"\$?[A-Z][A-Za-z0-9&._-]{1,20}")


class IntentParser:
    """LLM-first intent parser with robust heuristics fallback."""

    def __init__(self, model: str = "gpt-5-mini", base_url: str | None = None) -> None:
        self.model = model
        api_key = os.getenv("OPENAI_API_KEY")
        if base_url and not api_key:
            api_key = "dummy"
        self.client = OpenAI(api_key=api_key, base_url=base_url) if api_key else None

    def parse(self, user_request: str) -> tuple[ParsedIntent, list[str]]:
        errors: list[str] = []

        if self.client is None:
            msg = "OPENAI_API_KEY is missing; intent parser using heuristic mode"
            logger.warning(msg)
            errors.append(msg)
            return self._heuristic_parse(user_request), errors

        try:
            payload = self._llm_parse(user_request)
            result = ParsedIntent.model_validate(payload)
            result.focus_areas = _dedupe(result.focus_areas)
            result.entities = _dedupe(result.entities)
            result.source_urls = _dedupe(result.source_urls)
            return result, errors
        except Exception as exc:
            msg = f"Intent parser LLM failed; fallback in use: {exc}"
            logger.exception(msg)
            errors.append(msg)
            return self._heuristic_parse(user_request), errors

    def _llm_parse(self, user_request: str) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract monitoring intent from user text and output JSON with keys: "
                        "domain, focus_areas(array), entities(array), source_urls(array), "
                        "intent_summary, rationale, confidence(0..1). Keep it domain-agnostic."
                    ),
                },
                {"role": "user", "content": user_request},
            ],
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    def _heuristic_parse(self, user_request: str) -> ParsedIntent:
        text = user_request.strip()
        lowered = text.lower()

        source_urls = _dedupe(_URL_RE.findall(text))
        entities = _dedupe(_extract_entities(text))
        focus_areas = _dedupe(_extract_focus_areas(text))

        domain = _guess_domain(lowered, focus_areas)
        summary = f"Monitor {domain} signals with emphasis on {', '.join(focus_areas[:3]) or 'priority events'}."

        return ParsedIntent(
            domain=domain,
            focus_areas=focus_areas,
            entities=entities,
            source_urls=source_urls,
            intent_summary=summary,
            rationale="Heuristic parser inferred domain and focus from keywords and named entities.",
            confidence=0.55,
        )


def _extract_entities(text: str) -> list[str]:
    candidates = _ENTITY_RE.findall(text)
    quoted = re.findall(r'"([^"]{2,40})"', text)
    candidates.extend(quoted)

    stop = {
        "Monitor",
        "Monitoring",
        "Need",
        "Build",
        "Focus",
        "Track",
        "And",
        "The",
    }
    cleaned = [c.strip(" .,;:()[]{}") for c in candidates]
    return [c for c in cleaned if c and c not in stop]


def _extract_focus_areas(text: str) -> list[str]:
    lowered = text.lower()
    areas: list[str] = []

    clauses = re.split(r"[.;\n]", lowered)
    for clause in clauses:
        if any(token in clause for token in ["focus", "track", "watch", "monitor", "about", "cover"]):
            area = re.sub(r"\b(focus|track|watch|monitor|about|cover|on|for|the|and)\b", " ", clause)
            area = re.sub(r"\s+", " ", area).strip(" ,")
            if area:
                areas.extend([p.strip() for p in re.split(r",|/|\band\b", area) if p.strip()])

    if not areas:
        words = [w for w in re.split(r"[^a-z0-9]+", lowered) if len(w) > 4]
        areas = words[:5]

    return [a[:60] for a in areas if len(a) >= 3]


def _guess_domain(lowered_text: str, focus_areas: list[str]) -> str:
    text = f"{lowered_text} {' '.join(focus_areas)}"
    if any(k in text for k in ["security", "vulnerability", "threat", "cve", "breach"]):
        return "Cybersecurity"
    if any(k in text for k in ["stock", "market", "earnings", "fund", "revenue", "finance"]):
        return "Finance"
    if any(k in text for k in ["drug", "clinical", "hospital", "patient", "health"]):
        return "Healthcare"
    if any(k in text for k in ["factory", "logistics", "shipping", "procurement", "supply"]):
        return "Supply Chain"
    if any(k in text for k in ["policy", "regulation", "law", "government", "compliance"]):
        return "Policy"
    if any(k in text for k in ["ai", "software", "developer", "cloud", "platform", "technology"]):
        return "Technology"
    return "General"


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for item in items:
        norm = item.strip()
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(norm)
    return result
