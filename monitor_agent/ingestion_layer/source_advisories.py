from __future__ import annotations

from typing import Any

from monitor_agent.core.models import SourceAdvisory
from monitor_agent.ingestion_layer.source_cursor import normalize_url
from monitor_agent.strategy_engine.source_strategy_engine import _rss_fallback_page


def build_source_advisories(
    source_health: dict[str, dict[str, object]],
    strategy_cache: dict[str, object],
) -> list[SourceAdvisory]:
    advisories: list[SourceAdvisory] = []

    for source_key, health in source_health.items():
        if not isinstance(health, dict):
            continue
        advisory = _build_single_advisory(source_key, health, strategy_cache)
        if advisory is not None:
            advisories.append(advisory)
    return advisories


def _build_single_advisory(
    source_key: str,
    health: dict[str, object],
    strategy_cache: dict[str, object],
) -> SourceAdvisory | None:
    source_name = str(health.get("source_name") or source_key)
    source_type = str(health.get("source_type") or "html")
    if source_type not in {"rss", "html", "playwright"}:
        source_type = "html"
    source_url = str(health.get("source_url") or "")
    runtime_status = str(health.get("status") or "").strip().lower() or None
    refresh_interval_hours = _safe_positive_int(health.get("refresh_interval_hours"))

    cache_key = normalize_url(source_url).lower()
    cached = strategy_cache.get(cache_key)
    cached_payload = cached if isinstance(cached, dict) else {}
    cached_probe_status = str(cached_payload.get("probe_status") or "").strip().lower()
    cached_issues = _safe_str_list(cached_payload.get("issues"))
    cached_fixes = _safe_str_list(cached_payload.get("fixes"))
    suggested_source_link = _suggested_source_link(source_url, cached_payload)

    if runtime_status == "error":
        error_text = str(health.get("error") or "").strip() or "Unknown fetch error."
        issues = [error_text, *cached_issues]
        fixes = [*cached_fixes]
        if suggested_source_link is None and source_type == "rss":
            fallback_page = _rss_fallback_page(source_url)
            if fallback_page and fallback_page != source_url:
                suggested_source_link = {
                    "url": fallback_page,
                    "type": "playwright",
                    "name": f"{source_name} web fallback",
                }
                fixes.append(f"Try webpage ingestion instead: {fallback_page}")
        return SourceAdvisory(
            source_key=source_key,
            source_name=source_name,
            source_type=source_type,
            source_url=source_url,
            severity="error",
            issue_code="fetch_error",
            summary=f"Source fetch failed this run: {error_text}",
            runtime_status="error",
            refresh_interval_hours=refresh_interval_hours,
            suggested_source_link=suggested_source_link,
            issues=issues,
            fixes=_dedupe_list(fixes),
        )

    items_emitted = _safe_int(health.get("items_emitted"))
    candidate_count = _safe_int(health.get("candidate_count"))
    kept_count = _safe_int(health.get("kept_count"))
    if runtime_status == "success" and items_emitted == 0:
        if candidate_count > 0 and kept_count == 0:
            summary = "Source produced candidates, but all were filtered out before content extraction."
            issue_code = "no_incremental_items"
        else:
            summary = "Source returned no items in this run."
            issue_code = "empty_source"
        return SourceAdvisory(
            source_key=source_key,
            source_name=source_name,
            source_type=source_type,
            source_url=source_url,
            severity="warning",
            issue_code=issue_code,
            summary=summary,
            runtime_status="success",
            refresh_interval_hours=refresh_interval_hours,
            suggested_source_link=suggested_source_link,
            issues=_dedupe_list([*cached_issues]),
            fixes=_dedupe_list(cached_fixes),
        )

    if cached_probe_status in {"warning", "error"} and (cached_issues or cached_fixes or suggested_source_link is not None):
        summary = "Source strategy diagnosis found a likely better ingestion path or source URL."
        if cached_payload.get("reason"):
            summary = str(cached_payload.get("reason"))
        return SourceAdvisory(
            source_key=source_key,
            source_name=source_name,
            source_type=source_type,
            source_url=source_url,
            severity="warning" if cached_probe_status == "warning" else "error",
            issue_code="strategy_probe_warning",
            summary=summary,
            runtime_status=runtime_status if runtime_status in {"success", "skipped", "error"} else None,
            refresh_interval_hours=refresh_interval_hours,
            suggested_source_link=suggested_source_link,
            issues=_dedupe_list(cached_issues),
            fixes=_dedupe_list(cached_fixes),
        )

    return None


def _suggested_source_link(source_url: str, cached_payload: dict[str, Any]) -> dict[str, Any] | None:
    payload = cached_payload.get("normalized_source_link")
    if not isinstance(payload, dict):
        return None
    suggested_url = str(payload.get("url") or "").strip()
    if not suggested_url:
        return None
    if normalize_url(suggested_url).lower() == normalize_url(source_url).lower():
        return None
    return payload


def _safe_int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except Exception:
        return 0


def _safe_positive_int(value: object) -> int | None:
    parsed = _safe_int(value)
    return parsed if parsed >= 1 else None


def _safe_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe_list(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
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
