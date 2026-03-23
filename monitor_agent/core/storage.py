from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from monitor_agent.core.exceptions import StorageError
from monitor_agent.core.models import RawItem, RunArtifacts, Signal, SourceCursorState
from monitor_agent.core.utils import utc_now

logger = logging.getLogger(__name__)


class Storage:
    def __init__(self, root_dir: str) -> None:
        self.root = Path(root_dir).expanduser().resolve()
        self.runs_dir = self.root / "runs"
        self.raw_dir = self.root / "raw"
        self.logs_dir = self.root / "logs"
        self.playwright_profile_dir = self.root / "playwright_profile"
        self.webhooks_dir = self.root / "webhooks"
        self.webhooks_subscriptions_path = self.webhooks_dir / "subscriptions.json"
        self.inbox_dir = self.root / "inbox"
        self.inbox_signals_path = self.inbox_dir / "signals.json"
        self.events_dir = self.root / "events"
        self.events_store_path = self.events_dir / "events.json"
        self.llm_dedup_cache_path = self.events_dir / "llm_dedup_cache.json"
        self.canonical_dir = self.root / "canonical"
        self.canonical_signals_path = self.canonical_dir / "signals.json"
        self.summaries_dir = self.root / "summaries"
        self.strategy_dir = self.root / "strategy"
        self.strategy_state_path = self.strategy_dir / "state.json"
        self.strategy_history_path = self.strategy_dir / "history.json"
        self.source_strategy_cache_path = self.strategy_dir / "source_strategy_cache.json"
        self.source_cursors_path = self.root / "source_cursors.json"
        self.telegram_ingest_state_path = self.root / "telegram_ingest_state.json"

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.playwright_profile_dir.mkdir(parents=True, exist_ok=True)
        self.webhooks_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.canonical_dir.mkdir(parents=True, exist_ok=True)
        self.summaries_dir.mkdir(parents=True, exist_ok=True)
        self.strategy_dir.mkdir(parents=True, exist_ok=True)
        if not self.webhooks_subscriptions_path.exists():
            self.webhooks_subscriptions_path.write_text("[]", encoding="utf-8")
        if not self.inbox_signals_path.exists():
            self.inbox_signals_path.write_text("[]", encoding="utf-8")
        if not self.events_store_path.exists():
            self.events_store_path.write_text("{}", encoding="utf-8")
        if not self.llm_dedup_cache_path.exists():
            self.llm_dedup_cache_path.write_text("{}", encoding="utf-8")
        if not self.canonical_signals_path.exists():
            self.canonical_signals_path.write_text("[]", encoding="utf-8")
        if not self.strategy_state_path.exists():
            self.strategy_state_path.write_text("{}", encoding="utf-8")
        if not self.strategy_history_path.exists():
            self.strategy_history_path.write_text("[]", encoding="utf-8")
        if not self.source_strategy_cache_path.exists():
            self.source_strategy_cache_path.write_text("{}", encoding="utf-8")
        if not self.source_cursors_path.exists():
            self.source_cursors_path.write_text("{}", encoding="utf-8")
        if not self.telegram_ingest_state_path.exists():
            self.telegram_ingest_state_path.write_text("{}", encoding="utf-8")

    def create_run_dir(self, run_id: str) -> Path:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def save_raw_items(self, run_id: str, items: list[RawItem]) -> Path:
        try:
            run_dir = self.create_run_dir(run_id)
            path = run_dir / "raw_items.json"
            serialized = [item.model_dump(mode="json") for item in items]
            path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")

            mirror = self.raw_dir / f"{run_id}.json"
            mirror.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
            return path
        except Exception as exc:
            raise StorageError(f"Failed to save raw items: {exc}") from exc

    def save_signals(self, run_id: str, signals: list[Signal]) -> Path:
        try:
            run_dir = self.create_run_dir(run_id)
            path = run_dir / "signals.json"
            payload = [signal.model_dump(mode="json") for signal in signals]
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return path
        except Exception as exc:
            raise StorageError(f"Failed to save signals: {exc}") from exc

    def save_brief_text(self, run_id: str, text: str) -> Path:
        try:
            run_dir = self.create_run_dir(run_id)
            path = run_dir / "brief.txt"
            path.write_text(text, encoding="utf-8")
            return path
        except Exception as exc:
            raise StorageError(f"Failed to save brief text: {exc}") from exc

    def save_brief_audio(self, run_id: str, audio_bytes: bytes) -> Path:
        try:
            run_dir = self.create_run_dir(run_id)
            path = run_dir / "brief.mp3"
            path.write_bytes(audio_bytes)
            return path
        except Exception as exc:
            raise StorageError(f"Failed to save brief audio: {exc}") from exc

    def save_manifest(self, run_id: str, manifest: RunArtifacts) -> Path:
        try:
            run_dir = self.create_run_dir(run_id)
            path = run_dir / "manifest.json"
            path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            latest = self.runs_dir / "latest.json"
            latest.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            return path
        except Exception as exc:
            raise StorageError(f"Failed to save manifest: {exc}") from exc

    def save_debug_bundle(
        self,
        run_id: str,
        selected_inputs: list[RawItem],
        extracted_signals: list[Signal],
        final_brief: str,
        source_incremental_stats: dict[str, Any] | None = None,
        source_health_stats: dict[str, Any] | None = None,
        source_advisories: list[dict[str, Any]] | None = None,
    ) -> Path:
        run_dir = self.create_run_dir(run_id)
        debug_dir = run_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

        selected_payload = [item.model_dump(mode="json") for item in selected_inputs]
        extracted_payload = [signal.model_dump(mode="json") for signal in extracted_signals]

        (debug_dir / "selected_inputs.json").write_text(
            json.dumps(selected_payload, indent=2),
            encoding="utf-8",
        )
        (debug_dir / "extracted_signals.json").write_text(
            json.dumps(extracted_payload, indent=2),
            encoding="utf-8",
        )
        (debug_dir / "final_brief.txt").write_text(final_brief, encoding="utf-8")
        if source_incremental_stats is not None:
            (debug_dir / "source_incremental_stats.json").write_text(
                json.dumps(source_incremental_stats, indent=2),
                encoding="utf-8",
            )
        if source_health_stats is not None:
            (debug_dir / "source_health_stats.json").write_text(
                json.dumps(source_health_stats, indent=2),
                encoding="utf-8",
            )
        if source_advisories is not None:
            (debug_dir / "source_advisories.json").write_text(
                json.dumps(source_advisories, indent=2),
                encoding="utf-8",
            )
        return debug_dir

    def append_daily_summary(self, run_started_at: datetime, summary_row: dict[str, Any]) -> Path:
        day = run_started_at.astimezone(UTC).strftime("%Y-%m-%d")
        path = self.summaries_dir / f"{day}.jsonl"

        run_id = str(summary_row.get("run_id", "")).strip()
        if path.exists() and run_id:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("run_id", "")).strip() == run_id:
                    return path

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(summary_row, ensure_ascii=False))
            handle.write("\n")
        return path

    def load_daily_summary(self, day: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        target_day = day or datetime.now(UTC).strftime("%Y-%m-%d")
        path = self.summaries_dir / f"{target_day}.jsonl"
        if not path.exists():
            return []

        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        if limit is not None and limit > 0:
            return rows[-limit:]
        return rows

    def load_latest_manifest(self) -> RunArtifacts | None:
        latest = self.runs_dir / "latest.json"
        if not latest.exists():
            return None
        try:
            return RunArtifacts.model_validate_json(latest.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.exception("Failed to parse latest manifest")
            raise StorageError(f"Failed to parse latest manifest: {exc}") from exc

    def load_latest_signals(self) -> list[dict[str, Any]]:
        manifest = self.load_latest_manifest()
        if manifest is None or not manifest.persistent_signals_path:
            return []
        path = Path(manifest.persistent_signals_path)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            rows = payload.get("signals")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    def load_latest_brief(self) -> str:
        manifest = self.load_latest_manifest()
        if manifest is None:
            return ""

        if manifest.persistent_brief_md_path:
            path = Path(manifest.persistent_brief_md_path)
            if path.exists():
                return path.read_text(encoding="utf-8")
        if manifest.brief_text_path:
            path = Path(manifest.brief_text_path)
            if path.exists():
                return path.read_text(encoding="utf-8")
        return ""

    def load_latest_source_advisories(self) -> list[dict[str, Any]]:
        manifest = self.load_latest_manifest()
        if manifest is None:
            return []
        debug_dir = self.runs_dir / manifest.run_id / "debug"
        path = debug_dir / "source_advisories.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def load_source_cursors(self) -> dict[str, SourceCursorState]:
        if not self.source_cursors_path.exists():
            return {}
        try:
            payload = json.loads(self.source_cursors_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed source cursor state: %s", self.source_cursors_path)
            return {}
        if not isinstance(payload, dict):
            return {}

        results: dict[str, SourceCursorState] = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            try:
                results[key] = SourceCursorState.model_validate(value)
            except Exception:
                continue
        return results

    def save_source_cursors(self, states: dict[str, SourceCursorState]) -> Path:
        try:
            payload = {
                key: state.model_dump(mode="json")
                for key, state in sorted(states.items())
                if isinstance(key, str)
            }
            self.source_cursors_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return self.source_cursors_path
        except Exception as exc:
            raise StorageError(f"Failed to save source cursors: {exc}") from exc

    def load_recent_signals(self, lookback_days: int = 30) -> list[Signal]:
        if lookback_days <= 0:
            return []

        min_dt = utc_now() - timedelta(days=lookback_days)
        results: list[Signal] = []
        for run_dir in sorted(self.runs_dir.glob("*"), reverse=True):
            if not run_dir.is_dir():
                continue
            signals_path = run_dir / "signals.json"
            if not signals_path.exists():
                continue
            try:
                payload = json.loads(signals_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed signals file: %s", signals_path)
                continue
            for row in payload:
                try:
                    signal = Signal.model_validate(row)
                except Exception:
                    continue
                if signal.extracted_at >= min_dt:
                    results.append(signal)
        return results

    def upsert_canonical_signals(self, signals: list[Signal]) -> int:
        if not signals:
            return 0

        rows = self._read_canonical_signal_rows()
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            signal_id = str(row.get("id", "")).strip()
            if signal_id:
                by_id[signal_id] = row

        changed = 0
        for signal in signals:
            payload = signal.model_dump(mode="json")
            signal_id = str(payload.get("id", "")).strip()
            if not signal_id:
                continue
            if by_id.get(signal_id) != payload:
                changed += 1
            by_id[signal_id] = payload

        ordered = sorted(
            by_id.values(),
            key=lambda row: str(row.get("extracted_at", "")),
        )
        self.canonical_signals_path.write_text(
            json.dumps(ordered, indent=2),
            encoding="utf-8",
        )
        return changed

    def load_canonical_signals(self, lookback_days: int = 30, include_user: bool = False) -> list[Signal]:
        if lookback_days <= 0:
            return []

        min_dt = utc_now() - timedelta(days=lookback_days)
        rows = self._read_canonical_signal_rows()
        out: list[Signal] = []
        for row in rows:
            try:
                signal = Signal.model_validate(row)
            except Exception:
                continue
            if not include_user and signal.source == "user":
                continue
            if signal.extracted_at >= min_dt:
                out.append(signal)
        return out

    def load_webhook_subscriptions(self) -> list[dict[str, Any]]:
        if not self.webhooks_subscriptions_path.exists():
            return []
        try:
            return json.loads(self.webhooks_subscriptions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed webhook subscriptions file; returning empty list")
            return []

    def save_webhook_subscriptions(self, subscriptions: list[dict[str, Any]]) -> None:
        self.webhooks_subscriptions_path.write_text(
            json.dumps(subscriptions, indent=2),
            encoding="utf-8",
        )

    def load_inbox_signals(self) -> list[dict[str, Any]]:
        if not self.inbox_signals_path.exists():
            return []
        try:
            return json.loads(self.inbox_signals_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed inbox signals file; returning empty list")
            return []

    def save_inbox_signals(self, signals: list[dict[str, Any]]) -> None:
        self.inbox_signals_path.write_text(
            json.dumps(signals, indent=2),
            encoding="utf-8",
        )

    def load_events_store(self) -> dict[str, Any]:
        if not self.events_store_path.exists():
            return {}
        try:
            payload = json.loads(self.events_store_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed event store file; returning empty map")
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def save_events_store(self, events: dict[str, Any]) -> None:
        self.events_store_path.write_text(
            json.dumps(events, indent=2),
            encoding="utf-8",
        )

    def load_llm_dedup_cache(self) -> dict[str, str]:
        if not self.llm_dedup_cache_path.exists():
            return {}
        try:
            payload = json.loads(self.llm_dedup_cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed LLM dedup cache file; returning empty map")
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(k): str(v) for k, v in payload.items()}

    def save_llm_dedup_cache(self, cache: dict[str, str]) -> None:
        self.llm_dedup_cache_path.write_text(
            json.dumps(cache, indent=2),
            encoding="utf-8",
        )

    def load_strategy_state(self) -> dict[str, Any]:
        if not self.strategy_state_path.exists():
            return {}
        try:
            payload = json.loads(self.strategy_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed strategy state file; returning empty map")
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def save_strategy_state(self, state: dict[str, Any]) -> None:
        self.strategy_state_path.write_text(
            json.dumps(state, indent=2),
            encoding="utf-8",
        )

    def load_strategy_history(self) -> list[dict[str, Any]]:
        if not self.strategy_history_path.exists():
            return []
        try:
            payload = json.loads(self.strategy_history_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed strategy history file; returning empty list")
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def save_strategy_history(self, history: list[dict[str, Any]]) -> None:
        self.strategy_history_path.write_text(
            json.dumps(history, indent=2),
            encoding="utf-8",
        )

    def append_strategy_history(self, entry: dict[str, Any]) -> None:
        history = self.load_strategy_history()
        history.append(entry)
        self.save_strategy_history(history)

    def load_source_strategy_cache(self) -> dict[str, Any]:
        if not self.source_strategy_cache_path.exists():
            return {}
        try:
            payload = json.loads(self.source_strategy_cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed source strategy cache; returning empty map")
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def save_source_strategy_cache(self, payload: dict[str, Any]) -> None:
        self.source_strategy_cache_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def load_telegram_ingest_state(self) -> dict[str, Any]:
        if not self.telegram_ingest_state_path.exists():
            return {}
        try:
            payload = json.loads(self.telegram_ingest_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed telegram ingest state; returning empty map")
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def save_telegram_ingest_state(self, state: dict[str, Any]) -> None:
        self.telegram_ingest_state_path.write_text(
            json.dumps(state, indent=2),
            encoding="utf-8",
        )

    def _read_canonical_signal_rows(self) -> list[dict[str, Any]]:
        if not self.canonical_signals_path.exists():
            return []
        try:
            payload = json.loads(self.canonical_signals_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed canonical signal history; returning empty list")
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]
