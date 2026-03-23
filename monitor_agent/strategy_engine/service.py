from __future__ import annotations

import logging
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import yaml

from monitor_agent.core.models import MonitorConfig
from monitor_agent.core.storage import Storage
from monitor_agent.core.utils import utc_now
from monitor_agent.strategy_engine.config_generator import ConfigGenerator
from monitor_agent.strategy_engine.domain_mapper import DomainMapper
from monitor_agent.strategy_engine.intent_parser import IntentParser
from monitor_agent.strategy_engine.models import (
    DomainMapping,
    ParsedIntent,
    SourceStrategySuggestRequest,
    SourceStrategySuggestResult,
    StrategyDeployRequest,
    StrategyDeployResult,
    StrategyGenerateRequest,
    StrategyGenerationResult,
    StrategyGetRequest,
    StrategyGetResult,
    StrategyHistoryRequest,
    StrategyHistoryResult,
    StrategyPatchInstruction,
    StrategyPatchRequest,
    StrategyPatchResult,
    StrategyPreviewRequest,
    StrategyPreviewResult,
    StrategyState,
    StrategyVersionEntry,
    UIStrategyInput,
)
from monitor_agent.strategy_engine.normalizer import (
    build_ui_input_from_fields,
    normalize_ui_input,
    synthesize_user_request,
)
from monitor_agent.strategy_engine.source_strategy_engine import SourceStrategyEngine
from monitor_agent.strategy_engine.strategy_builder import StrategyBuilder
from monitor_agent.strategy_engine.strategy_patch_engine import StrategyPatchEngine

logger = logging.getLogger(__name__)


class StrategyEngine:
    """Strategy generation, incremental patching, versioning, and deployment service."""

    def __init__(self, base_config: MonitorConfig | None = None, storage: Storage | None = None) -> None:
        self.base_config = base_config
        self.storage = storage
        llm_model = base_config.llm.model if base_config else "gpt-5-mini"
        llm_base_url = base_config.llm.base_url if base_config else None

        self.intent_parser = IntentParser(model=llm_model, base_url=llm_base_url)
        self.domain_mapper = DomainMapper(model=llm_model, base_url=llm_base_url)
        self.strategy_builder = StrategyBuilder(model=llm_model, base_url=llm_base_url)
        self.config_generator = ConfigGenerator(base_config=base_config)
        self.patch_engine = StrategyPatchEngine(model=llm_model, base_url=llm_base_url)

    def generate(self, request: StrategyGenerateRequest) -> StrategyGenerationResult:
        result = self._build_generation(request)
        self._persist_generated_draft(result)
        return result

    def preview(self, request: StrategyPreviewRequest) -> StrategyPreviewResult:
        generation = self._build_generation(request)
        return StrategyPreviewResult(
            summary=generation.strategy_text,
            normalized_config=generation.config_object,
            strategy_text=generation.strategy_text,
        )

    def suggest_source_strategies(self, request: SourceStrategySuggestRequest) -> SourceStrategySuggestResult:
        if self.storage is None:
            raise RuntimeError("Source strategy suggestion requires storage for cache management.")

        urls = request.urls[:] if request.urls else self._collect_known_source_links()
        if not urls:
            raise ValueError("No URLs provided and no source links found in current strategy/config.")

        llm_cfg = self.base_config.llm if self.base_config else None
        engine = SourceStrategyEngine(
            storage=self.storage,
            llm_config=llm_cfg,
            use_llm=True,
        )
        return engine.suggest(
            urls=urls,
            refresh_interval_days=request.refresh_interval_days,
            force_refresh=request.force_refresh,
        )

    def _build_generation(
        self,
        request: StrategyGenerateRequest | StrategyPreviewRequest,
    ) -> StrategyGenerationResult:
        parser_errors: list[str] = []
        ui_input: UIStrategyInput | None = None
        resolved_source_links: list[str | dict[str, object]] | None = None
        source_diagnostics: list[dict[str, object]] = []

        if request.has_ui_payload:
            ui_input = build_ui_input_from_fields(
                domain=request.domain or "",
                focus_areas=request.focus_areas,
                entities=request.entities,
                keywords=request.keywords,
                source_links=request.source_links,
            )
            ui_input, resolved_source_links, source_diagnostics = self._resolve_ui_source_links_for_generate(
                ui_input=ui_input,
                advanced_settings=request.advanced_settings,
            )
            effective_user_request = request.user_request or synthesize_user_request(ui_input)
            parser_result = ParsedIntent(
                domain=ui_input.domain,
                focus_areas=ui_input.focus_areas,
                entities=ui_input.entities,
                source_urls=ui_input.source_links,
                intent_summary=f"Monitor {ui_input.domain} with configured strategy fields from UI.",
                rationale="UI simple strategy payload provided explicit domain/focus/entities/keywords/source links.",
                confidence=0.92,
            )
        else:
            effective_user_request = request.user_request or ""
            parser_result, parser_errors = self.intent_parser.parse(effective_user_request)

        mapping, mapper_errors = self.domain_mapper.map_intent(parser_result, effective_user_request)
        config_object, _ = self.config_generator.generate(request, parser_result, mapping)

        if ui_input is not None:
            config_object = self._apply_ui_overrides(
                config_object,
                ui_input,
                mapping,
                source_links_override=resolved_source_links,
            )
        else:
            ui_input = self._derive_ui_input(parser_result, mapping, config_object)

        internal_strategy = normalize_ui_input(
            ui_input,
            base_config=self.base_config,
            schedule_timezone=request.timezone,
            schedule_times=request.schedule_times,
            importance_threshold=request.importance_threshold,
            max_signals=request.max_signals,
            advanced_settings=request.advanced_settings,
        )
        config_object["internal_strategy"] = internal_strategy.model_dump(mode="json")
        validated = MonitorConfig.model_validate(config_object)
        config_object = validated.model_dump(mode="json")
        config_yaml = yaml.safe_dump(config_object, sort_keys=False, allow_unicode=False)

        strategy_text, builder_errors = self.strategy_builder.build(
            user_request=effective_user_request,
            parsed_intent=parser_result,
            domain_mapping=mapping,
            config_object=config_object,
        )
        if not strategy_text.strip():
            strategy_text = _deterministic_strategy_text(
                parsed_intent=parser_result,
                domain_mapping=mapping,
                config_object=config_object,
            )

        explainability = {
            "input_mode": "ui_simple" if request.has_ui_payload else "natural_language",
            "intent_summary": parser_result.intent_summary,
            "parser_confidence": parser_result.confidence,
            "domain_mapping_confidence": mapping.confidence,
            "mapping_reasoning": mapping.reasoning,
            "derived_focus_areas": parser_result.focus_areas,
            "derived_entities": parser_result.entities,
            "errors": parser_errors + mapper_errors + builder_errors,
        }
        if source_diagnostics:
            explainability["source_link_diagnostics"] = source_diagnostics

        return StrategyGenerationResult(
            parsed_intent=parser_result,
            domain_mapping=mapping,
            strategy_text=strategy_text,
            config_yaml=config_yaml,
            config_object=config_object,
            ui_input=ui_input,
            internal_strategy=internal_strategy,
            explainability=explainability,
        )

    @staticmethod
    def _apply_ui_overrides(
        config_object: dict,
        ui_input: UIStrategyInput,
        mapping: DomainMapping,
        source_links_override: list[str | dict[str, object]] | None = None,
    ) -> dict:
        out = deepcopy(config_object)
        keywords = ui_input.keywords if ui_input.keywords else mapping.recommended_tags

        source_links = ui_input.source_links[:]
        if not source_links:
            source_links = _dedupe(
                [
                    *(str(row.get("url", "")).strip() for row in out.get("sources", {}).get("rss", []) if isinstance(row, dict)),
                    *(str(row.get("url", "")).strip() for row in out.get("sources", {}).get("playwright", []) if isinstance(row, dict)),
                ]
            )
        if source_links_override is not None:
            source_links = source_links_override[:]

        out["domain"] = ui_input.domain
        out["domains"] = [ui_input.domain]
        out["domain_profiles"] = [
            {
                "domain": ui_input.domain,
                "focus_areas": _dedupe(ui_input.focus_areas),
                "entities": _dedupe(ui_input.entities),
                "keywords": _dedupe(keywords),
                "source_links": source_links,
            }
        ]
        out["strategy_profile"] = {
            "focus_areas": _dedupe(ui_input.focus_areas),
            "entities": _dedupe(ui_input.entities),
            "keywords": _dedupe(keywords),
        }
        if ui_input.source_links:
            # Keep UI schema simple; runtime source expansion will infer RSS/Playwright.
            out["sources"] = {"rss": [], "playwright": []}
        return out

    def _resolve_ui_source_links_for_generate(
        self,
        *,
        ui_input: UIStrategyInput,
        advanced_settings: dict | None,
    ) -> tuple[UIStrategyInput, list[str | dict[str, object]] | None, list[dict[str, object]]]:
        if self.storage is None:
            return ui_input, None, []

        opts = advanced_settings if isinstance(advanced_settings, dict) else {}
        enabled = bool(opts.get("auto_source_diagnosis", False))
        if not enabled or not ui_input.source_links:
            return ui_input, None, []

        existing_urls = {
            _url_key(_parse_source_link_token(raw).get("url", ""))
            for raw in self._collect_known_source_links()
            if _parse_source_link_token(raw).get("url")
        }
        parsed_entries = [_parse_source_link_token(token) for token in ui_input.source_links]
        new_urls = _dedupe(
            [
                str(row.get("url", "")).strip()
                for row in parsed_entries
                if str(row.get("url", "")).strip()
                and _url_key(str(row.get("url", "")).strip()) not in existing_urls
            ]
        )
        if not new_urls:
            return ui_input, None, []

        llm_cfg = self.base_config.llm if self.base_config is not None else None
        engine = SourceStrategyEngine(
            storage=self.storage,
            llm_config=llm_cfg,
            use_llm=True,
        )
        result = engine.suggest(
            urls=new_urls,
            refresh_interval_days=14,
            force_refresh=True,
        )
        suggestion_by_url = {
            _url_key(row.url): row
            for row in result.suggestions
            if row.url and row.normalized_source_link
        }

        resolved: list[str | dict[str, object]] = []
        diagnostics: list[dict[str, object]] = []
        for entry in parsed_entries:
            original_url = str(entry.get("url", "")).strip()
            if not original_url:
                continue
            forced_type = str(entry.get("type", "auto")).strip().lower()
            suggestion = suggestion_by_url.get(_url_key(original_url))
            if suggestion is None:
                resolved.append(_entry_to_source_link(entry))
                continue

            normalized = _sanitize_source_link_object(suggestion.normalized_source_link)
            if forced_type in {"rss", "playwright"}:
                normalized["type"] = forced_type
                if forced_type == "playwright":
                    normalized["force_playwright"] = True
                if forced_type == "rss":
                    normalized.pop("force_playwright", None)

            resolved.append(normalized)
            diagnostics.append(
                {
                    "input_url": original_url,
                    "applied_url": str(normalized.get("url", original_url)),
                    "probe_status": suggestion.probe_status,
                    "recommendation": suggestion.parser_recommendation,
                    "configured_type": suggestion.configured_type,
                    "issues": suggestion.issues,
                    "fixes": suggestion.fixes,
                    "cache_hit": suggestion.cache_hit,
                }
            )

        updated = UIStrategyInput.model_validate(ui_input.model_dump(mode="json"))
        updated.source_links = _dedupe([_source_link_url(item) for item in resolved if _source_link_url(item)])
        return updated, _dedupe_source_link_entries(resolved), diagnostics

    @staticmethod
    def _derive_ui_input(
        parsed_intent: ParsedIntent,
        mapping: DomainMapping,
        config_object: dict,
    ) -> UIStrategyInput:
        links: list[str] = []
        profiles = config_object.get("domain_profiles", [])
        if isinstance(profiles, list):
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                raw_links = profile.get("source_links", [])
                if not isinstance(raw_links, list):
                    continue
                for value in raw_links:
                    if isinstance(value, str) and value.strip():
                        links.append(value.strip())

        if not links:
            sources = config_object.get("sources", {})
            if isinstance(sources, dict):
                for row in sources.get("rss", []):
                    if isinstance(row, dict):
                        token = str(row.get("url", "")).strip()
                        if token:
                            links.append(token)
                for row in sources.get("playwright", []):
                    if isinstance(row, dict):
                        token = str(row.get("url", "")).strip()
                        if token:
                            links.append(token)

        return build_ui_input_from_fields(
            domain=parsed_intent.domain or mapping.canonical_domain,
            focus_areas=parsed_intent.focus_areas,
            entities=parsed_intent.entities,
            keywords=mapping.recommended_tags,
            source_links=links,
        )

    def patch(self, request: StrategyPatchRequest) -> StrategyPatchResult:
        self._ensure_storage()
        current = self._load_state()
        if current is None:
            raise ValueError("No strategy found. Generate and deploy a strategy before applying patches.")

        patch, parser_errors = self.patch_engine.parse(request.modification_request)
        patched_generation, changes = self._apply_patch(current.generation, patch)
        if parser_errors:
            patched_generation.explainability.setdefault("errors", [])
            patched_generation.explainability["errors"].extend(parser_errors)

        if not changes:
            return StrategyPatchResult(
                patch=patch,
                version=current.version,
                previous_version=current.version,
                pending_deploy=current.pending_deploy,
                changes=["No effective change; strategy already in requested state."],
                generation=current.generation,
            )

        next_version = current.version + 1
        next_state = StrategyState(
            version=next_version,
            deployed_version=current.deployed_version,
            pending_deploy=True,
            generation=patched_generation,
            change_log=current.change_log + changes,
            updated_at=utc_now(),
        )
        self._save_state(next_state)
        self._append_history(
            StrategyVersionEntry(
                version=next_version,
                previous_version=current.version,
                timestamp=utc_now(),
                changes=changes,
                patch=patch,
            ),
            next_state,
        )

        return StrategyPatchResult(
            patch=patch,
            version=next_state.version,
            previous_version=current.version,
            pending_deploy=True,
            changes=changes,
            generation=patched_generation,
        )

    def get(self, request: StrategyGetRequest) -> StrategyGetResult:
        self._ensure_storage()
        current = self._load_state()
        if current is None:
            return StrategyGetResult(message="No strategy has been stored yet.")

        if request.version is None or request.version == current.version:
            return StrategyGetResult(strategy=current)

        for row in reversed(self.storage.load_strategy_history()):
            if row.get("version") != request.version:
                continue
            snapshot = row.get("state_snapshot")
            if isinstance(snapshot, dict):
                try:
                    return StrategyGetResult(strategy=StrategyState.model_validate(snapshot))
                except Exception:
                    break
        return StrategyGetResult(message=f"Strategy version {request.version} is not available.")

    def history(self, request: StrategyHistoryRequest) -> StrategyHistoryResult:
        self._ensure_storage()
        rows = self.storage.load_strategy_history()
        entries: list[StrategyVersionEntry] = []
        for row in reversed(rows):
            try:
                entries.append(StrategyVersionEntry.model_validate(row))
            except Exception:
                continue
            if len(entries) >= request.limit:
                break
        return StrategyHistoryResult(entries=entries)

    def deploy(self, request: StrategyDeployRequest) -> StrategyDeployResult:
        if not request.confirm:
            raise ValueError("Deployment requires explicit confirmation. Set confirm=true.")

        generation: StrategyGenerationResult
        message: str

        if request.modification_request:
            self._ensure_storage()
            state = self._load_state()
            if state is None:
                raise ValueError("No strategy available to patch/deploy. Generate one first.")

            patch, parser_errors = self.patch_engine.parse(request.modification_request)
            patched_generation, changes = self._apply_patch(state.generation, patch)
            if parser_errors:
                patched_generation.explainability.setdefault("errors", [])
                patched_generation.explainability["errors"].extend(parser_errors)

            if changes:
                next_version = state.version + 1
                state = StrategyState(
                    version=next_version,
                    deployed_version=next_version,
                    pending_deploy=False,
                    generation=patched_generation,
                    change_log=state.change_log + changes,
                    updated_at=utc_now(),
                )
                self._save_state(state)
                self._append_history(
                    StrategyVersionEntry(
                        version=next_version,
                        previous_version=next_version - 1,
                        timestamp=utc_now(),
                        changes=changes,
                        patch=patch,
                    ),
                    state,
                )
                generation = patched_generation
                message = f"Patched and deployed strategy version {next_version}."
            else:
                state.deployed_version = state.version
                state.pending_deploy = False
                state.updated_at = utc_now()
                self._save_state(state)
                generation = state.generation
                message = f"No effective patch change; deployed existing version {state.version}."
        elif request.deploy_current:
            self._ensure_storage()
            state = self._load_state()
            if state is None:
                raise ValueError("No strategy available to deploy. Generate/deploy a strategy first.")
            if request.version is not None and request.version != state.version:
                raise ValueError("Only the current strategy version can be deployed directly.")
            generation = state.generation
            state.deployed_version = state.version
            state.pending_deploy = False
            state.updated_at = utc_now()
            self._save_state(state)
            message = f"Confirmed and deployed existing strategy version {state.version}."
        else:
            if not request.user_request and not request.has_ui_payload:
                raise ValueError("Provide either user_request or domain fields when deploy_current=false.")
            generate_request = StrategyGenerateRequest(
                user_request=request.user_request,
                domain=request.domain,
                focus_areas=request.focus_areas,
                entities=request.entities,
                keywords=request.keywords,
                source_links=request.source_links,
                advanced_settings=request.advanced_settings,
                timezone=request.timezone,
                schedule_times=request.schedule_times,
                importance_threshold=request.importance_threshold,
                max_signals=request.max_signals,
            )
            generation = self._build_generation(generate_request)

            if self.storage is None:
                message = "Generated and deployed strategy (state tracking disabled)."
            else:
                current = self._load_state()
                if current and _configs_equal(current.generation.config_object, generation.config_object):
                    current.deployed_version = current.version
                    current.pending_deploy = False
                    current.updated_at = utc_now()
                    self._save_state(current)
                    message = f"No duplicate strategy created; deployed existing version {current.version}."
                else:
                    next_version = (current.version + 1) if current else 1
                    next_state = StrategyState(
                        version=next_version,
                        deployed_version=next_version,
                        pending_deploy=False,
                        generation=generation,
                        change_log=(current.change_log if current else []) + ["Generated strategy deployment"],
                        updated_at=utc_now(),
                    )
                    self._save_state(next_state)
                    self._append_history(
                        StrategyVersionEntry(
                            version=next_state.version,
                            previous_version=current.version if current else None,
                            timestamp=utc_now(),
                            changes=["Generated strategy deployment"],
                            patch=None,
                        ),
                        next_state,
                    )
                    message = f"Generated and deployed new strategy version {next_state.version}."

        target_raw = request.target_config_path or os.getenv("MONITOR_CONFIG", "./config/config.yaml")
        target = Path(target_raw).expanduser().resolve()
        if target.exists() and not request.overwrite:
            raise FileExistsError(f"Config already exists: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_suffix(f"{target.suffix}.tmp" if target.suffix else ".tmp")
        temp_path.write_text(generation.config_yaml, encoding="utf-8")
        temp_path.replace(target)

        validated = MonitorConfig.model_validate(generation.config_object)
        self.base_config = validated
        self.config_generator.base_config = validated

        logger.info("Strategy config deployed to %s", target)
        return StrategyDeployResult(
            deployed=True,
            deployed_path=str(target),
            monitor_config_valid=True,
            generation=generation,
            message=message,
        )

    def _apply_patch(
        self,
        generation: StrategyGenerationResult,
        patch: StrategyPatchInstruction,
    ) -> tuple[StrategyGenerationResult, list[str]]:
        parsed_intent = ParsedIntent.model_validate(generation.parsed_intent.model_dump(mode="json"))
        domain_mapping = DomainMapping.model_validate(generation.domain_mapping.model_dump(mode="json"))
        config_object = deepcopy(generation.config_object)
        ui_input = (
            UIStrategyInput.model_validate(generation.ui_input.model_dump(mode="json"))
            if generation.ui_input is not None
            else self._derive_ui_input(parsed_intent, domain_mapping, config_object)
        )
        changes: list[str] = []

        if patch.target == "focus_areas":
            changes.extend(_apply_list_patch(parsed_intent.focus_areas, patch))
            changes.extend(_apply_query_patch(domain_mapping.source_queries, patch))
            _sync_queries(domain_mapping.source_queries, parsed_intent.focus_areas)
        elif patch.target == "entities":
            changes.extend(_apply_list_patch(parsed_intent.entities, patch))
            changes.extend(_apply_query_patch(domain_mapping.source_queries, patch))
            _sync_queries(domain_mapping.source_queries, parsed_intent.entities)
        elif patch.target == "keywords":
            changes.extend(_apply_list_patch(domain_mapping.recommended_tags, patch))
            changes.extend(_apply_query_patch(domain_mapping.source_queries, patch))

        parsed_intent.focus_areas = _dedupe(parsed_intent.focus_areas)
        parsed_intent.entities = _dedupe(parsed_intent.entities)
        domain_mapping.source_queries = _dedupe(domain_mapping.source_queries)
        domain_mapping.recommended_tags = _dedupe(domain_mapping.recommended_tags)

        config_object.setdefault("strategy_profile", {})
        config_object["strategy_profile"]["focus_areas"] = parsed_intent.focus_areas
        config_object["strategy_profile"]["entities"] = parsed_intent.entities
        config_object["strategy_profile"]["keywords"] = domain_mapping.recommended_tags
        ui_input.focus_areas = parsed_intent.focus_areas
        ui_input.entities = parsed_intent.entities
        ui_input.keywords = domain_mapping.recommended_tags

        is_ui_simple = generation.explainability.get("input_mode") == "ui_simple"
        if not (is_ui_simple and ui_input.source_links):
            _sync_google_news_feeds(config_object, domain_mapping.source_queries)

        existing_internal = config_object.get("internal_strategy", {})
        advanced_settings = (
            existing_internal.get("advanced_settings", {})
            if isinstance(existing_internal, dict)
            else {}
        )
        internal_strategy = normalize_ui_input(
            ui_input,
            base_config=self.base_config,
            advanced_settings=advanced_settings if isinstance(advanced_settings, dict) else {},
        )
        config_object["internal_strategy"] = internal_strategy.model_dump(mode="json")
        config_object = MonitorConfig.model_validate(config_object).model_dump(mode="json")
        config_yaml = yaml.safe_dump(config_object, sort_keys=False, allow_unicode=False)

        explainability = deepcopy(generation.explainability)
        explainability.setdefault("patches", [])
        explainability["patches"].append(
            {
                "timestamp": utc_now().isoformat(),
                "operation": patch.operation,
                "target": patch.target,
                "value": patch.value,
                "changes": changes,
            }
        )

        strategy_text = _deterministic_strategy_text(
            parsed_intent=parsed_intent,
            domain_mapping=domain_mapping,
            config_object=config_object,
        )
        return (
            StrategyGenerationResult(
                parsed_intent=parsed_intent,
                domain_mapping=domain_mapping,
                strategy_text=strategy_text,
                config_yaml=config_yaml,
                config_object=config_object,
                ui_input=ui_input,
                internal_strategy=internal_strategy,
                explainability=explainability,
            ),
            changes,
        )

    def _ensure_storage(self) -> None:
        if self.storage is None:
            raise RuntimeError("Strategy state storage is not configured.")

    def _load_state(self) -> StrategyState | None:
        payload = self.storage.load_strategy_state()
        if not payload:
            return None
        try:
            return StrategyState.model_validate(payload)
        except Exception:
            return None

    def _save_state(self, state: StrategyState) -> None:
        self.storage.save_strategy_state(state.model_dump(mode="json"))

    def _append_history(self, entry: StrategyVersionEntry, state: StrategyState) -> None:
        row = entry.model_dump(mode="json")
        row["state_snapshot"] = state.model_dump(mode="json")
        self.storage.append_strategy_history(row)

    def _persist_generated_draft(self, generation: StrategyGenerationResult) -> None:
        if self.storage is None:
            return

        current = self._load_state()
        if current and _configs_equal(current.generation.config_object, generation.config_object):
            current.generation = generation
            current.pending_deploy = current.deployed_version != current.version
            current.updated_at = utc_now()
            self._save_state(current)
            return

        next_version = (current.version + 1) if current else 1
        next_state = StrategyState(
            version=next_version,
            deployed_version=current.deployed_version if current else None,
            pending_deploy=True,
            generation=generation,
            change_log=(current.change_log if current else []) + ["Generated strategy draft"],
            updated_at=utc_now(),
        )
        self._save_state(next_state)
        self._append_history(
            StrategyVersionEntry(
                version=next_state.version,
                previous_version=current.version if current else None,
                timestamp=utc_now(),
                changes=["Generated strategy draft"],
                patch=None,
            ),
            next_state,
        )

    def _collect_known_source_links(self) -> list[str]:
        urls: list[str] = []
        state = self._load_state() if self.storage is not None else None
        if state is not None:
            ui_input = state.generation.ui_input
            if ui_input is not None:
                urls.extend(ui_input.source_links)
            else:
                cfg = state.generation.config_object
                urls.extend(_collect_urls_from_config_object(cfg))

        if not urls and self.base_config is not None:
            for profile in self.base_config.domain_profiles:
                for item in profile.source_links:
                    if isinstance(item, str):
                        token = item.strip()
                        if token:
                            urls.append(token)
                    else:
                        token = str(getattr(item, "url", "")).strip()
                        if token:
                            urls.append(token)
            if not urls:
                urls.extend([row.url for row in self.base_config.sources.rss])
                urls.extend([row.url for row in self.base_config.sources.playwright])
        return _dedupe(urls)


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
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


def _url_key(url: str) -> str:
    token = str(url or "").strip()
    if not token:
        return ""
    try:
        parsed = urlparse(token)
        scheme = (parsed.scheme or "https").lower()
        netloc = (parsed.netloc or "").lower()
        path = (parsed.path or "").rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{scheme}://{netloc}{path}{query}"
    except Exception:
        return token.lower().rstrip("/")


def _parse_source_link_token(token: str) -> dict[str, str]:
    raw = str(token or "").strip()
    if not raw:
        return {"url": "", "type": "auto"}

    lowered = raw.lower()
    for prefix, source_type in (
        ("playwright:", "playwright"),
        ("pw:", "playwright"),
        ("rss:", "rss"),
        ("auto:", "auto"),
    ):
        if lowered.startswith(prefix):
            return {"url": raw[len(prefix) :].strip(), "type": source_type}

    if "|" in raw:
        parts = [part.strip() for part in raw.split("|")]
        url = parts[0] if parts else ""
        source_type = parts[1].lower() if len(parts) >= 2 else "auto"
        if source_type in {"playwright", "pw", "browser", "page"}:
            source_type = "playwright"
        elif source_type in {"rss", "feed", "atom"}:
            source_type = "rss"
        elif source_type != "auto":
            source_type = "auto"
        return {"url": url, "type": source_type}

    return {"url": raw, "type": "auto"}


def _entry_to_source_link(entry: dict[str, str]) -> str | dict[str, object]:
    url = str(entry.get("url", "")).strip()
    source_type = str(entry.get("type", "auto")).strip().lower()
    if not url:
        return ""
    if source_type in {"rss", "playwright"}:
        return {"url": url, "type": source_type}
    return url


def _sanitize_source_link_object(payload: dict[str, object]) -> dict[str, object]:
    allowed = {
        "url",
        "type",
        "name",
        "force_playwright",
        "follow_links_enabled",
        "max_depth",
        "max_links_per_source",
        "same_domain_only",
        "link_selector",
        "article_url_patterns",
        "exclude_url_patterns",
        "article_wait_for_selector",
        "article_content_selector",
    }
    out: dict[str, object] = {}
    for key in allowed:
        if key in payload:
            out[key] = payload[key]
    url = str(out.get("url", "")).strip()
    if not url:
        url = str(payload.get("url", "")).strip()
    out["url"] = url
    source_type = str(out.get("type", "auto")).strip().lower()
    if source_type not in {"auto", "rss", "playwright"}:
        source_type = "auto"
    out["type"] = source_type
    return out


def _source_link_url(item: str | dict[str, object]) -> str:
    if isinstance(item, str):
        return _parse_source_link_token(item).get("url", "").strip()
    return str(item.get("url", "")).strip()


def _dedupe_source_link_entries(items: list[str | dict[str, object]]) -> list[str | dict[str, object]]:
    seen: set[str] = set()
    out: list[str | dict[str, object]] = []
    for item in items:
        url = _source_link_url(item)
        key = _url_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _normalize_token(value: str) -> str:
    return value.strip().lower()


def _split_update_value(value: str) -> tuple[str, str]:
    if "->" in value:
        left, right = value.split("->", 1)
        return left.strip(), right.strip()
    match = value.split(" to ", 1)
    if len(match) == 2:
        return match[0].strip(), match[1].strip()
    raise ValueError("Update patch value must be formatted as 'old -> new'.")


def _index_of(values: list[str], candidate: str) -> int:
    key = _normalize_token(candidate)
    for idx, value in enumerate(values):
        if _normalize_token(value) == key:
            return idx
    return -1


def _apply_list_patch(values: list[str], patch: StrategyPatchInstruction) -> list[str]:
    changes: list[str] = []

    if patch.operation == "add":
        if _index_of(values, patch.value) >= 0:
            return changes
        values.append(patch.value.strip())
        changes.append(f"Added {patch.target}: {patch.value.strip()}")
        return changes

    if patch.operation == "remove":
        idx = _index_of(values, patch.value)
        if idx < 0:
            return changes
        removed = values.pop(idx)
        changes.append(f"Removed {patch.target}: {removed}")
        return changes

    old_value, new_value = _split_update_value(patch.value)
    idx = _index_of(values, old_value)
    if idx < 0:
        raise ValueError(f"Cannot update missing {patch.target} value: {old_value}")
    if _index_of(values, new_value) >= 0:
        values.pop(idx)
        changes.append(f"Removed duplicate {patch.target} source value: {old_value}")
        return changes
    values[idx] = new_value
    changes.append(f"Updated {patch.target}: {old_value} -> {new_value}")
    return changes


def _apply_query_patch(values: list[str], patch: StrategyPatchInstruction) -> list[str]:
    try:
        _apply_list_patch(values, patch)
        return []
    except ValueError:
        if patch.operation != "update":
            raise
        _, new_value = _split_update_value(patch.value)
        if _index_of(values, new_value) >= 0:
            return []
        values.append(new_value)
        return [f"Added source query from {patch.target}: {new_value}"]


def _sync_queries(query_list: list[str], values: list[str]) -> None:
    for value in values:
        if _index_of(query_list, value) < 0:
            query_list.append(value)


def _sync_google_news_feeds(config_object: dict, queries: list[str]) -> None:
    sources = config_object.setdefault("sources", {})
    rss = sources.setdefault("rss", [])
    non_google = []
    for feed in rss:
        if not isinstance(feed, dict):
            continue
        name = str(feed.get("name", ""))
        if name.startswith("Google News - "):
            continue
        non_google.append(feed)

    for query in _dedupe(queries)[:12]:
        non_google.append(
            {
                "name": f"Google News - {query[:40]}",
                "url": f"https://news.google.com/rss/search?q={quote_plus(query)}",
                "max_items": 20,
            }
        )
    sources["rss"] = non_google[:20]


def _deterministic_strategy_text(
    parsed_intent: ParsedIntent,
    domain_mapping: DomainMapping,
    config_object: dict,
) -> str:
    focus = ", ".join(parsed_intent.focus_areas[:6]) or "general high-impact events"
    entities = ", ".join(parsed_intent.entities[:10]) or "none explicitly provided"
    keywords = ", ".join(domain_mapping.recommended_tags[:10]) or "none"
    schedule = config_object.get("schedule", {})
    times = ", ".join(schedule.get("times", []))
    timezone = schedule.get("timezone", "UTC")
    return (
        f"Objective\n"
        f"Track actionable signals for {domain_mapping.canonical_domain} with low redundancy.\n\n"
        f"Scope\n"
        f"Topic: {domain_mapping.canonical_domain}.\n"
        f"Optional refinements: focus areas {focus}; entities {entities}; keywords {keywords}.\n\n"
        f"Operational Cadence\n"
        f"Run at {times} ({timezone}).\n\n"
        f"Controls\n"
        f"Use incremental strategy updates, deterministic config patches, and versioned change history."
    )


def _collect_urls_from_config_object(config_object: dict) -> list[str]:
    urls: list[str] = []
    profiles = config_object.get("domain_profiles", [])
    if isinstance(profiles, list):
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            source_links = profile.get("source_links", [])
            if not isinstance(source_links, list):
                continue
            for item in source_links:
                if isinstance(item, str):
                    token = item.strip()
                    if token:
                        urls.append(token)
                elif isinstance(item, dict):
                    token = str(item.get("url", "")).strip()
                    if token:
                        urls.append(token)
    sources = config_object.get("sources", {})
    if isinstance(sources, dict):
        for row in sources.get("rss", []):
            if isinstance(row, dict):
                token = str(row.get("url", "")).strip()
                if token:
                    urls.append(token)
        for row in sources.get("playwright", []):
            if isinstance(row, dict):
                token = str(row.get("url", "")).strip()
                if token:
                    urls.append(token)
    return _dedupe(urls)


def _configs_equal(left: dict, right: dict) -> bool:
    return yaml.safe_dump(left, sort_keys=True) == yaml.safe_dump(right, sort_keys=True)
