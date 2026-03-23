# Contributing

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env
cp config/config.example.yaml config/config.local.yaml
```

## Run tests

```bash
python -m unittest discover -q
```

## Local operation

```bash
scripts/run_local.sh preflight
scripts/run_local.sh once
scripts/run_local.sh api
scripts/run_local.sh scheduled
```

## Contribution guidelines

- keep the system modular
- prefer small, explicit changes over broad refactors
- do not commit `.env`, `data/`, or local config files
- preserve local-MVP usability
- keep API and storage behavior deterministic where possible
- add tests for behavior changes, especially in ingestion, dedup, briefing, and config handling

## Areas where regressions are especially costly

- source ingestion and incremental state
- event deduplication
- briefing output structure
- notification formatting
- Telegram inbound behavior
- config UI save/load semantics

## Before opening a PR

```bash
python -m unittest discover -q
scripts/open_source_audit.sh
```
