from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from monitor_agent.core.models import Signal, SignalPriority
from monitor_agent.core.storage import Storage
from monitor_agent.core.utils import jaccard_similarity, make_fingerprint, tokenize, utc_now
from monitor_agent.user_input_resolver import UserInputResolver

logger = logging.getLogger(__name__)


class UserSignalInput(BaseModel):
    title: str = Field(min_length=3)
    context: str = Field(min_length=3)
    ingest_mode: Literal["save_only", "brief_now"] = "save_only"
    tracking_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    user_interest: float = Field(default=1.0, ge=0.0, le=1.0)
    original_context: str | None = None
    resolved_title: str | None = None
    resolved_context: str | None = None
    resolution_method: str | None = None
    resolution_errors: list[str] = Field(default_factory=list)


class IngestRequest(BaseModel):
    run_system_ingestion: bool = True
    user_signals: list[UserSignalInput] = Field(default_factory=list)


class InboxEngine:
    """Tracks user-priority inbox signals and follow-up updates across runs."""

    def __init__(
        self,
        storage: Storage,
        match_threshold: float = 0.2,
        resolver: UserInputResolver | None = None,
    ) -> None:
        self.storage = storage
        self.match_threshold = max(0.0, min(match_threshold, 1.0))
        self.resolver = resolver

    def set_match_threshold(self, value: float) -> None:
        self.match_threshold = max(0.0, min(value, 1.0))

    def ingest_user_signals(self, inputs: list[UserSignalInput]) -> list[Signal]:
        if not inputs:
            return []

        inputs = self._resolve_inputs(inputs)
        existing = self.get_tracked_signals()
        by_tracking = {s.tracking_id: s for s in existing if s.tracking_id}
        now = utc_now()

        upserted: list[Signal] = []
        for item in inputs:
            tracking_id = (item.tracking_id or self._make_tracking_id()).strip()
            current = by_tracking.get(tracking_id)
            original_context = self._original_context(item)
            effective_context = self._effective_context(item)
            resolved_title = self._resolved_title(item)
            resolution_method = self._resolution_method(item)

            if current is not None:
                latest_updates = current.latest_updates[:]
                latest_updates.append(effective_context)
                current.latest_updates = _dedupe_preserve(latest_updates)[-8:]
                current.title = resolved_title or item.title.strip() or current.title
                current.summary = current.latest_updates[-1][:1000]
                current.user_context = current.user_context or original_context
                current.tags = _dedupe_preserve(current.tags + item.tags + item.entities)
                current.source_urls = _dedupe_preserve(current.source_urls + item.source_urls)
                current.system_interpretation = (
                    "Follow-up received; resolved content will be monitored for fresh updates."
                    if effective_context != original_context
                    else "Follow-up received; awaiting next system run for fresh updates."
                )
                current.priority = SignalPriority(
                    importance=max(current.importance, 0.95),
                    source_weight=2.0,
                    user_interest=max(current.priority.user_interest, item.user_interest),
                    novelty=current.novelty_score,
                    final_score=max(current.priority.final_score, 0.9),
                )
                current.extracted_at = now
                current.publish_time = now
                current.age_hours = 0.0
                current.freshness = "fresh"
                upserted.append(current)
            else:
                created = Signal(
                    title=resolved_title or item.title.strip(),
                    summary=effective_context[:1000],
                    importance=0.98,
                    category="inbox",
                    source_urls=_dedupe_preserve(item.source_urls),
                    evidence=["user_submission"] + ([f"resolved_via={resolution_method}"] if resolution_method else []),
                    tags=_dedupe_preserve(item.tags + item.entities),
                    published_at=now,
                    publish_time=now,
                    age_hours=0.0,
                    freshness="fresh",
                    extracted_at=now,
                    fingerprint=make_fingerprint("user", tracking_id, resolved_title or item.title),
                    novelty_score=1.0,
                    source="user",
                    tracking_id=tracking_id,
                    user_context=original_context,
                    latest_updates=[effective_context[:2000]],
                    system_interpretation=(
                        "User-priority signal added from resolved content. System will monitor for follow-up updates."
                        if effective_context != original_context
                        else "User-priority signal added. System will monitor for follow-up updates."
                    ),
                    briefed_once_at=None,
                    priority=SignalPriority(
                        importance=0.98,
                        source_weight=2.0,
                        user_interest=item.user_interest,
                        novelty=1.0,
                        final_score=1.0,
                    ),
                )
                by_tracking[tracking_id] = created
                upserted.append(created)

        self._save_signals(list(by_tracking.values()))
        logger.info("Inbox engine ingested %d user signal(s)", len(upserted))
        return upserted

    def resolve_user_signals(self, inputs: list[UserSignalInput]) -> list[UserSignalInput]:
        return self._resolve_inputs(inputs)

    def _resolve_inputs(self, inputs: list[UserSignalInput]) -> list[UserSignalInput]:
        if self.resolver is None:
            return inputs
        try:
            return self.resolver.resolve(inputs)
        except Exception as exc:
            logger.warning("User input resolution disabled for this batch due to error: %s", exc)
            return inputs

    def get_tracked_signals(self) -> list[Signal]:
        rows = self.storage.load_inbox_signals()
        tracked: list[Signal] = []
        for row in rows:
            try:
                tracked.append(Signal.model_validate(row))
            except Exception:
                continue
        return tracked

    def refresh_with_system_signals(self, system_signals: list[Signal]) -> list[Signal]:
        tracked = self.get_tracked_signals()
        if not tracked:
            return []

        for user_signal in tracked:
            if user_signal.briefed_once_at is not None:
                continue
            matches = self._find_matches(user_signal, system_signals, threshold=self.match_threshold)
            if matches:
                updates = [f"{sig.title}: {sig.summary[:200]}" for sig in matches[:3]]
                user_signal.latest_updates = _dedupe_preserve(user_signal.latest_updates + updates)[-10:]
                user_signal.system_interpretation = (
                    f"{len(matches)} related system update(s) detected in the latest run."
                )
                user_signal.novelty_score = max(user_signal.novelty_score * 0.6, 0.2)
            else:
                user_signal.system_interpretation = "No fresh matching system updates detected in the latest run."
                user_signal.novelty_score = max(user_signal.novelty_score * 0.9, 0.1)

            user_signal.source = "user"
            user_signal.importance = max(user_signal.importance, 0.95)
            user_signal.extracted_at = utc_now()
            user_signal.priority = SignalPriority(
                importance=user_signal.importance,
                source_weight=2.0,
                user_interest=max(user_signal.priority.user_interest, 0.9),
                novelty=user_signal.novelty_score,
                final_score=max(user_signal.priority.final_score, 0.9),
            )

        self._save_signals(tracked)
        return tracked

    def build_watchlist(self) -> list[dict[str, Any]]:
        tracked = self.get_tracked_signals()
        watchlist: list[dict[str, Any]] = []
        for signal in tracked:
            if not signal.tracking_id:
                continue
            if signal.briefed_once_at is not None:
                continue
            watchlist.append(
                {
                    "tracking_id": signal.tracking_id,
                    "title": signal.title,
                    "last_update": signal.latest_updates[-1] if signal.latest_updates else None,
                    "updates_count": len(signal.latest_updates),
                    "last_seen": signal.extracted_at.astimezone(UTC).isoformat(),
                }
            )
        return watchlist

    def _save_signals(self, signals: list[Signal]) -> None:
        self.storage.save_inbox_signals([s.model_dump(mode="json") for s in signals if s.tracking_id])

    def mark_briefed(self, signal_ids: list[str], briefed_at: datetime | None = None) -> int:
        if not signal_ids:
            return 0
        tracked = self.get_tracked_signals()
        by_id = {signal.id: signal for signal in tracked}
        now = briefed_at or utc_now()
        updated = 0
        for signal_id in signal_ids:
            signal = by_id.get(signal_id)
            if signal is None or signal.briefed_once_at is not None:
                continue
            signal.briefed_once_at = now
            updated += 1
        if updated:
            self._save_signals(list(by_id.values()))
        return updated

    @staticmethod
    def _original_context(item: UserSignalInput) -> str:
        token = (item.original_context or item.context or "").strip()
        return token

    @staticmethod
    def _effective_context(item: UserSignalInput) -> str:
        token = (item.resolved_context or item.context or "").strip()
        return token or item.context.strip()

    @staticmethod
    def _resolved_title(item: UserSignalInput) -> str:
        token = (item.resolved_title or item.title or "").strip()
        return token

    @staticmethod
    def _resolution_method(item: UserSignalInput) -> str:
        return (item.resolution_method or "").strip()

    @staticmethod
    def _find_matches(user_signal: Signal, system_signals: list[Signal], threshold: float) -> list[Signal]:
        user_text = " ".join(
            [
                user_signal.title,
                user_signal.user_context or "",
                " ".join(user_signal.tags),
                user_signal.summary,
            ]
        )
        user_tokens = tokenize(user_text)

        scored: list[tuple[float, Signal]] = []
        for signal in system_signals:
            if signal.source == "user":
                continue
            system_tokens = tokenize(f"{signal.title} {signal.summary} {' '.join(signal.tags)}")
            sim = jaccard_similarity(user_tokens, system_tokens)
            if sim >= threshold:
                scored.append((sim, signal))

        scored.sort(key=lambda row: row[0], reverse=True)
        return [signal for _, signal in scored]

    @staticmethod
    def _make_tracking_id() -> str:
        return f"trk_{uuid4().hex[:12]}"


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out
