from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from monitor_agent.api.server import ApiServer
from monitor_agent.core.models import MonitorConfig, RunArtifacts
from monitor_agent.core.storage import Storage
from monitor_agent.core.webhooks import WebhookManager
from monitor_agent.inbox_engine import InboxEngine
from monitor_agent.storage_engine import StorageEngine


class _PipelineStub:
    def __init__(self, config: MonitorConfig, root_dir: str | None = None) -> None:
        self.config = config
        base_dir = root_dir or str(config.storage.persistent_base_path)
        self.storage_engine = StorageEngine(base_path=base_dir, timezone=config.schedule.timezone)

    def update_config(self, config: MonitorConfig) -> None:
        self.config = config


def _config(root_dir: str) -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "domain": "AI Infrastructure",
            "domain_profiles": [{"domain": "AI Infrastructure"}],
            "sources": {"rss": [], "playwright": []},
            "llm": {"provider": "openai", "model": "gpt-5-mini", "embedding_model": "text-embedding-3-small"},
            "tts": {"enabled": False, "provider": "gtts", "model": "gpt-4o-mini-tts", "voice": "alloy"},
            "notifications": {
                "channel": "none",
                "channels": [],
                "telegram": {"enabled": False},
                "dingtalk": {"enabled": False, "ingest_enabled": False},
            },
            "storage": {"root_dir": root_dir, "base_path": root_dir},
            "api": {"host": "127.0.0.1", "port": 8080, "scheduler_enabled": False},
        }
    )


class ApiSecurityTests(unittest.TestCase):
    def test_remote_requests_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"MONITOR_API_TOKEN": "secret-token"},
            clear=False,
        ):
            storage = Storage(tmpdir)
            app = FastAPI()
            ApiServer(
                app=app,
                pipeline=_PipelineStub(_config(tmpdir), tmpdir),  # type: ignore[arg-type]
                storage=storage,
                webhook_manager=WebhookManager(storage),
                inbox_engine=InboxEngine(storage),
            )
            client = TestClient(app)

            unauthorized = client.get("/signals/latest")
            self.assertEqual(unauthorized.status_code, 401)

            authorized = client.get("/signals/latest", headers={"X-Monitor-Token": "secret-token"})
            self.assertEqual(authorized.status_code, 200)

    def test_webhook_private_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"MONITOR_API_TOKEN": "secret-token"},
            clear=False,
        ):
            storage = Storage(tmpdir)
            app = FastAPI()
            ApiServer(
                app=app,
                pipeline=_PipelineStub(_config(tmpdir), tmpdir),  # type: ignore[arg-type]
                storage=storage,
                webhook_manager=WebhookManager(storage),
                inbox_engine=InboxEngine(storage),
            )
            client = TestClient(app)

            response = client.post(
                "/webhooks/subscribe",
                json={"url": "http://127.0.0.1/hook", "events": ["brief.new"]},
                headers={"X-Monitor-Token": "secret-token"},
            )
            self.assertEqual(response.status_code, 422)

    def test_latest_source_advisories_endpoint_returns_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"MONITOR_API_TOKEN": "secret-token"},
            clear=False,
        ):
            storage = Storage(tmpdir)
            storage.save_debug_bundle(
                run_id="run-1",
                selected_inputs=[],
                extracted_signals=[],
                final_brief="brief",
                source_advisories=[{"source_key": "rss::example", "severity": "warning"}],
            )
            storage.save_manifest(
                "run-1",
                RunArtifacts(run_id="run-1", started_at=datetime.now(UTC), domain="AI Infrastructure", status="completed"),
            )

            app = FastAPI()
            ApiServer(
                app=app,
                pipeline=_PipelineStub(_config(tmpdir), tmpdir),  # type: ignore[arg-type]
                storage=storage,
                webhook_manager=WebhookManager(storage),
                inbox_engine=InboxEngine(storage),
            )
            client = TestClient(app)

            response = client.get("/sources/advisories/latest", headers={"X-Monitor-Token": "secret-token"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["advisories"][0]["source_key"], "rss::example")

    def test_brief_history_endpoints_return_latest_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"MONITOR_API_TOKEN": "secret-token"},
            clear=False,
        ):
            storage = Storage(tmpdir)
            cfg = _config(tmpdir)
            pipeline = _PipelineStub(cfg, tmpdir)
            generated_at = datetime.now(UTC)
            persisted = pipeline.storage_engine.save_outputs(
                run_id="run-brief-1",
                domain="AI Infrastructure",
                brief_text="# Brief\\n\\nHello world",
                signals=[],
                generated_at=generated_at,
            )
            storage.save_manifest(
                "run-brief-1",
                RunArtifacts(run_id="run-brief-1", started_at=generated_at, domain="AI Infrastructure", status="completed"),
            )

            app = FastAPI()
            ApiServer(
                app=app,
                pipeline=pipeline,  # type: ignore[arg-type]
                storage=storage,
                webhook_manager=WebhookManager(storage),
                inbox_engine=InboxEngine(storage),
            )
            client = TestClient(app)

            history_resp = client.get("/brief/history?limit=5", headers={"X-Monitor-Token": "secret-token"})
            self.assertEqual(history_resp.status_code, 200)
            history_payload = history_resp.json()
            self.assertEqual(history_payload["count"], 1)
            self.assertEqual(history_payload["items"][0]["run_id"], "run-brief-1")

            detail_resp = client.get("/brief/history/run-brief-1", headers={"X-Monitor-Token": "secret-token"})
            self.assertEqual(detail_resp.status_code, 200)
            detail_payload = detail_resp.json()
            self.assertIn("Hello world", detail_payload["brief_text"])
            self.assertEqual(detail_payload["brief_md_path"], str(persisted.brief_md_path))


if __name__ == "__main__":
    unittest.main()
