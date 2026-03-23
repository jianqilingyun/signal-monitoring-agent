from __future__ import annotations

from typing import Any

from monitor_agent.core.webhooks import WebhookSubscribeRequest
from monitor_agent.inbox_engine import IngestRequest
from monitor_agent.strategy_engine.models import (
    SourceStrategySuggestRequest,
    StrategyDeployRequest,
    StrategyGenerateRequest,
    StrategyGetRequest,
    StrategyHistoryRequest,
    StrategyPatchRequest,
    StrategyPreviewRequest,
)


def build_tool_schema() -> dict[str, Any]:
    """Agent-friendly tool descriptor schema (OpenClaw-compatible JSON schema shape)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Signal Monitoring Agent Tool Interface",
        "type": "object",
        "required": ["schema_version", "tools"],
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "tools": {
                "type": "array",
                "items": {"$ref": "#/$defs/tool"},
            },
        },
        "$defs": {
            "tool": {
                "type": "object",
                "required": ["name", "description", "http", "input_schema"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "http": {
                        "type": "object",
                        "required": ["method", "path"],
                        "properties": {
                            "method": {"type": "string", "enum": ["GET", "POST", "DELETE"]},
                            "path": {"type": "string"},
                        },
                    },
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                },
            }
        },
        "schema_version": "1.0",
        "tools": [
            {
                "name": "get_latest_signals",
                "description": "Fetch latest structured signals.",
                "http": {"method": "GET", "path": "/signals/latest"},
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": ["string", "null"]},
                        "signal_count": {"type": "integer"},
                        "signals": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "title": {"type": "string"},
                                    "summary": {"type": "string"},
                                    "importance": {"type": "number"},
                                    "freshness": {"type": "string", "enum": ["fresh", "recent", "stale"]},
                                    "publish_time": {"type": ["string", "null"]},
                                    "age_hours": {"type": "number"},
                                    "event_id": {"type": ["string", "null"]},
                                    "event_type": {"type": "string", "enum": ["new", "update", "duplicate"]},
                                    "source": {"type": "string", "enum": ["system", "user"]},
                                    "source_urls": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["id", "title", "summary", "importance", "source"],
                            },
                        },
                    },
                },
            },
            {
                "name": "get_latest_brief",
                "description": "Fetch latest human-readable text briefing.",
                "http": {"method": "GET", "path": "/brief/latest"},
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "string"},
            },
            {
                "name": "generate_strategy",
                "description": "Generate explainable strategy and deterministic monitoring config from natural language intent OR simple UI payload (domain/focus/entities/keywords/source_links).",
                "http": {"method": "POST", "path": "/strategy/generate"},
                "input_schema": StrategyGenerateRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "required": ["strategy_text", "config_yaml", "parsed_intent", "domain_mapping"],
                },
            },
            {
                "name": "preview_strategy",
                "description": "Preview normalized strategy config and human-readable summary without persisting or deploying.",
                "http": {"method": "POST", "path": "/strategy/preview"},
                "input_schema": StrategyPreviewRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "required": ["summary", "normalized_config", "strategy_text"],
                },
            },
            {
                "name": "suggest_source_strategy",
                "description": "Suggest URL-level ingestion strategy (LLM-first, cached incrementally) with refresh interval control.",
                "http": {"method": "POST", "path": "/strategy/source/suggest"},
                "input_schema": SourceStrategySuggestRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "required": ["refresh_interval_days", "total_urls", "reused_cached", "recomputed", "suggestions"],
                },
            },
            {
                "name": "deploy_strategy",
                "description": "Deploy monitoring config. Requires confirm=true; supports deploy_current, fresh generation, or incremental modification_request patching.",
                "http": {"method": "POST", "path": "/strategy/deploy"},
                "input_schema": StrategyDeployRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "required": ["deployed", "deployed_path", "monitor_config_valid"],
                },
            },
            {
                "name": "patch_strategy",
                "description": "Apply an incremental natural-language patch to the stored strategy without full regeneration.",
                "http": {"method": "POST", "path": "/strategy/patch"},
                "input_schema": StrategyPatchRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "required": ["patch", "version", "previous_version", "pending_deploy", "changes"],
                },
            },
            {
                "name": "get_strategy",
                "description": "Get the current stored strategy (or a specific version when available).",
                "http": {"method": "POST", "path": "/strategy/get"},
                "input_schema": StrategyGetRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "strategy": {"type": ["object", "null"]},
                        "message": {"type": ["string", "null"]},
                    },
                },
            },
            {
                "name": "strategy_history",
                "description": "Get strategy version history with tracked changes.",
                "http": {"method": "POST", "path": "/strategy/history"},
                "input_schema": StrategyHistoryRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "properties": {"entries": {"type": "array"}},
                },
            },
            {
                "name": "trigger_ingestion",
                "description": "Run ingestion only across configured sources.",
                "http": {"method": "POST", "path": "/ingest"},
                "input_schema": IngestRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "raw_items_count": {"type": "integer"},
                        "user_signals_count": {"type": "integer"},
                    },
                },
            },
            {
                "name": "trigger_full_run",
                "description": "Trigger a full run now (ingest, extract, filter, brief, notify).",
                "http": {"method": "POST", "path": "/run_now"},
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "status": {"type": "string"},
                        "signal_count": {"type": "integer"},
                    },
                },
            },
            {
                "name": "subscribe_webhook",
                "description": "Subscribe an external webhook endpoint to new signals/briefings/run completion events.",
                "http": {"method": "POST", "path": "/webhooks/subscribe"},
                "input_schema": WebhookSubscribeRequest.model_json_schema(),
                "output_schema": {
                    "type": "object",
                    "required": ["id", "url", "events", "enabled"],
                },
            },
        ],
    }
