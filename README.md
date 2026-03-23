[中文](./README.zh-CN.md)

# Signal Monitoring Agent

Signal Monitoring Agent is a modular monitoring system for topic-based intelligence workflows.

You define a topic, attach trusted sources, and let the system:
- ingest new content from RSS and webpages
- extract structured signals with an LLM
- deduplicate at the event level
- rank and filter results
- generate a readable briefing
- deliver the result through Telegram, DingTalk, or API

The system is designed to run locally as a single-machine service and can later be integrated into external agent systems.

## What It Does

Core capabilities:
- topic-based monitoring with per-topic source buckets
- RSS ingestion with second-hop article fetch
- webpage ingestion with HTML parser first and Playwright fallback
- time-aware freshness gating (`fresh`, `recent`, `stale`, `unknown`)
- event-level deduplication with embeddings + LLM decisioning
- inbox handling for user-submitted links and notes
- briefing generation in Markdown plus structured JSON outputs
- Telegram outbound and inbound support
- FastAPI server for manual runs, ingest, strategy generation, config UI, and brief UI
- local persistent storage for briefs, signals, audio, run artifacts, and source state

## Architecture

```text
monitor_agent/
  api/                 FastAPI server + lightweight UIs
  briefing/            Brief generation and localization
  core/                Config, pipeline, storage, scheduler, logging
  ingestion_layer/     RSS / HTML / Playwright ingestion
  inbound/             Telegram / DingTalk input listeners
  notifier/            Telegram / DingTalk outbound
  signal_engine/       LLM signal extraction
  strategy_engine/     Strategy generation and source suggestions
  tts/                 Audio generation
  filter_engine/       Ranking and final selection
  time_engine.py       Freshness classification
  llm_dedup_engine.py  Event-level SAME/UPDATE/DIFFERENT decisions
  inbox_engine.py      User inbox persistence and tracking
  priority_engine.py   Final score calculation
  event_store.py       Canonical event history
```

## Current Product Model

The primary user-facing concepts are:
- `Topic`: what you care about
- `Sources`: where the system should look
- `Channel`: where the result should be delivered

Advanced fields such as focus areas, entities, and keywords still exist internally, but they are treated as optional refinements rather than the primary configuration surface.

## Interfaces

Web UI:
- `GET /config/ui` — configure topics, sources, models, channels, and source overrides
- `GET /brief/ui` — read the latest and historical briefings

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

## Quick Start

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

### 2. Create local config

```bash
cp .env.example .env
cp config/config.example.yaml config/config.local.yaml
```

Edit:
- `.env`
- `config/config.local.yaml`

### 3. Run startup checks

```bash
scripts/run_local.sh preflight
```

### 4. Run locally

```bash
scripts/run_local.sh once
scripts/run_local.sh api
scripts/run_local.sh scheduled
```

### 5. Open the local UIs

- Config UI: [http://127.0.0.1:8080/config/ui](http://127.0.0.1:8080/config/ui)
- Brief UI: [http://127.0.0.1:8080/brief/ui](http://127.0.0.1:8080/brief/ui)

## Local Operation Modes

Use the helper script:

```bash
scripts/run_local.sh preflight
scripts/run_local.sh once
scripts/run_local.sh api
scripts/run_local.sh scheduled
scripts/run_local.sh pw-login --url https://example.com/login
```

Mode summary:
- `preflight`: validate config, storage, browser runtime, model endpoints, and notification credentials
- `once`: run one monitoring cycle immediately
- `api`: start FastAPI service; scheduler obeys `api.scheduler_enabled`
- `scheduled`: run the local scheduler loop
- `pw-login`: open a headed browser once for manual login/session bootstrap

## Local Trial Notes

Recommended 3-5 day trial flow:
- day 1: validate source coverage and notification formatting
- day 2-3: tune source list and confirm incremental ingestion is working
- day 4-5: let scheduled mode run naturally and review briefing quality, stale/drop rates, and source advisories

Primary outputs:
- `data/briefs/`
- `data/signals/`
- `data/audio/` when TTS is enabled

Run-level debug:
- `data/runs/<run_id>/debug/selected_inputs.json`
- `data/runs/<run_id>/debug/extracted_signals.json`
- `data/runs/<run_id>/debug/source_incremental_stats.json`
- `data/runs/<run_id>/debug/source_health_stats.json`
- `data/runs/<run_id>/debug/source_advisories.json`

Operational logs:
- `data/logs/monitor_agent.log`
- `data/summaries/YYYY-MM-DD.jsonl`

## Outputs

The system writes local artifacts under `data/`:
- `data/briefs/` — Markdown and JSON briefings
- `data/signals/` — structured selected signals
- `data/audio/` — MP3 files when TTS is enabled
- `data/runs/` — run-level debug bundles
- `data/summaries/` — daily summary JSONL rows
- `data/events/` — canonical event history and LLM dedup cache
- `data/source_cursors.json` — source-level incremental ingestion state

## Inbound and Outbound Channels

Outbound:
- Telegram
- DingTalk

Inbound:
- Telegram
- DingTalk (if configured)
- API `POST /ingest`

User input behavior:
- `/brief`: summarize the submitted item immediately; does not enter later scheduled briefing
- `/save`: store the item for later scheduled briefing; it is surfaced once and not repeated every day

## Ingestion and Filtering Pipeline

High-level flow:

```text
source ingestion
  -> time extraction and freshness gating
  -> early stale drop
  -> embedding candidate retrieval
  -> LLM event relation decision (same / update / different)
  -> priority scoring
  -> final selection
  -> briefing generation
  -> notification / storage
```

Important implementation points:
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

Run tests:

```bash
python -m unittest discover -q
```

Optional release audit:

```bash
scripts/open_source_audit.sh
```

More details:
- contribution guide: [CONTRIBUTING.md](./CONTRIBUTING.md)
- social post draft: [docs/social-post.zh.md](./docs/social-post.zh.md)

## Status

This codebase is in a strong local-MVP state for single-machine usage.

It is intentionally optimized for:
- local operation
- explainable modularity
- extensibility toward additional sources and delivery channels

It is not yet positioned as a multi-tenant hosted product.
