from __future__ import annotations

import logging
from collections import Counter

from monitor_agent.core.models import Signal, SignalPriority

logger = logging.getLogger(__name__)


class PriorityEngine:
    """Computes signal priority with user-first weighting and redundancy penalties."""

    def compute(self, signals: list[Signal], history: list[Signal]) -> list[Signal]:
        if not signals:
            return []

        duplicate_counts = Counter(self._dedupe_key(signal) for signal in signals)

        scored: list[Signal] = []
        for signal in signals:
            source_weight = 2.0 if signal.source == "user" else 1.0
            user_interest = signal.priority.user_interest if signal.priority else (1.0 if signal.source == "user" else 0.35)
            novelty = max(signal.novelty_score, signal.priority.novelty if signal.priority else 0.0)

            final = (
                signal.importance * 0.45
                + min(source_weight / 2.0, 1.0) * 0.25
                + user_interest * 0.20
                + novelty * 0.10
            )

            if signal.source == "user":
                final += 0.12

            if duplicate_counts[self._dedupe_key(signal)] > 1:
                final *= 0.88

            # Novelty penalty: less novel items lose score.
            final *= 0.82 + (novelty * 0.18)

            signal.priority = SignalPriority(
                importance=signal.importance,
                source_weight=source_weight,
                user_interest=user_interest,
                novelty=novelty,
                final_score=max(0.0, round(final, 4)),
            )
            scored.append(signal)

        deduped = self._deduplicate_keep_best(scored)
        ranked = sorted(
            deduped,
            key=lambda s: (
                s.priority.final_score if s.priority else 0.0,
                s.extracted_at,
            ),
            reverse=True,
        )
        logger.info("Priority engine scored %d signals (%d after dedupe)", len(signals), len(ranked))
        return ranked

    def _deduplicate_keep_best(self, signals: list[Signal]) -> list[Signal]:
        best_by_key: dict[str, Signal] = {}
        for signal in signals:
            key = self._dedupe_key(signal)
            current = best_by_key.get(key)
            if current is None:
                best_by_key[key] = signal
                continue

            current_score = current.priority.final_score if current.priority else 0.0
            candidate_score = signal.priority.final_score if signal.priority else 0.0

            if signal.source == "user" and current.source != "user":
                best_by_key[key] = signal
            elif candidate_score > current_score:
                best_by_key[key] = signal

        return list(best_by_key.values())

    @staticmethod
    def _dedupe_key(signal: Signal) -> str:
        if signal.tracking_id:
            return f"tracking:{signal.tracking_id}"
        if signal.event_id:
            return f"event:{signal.event_id}"
        return f"fingerprint:{signal.fingerprint}"
