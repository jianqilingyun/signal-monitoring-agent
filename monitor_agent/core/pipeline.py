from __future__ import annotations

import logging
import threading
from datetime import UTC
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any

from monitor_agent.briefing.generator import BriefingGenerator
from monitor_agent.briefing.localizer import BriefingLocalizer
from monitor_agent.candidate_retrieval import CandidateMatch, CandidateRetrievalEngine, VectorIndex
from monitor_agent.core.models import MonitorConfig, RawItem, RunArtifacts, Signal
from monitor_agent.core.storage import Storage
from monitor_agent.core.utils import utc_now
from monitor_agent.core.webhooks import WebhookManager
from monitor_agent.event_store import EventRecord, EventStore
from monitor_agent.filter_engine.engine import FilterEngine
from monitor_agent.inbox_engine import InboxEngine, UserSignalInput
from monitor_agent.ingestion_layer.manager import IngestionManager
from monitor_agent.llm_dedup_engine import LLMDedupEngine
from monitor_agent.notifier.manager import NotificationManager
from monitor_agent.priority_engine import PriorityEngine
from monitor_agent.signal_engine.extractor import LLMSignalExtractor
from monitor_agent.storage_engine import StorageEngine
from monitor_agent.time_engine import TimeEngine
from monitor_agent.tts.manager import TTSManager
from monitor_agent.strategy_engine.source_strategy_engine import SourceStrategyEngine

logger = logging.getLogger(__name__)


@dataclass
class IngestOnlyResult:
    run_id: str
    items: list[RawItem]
    errors: list[str]


@dataclass
class DedupStats:
    duplicates_discarded: int = 0
    updates_kept: int = 0


class MonitoringPipeline:
    def __init__(
        self,
        config: MonitorConfig,
        storage: Storage,
        webhook_manager: WebhookManager | None = None,
        inbox_engine: InboxEngine | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.webhook_manager = webhook_manager

        self.ingestion_manager = IngestionManager(
            config=config,
            playwright_profile_dir=str(storage.playwright_profile_dir),
            storage=storage,
        )
        self.extractor = LLMSignalExtractor(config.llm)
        self.filter_engine = FilterEngine(config.filtering, trusted_domains=self._trusted_source_domains(config))
        self.briefing_generator = BriefingGenerator(localizer=BriefingLocalizer(config.llm))
        self.tts_manager = TTSManager(config.tts)
        self.notification_manager = NotificationManager(
            config.notifications,
            llm_config=config.llm,
            briefing_config=config.briefing,
        )
        self.inbox_engine = inbox_engine or InboxEngine(
            storage,
            match_threshold=config.filtering.inbox_match_threshold,
        )
        self.inbox_engine.set_match_threshold(config.filtering.inbox_match_threshold)
        self.priority_engine = PriorityEngine()
        self.time_engine = TimeEngine()
        self.candidate_retrieval = CandidateRetrievalEngine(config.llm, config.filtering)
        self.llm_dedup_engine = LLMDedupEngine(config.llm, storage)
        self.event_store = EventStore(storage)
        self.storage_engine = StorageEngine(
            base_path=config.storage.persistent_base_path,
            timezone=config.schedule.timezone,
        )
        self.source_strategy_engine = SourceStrategyEngine(
            storage=storage,
            llm_config=config.llm,
            use_llm=True,
        )

        self._lock = threading.Lock()

    def update_config(self, config: MonitorConfig) -> None:
        self.config = config
        self.ingestion_manager = IngestionManager(
            config=config,
            playwright_profile_dir=str(self.storage.playwright_profile_dir),
            storage=self.storage,
        )
        self.extractor = LLMSignalExtractor(config.llm)
        self.filter_engine = FilterEngine(config.filtering, trusted_domains=self._trusted_source_domains(config))
        self.briefing_generator = BriefingGenerator(localizer=BriefingLocalizer(config.llm))
        self.tts_manager = TTSManager(config.tts)
        self.notification_manager = NotificationManager(
            config.notifications,
            llm_config=config.llm,
            briefing_config=config.briefing,
        )
        self.inbox_engine.set_match_threshold(config.filtering.inbox_match_threshold)
        self.candidate_retrieval = CandidateRetrievalEngine(config.llm, config.filtering)
        self.llm_dedup_engine = LLMDedupEngine(config.llm, self.storage)
        self.storage_engine = StorageEngine(
            base_path=config.storage.persistent_base_path,
            timezone=config.schedule.timezone,
        )
        self.source_strategy_engine = SourceStrategyEngine(
            storage=self.storage,
            llm_config=config.llm,
            use_llm=True,
        )

    def run_once(self, trigger: str = "manual") -> RunArtifacts:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A monitoring run is already in progress")

        run_id = self._make_run_id(trigger)
        started_at = utc_now()
        domain_label = self._domain_label()
        manifest = RunArtifacts(run_id=run_id, started_at=started_at, domain=domain_label)
        run_metrics: dict[str, int] = {
            "ingested_items": 0,
            "incremental_candidates": 0,
            "incremental_dropped": 0,
            "incremental_overlap_kept": 0,
            "cursor_advanced_sources": 0,
            "sources_skipped_refresh": 0,
            "stale_dropped": 0,
            "unknown_dropped": 0,
            "dedup_duplicates": 0,
            "updates_kept": 0,
            "final_signals": 0,
        }
        manifest.run_metrics = run_metrics

        try:
            logger.info("Starting monitoring run %s (trigger=%s)", run_id, trigger)
            self._maybe_refresh_source_strategies(manifest)

            raw_items, ingest_errors = self.ingestion_manager.ingest_all()
            manifest.errors.extend(ingest_errors)
            manifest.raw_items_count = len(raw_items)
            run_metrics["ingested_items"] = len(raw_items)
            incremental_stats = getattr(self.ingestion_manager, "last_incremental_stats", {})
            run_metrics["incremental_candidates"] = sum(
                int(row.get("candidate_count", 0)) for row in incremental_stats.values()
            )
            run_metrics["incremental_dropped"] = sum(
                int(row.get("dropped_count", 0)) for row in incremental_stats.values()
            )
            run_metrics["incremental_overlap_kept"] = sum(
                int(row.get("overlap_kept", 0)) for row in incremental_stats.values()
            )
            run_metrics["cursor_advanced_sources"] = sum(
                1 for row in incremental_stats.values() if int(row.get("kept_count", 0)) > 0
            )
            source_health = getattr(self.ingestion_manager, "last_source_health", {})
            source_advisories = getattr(self.ingestion_manager, "last_source_advisories", [])
            run_metrics["sources_skipped_refresh"] = sum(
                1 for row in source_health.values() if str(row.get("status") or "") == "skipped"
            )

            fresh_items, stale_items = self.time_engine.annotate_items(raw_items)
            manifest.raw_items_path = str(self.storage.save_raw_items(run_id, raw_items))
            run_metrics["stale_dropped"] = len(stale_items)
            if stale_items:
                logger.info("Dropped %d stale item(s) before extraction", len(stale_items))

            if not raw_items:
                logger.warning("No raw items ingested in run %s", run_id)

            extracted_signals, extraction_errors = self.extractor.extract(
                domain_label,
                fresh_items,
                strategy_profile=self.config.effective_strategy_profile,
            )
            manifest.errors.extend(extraction_errors)
            for signal in extracted_signals:
                signal.source = "system"
                signal.publish_time = signal.publish_time or signal.published_at
                if signal.age_hours is None and signal.publish_time:
                    signal.age_hours = round(max(0.0, (signal.extracted_at - signal.publish_time).total_seconds() / 3600.0), 3)
                if signal.freshness not in {"fresh", "recent", "stale", "unknown"}:
                    signal.freshness = self._classify_freshness(signal.age_hours)

            history_window = max(
                self.config.filtering.dedup_window_days,
                self.config.filtering.novelty_window_days,
                self.config.filtering.event_candidate_lookback_days,
            )
            historical_signals = self.storage.load_canonical_signals(history_window)
            event_records = self.event_store.load()
            index, retrieval_errors = self.candidate_retrieval.build_recent_index(historical_signals)
            manifest.errors.extend(retrieval_errors)
            manifest.errors.extend(self.candidate_retrieval.ensure_embeddings(extracted_signals))

            event_signals, dedup_errors, dedup_stats = self._resolve_events(
                new_signals=extracted_signals,
                index=index,
                event_records=event_records,
            )
            manifest.errors.extend(dedup_errors)
            run_metrics["dedup_duplicates"] = dedup_stats.duplicates_discarded
            run_metrics["updates_kept"] = dedup_stats.updates_kept
            self.storage.upsert_canonical_signals(event_signals)
            self.event_store.save(event_records)

            inbox_signals = self.inbox_engine.refresh_with_system_signals(event_signals)
            inbox_signals = [signal for signal in inbox_signals if signal.briefed_once_at is None]
            prioritized_signals = self.priority_engine.compute(
                signals=inbox_signals + event_signals,
                history=historical_signals,
            )
            unknown_before_filter = self._count_unknown_system(prioritized_signals)
            filtered_signals = self.filter_engine.apply(prioritized_signals, historical_signals)
            unknown_after_filter = self._count_unknown_system(filtered_signals)
            run_metrics["unknown_dropped"] = max(0, unknown_before_filter - unknown_after_filter)
            manifest.signal_count = len(filtered_signals)
            run_metrics["final_signals"] = len(filtered_signals)
            source_contexts = self._build_signal_source_contexts(filtered_signals, fresh_items)

            signals_path = self.storage.save_signals(run_id, filtered_signals)
            manifest.signals_path = str(signals_path)

            generated_at = utc_now()
            brief_text = self.briefing_generator.generate(
                domain=domain_label,
                signals=filtered_signals,
                language=self.config.briefing.language,
                generated_at=generated_at,
                source_contexts=source_contexts,
                diagnostics={
                    "ingested_items": run_metrics.get("ingested_items", 0),
                    "stale_dropped": run_metrics.get("stale_dropped", 0),
                    "unknown_dropped": run_metrics.get("unknown_dropped", 0),
                    "dedup_duplicates": run_metrics.get("dedup_duplicates", 0),
                    "final_signals": run_metrics.get("final_signals", 0),
                    "errors": manifest.errors[:3],
                },
            )
            brief_path = self.storage.save_brief_text(run_id, brief_text)
            manifest.brief_text_path = str(brief_path)
            debug_dir = self.storage.save_debug_bundle(
                run_id=run_id,
                selected_inputs=fresh_items,
                extracted_signals=extracted_signals,
                final_brief=brief_text,
                source_incremental_stats=incremental_stats,
                source_health_stats=source_health,
                source_advisories=source_advisories,
            )
            manifest.debug_bundle_path = str(debug_dir)

            audio_bytes = b""
            if self.config.tts.enabled:
                audio_script = self.briefing_generator.generate_audio_script(
                    domain=domain_label,
                    signals=filtered_signals,
                    language=self.config.briefing.language,
                    generated_at=generated_at,
                    source_contexts=source_contexts,
                )
                audio_bytes, tts_errors = self.tts_manager.synthesize(audio_script)
                manifest.errors.extend(tts_errors)
                if audio_bytes:
                    audio_path = self.storage.save_brief_audio(run_id, audio_bytes)
                    manifest.brief_audio_path = str(audio_path)

            try:
                persisted = self.storage_engine.save_outputs(
                    run_id=run_id,
                    domain=domain_label,
                    brief_text=brief_text,
                    signals=filtered_signals,
                    generated_at=generated_at,
                    brief_language=self.config.briefing.language,
                    audio_bytes=audio_bytes if audio_bytes else None,
                )
                manifest.persistent_brief_md_path = str(persisted.brief_md_path)
                manifest.persistent_brief_json_path = str(persisted.brief_json_path)
                manifest.persistent_signals_path = str(persisted.signals_json_path)
                if persisted.audio_mp3_path:
                    manifest.persistent_audio_path = str(persisted.audio_mp3_path)
            except Exception as exc:
                msg = f"Persistent storage stage failed: {exc}"
                logger.exception(msg)
                manifest.errors.append(msg)

            try:
                signal_cards = self.briefing_generator.build_signal_cards(
                    domain=domain_label,
                    signals=filtered_signals,
                    language=self.config.briefing.language,
                    source_contexts=source_contexts,
                )
                notified, notify_errors = self.notification_manager.notify(
                    brief_text=brief_text,
                    run_id=run_id,
                    domain=domain_label,
                    generated_at=generated_at,
                    signal_cards=signal_cards,
                    audio_path=manifest.brief_audio_path,
                )
                if notified:
                    self.inbox_engine.mark_briefed(
                        [
                            signal.id
                            for signal in filtered_signals
                            if signal.source == "user" and signal.tracking_id
                        ]
                    )
                if notify_errors:
                    manifest.errors.extend(notify_errors)
            except Exception as exc:
                msg = f"Notification stage failed: {exc}"
                logger.exception(msg)
                manifest.errors.append(msg)

            manifest.status = "completed"
            manifest.finished_at = utc_now()
            self._log_run_metrics(run_id, run_metrics)
            summary_path = self._append_daily_summary(
                manifest=manifest,
                trigger=trigger,
                run_metrics=run_metrics,
            )
            if summary_path is not None:
                manifest.daily_summary_path = str(summary_path)
            if self.webhook_manager is not None:
                webhook_errors = self.webhook_manager.publish_run_outputs(
                    run_id=run_id,
                    domain=domain_label,
                    signals=filtered_signals,
                    brief_text=brief_text,
                    brief_audio_path=manifest.brief_audio_path,
                    manifest=manifest,
                )
                manifest.errors.extend(webhook_errors)
            self.storage.save_manifest(run_id, manifest)
            logger.info("Completed monitoring run %s with %d signals", run_id, manifest.signal_count)
            return manifest

        except Exception as exc:
            msg = f"Run failed: {exc}"
            logger.exception(msg)
            manifest.status = "failed"
            manifest.errors.append(msg)
            manifest.finished_at = utc_now()
            self._log_run_metrics(run_id, run_metrics)
            summary_path = self._append_daily_summary(
                manifest=manifest,
                trigger=trigger,
                run_metrics=run_metrics,
            )
            if summary_path is not None:
                manifest.daily_summary_path = str(summary_path)
            self.storage.save_manifest(run_id, manifest)
            return manifest
        finally:
            self._lock.release()

    def brief_user_signal(self, user_signal: UserSignalInput) -> dict[str, Any]:
        resolved_title = (user_signal.resolved_title or user_signal.title or "").strip()
        resolved_context = (user_signal.resolved_context or user_signal.context or "").strip()
        source_url = user_signal.source_urls[0] if user_signal.source_urls else None
        now = utc_now()

        raw_item = RawItem(
            source_type="html",
            source_name="user",
            title=resolved_title or user_signal.title,
            url=source_url,
            content=resolved_context or user_signal.context,
            fetched_at=now,
            published_at=now,
            publish_time=now,
            age_hours=0.0,
            freshness="fresh",
            metadata={
                "input_mode": user_signal.ingest_mode,
                "source_urls": user_signal.source_urls,
                "original_context": user_signal.original_context or user_signal.context,
            },
        )

        extracted_signals, extraction_errors = self.extractor.extract(
            self._domain_label(),
            [raw_item],
            strategy_profile=self.config.effective_strategy_profile,
        )
        if not extracted_signals:
            extracted_signals = [
                Signal(
                    title=raw_item.title,
                    summary=raw_item.content[:1000],
                    importance=0.8,
                    category="user_brief",
                    source_urls=[source_url] if source_url else [],
                    evidence=["user_submission"],
                    tags=["user"],
                    published_at=now,
                    publish_time=now,
                    age_hours=0.0,
                    freshness="fresh",
                    extracted_at=now,
                    fingerprint=raw_item.id,
                    novelty_score=1.0,
                    source="user",
                )
            ]

        cards = self.briefing_generator.build_signal_cards(
            domain=self._domain_label(),
            signals=extracted_signals,
            language=self.config.briefing.language,
            source_contexts={sig.id: raw_item.content for sig in extracted_signals},
        )
        card = cards[0] if cards else self._fallback_brief_card(
            raw_item,
            extracted_signals[0],
            language=self.config.briefing.language,
        )
        return {
            "card": card,
            "errors": extraction_errors,
            "brief_text": self._format_user_brief(card, raw_item, language=self.config.briefing.language),
        }

    def ingest_only(self) -> IngestOnlyResult:
        run_id = self._make_run_id("ingest")
        items, errors = self.ingestion_manager.ingest_all()
        self.time_engine.annotate_items(items)
        self.storage.save_raw_items(run_id, items)
        return IngestOnlyResult(run_id=run_id, items=items, errors=errors)

    @staticmethod
    def _make_run_id(trigger: str) -> str:
        return f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}_{trigger}"

    def _domain_label(self) -> str:
        domains = self.config.domain_scope
        if not domains:
            return self.config.domain
        return " | ".join(domains)

    def _resolve_events(
        self,
        new_signals: list[Signal],
        index: VectorIndex,
        event_records: dict[str, EventRecord],
    ) -> tuple[list[Signal], list[str], DedupStats]:
        accepted: list[Signal] = []
        errors: list[str] = []
        stats = DedupStats()

        for signal in new_signals:
            if signal.freshness == "stale":
                continue

            matches = self.candidate_retrieval.retrieve(signal, index)
            candidate_signals = [match.signal for match in matches]
            relation_rows: list[dict[str, str]] = []
            if self.candidate_retrieval.should_call_llm(matches):
                relation_rows, llm_errors = self.llm_dedup_engine.compare(signal, candidate_signals)
                errors.extend(llm_errors)

            relation_by_id = {row["candidate_id"]: row["relation"] for row in relation_rows}
            best_match, best_relation = self._select_best_relation(matches, relation_by_id)
            if best_relation == "SAME_EVENT" and best_match is not None:
                signal.event_type = "duplicate"
                signal.event_id = best_match.signal.event_id or self.event_store.ensure_event_for_signal(
                    event_records,
                    best_match.signal,
                )
                stats.duplicates_discarded += 1
                continue

            if best_relation == "UPDATE" and best_match is not None:
                event_id = best_match.signal.event_id or self.event_store.ensure_event_for_signal(
                    event_records,
                    best_match.signal,
                )
                signal.event_id = event_id
                signal.event_type = "update"
                stats.updates_kept += 1
                self.event_store.upsert_signal(event_records, signal)
            else:
                signal.event_type = "new"
                self.event_store.upsert_signal(event_records, signal)

            accepted.append(signal)
            index.add(signal)

        if stats.duplicates_discarded:
            logger.info("Discarded %d duplicate signal(s) via event dedup", stats.duplicates_discarded)
        return accepted, errors, stats

    @staticmethod
    def _classify_freshness(age_hours: float | None) -> str:
        if age_hours is None:
            return "unknown"
        if age_hours <= 24:
            return "fresh"
        if age_hours <= 72:
            return "recent"
        return "stale"

    @staticmethod
    def _select_best_relation(
        matches: list[CandidateMatch],
        relation_by_id: dict[str, str],
    ) -> tuple[CandidateMatch | None, str]:
        if not matches:
            return None, "DIFFERENT"

        relation_rank = {"DIFFERENT": 0, "SAME_EVENT": 1, "UPDATE": 2}

        def _score(match: CandidateMatch) -> tuple[int, float, float]:
            relation = relation_by_id.get(match.signal.id, "DIFFERENT")
            rank = relation_rank.get(relation, 0)
            candidate_time = match.signal.publish_time or match.signal.published_at or match.signal.extracted_at
            return (rank, match.similarity, candidate_time.timestamp())

        best = max(matches, key=_score)
        relation = relation_by_id.get(best.signal.id, "DIFFERENT")
        if relation not in relation_rank:
            relation = "DIFFERENT"
        return best, relation

    @staticmethod
    def _trusted_source_domains(config: MonitorConfig) -> set[str]:
        hosts: set[str] = set()
        for source in config.sources.rss:
            host = _extract_host(source.url)
            if host:
                hosts.add(host)
        for source in config.sources.playwright:
            host = _extract_host(source.url)
            if host:
                hosts.add(host)
        return hosts

    @staticmethod
    def _count_unknown_system(signals: list[Signal]) -> int:
        return sum(1 for signal in signals if signal.source != "user" and signal.freshness == "unknown")

    def _maybe_refresh_source_strategies(self, manifest: RunArtifacts) -> None:
        enabled, refresh_days = self._source_strategy_refresh_settings()
        if not enabled:
            return
        urls = self._source_strategy_urls()
        if not urls:
            return
        try:
            result = self.source_strategy_engine.suggest(
                urls=urls,
                refresh_interval_days=refresh_days,
                force_refresh=False,
            )
            logger.info(
                "Source strategy refresh: total=%d reused=%d recomputed=%d interval_days=%d",
                result.total_urls,
                result.reused_cached,
                result.recomputed,
                refresh_days,
            )
            manifest.run_metrics.setdefault("source_strategy_recomputed", result.recomputed)
            manifest.run_metrics.setdefault("source_strategy_cached", result.reused_cached)
        except Exception as exc:
            msg = f"Source strategy refresh failed: {exc}"
            logger.warning(msg)
            manifest.errors.append(msg)

    def _source_strategy_refresh_settings(self) -> tuple[bool, int]:
        internal = self.config.internal_strategy if isinstance(self.config.internal_strategy, dict) else {}
        advanced = internal.get("advanced_settings", {}) if isinstance(internal, dict) else {}
        if not isinstance(advanced, dict):
            advanced = {}
        enabled = bool(advanced.get("source_strategy_auto_refresh", True))
        try:
            refresh_days = int(advanced.get("source_strategy_refresh_days", 14))
        except (TypeError, ValueError):
            refresh_days = 14
        refresh_days = max(7, min(90, refresh_days))
        return enabled, refresh_days

    def _source_strategy_urls(self) -> list[str]:
        urls: list[str] = []
        for profile in self.config.domain_profiles:
            for item in profile.source_links:
                if isinstance(item, str):
                    token = item.strip()
                    if token:
                        urls.append(token)
                else:
                    token = item.url.strip()
                    if token:
                        urls.append(token)
        if not urls:
            urls.extend([row.url for row in self.config.sources.rss])
            urls.extend([row.url for row in self.config.sources.playwright])
        deduped: list[str] = []
        seen: set[str] = set()
        for item in urls:
            key = item.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item.strip())
        return deduped

    def _append_daily_summary(
        self,
        manifest: RunArtifacts,
        trigger: str,
        run_metrics: dict[str, int],
    ) -> str | None:
        finished = manifest.finished_at.astimezone(UTC).isoformat() if manifest.finished_at else None
        summary_row = {
            "run_id": manifest.run_id,
            "domain": manifest.domain,
            "trigger": trigger,
            "status": manifest.status,
            "started_at": manifest.started_at.astimezone(UTC).isoformat(),
            "finished_at": finished,
            "metrics": run_metrics,
            "source_incremental_stats": getattr(self.ingestion_manager, "last_incremental_stats", {}),
            "source_health_stats": getattr(self.ingestion_manager, "last_source_health", {}),
            "source_advisories": getattr(self.ingestion_manager, "last_source_advisories", []),
            "signals_path": manifest.persistent_signals_path or manifest.signals_path,
            "brief_path": manifest.persistent_brief_md_path or manifest.brief_text_path,
            "debug_bundle_path": manifest.debug_bundle_path,
            "error_count": len(manifest.errors),
        }
        try:
            return str(self.storage.append_daily_summary(manifest.started_at, summary_row))
        except Exception:
            logger.exception("Failed to append daily summary for run %s", manifest.run_id)
            return None

    @staticmethod
    def _log_run_metrics(run_id: str, run_metrics: dict[str, int]) -> None:
        logger.info(
            (
                "Run stats %s | ingested=%d | dropped_stale=%d | dropped_unknown=%d "
                "| dedup_duplicates=%d | updates=%d | final_signals=%d | incremental_dropped=%d | sources_skipped=%d"
            ),
            run_id,
            run_metrics.get("ingested_items", 0),
            run_metrics.get("stale_dropped", 0),
            run_metrics.get("unknown_dropped", 0),
            run_metrics.get("dedup_duplicates", 0),
            run_metrics.get("updates_kept", 0),
            run_metrics.get("final_signals", 0),
            run_metrics.get("incremental_dropped", 0),
            run_metrics.get("sources_skipped_refresh", 0),
        )

    def _build_signal_source_contexts(
        self,
        signals: list[Signal],
        raw_items: list[RawItem],
    ) -> dict[str, str]:
        if not signals:
            return {}

        url_index: dict[str, list[RawItem]] = {}
        for item in raw_items:
            if not item.url:
                continue
            key = self._normalize_url_key(item.url)
            if not key:
                continue
            url_index.setdefault(key, []).append(item)

        contexts: dict[str, str] = {}
        for signal in signals:
            chunks: list[str] = []
            seen_chunks: set[str] = set()
            for url in signal.source_urls[:3]:
                key = self._normalize_url_key(url)
                if not key:
                    continue
                for item in url_index.get(key, [])[:2]:
                    dedupe_key = f"{item.source_name}|{item.title}|{item.url}"
                    if dedupe_key in seen_chunks:
                        continue
                    seen_chunks.add(dedupe_key)
                    chunks.append(
                        "\n".join(
                            [
                                f"source_name: {item.source_name}",
                                f"title: {item.title}",
                                f"url: {item.url or ''}",
                                f"content_snippet: {self._compact_text(item.content, 420)}",
                            ]
                        )
                    )

            if not chunks:
                fallback_parts: list[str] = []
                if signal.summary:
                    fallback_parts.append(f"summary: {signal.summary}")
                if signal.evidence:
                    fallback_parts.append("evidence: " + " | ".join(signal.evidence[:3]))
                if fallback_parts:
                    chunks.append("\n".join(fallback_parts))

            if chunks:
                contexts[signal.id] = "\n\n".join(chunks[:3])

        return contexts

    def _format_user_brief(self, card: dict[str, Any], raw_item: RawItem, *, language: str = "zh") -> str:
        lines: list[str] = []
        lang = "en" if str(language).strip().lower() == "en" else "zh"
        title = str(card.get("title") or raw_item.title).strip()
        what = str(card.get("what") or raw_item.content[:800]).strip()
        why = str(card.get("why") or "").strip()
        follow_up = [str(v).strip() for v in card.get("follow_up", []) if str(v).strip()]
        source_links = card.get("source_links", [])

        lines.append(f"Single-item brief: {title}" if lang == "en" else f"单篇简报：{title}")
        if what:
            lines.extend(["", "What Happened" if lang == "en" else "发生了什么", what])
        if why:
            lines.extend(["", "Why It Matters" if lang == "en" else "为什么重要", why])
        if follow_up:
            lines.extend(["", "Follow-Up" if lang == "en" else "后续跟踪"])
            for point in follow_up[:2]:
                lines.append(f"- {point}")
        if isinstance(source_links, list) and source_links:
            lines.extend(["", "Sources" if lang == "en" else "来源"])
            for item in source_links[:2]:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or ("Source" if lang == "en" else "来源")).strip()
                url = str(item.get("url") or "").strip()
                if url:
                    lines.append(f"- {label}: {url}")
        return "\n".join(lines).strip()

    @staticmethod
    def _fallback_brief_card(raw_item: RawItem, signal: Signal, language: str = "zh") -> dict[str, Any]:
        lang = "en" if str(language).strip().lower() == "en" else "zh"
        source_url = raw_item.url
        source_links = [{"label": "Source" if lang == "en" else "来源", "url": source_url}] if source_url else []
        return {
            "title": signal.title,
            "what": signal.summary[:500],
            "why": (
                "This item is relevant to the current monitoring topic and is worth reviewing separately."
                if lang == "en"
                else "这条内容与当前监控主题相关，值得单独关注。"
            ),
            "follow_up": (
                ["If additional details appear later, revisit it as a follow-up."]
                if lang == "en"
                else ["后续如果有增量披露，再做二次跟踪。"]
            ),
            "source_links": source_links,
        }

    @staticmethod
    def _normalize_url_key(url: str | None) -> str:
        token = str(url or "").strip()
        if not token:
            return ""
        parsed = urlparse(token if "://" in token else f"https://{token}")
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").rstrip("/")
        if host.startswith("www."):
            host = host[4:]
        return f"{host}{path}".strip("/")

    @staticmethod
    def _compact_text(text: str, limit: int) -> str:
        token = " ".join(str(text or "").split())
        if len(token) <= limit:
            return token
        return token[: limit - 1].rstrip() + "…"


def _extract_host(url: str) -> str | None:
    if not url:
        return None
    cleaned = url.strip()
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    try:
        from urllib.parse import urlparse

        host = (urlparse(cleaned).hostname or "").strip().lower()
    except Exception:
        return None
    if not host:
        return None
    if host.startswith("www."):
        return host[4:]
    return host
