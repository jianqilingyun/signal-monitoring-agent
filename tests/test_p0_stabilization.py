from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from monitor_agent.candidate_retrieval import CandidateMatch, VectorIndex
from monitor_agent.briefing.generator import BriefingGenerator
from monitor_agent.core.models import MonitorConfig, RawItem, RunArtifacts, Signal
from monitor_agent.core.pipeline import MonitoringPipeline
from monitor_agent.core.scheduler import SchedulerService
from monitor_agent.core.storage import Storage
from monitor_agent.core.utils import make_fingerprint, utc_now
from monitor_agent.filter_engine.engine import FilterEngine
from monitor_agent.inbox_engine import InboxEngine, UserSignalInput
from monitor_agent.time_engine import TimeEngine


def _base_config() -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "domain": "Test Domain",
            "sources": {
                "rss": [{"name": "Trusted Feed", "url": "https://trusted.example/rss.xml"}],
                "playwright": [],
            },
            "schedule": {"timezone": "UTC", "times": ["07:00", "22:00"], "enabled": True},
            "api": {"host": "127.0.0.1", "port": 8080, "scheduler_enabled": False},
        }
    )


def _make_signal(
    *,
    title: str,
    summary: str,
    extracted_at: datetime | None = None,
    publish_time: datetime | None = None,
    freshness: str = "fresh",
    importance: float = 0.8,
    source: str = "system",
    source_urls: list[str] | None = None,
    tags: list[str] | None = None,
    event_id: str | None = None,
) -> Signal:
    extracted = extracted_at or utc_now()
    published = publish_time
    if freshness != "unknown" and published is None:
        published = extracted - timedelta(hours=2)
    age_hours = None if freshness == "unknown" else max(0.0, (extracted - published).total_seconds() / 3600.0)  # type: ignore[arg-type]
    return Signal(
        title=title,
        summary=summary,
        importance=importance,
        category="news",
        source_urls=source_urls or ["https://trusted.example/article"],
        evidence=["unit_test"],
        tags=tags or [],
        published_at=published,
        publish_time=published,
        age_hours=age_hours,
        freshness=freshness,  # type: ignore[arg-type]
        extracted_at=extracted,
        fingerprint=make_fingerprint(title, summary, extracted.isoformat()),
        novelty_score=0.7,
        source=source,  # type: ignore[arg-type]
        event_id=event_id,
    )


class P0StabilizationTests(unittest.TestCase):
    def test_cross_day_dedup_uses_canonical_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            config = _base_config()
            pipeline = MonitoringPipeline(
                config=config,
                storage=storage,
                inbox_engine=InboxEngine(storage, match_threshold=config.filtering.inbox_match_threshold),
            )

            yesterday = utc_now() - timedelta(days=1, hours=1)
            prior = _make_signal(
                title="Acme raises Series B",
                summary="Acme announced a 40 million Series B round led by North Fund.",
                extracted_at=yesterday,
                source_urls=["https://trusted.example/acme-series-b"],
                tags=["Acme", "funding"],
            )
            pipeline.candidate_retrieval.ensure_embeddings([prior])
            storage.upsert_canonical_signals([prior])

            # No run artifact history exists: canonical store is the only source.
            self.assertEqual(storage.load_recent_signals(lookback_days=3), [])
            canonical = storage.load_canonical_signals(lookback_days=3)
            self.assertEqual(len(canonical), 1)

            index, _ = pipeline.candidate_retrieval.build_recent_index(canonical)
            incoming = _make_signal(
                title="Acme raises Series B",
                summary="Acme closes a 40M Series B round led by North Fund.",
                extracted_at=utc_now(),
                source_urls=["https://trusted.example/acme-series-b-update"],
                tags=["Acme", "funding"],
            )

            accepted, _, _ = pipeline._resolve_events([incoming], index, pipeline.event_store.load())
            self.assertFalse(accepted and accepted[0].event_type == "new")

    def test_unknown_publish_time_not_auto_fresh(self) -> None:
        now = utc_now()
        item = RawItem(
            source_type="playwright",
            source_name="Evergreen",
            title="Evergreen landing page",
            url="https://example.org/page",
            content="Welcome to our documentation portal.",
            fetched_at=now,
            metadata={},
        )
        annotated = TimeEngine().annotate_item(item)
        self.assertIsNone(annotated.publish_time)
        self.assertIsNone(annotated.age_hours)
        self.assertEqual(annotated.freshness, "unknown")

        filtering = _base_config().filtering.model_copy(update={"unknown_freshness_importance_threshold": 0.9})
        filter_engine = FilterEngine(filtering, trusted_domains={"trusted.example"})

        untrusted_unknown = _make_signal(
            title="Ambiguous evergreen page",
            summary="No reliable publish timestamp was found.",
            freshness="unknown",
            importance=0.99,
            source_urls=["https://untrusted.example/evergreen"],
        )
        trusted_unknown = _make_signal(
            title="Trusted unknown timestamp item",
            summary="No timestamp but source is trusted and signal is high-importance.",
            freshness="unknown",
            importance=0.95,
            source_urls=["https://trusted.example/notice"],
        )

        self.assertEqual(filter_engine.apply([untrusted_unknown], history=[]), [])
        kept = filter_engine.apply([trusted_unknown], history=[])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].title, trusted_unknown.title)

    def test_multi_candidate_policy_prefers_update_over_same(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            config = _base_config()
            pipeline = MonitoringPipeline(
                config=config,
                storage=storage,
                inbox_engine=InboxEngine(storage, match_threshold=config.filtering.inbox_match_threshold),
            )

            candidate_same = _make_signal(
                title="Datacenter outage in region A",
                summary="Initial outage report for region A.",
                extracted_at=utc_now() - timedelta(hours=3),
                event_id="evt_same",
            )
            candidate_update = _make_signal(
                title="Datacenter outage in region A",
                summary="Follow-up details: recovery at 60 percent.",
                extracted_at=utc_now() - timedelta(hours=1),
                event_id="evt_update",
            )
            incoming = _make_signal(
                title="Datacenter outage in region A recovery update",
                summary="Recovery reached 90 percent and ETA published.",
                extracted_at=utc_now(),
            )

            class _FakeRetrieval:
                @staticmethod
                def retrieve(signal: Signal, index: VectorIndex) -> list[CandidateMatch]:
                    return [
                        CandidateMatch(signal=candidate_same, similarity=0.96),
                        CandidateMatch(signal=candidate_update, similarity=0.81),
                    ]

                @staticmethod
                def should_call_llm(matches: list[CandidateMatch]) -> bool:
                    return True

            class _FakeDedup:
                @staticmethod
                def compare(new_signal: Signal, candidates: list[Signal]) -> tuple[list[dict[str, str]], list[str]]:
                    return (
                        [
                            {"candidate_id": candidate_same.id, "relation": "SAME_EVENT"},
                            {"candidate_id": candidate_update.id, "relation": "UPDATE"},
                        ],
                        [],
                    )

            pipeline.candidate_retrieval = _FakeRetrieval()  # type: ignore[assignment]
            pipeline.llm_dedup_engine = _FakeDedup()  # type: ignore[assignment]

            event_records = pipeline.event_store.load()
            accepted, errors, _ = pipeline._resolve_events([incoming], VectorIndex(), event_records)
            self.assertEqual(errors, [])
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0].event_type, "update")
            self.assertEqual(accepted[0].event_id, "evt_update")

    def test_scheduler_disabled_mode(self) -> None:
        class _FakePipeline:
            def run_once(self, trigger: str = "scheduled") -> None:
                return None

        scheduler = SchedulerService(
            timezone="UTC",
            times=["07:00", "22:00"],
            pipeline=_FakePipeline(),  # type: ignore[arg-type]
            enabled=False,
        )
        scheduler.start()
        self.assertFalse(scheduler._started)
        self.assertEqual(scheduler.scheduler.get_jobs(), [])
        scheduler.shutdown()

    def test_latest_reads_use_canonical_persistent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            run_id = "20260317T070000Z_manual"
            run_dir = storage.create_run_dir(run_id)

            legacy_signals_path = run_dir / "signals.json"
            legacy_signals_path.write_text('[{"title":"legacy"}]', encoding="utf-8")
            legacy_brief_path = run_dir / "brief.txt"
            legacy_brief_path.write_text("legacy brief", encoding="utf-8")

            persistent_signals_path = storage.root / "signals" / "2026-03-17-morning.json"
            persistent_signals_path.parent.mkdir(parents=True, exist_ok=True)
            persistent_signals_path.write_text(
                '{"signals":[{"id":"1","title":"canonical"}]}',
                encoding="utf-8",
            )
            persistent_brief_md_path = storage.root / "briefs" / "2026-03-17-morning.md"
            persistent_brief_md_path.parent.mkdir(parents=True, exist_ok=True)
            persistent_brief_md_path.write_text(
                "canonical brief markdown",
                encoding="utf-8",
            )

            manifest = RunArtifacts(
                run_id=run_id,
                started_at=datetime.now(UTC),
                domain="Test Domain",
                signals_path=str(legacy_signals_path),
                brief_text_path=str(legacy_brief_path),
                persistent_signals_path=str(persistent_signals_path),
                persistent_brief_md_path=str(persistent_brief_md_path),
                status="completed",
            )
            storage.save_manifest(run_id, manifest)

            latest_signals = storage.load_latest_signals()
            self.assertEqual(latest_signals[0]["title"], "canonical")
            self.assertEqual(storage.load_latest_brief(), "canonical brief markdown")

    def test_inbox_linkage_threshold_reduces_false_positive_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage, match_threshold=0.2)
            added = inbox.ingest_user_signals(
                [
                    UserSignalInput(
                        title="Nvidia Blackwell launch timeline",
                        context="Track Nvidia Blackwell launch delays and production updates.",
                        tags=["Nvidia", "Blackwell"],
                        entities=["Nvidia"],
                    )
                ]
            )
            self.assertEqual(len(added), 1)
            tracking_id = added[0].tracking_id

            weak_signal = _make_signal(
                title="Oil market move",
                summary="Oil prices moved lower in Europe trading.",
                tags=["energy"],
                source_urls=["https://trusted.example/energy"],
            )
            inbox.refresh_with_system_signals([weak_signal])
            tracked = {row.tracking_id: row for row in inbox.get_tracked_signals()}
            self.assertIn("No fresh matching system updates", tracked[tracking_id].system_interpretation)

            strong_signal = _make_signal(
                title="Nvidia Blackwell production update",
                summary="Nvidia Blackwell launch timeline shifted with new production update.",
                tags=["Nvidia", "Blackwell", "production"],
                source_urls=["https://trusted.example/nvidia-update"],
            )
            inbox.refresh_with_system_signals([strong_signal])
            tracked_after = {row.tracking_id: row for row in inbox.get_tracked_signals()}
            latest_updates = tracked_after[tracking_id].latest_updates
            self.assertTrue(any("Nvidia Blackwell production update" in row for row in latest_updates))

    def test_user_signals_are_surfaced_once_after_briefing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            inbox = InboxEngine(storage)
            added = inbox.ingest_user_signals(
                [
                    UserSignalInput(
                        title="Track OpenAI harness engineering",
                        context="Please monitor OpenAI harness engineering updates.",
                        tags=["OpenAI"],
                    )
                ]
            )
            self.assertEqual(len(added), 1)
            self.assertEqual(len(inbox.build_watchlist()), 1)

            inbox.mark_briefed([added[0].id])
            self.assertEqual(len(inbox.build_watchlist()), 0)

            brief = BriefingGenerator().generate(
                domain="Test Domain",
                signals=inbox.get_tracked_signals(),
                generated_at=datetime.now(UTC),
            )
            self.assertNotIn("Track OpenAI harness engineering", brief)


if __name__ == "__main__":
    unittest.main()
