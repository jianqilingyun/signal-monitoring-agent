from __future__ import annotations

import json
import logging
import os
from typing import Literal

from openai import OpenAI

from monitor_agent.core.models import LLMConfig, Signal
from monitor_agent.core.storage import Storage
from monitor_agent.core.utils import jaccard_similarity, tokenize

logger = logging.getLogger(__name__)

Relation = Literal["SAME_EVENT", "UPDATE", "DIFFERENT"]


class LLMDedupEngine:
    _VALID_RELATIONS = {"SAME_EVENT", "UPDATE", "DIFFERENT"}

    def __init__(self, llm_config: LLMConfig, storage: Storage) -> None:
        self.llm_config = llm_config
        self.storage = storage
        self._cache = storage.load_llm_dedup_cache()

        api_key = os.getenv("OPENAI_API_KEY")
        if llm_config.base_url and not api_key:
            api_key = "dummy"
        self.client = OpenAI(api_key=api_key, base_url=llm_config.base_url) if api_key else None

    def compare(
        self,
        new_signal: Signal,
        candidates: list[Signal],
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not candidates:
            return [], []

        results: dict[str, Relation] = {}
        unresolved: list[Signal] = []
        for candidate in candidates:
            cache_key = self._cache_key(new_signal, candidate)
            cached = self._cache.get(cache_key)
            if cached in self._VALID_RELATIONS:
                results[candidate.id] = cached  # type: ignore[assignment]
                continue
            unresolved.append(candidate)

        errors: list[str] = []
        if unresolved:
            if self.client is None:
                inferred = self._heuristic_compare(new_signal, unresolved)
            else:
                inferred, llm_errors = self._llm_compare(new_signal, unresolved)
                errors.extend(llm_errors)
                if llm_errors:
                    # On partial/failed parse, fallback for missing rows.
                    for row in self._heuristic_compare(new_signal, unresolved):
                        if row["candidate_id"] not in {r["candidate_id"] for r in inferred}:
                            inferred.append(row)

            for row in inferred:
                relation = row["relation"]
                candidate_id = row["candidate_id"]
                if relation not in self._VALID_RELATIONS:
                    relation = "DIFFERENT"
                results[candidate_id] = relation  # type: ignore[assignment]

            for candidate in unresolved:
                relation = results.get(candidate.id, "DIFFERENT")
                self._cache[self._cache_key(new_signal, candidate)] = relation

            self.storage.save_llm_dedup_cache(self._cache)

        ordered = [{"candidate_id": candidate.id, "relation": results.get(candidate.id, "DIFFERENT")} for candidate in candidates]
        return ordered, errors

    def _llm_compare(
        self,
        new_signal: Signal,
        candidates: list[Signal],
    ) -> tuple[list[dict[str, str]], list[str]]:
        payload = {
            "new_signal": _signal_payload(new_signal),
            "candidates": [_signal_payload(candidate) for candidate in candidates],
            "instruction": (
                "You are an event analyst.\n\n"
                "Determine whether the new signal refers to:\n"
                "1. The same underlying real-world event\n"
                "2. An update of a previous event\n"
                "3. A completely different event\n\n"
                "Do NOT rely on wording similarity.\n"
                "Focus on whether the real-world event is the same.\n\n"
                "Return JSON."
            ),
            "output_schema": {
                "results": [
                    {
                        "candidate_id": "string",
                        "relation": "SAME_EVENT | UPDATE | DIFFERENT",
                    }
                ]
            },
        }

        try:
            response = self.client.chat.completions.create(  # type: ignore[union-attr]
                model=self.llm_config.dedup_model or self.llm_config.model,
                temperature=self.llm_config.dedup_temperature,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": "You are an event analyst. Return strict JSON only.",
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
        except Exception as exc:
            msg = f"LLM dedup failed; fallback heuristics used: {exc}"
            logger.warning(msg)
            return [], [msg]

        rows: list[dict[str, str]] = []
        results = parsed.get("results", parsed if isinstance(parsed, list) else [])
        if isinstance(results, list):
            for row in results:
                if not isinstance(row, dict):
                    continue
                candidate_id = str(row.get("candidate_id", "")).strip()
                relation = str(row.get("relation", "")).strip().upper()
                if not candidate_id or relation not in self._VALID_RELATIONS:
                    continue
                rows.append({"candidate_id": candidate_id, "relation": relation})
        return rows, []

    def _heuristic_compare(self, new_signal: Signal, candidates: list[Signal]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        new_tokens = tokenize(f"{new_signal.title} {new_signal.summary} {' '.join(new_signal.tags)}")
        new_title_tokens = tokenize(new_signal.title)
        new_tag_tokens = {tag.strip().lower() for tag in new_signal.tags if tag.strip()}
        new_time = new_signal.publish_time or new_signal.published_at or new_signal.extracted_at

        for candidate in candidates:
            cand_tokens = tokenize(f"{candidate.title} {candidate.summary} {' '.join(candidate.tags)}")
            cand_title_tokens = tokenize(candidate.title)
            cand_tag_tokens = {tag.strip().lower() for tag in candidate.tags if tag.strip()}
            sim = jaccard_similarity(new_tokens, cand_tokens)
            title_sim = jaccard_similarity(new_title_tokens, cand_title_tokens)
            tag_sim = jaccard_similarity(new_tag_tokens, cand_tag_tokens)
            candidate_time = candidate.publish_time or candidate.published_at or candidate.extracted_at

            relation: Relation = "DIFFERENT"
            if sim >= 0.9 or (sim >= 0.82 and title_sim >= 0.9):
                relation = "SAME_EVENT"
            elif (
                new_time >= candidate_time
                and (sim >= 0.58 or (title_sim >= 0.66 and tag_sim >= 0.5))
            ):
                relation = "UPDATE"
            elif sim >= 0.82:
                relation = "SAME_EVENT"

            rows.append({"candidate_id": candidate.id, "relation": relation})
        return rows

    @staticmethod
    def _cache_key(new_signal: Signal, candidate: Signal) -> str:
        return f"{new_signal.fingerprint}::{candidate.fingerprint}"


def _signal_payload(signal: Signal) -> dict[str, str | float | list[str] | None]:
    publish_time = signal.publish_time or signal.published_at
    return {
        "id": signal.id,
        "title": signal.title,
        "summary": signal.summary,
        "tags": signal.tags,
        "publish_time": publish_time.isoformat() if publish_time else None,
        "importance": signal.importance,
        "source_urls": signal.source_urls[:3],
    }
