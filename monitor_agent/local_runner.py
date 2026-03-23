from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

import uvicorn
from fastapi import FastAPI

from monitor_agent.api.server import ApiServer
from monitor_agent.core.config import load_config
from monitor_agent.core.logging import setup_logging
from monitor_agent.core.models import MonitorConfig
from monitor_agent.core.pipeline import MonitoringPipeline
from monitor_agent.core.scheduler import SchedulerService
from monitor_agent.core.storage import Storage
from monitor_agent.core.webhooks import WebhookManager
from monitor_agent.inbox_engine import InboxEngine
from monitor_agent.inbound.dingtalk_service import DingTalkInboundService
from monitor_agent.inbound.telegram_service import TelegramInboundService
from monitor_agent.ingestion_layer.playwright_ingestor import PlaywrightIngestor
from monitor_agent.preflight import run_preflight
from monitor_agent.user_input_resolver import UserInputResolver

logger = logging.getLogger(__name__)


def run_once(config_path: str | None = None, trigger: str = "local_once") -> int:
    _, pipeline, _, _, _ = _build_runtime(config_path)
    manifest = pipeline.run_once(trigger=trigger)
    logger.info(
        "One-shot run complete: run_id=%s status=%s signals=%d",
        manifest.run_id,
        manifest.status,
        manifest.signal_count,
    )
    return 0 if manifest.status == "completed" else 1


def run_api(config_path: str | None = None) -> int:
    config, pipeline, storage, webhook_manager, inbox_engine = _build_runtime(config_path)
    api_scheduler = SchedulerService(
        timezone=config.schedule.timezone,
        times=config.schedule.times,
        pipeline=pipeline,
        enabled=config.schedule.enabled and config.api.scheduler_enabled,
    )
    telegram_inbound = TelegramInboundService(
        storage=storage,
        inbox_engine=inbox_engine,
        api_config=config.api,
        pipeline=pipeline,
    )
    dingtalk_inbound = DingTalkInboundService(
        storage=storage,
        inbox_engine=inbox_engine,
        dingtalk_config=config.notifications.dingtalk,
        pipeline=pipeline,
    )

    app = FastAPI(title="Signal Monitoring Agent", version="0.1.0")
    ApiServer(
        app=app,
        pipeline=pipeline,
        storage=storage,
        webhook_manager=webhook_manager,
        inbox_engine=inbox_engine,
    )

    @app.on_event("startup")
    def _startup() -> None:
        api_scheduler.start()
        telegram_inbound.start()
        dingtalk_inbound.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        telegram_inbound.shutdown()
        dingtalk_inbound.shutdown()
        api_scheduler.shutdown()

    uvicorn.run(
        app,
        host=config.api.host,
        port=config.api.port,
        reload=False,
    )
    return 0


def run_scheduled(config_path: str | None = None, run_boot_once: bool = True) -> int:
    config, pipeline, _, _, _ = _build_runtime(config_path)
    scheduler = SchedulerService(
        timezone=config.schedule.timezone,
        times=config.schedule.times,
        pipeline=pipeline,
        enabled=config.schedule.enabled,
    )
    telegram_inbound = TelegramInboundService(
        storage=pipeline.storage,
        inbox_engine=pipeline.inbox_engine,
        api_config=config.api,
        pipeline=pipeline,
    )
    dingtalk_inbound = DingTalkInboundService(
        storage=pipeline.storage,
        inbox_engine=pipeline.inbox_engine,
        dingtalk_config=config.notifications.dingtalk,
        pipeline=pipeline,
    )
    scheduler.start()
    telegram_inbound.start()
    dingtalk_inbound.start()

    if run_boot_once:
        pipeline.run_once(trigger="local_boot")

    def _stop_handler(signum: int, _: object) -> None:
        logger.info("Received signal %s, shutting down local scheduler", signum)
        telegram_inbound.shutdown()
        dingtalk_inbound.shutdown()
        scheduler.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    while True:
        time.sleep(1)


def run_playwright_login(config_path: str | None = None, url: str | None = None) -> int:
    from playwright.sync_api import sync_playwright

    config, _, storage, _, _ = _build_runtime(config_path)
    target_url = _resolve_login_url(config, url)
    if not target_url:
        logger.error("No URL available for Playwright login bootstrap. Pass --url or configure a playwright source.")
        return 2

    options = PlaywrightIngestor.build_context_options(
        profile_dir=str(storage.playwright_profile_dir),
        runtime=config.playwright,
        force_headed=True,
    )
    # Always bootstrap in visible mode so the user can complete login/captcha/MFA.
    options["headless"] = False
    logger.info("Launching Playwright login bootstrap for %s", target_url)
    logger.info("Profile directory: %s", storage.playwright_profile_dir)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(**options)
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded")
        print("\nPlaywright login bootstrap is open.")
        print(f"- URL: {target_url}")
        print(f"- Profile: {storage.playwright_profile_dir}")
        print("- Complete login (and extension setup if needed), then press Enter here to save session.\n")
        input()
        context.close()
    logger.info("Playwright login bootstrap completed; session persisted.")
    return 0


def _build_runtime(
    config_path: str | None,
) -> tuple[MonitorConfig, MonitoringPipeline, Storage, WebhookManager, InboxEngine]:
    config = load_config(config_path)
    storage = Storage(config.storage.root_dir)
    setup_logging(storage.logs_dir)

    webhook_manager = WebhookManager(storage)
    user_input_resolver = UserInputResolver(config=config, storage=storage)
    inbox_engine = InboxEngine(
        storage,
        match_threshold=config.filtering.inbox_match_threshold,
        resolver=user_input_resolver,
    )
    pipeline = MonitoringPipeline(
        config=config,
        storage=storage,
        webhook_manager=webhook_manager,
        inbox_engine=inbox_engine,
    )
    return config, pipeline, storage, webhook_manager, inbox_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local runner for Signal Monitoring Agent")
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to config YAML (overrides MONITOR_CONFIG/default lookup)",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    once_parser = sub.add_parser("once", help="Run the pipeline once")
    once_parser.add_argument("--trigger", default="local_once", help="Trigger label for run_id")

    sub.add_parser("api", help="Run FastAPI service")
    preflight_parser = sub.add_parser("preflight", help="Run startup checks for local trial")
    preflight_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=6.0,
        help="HTTP timeout for endpoint probes",
    )

    scheduled_parser = sub.add_parser("scheduled", help="Run scheduler loop")
    scheduled_parser.add_argument(
        "--skip-boot-run",
        action="store_true",
        help="Do not run an immediate boot cycle before waiting for schedule times",
    )
    login_parser = sub.add_parser(
        "playwright_login",
        help="Open a headed Playwright browser for one-time login/session bootstrap",
    )
    login_parser.add_argument(
        "--url",
        default=None,
        help="Target URL for manual login (defaults to first configured playwright source if omitted)",
    )

    args = parser.parse_args(argv)
    if args.mode == "once":
        return run_once(config_path=args.config_path, trigger=args.trigger)
    if args.mode == "api":
        return run_api(config_path=args.config_path)
    if args.mode == "preflight":
        return run_preflight(config_path=args.config_path, timeout_seconds=args.timeout_seconds)
    if args.mode == "scheduled":
        return run_scheduled(
            config_path=args.config_path,
            run_boot_once=not args.skip_boot_run,
        )
    if args.mode == "playwright_login":
        return run_playwright_login(config_path=args.config_path, url=args.url)
    parser.error(f"Unsupported mode: {args.mode}")
    return 2


def _resolve_login_url(config: MonitorConfig, explicit_url: str | None) -> str | None:
    if explicit_url and explicit_url.strip():
        return explicit_url.strip()
    if config.sources.playwright:
        return config.sources.playwright[0].url
    if config.sources.rss:
        return config.sources.rss[0].url
    return None


if __name__ == "__main__":
    raise SystemExit(main())
