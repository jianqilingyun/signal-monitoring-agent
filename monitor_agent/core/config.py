from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from monitor_agent.core.models import MonitorConfig


def load_config(config_path: str | None = None) -> MonitorConfig:
    load_dotenv()
    raw_path = config_path or os.getenv("MONITOR_CONFIG", "./config/config.yaml")
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        fallback = Path("./config/config.example.yaml").resolve()
        if fallback.exists():
            path = fallback
        else:
            raise FileNotFoundError(f"Configuration file not found: {raw_path}")

    payload = _load_config_with_imports(path)
    payload = _expand_from_domain_profiles(payload)
    return MonitorConfig.model_validate(payload)


def _load_config_with_imports(path: Path, stack: list[Path] | None = None) -> dict[str, Any]:
    stack = stack or []
    resolved_path = path.resolve()
    if resolved_path in stack:
        cycle = " -> ".join(str(p) for p in stack + [resolved_path])
        raise ValueError(f"Config import cycle detected: {cycle}")

    with resolved_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Config file must be a mapping: {resolved_path}")

    imports = payload.pop("imports", []) or []
    if not isinstance(imports, list):
        raise ValueError(f"'imports' must be a list in {resolved_path}")

    merged: dict[str, Any] = {}
    for ref in imports:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"Invalid import entry in {resolved_path}: {ref!r}")
        ref_path = Path(ref).expanduser()
        if not ref_path.is_absolute():
            ref_path = (resolved_path.parent / ref_path).resolve()
        if not ref_path.exists():
            raise FileNotFoundError(f"Imported config not found: {ref_path}")
        imported_payload = _load_config_with_imports(ref_path, stack + [resolved_path])
        merged = _deep_merge(merged, imported_payload)

    merged = _deep_merge(merged, payload)
    return merged


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


def _expand_from_domain_profiles(payload: dict[str, Any]) -> dict[str, Any]:
    raw_profiles = payload.get("domain_profiles")
    if not isinstance(raw_profiles, list):
        return payload

    profiles = [row for row in raw_profiles if isinstance(row, dict)]
    if not profiles:
        return payload

    existing_sources = payload.get("sources", {})
    if not isinstance(existing_sources, dict):
        existing_sources = {}

    rss_rows = [row for row in existing_sources.get("rss", []) if isinstance(row, dict)]
    page_rows = [row for row in existing_sources.get("playwright", []) if isinstance(row, dict)]
    seen_rss = {str(row.get("url", "")).strip() for row in rss_rows if str(row.get("url", "")).strip()}
    seen_page = {str(row.get("url", "")).strip() for row in page_rows if str(row.get("url", "")).strip()}

    for profile in profiles:
        profile_domain = str(profile.get("domain", "")).strip() or "Domain"
        source_links = profile.get("source_links", [])
        if not isinstance(source_links, list):
            continue

        for idx, raw_link in enumerate(source_links, start=1):
            link = _parse_source_link(raw_link)
            if not link:
                continue
            url = link["url"]
            forced_type = link.get("type", "auto")
            name_seed = str(link.get("name", "")).strip()
            host = _source_host(url)
            name = name_seed or f"{profile_domain} - {host or f'source-{idx}'}"

            is_rss = forced_type == "rss" or (forced_type == "auto" and _looks_like_rss(url))
            if is_rss:
                if url in seen_rss:
                    continue
                seen_rss.add(url)
                rss_rows.append(
                    {
                        "name": name,
                        "url": url,
                        "max_items": _infer_rss_max_items(url),
                        "timeout_seconds": _infer_rss_timeout_seconds(url),
                        "fetch_full_text": True,
                        "article_timeout_seconds": 12,
                        "article_max_chars": 12000,
                    }
                )
            else:
                if url in seen_page:
                    continue
                seen_page.add(url)
                page_rows.append(
                    {
                        "name": name,
                        "url": url,
                        "wait_for_selector": "body",
                        "content_selector": "body",
                        "max_chars": _infer_page_max_chars(url),
                        "timeout_ms": _infer_page_timeout_ms(url),
                        "follow_links_enabled": bool(link.get("follow_links_enabled", False)),
                        "max_depth": _int_or_default(link.get("max_depth"), default=1, minimum=1, maximum=2),
                        "max_links_per_source": _int_or_default(
                            link.get("max_links_per_source"), default=3, minimum=1, maximum=20
                        ),
                        "same_domain_only": bool(link.get("same_domain_only", True)),
                        "link_selector": str(link.get("link_selector", "a[href]")).strip() or "a[href]",
                        "article_url_patterns": _clean_string_list(link.get("article_url_patterns")),
                        "exclude_url_patterns": _clean_string_list(link.get("exclude_url_patterns")),
                        "article_wait_for_selector": _clean_optional_string(link.get("article_wait_for_selector")),
                        "article_content_selector": _clean_optional_string(link.get("article_content_selector")) or "body",
                        "force_playwright": (
                            bool(link.get("force_playwright"))
                            if link.get("force_playwright") is not None
                            else forced_type == "playwright"
                        ),
                    }
                )

    payload["sources"] = {"rss": rss_rows, "playwright": page_rows}

    domains = _dedupe_tokens(
        [str(profile.get("domain", "")).strip() for profile in profiles]
        + [str(payload.get("domain", "")).strip()]
        + [str(v).strip() for v in payload.get("domains", []) if isinstance(v, str)]
    )
    if domains:
        payload["domain"] = domains[0]
        payload["domains"] = domains

    strategy_profile = payload.get("strategy_profile")
    if not isinstance(strategy_profile, dict):
        strategy_profile = {}
    payload["strategy_profile"] = {
        "focus_areas": _dedupe_tokens(
            [str(v).strip() for v in strategy_profile.get("focus_areas", []) if isinstance(v, str)]
            + _collect_profile_tokens(profiles, "focus_areas")
        ),
        "entities": _dedupe_tokens(
            [str(v).strip() for v in strategy_profile.get("entities", []) if isinstance(v, str)]
            + _collect_profile_tokens(profiles, "entities")
        ),
        "keywords": _dedupe_tokens(
            [str(v).strip() for v in strategy_profile.get("keywords", []) if isinstance(v, str)]
            + _collect_profile_tokens(profiles, "keywords")
        ),
    }
    return payload


def _collect_profile_tokens(profiles: list[dict[str, Any]], key: str) -> list[str]:
    out: list[str] = []
    for profile in profiles:
        values = profile.get(key, [])
        if not isinstance(values, list):
            continue
        out.extend(str(v).strip() for v in values if isinstance(v, str) and str(v).strip())
    return out


def _parse_source_link(raw_link: Any) -> dict[str, Any] | None:
    if isinstance(raw_link, str):
        return _parse_source_link_string(raw_link)

    if not isinstance(raw_link, dict):
        return None

    url = _normalize_url(raw_link.get("url"))
    if not url:
        return None
    source_type = _normalize_source_link_type(str(raw_link.get("type", "auto")))
    return _build_source_link_payload(
        url=url,
        source_type=source_type,
        name=str(raw_link.get("name", "")).strip(),
        follow_links_enabled=bool(raw_link.get("follow_links_enabled", False)),
        max_depth=raw_link.get("max_depth"),
        max_links_per_source=raw_link.get("max_links_per_source"),
        same_domain_only=bool(raw_link.get("same_domain_only", True)),
        link_selector=raw_link.get("link_selector"),
        article_url_patterns=raw_link.get("article_url_patterns"),
        exclude_url_patterns=raw_link.get("exclude_url_patterns"),
        article_wait_for_selector=raw_link.get("article_wait_for_selector"),
        article_content_selector=raw_link.get("article_content_selector"),
        force_playwright=raw_link.get("force_playwright"),
    )


def _parse_source_link_string(raw_link: str) -> dict[str, Any] | None:
    token = raw_link.strip()
    if not token:
        return None

    lower = token.lower()
    prefix_pairs = (
        ("playwright:", "playwright"),
        ("pw:", "playwright"),
        ("rss:", "rss"),
        ("auto:", "auto"),
    )
    for prefix, source_type in prefix_pairs:
        if lower.startswith(prefix):
            url = _normalize_url(token[len(prefix) :].strip())
            if not url:
                return None
            return _build_source_link_payload(url=url, source_type=source_type)

    if "|" in token:
        parts = [part.strip() for part in token.split("|")]
        if len(parts) >= 2:
            source_type_token = parts[1].strip().lower()
            source_type = _normalize_source_link_type(source_type_token)
            url = _normalize_url(parts[0])
            if not url:
                return None
            if source_type_token in {"auto", "rss", "playwright", "pw", "feed", "atom", "browser", "page"}:
                name = parts[2].strip() if len(parts) >= 3 else ""
                return _build_source_link_payload(url=url, source_type=source_type, name=name)
            return _build_source_link_payload(url=url, source_type="auto")

    url = _normalize_url(token)
    if not url:
        return None
    return _build_source_link_payload(url=url, source_type="auto")


def _normalize_source_link_type(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"playwright", "pw", "browser", "page"}:
        return "playwright"
    if token in {"rss", "feed", "atom"}:
        return "rss"
    return "auto"


def _build_source_link_payload(
    *,
    url: str,
    source_type: str = "auto",
    name: str = "",
    follow_links_enabled: bool = False,
    max_depth: Any = 1,
    max_links_per_source: Any = 3,
    same_domain_only: bool = True,
    link_selector: Any = "a[href]",
    article_url_patterns: Any = None,
    exclude_url_patterns: Any = None,
    article_wait_for_selector: Any = None,
    article_content_selector: Any = "body",
    force_playwright: Any = None,
) -> dict[str, Any]:
    force_value: bool | None = None
    if force_playwright is not None:
        force_value = bool(force_playwright)
    return {
        "url": url,
        "type": source_type,
        "name": name,
        "follow_links_enabled": follow_links_enabled,
        "max_depth": _int_or_default(max_depth, default=1, minimum=1, maximum=2),
        "max_links_per_source": _int_or_default(max_links_per_source, default=3, minimum=1, maximum=20),
        "same_domain_only": same_domain_only,
        "link_selector": str(link_selector or "a[href]").strip() or "a[href]",
        "article_url_patterns": _clean_string_list(article_url_patterns),
        "exclude_url_patterns": _clean_string_list(exclude_url_patterns),
        "article_wait_for_selector": _clean_optional_string(article_wait_for_selector),
        "article_content_selector": _clean_optional_string(article_content_selector) or "body",
        "force_playwright": force_value,
    }


def _clean_optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    return token or None


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _int_or_default(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _normalize_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"https://{url}"
    return url


def _source_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _looks_like_rss(url: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered
        for token in (
            "/rss",
            "rss.xml",
            "/feed",
            ".atom",
            "format=rss",
            "output=rss",
            "hnrss.org",
        )
    ) or lowered.endswith(".xml")


def _infer_rss_max_items(url: str) -> int:
    host = _source_host(url)
    lowered = url.lower()
    if host in {"news.google.com", "hnrss.org"}:
        return 35
    if "arxiv.org" in host:
        return 15
    if any(token in lowered for token in ("/blog", "substack", "medium.com")):
        return 10
    return 20


def _infer_rss_timeout_seconds(url: str) -> int:
    host = _source_host(url)
    if host in {"news.google.com", "hnrss.org"}:
        return 12
    return 15


def _infer_page_max_chars(url: str) -> int:
    lowered = url.lower()
    if any(token in lowered for token in ("/blog", "/news", "substack", "medium.com")):
        return 9000
    return 6000


def _infer_page_timeout_ms(url: str) -> int:
    host = _source_host(url)
    if any(token in host for token in ("bloomberg.com", "wsj.com", "ft.com")):
        return 45000
    return 30000


def _dedupe_tokens(items: list[str]) -> list[str]:
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
