[中文](./README.zh-CN.md)

# Signal Monitoring Agent

Signal Monitoring Agent is a local-first monitoring system for topic-based intelligence workflows.

You define a topic, attach trusted sources, and let the system:
- ingest new content from RSS and webpages
- extract structured signals with an LLM
- deduplicate at the event level
- rank and filter results
- generate a readable briefing
- deliver results through Telegram, DingTalk, or API

It is designed for single-machine operation first, with a modular architecture that can later be integrated into external agent systems.

It can run standalone, or act as a monitoring backend for external agent frameworks through its REST API and tool-ready schema.

## Core Capabilities

- topic-based monitoring with per-topic source buckets
- RSS ingestion with second-hop article fetch
- webpage ingestion with HTML parser first and Playwright fallback
- time-aware freshness gating (`fresh`, `recent`, `stale`, `unknown`)
- event-level deduplication with embeddings + LLM decisioning
- user inbox handling for submitted links and notes
- Markdown briefings plus structured JSON outputs
- Telegram outbound and inbound support
- FastAPI server with Config UI and Brief UI
- REST API for ingest, run control, and strategy workflows
- tool-ready schema for external agent integration
- local persistent storage for briefs, signals, runs, events, and source state

## Interfaces

Web UI:
- `GET /config/ui` - configure topics, sources, models, channels, and source overrides
- `GET /brief/ui` - read the latest and historical briefings

API:
- `GET /signals/latest`
- `GET /brief/latest`
- `GET /brief/latest/audio`
- `POST /ingest`
- `POST /run_now`
- `POST /strategy/generate`
- `POST /strategy/preview`
- `POST /strategy/source/suggest`
- `POST /strategy/deploy`
- `POST /strategy/patch`
- `POST /strategy/get`
- `POST /strategy/history`
- `GET /sources/advisories/latest`

Agent integration:
- tool-ready schema: `GET /tool/schema`
- OpenClaw-style external orchestration is supported through the API layer and tool schema shape

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env
cp config/config.example.yaml config/config.local.yaml
scripts/run_local.sh preflight
scripts/run_local.sh once
scripts/run_local.sh api
scripts/run_local.sh scheduled
```

Edit:
- `.env`
- `config/config.local.yaml`

Local UIs:
- Config UI: [http://127.0.0.1:8080/config/ui](http://127.0.0.1:8080/config/ui)
- Brief UI: [http://127.0.0.1:8080/brief/ui](http://127.0.0.1:8080/brief/ui)

## Local Operation

Helper modes:
- `preflight`: validate config, storage, browser runtime, model endpoints, and notification credentials
- `once`: run one monitoring cycle immediately
- `api`: start FastAPI service; scheduler obeys `api.scheduler_enabled`
- `scheduled`: run the local scheduler loop
- `pw-login`: open a headed browser once for manual login/session bootstrap

## Outputs

The system writes local artifacts under `data/`:
- `data/briefs/` — Markdown and JSON briefings
- `data/signals/` — structured selected signals
- `data/audio/` — MP3 files when TTS is enabled
- `data/runs/` — run-level debug bundles
- `data/summaries/` — daily summary JSONL rows
- `data/events/` — canonical event history and LLM dedup cache
- `data/source_cursors.json` — source-level incremental ingestion state

Key pipeline behavior:
- source-level incremental ingestion reduces repeat fetching
- RSS and webpage sources keep small overlap windows to avoid missing newly published items
- source refresh intervals are auto-managed in the backend
- source advisories are generated to suggest better source entries or safer overrides, but the system does not auto-delete your sources

## Security and Privacy

Safe defaults:
- API host defaults to `127.0.0.1`
- remote access requires `MONITOR_API_TOKEN`
- inbound chat channels are allowlisted
- private/internal URLs are blocked from user-submitted fetches
- user-submitted URLs do not rely on Playwright fallback unless explicitly enabled
- source advisories never auto-delete or auto-rewrite your sources

Do not commit:
- `.env`
- `MONITOR_API_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_ALLOWED_CHAT_IDS`
- `DINGTALK_*`
- `config/config.local.yaml`
- `data/`

Before publishing or sharing the repository, read:
- [SECURITY.md](./SECURITY.md)

## Development

```bash
python -m unittest discover -q
scripts/open_source_audit.sh
```

Additional docs:
- contribution guide: [CONTRIBUTING.md](./CONTRIBUTING.md)
- social post draft: [docs/social-post.zh.md](./docs/social-post.zh.md)

## Status

This codebase is in a strong local-MVP state for single-machine usage.

It is optimized for:
- local operation
- explainable modularity
- extensibility toward additional sources and delivery channels

It is not yet positioned as a multi-tenant hosted product.
