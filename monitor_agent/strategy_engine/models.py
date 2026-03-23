from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class UIStrategyInput(BaseModel):
    domain: str = Field(min_length=2)
    focus_areas: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    source_links: list[str] = Field(default_factory=list)


class InternalStrategyConfig(BaseModel):
    signal_categories: list[str] = Field(default_factory=list)
    source_weights: dict[str, float] = Field(default_factory=dict)
    topic_weights: dict[str, float] = Field(default_factory=dict)
    filter_rules: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    delivery_options: dict[str, Any] = Field(default_factory=dict)
    version_metadata: dict[str, Any] = Field(default_factory=dict)
    advanced_settings: dict[str, Any] = Field(default_factory=dict)


class StrategyGenerateRequest(BaseModel):
    user_request: str | None = Field(default=None, min_length=8)
    domain: str | None = Field(default=None, min_length=2)
    focus_areas: list[str] | None = None
    entities: list[str] | None = None
    keywords: list[str] | None = None
    source_links: list[str] | None = None
    advanced_settings: dict[str, Any] | None = None
    timezone: str | None = None
    schedule_times: list[str] | None = None
    importance_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_signals: int | None = Field(default=None, ge=1, le=100)

    @property
    def has_ui_payload(self) -> bool:
        return bool((self.domain or "").strip())

    @model_validator(mode="after")
    def _validate_mode(self) -> "StrategyGenerateRequest":
        if not self.user_request and not self.has_ui_payload:
            raise ValueError("Either user_request or domain must be provided.")
        return self


class StrategyDeployRequest(BaseModel):
    user_request: str | None = Field(default=None, min_length=8)
    domain: str | None = Field(default=None, min_length=2)
    focus_areas: list[str] | None = None
    entities: list[str] | None = None
    keywords: list[str] | None = None
    source_links: list[str] | None = None
    advanced_settings: dict[str, Any] | None = None
    modification_request: str | None = Field(default=None, min_length=3)
    timezone: str | None = None
    schedule_times: list[str] | None = None
    importance_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_signals: int | None = Field(default=None, ge=1, le=100)
    deploy_current: bool = False
    version: int | None = Field(default=None, ge=1)
    confirm: bool = False
    target_config_path: str | None = None
    overwrite: bool = True

    @property
    def has_ui_payload(self) -> bool:
        return bool((self.domain or "").strip())


class StrategyPatchInstruction(BaseModel):
    operation: Literal["add", "remove", "update"]
    target: Literal["focus_areas", "entities", "keywords"]
    value: str = Field(min_length=1)


class StrategyPatchRequest(BaseModel):
    modification_request: str = Field(min_length=3)


class StrategyGetRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)


class StrategyHistoryRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=200)


class StrategyPreviewRequest(StrategyGenerateRequest):
    pass


class SourceStrategySuggestRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    refresh_interval_days: int = Field(default=14, ge=7, le=90)
    force_refresh: bool = False


class SourceStrategySuggestion(BaseModel):
    url: str
    parser_recommendation: Literal["rss", "html_parser", "playwright"]
    configured_type: Literal["rss", "playwright"]
    normalized_source_link: dict[str, Any] = Field(default_factory=dict)
    reason: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    analysis: dict[str, Any] = Field(default_factory=dict)
    probe_status: Literal["ok", "warning", "error"] = "ok"
    issues: list[str] = Field(default_factory=list)
    fixes: list[str] = Field(default_factory=list)
    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    next_refresh_at: datetime
    cache_hit: bool = False


class SourceStrategySuggestResult(BaseModel):
    refresh_interval_days: int
    total_urls: int
    reused_cached: int
    recomputed: int
    suggestions: list[SourceStrategySuggestion] = Field(default_factory=list)


class ParsedIntent(BaseModel):
    domain: str
    focus_areas: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    intent_summary: str
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)


class FeedSeed(BaseModel):
    name: str
    url: str
    max_items: int = 20


class DomainMapping(BaseModel):
    canonical_domain: str
    domain_taxonomy: list[str] = Field(default_factory=list)
    source_queries: list[str] = Field(default_factory=list)
    baseline_rss_feeds: list[FeedSeed] = Field(default_factory=list)
    recommended_tags: list[str] = Field(default_factory=list)
    reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)


class StrategyGenerationResult(BaseModel):
    parsed_intent: ParsedIntent
    domain_mapping: DomainMapping
    strategy_text: str
    config_yaml: str
    config_object: dict[str, Any]
    ui_input: UIStrategyInput | None = None
    internal_strategy: InternalStrategyConfig | None = None
    explainability: dict[str, Any] = Field(default_factory=dict)


class StrategyVersionEntry(BaseModel):
    version: int
    previous_version: int | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    changes: list[str] = Field(default_factory=list)
    patch: StrategyPatchInstruction | None = None


class StrategyState(BaseModel):
    version: int = 1
    deployed_version: int | None = None
    pending_deploy: bool = True
    generation: StrategyGenerationResult
    change_log: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StrategyPatchResult(BaseModel):
    patch: StrategyPatchInstruction
    version: int
    previous_version: int
    pending_deploy: bool
    changes: list[str] = Field(default_factory=list)
    generation: StrategyGenerationResult


class StrategyGetResult(BaseModel):
    strategy: StrategyState | None = None
    message: str | None = None


class StrategyHistoryResult(BaseModel):
    entries: list[StrategyVersionEntry] = Field(default_factory=list)


class StrategyDeployResult(BaseModel):
    deployed: bool
    deployed_path: str
    monitor_config_valid: bool
    generation: StrategyGenerationResult
    message: str


class StrategyPreviewResult(BaseModel):
    summary: str
    normalized_config: dict[str, Any]
    strategy_text: str
