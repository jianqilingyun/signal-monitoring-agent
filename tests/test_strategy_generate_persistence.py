from __future__ import annotations

import tempfile
import unittest

from monitor_agent.core.models import MonitorConfig
from monitor_agent.core.storage import Storage
from monitor_agent.strategy_engine.models import StrategyGenerateRequest, StrategyGetRequest
from monitor_agent.strategy_engine.service import StrategyEngine


def _base_config() -> MonitorConfig:
    return MonitorConfig.model_validate(
        {
            "domain": "Technology",
            "sources": {"rss": [], "playwright": []},
        }
    )


class StrategyGeneratePersistenceTests(unittest.TestCase):
    def test_generate_persists_draft_state_for_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            engine = StrategyEngine(base_config=_base_config(), storage=storage)

            result = engine.generate(
                StrategyGenerateRequest(
                    user_request="Monitor AI infrastructure, GPU supply, and cloud capex changes.",
                    timezone="Asia/Shanghai",
                    schedule_times=["07:00", "22:00"],
                )
            )
            self.assertTrue(bool(result.config_object))

            fetched = engine.get(StrategyGetRequest())
            self.assertIsNotNone(fetched.strategy)
            assert fetched.strategy is not None
            self.assertEqual(fetched.strategy.version, 1)
            self.assertTrue(fetched.strategy.pending_deploy)
            self.assertEqual(fetched.strategy.generation.config_object["domain"], result.config_object["domain"])

    def test_generate_same_config_does_not_create_duplicate_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = Storage(tmpdir)
            engine = StrategyEngine(base_config=_base_config(), storage=storage)

            req = StrategyGenerateRequest(
                user_request="Monitor AI infrastructure GPU supply and cloud capex trends.",
                timezone="Asia/Shanghai",
                schedule_times=["07:00", "22:00"],
            )
            engine.generate(req)
            first = engine.get(StrategyGetRequest())
            engine.generate(req)
            second = engine.get(StrategyGetRequest())

            assert first.strategy is not None and second.strategy is not None
            self.assertEqual(first.strategy.version, second.strategy.version)


if __name__ == "__main__":
    unittest.main()
