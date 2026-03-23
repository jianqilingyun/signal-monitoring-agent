from __future__ import annotations

import re
from monitor_agent.core.utils import make_fingerprint

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+")


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for raw in _URL_RE.findall(text):
        url = raw.strip().rstrip(".,;，。；)")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def derive_title(text: str, urls: list[str], *, fallback_prefix: str = "Inbound signal") -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    if first and not _URL_RE.fullmatch(first):
        if len(first) > 60:
            return first[:57].rstrip() + "..."
        return first
    if urls:
        domain = urls[0].split("//", 1)[-1].split("/", 1)[0].replace("www.", "")
        return f"{fallback_prefix} - {domain}"
    return fallback_prefix


def derive_tracking_id(urls: list[str], text: str, *, prefix: str) -> str | None:
    if urls:
        return f"{prefix}_{make_fingerprint(urls[0])}"
    cleaned = text.strip()
    if cleaned:
        return f"{prefix}_{make_fingerprint(cleaned)[:12]}"
    return None


def extract_ingest_mode(text: str) -> tuple[str, str]:
    normalized = text.strip()
    if not normalized.startswith("/"):
        return "save_only", normalized

    lines = normalized.splitlines()
    first_line = lines[0].strip()
    remainder_lines = lines[1:]
    parts = first_line.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if remainder_lines:
        tail = "\n".join(line for line in remainder_lines if line.strip()).strip()
        if tail:
            rest = f"{rest}\n{tail}".strip() if rest else tail

    if command == "/brief":
        return "brief_now", rest
    if command == "/save":
        return "save_only", rest
    return "save_only", normalized


def help_text() -> str:
    return (
        f"把网页链接或相关文本直接发给我，我会帮你加入监控。\n"
        f"默认是保存到收件箱，等下一轮整理。\n"
        f"快捷命令：/save 保存，/brief 立即单篇简报，/help 查看说明。"
    )


def parse_id_list(value: str | None) -> set[str]:
    raw = str(value or "").replace("\n", ",")
    return {token.strip() for token in raw.split(",") if token.strip()}
