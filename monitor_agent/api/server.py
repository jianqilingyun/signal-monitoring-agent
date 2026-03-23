from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Any
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from monitor_agent.api.brief_panel import BRIEF_PANEL_HTML
from monitor_agent.api.config_panel import CONFIG_PANEL_HTML, ConfigPanelSaveRequest, ConfigPanelService
from monitor_agent.api.tool_schema import build_tool_schema
from monitor_agent.core.config import load_config
from monitor_agent.core.pipeline import MonitoringPipeline
from monitor_agent.core.storage import Storage
from monitor_agent.core.utils import utc_now
from monitor_agent.core.webhooks import WebhookManager, WebhookSubscribeRequest
from monitor_agent.core.models import MonitorConfig
from monitor_agent.inbox_engine import IngestRequest, InboxEngine
from monitor_agent.core.url_safety import is_loopback_host
from monitor_agent.strategy_engine.models import (
    SourceStrategySuggestRequest,
    StrategyDeployRequest,
    StrategyGenerateRequest,
    StrategyGetRequest,
    StrategyHistoryRequest,
    StrategyPatchRequest,
    StrategyPreviewRequest,
)
from monitor_agent.strategy_engine.service import StrategyEngine

logger = logging.getLogger(__name__)


class ApiServer:
    def __init__(
        self,
        app: FastAPI,
        pipeline: MonitoringPipeline,
        storage: Storage,
        webhook_manager: WebhookManager,
        inbox_engine: InboxEngine,
    ) -> None:
        self.app = app
        self.pipeline = pipeline
        self.storage = storage
        self.webhook_manager = webhook_manager
        self.inbox_engine = inbox_engine
        self._register_routes()

    def _new_strategy_engine(self) -> StrategyEngine:
        # API layer stays stateless; state lives in core config/storage.
        return StrategyEngine(base_config=self.pipeline.config, storage=self.storage)

    def _register_routes(self) -> None:
        config_panel = ConfigPanelService()

        def _require_access(req: Request) -> None:
            client_host = req.client.host if req.client else ""
            if is_loopback_host(client_host):
                return

            expected = os.getenv("MONITOR_API_TOKEN", "").strip()
            provided = (req.headers.get("X-Monitor-Token") or "").strip()
            if not provided:
                auth = (req.headers.get("Authorization") or "").strip()
                if auth.lower().startswith("bearer "):
                    provided = auth[7:].strip()
            if not expected:
                raise HTTPException(status_code=403, detail="Remote API access requires MONITOR_API_TOKEN")
            if provided != expected:
                raise HTTPException(status_code=401, detail="Invalid API token")

        @self.app.get("/health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        @self.app.get("/config/ui", response_class=HTMLResponse)
        def config_ui() -> str:
            return CONFIG_PANEL_HTML

        @self.app.get("/brief/ui", response_class=HTMLResponse)
        def brief_ui() -> str:
            return BRIEF_PANEL_HTML

        @self.app.get("/config/editor/load")
        def config_editor_load(req: Request) -> dict[str, Any]:
            _require_access(req)
            try:
                return config_panel.load_state()
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Config load failed: {exc}") from exc

        @self.app.post("/config/editor/save")
        def config_editor_save(req: Request, request: ConfigPanelSaveRequest) -> dict[str, Any]:
            _require_access(req)
            try:
                saved = config_panel.save_state(request)
                refreshed = load_config(str(config_panel.config_path))
                self.pipeline.update_config(refreshed)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Config save failed: {exc}") from exc
            return saved

        @self.app.get("/signals/latest")
        def latest_signals(req: Request) -> dict[str, Any]:
            _require_access(req)
            manifest = self.storage.load_latest_manifest()
            signals = self.storage.load_latest_signals()
            if manifest is None:
                return {
                    "run_id": None,
                    "signal_count": 0,
                    "signals": [],
                    "status": "empty",
                }
            return {
                "run_id": manifest.run_id,
                "status": manifest.status,
                "started_at": manifest.started_at,
                "finished_at": manifest.finished_at,
                "signal_count": manifest.signal_count,
                "signals": signals,
                "errors": manifest.errors,
            }

        @self.app.get("/sources/advisories/latest")
        def latest_source_advisories(req: Request) -> dict[str, Any]:
            _require_access(req)
            manifest = self.storage.load_latest_manifest()
            advisories = self.storage.load_latest_source_advisories()
            if manifest is None:
                return {
                    "run_id": None,
                    "count": 0,
                    "advisories": [],
                    "status": "empty",
                }
            return {
                "run_id": manifest.run_id,
                "status": manifest.status,
                "count": len(advisories),
                "advisories": advisories,
            }

        @self.app.get("/brief/latest", response_class=PlainTextResponse)
        def latest_brief(req: Request) -> str:
            _require_access(req)
            brief = self.storage.load_latest_brief()
            if not brief:
                raise HTTPException(status_code=404, detail="No briefing available")
            return brief

        @self.app.get("/brief/latest/audio")
        def latest_brief_audio(req: Request) -> FileResponse:
            _require_access(req)
            manifest = self.storage.load_latest_manifest()
            if manifest is None:
                raise HTTPException(status_code=404, detail="No audio briefing available")
            audio_path = manifest.persistent_audio_path or manifest.brief_audio_path
            if not audio_path:
                raise HTTPException(status_code=404, detail="No audio briefing available")
            path = Path(audio_path)
            if not path.exists():
                raise HTTPException(status_code=404, detail="No audio briefing available")
            return FileResponse(path=path, media_type="audio/mpeg", filename=path.name)

        @self.app.get("/brief/history")
        def brief_history(req: Request, limit: int = 20) -> dict[str, Any]:
            _require_access(req)
            items = self._load_brief_history(limit=limit)
            return {"count": len(items), "items": items}

        @self.app.get("/brief/history/{run_id}")
        def brief_history_item(req: Request, run_id: str) -> dict[str, Any]:
            _require_access(req)
            row = self._load_brief_history_item(run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Brief history item not found")
            return row

        @self.app.post("/ingest")
        def ingest(req: Request, request: IngestRequest | None = None) -> dict[str, Any]:
            _require_access(req)
            ingest_request = request or IngestRequest()
            resolved_user_signals = self.inbox_engine.resolve_user_signals(ingest_request.user_signals)
            stored_user_signals = [item for item in resolved_user_signals if item.ingest_mode != "brief_now"]
            brief_now_user_signals = [item for item in resolved_user_signals if item.ingest_mode == "brief_now"]
            user_added = self.inbox_engine.ingest_user_signals(stored_user_signals)

            immediate_briefs: list[dict[str, Any]] = []
            if self.pipeline is not None:
                for item in brief_now_user_signals:
                    try:
                        immediate_brief = self.pipeline.brief_user_signal(item)
                        immediate_briefs.append(immediate_brief)
                    except Exception as exc:
                        logger.exception("Immediate user brief failed")
                        immediate_briefs.append({"errors": [str(exc)]})

            if ingest_request.run_system_ingestion and not ingest_request.user_signals:
                result = self.pipeline.ingest_only()
                run_id = result.run_id
                raw_items = result.items
                errors = result.errors
            else:
                run_id = f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}_inbox_only"
                raw_items = []
                errors = []

            return {
                "run_id": run_id,
                "raw_items_count": len(raw_items),
                "user_signals_count": len(user_added),
                "tracking_ids": [s.tracking_id for s in user_added if s.tracking_id],
                "errors": errors,
                "items": [item.model_dump(mode="json") for item in raw_items],
                "user_signals": [item.model_dump(mode="json") for item in user_added],
                "brief_now_count": len(brief_now_user_signals),
                "immediate_briefs": immediate_briefs,
            }

        @self.app.post("/run_now")
        def run_now(req: Request) -> dict[str, Any]:
            _require_access(req)
            try:
                manifest = self.pipeline.run_once(trigger="api")
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return manifest.model_dump(mode="json")

        @self.app.post("/strategy/generate")
        def strategy_generate(req: Request, request: StrategyGenerateRequest) -> dict[str, Any]:
            _require_access(req)
            try:
                payload = request.model_dump(mode="json")
                advanced = payload.get("advanced_settings")
                if not isinstance(advanced, dict):
                    advanced = {}
                advanced["auto_source_diagnosis"] = True
                payload["advanced_settings"] = advanced
                effective_request = StrategyGenerateRequest.model_validate(payload)
                result = self._new_strategy_engine().generate(effective_request)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("Strategy generation failed")
                raise HTTPException(status_code=500, detail=f"Strategy generation failed: {exc}") from exc
            return result.model_dump(mode="json")

        @self.app.post("/strategy/preview")
        def strategy_preview(req: Request, request: StrategyPreviewRequest) -> dict[str, Any]:
            _require_access(req)
            try:
                result = self._new_strategy_engine().preview(request)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("Strategy preview failed")
                raise HTTPException(status_code=500, detail=f"Strategy preview failed: {exc}") from exc
            return result.model_dump(mode="json")

        @self.app.post("/strategy/source/suggest")
        def strategy_source_suggest(req: Request, request: SourceStrategySuggestRequest) -> dict[str, Any]:
            _require_access(req)
            try:
                result = self._new_strategy_engine().suggest_source_strategies(request)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("Source strategy suggestion failed")
                raise HTTPException(status_code=500, detail=f"Source strategy suggestion failed: {exc}") from exc
            return result.model_dump(mode="json")

        @self.app.post("/strategy/deploy")
        def strategy_deploy(req: Request, request: StrategyDeployRequest) -> dict[str, Any]:
            _require_access(req)
            try:
                result = self._new_strategy_engine().deploy(request)
                self.pipeline.update_config(MonitorConfig.model_validate(result.generation.config_object))
            except FileExistsError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("Strategy deploy failed")
                raise HTTPException(status_code=500, detail=f"Strategy deploy failed: {exc}") from exc
            return result.model_dump(mode="json")

        @self.app.post("/strategy/patch")
        def strategy_patch(req: Request, request: StrategyPatchRequest) -> dict[str, Any]:
            _require_access(req)
            try:
                result = self._new_strategy_engine().patch(request)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("Strategy patch failed")
                raise HTTPException(status_code=500, detail=f"Strategy patch failed: {exc}") from exc
            return result.model_dump(mode="json")

        @self.app.post("/strategy/get")
        def strategy_get(req: Request, request: StrategyGetRequest | None = None) -> dict[str, Any]:
            _require_access(req)
            try:
                result = self._new_strategy_engine().get(request or StrategyGetRequest())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("Strategy get failed")
                raise HTTPException(status_code=500, detail=f"Strategy get failed: {exc}") from exc
            return result.model_dump(mode="json")

        @self.app.post("/strategy/history")
        def strategy_history(req: Request, request: StrategyHistoryRequest | None = None) -> dict[str, Any]:
            _require_access(req)
            try:
                result = self._new_strategy_engine().history(request or StrategyHistoryRequest())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("Strategy history failed")
                raise HTTPException(status_code=500, detail=f"Strategy history failed: {exc}") from exc
            return result.model_dump(mode="json")

        @self.app.get("/tools/schema")
        def tools_schema(req: Request) -> dict[str, Any]:
            _require_access(req)
            return build_tool_schema()

        @self.app.post("/webhooks/subscribe")
        def webhook_subscribe(req: Request, request: WebhookSubscribeRequest) -> dict[str, Any]:
            _require_access(req)
            try:
                sub = self.webhook_manager.subscribe(request)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return sub.model_dump(mode="json")

        @self.app.get("/webhooks/subscriptions")
        def webhook_subscriptions(req: Request) -> list[dict[str, Any]]:
            _require_access(req)
            return [s.model_dump(mode="json") for s in self.webhook_manager.list_subscriptions()]

        @self.app.delete("/webhooks/subscriptions/{subscription_id}")
        def webhook_unsubscribe(req: Request, subscription_id: str) -> dict[str, Any]:
            _require_access(req)
            removed = self.webhook_manager.unsubscribe(subscription_id)
            if not removed:
                raise HTTPException(status_code=404, detail="Subscription not found")
            return {"removed": True, "subscription_id": subscription_id}

    def _load_brief_history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.pipeline.storage_engine.load_history(limit=limit)
        out: list[dict[str, Any]] = []
        for row in rows:
            summary = self._summarize_history_row(row)
            if summary is not None:
                out.append(summary)
        return out

    def _load_brief_history_item(self, run_id: str) -> dict[str, Any] | None:
        for row in self.pipeline.storage_engine.load_history(limit=None):
            if str(row.get("run_id") or "") != run_id:
                continue
            summary = self._summarize_history_row(row)
            if summary is None:
                return None
            brief_md_path = Path(str(row.get("brief_md_path") or ""))
            if not brief_md_path.exists():
                return None
            summary["brief_text"] = brief_md_path.read_text(encoding="utf-8")
            summary["brief_md_path"] = str(brief_md_path)
            summary["brief_json_path"] = str(row.get("brief_json_path") or "")
            summary["signals_json_path"] = str(row.get("signals_json_path") or "")
            return summary
        return None

    @staticmethod
    def _summarize_history_row(row: dict[str, Any]) -> dict[str, Any] | None:
        brief_md_path = Path(str(row.get("brief_md_path") or ""))
        if not brief_md_path.exists():
            return None
        brief_json_path = Path(str(row.get("brief_json_path") or ""))
        signal_count = 0
        if brief_json_path.exists():
            try:
                payload = json.loads(brief_json_path.read_text(encoding="utf-8"))
                signal_count = int(payload.get("signal_count", 0) or 0) if isinstance(payload, dict) else 0
            except Exception:
                signal_count = 0
        audio_token = str(row.get("audio_mp3_path") or "").strip()
        audio_available = bool(audio_token) and Path(audio_token).exists()
        return {
            "run_id": str(row.get("run_id") or ""),
            "domain": str(row.get("domain") or ""),
            "slot": str(row.get("slot") or ""),
            "generated_at": str(row.get("generated_at") or ""),
            "brief_language": str(row.get("brief_language") or "zh"),
            "signal_count": signal_count,
            "audio_available": audio_available,
            "brief_md_path": str(brief_md_path),
            "brief_json_path": str(brief_json_path) if brief_json_path.exists() else "",
            "signals_json_path": str(row.get("signals_json_path") or ""),
        }
