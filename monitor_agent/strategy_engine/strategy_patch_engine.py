from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from openai import OpenAI

from monitor_agent.strategy_engine.models import StrategyPatchInstruction

logger = logging.getLogger(__name__)


class StrategyPatchEngine:
    """Parse natural-language strategy modification requests into patch instructions."""

    def __init__(self, model: str = "gpt-5-mini", base_url: str | None = None) -> None:
        self.model = model
        api_key = os.getenv("OPENAI_API_KEY")
        if base_url and not api_key:
            api_key = "dummy"
        self.client = OpenAI(api_key=api_key, base_url=base_url) if api_key else None

    def parse(self, modification_request: str) -> tuple[StrategyPatchInstruction, list[str]]:
        errors: list[str] = []
        if self.client is None:
            msg = "OPENAI_API_KEY is missing; strategy patch parser using heuristic mode"
            logger.warning(msg)
            errors.append(msg)
            return self._heuristic_parse(modification_request), errors

        try:
            payload = self._llm_parse(modification_request)
            patch = StrategyPatchInstruction.model_validate(payload)
            return patch, errors
        except Exception as exc:
            msg = f"Strategy patch parser LLM failed; fallback in use: {exc}"
            logger.exception(msg)
            errors.append(msg)
            return self._heuristic_parse(modification_request), errors

    def _llm_parse(self, modification_request: str) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Convert the user's strategy modification request into JSON with keys: "
                        "operation(add|remove|update), target(focus_areas|entities|keywords), value(string). "
                        "For update operations, use value format 'old -> new'. Return JSON only."
                    ),
                },
                {"role": "user", "content": modification_request},
            ],
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    def _heuristic_parse(self, modification_request: str) -> StrategyPatchInstruction:
        raw = modification_request.strip()
        lowered = raw.lower()

        operation = _infer_operation(lowered)
        target = _infer_target(lowered)
        value = _infer_value(raw, operation)

        return StrategyPatchInstruction(operation=operation, target=target, value=value)


def _infer_operation(lowered: str) -> str:
    if any(token in lowered for token in ("remove", "delete", "drop", "exclude", "stop tracking")):
        return "remove"
    if any(token in lowered for token in ("update", "replace", "change", "rename", "swap")):
        return "update"
    return "add"


def _infer_target(lowered: str) -> str:
    if any(token in lowered for token in ("entity", "entities", "company", "org", "person", "ticker")):
        return "entities"
    if any(token in lowered for token in ("keyword", "keywords", "term", "terms", "tag", "tags")):
        return "keywords"
    if any(token in lowered for token in ("focus", "area", "areas", "topic", "topics", "theme")):
        return "focus_areas"
    return "focus_areas"


def _infer_value(raw: str, operation: str) -> str:
    quoted = re.findall(r'"([^"]+)"', raw)
    if quoted:
        if operation == "update" and len(quoted) >= 2:
            return f"{quoted[0].strip()} -> {quoted[1].strip()}"
        return quoted[0].strip()

    if operation == "update":
        match = re.search(r"from\s+(.+?)\s+to\s+(.+)", raw, flags=re.IGNORECASE)
        if match:
            return f"{match.group(1).strip()} -> {match.group(2).strip()}"

    cleaned = re.sub(
        r"\b(add|remove|delete|drop|exclude|update|replace|change|focus|areas?|entities?|keywords?|to|from|with)\b",
        " ",
        raw,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:-")
    return cleaned or raw.strip()
