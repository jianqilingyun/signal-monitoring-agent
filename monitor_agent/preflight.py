from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv

from monitor_agent.core.config import load_config
from monitor_agent.core.storage import Storage

CheckStatus = Literal["PASS", "WARN", "FAIL"]


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str


def run_preflight(config_path: str | None = None, timeout_seconds: float = 6.0) -> int:
    load_dotenv()
    results: list[CheckResult] = []

    try:
        config = load_config(config_path)
        results.append(CheckResult("config.load", "PASS", "Config loaded successfully"))
    except Exception as exc:
        results.append(CheckResult("config.load", "FAIL", f"Config load failed: {exc}"))
        _print_results(results)
        return 2

    storage = Storage(config.storage.root_dir)
    results.extend(_check_storage(storage))
    results.extend(_check_sources(config))
    results.extend(_check_playwright())
    results.extend(_check_playwright_extensions(config.playwright.extension_paths))
    results.extend(_check_llm_endpoint(config.llm.base_url, timeout_seconds))
    results.extend(
        _check_embedding_endpoint(
            embedding_base_url=config.llm.embedding_base_url or config.llm.base_url,
            timeout_seconds=timeout_seconds,
        )
    )
    results.extend(_check_tts(config.tts))
    results.extend(_check_notifications(config.notifications))

    _print_results(results)
    if any(item.status == "FAIL" for item in results):
        return 1
    return 0


def _check_storage(storage: Storage) -> list[CheckResult]:
    checks: list[CheckResult] = []
    marker = storage.root / ".preflight_write_test"
    try:
        marker.write_text("ok", encoding="utf-8")
        marker.unlink(missing_ok=True)
        checks.append(CheckResult("storage.write", "PASS", f"Storage writable: {storage.root}"))
    except Exception as exc:
        checks.append(CheckResult("storage.write", "FAIL", f"Storage not writable ({storage.root}): {exc}"))
    return checks


def _check_sources(config) -> list[CheckResult]:
    rss_count = len(config.sources.rss)
    playwright_count = len(config.sources.playwright)
    total = rss_count + playwright_count
    if total == 0:
        return [CheckResult("sources.count", "FAIL", "No sources configured (rss/playwright are both empty)")]
    return [
        CheckResult(
            "sources.count",
            "PASS",
            f"Configured sources: total={total}, rss={rss_count}, playwright={playwright_count}",
        )
    ]


def _check_playwright() -> list[CheckResult]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        return [CheckResult("playwright.import", "FAIL", f"Playwright import failed: {exc}")]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return [CheckResult("playwright.chromium", "PASS", "Chromium launch check succeeded")]
    except Exception as exc:
        return [
            CheckResult(
                "playwright.chromium",
                "FAIL",
                f"Chromium launch failed: {exc}. Try: playwright install chromium",
            )
        ]


def _check_playwright_extensions(extension_paths: list[str]) -> list[CheckResult]:
    if not extension_paths:
        return [CheckResult("playwright.extensions", "PASS", "No extension paths configured")]
    missing: list[str] = []
    for raw in extension_paths:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            missing.append(str(path))
    if missing:
        return [CheckResult("playwright.extensions", "WARN", f"Missing extension path(s): {', '.join(missing)}")]
    return [CheckResult("playwright.extensions", "PASS", f"Extension paths found: {len(extension_paths)}")]


def _check_llm_endpoint(base_url: str | None, timeout_seconds: float) -> list[CheckResult]:
    if not base_url:
        return [CheckResult("llm.endpoint", "WARN", "llm.base_url not set; using provider default endpoint")]

    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return [CheckResult("llm.endpoint", "WARN", "OPENAI_API_KEY is empty; LLM calls may fallback")]

    ok, message = _probe_models(base_url, api_key=key, timeout_seconds=timeout_seconds)
    status: CheckStatus = "PASS" if ok else "FAIL"
    return [CheckResult("llm.endpoint", status, message)]


def _check_embedding_endpoint(embedding_base_url: str | None, timeout_seconds: float) -> list[CheckResult]:
    if not embedding_base_url:
        return [CheckResult("embedding.endpoint", "WARN", "No embedding endpoint configured")]

    key = os.getenv("EMBEDDING_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip() or "dummy"
    ok, message = _probe_models(embedding_base_url, api_key=key, timeout_seconds=timeout_seconds)
    status: CheckStatus = "PASS" if ok else "FAIL"
    return [CheckResult("embedding.endpoint", status, message)]


def _check_tts(tts) -> list[CheckResult]:
    if not tts.enabled:
        return [CheckResult("tts.enabled", "PASS", "TTS disabled")]

    provider = tts.provider
    if provider == "gtts":
        try:
            from gtts import gTTS  # type: ignore

            buff = io.BytesIO()
            gTTS(text="preflight").write_to_fp(buff)
            return [CheckResult("tts.gtts", "PASS", "gTTS synthesis test succeeded")]
        except Exception as exc:
            return [CheckResult("tts.gtts", "WARN", f"gTTS synthesis test failed: {exc}")]

    if provider == "openai":
        key = os.getenv("TTS_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
        base_url = os.getenv("TTS_BASE_URL", "").strip() or getattr(tts, "base_url", None) or ""
        if key or base_url:
            source = "TTS_API_KEY" if key else "TTS_BASE_URL"
            return [CheckResult("tts.openai", "PASS", f"{source} configured for OpenAI-compatible TTS")]
        return [CheckResult("tts.openai", "WARN", "TTS_API_KEY/OPENAI_API_KEY missing and no TTS base_url configured")]

    return [CheckResult("tts.provider", "WARN", f"Unknown tts.provider={provider}")]


def _check_notifications(notifications) -> list[CheckResult]:
    rows: list[CheckResult] = []
    channels = list(getattr(notifications, "channels", []) or [])
    if not channels:
        return [CheckResult("notify.channel", "PASS", "Notifications disabled")]
    if "telegram" in channels:
        bot = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if bot and chat:
            rows.append(CheckResult("notify.telegram", "PASS", "Telegram token/chat id are set"))
        else:
            rows.append(CheckResult("notify.telegram", "WARN", "Telegram selected but token/chat id missing"))
    if "dingtalk" in channels:
        webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
        if webhook:
            rows.append(CheckResult("notify.dingtalk", "PASS", "DingTalk webhook is set"))
        else:
            rows.append(CheckResult("notify.dingtalk", "WARN", "DingTalk selected but webhook missing"))
    if getattr(notifications.dingtalk, "ingest_enabled", False):
        app_key = os.getenv("DINGTALK_APP_KEY", "").strip() or os.getenv("DINGTALK_CLIENT_ID", "").strip()
        app_secret = os.getenv("DINGTALK_APP_SECRET", "").strip() or os.getenv("DINGTALK_CLIENT_SECRET", "").strip()
        if app_key and app_secret:
            rows.append(CheckResult("ingest.dingtalk", "PASS", "DingTalk app key/secret are set"))
        else:
            rows.append(CheckResult("ingest.dingtalk", "WARN", "DingTalk ingest enabled but app key/secret missing"))
    if rows:
        return rows
    return [CheckResult("notify.channel", "WARN", f"Unknown notifications.channels={channels}")]


def _probe_models(base_url: str, api_key: str, timeout_seconds: float) -> tuple[bool, str]:
    url = _models_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = httpx.get(url, headers=headers, timeout=timeout_seconds)
    except Exception as exc:
        return False, f"{url} probe failed: {exc}"

    if response.status_code >= 400:
        return False, f"{url} returned HTTP {response.status_code}: {response.text[:160]}"
    return True, f"{url} reachable (HTTP {response.status_code})"


def _models_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/models"
    return f"{normalized}/v1/models"


def _print_results(results: list[CheckResult]) -> None:
    print("== Preflight Check ==")
    for row in results:
        print(f"[{row.status}] {row.name}: {row.message}")
