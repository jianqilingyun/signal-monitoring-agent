from __future__ import annotations

from urllib.parse import urlparse

from monitor_agent.core.models import MonitorConfig
from monitor_agent.strategy_engine.models import InternalStrategyConfig, UIStrategyInput


def normalize_ui_input(
    ui_input: UIStrategyInput,
    *,
    base_config: MonitorConfig | None = None,
    version_hint: int | None = None,
    schedule_timezone: str | None = None,
    schedule_times: list[str] | None = None,
    importance_threshold: float | None = None,
    max_signals: int | None = None,
    advanced_settings: dict[str, object] | None = None,
) -> InternalStrategyConfig:
    filtering = base_config.filtering if base_config else None
    schedule = base_config.schedule if base_config else None
    notifications = base_config.notifications if base_config else None
    tts = base_config.tts if base_config else None

    return InternalStrategyConfig(
        signal_categories=_domain_signal_categories(ui_input.domain),
        source_weights=_source_weights(ui_input.source_links),
        topic_weights=_topic_weights(ui_input),
        filter_rules={
            "importance_threshold": (
                importance_threshold
                if importance_threshold is not None
                else (filtering.importance_threshold if filtering else 0.6)
            ),
            "max_signals": max_signals if max_signals is not None else (filtering.max_signals if filtering else 10),
            "max_system_signals": filtering.max_system_signals if filtering else 5,
            "unknown_freshness_importance_threshold": (
                filtering.unknown_freshness_importance_threshold if filtering else 0.9
            ),
            "event_similarity_threshold": filtering.event_similarity_threshold if filtering else 0.75,
            "inbox_match_threshold": filtering.inbox_match_threshold if filtering else 0.2,
        },
        schedule={
            "timezone": schedule_timezone or (schedule.timezone if schedule else "UTC"),
            "times": schedule_times or (schedule.times if schedule else ["07:00"]),
            "enabled": schedule.enabled if schedule else True,
        },
        delivery_options={
            "outputs": {
                "json": True,
                "text": True,
                "audio": tts.enabled if tts else False,
            },
            "channels": {
                "notification_channels": notifications.channels if notifications else [],
                "notification_channel": notifications.channel if notifications else "none",
                "telegram_enabled": notifications.telegram.enabled if notifications else False,
                "dingtalk_enabled": notifications.dingtalk.enabled if notifications else False,
                "webhooks_enabled": True,
            },
            "tts": {
                "enabled": tts.enabled if tts else False,
                "provider": tts.provider if tts else "openai",
                "model": tts.model if tts else "gpt-4o-mini-tts",
                "voice": tts.voice if tts else "alloy",
            },
        },
        version_metadata={
            "strategy_version": version_hint or 1,
            "normalization_source": "ui_simple",
            "domain": ui_input.domain,
            "schema_version": 1,
        },
        advanced_settings={k: v for k, v in (advanced_settings or {}).items()},
    )


def synthesize_user_request(ui_input: UIStrategyInput) -> str:
    focus = ", ".join(ui_input.focus_areas[:5]) or "key developments"
    entities = ", ".join(ui_input.entities[:8]) or "priority entities"
    links = ", ".join(ui_input.source_links[:5]) or "configured source links"
    return (
        f"Monitor topic {ui_input.domain}. Optional focus areas: {focus}. "
        f"Optional entities: {entities}. Optional keywords: {', '.join(ui_input.keywords[:10]) or 'none'}. "
        f"Use sources: {links}."
    )


def build_ui_input_from_fields(
    *,
    domain: str,
    focus_areas: list[str] | None = None,
    entities: list[str] | None = None,
    keywords: list[str] | None = None,
    source_links: list[str] | None = None,
) -> UIStrategyInput:
    return UIStrategyInput(
        domain=domain.strip(),
        focus_areas=_dedupe(focus_areas or []),
        entities=_dedupe(entities or []),
        keywords=_dedupe(keywords or []),
        source_links=_dedupe(source_links or []),
    )


def _domain_signal_categories(domain: str) -> list[str]:
    lowered = domain.strip().lower()
    if any(token in lowered for token in ("ai", "infra", "cloud", "compute", "gpu")):
        return [
            "product_release",
            "infrastructure_expansion",
            "supply_chain_shift",
            "cost_pricing_change",
            "policy_regulatory_update",
            "incident_outage",
        ]
    if any(token in lowered for token in ("security", "cyber")):
        return [
            "vulnerability_disclosure",
            "exploit_activity",
            "breach_incident",
            "vendor_advisory",
            "policy_regulatory_update",
        ]
    if any(token in lowered for token in ("finance", "market", "macro")):
        return [
            "earnings_update",
            "guidance_revision",
            "macro_policy_change",
            "liquidity_credit_signal",
            "m_and_a",
        ]
    return [
        "product_release",
        "strategic_partnership",
        "market_movement",
        "policy_regulatory_update",
        "operational_incident",
    ]


def _source_weights(source_links: list[str]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for link in source_links:
        host = _host(link)
        if not host:
            continue
        weights[host] = _host_weight(host)
    return weights


def _host_weight(host: str) -> float:
    lowered = host.lower()
    if any(
        lowered.endswith(token)
        for token in (
            "openai.com",
            "anthropic.com",
            "nvidia.com",
            "amd.com",
            "aws.amazon.com",
            "cloud.google.com",
            "microsoft.com",
        )
    ):
        return 1.0
    if any(
        token in lowered
        for token in ("reuters.com", "bloomberg.com", "wsj.com", "ft.com", "theinformation.com")
    ):
        return 0.9
    if any(token in lowered for token in ("news.google.com", "hnrss.org", "reddit.com", "x.com")):
        return 0.65
    return 0.78


def _topic_weights(ui_input: UIStrategyInput) -> dict[str, float]:
    weights: dict[str, float] = {}
    for token in ui_input.focus_areas:
        weights[token] = max(weights.get(token, 0.0), 1.0)
    for token in ui_input.entities:
        weights[token] = max(weights.get(token, 0.0), 0.9)
    for token in ui_input.keywords:
        weights[token] = max(weights.get(token, 0.0), 0.75)
    return weights


def _host(url: str) -> str:
    token = _normalize_source_link_token(url)
    if not token:
        return ""
    parsed = urlparse(token if "://" in token else f"https://{token}")
    return (parsed.hostname or "").strip().lower()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        token = item.strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _normalize_source_link_token(value: str) -> str:
    token = value.strip()
    if not token:
        return ""
    lower = token.lower()
    for prefix in ("playwright:", "pw:", "rss:", "auto:"):
        if lower.startswith(prefix):
            return token[len(prefix) :].strip()
    if "|" in token:
        return token.split("|", 1)[0].strip()
    return token
