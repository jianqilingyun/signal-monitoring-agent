from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlparse

from monitor_agent.core.models import FilteringConfig, Signal
from monitor_agent.core.utils import jaccard_similarity, tokenize, utc_now

logger = logging.getLogger(__name__)


class FilterEngine:
    def __init__(self, config: FilteringConfig, trusted_domains: set[str] | None = None) -> None:
        self.config = config
        normalized: set[str] = set()
        for value in trusted_domains or set():
            host = self._normalize_host(value)
            if host:
                normalized.add(host)
        self.trusted_domains = normalized

    def apply(self, signals: list[Signal], history: list[Signal]) -> list[Signal]:
        if not signals:
            return []

        user_signals = [s for s in signals if s.source == "user"]
        system_signals = [s for s in signals if s.source != "user"]
        historical_event_ids = {s.event_id for s in history if s.event_id}

        # Never remove user signals; only de-redundant them.
        user_signals = self._dedupe_user(user_signals)

        thresholded_system = [s for s in system_signals if self._passes_system_threshold(s)]
        deduped_system = self._deduplicate_system(thresholded_system, history, protected=user_signals)
        scored_system = self._score_novelty(deduped_system, history)

        ranked_system = sorted(
            scored_system,
            key=lambda s: self._system_rank_key(s, historical_event_ids),
            reverse=True,
        )
        limited_system = ranked_system[: self.config.max_system_signals]

        # Respect global cap but always keep user signals.
        remaining = max(0, self.config.max_signals - len(user_signals))
        if remaining < len(limited_system):
            limited_system = limited_system[:remaining]

        combined = user_signals + limited_system
        logger.info(
            "Filter result: total=%d user=%d system=%d (from %d)",
            len(combined),
            len(user_signals),
            len(limited_system),
            len(signals),
        )
        return combined

    def _passes_system_threshold(self, signal: Signal) -> bool:
        if signal.event_type == "duplicate":
            return False
        if signal.freshness == "unknown":
            if signal.source == "user":
                return True
            if not self._is_trusted_source(signal):
                return False
            return signal.importance >= self.config.unknown_freshness_importance_threshold
        if signal.event_type == "update":
            return (
                signal.importance >= self.config.importance_threshold * 0.75
                or signal.novelty_score >= 0.15
                or (signal.priority.final_score if signal.priority else 0.0) >= 0.55
            )
        return signal.importance >= self.config.importance_threshold

    def _deduplicate_system(
        self,
        signals: list[Signal],
        history: list[Signal],
        protected: list[Signal],
    ) -> list[Signal]:
        cutoff = utc_now() - timedelta(days=self.config.dedup_window_days)

        historical_fingerprints = {
            s.fingerprint
            for s in history
            if s.extracted_at >= cutoff
        }
        historical_event_ids = {
            s.event_id
            for s in history
            if s.extracted_at >= cutoff and s.event_id
        }

        protected_keys = {s.fingerprint for s in protected}
        if protected:
            protected_keys.update(s.event_id for s in protected if s.event_id)

        seen = set(historical_fingerprints) | protected_keys
        unique_by_key: dict[str, Signal] = {}

        for signal in signals:
            if signal.event_type == "duplicate":
                continue

            key = signal.event_id or signal.fingerprint

            if signal.event_type != "update" and (
                signal.fingerprint in seen or (signal.event_id and signal.event_id in historical_event_ids)
            ):
                continue
            seen.add(key)

            current = unique_by_key.get(key)
            if current is None:
                unique_by_key[key] = signal
                continue
            if self._system_rank_key(signal, historical_event_ids) > self._system_rank_key(current, historical_event_ids):
                unique_by_key[key] = signal

        return list(unique_by_key.values())

    def _score_novelty(self, signals: list[Signal], history: list[Signal]) -> list[Signal]:
        cutoff = utc_now() - timedelta(days=self.config.novelty_window_days)
        candidate_history = [s for s in history if s.extracted_at >= cutoff]

        for signal in signals:
            current_tokens = tokenize(f"{signal.title} {signal.summary}")
            max_similarity = 0.0
            for prior in candidate_history:
                prior_tokens = tokenize(f"{prior.title} {prior.summary}")
                sim = jaccard_similarity(current_tokens, prior_tokens)
                if sim > max_similarity:
                    max_similarity = sim
            signal.novelty_score = max(0.0, min(1.0, 1.0 - max_similarity))

        return signals

    @staticmethod
    def _dedupe_user(signals: list[Signal]) -> list[Signal]:
        by_key: dict[str, Signal] = {}
        for signal in signals:
            key = signal.tracking_id or signal.fingerprint
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = signal
                continue
            if signal.extracted_at > existing.extracted_at:
                by_key[key] = signal
        return list(by_key.values())

    @staticmethod
    def _freshness_rank(signal: Signal) -> float:
        if signal.freshness == "fresh":
            return 1.0
        if signal.freshness == "recent":
            return 0.6
        if signal.freshness == "unknown":
            return 0.0
        return 0.1

    def _system_rank_key(self, signal: Signal, historical_event_ids: set[str]) -> tuple[float, float, float]:
        base_score = (
            signal.priority.final_score
            if signal.priority
            else (signal.importance * 0.7 + signal.novelty_score * 0.3)
        )
        freshness_score = self._freshness_rank(signal)
        update_boost = 0.22 if signal.event_type == "update" else 0.0
        tracked_boost = 0.16 if signal.event_id and signal.event_id in historical_event_ids else 0.0
        return (
            base_score + freshness_score + update_boost + tracked_boost,
            freshness_score,
            signal.extracted_at.timestamp(),
        )

    def _is_trusted_source(self, signal: Signal) -> bool:
        if not self.trusted_domains:
            return False
        for url in signal.source_urls:
            host = self._host_from_url(url)
            if not host:
                continue
            if host in self.trusted_domains:
                return True
            if any(host.endswith(f".{trusted}") for trusted in self.trusted_domains):
                return True
        return False

    @staticmethod
    def _host_from_url(url: str) -> str | None:
        if not url:
            return None
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return None
        return FilterEngine._normalize_host(host)

    @staticmethod
    def _normalize_host(host: str) -> str:
        normalized = host.strip().lower()
        if normalized.startswith("www."):
            return normalized[4:]
        return normalized
