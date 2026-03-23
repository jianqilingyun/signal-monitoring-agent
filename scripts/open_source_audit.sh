#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FAILED=0

check_file_exists() {
  local path="$1"
  local message="$2"
  if [[ -e "$path" ]]; then
    echo "[WARN] $message: $path"
    FAILED=1
  else
    echo "[PASS] not present: $path"
  fi
}

check_grep_clean() {
  local pattern="$1"
  local message="$2"
  if rg -n --hidden \
      --glob '!**/.git/**' \
      --glob '!data/**' \
      --glob '!.venv/**' \
      --glob '!venv/**' \
      --glob '!**/__pycache__/**' \
      --glob '!.env' \
      --glob '!.env.example' \
      --glob '!scripts/open_source_audit.sh' \
      --glob '!tests/test_logging_security.py' \
      -- "$pattern" . >/tmp/monitor_audit_hits.txt; then
    echo "[WARN] $message"
    cat /tmp/monitor_audit_hits.txt
    FAILED=1
  else
    echo "[PASS] $message"
  fi
}

echo "== Open Source Audit =="

check_file_exists ".env" "local secret file should not be published"
check_file_exists "config/config.local.yaml" "local config should not be published"

if [[ -d data ]] && [[ -n "$(find data -type f -print -quit)" ]]; then
  echo "[WARN] runtime data directory contains files"
  find data -maxdepth 2 -type f | sed -n '1,40p'
  FAILED=1
else
  echo "[PASS] runtime data directory is clean"
fi

check_grep_clean 'sk-[A-Za-z0-9]+' 'no obvious OpenAI-style secret found outside ignored files'
check_grep_clean 'api\.telegram\.org/bot[0-9]+:[A-Za-z0-9_-]+' 'no Telegram bot token found outside ignored files'
check_grep_clean '-----BEGIN (RSA|EC|OPENSSH|DSA|PRIVATE KEY)' 'no PEM private key found outside ignored files'
check_grep_clean '/Users/[^/]+/' 'no hard-coded local absolute macOS home path found outside ignored files'

if [[ "$FAILED" -ne 0 ]]; then
  echo
  echo "Audit finished with warnings. Review before publishing."
  exit 1
fi

echo

echo "Audit passed. Repository looks safe to review for publication."
