from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from openai import OpenAI

from monitor_agent.core.models import LLMConfig

logger = logging.getLogger(__name__)
LLM_TIMEOUT_SECONDS = int(os.getenv("BRIEFING_LLM_TIMEOUT_SECONDS", "25"))


class BriefingLocalizer:
    """Translate briefing fragments into Chinese with lightweight quality checks."""

    def __init__(self, llm_config: LLMConfig | None = None) -> None:
        self.llm_config = llm_config
        self._cache: dict[str, str] = {}
        self.client: OpenAI | None = None

        if llm_config is None:
            return
        api_key = os.getenv("OPENAI_API_KEY")
        if llm_config.base_url and not api_key:
            api_key = "dummy"
        if api_key:
            self.client = OpenAI(api_key=api_key, base_url=llm_config.base_url)

    def translate_to_zh(self, texts: list[str], *, domain: str = "") -> list[str]:
        if not texts:
            return []

        cleaned = [str(v or "").strip() for v in texts]
        pending: list[str] = []
        pending_keys: list[str] = []
        for text in cleaned:
            if not text:
                continue
            if text in self._cache:
                continue
            pending.append(text)
            pending_keys.append(text)

        if pending:
            translated = self._translate_batch(pending, domain=domain) if self.client else pending
            for src, zh in zip(pending_keys, translated):
                value = zh.strip() if zh and zh.strip() else src
                if not self._passes_basic_quality(src, value):
                    repaired = self._repair_single(src, value, domain=domain)
                    value = repaired if repaired and repaired.strip() else src
                self._cache[src] = value

        out: list[str] = []
        for text in cleaned:
            out.append(self._cache.get(text, text))
        return out

    def compose_signal_blocks(
        self,
        signals: list[dict[str, Any]],
        *,
        domain: str = "",
    ) -> dict[str, dict[str, Any]]:
        if not signals:
            return {}

        normalized: list[dict[str, Any]] = []
        for row in signals:
            signal_id = str(row.get("id", "")).strip()
            if not signal_id:
                continue
            normalized.append(
                {
                    "id": signal_id,
                    "title": str(row.get("title", "")).strip(),
                    "summary": str(row.get("summary", "")).strip(),
                    "event_type": str(row.get("event_type", "new")).strip().lower() or "new",
                    "freshness": str(row.get("freshness", "unknown")).strip().lower() or "unknown",
                    "tags": [str(v).strip() for v in row.get("tags", []) if str(v).strip()],
                    "hosts": [str(v).strip() for v in row.get("hosts", []) if str(v).strip()],
                }
            )
        if not normalized:
            return {}

        llm_blocks = self._compose_signal_blocks_llm(normalized, domain=domain)
        if llm_blocks:
            merged: dict[str, dict[str, Any]] = {}
            fallback = self._compose_signal_blocks_fallback(normalized, domain=domain)
            for row in normalized:
                sid = row["id"]
                candidate = llm_blocks.get(sid)
                if candidate is None:
                    merged[sid] = fallback[sid]
                    continue
                if not self._is_composed_block_valid(candidate):
                    merged[sid] = fallback[sid]
                    continue
                merged[sid] = candidate
            return merged

        return self._compose_signal_blocks_fallback(normalized, domain=domain)

    def compose_cn_brief_blocks(
        self,
        signals: list[dict[str, Any]],
        *,
        domain: str = "",
    ) -> dict[str, dict[str, Any]]:
        if not signals:
            return {}

        normalized: list[dict[str, Any]] = []
        for row in signals:
            signal_id = str(row.get("id", "")).strip()
            if not signal_id:
                continue
            normalized.append(
                {
                    "id": signal_id,
                    "title": str(row.get("title", "")).strip(),
                    "summary": str(row.get("summary", "")).strip(),
                    "evidence": self._clean_list(row.get("evidence"), limit=4),
                    "tags": self._clean_list(row.get("tags"), limit=6),
                    "event_type": str(row.get("event_type", "new")).strip().lower() or "new",
                    "freshness": str(row.get("freshness", "unknown")).strip().lower() or "unknown",
                    "publish_time": str(row.get("publish_time", "") or "").strip(),
                    "source_hosts": self._clean_list(row.get("source_hosts"), limit=4),
                    "source_context": str(row.get("source_context", "") or "").strip(),
                }
            )

        if not normalized:
            return {}

        if self.client is None or self.llm_config is None:
            logger.warning("Brief localizer disabled: no LLM client available")
            return {}

        llm_blocks: dict[str, dict[str, Any]] = {}
        for row in normalized:
            single = self._compose_cn_brief_block_single(row, domain=domain)
            if single and self._is_cn_brief_block_valid(single):
                llm_blocks[row["id"]] = single

        merged: dict[str, dict[str, Any]] = {}
        dropped_count = 0
        for row in normalized:
            sid = row["id"]
            candidate = llm_blocks.get(sid)
            if candidate is not None and self._is_cn_brief_block_valid(candidate):
                merged[sid] = candidate
            else:
                dropped_count += 1
        if dropped_count:
            logger.warning("Brief localizer dropped %d/%d signals due to LLM quality/timeout", dropped_count, len(normalized))
        logger.info("Brief localizer produced %d/%d high-quality LLM blocks", len(merged), len(normalized))
        return merged

    def _translate_batch(self, texts: list[str], *, domain: str) -> list[str]:
        if self.client is None or self.llm_config is None:
            return texts
        payload = {
            "domain": domain,
            "texts": texts,
            "instruction": (
                "Translate each English text into natural Simplified Chinese for executive briefing. "
                "Keep proper nouns, product names, company names, numbers, and dates accurate. "
                "Do not add facts. Return JSON only."
            ),
        }
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are a professional bilingual analyst translator. "
                    "Return JSON object with key 'translations' as array matching input order."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for _ in range(2):
            try:
                resp = self.client.chat.completions.create(
                    model=self.llm_config.model,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=messages,
                    timeout=LLM_TIMEOUT_SECONDS,
                )
                parsed = json.loads(resp.choices[0].message.content or "{}")
                values = parsed.get("translations", [])
                if isinstance(values, list) and len(values) == len(texts):
                    return [str(v or "").strip() for v in values]
                messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Output invalid. Need exactly {len(texts)} items in translations array. "
                            "Re-output strict JSON only."
                        ),
                    }
                )
            except Exception:
                return texts
        return texts

    def _compose_signal_blocks_llm(
        self,
        signals: list[dict[str, Any]],
        *,
        domain: str,
    ) -> dict[str, dict[str, Any]]:
        if self.client is None or self.llm_config is None:
            return {}
        payload = {
            "domain": domain,
            "signals": signals,
            "instruction": (
                "For each signal, produce polished bilingual briefing blocks. "
                "Each item must include: id, zh_title, en_title, zh_paragraph, en_paragraph, "
                "zh_watchpoints (2 items), en_watchpoints (2 items). "
                "zh_paragraph should be 2-4 Chinese sentences with analysis depth; "
                "en_paragraph should be 2-4 English sentences. "
                "Do not invent facts beyond provided signal input."
            ),
        }
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are a bilingual monitoring editor. "
                    "Return strict JSON only with key 'items'."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for _ in range(2):
            try:
                resp = self.client.chat.completions.create(
                    model=self.llm_config.model,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=messages,
                    timeout=LLM_TIMEOUT_SECONDS,
                )
                parsed = json.loads(resp.choices[0].message.content or "{}")
                items = parsed.get("items", [])
                if not isinstance(items, list):
                    raise ValueError("items must be list")
                out: dict[str, dict[str, Any]] = {}
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sid = str(item.get("id", "")).strip()
                    if not sid:
                        continue
                    out[sid] = {
                        "zh_title": str(item.get("zh_title", "")).strip(),
                        "en_title": str(item.get("en_title", "")).strip(),
                        "zh_paragraph": str(item.get("zh_paragraph", "")).strip(),
                        "en_paragraph": str(item.get("en_paragraph", "")).strip(),
                        "zh_watchpoints": self._clean_list(item.get("zh_watchpoints"), limit=3),
                        "en_watchpoints": self._clean_list(item.get("en_watchpoints"), limit=3),
                    }
                if out:
                    return out
                raise ValueError("empty items")
            except Exception as exc:
                messages.append({"role": "assistant", "content": f"invalid: {exc}"})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Re-output strict JSON only: "
                            "{'items':[{'id','zh_title','en_title','zh_paragraph','en_paragraph','zh_watchpoints','en_watchpoints'}]}"
                        ),
                    }
                )
        return {}

    def _compose_cn_brief_blocks_llm(
        self,
        signals: list[dict[str, Any]],
        *,
        domain: str,
    ) -> dict[str, dict[str, Any]]:
        if self.client is None or self.llm_config is None:
            return {}

        payload = {
            "domain": domain,
            "signals": signals,
            "instruction": (
                "请直接基于每条信号的原始上下文，生成中文简报内容。"
                "不要先写英文再翻译。"
                "每条输出字段: id, zh_title, zh_what_happened, zh_why_it_matters, zh_follow_up(1-2条), en_note。"
                "zh_what_happened 必须具体说明事件发生了什么，可引用 summary/evidence/source_context 的事实。"
                "优先写出主体、动作、对象、关键数字或合作方。"
                "zh_what_happened 用 2-4 句、至少 60 个中文字符，不要写“该更新围绕”这类空模板。"
                "zh_why_it_matters 解释影响，不要写空话，2-3 句、至少 40 个中文字符。"
                "不要在正文里复读系统处理标签，如 event_type/freshness/时间戳字段名。"
                "en_note 仅保留一句英文附注（标题或关键术语）。"
                "不得编造事实。"
            ),
        }
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "你是专业中文科技情报编辑。"
                    "返回严格 JSON，对象格式为 {\"items\": [...]}。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        for _ in range(2):
            try:
                resp = self.client.chat.completions.create(
                    model=self.llm_config.model,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=messages,
                    timeout=LLM_TIMEOUT_SECONDS,
                )
                parsed = json.loads(resp.choices[0].message.content or "{}")
                items = parsed.get("items", [])
                if not isinstance(items, list):
                    raise ValueError("items must be list")

                out: dict[str, dict[str, Any]] = {}
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sid = str(item.get("id", "")).strip()
                    if not sid:
                        continue
                    out[sid] = {
                        "zh_title": str(item.get("zh_title", "")).strip(),
                        "zh_what_happened": str(item.get("zh_what_happened", "")).strip(),
                        "zh_why_it_matters": str(item.get("zh_why_it_matters", "")).strip(),
                        "zh_follow_up": self._clean_list(item.get("zh_follow_up"), limit=2),
                        "en_note": str(item.get("en_note", "")).strip(),
                    }
                if out:
                    return out
                raise ValueError("empty items")
            except Exception as exc:
                messages.append({"role": "assistant", "content": f"invalid: {exc}"})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "请重新输出严格 JSON，仅包含 items 数组，"
                            "每项包含 id, zh_title, zh_what_happened, zh_why_it_matters, zh_follow_up, en_note。"
                        ),
                    }
                )
        logger.warning("Brief localizer batch LLM compose failed after retries")
        return {}

    def _compose_cn_brief_block_single(
        self,
        signal: dict[str, Any],
        *,
        domain: str,
    ) -> dict[str, Any]:
        if self.client is None or self.llm_config is None:
            return {}

        payload = {
            "domain": domain,
            "signals": [signal],
            "instruction": (
                "你只需要处理一条信号，直接输出高质量中文简报块。"
                "字段: id, zh_title, zh_what_happened, zh_why_it_matters, zh_follow_up(1-2条), en_note。"
                "zh_what_happened 必须具体、包含事实细节，不得空泛。"
                "zh_why_it_matters 说明业务/战略影响。"
                "不要复读 event_type/freshness/时间戳字段名。"
            ),
        }
        messages = [
            {"role": "system", "content": "你是中文情报编辑，请输出严格 JSON。"},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for _ in range(2):
            try:
                resp = self.client.chat.completions.create(
                    model=self.llm_config.model,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                    messages=messages,
                    timeout=LLM_TIMEOUT_SECONDS,
                )
                parsed = json.loads(resp.choices[0].message.content or "{}")
                items = parsed.get("items", [])
                if isinstance(items, list) and items:
                    item = items[0] if isinstance(items[0], dict) else {}
                elif isinstance(parsed, dict):
                    item = parsed
                else:
                    raise ValueError("invalid payload")
                out = {
                    "zh_title": str(item.get("zh_title", "")).strip(),
                    "zh_what_happened": str(item.get("zh_what_happened", "")).strip(),
                    "zh_why_it_matters": str(item.get("zh_why_it_matters", "")).strip(),
                    "zh_follow_up": self._clean_list(item.get("zh_follow_up"), limit=2),
                    "en_note": str(item.get("en_note", "")).strip(),
                }
                if self._is_cn_brief_block_valid(out):
                    return out
                raise ValueError("quality gate failed")
            except Exception as exc:
                messages.append({"role": "assistant", "content": f"invalid: {exc}"})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一次输出不满足要求。请重写并确保："
                            "1) 发生了什么至少60字，含具体事实；"
                            "2) 为什么重要至少40字，含影响判断；"
                            "3) 全文以中文为主，不要空泛模板句。"
                        ),
                    }
                )
        return {}

    def _compose_signal_blocks_fallback(
        self,
        signals: list[dict[str, Any]],
        *,
        domain: str,
    ) -> dict[str, dict[str, Any]]:
        titles = [row["title"] for row in signals]
        summaries = [row["summary"] for row in signals]
        zh_titles = self.translate_to_zh(titles, domain=domain)
        zh_summaries = self.translate_to_zh(summaries, domain=domain)

        out: dict[str, dict[str, Any]] = {}
        for idx, row in enumerate(signals):
            sid = row["id"]
            title = row["title"]
            summary = row["summary"]
            zh_title = zh_titles[idx] if idx < len(zh_titles) else title
            zh_summary = zh_summaries[idx] if idx < len(zh_summaries) else summary
            freshness = row.get("freshness", "unknown")
            event_type = row.get("event_type", "new")
            hosts = row.get("hosts", [])

            zh_para = (
                f"{zh_summary}"
                f"该信号属于{self._event_type_zh(event_type)}，当前时效为{self._freshness_zh(freshness)}。"
                f"重点应关注其对算力供给、成本结构与竞争格局的后续影响。"
            )
            en_para = (
                f"{summary} This signal is classified as {event_type} with {freshness} freshness. "
                "The key analytical angle is how it may affect capacity supply, cost structure, and competitive dynamics."
            )

            host_text = "、".join(hosts[:2]) if hosts else "核心来源"
            zh_watch = [
                f"继续跟踪 {host_text} 在未来 24-72 小时内是否发布增量信息。",
                "评估该事件是否改变你当前的关注主题优先级与资源配置判断。",
            ]
            en_watch = [
                f"Track whether {', '.join(hosts[:2]) if hosts else 'core sources'} publish incremental updates in the next 24-72 hours.",
                "Assess whether this event changes your current topic priority or resource-allocation assumptions.",
            ]
            out[sid] = {
                "zh_title": zh_title,
                "en_title": title,
                "zh_paragraph": zh_para,
                "en_paragraph": en_para,
                "zh_watchpoints": zh_watch,
                "en_watchpoints": en_watch,
            }
        return out

    def _compose_cn_brief_blocks_fallback(
        self,
        signals: list[dict[str, Any]],
        *,
        domain: str = "",
    ) -> dict[str, dict[str, Any]]:
        summaries = [str(row.get("summary", "") or "").strip() for row in signals]
        zh_summaries = self.translate_to_zh(summaries, domain=domain) if summaries else []
        evidence_hints = [self._pick_fact_hint(row.get("evidence", [])) for row in signals]
        zh_evidence_hints = self.translate_to_zh(evidence_hints, domain=domain) if evidence_hints else []

        out: dict[str, dict[str, Any]] = {}
        for idx, row in enumerate(signals):
            sid = row["id"]
            title = row["title"]
            summary = summaries[idx] if idx < len(summaries) else str(row.get("summary", "") or "").strip()
            zh_summary = zh_summaries[idx] if idx < len(zh_summaries) else summary
            evidence = row.get("evidence", [])
            tags = row.get("tags", [])
            event_type = row.get("event_type", "new")
            hosts = row.get("source_hosts", [])
            source_context = str(row.get("source_context", "") or "").strip()

            what = f"这条更新聚焦“{title}”。"
            if zh_summary:
                what += f" 核心事实：{zh_summary}"
            if evidence:
                fact_hint = zh_evidence_hints[idx] if idx < len(zh_evidence_hints) else self._pick_fact_hint(evidence)
                if fact_hint:
                    what += f" 补充细节：{fact_hint}"
            elif source_context:
                extracted = self._pick_context_fact(source_context)
                if extracted:
                    what += f" 关键细节：{extracted}"
            elif tags:
                what += f" 主要涉及 {'、'.join(tags[:3])} 相关动态。"

            if tags:
                topic = "、".join(tags[:3])
                why = f"这件事会影响 {topic} 相关决策，尤其是近期优先级与资源分配判断。"
            else:
                why = "该事件与当前监控主题直接相关，可能改变短期判断和后续跟踪节奏。"
            if event_type == "update":
                why += " 由于它属于事件更新，重点看新增信息是否改变原有趋势。"

            host_hint = "、".join(hosts[:2]) if hosts else "核心来源"
            follow_up = [
                f"跟踪 {host_hint} 在未来 24-72 小时是否有增量披露。",
                "确认该事件是否改变当前关注事项的优先级。",
            ]

            out[sid] = {
                "zh_title": title,
                "zh_what_happened": what,
                "zh_why_it_matters": why,
                "zh_follow_up": follow_up,
                "en_note": title,
            }
        return out

    def _repair_single(self, source: str, candidate: str, *, domain: str) -> str:
        if self.client is None or self.llm_config is None:
            return source
        payload = {
            "domain": domain,
            "source": source,
            "candidate": candidate,
            "instruction": (
                "Fix candidate Chinese translation so key numbers/dates/entities from source are preserved. "
                "Return JSON with key 'translation'."
            ),
        }
        try:
            resp = self.client.chat.completions.create(
                model=self.llm_config.model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": "You are a careful translator. Return strict JSON only."},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                timeout=LLM_TIMEOUT_SECONDS,
            )
            parsed = json.loads(resp.choices[0].message.content or "{}")
            value = str(parsed.get("translation", "")).strip()
            return value or source
        except Exception:
            return source

    @staticmethod
    def _passes_basic_quality(source: str, zh: str) -> bool:
        digits = re.findall(r"\d+(?:\.\d+)?", source)
        for token in digits:
            if token not in zh:
                return False

        entities = re.findall(r"\b[A-Z][A-Za-z0-9\-.]{1,}\b", source)
        key_entities = [token for token in entities if token.upper() == token or token[:1].isupper()]
        for token in key_entities[:6]:
            if token.lower() in {"the", "and", "for", "with"}:
                continue
            if token not in zh:
                return False
        return True

    @staticmethod
    def _clean_list(value: Any, *, limit: int = 3) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            token = str(item or "").strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(token)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _is_composed_block_valid(block: dict[str, Any]) -> bool:
        zh_title = str(block.get("zh_title", "")).strip()
        en_title = str(block.get("en_title", "")).strip()
        zh_para = str(block.get("zh_paragraph", "")).strip()
        en_para = str(block.get("en_paragraph", "")).strip()
        zh_watch = block.get("zh_watchpoints", [])
        en_watch = block.get("en_watchpoints", [])
        return bool(
            zh_title
            and en_title
            and len(zh_para) >= 20
            and len(en_para) >= 20
            and isinstance(zh_watch, list)
            and isinstance(en_watch, list)
        )

    @staticmethod
    def _is_cn_brief_block_valid(block: dict[str, Any]) -> bool:
        title = str(block.get("zh_title", "")).strip()
        what = str(block.get("zh_what_happened", "")).strip()
        why = str(block.get("zh_why_it_matters", "")).strip()
        follow = block.get("zh_follow_up", [])
        banned = ("该更新围绕", "主要涉及", "相关动态")
        what_cn = BriefingLocalizer._cn_ratio(what)
        why_cn = BriefingLocalizer._cn_ratio(why)
        return bool(
            title
            and len(what) >= 60
            and len(why) >= 40
            and what_cn >= 0.35
            and why_cn >= 0.35
            and not any(token in what for token in banned)
            and isinstance(follow, list)
            and len([v for v in follow if str(v).strip()]) >= 1
        )

    @staticmethod
    def _cn_ratio(text: str) -> float:
        token = str(text or "").strip()
        if not token:
            return 0.0
        cn = len(re.findall(r"[\u4e00-\u9fff]", token))
        return cn / max(1, len(token))

    @staticmethod
    def _pick_fact_hint(evidence: list[str]) -> str:
        for token in evidence:
            text = str(token or "").strip()
            if not text:
                continue
            if re.search(r"[\u4e00-\u9fff]", text):
                return text[:90]
        for token in evidence:
            text = str(token or "").strip()
            if not text:
                continue
            if re.search(r"\d", text):
                return text[:90]
        return ""

    @staticmethod
    def _pick_context_fact(source_context: str) -> str:
        lines = [line.strip() for line in str(source_context or "").splitlines() if line.strip()]
        for line in lines:
            lower = line.lower()
            if lower.startswith(("title:", "summary:", "event_type:", "freshness:", "publish_time:")):
                continue
            if len(line) < 20:
                continue
            return line[:120]
        return ""

    @staticmethod
    def _freshness_zh(freshness: str) -> str:
        token = str(freshness or "").strip().lower()
        mapping = {
            "fresh": "新近",
            "recent": "近期",
            "unknown": "时间未知",
            "stale": "过旧",
        }
        return mapping.get(token, "未知")

    @staticmethod
    def _event_type_zh(event_type: str) -> str:
        token = str(event_type or "").strip().lower()
        mapping = {
            "new": "新事件",
            "update": "事件更新",
            "duplicate": "重复线索",
        }
        return mapping.get(token, "事件")
