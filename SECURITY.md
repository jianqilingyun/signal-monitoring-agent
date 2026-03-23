# Security

## Supported deployment posture

This project is currently intended for local or tightly controlled single-operator deployments.

It is not advertised as a hardened multi-tenant hosted service.

## Secret handling

Never commit:
- `.env`
- local config files with real credentials
- runtime `data/` artifacts
- browser profile/session data
- generated debug bundles

Use:
- `.env.example`
- `config/config.example.yaml`

## Local safe defaults

- API binds to `127.0.0.1` by default
- remote API usage requires `MONITOR_API_TOKEN`
- inbound chat channels require explicit allowlists
- private/internal hosts are blocked from untrusted URL ingestion
- source advisories are suggestions only and never auto-delete or auto-rewrite your sources

## Local operator guidance

Telegram inbound should only be enabled when:
- `api.telegram_ingest_enabled=true`
- `TELEGRAM_BOT_TOKEN` is set
- the incoming chat is explicitly allowed

Recommended local setup:
- one private chat with the bot
- one `TELEGRAM_CHAT_ID`
- `TELEGRAM_ALLOWED_CHAT_IDS` unset or restricted to that same chat

User-submitted URLs should be treated as untrusted input.

Current protections:
- private/internal hosts are blocked
- HTML parser is preferred first
- Playwright fallback for user-submitted URLs should remain disabled unless explicitly needed

Restart the running service after changing:
- API host/port
- scheduler enablement
- Telegram inbound enablement
- DingTalk inbound enablement
- related credentials

## Reporting

If you find a security issue:
- do not publish working exploit details in a public issue
- report the issue privately to the maintainer before public disclosure
