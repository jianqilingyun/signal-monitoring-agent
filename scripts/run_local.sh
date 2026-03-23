#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

if [[ "${1:-}" == "--config" ]]; then
  if [[ -z "${2:-}" ]]; then
    echo "--config requires a path argument" >&2
    exit 2
  fi
  export MONITOR_CONFIG="${2}"
  shift 2
fi

resolve_config() {
  if [[ -n "${MONITOR_CONFIG:-}" ]]; then
    echo "${MONITOR_CONFIG}"
    return
  fi

  if [[ -f "${ROOT_DIR}/config/config.local.yaml" ]]; then
    echo "${ROOT_DIR}/config/config.local.yaml"
    return
  fi

  if [[ -f "${ROOT_DIR}/config/config.yaml" ]]; then
    echo "${ROOT_DIR}/config/config.yaml"
    return
  fi

  cp "${ROOT_DIR}/config/config.example.yaml" "${ROOT_DIR}/config/config.local.yaml"
  echo "Created ${ROOT_DIR}/config/config.local.yaml from config.example.yaml" >&2
  echo "${ROOT_DIR}/config/config.local.yaml"
}

if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

CONFIG_PATH="$(resolve_config)"
export MONITOR_CONFIG="${CONFIG_PATH}"

cd "${ROOT_DIR}"

case "${MODE}" in
  preflight)
    echo "Running preflight checks with config: ${MONITOR_CONFIG}"
    "${PYTHON_BIN}" -m monitor_agent.local_runner preflight "$@"
    ;;
  once)
    echo "Running one-shot pipeline with config: ${MONITOR_CONFIG}"
    "${PYTHON_BIN}" -m monitor_agent.local_runner once "$@"
    ;;
  api)
    echo "Starting API mode with config: ${MONITOR_CONFIG}"
    "${PYTHON_BIN}" -m monitor_agent.local_runner api "$@"
    ;;
  scheduled)
    echo "Starting scheduled mode with config: ${MONITOR_CONFIG}"
    "${PYTHON_BIN}" -m monitor_agent.local_runner scheduled "$@"
    ;;
  playwright-login|pw-login)
    echo "Starting Playwright login bootstrap with config: ${MONITOR_CONFIG}"
    "${PYTHON_BIN}" -m monitor_agent.local_runner playwright_login "$@"
    ;;
  help|-h|--help)
    cat <<'EOF'
Usage: scripts/run_local.sh <mode> [args...]

Modes:
  preflight  Run startup checks (config/storage/playwright/endpoints/notifications).
  once       Run one monitoring cycle immediately.
  api        Start FastAPI service (scheduler obeys api.scheduler_enabled).
  scheduled  Start local scheduler worker loop.
  playwright-login / pw-login
             Open headed Playwright once for manual login/session bootstrap.

Examples:
  scripts/run_local.sh preflight
  scripts/run_local.sh once
  scripts/run_local.sh api
  scripts/run_local.sh scheduled
  scripts/run_local.sh pw-login --url https://example.com/login
  MONITOR_CONFIG=./config/config.yaml scripts/run_local.sh once --trigger manual
  scripts/run_local.sh once --config ./config/config.local.yaml --trigger manual
EOF
    ;;
  *)
    echo "Unknown mode: ${MODE}" >&2
    echo "Run scripts/run_local.sh --help for usage." >&2
    exit 2
    ;;
esac
