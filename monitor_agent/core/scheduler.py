from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from monitor_agent.core.pipeline import MonitoringPipeline

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(
        self,
        timezone: str,
        times: list[str],
        pipeline: MonitoringPipeline,
        enabled: bool = True,
    ) -> None:
        self.timezone = timezone
        self.times = times
        self.pipeline = pipeline
        self.enabled = enabled
        self.scheduler = BackgroundScheduler(timezone=timezone)
        self._started = False

    def start(self) -> None:
        if not self.enabled:
            logger.info("Scheduler is disabled by config; startup skipped")
            return
        if self._started:
            return

        for idx, time_str in enumerate(self.times):
            hour, minute = self._parse_time(time_str)
            trigger = CronTrigger(hour=hour, minute=minute, timezone=self.timezone)
            self.scheduler.add_job(
                self._safe_run,
                trigger=trigger,
                id=f"monitor_{idx}_{hour:02d}{minute:02d}",
                replace_existing=True,
            )
            logger.info("Scheduled monitoring run at %02d:%02d %s", hour, minute, self.timezone)

        self.scheduler.start()
        self._started = True
        logger.info("Scheduler started")

    def shutdown(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False
            logger.info("Scheduler stopped")

    def _safe_run(self) -> None:
        try:
            self.pipeline.run_once(trigger="scheduled")
        except Exception:
            logger.exception("Scheduled run failed")

    @staticmethod
    def _parse_time(value: str) -> tuple[int, int]:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid time format: {value}")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid time value: {value}")
        return hour, minute
