from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from monitor_agent.api.server import ApiServer
from monitor_agent.core.models import MonitorConfig, RunArtifacts
from monitor_agent.core.storage import Storage
from monitor_agent.core.webhooks import WebhookManager
from monitor_agent.inbox_engine import InboxEngine


@dataclass
class _IngestOnlyResult:
    run_id: str
    items: list
    errors: list[str]


class _FakePipeline:
    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.run_once_calls = 0
        self.ingest_only_calls = 0
        self.brief_user_signal_calls = 0

    def run_once(self, trigger: str = "manual") -> RunArtifacts:
        self.run_once_calls += 1
        now = datetime.now(UTC)
        return RunArtifacts(
            run_id=f"run_{self.run_once_calls}",
            started_at=now,
            finished_at=now,
            domain=self.config.domain,
            status="completed",
            signal_count=1,
        )

    def ingest_only(self) -> _IngestOnlyResult:
        self.ingest_only_calls += 1
        return _IngestOnlyResult(run_id="ingest_only", items=[], errors=[])

    def brief_user_signal(self, user_signal) -> dict[str, object]:
        self.brief_user_signal_calls += 1
        return {
            "brief_text": f"单篇简报：{getattr(user_signal, 'title', 'Untitled')}",
            "card": {
                "title": getattr(user_signal, "title", "Untitled"),
                "what": "fake",
                "why": "fake",
                "follow_up": ["fake"],
                "source_links": [],
            },
            "errors": [],
        }

    def update_config(self, config: MonitorConfig) -> None:
        self.config = config


def _base_config(*, auto_run_on_user_ingest: bool) -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "domain": "Test Domain",
            "sources": {"rss": [], "playwright": []},
            "api": {
                "host": "127.0.0.1",
                "port": 8080,
                "scheduler_enabled": False,
                "auto_run_on_user_ingest": auto_run_on_user_ingest,
            },
        }
    )


class IngestAutoRunTests(unittest.TestCase):
    def test_ingest_user_signals_saves_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"MONITOR_API_TOKEN": "secret-token"}, clear=False):
            storage = Storage(tmpdir)
            config = _base_config(auto_run_on_user_ingest=True)
            pipeline = _FakePipeline(config)
            app = FastAPI()
            ApiServer(
                app=app,
                pipeline=pipeline,  # type: ignore[arg-type]
                storage=storage,
                webhook_manager=WebhookManager(storage),
                inbox_engine=InboxEngine(storage),
            )
            client = TestClient(app)

            resp = client.post(
                "/ingest",
                json={
                    "run_system_ingestion": True,
                    "user_signals": [
                        {"title": "Track NVIDIA", "context": "Please monitor Blackwell supply updates."}
                    ],
                },
                headers={"X-Monitor-Token": "secret-token"},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["immediate_briefs"], [])
            self.assertEqual(pipeline.run_once_calls, 0)
            self.assertEqual(pipeline.ingest_only_calls, 0)

    def test_ingest_user_signals_brief_now_returns_immediate_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict("os.environ", {"MONITOR_API_TOKEN": "secret-token"}, clear=False):
            storage = Storage(tmpdir)
            config = _base_config(auto_run_on_user_ingest=False)
            pipeline = _FakePipeline(config)
            app = FastAPI()
            ApiServer(
                app=app,
                pipeline=pipeline,  # type: ignore[arg-type]
                storage=storage,
                webhook_manager=WebhookManager(storage),
                inbox_engine=InboxEngine(storage),
            )
            client = TestClient(app)

            resp = client.post(
                "/ingest",
                json={
                    "user_signals": [
                        {
                            "title": "Track AMD",
                            "context": "Please monitor AMD MI roadmap updates.",
                            "ingest_mode": "brief_now",
                        }
                    ],
                },
                headers={"X-Monitor-Token": "secret-token"},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["immediate_briefs"])
            self.assertEqual(pipeline.run_once_calls, 0)
            self.assertEqual(pipeline.ingest_only_calls, 0)
            self.assertEqual(pipeline.brief_user_signal_calls, 1)


if __name__ == "__main__":
    unittest.main()
