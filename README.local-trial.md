# Local Trial Guide

This guide is for a 3-5 day single-machine trial.

## 1. One-time setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env
cp config/config.example.yaml config/config.local.yaml
```

## 2. Fill local secrets and config

Edit `.env`:
- `OPENAI_API_KEY`
- optional `EMBEDDING_API_KEY`
- optional `TELEGRAM_BOT_TOKEN`
- optional `TELEGRAM_CHAT_ID`

Edit `config/config.local.yaml`:
- topic/domain
- source links
- schedule
- notification channel

## 3. Preflight

```bash
scripts/run_local.sh preflight
```

## 4. Run modes

```bash
scripts/run_local.sh once
scripts/run_local.sh api
scripts/run_local.sh scheduled
scripts/run_local.sh pw-login --url https://example.com/login
```

## 5. Local UIs

- Config UI: [http://127.0.0.1:8080/config/ui](http://127.0.0.1:8080/config/ui)
- Brief UI: [http://127.0.0.1:8080/brief/ui](http://127.0.0.1:8080/brief/ui)

## 6. What to inspect after each run

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

## 7. Suggested trial cadence

Day 1:
- validate source coverage
- validate notification formatting
- confirm incremental ingestion is working

Day 2-3:
- tune source list
- tune notification channel
- use Telegram inbound if desired

Day 4-5:
- let scheduled mode run naturally
- review briefing quality, source advisories, and stale/drop rates

## 8. Notes

- TTS is disabled by default.
- Brief UI is read-only in the current version.
- Source advisories are suggestions only; the system does not auto-delete sources.
- For local-only use, keep API bound to `127.0.0.1`.
