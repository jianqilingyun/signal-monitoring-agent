from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime

from monitor_agent.core.models import RunArtifacts
from monitor_agent.core.storage import Storage
from monitor_agent.ingestion_layer.source_advisories import build_source_advisories


class SourceAdvisoriesTests(unittest.TestCase):
    def test_build_source_advisory_uses_cache_alternative_for_failing_feed(self) -> None:
        advisories = build_source_advisories(
            {
                "rss::https://cloud.google.com/blog/rss/": {
                    "source_name": "Google Cloud Blog",
                    "source_type": "rss",
                    "source_url": "https://cloud.google.com/blog/rss/",
                    "status": "error",
                    "error": "feed returned zero entries",
                }
            },
            {
                "https://cloud.google.com/blog/rss/": {
                    "probe_status": "warning",
                    "issues": ["RSS probe unhealthy: feed returned zero entries"],
                    "fixes": ["Try webpage ingestion instead: https://cloud.google.com/blog/"],
                    "normalized_source_link": {
                        "url": "https://cloud.google.com/blog/",
                        "type": "playwright",
                        "name": "cloud.google.com web",
                    },
                }
            },
        )
        self.assertEqual(len(advisories), 1)
        advisory = advisories[0]
        self.assertEqual(advisory.issue_code, "fetch_error")
        self.assertEqual(advisory.severity, "error")
        self.assertEqual(advisory.suggested_source_link["url"], "https://cloud.google.com/blog/")

    def test_storage_load_latest_source_advisories_reads_debug_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            run_id = "20260322T120117Z_local_once"
            storage.save_debug_bundle(
                run_id=run_id,
                selected_inputs=[],
                extracted_signals=[],
                final_brief="brief",
                source_advisories=[{"source_key": "rss::example", "severity": "warning"}],
            )
            manifest = RunArtifacts(
                run_id=run_id,
                started_at=datetime.now(UTC),
                domain="AI Infrastructure",
                status="completed",
            )
            storage.save_manifest(run_id, manifest)

            advisories = storage.load_latest_source_advisories()
            self.assertEqual(len(advisories), 1)
            self.assertEqual(advisories[0]["source_key"], "rss::example")


if __name__ == "__main__":
    unittest.main()
