from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import yaml

from monitor_agent.api.config_panel import ConfigPanelSaveRequest, ConfigPanelService
from monitor_agent.core.models import (
    ApiConfig,
    DomainProfileConfig,
    LLMConfig,
    NotificationsConfig,
    ScheduleConfig,
    SourceLinkConfig,
    TTSConfig,
)


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class ConfigPanelTests(unittest.TestCase):
    def test_save_updates_domain_profiles_and_env(self) -> None:
        previous_openai = os.environ.get("OPENAI_API_KEY")
        previous_embedding = os.environ.get("EMBEDDING_API_KEY")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                config_path = root / "config" / "config.local.yaml"
                llm_fragment = root / "config" / "fragments" / "llm.provider.yaml"
                tts_fragment = root / "config" / "fragments" / "tts.provider.yaml"
                sources_fragment = root / "config" / "fragments" / "sources.ai_infra.yaml"
                env_path = root / ".env"

                _write_yaml(
                    config_path,
                    {
                        "imports": [
                            "./fragments/llm.provider.yaml",
                            "./fragments/tts.provider.yaml",
                            "./fragments/sources.ai_infra.yaml",
                        ],
                        "domain": "AI Infrastructure",
                        "domain_profiles": [
                            {
                                "domain": "AI Infrastructure",
                                "focus_areas": ["GPU supply"],
                                "entities": ["NVIDIA"],
                                "keywords": ["blackwell"],
                                "source_links": ["https://hnrss.org/frontpage"],
                            }
                        ],
                        "schedule": {"timezone": "Asia/Shanghai", "times": ["07:00", "22:00"], "enabled": True},
                        "filtering": {"importance_threshold": 0.6, "max_signals": 10, "max_system_signals": 5},
                        "notifications": {
                            "channel": "none",
                            "channels": [],
                            "telegram": {"enabled": False},
                            "dingtalk": {"enabled": False, "ingest_enabled": False},
                        },
                        "storage": {"root_dir": str(root / "data"), "base_path": str(root / "data")},
                        "api": {"host": "127.0.0.1", "port": 8080, "scheduler_enabled": False},
                    },
                )
                _write_yaml(
                    llm_fragment,
                    {
                        "llm": {
                            "provider": "openai",
                            "model": "deepseek-chat",
                            "dedup_model": "deepseek-chat",
                            "embedding_model": "text-embedding-3-small",
                            "embedding_base_url": "http://127.0.0.1:1234/v1",
                            "base_url": "https://api.deepseek.com/v1",
                            "temperature": 0.1,
                            "dedup_temperature": 0.0,
                            "max_input_items": 40,
                        }
                    },
                )
                _write_yaml(
                    tts_fragment,
                    {
                        "tts": {
                            "enabled": False,
                            "provider": "gtts",
                            "model": "gpt-4o-mini-tts",
                            "voice": "alloy",
                            "base_url": None,
                        }
                    },
                )
                _write_yaml(
                    sources_fragment,
                    {
                        "sources": {
                            "rss": [{"name": "OpenAI News", "url": "https://openai.com/news/rss.xml", "max_items": 20}],
                            "playwright": [],
                        }
                    },
                )
                env_path.write_text("", encoding="utf-8")

                service = ConfigPanelService(config_path=str(config_path), repo_root=root)

                request = ConfigPanelSaveRequest(
                    domain_profiles=[
                        DomainProfileConfig(
                            domain="AI Infra",
                            focus_areas=["GPU supply", "Inference cost"],
                            entities=["NVIDIA", "OpenAI"],
                            keywords=["capex", "blackwell"],
                            source_links=[
                                "https://hnrss.org/frontpage",
                                "https://openai.com/news/",
                                SourceLinkConfig(
                                    url="https://openai.com/news/",
                                    type="playwright",
                                    follow_links_enabled=True,
                                    max_links_per_source=5,
                                    article_url_patterns=["/index/"],
                                ),
                            ],
                        ),
                        DomainProfileConfig(
                            domain="Cybersecurity",
                            focus_areas=["vulnerability"],
                            entities=["CISA"],
                            keywords=["CVE"],
                            source_links=["https://krebsonsecurity.com/"],
                        ),
                    ],
                    llm=LLMConfig(
                        provider="openai",
                        model="deepseek-chat",
                        dedup_model="deepseek-chat",
                        embedding_model="text-embedding-qwen3-embedding-0.6b",
                        embedding_base_url="http://127.0.0.1:1234/v1",
                        base_url="https://api.deepseek.com/v1",
                        temperature=0.1,
                        dedup_temperature=0.0,
                        max_input_items=40,
                    ),
                    schedule=ScheduleConfig(
                        timezone="Asia/Shanghai",
                        times=["08:00"],
                        enabled=True,
                    ),
                    api=ApiConfig(
                        host="127.0.0.1",
                        port=8080,
                        scheduler_enabled=False,
                        auto_run_on_user_ingest=True,
                    ),
                    tts=TTSConfig(enabled=False, provider="gtts", model="gpt-4o-mini-tts", voice="alloy"),
                    notifications=NotificationsConfig(
                        channel="dingtalk",
                        channels=["dingtalk"],
                        telegram={"enabled": False},
                        dingtalk={"enabled": True, "ingest_enabled": True},
                    ),
                    secrets={
                        "openai_api_key": "abc123",
                        "embedding_api_key": "emb456",
                        "tts_api_key": "tts789",
                        "telegram_bot_token": "tg_bot_123",
                        "telegram_chat_id": "123456",
                        "dingtalk_app_key": "ding_app_key",
                        "dingtalk_app_secret": "ding_app_secret",
                        "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=test",
                        "dingtalk_secret": "ding_secret",
                    },
                )
                saved = service.save_state(request)

                domains = [row["domain"] for row in saved["domain_profiles"]]
                self.assertEqual(domains, ["AI Infra", "Cybersecurity"])
                self.assertEqual(saved["domain_scope"], ["AI Infra", "Cybersecurity"])
                self.assertEqual(saved["schedule"]["times"], ["08:00"])
                self.assertTrue(saved["api"]["auto_run_on_user_ingest"])
                self.assertFalse(saved["tts"]["enabled"])
                self.assertEqual(saved["notifications"]["channel"], "dingtalk")
                self.assertTrue(saved["notifications"]["dingtalk"]["ingest_enabled"])
                self.assertTrue(saved["restart_required"])
                self.assertIn("Schedule service", saved["restart_reasons"])
                self.assertIn("DingTalk inbound", saved["restart_reasons"])
                ai_profile = saved["domain_profiles"][0]
                advanced_links = [row for row in ai_profile["source_links"] if isinstance(row, dict)]
                self.assertEqual(len(advanced_links), 1)
                self.assertTrue(advanced_links[0]["follow_links_enabled"])
                self.assertEqual(advanced_links[0]["max_links_per_source"], 5)
                self.assertEqual(advanced_links[0]["url"], "https://openai.com/news/")

                # Sources fragment is controlled by UI workflow and intentionally reset;
                # runtime sources are expanded from domain_profiles.source_links.
                sources_payload = yaml.safe_load(sources_fragment.read_text(encoding="utf-8"))
                self.assertEqual(sources_payload["sources"], {"rss": [], "playwright": []})

                env = env_path.read_text(encoding="utf-8")
                self.assertIn('OPENAI_API_KEY="abc123"', env)
                self.assertIn('EMBEDDING_API_KEY="emb456"', env)
                self.assertIn('TTS_API_KEY="tts789"', env)
                self.assertIn('TELEGRAM_BOT_TOKEN="tg_bot_123"', env)
                self.assertIn('TELEGRAM_CHAT_ID="123456"', env)
                self.assertIn('DINGTALK_APP_KEY="ding_app_key"', env)
                self.assertIn('DINGTALK_APP_SECRET="ding_app_secret"', env)
                self.assertIn('DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=test"', env)
                self.assertIn('DINGTALK_SECRET="ding_secret"', env)
        finally:
            if previous_openai is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous_openai
            if previous_embedding is None:
                os.environ.pop("EMBEDDING_API_KEY", None)
            else:
                os.environ["EMBEDDING_API_KEY"] = previous_embedding

    def test_save_can_clear_existing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config" / "config.local.yaml"
            env_path = root / ".env"
            _write_yaml(
                config_path,
                {
                    "domain": "AI Infrastructure",
                    "domain_profiles": [{"domain": "AI Infrastructure"}],
                    "sources": {"rss": [], "playwright": []},
                    "llm": {"provider": "openai", "model": "gpt-5-mini", "embedding_model": "text-embedding-3-small"},
                    "tts": {"enabled": False, "provider": "gtts", "model": "gpt-4o-mini-tts", "voice": "alloy"},
                    "notifications": {
                        "channel": "telegram",
                        "channels": ["telegram"],
                        "telegram": {"enabled": True},
                        "dingtalk": {"enabled": False, "ingest_enabled": False},
                    },
                    "storage": {"root_dir": str(root / "data"), "base_path": str(root / "data")},
                    "api": {"host": "127.0.0.1", "port": 8080, "scheduler_enabled": False},
                },
            )
            env_path.write_text('TELEGRAM_BOT_TOKEN="secret-token"\n', encoding="utf-8")
            service = ConfigPanelService(config_path=str(config_path), repo_root=root)

            request = ConfigPanelSaveRequest(
                domain_profiles=[DomainProfileConfig(domain="AI Infrastructure")],
                llm=LLMConfig(provider="openai", model="gpt-5-mini", embedding_model="text-embedding-3-small"),
                api=ApiConfig(host="127.0.0.1", port=8080, scheduler_enabled=False),
                tts=TTSConfig(enabled=False, provider="gtts", model="gpt-4o-mini-tts", voice="alloy"),
                notifications=NotificationsConfig(
                    channel="telegram",
                    channels=["telegram"],
                    telegram={"enabled": True},
                    dingtalk={"enabled": False, "ingest_enabled": False},
                ),
                clear_secrets=["telegram_bot_token"],
            )

            saved = service.save_state(request)
            env = env_path.read_text(encoding="utf-8")
            self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
            self.assertTrue(saved["restart_required"])
            self.assertIn("Telegram credentials", saved["restart_reasons"])


if __name__ == "__main__":
    unittest.main()
