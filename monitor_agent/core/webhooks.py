from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, field_validator

from monitor_agent.core.models import RunArtifacts, Signal
from monitor_agent.core.storage import Storage
from monitor_agent.core.url_safety import UnsafeUrlError, validate_public_http_url
from monitor_agent.core.utils import utc_now

logger = logging.getLogger(__name__)

WebhookEvent = Literal["signals.new", "brief.new", "run.completed"]
SUPPORTED_EVENTS: set[str] = {"signals.new", "brief.new", "run.completed"}


class WebhookSubscribeRequest(BaseModel):
    url: str
    events: list[WebhookEvent] = Field(default_factory=lambda: ["signals.new", "brief.new"])
    secret: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        normalized = value.strip()
        try:
            return validate_public_http_url(normalized)
        except UnsafeUrlError as exc:
            raise ValueError(str(exc)) from exc


class WebhookSubscription(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    url: str
    events: list[WebhookEvent]
    secret: str | None = None
    enabled: bool = True
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class WebhookManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._lock = threading.Lock()

    def list_subscriptions(self) -> list[WebhookSubscription]:
        with self._lock:
            return [
                WebhookSubscription.model_validate(item)
                for item in self.storage.load_webhook_subscriptions()
            ]

    def subscribe(self, request: WebhookSubscribeRequest) -> WebhookSubscription:
        with self._lock:
            existing = self.storage.load_webhook_subscriptions()
            subscription = WebhookSubscription(
                url=request.url,
                events=request.events,
                secret=request.secret,
            )
            existing.append(subscription.model_dump(mode="json"))
            self.storage.save_webhook_subscriptions(existing)
            return subscription

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            existing = self.storage.load_webhook_subscriptions()
            filtered = [row for row in existing if row.get("id") != subscription_id]
            changed = len(filtered) != len(existing)
            if changed:
                self.storage.save_webhook_subscriptions(filtered)
            return changed

    def publish_run_outputs(
        self,
        run_id: str,
        domain: str,
        signals: list[Signal],
        brief_text: str,
        brief_audio_path: str | None,
        manifest: RunArtifacts,
    ) -> list[str]:
        errors: list[str] = []
        subscriptions = [s for s in self.list_subscriptions() if s.enabled]
        if not subscriptions:
            return errors

        timestamp = utc_now().isoformat()

        payloads: dict[str, dict] = {
            "signals.new": {
                "event": "signals.new",
                "timestamp": timestamp,
                "domain": domain,
                "run_id": run_id,
                "signal_count": len(signals),
                "signals": [s.model_dump(mode="json") for s in signals],
            },
            "brief.new": {
                "event": "brief.new",
                "timestamp": timestamp,
                "domain": domain,
                "run_id": run_id,
                "brief_text": brief_text,
                "brief_audio_path": brief_audio_path,
            },
            "run.completed": {
                "event": "run.completed",
                "timestamp": timestamp,
                "domain": domain,
                "run_id": run_id,
                "manifest": manifest.model_dump(mode="json"),
            },
        }

        for subscription in subscriptions:
            for event in subscription.events:
                payload = payloads.get(event)
                if payload is None:
                    continue
                try:
                    self._deliver(subscription, event, payload)
                except Exception as exc:
                    msg = f"Webhook delivery failed ({subscription.id}, {event}): {exc}"
                    logger.exception(msg)
                    errors.append(msg)

        return errors

    def _deliver(self, subscription: WebhookSubscription, event: str, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-Monitor-Event": event,
        }
        if subscription.secret:
            signature = hmac.new(
                subscription.secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-Monitor-Signature"] = f"sha256={signature}"

        with httpx.Client(timeout=10.0) as client:
            response = client.post(subscription.url, content=body, headers=headers)
            response.raise_for_status()
