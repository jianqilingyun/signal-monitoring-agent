from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from monitor_agent.candidate_retrieval import CandidateMatch, VectorIndex
from monitor_agent.core.models import MonitorConfig, RawItem, Signal
from monitor_agent.core.pipeline import MonitoringPipeline
from monitor_agent.core.storage import Storage
from monitor_agent.core.utils import make_fingerprint, utc_now
from monitor_agent.inbox_engine import InboxEngine
from monitor_agent.storage_engine import StorageEngine


def _config() -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "domain": "Test Domain",
            "sources": {
                "rss": [{"name": "Trusted Feed", "url": "https://trusted.example/rss.xml"}],
                "playwright": [],
            },
        }
    )


def _signal(title: str, summary: str, *, event_id: str | None = None) -> Signal:
    now = utc_now()
    return Signal(
        title=title,
        summary=summary,
        importance=0.8,
        category="news",
        source_urls=["https://trusted.example/article"],
        evidence=["unit_test"],
        tags=["test"],
        published_at=now - timedelta(hours=1),
        publish_time=now - timedelta(hours=1),
        age_hours=1.0,
        freshness="fresh",
        extracted_at=now,
        fingerprint=make_fingerprint(title, summary, now.isoformat()),
        novelty_score=0.6,
        source="system",
        event_id=event_id,
    )


class LocalTrialOpsTests(unittest.TestCase):
    def test_monitor_config_domain_scope_dedupes(self) -> None:
        cfg = MonitorConfig.model_validate(
            {
                "domain": "AI Infrastructure",
                "domains": ["AI Infrastructure", "Cybersecurity", "cybersecurity"],
                "sources": {"rss": [], "playwright": []},
            }
        )
        self.assertEqual(cfg.domain_scope, ["AI Infrastructure", "Cybersecurity"])

    def test_save_debug_bundle_writes_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            now = utc_now()
            raw_item = RawItem(
                source_type="rss",
                source_name="feed",
                title="Raw input",
                url="https://trusted.example/input",
                content="example",
                fetched_at=now,
            )
            extracted = _signal("Extracted signal", "Extracted summary")

            debug_dir = storage.save_debug_bundle(
                run_id="20260317T220000Z_manual",
                selected_inputs=[raw_item],
                extracted_signals=[extracted],
                final_brief="Final brief text",
                source_incremental_stats={"rss::example": {"kept_count": 2}},
                source_health_stats={"rss::example": {"status": "success"}},
                source_advisories=[{"source_key": "rss::example", "severity": "warning"}],
            )
            self.assertTrue((debug_dir / "selected_inputs.json").exists())
            self.assertTrue((debug_dir / "extracted_signals.json").exists())
            self.assertTrue((debug_dir / "final_brief.txt").exists())
            self.assertTrue((debug_dir / "source_incremental_stats.json").exists())
            self.assertTrue((debug_dir / "source_health_stats.json").exists())
            self.assertTrue((debug_dir / "source_advisories.json").exists())

    def test_pipeline_domain_label_aggregates_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            cfg = MonitorConfig.model_validate(
                {
                    "domain": "AI Infrastructure",
                    "domains": ["Cybersecurity"],
                    "sources": {"rss": [], "playwright": []},
                }
            )
            pipeline = MonitoringPipeline(
                config=cfg,
                storage=storage,
                inbox_engine=InboxEngine(storage, match_threshold=0.2),
            )
            self.assertEqual(pipeline._domain_label(), "AI Infrastructure | Cybersecurity")

    def test_append_daily_summary_is_idempotent_by_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            started_at = datetime.now(UTC)
            row = {"run_id": "run-123", "status": "completed", "metrics": {"final_signals": 4}}
            path = storage.append_daily_summary(started_at, row)
            storage.append_daily_summary(started_at, row)

            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["run_id"], "run-123")

    def test_resolve_events_returns_duplicate_and_update_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            pipeline = MonitoringPipeline(
                config=_config(),
                storage=storage,
                inbox_engine=InboxEngine(storage, match_threshold=0.2),
            )
            candidate = _signal("Outage baseline", "Initial outage report.", event_id="evt_1")

            class _FakeRetrieval:
                @staticmethod
                def retrieve(signal: Signal, index: VectorIndex) -> list[CandidateMatch]:
                    return [CandidateMatch(signal=candidate, similarity=0.9)]

                @staticmethod
                def should_call_llm(matches: list[CandidateMatch]) -> bool:
                    return True

            class _FakeDedup:
                @staticmethod
                def compare(new_signal: Signal, candidates: list[Signal]) -> tuple[list[dict[str, str]], list[str]]:
                    relation = "SAME_EVENT" if "duplicate" in new_signal.title.lower() else "UPDATE"
                    return ([{"candidate_id": candidate.id, "relation": relation}], [])

            pipeline.candidate_retrieval = _FakeRetrieval()  # type: ignore[assignment]
            pipeline.llm_dedup_engine = _FakeDedup()  # type: ignore[assignment]

            incoming_duplicate = _signal("Duplicate report", "Same event wording.")
            incoming_update = _signal("Update report", "New details on same event.")
            accepted, _, stats = pipeline._resolve_events(
                [incoming_duplicate, incoming_update],
                VectorIndex(),
                pipeline.event_store.load(),
            )
            self.assertEqual(stats.duplicates_discarded, 1)
            self.assertEqual(stats.updates_kept, 1)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0].event_type, "update")

    def test_storage_engine_brief_json_uses_signals_path_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = StorageEngine(base_path=tmpdir, timezone="UTC")
            signal = _signal("A", "B")
            out = engine.save_outputs(
                run_id="run-1",
                domain="Test Domain",
                brief_text="brief content",
                signals=[signal],
                generated_at=utc_now(),
            )
            payload = json.loads(Path(out.brief_json_path).read_text(encoding="utf-8"))
            self.assertIn("signals_path", payload)
            self.assertNotIn("signals", payload)


if __name__ == "__main__":
    unittest.main()
