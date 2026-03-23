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

## Reporting

If you find a security issue:
- do not publish working exploit details in a public issue
- report the issue privately to the maintainer before public disclosure

For a local fork, review `README.local-safety.md` before exposing the API outside your machine.
