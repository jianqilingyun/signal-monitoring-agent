from __future__ import annotations

import io
import logging
import os

from gtts import gTTS
from openai import OpenAI

from monitor_agent.core.models import TTSConfig

logger = logging.getLogger(__name__)


class TTSManager:
    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        api_key = (
            os.getenv("TTS_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
            or "dummy"
        )
        base_url = (self.config.base_url or os.getenv("TTS_BASE_URL", "")).strip() or None
        self._openai_client = OpenAI(api_key=api_key, base_url=base_url) if self.config.provider == "openai" else None

    def synthesize(self, text: str) -> tuple[bytes, list[str]]:
        errors: list[str] = []
        if not self.config.enabled:
            return b"", errors
        if not text.strip():
            return b"", errors

        providers = [self.config.provider]
        if (
            self.config.provider == "openai"
            and os.getenv("ENABLE_GTTS_FALLBACK", "false").strip().lower() == "true"
        ):
            providers.append("gtts")

        for provider in providers:
            try:
                if provider == "openai":
                    return self._synthesize_openai(text), errors
                if provider == "gtts":
                    return self._synthesize_gtts(text, self.config.language), errors
            except Exception as exc:
                msg = f"TTS provider {provider} failed: {exc}"
                logger.warning(msg)
                errors.append(msg)

        return b"", errors

    def _synthesize_openai(self, text: str) -> bytes:
        if self._openai_client is None:
            raise RuntimeError("OpenAI-compatible TTS client is not configured")
        clipped = text[:7000]
        response = self._openai_client.audio.speech.create(
            model=self.config.model,
            voice=self.config.voice,
            input=clipped,
            format="mp3",
        )
        if hasattr(response, "read"):
            return response.read()
        if hasattr(response, "content"):
            return response.content
        raise RuntimeError("Unexpected OpenAI TTS response type")

    @staticmethod
    def _synthesize_gtts(text: str, language: str) -> bytes:
        clipped = text[:3500]
        fp = io.BytesIO()
        lang = (language or "zh-CN").strip()
        tts = gTTS(text=clipped, lang=lang)
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
