#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://127.0.0.1:8080}"
TARGET_CONFIG="${TARGET_CONFIG:-./config/config.local.yaml}"
TIMEZONE="${TIMEZONE:-Asia/Shanghai}"
SCHEDULE_TIMES="${SCHEDULE_TIMES:-[\"07:00\",\"22:00\"]}"
RUN_NOW_AFTER_DEPLOY="${RUN_NOW_AFTER_DEPLOY:-true}"

DEFAULT_REQUEST="监控 AI infra，重点关注 GPU 供给、云厂商 capex、Blackwell 交付、推理成本变化，并优先跟踪 NVIDIA、AMD、AWS、Google Cloud、CoreWeave、Anthropic、OpenAI 动态。"
USER_REQUEST="${1:-$DEFAULT_REQUEST}"

echo "== Strategy Flow =="
echo "API_URL=${API_URL}"
echo "TARGET_CONFIG=${TARGET_CONFIG}"
echo

echo "[1/5] health check"
curl -sSf "${API_URL}/health" >/dev/null
echo "ok"

echo "[2/5] strategy generate"
GEN_PAYLOAD="$(python - <<PY
import json
print(json.dumps({
    "user_request": """$USER_REQUEST""",
    "timezone": "$TIMEZONE",
    "schedule_times": json.loads("""$SCHEDULE_TIMES""")
}, ensure_ascii=False))
PY
)"
curl -sSf "${API_URL}/strategy/generate" \
  -H "Content-Type: application/json" \
  -d "${GEN_PAYLOAD}" \
  >/tmp/strategy_generate.json
python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("/tmp/strategy_generate.json").read_text(encoding="utf-8"))
intent = payload.get("parsed_intent", {})
print("generated domain:", intent.get("domain"))
print("focus areas:", ", ".join(intent.get("focus_areas", [])[:4]))
PY

echo "[3/5] strategy get (should now be persisted)"
curl -sSf "${API_URL}/strategy/get" \
  -H "Content-Type: application/json" \
  -d '{}' \
  >/tmp/strategy_get.json
python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("/tmp/strategy_get.json").read_text(encoding="utf-8"))
strategy = payload.get("strategy")
if not strategy:
    raise SystemExit("No persisted strategy found after generate.")
print("stored version:", strategy.get("version"))
print("pending_deploy:", strategy.get("pending_deploy"))
PY

echo "[4/5] deploy current strategy"
DEPLOY_PAYLOAD="$(python - <<PY
import json
print(json.dumps({
    "deploy_current": True,
    "confirm": True,
    "target_config_path": "$TARGET_CONFIG",
    "overwrite": True
}, ensure_ascii=False))
PY
)"
curl -sSf "${API_URL}/strategy/deploy" \
  -H "Content-Type: application/json" \
  -d "${DEPLOY_PAYLOAD}" \
  >/tmp/strategy_deploy.json
python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("/tmp/strategy_deploy.json").read_text(encoding="utf-8"))
print(payload.get("message", "deploy done"))
print("deployed_path:", payload.get("deployed_path"))
PY

if [[ "${RUN_NOW_AFTER_DEPLOY}" == "true" ]]; then
  echo "[5/5] run_now"
  curl -sSf -X POST "${API_URL}/run_now" >/tmp/run_now.json
  python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("/tmp/run_now.json").read_text(encoding="utf-8"))
print("run_id:", payload.get("run_id"))
print("status:", payload.get("status"))
print("signal_count:", payload.get("signal_count"))
PY
else
  echo "[5/5] run_now skipped (RUN_NOW_AFTER_DEPLOY=${RUN_NOW_AFTER_DEPLOY})"
fi

echo
echo "Done."
