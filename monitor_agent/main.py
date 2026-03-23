from __future__ import annotations

import logging
import signal
import sys
import time

import uvicorn
from fastapi import FastAPI

from monitor_agent.api.server import ApiServer
from monitor_agent.core.config import load_config
from monitor_agent.core.logging import setup_logging
from monitor_agent.core.pipeline import MonitoringPipeline
from monitor_agent.core.scheduler import SchedulerService
from monitor_agent.core.storage import Storage
from monitor_agent.core.webhooks import WebhookManager
from monitor_agent.inbox_engine import InboxEngine
from monitor_agent.inbound.dingtalk_service import DingTalkInboundService
from monitor_agent.inbound.telegram_service import TelegramInboundService
from monitor_agent.user_input_resolver import UserInputResolver

config = load_config()
storage = Storage(config.storage.root_dir)
setup_logging(storage.logs_dir)
logger = logging.getLogger(__name__)

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
def on_startup() -> None:
    api_scheduler.start()
    telegram_inbound.start()
    dingtalk_inbound.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    telegram_inbound.shutdown()
    dingtalk_inbound.shutdown()
    api_scheduler.shutdown()


def run_api() -> None:
    uvicorn.run(
        "monitor_agent.main:app",
        host=config.api.host,
        port=config.api.port,
        reload=False,
    )


def run_worker() -> None:
    logger.info("Starting standalone worker")
    worker_scheduler = SchedulerService(
        timezone=config.schedule.timezone,
        times=config.schedule.times,
        pipeline=pipeline,
        enabled=config.schedule.enabled,
    )
    worker_telegram_inbound = TelegramInboundService(
        storage=storage,
        inbox_engine=inbox_engine,
        api_config=config.api,
        pipeline=pipeline,
    )
    worker_dingtalk_inbound = DingTalkInboundService(
        storage=storage,
        inbox_engine=inbox_engine,
        dingtalk_config=config.notifications.dingtalk,
        pipeline=pipeline,
    )
    worker_scheduler.start()
    worker_telegram_inbound.start()
    worker_dingtalk_inbound.start()

    def _stop_handler(signum: int, _: object) -> None:
        logger.info("Received signal %s, shutting down", signum)
        worker_telegram_inbound.shutdown()
        worker_dingtalk_inbound.shutdown()
        worker_scheduler.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    pipeline.run_once(trigger="worker_boot")
    while True:
        time.sleep(1)
