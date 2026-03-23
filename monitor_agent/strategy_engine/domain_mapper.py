from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from openai import OpenAI

from monitor_agent.strategy_engine.models import DomainMapping, FeedSeed, ParsedIntent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Preset:
    name: str
    keywords: tuple[str, ...]
    taxonomy: tuple[str, ...]
    seed_queries: tuple[str, ...]
    tags: tuple[str, ...]
    feeds: tuple[tuple[str, str], ...]


_PRESETS: tuple[_Preset, ...] = (
    _Preset(
        name="Technology",
        keywords=("software", "ai", "cloud", "developer", "platform", "chip", "startup"),
        taxonomy=("product updates", "partnerships", "incidents", "funding", "competition"),
        seed_queries=("technology product launch", "developer platform updates"),
        tags=("technology", "product", "market"),
        feeds=(("Hacker News", "https://hnrss.org/frontpage"),),
    ),
    _Preset(
        name="Cybersecurity",
        keywords=("security", "threat", "vulnerability", "cve", "breach", "ransomware"),
        taxonomy=("new vulnerabilities", "active exploits", "vendor advisories", "incident response"),
        seed_queries=("CVE vulnerability disclosure", "security incident advisory"),
        tags=("security", "risk", "incident"),
        feeds=(("US-CERT Alerts", "https://www.cisa.gov/cybersecurity-advisories/all.xml"),),
    ),
    _Preset(
        name="Finance",
        keywords=("equity", "earnings", "macro", "rates", "credit", "fund", "investment"),
        taxonomy=("earnings", "guidance", "macro indicators", "policy rate changes"),
        seed_queries=("earnings guidance revision", "central bank policy decision"),
        tags=("finance", "macro", "earnings"),
        feeds=(("SEC Press Releases", "https://www.sec.gov/news/pressreleases.rss"),),
    ),
    _Preset(
        name="Healthcare",
        keywords=("hospital", "clinical", "trial", "drug", "fda", "medtech", "patient"),
        taxonomy=("clinical trial outcomes", "regulatory approvals", "safety updates", "reimbursement"),
        seed_queries=("clinical trial phase results", "healthcare regulatory approval"),
        tags=("healthcare", "clinical", "regulatory"),
        feeds=(("FDA News", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"),),
    ),
    _Preset(
        name="Supply Chain",
        keywords=("logistics", "shipping", "supplier", "procurement", "inventory", "port", "manufacturing"),
        taxonomy=("supplier disruptions", "transport delays", "capacity shifts", "cost pressure"),
        seed_queries=("global shipping disruption", "supplier production outage"),
        tags=("supply-chain", "operations", "logistics"),
        feeds=(("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml"),),
    ),
    _Preset(
        name="Policy",
        keywords=("policy", "regulation", "law", "compliance", "agency", "government"),
        taxonomy=("new regulations", "enforcement actions", "consultations", "implementation timelines"),
        seed_queries=("regulatory rulemaking update", "compliance enforcement action"),
        tags=("policy", "regulation", "compliance"),
        feeds=(("Federal Register", "https://www.federalregister.gov/documents/search.rss"),),
    ),
)


class DomainMapper:
    """Rule-first domain mapping, optionally refined by LLM."""

    def __init__(self, model: str = "gpt-5-mini", base_url: str | None = None) -> None:
        self.model = model
        api_key = os.getenv("OPENAI_API_KEY")
        if base_url and not api_key:
            api_key = "dummy"
        self.client = OpenAI(api_key=api_key, base_url=base_url) if api_key else None

    def map_intent(self, intent: ParsedIntent, user_request: str) -> tuple[DomainMapping, list[str]]:
        errors: list[str] = []

        rule_based = self._rule_map(intent, user_request)
        if self.client is None:
            return rule_based, errors

        try:
            refined = self._llm_refine(intent, rule_based)
            return refined, errors
        except Exception as exc:
            msg = f"Domain mapper LLM refinement failed; rule mapping retained: {exc}"
            logger.exception(msg)
            errors.append(msg)
            return rule_based, errors

    def _rule_map(self, intent: ParsedIntent, user_request: str) -> DomainMapping:
        text = f"{intent.domain} {' '.join(intent.focus_areas)} {' '.join(intent.entities)} {user_request}".lower()

        best: _Preset | None = None
        best_score = -1
        for preset in _PRESETS:
            score = sum(1 for keyword in preset.keywords if keyword in text)
            if preset.name.lower() == intent.domain.lower():
                score += 2
            if score > best_score:
                best = preset
                best_score = score

        if best is None or best_score <= 0:
            best = _Preset(
                name=intent.domain or "General",
                keywords=(),
                taxonomy=("market updates", "operational risks", "strategic moves"),
                seed_queries=(f"{intent.domain} major updates",),
                tags=("monitoring",),
                feeds=(),
            )

        queries = list(best.seed_queries)
        queries.extend(intent.focus_areas[:4])
        queries.extend(intent.entities[:3])

        feeds = [FeedSeed(name=name, url=url) for name, url in best.feeds]
        return DomainMapping(
            canonical_domain=best.name,
            domain_taxonomy=list(best.taxonomy),
            source_queries=_dedupe([q for q in queries if q]),
            baseline_rss_feeds=feeds,
            recommended_tags=_dedupe(list(best.tags) + intent.focus_areas[:3]),
            reasoning=(
                f"Selected '{best.name}' by keyword overlap ({best_score}) against request, "
                "then expanded with focus areas and entities."
            ),
            confidence=0.65 if best_score > 0 else 0.45,
        )

    def _llm_refine(self, intent: ParsedIntent, rule_mapping: DomainMapping) -> DomainMapping:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Refine monitoring domain mapping. Return JSON with keys: canonical_domain, "
                        "domain_taxonomy(array), source_queries(array), recommended_tags(array), "
                        "reasoning, confidence(0..1). Keep recommendations cross-domain and practical."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "intent": intent.model_dump(mode="json"),
                            "rule_mapping": rule_mapping.model_dump(mode="json"),
                        }
                    ),
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        payload = json.loads(content)

        return DomainMapping(
            canonical_domain=str(payload.get("canonical_domain") or rule_mapping.canonical_domain),
            domain_taxonomy=_dedupe(
                [str(v) for v in payload.get("domain_taxonomy", []) if v]
            )
            or rule_mapping.domain_taxonomy,
            source_queries=_dedupe(
                [str(v) for v in payload.get("source_queries", []) if v]
                + rule_mapping.source_queries
            ),
            baseline_rss_feeds=rule_mapping.baseline_rss_feeds,
            recommended_tags=_dedupe(
                [str(v) for v in payload.get("recommended_tags", []) if v]
                + rule_mapping.recommended_tags
            ),
            reasoning=str(payload.get("reasoning") or rule_mapping.reasoning),
            confidence=_bounded_float(payload.get("confidence"), default=rule_mapping.confidence),
        )


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for item in items:
        norm = item.strip()
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def _bounded_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))
