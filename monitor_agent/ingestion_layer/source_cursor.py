from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urldefrag, urlparse

from monitor_agent.core.models import SourceCursorState
from monitor_agent.core.utils import utc_now

_RECENT_ID_LIMIT = 200
_RECENT_URL_LIMIT = 200


def make_source_key(source_type: str, url: str) -> str:
    return f"{source_type.lower()}::{normalize_url(url)}"


def normalize_url(url: str) -> str:
    token = str(url or "").strip()
    if not token:
        return ""
    token = urldefrag(token)[0].strip()
    if "://" not in token:
        token = f"https://{token}"
    parsed = urlparse(token)
    scheme = parsed.scheme.lower() or "https"
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"
    return parsed._replace(scheme=scheme, netloc=host, path=path, params="", query=parsed.query, fragment="").geturl()


def cursor_from_mapping(
    payload: dict[str, object] | None,
    *,
    source_type: str,
    source_url: str,
    overlap_count: int = 2,
) -> SourceCursorState:
    if payload:
        try:
            state = SourceCursorState.model_validate(payload)
            state.source_key = make_source_key(source_type, source_url)
            state.source_type = source_type  # type: ignore[assignment]
            state.source_url = normalize_url(source_url)
            state.overlap_count = max(0, min(10, int(state.overlap_count)))
            return state
        except Exception:
            pass
    return SourceCursorState(
        source_key=make_source_key(source_type, source_url),
        source_type=source_type,  # type: ignore[arg-type]
        source_url=normalize_url(source_url),
        overlap_count=max(0, min(10, int(overlap_count))),
        incremental_mode="mixed",
    )


def filter_rss_rows(
    rows: list[dict[str, object]],
    cursor: SourceCursorState,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    if not rows:
        return [], {"candidate_count": 0, "kept_count": 0, "overlap_kept": 0, "dropped_count": 0}
    if not cursor.last_seen_published_at and not cursor.last_seen_ids and not cursor.last_seen_urls:
        return rows, {
            "candidate_count": len(rows),
            "kept_count": len(rows),
            "overlap_kept": 0,
            "dropped_count": 0,
        }

    seen_ids = {token.lower() for token in cursor.last_seen_ids}
    seen_urls = {normalize_url(token).lower() for token in cursor.last_seen_urls}
    overlap_limit = max(0, int(cursor.overlap_count))
    kept: list[dict[str, object]] = []
    overlap_kept = 0

    for idx, row in enumerate(rows):
        keep = False
        if idx < overlap_limit:
            keep = True
            overlap_kept += 1

        published_at = row.get("published_at")
        if isinstance(published_at, datetime) and cursor.last_seen_published_at is not None:
            if published_at.astimezone(UTC) > cursor.last_seen_published_at.astimezone(UTC):
                keep = True

        entry_id = str(row.get("entry_id") or "").strip().lower()
        if entry_id and entry_id not in seen_ids:
            keep = True

        link = normalize_url(str(row.get("link") or ""))
        if link and link.lower() not in seen_urls:
            keep = True

        if keep:
            kept.append(row)

    return kept, {
        "candidate_count": len(rows),
        "kept_count": len(kept),
        "overlap_kept": overlap_kept,
        "dropped_count": max(0, len(rows) - len(kept)),
    }


def advance_rss_cursor(rows: list[dict[str, object]], cursor: SourceCursorState) -> SourceCursorState:
    if not rows:
        return cursor

    latest_dt = cursor.last_seen_published_at
    recent_ids = [token for token in cursor.last_seen_ids if token.strip()]
    recent_urls = [normalize_url(token) for token in cursor.last_seen_urls if normalize_url(token)]

    for row in rows:
        published_at = row.get("published_at")
        if isinstance(published_at, datetime):
            normalized = published_at.astimezone(UTC)
            if latest_dt is None or normalized > latest_dt.astimezone(UTC):
                latest_dt = normalized

        entry_id = str(row.get("entry_id") or "").strip()
        if entry_id:
            recent_ids.append(entry_id)

        link = normalize_url(str(row.get("link") or ""))
        if link:
            recent_urls.append(link)

    cursor.last_seen_published_at = latest_dt
    cursor.last_seen_ids = _dedupe_tail(recent_ids, _RECENT_ID_LIMIT)
    cursor.last_seen_urls = _dedupe_tail(recent_urls, _RECENT_URL_LIMIT)
    cursor.last_success_at = utc_now()
    cursor.incremental_mode = "mixed" if cursor.last_seen_published_at else "id"
    return cursor


def filter_follow_urls(
    urls: list[str],
    cursor: SourceCursorState,
) -> tuple[list[str], dict[str, int]]:
    candidates = [{"url": url, "publish_time": None} for url in urls]
    kept, stats = filter_follow_candidates(candidates, cursor)
    return [str(row.get("url") or "") for row in kept if str(row.get("url") or "").strip()], stats


def filter_follow_candidates(
    candidates: list[dict[str, object]],
    cursor: SourceCursorState,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    normalized_candidates: list[dict[str, object]] = []
    for row in candidates:
        url = normalize_url(str(row.get("url") or ""))
        if not url:
            continue
        normalized_candidates.append(
            {
                "url": url,
                "publish_time": str(row.get("publish_time") or "").strip() or None,
            }
        )
    if not normalized_candidates:
        return [], {"candidate_count": 0, "kept_count": 0, "overlap_kept": 0, "dropped_count": 0}
    if not cursor.last_seen_urls and cursor.last_seen_published_at is None:
        return normalized_candidates, {
            "candidate_count": len(normalized_candidates),
            "kept_count": len(normalized_candidates),
            "overlap_kept": 0,
            "dropped_count": 0,
        }

    seen_urls = {token.lower() for token in cursor.last_seen_urls}
    overlap_limit = max(0, int(cursor.overlap_count))
    kept: list[dict[str, object]] = []
    overlap_kept = 0
    last_seen_dt = cursor.last_seen_published_at.astimezone(UTC) if cursor.last_seen_published_at else None

    for idx, row in enumerate(normalized_candidates):
        keep = False
        if idx < overlap_limit:
            keep = True
            overlap_kept += 1

        publish_time = _parse_candidate_dt(row.get("publish_time"))
        if publish_time is not None and last_seen_dt is not None and publish_time > last_seen_dt:
            keep = True

        if str(row.get("url") or "").lower() not in seen_urls:
            keep = True

        if keep:
            kept.append(row)

    return kept, {
        "candidate_count": len(normalized_candidates),
        "kept_count": len(kept),
        "overlap_kept": overlap_kept,
        "dropped_count": max(0, len(normalized_candidates) - len(kept)),
    }


def advance_url_cursor(urls: list[str] | list[dict[str, object]], cursor: SourceCursorState) -> SourceCursorState:
    normalized_urls: list[str] = []
    latest_dt = cursor.last_seen_published_at.astimezone(UTC) if cursor.last_seen_published_at else None
    for row in urls:
        if isinstance(row, dict):
            token = normalize_url(str(row.get("url") or ""))
            publish_time = _parse_candidate_dt(row.get("publish_time"))
        else:
            token = normalize_url(str(row or ""))
            publish_time = None
        if token:
            normalized_urls.append(token)
        if publish_time is not None and (latest_dt is None or publish_time > latest_dt):
            latest_dt = publish_time
    if not normalized_urls:
        return cursor
    cursor.last_seen_urls = _dedupe_tail(cursor.last_seen_urls + normalized_urls, _RECENT_URL_LIMIT)
    cursor.last_seen_published_at = latest_dt
    cursor.last_success_at = utc_now()
    cursor.incremental_mode = "mixed" if latest_dt is not None else "url"
    return cursor


def _dedupe_tail(items: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in reversed(items):
        token = str(raw or "").strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def _parse_candidate_dt(value: object) -> datetime | None:
    token = str(value or "").strip()
    if not token:
        return None
    try:
        normalized = token.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None
