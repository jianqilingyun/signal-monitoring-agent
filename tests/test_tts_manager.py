from __future__ import annotations

import unittest

from monitor_agent.core.models import TTSConfig
from monitor_agent.tts.manager import TTSManager


class TTSManagerTests(unittest.TestCase):
    def test_disabled_tts_returns_empty_audio_without_errors(self) -> None:
        manager = TTSManager(TTSConfig(enabled=False, provider="gtts"))
        audio, errors = manager.synthesize("This should be skipped.")
        self.assertEqual(audio, b"")
        self.assertEqual(errors, [])

    def test_openai_tts_accepts_custom_base_url_without_env_key(self) -> None:
        manager = TTSManager(
            TTSConfig(
                enabled=False,
                provider="openai",
                base_url="http://127.0.0.1:1234/v1",
            )
        )
        audio, errors = manager.synthesize("This should also be skipped.")
        self.assertEqual(audio, b"")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
