from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from monitor_agent.briefing.localizer import BriefingLocalizer
from monitor_agent.core.models import Signal

logger = logging.getLogger(__name__)


class BriefingGenerator:
    def __init__(self, localizer: BriefingLocalizer | None = None) -> None:
        self.localizer = localizer
        self._compose_cache: dict[str, dict[str, dict[str, Any]]] = {}

    def generate(
        self,
        domain: str,
        signals: list[Signal],
        language: str = "zh",
        trends: list[dict[str, Any]] | None = None,
        watchlist: list[dict[str, Any]] | None = None,
        generated_at: datetime | None = None,
        diagnostics: dict[str, Any] | None = None,
        source_contexts: dict[str, str] | None = None,
    ) -> str:
        _ = diagnostics
        ts = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%SZ")
        normalized = self._normalize_signals(signals)
        context_map = self._normalize_source_contexts(source_contexts)
        lang = self._normalize_language(language)
        composed = self._compose_blocks(normalized, domain=domain, source_contexts=context_map, language=lang)
        brief_rows = [row for row in normalized if row["id"] in composed and not self._is_briefed_once(row)]

        if lang == "en":
            lines = [f"# Monitoring Brief - {domain}", f"_Generated: {ts}_", ""]
        else:
            lines = [f"# 监控简报 / Monitoring Brief - {domain}", f"_生成时间 / Generated: {ts}_", ""]

        rendered_sections = 0
        inbox_signals = [row for row in brief_rows if row["source"] == "user"]
        system_signals = [row for row in brief_rows if row["source"] != "user"]

        if inbox_signals:
            rendered_sections += 1
            lines.extend(["## Section 1: Inbox" if lang == "en" else "## Section 1: 收件箱重点 / Inbox", ""])
            for idx, row in enumerate(inbox_signals, start=1):
                lines.extend(
                    self._render_signal_card(
                        idx=idx,
                        row=row,
                        block=composed[row["id"]],
                        include_tracking=True,
                        language=lang,
                    )
                )

        if system_signals:
            rendered_sections += 1
            lines.extend(["## Section 2: System Signals" if lang == "en" else "## Section 2: 系统重点信号 / System Signals", ""])
            for idx, row in enumerate(system_signals, start=1):
                lines.extend(
                    self._render_signal_card(
                        idx=idx,
                        row=row,
                        block=composed[row["id"]],
                        include_tracking=False,
                        language=lang,
                    )
                )

        if rendered_sections == 0:
            lines.append("No high-signal updates matched this cycle." if lang == "en" else "本轮暂无与关注策略高度相关的更新。")

        return "\n".join(lines).strip()

    def generate_audio_script(
        self,
        domain: str,
        signals: list[Signal],
        language: str = "zh",
        trends: list[dict[str, Any]] | None = None,
        generated_at: datetime | None = None,
        source_contexts: dict[str, str] | None = None,
    ) -> str:
        ts = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%SZ")
        normalized = self._normalize_signals(signals)
        context_map = self._normalize_source_contexts(source_contexts)
        lang = self._normalize_language(language)
        composed = self._compose_blocks(normalized, domain=domain, source_contexts=context_map, language=lang)
        brief_rows = [row for row in normalized if row["id"] in composed and not self._is_briefed_once(row)]

        if not brief_rows:
            if lang == "en":
                return f"{domain} audio briefing, generated at {ts}. No high-signal updates matched this cycle."
            return f"{domain}中文语音简报，生成时间{ts}。本轮暂无与关注策略高度相关的更新。"

        if lang == "en":
            lines: list[str] = [f"{domain} audio briefing, generated at {ts}.", f"There are {len(brief_rows)} relevant updates in this cycle."]
        else:
            lines = [f"{domain}中文语音简报，生成时间{ts}。", f"本轮共 {len(brief_rows)} 条相关更新。"]
        inbox_signals = [row for row in brief_rows if row["source"] == "user"]
        system_signals = [row for row in brief_rows if row["source"] != "user"]

        if inbox_signals:
            lines.append("First, inbox items." if lang == "en" else "先看收件箱重点。")
            for idx, row in enumerate(inbox_signals[:3], start=1):
                block = composed[row["id"]]
                title = str(block.get("en_title") or row["title"]) if lang == "en" else str(block.get("zh_title") or row["title"])
                what_key = "en_what_happened" if lang == "en" else "zh_what_happened"
                why_key = "en_why_it_matters" if lang == "en" else "zh_why_it_matters"
                what = self._strip_terminal_punct(self._truncate_sentence(str(block.get(what_key) or ""), 150))
                why = self._strip_terminal_punct(self._truncate_sentence(str(block.get(why_key) or ""), 120))
                follow = self._first_follow_up(block, language=lang)
                if lang == "en":
                    lines.append(f"Inbox item {idx}, {title}. {what}. Why it matters: {why}.")
                else:
                    lines.append(f"收件箱第{idx}条，{title}。{what}。影响上，{why}。")
                if follow:
                    lines.append(f"Follow-up: {follow}" if lang == "en" else f"跟踪点：{follow}")

        if system_signals:
            lines.append("Next, system signals." if lang == "en" else "再看系统重点信号。")
            for idx, row in enumerate(system_signals[:6], start=1):
                block = composed[row["id"]]
                title = str(block.get("en_title") or row["title"]) if lang == "en" else str(block.get("zh_title") or row["title"])
                status = ("update" if row["event_type"] == "update" else "new event") if lang == "en" else ("事件更新" if row["event_type"] == "update" else "新事件")
                what_key = "en_what_happened" if lang == "en" else "zh_what_happened"
                why_key = "en_why_it_matters" if lang == "en" else "zh_why_it_matters"
                what = self._strip_terminal_punct(self._truncate_sentence(str(block.get(what_key) or ""), 160))
                why = self._strip_terminal_punct(self._truncate_sentence(str(block.get(why_key) or ""), 120))
                follow = self._first_follow_up(block, language=lang)
                if lang == "en":
                    lines.append(f"Item {idx}, {title}, classified as {status}. {what}.")
                else:
                    lines.append(f"第{idx}条，{title}，属于{status}。{what}。")
                if why:
                    lines.append(f"Importance: {why}." if lang == "en" else f"重要性在于：{why}。")
                if follow:
                    lines.append(f"Follow-up: {follow}" if lang == "en" else f"后续关注：{follow}")

        lines.append("End of briefing." if lang == "en" else "以上为本轮简报。")
        return " ".join(self._sanitize_audio_line(line) for line in lines if line.strip()).strip()

    def _render_signal_card(
        self,
        *,
        idx: int,
        row: dict[str, Any],
        block: dict[str, Any],
        include_tracking: bool,
        language: str,
    ) -> list[str]:
        lang = self._normalize_language(language)
        title = str(block.get("en_title") or row["title"]).strip() if lang == "en" else str(block.get("zh_title") or row["title"]).strip()
        what_key = "en_what_happened" if lang == "en" else "zh_what_happened"
        why_key = "en_why_it_matters" if lang == "en" else "zh_why_it_matters"
        follow_key = "en_follow_up" if lang == "en" else "zh_follow_up"
        what = str(block.get(what_key) or row["summary"]).strip()
        why = str(block.get(why_key) or "").strip()
        follow = [str(v).strip() for v in block.get(follow_key, []) if str(v).strip()]
        en_note = str(block.get("en_note") or "").strip()
        source_links = self._format_source_links(row["source_urls"], language=lang)
        include_en_note = os.getenv("BRIEFING_INCLUDE_EN_NOTE", "0").strip() == "1"

        lines = [f"### {idx}. {title}"]
        if include_tracking and row.get("tracking_id"):
            lines.append(f"**Tracking ID** `{row['tracking_id']}`" if lang == "en" else f"**追踪ID** `{row['tracking_id']}`")
        lines.extend(
            [
                "",
                "**What Happened**" if lang == "en" else "**发生了什么**",
                what,
                "",
                "**Why It Matters**" if lang == "en" else "**为什么重要**",
                why or ("This update is directly relevant to the current monitoring topic." if lang == "en" else "该更新与当前监控主题直接相关。"),
                "",
                "**Follow-Up**" if lang == "en" else "**后续跟踪**",
            ]
        )
        if follow:
            for point in follow[:2]:
                lines.append(f"- {point}")
        else:
            lines.append("- Track whether additional details appear in the next 24-72 hours." if lang == "en" else "- 继续跟踪后续 24-72 小时内是否出现增量披露。")

        if include_en_note and en_note:
            lines.extend(["", f"**English Note** {en_note}" if lang == "en" else f"**英文附注** {en_note}"])
        if source_links:
            lines.extend(["", f"**Sources** {' '.join(source_links)}" if lang == "en" else f"**来源** {' '.join(source_links)}"])
        lines.append("")
        return lines

    def build_signal_cards(
        self,
        *,
        domain: str,
        signals: list[Signal],
        language: str = "zh",
        source_contexts: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        normalized = self._normalize_signals(signals)
        context_map = self._normalize_source_contexts(source_contexts)
        lang = self._normalize_language(language)
        composed = self._compose_blocks(normalized, domain=domain, source_contexts=context_map, language=lang)
        cards: list[dict[str, Any]] = []
        for row in normalized:
            if self._is_briefed_once(row):
                continue
            block = composed.get(row["id"])
            if not block:
                continue
            title_key = "en_title" if lang == "en" else "zh_title"
            what_key = "en_what_happened" if lang == "en" else "zh_what_happened"
            why_key = "en_why_it_matters" if lang == "en" else "zh_why_it_matters"
            follow_key = "en_follow_up" if lang == "en" else "zh_follow_up"
            what = str(block.get(what_key) or row["summary"]).strip()
            why = str(block.get(why_key) or "").strip()
            follow = [str(v).strip() for v in block.get(follow_key, []) if str(v).strip()]
            if not follow:
                follow = ["Track whether additional details appear in the next 24-72 hours."] if lang == "en" else ["继续关注未来 24-72 小时是否出现增量披露。"]
            cards.append(
                {
                    "id": row["id"],
                    "title": str(block.get(title_key) or row["title"]).strip(),
                    "what": what,
                    "why": why,
                    "follow_up": follow[:2],
                    "source_links": self._source_link_rows(row["source_urls"], language=lang),
                    "event_type": row["event_type"],
                    "source": row["source"],
                    "language": lang,
                }
            )
        return cards

    def _compose_blocks(
        self,
        normalized: list[dict[str, Any]],
        *,
        domain: str,
        source_contexts: dict[str, str],
        language: str,
    ) -> dict[str, dict[str, Any]]:
        if self._normalize_language(language) == "en":
            return self._compose_en_blocks(normalized, domain=domain)
        return self._compose_cn_blocks(normalized, domain=domain, source_contexts=source_contexts)

    def _compose_cn_blocks(
        self,
        normalized: list[dict[str, Any]],
        *,
        domain: str,
        source_contexts: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        if not normalized:
            return {}
        allow_template_fallback = self.localizer is None or os.getenv("BRIEFING_ALLOW_TEMPLATE_FALLBACK", "0").strip() == "1"

        cache_key = self._compose_cache_key(normalized, source_contexts=source_contexts, domain=domain)
        if cache_key in self._compose_cache:
            return self._compose_cache[cache_key]

        payload: list[dict[str, Any]] = []
        for row in normalized:
            payload.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "evidence": row["evidence"],
                    "tags": row["tags"],
                    "event_type": row["event_type"],
                    "freshness": row["freshness"],
                    "publish_time": row["publish_time"],
                    "source_hosts": row["hosts"],
                    "source_context": source_contexts.get(row["id"], self._build_source_context(row)),
                }
            )

        blocks: dict[str, dict[str, Any]] = {}
        if self.localizer is not None:
            blocks = self.localizer.compose_cn_brief_blocks(payload, domain=domain)

        if allow_template_fallback:
            for row in normalized:
                sid = row["id"]
                if sid not in blocks:
                    blocks[sid] = self._fallback_cn_block(row)
        else:
            missing = [row["id"] for row in normalized if row["id"] not in blocks]
            if missing:
                logger.warning(
                    "Brief generator dropped %d signals because template fallback is disabled; missing_ids=%s",
                    len(missing),
                    ",".join(missing[:5]),
                )

        if len(self._compose_cache) > 8:
            self._compose_cache = {}
        self._compose_cache[cache_key] = blocks
        return blocks

    @staticmethod
    def _compose_cache_key(
        normalized: list[dict[str, Any]],
        *,
        source_contexts: dict[str, str],
        domain: str,
    ) -> str:
        body = "|".join(
            f"{row['id']}::{row['title']}::{row['summary']}::{row['event_type']}::{row['freshness']}"
            for row in normalized
        )
        contexts = "|".join(f"{k}:{v}" for k, v in sorted(source_contexts.items()))
        raw = f"{domain}::{body}::{contexts}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _fallback_cn_block(self, row: dict[str, Any]) -> dict[str, Any]:
        summary = str(row.get("summary") or "").strip()
        evidence = [str(v).strip() for v in row.get("evidence", []) if str(v).strip()]
        tags = [str(v).strip() for v in row.get("tags", []) if str(v).strip()]
        what = f"该更新聚焦于“{row['title']}”。"
        if summary:
            what += f" 具体内容：{summary}"
        elif evidence:
            what += f" 具体内容：{evidence[0]}"
        elif tags:
            what += f" 主要涉及 {'、'.join(tags[:3])} 相关动态。"
        else:
            what += " 该事件来自已配置监控源的最新披露。"
        if evidence:
            fact_hint = evidence[1] if len(evidence) > 1 else evidence[0]
            what += f" 关键信息：{fact_hint}"

        if tags:
            topic = "、".join(tags[:3])
            why = f"该事件可能影响 {topic} 相关判断，尤其是资源投入优先级与后续策略选择。"
        else:
            why = "该事件与当前监控主题相关，可能影响短期判断和后续跟踪节奏。"
        if row["event_type"] == "update":
            why += " 由于其属于既有事件更新，需重点关注是否出现方向性变化。"

        host_hint = "、".join(row["hosts"][:2]) if row["hosts"] else "核心来源"
        follow = [
            f"继续跟踪 {host_hint} 在未来 24-72 小时是否披露增量信息。",
            "确认该更新是否改变你当前关注事项的优先级排序。",
        ]

        return {
            "zh_title": row["title"],
            "zh_what_happened": what,
            "zh_why_it_matters": why,
            "zh_follow_up": follow,
            "en_note": row["title"],
        }

    def _build_source_context(self, row: dict[str, Any]) -> str:
        parts = [
            f"title: {row['title']}",
            f"summary: {row['summary']}",
        ]
        if row["evidence"]:
            parts.append("evidence: " + " | ".join(row["evidence"][:3]))
        if row["tags"]:
            parts.append("tags: " + ", ".join(row["tags"][:6]))
        if row["source_urls"]:
            parts.append("sources: " + " | ".join(row["source_urls"][:3]))
        return "\n".join(parts)

    def _format_source_links(self, urls: list[str], *, language: str = "zh") -> list[str]:
        return [f"[{row['label']}]({row['url']})" for row in self._source_link_rows(urls, language=language)]

    def _source_link_rows(self, urls: list[str], *, language: str = "zh") -> list[dict[str, str]]:
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            token = str(url or "").strip()
            if not token:
                continue
            if token in seen:
                continue
            seen.add(token)
            deduped.append(token)
            if len(deduped) >= 3:
                break
        if not deduped:
            return []

        host_counts = Counter(self._host_only(url) for url in deduped)
        host_seen: dict[str, int] = {}
        links: list[dict[str, str]] = []
        for url in deduped:
            host = self._host_only(url)
            host_seen[host] = host_seen.get(host, 0) + 1
            label = self._source_label(
                url,
                duplicate_in_host=host_counts[host] > 1,
                host_idx=host_seen[host],
                language=language,
            )
            links.append({"label": label, "url": url})
        return links

    def _source_label(self, url: str, *, duplicate_in_host: bool, host_idx: int, language: str = "zh") -> str:
        host = self._host_only(url)
        publisher = self._publisher_name(host, language=language)
        if duplicate_in_host:
            topic = self._url_topic(url)
            if topic:
                return f"{publisher}·{topic}"
            return f"{publisher} {host_idx}"
        return publisher

    @staticmethod
    def _publisher_name(host: str, *, language: str = "zh") -> str:
        mapping_zh = {
            "mp.weixin.qq.com": "微信公众号",
            "blogs.nvidia.com": "NVIDIA 博客",
            "nvidianews.nvidia.com": "NVIDIA 新闻",
            "openai.com": "OpenAI",
            "anthropic.com": "Anthropic",
            "techcrunch.com": "TechCrunch",
            "aws.amazon.com": "AWS",
            "cloud.google.com": "Google Cloud",
            "azure.microsoft.com": "Azure",
            "semianalysis.com": "SemiAnalysis",
        }
        mapping_en = dict(mapping_zh)
        mapping_en["mp.weixin.qq.com"] = "WeChat"
        mapping_en["blogs.nvidia.com"] = "NVIDIA Blog"
        mapping_en["nvidianews.nvidia.com"] = "NVIDIA News"
        mapping = mapping_en if language == "en" else mapping_zh
        if host in mapping:
            return mapping[host]
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return "Source" if language == "en" else "来源"
        token = host.split(".")[0]
        if not token:
            return "Source" if language == "en" else "来源"
        return token[:1].upper() + token[1:]

    @staticmethod
    def _url_topic(url: str) -> str:
        try:
            parsed = urlparse(url)
            tail = (parsed.path or "").strip("/").split("/")[-1]
            if not tail:
                return ""
            tail = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", tail)
            tail = tail.replace("-", " ").replace("_", " ").strip()
            words = [w for w in tail.split() if w]
            if not words:
                return ""
            topic = " ".join(words[:3])
            if len(topic) > 20:
                return topic[:17].rstrip() + "..."
            return topic
        except Exception:
            return ""

    def _normalize_signals(self, signals: list[Signal]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for idx, signal in enumerate(signals, start=1):
            sid = str(self._signal_get(signal, "id", f"sig-{idx}"))
            title = str(self._signal_get(signal, "title", "Untitled Signal"))
            summary = str(self._signal_get(signal, "summary", "")).strip() or "No summary available."
            source = str(self._signal_get(signal, "source", "system")).strip().lower() or "system"
            event_type = str(self._signal_get(signal, "event_type", "new")).strip().lower() or "new"
            if event_type not in {"new", "update", "duplicate"}:
                event_type = "new"
            freshness = str(self._signal_get(signal, "freshness", "unknown")).strip().lower() or "unknown"
            if freshness not in {"fresh", "recent", "stale", "unknown"}:
                freshness = "unknown"
            source_urls = [u for u in self._as_list(self._signal_get(signal, "source_urls", [])) if u]
            tags = [v for v in self._as_list(self._signal_get(signal, "tags", [])) if v]
            evidence = [v for v in self._as_list(self._signal_get(signal, "evidence", [])) if v]
            latest_updates = [v for v in self._as_list(self._signal_get(signal, "latest_updates", [])) if v]
            briefed_once_at = self._to_iso(self._signal_get(signal, "briefed_once_at", None))
            hosts = [self._host_only(url) for url in source_urls if self._host_only(url)]
            publish_time = self._to_iso(self._signal_get(signal, "publish_time", None) or self._signal_get(signal, "published_at", None))
            normalized.append(
                {
                    "id": sid,
                    "title": title,
                    "summary": summary,
                    "source": source,
                    "event_type": event_type,
                    "freshness": freshness,
                    "source_urls": source_urls,
                    "tags": tags,
                    "evidence": evidence,
                    "hosts": hosts,
                    "publish_time": publish_time,
                    "tracking_id": str(self._signal_get(signal, "tracking_id", "") or "").strip(),
                    "user_context": str(self._signal_get(signal, "user_context", "") or "").strip(),
                    "latest_updates": latest_updates,
                    "system_interpretation": str(self._signal_get(signal, "system_interpretation", "") or "").strip(),
                    "briefed_once_at": briefed_once_at,
                }
            )
        return normalized

    @staticmethod
    def _is_briefed_once(row: dict[str, Any]) -> bool:
        token = str(row.get("briefed_once_at") or "").strip()
        return bool(token)

    @staticmethod
    def _normalize_source_contexts(source_contexts: dict[str, str] | None) -> dict[str, str]:
        if not isinstance(source_contexts, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in source_contexts.items():
            sid = str(key).strip()
            ctx = str(value or "").strip()
            if sid and ctx:
                out[sid] = ctx
        return out

    @staticmethod
    def _to_iso(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC).isoformat()
        token = str(value).strip()
        return token or None

    @staticmethod
    def _signal_get(signal: Any, key: str, default: Any = None) -> Any:
        if isinstance(signal, dict):
            return signal.get(key, default)
        return getattr(signal, key, default)

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return []

    @staticmethod
    def _host_only(url: str) -> str:
        try:
            host = (urlparse(url).netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    @staticmethod
    def _first_follow_up(block: dict[str, Any], *, language: str = "zh") -> str:
        values = block.get("en_follow_up" if language == "en" else "zh_follow_up", [])
        if isinstance(values, list) and values:
            return str(values[0]).strip()
        return ""

    @staticmethod
    def _normalize_language(language: str | None) -> str:
        return "en" if str(language or "").strip().lower() == "en" else "zh"

    def _compose_en_blocks(self, normalized: list[dict[str, Any]], *, domain: str) -> dict[str, dict[str, Any]]:
        if not normalized:
            return {}
        if self.localizer is not None:
            payload = [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "event_type": row["event_type"],
                    "freshness": row["freshness"],
                    "tags": row["tags"],
                    "hosts": row["hosts"],
                }
                for row in normalized
            ]
            bilingual = self.localizer.compose_signal_blocks(payload, domain=domain)
            if bilingual:
                out: dict[str, dict[str, Any]] = {}
                for row in normalized:
                    sid = row["id"]
                    candidate = bilingual.get(sid, {})
                    out[sid] = {
                        "en_title": str(candidate.get("en_title") or row["title"]).strip(),
                        "en_what_happened": str(candidate.get("en_paragraph") or row["summary"]).strip(),
                        "en_why_it_matters": self._fallback_en_why(row),
                        "en_follow_up": candidate.get("en_watchpoints") or self._fallback_en_follow(row),
                    }
                return out

        out: dict[str, dict[str, Any]] = {}
        for row in normalized:
            out[row["id"]] = {
                "en_title": row["title"],
                "en_what_happened": self._fallback_en_what(row),
                "en_why_it_matters": self._fallback_en_why(row),
                "en_follow_up": self._fallback_en_follow(row),
            }
        return out

    @staticmethod
    def _fallback_en_what(row: dict[str, Any]) -> str:
        summary = str(row.get("summary") or "").strip()
        evidence = [str(v).strip() for v in row.get("evidence", []) if str(v).strip()]
        if summary:
            what = summary
        elif evidence:
            what = evidence[0]
        else:
            what = "This update comes from a configured monitoring source."
        if evidence:
            fact_hint = evidence[1] if len(evidence) > 1 else evidence[0]
            what += f" Key detail: {fact_hint}"
        return what.strip()

    @staticmethod
    def _fallback_en_why(row: dict[str, Any]) -> str:
        tags = [str(v).strip() for v in row.get("tags", []) if str(v).strip()]
        if tags:
            topic = ", ".join(tags[:3])
            why = f"This may affect judgments around {topic}, especially near-term prioritization and resource allocation."
        else:
            why = "This update is directly relevant to the current monitoring topic and may affect short-term decisions."
        if row.get("event_type") == "update":
            why += " Because it is an update to an existing event, the main question is whether the direction has changed."
        return why

    @staticmethod
    def _fallback_en_follow(row: dict[str, Any]) -> list[str]:
        hosts = [str(v).strip() for v in row.get("hosts", []) if str(v).strip()]
        host_hint = ", ".join(hosts[:2]) if hosts else "core sources"
        return [
            f"Track whether {host_hint} publish incremental details in the next 24-72 hours.",
            "Check whether this changes the current priority order for the topic.",
        ]

    @staticmethod
    def _truncate_sentence(text: str, limit: int) -> str:
        token = re.sub(r"\s+", " ", str(text or "").strip())
        if len(token) <= limit:
            return token
        clipped = token[: limit - 1].rstrip("，,;；:：。")
        return f"{clipped}。"

    @staticmethod
    def _strip_terminal_punct(text: str) -> str:
        return str(text or "").strip().rstrip("。.!！?？")

    @staticmethod
    def _sanitize_audio_line(text: str) -> str:
        token = str(text or "").strip()
        token = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", token)
        token = re.sub(r"http[s]?://\S+", "", token)
        token = re.sub(r"\s+", " ", token).strip()
        return token

    @staticmethod
    def _direction_zh(direction: str) -> str:
        token = direction.strip().lower()
        mapping = {
            "increasing": "上升",
            "decreasing": "下降",
            "stable": "平稳",
            "active": "活跃",
        }
        return mapping.get(token, token or "平稳")
