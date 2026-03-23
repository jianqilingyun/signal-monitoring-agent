from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from monitor_agent.core.models import Signal

logger = logging.getLogger(__name__)


@dataclass
class PersistedOutputPaths:
    brief_md_path: Path
    brief_json_path: Path
    signals_json_path: Path
    audio_mp3_path: Path | None
    slot_name: str


class StorageEngine:
    """Persistent filesystem layer for brief/signal/audio outputs."""

    def __init__(self, base_path: str, timezone: str = "UTC") -> None:
        self.base_path = Path(base_path).expanduser().resolve()
        self.briefs_dir = self.base_path / "briefs"
        self.signals_dir = self.base_path / "signals"
        self.audio_dir = self.base_path / "audio"
        self.history_path = self.base_path / "history.json"

        self.briefs_dir.mkdir(parents=True, exist_ok=True)
        self.signals_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        if not self.history_path.exists():
            self.history_path.write_text("[]", encoding="utf-8")

        try:
            self.tz = ZoneInfo(timezone)
        except Exception:
            logger.warning("Invalid timezone '%s' for storage engine; falling back to UTC", timezone)
            self.tz = ZoneInfo("UTC")

    def save_outputs(
        self,
        run_id: str,
        domain: str,
        brief_text: str,
        signals: list[Signal],
        generated_at: datetime,
        brief_language: str = "zh",
        audio_bytes: bytes | None = None,
    ) -> PersistedOutputPaths:
        slot_name = self._slot_name(generated_at)
        stored_at = datetime.now(UTC)

        md_payload = self._render_markdown_brief(
            run_id=run_id,
            domain=domain,
            generated_at=generated_at,
            brief_text=brief_text,
        )
        brief_md_path = self._write_idempotent(self.briefs_dir / f"{slot_name}.md", md_payload.encode("utf-8"))

        signals_payload = {
            "run_id": run_id,
            "domain": domain,
            "slot": slot_name,
            "generated_at": generated_at.astimezone(UTC).isoformat(),
            "signals": [signal.model_dump(mode="json") for signal in signals],
        }
        signals_json_path = self._write_idempotent(
            self.signals_dir / f"{slot_name}.json",
            json.dumps(signals_payload, indent=2).encode("utf-8"),
        )

        structured_brief = {
            "run_id": run_id,
            "domain": domain,
            "slot": slot_name,
            "generated_at": generated_at.astimezone(UTC).isoformat(),
            "brief_language": brief_language,
            "brief_text": brief_text,
            "signal_count": len(signals),
            "signals_path": str(signals_json_path),
        }
        brief_json_path = self._write_idempotent(
            self.briefs_dir / f"{slot_name}.json",
            json.dumps(structured_brief, indent=2).encode("utf-8"),
        )

        audio_path: Path | None = None
        if audio_bytes:
            audio_path = self._write_idempotent(self.audio_dir / f"{slot_name}.mp3", audio_bytes)

        self._append_history(
            {
                "run_id": run_id,
                "domain": domain,
                "slot": slot_name,
                "generated_at": generated_at.astimezone(UTC).isoformat(),
                "brief_language": brief_language,
                "stored_at": stored_at.isoformat(),
                "brief_md_path": str(brief_md_path),
                "brief_json_path": str(brief_json_path),
                "signals_json_path": str(signals_json_path),
                "audio_mp3_path": str(audio_path) if audio_path else None,
            }
        )

        return PersistedOutputPaths(
            brief_md_path=brief_md_path,
            brief_json_path=brief_json_path,
            signals_json_path=signals_json_path,
            audio_mp3_path=audio_path,
            slot_name=slot_name,
        )

    def load_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self._read_history()
        rows = list(reversed(rows))
        if limit is not None and limit > 0:
            return rows[:limit]
        return rows

    def _slot_name(self, dt: datetime) -> str:
        local = dt.astimezone(self.tz)
        daypart = "morning" if local.hour < 15 else "evening"
        return f"{local.strftime('%Y-%m-%d')}-{daypart}"

    @staticmethod
    def _render_markdown_brief(
        run_id: str,
        domain: str,
        generated_at: datetime,
        brief_text: str,
    ) -> str:
        _ = run_id, domain, generated_at
        # Keep the markdown file focused on human-readable brief content.
        return f"{brief_text.strip()}\n"

    def _write_idempotent(self, target: Path, data: bytes) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            existing = target.read_bytes()
            if existing == data:
                return target

            stamp = datetime.now(UTC).strftime("%H%M%S")
            for attempt in range(0, 100):
                suffix = f"-{stamp}" if attempt == 0 else f"-{stamp}-{attempt}"
                candidate = target.with_name(f"{target.stem}{suffix}{target.suffix}")
                if candidate.exists():
                    if candidate.read_bytes() == data:
                        return candidate
                    continue
                try:
                    with candidate.open("xb") as f:
                        f.write(data)
                    return candidate
                except FileExistsError:
                    continue
            raise RuntimeError(f"Failed to find idempotent target for {target}")

        # First writer wins for canonical morning/evening file.
        with target.open("xb") as f:
            f.write(data)
        return target

    def _read_history(self) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed storage history file; returning empty list")
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _append_history(self, row: dict[str, Any]) -> None:
        history = self._read_history()
        dedupe_key = (
            str(row.get("run_id", "")),
            str(row.get("slot", "")),
            str(row.get("brief_md_path", "")),
        )
        for existing in history:
            existing_key = (
                str(existing.get("run_id", "")),
                str(existing.get("slot", "")),
                str(existing.get("brief_md_path", "")),
            )
            if existing_key == dedupe_key:
                return
        history.append(row)
        self.history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
