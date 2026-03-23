from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)


class DingTalkNotifier:
    def __init__(self) -> None:
        self.webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
        self.secret = os.getenv("DINGTALK_SECRET", "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook)

    def send_markdown(self, *, title: str, text: str) -> None:
        if not self.is_configured:
            raise RuntimeError("DingTalk webhook is not configured")

        url = self._signed_url()
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text,
            },
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
        if int(body.get("errcode", -1)) != 0:
            raise RuntimeError(f"DingTalk send failed: {body}")
        logger.info("DingTalk notification sent")

    def _signed_url(self) -> str:
        if not self.secret:
            return self.webhook
        timestamp = str(int(time.time() * 1000))
        sign_value = f"{timestamp}\n{self.secret}".encode("utf-8")
        digest = hmac.new(self.secret.encode("utf-8"), sign_value, digestmod=hashlib.sha256).digest()
        sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
        separator = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{separator}timestamp={timestamp}&sign={sign}"
