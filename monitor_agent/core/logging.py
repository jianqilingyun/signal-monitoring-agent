from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"(https://api\.telegram\.org/bot)([^/\s?]+)")
_TELEGRAM_BOT_TOKEN_SIMPLE_RE = re.compile(r"\bbot([0-9]+:[A-Za-z0-9_-]+)\b", flags=re.IGNORECASE)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        sanitized = self._sanitize(message)
        if sanitized != message:
            record.msg = sanitized
            record.args = ()
        return True

    @staticmethod
    def _sanitize(message: str) -> str:
        text = str(message or "")
        text = _TELEGRAM_BOT_TOKEN_RE.sub(r"\1[REDACTED]", text)
        text = _TELEGRAM_BOT_TOKEN_SIMPLE_RE.sub("bot[REDACTED]", text)
        return text


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "monitor_agent.log"

    root = logging.getLogger()
    root.setLevel(level)

    if root.handlers:
        for handler in list(root.handlers):
            root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    console.addFilter(SensitiveDataFilter())

    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler.addFilter(SensitiveDataFilter())

    root.addHandler(console)
    root.addHandler(file_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
