# Local Safety Notes

This document covers the intended safe-default local posture for Signal Monitoring Agent.

## Safe defaults

- API host defaults to `127.0.0.1`
- remote access requires `MONITOR_API_TOKEN`
- Telegram inbound is restricted by allowlisted chat IDs
- DingTalk inbound is disabled unless explicitly configured
- user-submitted URLs are blocked from private/internal hosts
- source advisories never auto-delete or auto-rewrite your sources

## Keep these local

Do not commit these values:
- `.env`
- `MONITOR_API_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_ALLOWED_CHAT_IDS`
- `DINGTALK_*`
- `config/config.local.yaml`
- `data/`

## Telegram inbound

Telegram can be used as an input channel only when:
- `api.telegram_ingest_enabled=true`
- `TELEGRAM_BOT_TOKEN` is set
- the incoming chat is allowed

Recommended local setup:
- one private chat with the bot
- one `TELEGRAM_CHAT_ID`
- `TELEGRAM_ALLOWED_CHAT_IDS` unset or restricted to that same chat

## User-submitted URLs

User-submitted URLs should be treated as untrusted input.

Current protections:
- private/internal hosts are blocked
- HTML parser is preferred first
- Playwright fallback for user-submitted URLs should remain disabled unless you explicitly need it

## Source advisories

Source advisories are operational hints, not destructive actions.

They may suggest:
- a healthier RSS endpoint
- a webpage fallback
- forcing HTML or Playwright
- using a more conservative refresh policy

They do not automatically:
- delete a source
- replace a source
- rewrite your strategy without your confirmation

## Recommended open-source hygiene

Before publishing:
- remove or ignore local runtime artifacts
- keep only `.env.example`
- keep only sanitized example config files
- run `scripts/open_source_audit.sh`

## Restart-sensitive settings

After changing these, restart the running service:
- API host/port
- scheduler enablement
- Telegram inbound enablement
- DingTalk inbound enablement
- related credentials

The UI reports `restart_required` when applicable.
