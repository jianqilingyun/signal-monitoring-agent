from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from monitor_agent.core.logging import setup_logging


class LoggingSecurityTests(unittest.TestCase):
    def test_telegram_token_is_redacted_from_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            setup_logging(log_dir)

            logger = logging.getLogger("security.test")
            logger.info("HTTP Request: GET https://api.telegram.org/bot123456:ABCDEF/getUpdates?timeout=20")
            for handler in logging.getLogger().handlers:
                handler.flush()

            log_text = (log_dir / "monitor_agent.log").read_text(encoding="utf-8")
            self.assertNotIn("123456:ABCDEF", log_text)
            self.assertIn("[REDACTED]", log_text)


if __name__ == "__main__":
    unittest.main()
