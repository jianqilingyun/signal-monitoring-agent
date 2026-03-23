from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Iterable


_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def make_fingerprint(*parts: str) -> str:
    normalized = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def tokenize(value: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT_RE.split(value.lower()) if t}


def jaccard_similarity(a: Iterable[str], b: Iterable[str]) -> float:
    set_a = set(a)
    set_b = set(b)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)
