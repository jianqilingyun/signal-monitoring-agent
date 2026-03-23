from __future__ import annotations

import json
import logging
import os

from openai import OpenAI

from monitor_agent.strategy_engine.models import DomainMapping, ParsedIntent

logger = logging.getLogger(__name__)


class StrategyBuilder:
    """Build a clear, explainable human-readable monitoring strategy."""

    def __init__(self, model: str = "gpt-5-mini", base_url: str | None = None) -> None:
        self.model = model
        api_key = os.getenv("OPENAI_API_KEY")
        if base_url and not api_key:
            api_key = "dummy"
        self.client = OpenAI(api_key=api_key, base_url=base_url) if api_key else None

    def build(
        self,
        user_request: str,
        parsed_intent: ParsedIntent,
        domain_mapping: DomainMapping,
        config_object: dict,
    ) -> tuple[str, list[str]]:
        errors: list[str] = []

        if self.client is None:
            return self._deterministic_strategy(parsed_intent, domain_mapping, config_object), errors

        try:
            strategy = self._llm_strategy(user_request, parsed_intent, domain_mapping, config_object)
            if strategy.strip():
                return strategy.strip(), errors
        except Exception as exc:
            msg = f"Strategy builder LLM failed; deterministic strategy used: {exc}"
            logger.exception(msg)
            errors.append(msg)

        return self._deterministic_strategy(parsed_intent, domain_mapping, config_object), errors

    def _llm_strategy(
        self,
        user_request: str,
        parsed_intent: ParsedIntent,
        domain_mapping: DomainMapping,
        config_object: dict,
    ) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write a concise, structured monitoring strategy with sections: "
                        "Objective, Scope, Signal Priorities, Source Plan, Quality Controls, "
                        "Operational Cadence, and Why This Is Explainable."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_request": user_request,
                            "parsed_intent": parsed_intent.model_dump(mode="json"),
                            "domain_mapping": domain_mapping.model_dump(mode="json"),
                            "generated_config": config_object,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        return response.choices[0].message.content or ""

    def _deterministic_strategy(
        self,
        parsed_intent: ParsedIntent,
        domain_mapping: DomainMapping,
        config_object: dict,
    ) -> str:
        focus = ", ".join(parsed_intent.focus_areas[:5]) or "general high-impact events"
        entities = ", ".join(parsed_intent.entities[:8]) or "none explicitly provided"
        queries = ", ".join(domain_mapping.source_queries[:6]) or "domain-level updates"
        times = ", ".join(config_object.get("schedule", {}).get("times", []))

        return (
            f"Objective\n"
            f"Track actionable signals for {domain_mapping.canonical_domain} with high relevance and low noise.\n\n"
            f"Scope\n"
            f"Topic: {domain_mapping.canonical_domain}.\n"
            f"Optional refinements: focus areas {focus}; entities {entities}.\n\n"
            f"Signal Priorities\n"
            f"Prioritize incidents, launches, regulatory changes, partnerships, and financial/operational shifts impacting the monitored domain.\n\n"
            f"Source Plan\n"
            f"Use baseline domain feeds and query-driven RSS coverage from: {queries}.\n"
            f"Use browser scraping for user-specified URLs or dashboards when provided.\n\n"
            f"Quality Controls\n"
            f"Apply importance threshold, fingerprint-based deduplication, and novelty scoring against recent history.\n\n"
            f"Operational Cadence\n"
            f"Run at {times} ({config_object.get('schedule', {}).get('timezone', 'UTC')}).\n"
            f"Deliver text brief, JSON signals, MP3 brief, then notify through the selected delivery channels.\n\n"
            f"Why This Is Explainable\n"
            f"Every output ties back to the topic, source evidence URLs, and deterministic filter rules."
        )
