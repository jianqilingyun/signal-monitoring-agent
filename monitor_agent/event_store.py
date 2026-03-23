from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from monitor_agent.core.models import Signal
from monitor_agent.core.storage import Storage

logger = logging.getLogger(__name__)


class EventTimelineEntry(BaseModel):
    timestamp: datetime
    summary: str
    source: str


class EventRecord(BaseModel):
    event_id: str
    entities: list[str] = Field(default_factory=list)
    first_seen: datetime
    last_updated: datetime
    timeline: list[EventTimelineEntry] = Field(default_factory=list)


class EventStore:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def load(self) -> dict[str, EventRecord]:
        payload = self.storage.load_events_store()
        records: dict[str, EventRecord] = {}
        for key, row in payload.items():
            try:
                record = EventRecord.model_validate(row)
            except Exception:
                continue
            records[key] = record
        return records

    def save(self, records: dict[str, EventRecord]) -> None:
        payload = {event_id: record.model_dump(mode="json") for event_id, record in records.items()}
        self.storage.save_events_store(payload)

    def create_event(self, signal: Signal) -> EventRecord:
        now = _signal_time(signal)
        event_id = f"evt_{uuid4().hex[:12]}"
        entry = EventTimelineEntry(
            timestamp=now,
            summary=signal.summary[:400],
            source=_timeline_source(signal),
        )
        return EventRecord(
            event_id=event_id,
            entities=_signal_entities(signal),
            first_seen=now,
            last_updated=now,
            timeline=[entry],
        )

    def ensure_event_for_signal(self, records: dict[str, EventRecord], signal: Signal) -> str:
        if signal.event_id and signal.event_id in records:
            return signal.event_id

        if signal.event_id and signal.event_id not in records:
            record = self.create_event(signal)
            record.event_id = signal.event_id
            records[record.event_id] = record
            return record.event_id

        legacy_event_id = f"evt_{signal.fingerprint[:12]}"
        if legacy_event_id not in records:
            record = self.create_event(signal)
            record.event_id = legacy_event_id
            records[legacy_event_id] = record
        return legacy_event_id

    def append_update(self, records: dict[str, EventRecord], event_id: str, signal: Signal) -> EventRecord:
        if event_id not in records:
            records[event_id] = self.create_event(signal)
            records[event_id].event_id = event_id

        record = records[event_id]
        record.last_updated = _signal_time(signal)
        record.entities = _dedupe(record.entities + _signal_entities(signal))[:15]
        record.timeline.append(
            EventTimelineEntry(
                timestamp=record.last_updated,
                summary=signal.summary[:400],
                source=_timeline_source(signal),
            )
        )
        if len(record.timeline) > 50:
            record.timeline = record.timeline[-50:]
        return record

    def upsert_signal(self, records: dict[str, EventRecord], signal: Signal) -> EventRecord:
        if signal.event_type == "new" or not signal.event_id:
            record = self.create_event(signal)
            records[record.event_id] = record
            signal.event_id = record.event_id
            return record

        return self.append_update(records, signal.event_id, signal)


def _signal_time(signal: Signal) -> datetime:
    dt = signal.publish_time or signal.published_at or signal.extracted_at
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _signal_entities(signal: Signal) -> list[str]:
    return _dedupe([tag for tag in signal.tags if tag.strip()])[:15]


def _timeline_source(signal: Signal) -> str:
    if signal.source_urls:
        return signal.source_urls[0]
    return signal.source


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        token = item.strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out
