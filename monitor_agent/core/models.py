from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class RssSourceConfig(BaseModel):
    name: str
    url: str
    max_items: int = 20
    incremental_overlap_count: int = Field(default=2, ge=0, le=10)
    refresh_interval_hours: int | None = Field(default=None, ge=1, le=168)
    timeout_seconds: int = 15
    fetch_full_text: bool = True
    article_timeout_seconds: int = 12
    article_max_chars: int = 12000


class PlaywrightSourceConfig(BaseModel):
    name: str
    url: str
    wait_for_selector: str | None = None
    content_selector: str = "body"
    max_chars: int = 6000
    timeout_ms: int = 30000
    follow_links_enabled: bool = False
    incremental_overlap_count: int = Field(default=2, ge=0, le=10)
    refresh_interval_hours: int | None = Field(default=None, ge=1, le=168)
    max_depth: int = Field(default=1, ge=1, le=2)
    max_links_per_source: int = Field(default=3, ge=1, le=20)
    same_domain_only: bool = True
    link_selector: str = "a[href]"
    article_url_patterns: list[str] = Field(default_factory=list)
    exclude_url_patterns: list[str] = Field(default_factory=list)
    article_wait_for_selector: str | None = None
    article_content_selector: str = "body"
    force_playwright: bool = False


class SourcesConfig(BaseModel):
    rss: list[RssSourceConfig] = Field(default_factory=list)
    playwright: list[PlaywrightSourceConfig] = Field(default_factory=list)


class ScheduleConfig(BaseModel):
    timezone: str = "UTC"
    times: list[str] = Field(default_factory=lambda: ["07:00"])
    enabled: bool = True


class FilteringConfig(BaseModel):
    importance_threshold: float = 0.6
    dedup_window_days: int = 14
    novelty_window_days: int = 30
    max_signals: int = 10
    max_system_signals: int = 5
    event_similarity_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    event_candidate_top_k: int = Field(default=5, ge=1, le=10)
    event_candidate_lookback_days: int = Field(default=3, ge=1, le=30)
    inbox_match_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    unknown_freshness_importance_threshold: float = Field(default=0.9, ge=0.0, le=1.0)


class StrategyProfileConfig(BaseModel):
    focus_areas: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class SourceLinkConfig(BaseModel):
    url: str
    type: Literal["auto", "rss", "playwright"] = "auto"
    name: str | None = None
    force_playwright: bool | None = None
    follow_links_enabled: bool = False
    incremental_overlap_count: int = Field(default=2, ge=0, le=10)
    refresh_interval_hours: int | None = Field(default=None, ge=1, le=168)
    max_depth: int = Field(default=1, ge=1, le=2)
    max_links_per_source: int = Field(default=3, ge=1, le=20)
    same_domain_only: bool = True
    link_selector: str = "a[href]"
    article_url_patterns: list[str] = Field(default_factory=list)
    exclude_url_patterns: list[str] = Field(default_factory=list)
    article_wait_for_selector: str | None = None
    article_content_selector: str = "body"


class DomainProfileConfig(BaseModel):
    domain: str
    focus_areas: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    source_links: list[str | SourceLinkConfig] = Field(default_factory=list)


class LLMConfig(BaseModel):
    provider: Literal["openai"] = "openai"
    model: str = "gpt-5-mini"
    dedup_model: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str | None = None
    base_url: str | None = None
    temperature: float = 0.1
    dedup_temperature: float = 0.0
    max_input_items: int = 40


class PlaywrightRuntimeConfig(BaseModel):
    headless: bool = True
    channel: str | None = None
    extension_paths: list[str] = Field(default_factory=list)
    launch_args: list[str] = Field(default_factory=list)


class TTSConfig(BaseModel):
    enabled: bool = False
    provider: Literal["openai", "gtts"] = "openai"
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    language: str = "zh-CN"
    base_url: str | None = None


class BriefingConfig(BaseModel):
    language: Literal["zh", "en"] = "zh"


class TelegramConfig(BaseModel):
    enabled: bool = False


class DingTalkConfig(BaseModel):
    enabled: bool = False
    ingest_enabled: bool = False


class NotificationsConfig(BaseModel):
    channel: Literal["none", "telegram", "dingtalk"] | None = "none"
    channels: list[Literal["telegram", "dingtalk"]] = Field(default_factory=list)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_email(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if payload.get("channel") == "email":
            payload["channel"] = "none"
        if isinstance(payload.get("channels"), list):
            payload["channels"] = [item for item in payload["channels"] if item != "email"]
        payload.pop("email", None)
        return payload

    @model_validator(mode="after")
    def _sync_channels(self) -> "NotificationsConfig":
        normalized: list[Literal["telegram", "dingtalk"]] = []
        seen: set[str] = set()
        for item in self.channels:
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        if self.channel and self.channel != "none" and self.channel not in seen:
            normalized.append(self.channel)
        self.channels = normalized
        self.channel = normalized[0] if normalized else "none"
        self.telegram.enabled = "telegram" in normalized
        self.dingtalk.enabled = "dingtalk" in normalized
        return self


class StorageConfig(BaseModel):
    root_dir: str = "./data"
    base_path: str | None = None

    @property
    def persistent_base_path(self) -> str:
        return self.base_path or self.root_dir


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    scheduler_enabled: bool = False
    auto_run_on_user_ingest: bool = True
    telegram_ingest_enabled: bool = False
    telegram_ingest_poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=60.0)


class MonitorConfig(BaseModel):
    domain: str
    domains: list[str] = Field(default_factory=list)
    domain_profiles: list[DomainProfileConfig] = Field(default_factory=list)
    sources: SourcesConfig
    strategy_profile: StrategyProfileConfig = Field(default_factory=StrategyProfileConfig)
    internal_strategy: dict[str, Any] = Field(default_factory=dict)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    filtering: FilteringConfig = Field(default_factory=FilteringConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    playwright: PlaywrightRuntimeConfig = Field(default_factory=PlaywrightRuntimeConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)

    @property
    def domain_scope(self) -> list[str]:
        values: list[str] = []
        seed = [profile.domain for profile in self.domain_profiles] + [self.domain] + self.domains
        seen: set[str] = set()
        for item in seed:
            token = item.strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(token)
        return values

    @property
    def effective_strategy_profile(self) -> StrategyProfileConfig:
        if not self.domain_profiles:
            return self.strategy_profile

        focus = self.strategy_profile.focus_areas[:]
        entities = self.strategy_profile.entities[:]
        keywords = self.strategy_profile.keywords[:]
        for profile in self.domain_profiles:
            focus.extend(profile.focus_areas)
            entities.extend(profile.entities)
            keywords.extend(profile.keywords)
        return StrategyProfileConfig(
            focus_areas=_dedupe_tokens(focus),
            entities=_dedupe_tokens(entities),
            keywords=_dedupe_tokens(keywords),
        )


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


class RawItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source_type: Literal["rss", "playwright", "html"]
    source_name: str
    title: str
    url: str | None = None
    content: str
    published_at: datetime | None = None
    fetched_at: datetime
    publish_time: datetime | None = None
    age_hours: float | None = Field(default=None, ge=0.0)
    freshness: Literal["fresh", "recent", "stale", "unknown"] = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)


class SignalPriority(BaseModel):
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source_weight: float = Field(default=1.0, ge=0.0)
    user_interest: float = Field(default=0.5, ge=0.0, le=1.0)
    novelty: float = Field(default=0.5, ge=0.0, le=1.0)
    final_score: float = Field(default=0.0, ge=0.0)


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    summary: str
    importance: float = Field(ge=0.0, le=1.0)
    category: str
    source_urls: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    publish_time: datetime | None = None
    age_hours: float | None = Field(default=None, ge=0.0)
    freshness: Literal["fresh", "recent", "stale", "unknown"] = "unknown"
    extracted_at: datetime
    fingerprint: str
    novelty_score: float = Field(default=0.0, ge=0.0, le=1.0)
    event_id: str | None = None
    event_type: Literal["new", "update", "duplicate"] = "new"
    embedding: list[float] = Field(default_factory=list)
    source: Literal["system", "user"] = "system"
    tracking_id: str | None = None
    user_context: str | None = None
    latest_updates: list[str] = Field(default_factory=list)
    system_interpretation: str | None = None
    briefed_once_at: datetime | None = None
    priority: SignalPriority = Field(default_factory=SignalPriority)


class RunArtifacts(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    domain: str
    raw_items_count: int = 0
    signal_count: int = 0
    signals_path: str | None = None
    brief_text_path: str | None = None
    brief_audio_path: str | None = None
    raw_items_path: str | None = None
    persistent_signals_path: str | None = None
    persistent_brief_md_path: str | None = None
    persistent_brief_json_path: str | None = None
    persistent_audio_path: str | None = None
    debug_bundle_path: str | None = None
    daily_summary_path: str | None = None
    status: Literal["running", "completed", "failed"] = "running"
    run_metrics: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class SourceCursorState(BaseModel):
    source_key: str
    source_type: Literal["rss", "html", "playwright"]
    source_url: str
    last_seen_published_at: datetime | None = None
    last_seen_ids: list[str] = Field(default_factory=list)
    last_seen_urls: list[str] = Field(default_factory=list)
    last_success_at: datetime | None = None
    overlap_count: int = Field(default=2, ge=0, le=10)
    incremental_mode: Literal["time", "id", "url", "mixed"] = "mixed"


class SourceAdvisory(BaseModel):
    source_key: str
    source_name: str
    source_type: Literal["rss", "html", "playwright"]
    source_url: str
    severity: Literal["warning", "error"]
    issue_code: str
    summary: str
    runtime_status: Literal["success", "skipped", "error"] | None = None
    refresh_interval_hours: int | None = Field(default=None, ge=1, le=168)
    suggested_source_link: dict[str, Any] | None = None
    issues: list[str] = Field(default_factory=list)
    fixes: list[str] = Field(default_factory=list)
