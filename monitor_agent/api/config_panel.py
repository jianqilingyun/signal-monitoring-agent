from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field

from monitor_agent.core.config import load_config
from monitor_agent.core.models import (
    ApiConfig,
    BriefingConfig,
    DomainProfileConfig,
    LLMConfig,
    NotificationsConfig,
    PlaywrightRuntimeConfig,
    ScheduleConfig,
    SourceLinkConfig,
    StrategyProfileConfig,
    TTSConfig,
)


class ConfigSecretsPatch(BaseModel):
    openai_api_key: str | None = None
    embedding_api_key: str | None = None
    tts_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    dingtalk_app_key: str | None = None
    dingtalk_app_secret: str | None = None
    dingtalk_webhook: str | None = None
    dingtalk_secret: str | None = None


class ConfigPanelSaveRequest(BaseModel):
    domain_profiles: list[DomainProfileConfig] = Field(default_factory=list)
    llm: LLMConfig
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    playwright: PlaywrightRuntimeConfig = Field(default_factory=PlaywrightRuntimeConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    tts: TTSConfig
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    secrets: ConfigSecretsPatch = Field(default_factory=ConfigSecretsPatch)
    clear_secrets: list[str] = Field(default_factory=list)


class ConfigPanelService:
    """Read/write local monitoring config for lightweight UI editing."""

    def __init__(self, config_path: str | None = None, repo_root: str | Path | None = None) -> None:
        self.repo_root = (
            Path(repo_root).expanduser().resolve()
            if repo_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.config_path = self._resolve_config_path(config_path)
        self.env_path = self.repo_root / ".env"

    def load_state(self) -> dict[str, Any]:
        effective = load_config(str(self.config_path))
        root_payload = _read_yaml_mapping(self.config_path)
        section_files = self._resolve_section_files(root_payload)
        env_payload = dotenv_values(self.env_path)

        profiles = _derive_domain_profiles(root_payload, effective)
        strategy_profile = _aggregate_strategy_profile(profiles)

        return {
            "domain_profiles": [profile.model_dump(mode="json") for profile in profiles],
            "domain_scope": effective.domain_scope,
            "strategy_profile": strategy_profile.model_dump(mode="json"),
            "resolved_sources": {
                "rss_count": len(effective.sources.rss),
                "playwright_count": len(effective.sources.playwright),
                "rss": [row.model_dump(mode="json") for row in effective.sources.rss],
                "playwright": [row.model_dump(mode="json") for row in effective.sources.playwright],
            },
            "schedule": effective.schedule.model_dump(mode="json"),
            "api": effective.api.model_dump(mode="json"),
            "llm": effective.llm.model_dump(mode="json"),
            "playwright": effective.playwright.model_dump(mode="json"),
            "briefing": effective.briefing.model_dump(mode="json"),
            "tts": effective.tts.model_dump(mode="json"),
            "notifications": effective.notifications.model_dump(mode="json"),
            "secrets_status": {
                "openai_api_key_set": bool((env_payload.get("OPENAI_API_KEY") or "").strip()),
                "embedding_api_key_set": bool((env_payload.get("EMBEDDING_API_KEY") or "").strip()),
                "tts_api_key_set": bool((env_payload.get("TTS_API_KEY") or "").strip()),
                "telegram_bot_token_set": bool((env_payload.get("TELEGRAM_BOT_TOKEN") or "").strip()),
                "telegram_chat_id_set": bool((env_payload.get("TELEGRAM_CHAT_ID") or "").strip()),
                "dingtalk_app_key_set": bool((env_payload.get("DINGTALK_APP_KEY") or "").strip()),
                "dingtalk_app_secret_set": bool((env_payload.get("DINGTALK_APP_SECRET") or "").strip()),
                "dingtalk_webhook_set": bool((env_payload.get("DINGTALK_WEBHOOK") or "").strip()),
                "dingtalk_secret_set": bool((env_payload.get("DINGTALK_SECRET") or "").strip()),
            },
            "section_files": {key: (str(path) if path else None) for key, path in section_files.items()},
            "field_guide": _field_guide(),
        }

    def save_state(self, request: ConfigPanelSaveRequest) -> dict[str, Any]:
        before = load_config(str(self.config_path))
        env_before = dotenv_values(self.env_path)
        root_payload = _read_yaml_mapping(self.config_path)
        section_files = self._resolve_section_files(root_payload)

        profiles = _clean_profiles(request.domain_profiles)
        if not profiles:
            raise ValueError("At least one domain profile is required.")

        domain_scope = _dedupe_tokens([profile.domain for profile in profiles])
        merged_strategy = _aggregate_strategy_profile(profiles)

        root_payload["domain_profiles"] = [profile.model_dump(mode="json") for profile in profiles]
        root_payload["domain"] = domain_scope[0]
        root_payload["domains"] = domain_scope
        root_payload["strategy_profile"] = merged_strategy.model_dump(mode="json")
        if "schedule" in request.model_fields_set:
            root_payload["schedule"] = request.schedule.model_dump(mode="json")
        if "api" in request.model_fields_set:
            root_payload["api"] = request.api.model_dump(mode="json")

        # Keep source ownership explicit: domain_profiles.source_links are canonical for UI usage.
        self._upsert_section(
            key="sources",
            value={"rss": [], "playwright": []},
            root_payload=root_payload,
            section_path=section_files.get("sources"),
        )
        self._upsert_section(
            key="llm",
            value=request.llm.model_dump(mode="json"),
            root_payload=root_payload,
            section_path=section_files.get("llm"),
        )
        root_payload["playwright"] = request.playwright.model_dump(mode="json")
        root_payload["briefing"] = request.briefing.model_dump(mode="json")
        self._upsert_section(
            key="tts",
            value=request.tts.model_dump(mode="json"),
            root_payload=root_payload,
            section_path=section_files.get("tts"),
        )
        root_payload["notifications"] = request.notifications.model_dump(mode="json")

        _write_yaml_mapping(self.config_path, root_payload)

        secret_updates: dict[str, str] = {}
        if request.secrets.openai_api_key is not None:
            value = request.secrets.openai_api_key.strip()
            if value:
                secret_updates["OPENAI_API_KEY"] = value
        if request.secrets.embedding_api_key is not None:
            value = request.secrets.embedding_api_key.strip()
            if value:
                secret_updates["EMBEDDING_API_KEY"] = value
        if request.secrets.tts_api_key is not None:
            value = request.secrets.tts_api_key.strip()
            if value:
                secret_updates["TTS_API_KEY"] = value
        if request.secrets.telegram_bot_token is not None:
            value = request.secrets.telegram_bot_token.strip()
            if value:
                secret_updates["TELEGRAM_BOT_TOKEN"] = value
        if request.secrets.telegram_chat_id is not None:
            value = request.secrets.telegram_chat_id.strip()
            if value:
                secret_updates["TELEGRAM_CHAT_ID"] = value
        if request.secrets.dingtalk_app_key is not None:
            value = request.secrets.dingtalk_app_key.strip()
            if value:
                secret_updates["DINGTALK_APP_KEY"] = value
        if request.secrets.dingtalk_app_secret is not None:
            value = request.secrets.dingtalk_app_secret.strip()
            if value:
                secret_updates["DINGTALK_APP_SECRET"] = value
        if request.secrets.dingtalk_webhook is not None:
            value = request.secrets.dingtalk_webhook.strip()
            if value:
                secret_updates["DINGTALK_WEBHOOK"] = value
        if request.secrets.dingtalk_secret is not None:
            value = request.secrets.dingtalk_secret.strip()
            if value:
                secret_updates["DINGTALK_SECRET"] = value
        clear_secret_env_keys = [_SECRET_ENV_KEYS[key] for key in request.clear_secrets if key in _SECRET_ENV_KEYS]
        if secret_updates or clear_secret_env_keys:
            _upsert_env_values(self.env_path, secret_updates, removals=clear_secret_env_keys)
            for key, value in secret_updates.items():
                os.environ[key] = value
            for key in clear_secret_env_keys:
                os.environ.pop(key, None)

        load_config(str(self.config_path))
        response = self.load_state()
        restart_reasons = _restart_reasons(
            before=before,
            after_api=request.api,
            after_schedule=request.schedule,
            after_notifications=request.notifications,
            changed_env_keys=set(secret_updates) | set(clear_secret_env_keys),
        )
        response["restart_required"] = bool(restart_reasons)
        response["restart_reasons"] = restart_reasons
        return response

    def _resolve_config_path(self, config_path: str | None) -> Path:
        if config_path:
            return Path(config_path).expanduser().resolve()

        env_path = os.getenv("MONITOR_CONFIG", "").strip()
        if env_path:
            return Path(env_path).expanduser().resolve()

        local = (self.repo_root / "config" / "config.local.yaml").resolve()
        if local.exists():
            return local

        default = (self.repo_root / "config" / "config.yaml").resolve()
        if default.exists():
            return default

        example = (self.repo_root / "config" / "config.example.yaml").resolve()
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        return local

    def _resolve_section_files(self, root_payload: dict[str, Any]) -> dict[str, Path | None]:
        section_files: dict[str, Path | None] = {"sources": None, "llm": None, "tts": None}
        imports = root_payload.get("imports", [])
        if not isinstance(imports, list):
            return section_files

        for raw_ref in imports:
            if not isinstance(raw_ref, str) or not raw_ref.strip():
                continue
            ref_path = Path(raw_ref).expanduser()
            if not ref_path.is_absolute():
                ref_path = (self.config_path.parent / ref_path).resolve()
            payload = _read_yaml_mapping(ref_path)
            for key in section_files:
                if section_files[key] is None and isinstance(payload.get(key), dict):
                    section_files[key] = ref_path
        return section_files

    @staticmethod
    def _upsert_section(
        key: str,
        value: dict[str, Any],
        root_payload: dict[str, Any],
        section_path: Path | None,
    ) -> None:
        if section_path is None:
            root_payload[key] = value
            return
        section_payload = _read_yaml_mapping(section_path)
        section_payload[key] = value
        _write_yaml_mapping(section_path, section_payload)
        root_payload.pop(key, None)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return raw
    return {}


def _write_yaml_mapping(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    temp.replace(path)


def _upsert_env_values(path: Path, updates: dict[str, str], removals: list[str] | None = None) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    removals = removals or []
    key_to_index: dict[str, int] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key:
            key_to_index[key] = index

    for key, value in updates.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        rendered = f'{key}="{escaped}"'
        if key in key_to_index:
            lines[key_to_index[key]] = rendered
        else:
            lines.append(rendered)
    for key in removals:
        if key in key_to_index:
            lines[key_to_index[key]] = ""

    path.parent.mkdir(parents=True, exist_ok=True)
    filtered = [line for line in lines if line != ""]
    path.write_text("\n".join(filtered) + ("\n" if filtered else ""), encoding="utf-8")


_SECRET_ENV_KEYS = {
    "openai_api_key": "OPENAI_API_KEY",
    "embedding_api_key": "EMBEDDING_API_KEY",
    "tts_api_key": "TTS_API_KEY",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "dingtalk_app_key": "DINGTALK_APP_KEY",
    "dingtalk_app_secret": "DINGTALK_APP_SECRET",
    "dingtalk_webhook": "DINGTALK_WEBHOOK",
    "dingtalk_secret": "DINGTALK_SECRET",
}


def _restart_reasons(
    *,
    before,
    after_api: ApiConfig,
    after_schedule: ScheduleConfig,
    after_notifications: NotificationsConfig,
    changed_env_keys: set[str],
) -> list[str]:
    reasons: list[str] = []
    if before.api.host != after_api.host:
        reasons.append("API host")
    if before.api.port != after_api.port:
        reasons.append("API port")
    if before.api.scheduler_enabled != after_api.scheduler_enabled:
        reasons.append("API scheduler")
    if before.schedule.enabled != after_schedule.enabled or before.schedule.timezone != after_schedule.timezone or before.schedule.times != after_schedule.times:
        reasons.append("Schedule service")
    if before.api.telegram_ingest_enabled != after_api.telegram_ingest_enabled:
        reasons.append("Telegram inbound")
    if before.api.telegram_ingest_poll_interval_seconds != after_api.telegram_ingest_poll_interval_seconds:
        reasons.append("Telegram inbound poll interval")
    if before.notifications.dingtalk.ingest_enabled != after_notifications.dingtalk.ingest_enabled:
        reasons.append("DingTalk inbound")
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if key in changed_env_keys:
            reasons.append("Telegram credentials")
            break
    for key in ("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET"):
        if key in changed_env_keys:
            reasons.append("DingTalk credentials")
            break
    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        deduped.append(reason)
    return deduped


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


def _clean_profiles(profiles: list[DomainProfileConfig]) -> list[DomainProfileConfig]:
    cleaned: list[DomainProfileConfig] = []
    seen_domain: set[str] = set()
    for profile in profiles:
        domain = profile.domain.strip()
        if not domain:
            continue
        domain_key = domain.lower()
        if domain_key in seen_domain:
            continue
        seen_domain.add(domain_key)
        cleaned.append(
            DomainProfileConfig(
                domain=domain,
                focus_areas=_dedupe_tokens(profile.focus_areas),
                entities=_dedupe_tokens(profile.entities),
                keywords=_dedupe_tokens(profile.keywords),
                source_links=_clean_source_links(profile.source_links),
            )
        )
    return cleaned


def _clean_source_links(links: list[str | SourceLinkConfig]) -> list[str | SourceLinkConfig]:
    cleaned: list[str | SourceLinkConfig] = []
    index_by_url: dict[str, int] = {}
    for item in links:
        if isinstance(item, str):
            url = item.strip()
            if not url:
                continue
            key = url.lower()
            if key in index_by_url:
                continue
            index_by_url[key] = len(cleaned)
            cleaned.append(url)
            continue

        if not isinstance(item, SourceLinkConfig):
            # Pydantic may surface dict-like values depending on parse context.
            try:
                parsed = SourceLinkConfig.model_validate(item)
            except Exception:
                continue
        else:
            parsed = item

        url = parsed.url.strip()
        if not url:
            continue
        key = url.lower()
        existing_index = index_by_url.get(key)
        if existing_index is None:
            index_by_url[key] = len(cleaned)
            cleaned.append(parsed)
            continue
        # Advanced rule should override a simple URL entry for the same source.
        cleaned[existing_index] = parsed
    return cleaned


def _aggregate_strategy_profile(profiles: list[DomainProfileConfig]) -> StrategyProfileConfig:
    focus: list[str] = []
    entities: list[str] = []
    keywords: list[str] = []
    for profile in profiles:
        focus.extend(profile.focus_areas)
        entities.extend(profile.entities)
        keywords.extend(profile.keywords)
    return StrategyProfileConfig(
        focus_areas=_dedupe_tokens(focus),
        entities=_dedupe_tokens(entities),
        keywords=_dedupe_tokens(keywords),
    )


def _derive_domain_profiles(root_payload: dict[str, Any], effective_config) -> list[DomainProfileConfig]:
    raw_profiles = root_payload.get("domain_profiles")
    if isinstance(raw_profiles, list):
        profiles: list[DomainProfileConfig] = []
        for row in raw_profiles:
            if not isinstance(row, dict):
                continue
            try:
                profiles.append(DomainProfileConfig.model_validate(row))
            except Exception:
                continue
        cleaned = _clean_profiles(profiles)
        if cleaned:
            return cleaned

    links: list[str] = []
    links.extend(source.url for source in effective_config.sources.rss)
    links.extend(source.url for source in effective_config.sources.playwright)
    fallback = DomainProfileConfig(
        domain=effective_config.domain,
        focus_areas=effective_config.strategy_profile.focus_areas,
        entities=effective_config.strategy_profile.entities,
        keywords=effective_config.strategy_profile.keywords,
        source_links=_dedupe_tokens(links),
    )
    return [fallback]


def _field_guide() -> list[dict[str, str]]:
    return [
        {
            "field": "来源策略",
            "usage": "每个主题的抓取来源。先点“添加并体检”，需要时再在表格里微调。",
        },
        {
            "field": "主题",
            "usage": "你最关心的方向，比如 AI Infra、网络安全、药物研发。",
        },
        {
            "field": "关注点（高级）",
            "usage": "更细的监控侧重点，比如 GPU 供给、推理成本、云厂商 capex。",
        },
        {
            "field": "重点实体（高级）",
            "usage": "你想重点盯的公司、产品、机构，比如 NVIDIA、OpenAI、AWS。",
        },
        {
            "field": "匹配关键词（高级）",
            "usage": "辅助系统理解你关心的术语、项目名和代号。",
        },
        {
            "field": "多主题汇总",
            "usage": "如果你配置了多个主题，系统会自动合并后再生成简报。",
        },
        {
            "field": "用户输入后自动更新",
            "usage": "收到你发来的链接或文本后，要不要立刻跑一次系统。",
        },
        {
            "field": "Telegram 输入入口",
            "usage": "是否把 Telegram 当成收链接/文本的入口，以及多久轮询一次。",
        },
        {
            "field": "DingTalk 输入入口",
            "usage": "是否把钉钉应用机器人当成收链接/文本的入口。",
        },
        {
            "field": "定时运行",
            "usage": "系统每天什么时候自动跑，是否启用后台定时任务。",
        },
        {
            "field": "浏览器插件",
            "usage": "给 Playwright 用的浏览器扩展目录，适合登录和会话复用。",
        },
        {
            "field": "通知渠道",
            "usage": "简报最后发到哪里。可同时勾选多个通道。Telegram 可收可发，DingTalk 可作为群通知或应用机器人入口。",
        },
        {
            "field": "简报语言",
            "usage": "控制 brief Markdown、Telegram / DingTalk 推送和单篇简报的正文语言。",
        },
        {
            "field": "模型与语音",
            "usage": "LLM、Embedding、TTS 的模型地址、key 和常用参数。",
        },
    ]


CONFIG_PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Monitor Config Panel</title>
  <style>
    :root {
      --bg: #f6f7fb;
      --card: #ffffff;
      --ink: #182033;
      --muted: #5f6c86;
      --line: #d7deea;
      --accent: #155eef;
      --ok: #067647;
      --danger: #b42318;
    }
    body {
      margin: 0;
      font-family: "SF Pro Text","Avenir Next","PingFang SC","Segoe UI",sans-serif;
      background: radial-gradient(circle at 0% 0%, #e7eeff 0, var(--bg) 38%) fixed;
      color: var(--ink);
      padding: 20px;
    }
    .wrap { max-width: 1440px; margin: 0 auto; }
    h1 { margin: 0 0 10px; font-size: 24px; }
    h2 { margin: 0 0 10px; font-size: 17px; }
    label { font-size: 13px; color: var(--muted); display: block; margin: 8px 0 4px; }
    .hint { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      margin-bottom: 18px;
    }
    .topbar .title h1 { margin: 0; }
    .topbar .title .hint {
      margin-top: 8px;
      max-width: 920px;
    }
    .topbar .actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .lang-switch {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.8);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.65);
    }
    .lang-switch button {
      padding: 8px 12px;
      border-radius: 9px;
      background: transparent;
      color: var(--muted);
      font-weight: 700;
    }
    .lang-switch button.active {
      background: #344054;
      color: #fff;
    }
    .layout {
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    .settings-rail {
      position: sticky;
      top: 18px;
      max-height: calc(100vh - 36px);
      overflow: auto;
      padding-right: 4px;
    }
    .settings-shell {
      background: rgba(255, 255, 255, 0.88);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 10px 30px rgba(21, 94, 239, 0.08);
      padding: 12px;
      backdrop-filter: blur(10px);
    }
    .settings-shell h2 { margin-bottom: 6px; }
    .settings-toolbar {
      position: sticky;
      top: 0;
      z-index: 3;
      padding: 6px 0 12px;
      margin-bottom: 8px;
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(255,255,255,0.82));
      backdrop-filter: blur(10px);
      border-bottom: 1px solid rgba(215, 222, 234, 0.75);
    }
    .settings-toolbar .toolbar-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .settings-toolbar .toolbar-status {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      min-height: 16px;
    }
    .settings-section {
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.9);
      overflow: hidden;
    }
    .settings-section summary {
      list-style: none;
      cursor: pointer;
      padding: 10px 12px;
      font-size: 14px;
      font-weight: 700;
      color: var(--ink);
      border-bottom: 1px solid rgba(215, 222, 234, 0.6);
      background: linear-gradient(180deg, rgba(248,250,255,0.95), rgba(255,255,255,0.92));
    }
    .settings-section summary::-webkit-details-marker { display: none; }
    .settings-section .section-body { padding: 12px; }
    .settings-shell .row { grid-template-columns: 1fr; }
    .settings-shell textarea { min-height: 72px; }
    .workspace { min-width: 0; }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 6px 20px rgba(21, 94, 239, 0.07);
    }
    .profile {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      margin-bottom: 10px;
      background: #fbfcff;
    }
    .profile-header {
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
      padding-bottom: 10px;
      border-bottom: 1px dashed var(--line);
    }
    .profile-summary {
      font-size: 14px;
      font-weight: 700;
      color: var(--ink);
      line-height: 1.4;
      min-width: 0;
    }
    input, textarea, select {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 10px;
      font-size: 14px;
      background: #fff;
    }
    textarea { min-height: 80px; resize: vertical; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .actions { display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
    button {
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
      font-weight: 600;
    }
    button.secondary { background: #344054; }
    button.danger { background: var(--danger); }
    #status { font-size: 13px; color: var(--ok); min-height: 20px; margin-top: 8px; }
    .guide-item { padding: 6px 0; border-top: 1px dashed var(--line); }
    .guide-item:first-child { border-top: 0; }
    details { border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; background: #fff; }
    details > summary { cursor: pointer; color: var(--muted); }
    details[open] { background: #fcfdff; }
    .source-table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: #fff; margin-top: 8px; }
    .source-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .source-table th, .source-table td { padding: 8px; border-top: 1px solid var(--line); text-align: left; vertical-align: top; }
    .source-table thead th { border-top: 0; background: #f8faff; color: var(--muted); font-weight: 600; }
    .source-table td a { color: var(--accent); text-decoration: none; word-break: break-all; }
    .source-table td a:hover { text-decoration: underline; }
    .muted-cell { color: var(--muted); }
    .advisory-chip {
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 4px;
      white-space: nowrap;
    }
    .advisory-chip.warning { background: #fff7e6; color: #b54708; }
    .advisory-chip.error { background: #fef3f2; color: #b42318; }
    .advisory-cell {
      min-width: 220px;
      max-width: 320px;
      line-height: 1.45;
      color: var(--muted);
      font-size: 12px;
    }
    .advisory-cell strong { color: var(--ink); }
    .advisory-list {
      margin-top: 12px;
      border-top: 1px dashed var(--line);
      padding-top: 10px;
    }
    .advisory-item {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      margin-top: 8px;
      background: #fcfdff;
    }
    .advisory-item:first-child { margin-top: 0; }
    .advisory-title {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 4px;
    }
    .advisory-body {
      font-size: 13px;
      color: var(--muted);
      line-height: 1.6;
    }
    .advisory-body a { color: var(--accent); text-decoration: none; }
    .advisory-body a:hover { text-decoration: underline; }
    .subsection { margin-top: 12px; }
    .subsection h3 {
      margin: 0 0 8px;
      font-size: 15px;
      color: var(--text);
    }
    .subsection .hint-line {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    .secret-actions {
      display: flex;
      justify-content: flex-end;
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
    }
    .secret-actions label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
    }
    .source-details {
      margin-top: 10px;
      background: #ffffff;
    }
    .source-details summary {
      font-weight: 700;
      color: var(--ink);
      padding: 0;
    }
    .source-details .source-body { padding-top: 8px; }
    @media (max-width: 1024px) {
      .layout { grid-template-columns: 1fr; }
      .topbar {
        flex-direction: column;
        align-items: stretch;
      }
      .topbar .actions { justify-content: flex-start; }
      .settings-rail {
        position: static;
        max-height: none;
        overflow: visible;
        padding-right: 0;
      }
    }
    @media (max-width: 760px) { .row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">
        <h1 id="page_title">Monitoring Config Panel</h1>
        <div id="page_hint" class="hint">每个主题单独配置来源；关注点、实体和关键词是可选高级项，系统通常会自动补全。来源建议通过“添加并体检”自动生成，再在表格中手动微调。</div>
      </div>
      <div class="actions">
        <div class="lang-switch" aria-label="UI language switch">
          <button id="lang_zh" type="button" class="secondary" onclick="setUiLang('zh')">中文</button>
          <button id="lang_en" type="button" class="secondary" onclick="setUiLang('en')">EN</button>
        </div>
        <a id="open_brief_button" class="button secondary" href="/brief/ui">打开 Brief</a>
      </div>
    </div>

    <div class="layout">
      <aside class="settings-rail">
        <section class="settings-shell">
          <div class="settings-toolbar">
            <h2 id="settings_title">Settings</h2>
            <div class="toolbar-actions">
              <button id="refresh_button" class="secondary" onclick="loadConfig()">刷新</button>
              <button id="save_button" onclick="saveConfig()">保存配置</button>
            </div>
            <div id="status" class="toolbar-status"></div>
          </div>
          <div id="settings_hint" class="hint">左侧默认折叠，按“服务与调度 / 模型 / 输入与通知 / 抓取运行”分类；右侧只专注主题与来源。</div>

          <details class="settings-section">
            <summary id="summary_service">服务与调度</summary>
            <div class="section-body">
              <div class="row">
                <div>
                  <label>Schedule Timezone</label>
                  <input id="schedule_timezone" placeholder="Asia/Shanghai" />
                </div>
                <div>
                  <label>Schedule Enabled</label>
                  <select id="schedule_enabled">
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                </div>
              </div>
              <label>Schedule Times（每天执行时间，一行一个 HH:MM）</label>
              <textarea id="schedule_times" placeholder="07:00"></textarea>
              <div class="row">
                <div>
                  <label>API Host</label>
                  <input id="api_host" placeholder="127.0.0.1" />
                </div>
                <div>
                  <label>API Port</label>
                  <input id="api_port" type="number" min="1" max="65535" />
                </div>
              </div>
              <div class="row">
                <div>
                  <label>API Scheduler Enabled</label>
                  <select id="api_scheduler_enabled">
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                </div>
              </div>
            </div>
          </details>

          <details class="settings-section">
            <summary id="summary_models">模型</summary>
            <div class="section-body">
              <div class="subsection">
                <h3>LLM</h3>
                <p class="hint-line">用于信号抽取、策略生成、事件去重与标题改写。</p>
                <div class="row">
                  <div>
                    <label>LLM Base URL</label>
                    <input id="llm_base_url" />
                  </div>
                  <div>
                    <label>LLM Model</label>
                    <input id="llm_model" />
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label>Dedup Model</label>
                    <input id="llm_dedup_model" />
                  </div>
                  <div>
                    <label>LLM Temperature</label>
                    <input id="llm_temperature" type="number" step="0.01" min="0" max="2" />
                  </div>
                </div>
                <label>OPENAI_API_KEY（可选更新）</label>
                <input id="openai_api_key" type="password" placeholder="留空则不修改" />
              </div>

              <div class="subsection">
                <h3>Embedding</h3>
                <p class="hint-line">用于候选召回与相似事件检索。</p>
                <div class="row">
                  <div>
                    <label>Embedding Base URL</label>
                    <input id="embedding_base_url" />
                  </div>
                  <div>
                    <label>Embedding Model</label>
                    <input id="embedding_model" />
                  </div>
                </div>
                <label>EMBEDDING_API_KEY（可选更新）</label>
                <input id="embedding_api_key" type="password" placeholder="留空则不修改" />
              </div>

              <div class="subsection">
                <h3>TTS</h3>
                <p class="hint-line">音频简报默认关闭。gTTS 不需要 key；OpenAI / 自建兼容服务可配置 base_url 和 TTS API key。</p>
                <div class="row">
                  <div>
                    <label>TTS Enabled</label>
                    <select id="tts_enabled">
                      <option value="false">false</option>
                      <option value="true">true</option>
                    </select>
                  </div>
                  <div>
                    <label>TTS Provider</label>
                    <select id="tts_provider">
                      <option value="gtts">gtts</option>
                      <option value="openai">openai</option>
                    </select>
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label>TTS Model</label>
                    <input id="tts_model" />
                  </div>
                  <div>
                    <label>TTS Voice</label>
                    <input id="tts_voice" />
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label>TTS Language</label>
                    <input id="tts_language" placeholder="zh-CN" />
                  </div>
                  <div>
                    <label>TTS Base URL（可选）</label>
                    <input id="tts_base_url" placeholder="http://127.0.0.1:1234/v1" />
                  </div>
                </div>
                <label>TTS API Key（可选更新）</label>
                <input id="tts_api_key" type="password" placeholder="留空则不修改" />
              </div>
            </div>
          </details>

          <details class="settings-section">
            <summary id="summary_io">输入与通知</summary>
            <div class="section-body">
              <div class="subsection">
                <h3>通知路由</h3>
                <p class="hint-line">可同时勾选多个通道；这里只决定发送去哪里，不包含平台凭证。</p>
                <label>Notification Channels</label>
                <div class="row">
                  <label style="display:flex;align-items:center;gap:8px;color:var(--ink);margin:0;"><input id="notify_channel_telegram" type="checkbox" style="width:auto;" /> telegram</label>
                  <label style="display:flex;align-items:center;gap:8px;color:var(--ink);margin:0;"><input id="notify_channel_dingtalk" type="checkbox" style="width:auto;" /> dingtalk</label>
                </div>
              </div>

              <div class="subsection">
                <h3>入站行为</h3>
                <p class="hint-line">定义用户发来的链接/文本是否立刻触发系统更新。</p>
                <div class="row">
                  <div>
                    <label>Auto Run On User Ingest</label>
                    <select id="api_auto_run_on_user_ingest">
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  </div>
                  <div>
                    <label>Brief Language</label>
                    <select id="brief_language">
                      <option value="zh">zh</option>
                      <option value="en">en</option>
                    </select>
                  </div>
                </div>
              </div>

              <div class="subsection">
                <h3>Telegram</h3>
                <p class="hint-line">用于发送简报，也可接收链接/文本并转成 user signal。</p>
                <div class="row">
                  <div>
                    <label>TELEGRAM_BOT_TOKEN（可选更新）</label>
                    <input id="telegram_bot_token" type="password" placeholder="留空则不修改" />
                  </div>
                  <div>
                    <label>TELEGRAM_CHAT_ID（可选更新）</label>
                    <input id="telegram_chat_id" placeholder="留空则不修改" />
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label>Telegram Ingest Enabled</label>
                    <select id="api_telegram_ingest_enabled">
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  </div>
                  <div>
                    <label>Telegram Poll Interval (seconds)</label>
                    <input id="api_telegram_poll_interval" type="number" min="0.5" step="0.5" />
                  </div>
                </div>
              </div>

              <div class="subsection">
                <h3>DingTalk</h3>
                <p class="hint-line">适合工作群通知；也可用应用机器人 + Stream Mode 接收链接/文本。群 webhook 负责输出，应用机器人负责输入。</p>
                <div class="row">
                  <div>
                    <label>DINGTALK_WEBHOOK（可选更新）</label>
                    <input id="dingtalk_webhook" placeholder="留空则不修改" />
                  </div>
                  <div>
                    <label>DINGTALK_SECRET（可选更新）</label>
                    <input id="dingtalk_secret" type="password" placeholder="留空则不修改" />
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label>DINGTALK_APP_KEY（可选更新）</label>
                    <input id="dingtalk_app_key" placeholder="留空则不修改" />
                  </div>
                  <div>
                    <label>DINGTALK_APP_SECRET（可选更新）</label>
                    <input id="dingtalk_app_secret" type="password" placeholder="留空则不修改" />
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label>DingTalk Ingest Enabled</label>
                    <select id="dingtalk_ingest_enabled">
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  </div>
                </div>
              </div>

            </div>
          </details>

          <details class="settings-section">
            <summary id="summary_ingest">抓取运行</summary>
            <div class="section-body">
              <div class="subsection">
                <h3>Playwright</h3>
                <p class="hint-line">用于需要浏览器会话、Cookie、JS 渲染或权限登录的来源。</p>
                <div class="row">
                  <div>
                    <label>Playwright Channel (optional)</label>
                    <input id="pw_channel" placeholder="e.g. chrome / msedge" />
                  </div>
                  <div>
                    <label>Playwright Headless (runtime)</label>
                    <select id="pw_headless">
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  </div>
                </div>
                <label>Playwright Extension Paths (unpacked, one per line)</label>
                <textarea id="pw_extensions"></textarea>
                <label>Playwright Extra Launch Args (one per line)</label>
                <textarea id="pw_args"></textarea>
              </div>
            </div>
          </details>

          <details class="settings-section">
            <summary id="summary_guide">字段说明</summary>
            <div class="section-body">
              <div id="field_guide"></div>
            </div>
          </details>
        </section>
      </aside>

      <main class="workspace">
        <section class="card">
          <h2 id="topics_title">主题策略桶</h2>
          <div id="profiles"></div>
          <div class="actions">
            <button id="add_topic_button" class="secondary" onclick="addProfile()">新增主题</button>
            <button id="collapse_sources_button" class="secondary" onclick="setAllSourceStrategiesExpanded(false)">全部折叠来源策略</button>
            <button id="expand_sources_button" class="secondary" onclick="setAllSourceStrategiesExpanded(true)">全部展开来源策略</button>
          </div>
        </section>
      </main>
    </div>
  </div>

  <script>
    let current = null;
    let latestSourceAdvisories = [];
    let uiLang = localStorage.getItem("monitor_ui_lang") || "zh";
    const DEFAULT_SOURCE_RULE = {
      type: "auto",
      name: "",
      force_playwright: null,
      follow_links_enabled: false,
      incremental_overlap_count: 2,
      refresh_interval_hours: null,
      max_depth: 1,
      max_links_per_source: 3,
      same_domain_only: true,
      link_selector: "a[href]",
      article_url_patterns: [],
      exclude_url_patterns: [],
      article_wait_for_selector: null,
      article_content_selector: "body"
    };
    const SECRET_FIELD_MAP = {
      openai_api_key: "openai_api_key",
      embedding_api_key: "embedding_api_key",
      tts_api_key: "tts_api_key",
      telegram_bot_token: "telegram_bot_token",
      telegram_chat_id: "telegram_chat_id",
      dingtalk_app_key: "dingtalk_app_key",
      dingtalk_app_secret: "dingtalk_app_secret",
      dingtalk_webhook: "dingtalk_webhook",
      dingtalk_secret: "dingtalk_secret",
      smtp_host: "smtp_host",
      smtp_port: "smtp_port",
      smtp_username: "smtp_username",
      smtp_password: "smtp_password",
      smtp_from: "smtp_from",
      smtp_to: "smtp_to"
    };
    const I18N = {
      zh: {
        pageTitle: "Monitoring Config Panel",
        pageHint: "每个主题单独配置来源；关注点、实体和关键词是可选高级项，系统通常会自动补全。来源建议通过“添加并体检”自动生成，再在表格中手动微调。",
        openBrief: "打开 Brief",
        settings: "Settings",
        refresh: "刷新",
        save: "保存配置",
        settingsHint: "左侧默认折叠，按“服务与调度 / 模型 / 输入与通知 / 抓取运行”分类；右侧只专注主题与来源。",
        summaryService: "服务与调度",
        summaryModels: "模型",
        summaryIo: "输入与通知",
        summaryIngest: "抓取运行",
        summaryGuide: "字段说明",
        topicsTitle: "主题策略桶",
        addTopic: "新增主题",
        collapseSources: "全部折叠来源策略",
        expandSources: "全部展开来源策略",
        unnamedTopic: "未命名主题",
        sourceCount: "{count} 个来源",
        deleteTopic: "删除该主题",
        topic: "主题",
        topicRefine: "主题细化（高级，可选）",
        topicRefineHint: "通常不需要手动维护；系统会根据主题和来源自动补全。",
        focusAreas: "关注点（一行一个）",
        entities: "重点实体（一行一个）",
        keywords: "匹配关键词（一行一个）",
        sourceStrategy: "来源策略（默认折叠）",
        sourceStrategyHint: "建议通过“添加并体检”维护；只有需要精调时再直接改表格。",
        addLink: "添加链接（自动体检后写入该主题）",
        typeOverride: "类型覆盖（可选）",
        addAndProbe: "添加并体检",
        addSourceRow: "手动新增来源行",
        advisoryNone: "本轮暂无来源诊断建议。",
        advisoryTitle: "来源诊断建议（{count}）",
        advisoryHint: "仅展示最近一轮发现的问题。系统不会自动删除或替换这些来源。",
        suggestedAlt: "备选",
        suggestedEntry: "备选入口",
        advisoryFixes: "建议",
        adoptSuggestion: "采纳建议",
        advisoryWarning: "提示",
        advisoryError: "异常",
        sourceHealthy: "正常",
        templatePlaceholder: "模板",
        templateForceHtml: "强制 HTML",
        templateForcePlaywright: "强制 Playwright",
        templateConservative: "保守抓取",
        templateListPage: "列表页优先",
        apply: "应用",
        remove: "删除",
        sourceTableUrl: "URL",
        sourceTableType: "Type",
        sourceTableName: "名称",
        sourceTableFollow: "跟链",
        sourceTableMaxLinks: "MaxLinks",
        sourceTableForcePw: "force_pw",
        sourceTableTemplate: "模板",
        sourceTableAdvice: "状态/建议",
        sourceTableAction: "操作",
        optionalName: "可选名称",
        clearSavedValue: "清除已保存值",
        loading: "加载中...",
        loadFailed: "加载失败: {message}",
        loaded: "已加载（sources: rss={rss}, web={web}{advisories}）",
        saveSuccess: "保存成功。新的运行会按主题和来源自动参数执行。",
        saveRestart: "保存成功。以下更改需重启服务后生效：{reasons}",
        saveFailed: "保存失败: {message}",
        missingTopic: "至少需要一个主题配置桶",
        noSuggestion: "当前没有可采纳的来源建议",
        invalidSuggestion: "建议来源无效，无法采纳",
        suggestionAdded: "已新增建议来源：{url}",
        templateApplied: "已应用来源模板：{template}",
        unableLocateTopic: "无法定位当前主题",
        inputLinkFirst: "请先输入链接",
        addLinkFailed: "添加链接失败: {message}",
        probing: "体检中: {url}",
        addLinkDone: "链接已添加并体检：{url}",
        sourceLineSyntaxError: "source_links 行内语法错误: {line}",
        routingHint: "可同时勾选多个通道；这里只决定发送去哪里，不包含平台凭证。",
      },
      en: {
        pageTitle: "Monitoring Config Panel",
        pageHint: "Configure sources per topic. Focus areas, entities, and keywords are optional refinements. The system can usually infer them. Source suggestions are best added through the probe flow, then adjusted in the table only when needed.",
        openBrief: "Open Brief",
        settings: "Settings",
        refresh: "Refresh",
        save: "Save Config",
        settingsHint: "The left rail stays collapsed by default and is grouped by service, models, input, and ingestion. The right side stays focused on topics and sources.",
        summaryService: "Service & Schedule",
        summaryModels: "Models",
        summaryIo: "Input & Delivery",
        summaryIngest: "Ingestion Runtime",
        summaryGuide: "Field Guide",
        topicsTitle: "Topic Buckets",
        addTopic: "Add Topic",
        collapseSources: "Collapse All Source Sections",
        expandSources: "Expand All Source Sections",
        unnamedTopic: "Untitled topic",
        sourceCount: "{count} sources",
        deleteTopic: "Delete Topic",
        topic: "Topic",
        topicRefine: "Topic Refinements (Advanced)",
        topicRefineHint: "Usually not needed. The system can infer these from the topic and its sources.",
        focusAreas: "Focus Areas (one per line)",
        entities: "Tracked Entities (one per line)",
        keywords: "Keywords (one per line)",
        sourceStrategy: "Source Strategy (collapsed by default)",
        sourceStrategyHint: "Prefer “Add and Probe” for maintenance. Edit the table directly only when you need manual overrides.",
        addLink: "Add Link (probe and add to this topic)",
        typeOverride: "Type Override (optional)",
        addAndProbe: "Add and Probe",
        addSourceRow: "Add Source Row",
        advisoryNone: "No source advisories in the latest run.",
        advisoryTitle: "Source Advisories ({count})",
        advisoryHint: "Only shows issues from the latest run. The system will not delete or replace these sources automatically.",
        suggestedAlt: "Alternative",
        suggestedEntry: "Suggested Entry",
        advisoryFixes: "Suggested action",
        adoptSuggestion: "Adopt Suggestion",
        advisoryWarning: "Notice",
        advisoryError: "Error",
        sourceHealthy: "Healthy",
        templatePlaceholder: "Template",
        templateForceHtml: "Force HTML",
        templateForcePlaywright: "Force Playwright",
        templateConservative: "Conservative",
        templateListPage: "List Page First",
        apply: "Apply",
        remove: "Remove",
        sourceTableUrl: "URL",
        sourceTableType: "Type",
        sourceTableName: "Name",
        sourceTableFollow: "Follow",
        sourceTableMaxLinks: "MaxLinks",
        sourceTableForcePw: "Force PW",
        sourceTableTemplate: "Template",
        sourceTableAdvice: "Status / Advice",
        sourceTableAction: "Action",
        optionalName: "Optional name",
        clearSavedValue: "Clear saved value",
        loading: "Loading...",
        loadFailed: "Load failed: {message}",
        loaded: "Loaded (sources: rss={rss}, web={web}{advisories})",
        saveSuccess: "Saved. Future runs will use the updated topic and source settings.",
        saveRestart: "Saved. Restart required for: {reasons}",
        saveFailed: "Save failed: {message}",
        missingTopic: "At least one topic bucket is required",
        noSuggestion: "No suggestion is available for this source",
        invalidSuggestion: "Suggested source is invalid and cannot be adopted",
        suggestionAdded: "Added suggested source: {url}",
        templateApplied: "Applied source template: {template}",
        unableLocateTopic: "Unable to locate the current topic",
        inputLinkFirst: "Enter a link first",
        addLinkFailed: "Failed to add link: {message}",
        probing: "Probing: {url}",
        addLinkDone: "Added and probed: {url}",
        sourceLineSyntaxError: "source_links syntax error: {line}",
        routingHint: "You can select multiple channels here. This only controls delivery targets, not platform credentials.",
      }
    };

    function t(key, vars = {}) {
      const table = I18N[uiLang] || I18N.zh;
      let out = table[key] || I18N.zh[key] || key;
      for (const [k, v] of Object.entries(vars)) {
        out = out.replaceAll(`{${k}}`, String(v));
      }
      return out;
    }

    function toLines(items) {
      return (items || []).join("\\n");
    }

    function fromLines(text) {
      return (text || "").split(/\\r?\\n/).map(v => v.trim()).filter(Boolean);
    }

    function setStatus(message, isError = false) {
      const el = document.getElementById("status");
      el.style.color = isError ? "#b42318" : "#067647";
      el.textContent = message;
    }

    function applyUiLanguage() {
      document.documentElement.lang = uiLang === "en" ? "en" : "zh-CN";
      document.getElementById("page_title").textContent = t("pageTitle");
      document.getElementById("page_hint").textContent = t("pageHint");
      document.getElementById("open_brief_button").textContent = t("openBrief");
      document.getElementById("settings_title").textContent = t("settings");
      document.getElementById("refresh_button").textContent = t("refresh");
      document.getElementById("save_button").textContent = t("save");
      document.getElementById("settings_hint").textContent = t("settingsHint");
      document.getElementById("summary_service").textContent = t("summaryService");
      document.getElementById("summary_models").textContent = t("summaryModels");
      document.getElementById("summary_io").textContent = t("summaryIo");
      document.getElementById("summary_ingest").textContent = t("summaryIngest");
      document.getElementById("summary_guide").textContent = t("summaryGuide");
      document.getElementById("topics_title").textContent = t("topicsTitle");
      document.getElementById("add_topic_button").textContent = t("addTopic");
      document.getElementById("collapse_sources_button").textContent = t("collapseSources");
      document.getElementById("expand_sources_button").textContent = t("expandSources");
      document.getElementById("lang_zh").classList.toggle("active", uiLang === "zh");
      document.getElementById("lang_en").classList.toggle("active", uiLang === "en");
      translateStaticText();
    }

    function setUiLang(next) {
      uiLang = next === "en" ? "en" : "zh";
      localStorage.setItem("monitor_ui_lang", uiLang);
      applyUiLanguage();
      if (current) {
        renderProfiles(current.domain_profiles || []);
        renderFieldGuide(current.field_guide || []);
      }
    }

    function translateStaticText() {
      const replacements = new Map([
        ["服务与调度", t("summaryService")],
        ["模型", t("summaryModels")],
        ["输入与通知", t("summaryIo")],
        ["抓取运行", t("summaryIngest")],
        ["字段说明", t("summaryGuide")],
        ["通知路由", uiLang === "en" ? "Delivery Routing" : "通知路由"],
        ["可同时勾选多个通道；这里只决定发送去哪里，不包含平台凭证。", t("routingHint")],
        ["入站行为", uiLang === "en" ? "Inbound Behavior" : "入站行为"],
        ["定义用户发来的链接/文本是否立刻触发系统更新。", uiLang === "en" ? "Controls whether user-submitted links or text trigger an immediate system run." : "定义用户发来的链接/文本是否立刻触发系统更新。"],
        ["用于信号抽取、策略生成、事件去重与标题改写。", uiLang === "en" ? "Used for signal extraction, strategy generation, event deduplication, and headline rewriting." : "用于信号抽取、策略生成、事件去重与标题改写。"],
        ["用于候选召回与相似事件检索。", uiLang === "en" ? "Used for candidate retrieval and similar-event search." : "用于候选召回与相似事件检索。"],
        ["音频简报默认关闭。gTTS 不需要 key；OpenAI / 自建兼容服务可配置 base_url 和 TTS API key。", uiLang === "en" ? "Audio briefing is disabled by default. gTTS needs no key; OpenAI or compatible self-hosted services can use base_url and a TTS API key." : "音频简报默认关闭。gTTS 不需要 key；OpenAI / 自建兼容服务可配置 base_url 和 TTS API key。"],
        ["用于发送简报，也可接收链接/文本并转成 user signal。", uiLang === "en" ? "Used for brief delivery and can also ingest links or text as user signals." : "用于发送简报，也可接收链接/文本并转成 user signal。"],
        ["适合工作群通知；也可用应用机器人 + Stream Mode 接收链接/文本。群 webhook 负责输出，应用机器人负责输入。", uiLang === "en" ? "Suitable for work-group notifications. App bots with Stream Mode can ingest links or text. Group webhooks handle outbound delivery; app bots handle inbound." : "适合工作群通知；也可用应用机器人 + Stream Mode 接收链接/文本。群 webhook 负责输出，应用机器人负责输入。"],
        ["用于需要浏览器会话、Cookie、JS 渲染或权限登录的来源。", uiLang === "en" ? "For sources that require browser sessions, cookies, JS rendering, or authenticated access." : "用于需要浏览器会话、Cookie、JS 渲染或权限登录的来源。"],
        ["Playwright Extension Paths (unpacked, one per line)", uiLang === "en" ? "Playwright Extension Paths (unpacked, one per line)" : "Playwright Extension Paths（解压目录，一行一个）"],
        ["Playwright Extra Launch Args (one per line)", uiLang === "en" ? "Playwright Extra Launch Args (one per line)" : "Playwright Extra Launch Args（一行一个）"],
        ["Schedule Times（每天执行时间，一行一个 HH:MM）", uiLang === "en" ? "Schedule Times (one HH:MM per line)" : "Schedule Times（每天执行时间，一行一个 HH:MM）"],
        ["Telegram Poll Interval (seconds)", uiLang === "en" ? "Telegram Poll Interval (seconds)" : "Telegram Poll Interval（秒）"],
      ]);
      document.querySelectorAll("label, .hint-line, .subsection h3").forEach((el) => {
        const raw = (el.textContent || "").trim();
        if (replacements.has(raw)) {
          el.textContent = replacements.get(raw);
        }
      });
      document.querySelectorAll(".secret-actions label").forEach((el) => {
        const textNode = Array.from(el.childNodes).find((node) => node.nodeType === Node.TEXT_NODE);
        if (textNode) {
          textNode.textContent = t("clearSavedValue");
        }
      });
    }

    function translateGuideField(field) {
      if (uiLang !== "en") return field;
      return {
        "来源策略": "Source Strategy",
        "主题": "Topic",
        "关注点（高级）": "Focus Areas (Advanced)",
        "重点实体（高级）": "Tracked Entities (Advanced)",
        "匹配关键词（高级）": "Keywords (Advanced)",
        "多主题汇总": "Multi-topic Merge",
        "用户输入后自动更新": "Auto Run After User Input",
        "Telegram 输入入口": "Telegram Input",
        "DingTalk 输入入口": "DingTalk Input",
        "定时运行": "Scheduled Runs",
        "浏览器插件": "Browser Extensions",
        "通知渠道": "Notification Channel",
        "简报语言": "Brief Language",
        "模型与语音": "Models and TTS",
      }[field] || field;
    }

    function translateGuideUsage(text) {
      if (uiLang !== "en") return text;
      return {
        "每个主题的抓取来源。先点“添加并体检”，需要时再在表格里微调。": "Sources attached to each topic. Use “Add and Probe” first, then fine-tune in the table only when needed.",
        "你最关心的方向，比如 AI Infra、网络安全、药物研发。": "Your primary area of interest, such as AI infra, security, or drug discovery.",
        "更细的监控侧重点，比如 GPU 供给、推理成本、云厂商 capex。": "Finer subtopics such as GPU supply, inference cost, or cloud capex.",
        "你想重点盯的公司、产品、机构，比如 NVIDIA、OpenAI、AWS。": "Companies, products, or organizations you want to track closely, such as NVIDIA, OpenAI, or AWS.",
        "辅助系统理解你关心的术语、项目名和代号。": "Extra terms, project names, and code names that help the system match relevant items.",
        "如果你配置了多个主题，系统会自动合并后再生成简报。": "If you configure multiple topics, the system merges them before generating the brief.",
        "收到你发来的链接或文本后，要不要立刻跑一次系统。": "Controls whether the system runs immediately after receiving your links or text.",
        "是否把 Telegram 当成收链接/文本的入口，以及多久轮询一次。": "Controls whether Telegram is used as an input channel and how often it polls.",
        "是否把钉钉应用机器人当成收链接/文本的入口。": "Controls whether a DingTalk app bot is used as an input channel.",
        "系统每天什么时候自动跑，是否启用后台定时任务。": "Defines when the system runs automatically each day and whether background scheduling is enabled.",
        "给 Playwright 用的浏览器扩展目录，适合登录和会话复用。": "Browser extension directories used by Playwright, useful for login and session reuse.",
        "简报最后发到哪里。Telegram 可收可发，DingTalk 可作为群通知或应用机器人入口。": "Where the final brief is delivered. Telegram supports input and output; DingTalk can be used for group delivery or app-bot input.",
        "控制 brief Markdown、Telegram / DingTalk 推送和单篇简报的正文语言。": "Controls the language of the Markdown brief, Telegram / DingTalk delivery, and single-item summaries.",
        "LLM、Embedding、TTS 的模型地址、key 和常用参数。": "Model endpoints, keys, and common parameters for LLM, embeddings, and TTS.",
      }[text] || text;
    }

    function renderFieldGuide(items) {
      const guide = document.getElementById("field_guide");
      guide.innerHTML = "";
      for (const item of (items || [])) {
        const line = document.createElement("div");
        line.className = "guide-item";
        line.innerHTML = `<strong>${escapeHtml(translateGuideField(item.field || ""))}</strong>：${escapeHtml(translateGuideUsage(item.usage || ""))}`;
        guide.appendChild(line);
      }
    }

    function decorateSecretInputs() {
      for (const [inputId, clearKey] of Object.entries(SECRET_FIELD_MAP)) {
        const input = document.getElementById(inputId);
        if (!input || input.dataset.secretDecorated === "true") continue;
        input.dataset.secretDecorated = "true";
        const wrapper = document.createElement("div");
        wrapper.className = "secret-actions";
        wrapper.innerHTML = `<label><input type="checkbox" id="${inputId}__clear" data-clear-key="${clearKey}" />清除已保存值</label>`;
        input.insertAdjacentElement("afterend", wrapper);
      }
    }

    function resetSecretClearControls() {
      document.querySelectorAll("[data-clear-key]").forEach((el) => {
        el.checked = false;
      });
    }

    function normalizeSourceLink(item) {
      if (typeof item === "string") {
        const parsed = parseSourceLine(item);
        if (parsed && typeof parsed === "object") {
          const url = String(parsed.url || "").trim();
          if (!url) return null;
          const type = ["auto", "rss", "playwright"].includes(parsed.type) ? parsed.type : "auto";
          return { ...DEFAULT_SOURCE_RULE, ...parsed, url, type };
        }
        return { ...DEFAULT_SOURCE_RULE, url: item.trim() };
      }
      if (!item || typeof item !== "object") {
        return null;
      }
      const url = String(item.url || "").trim();
      if (!url) return null;
      const type = ["auto", "rss", "playwright"].includes(item.type) ? item.type : "auto";
        return {
          ...DEFAULT_SOURCE_RULE,
          ...item,
          url,
          type,
          name: String(item.name || "").trim(),
          force_playwright: item.force_playwright === true ? true : (item.force_playwright === false ? false : null),
          follow_links_enabled: Boolean(item.follow_links_enabled),
          incremental_overlap_count: Number(item.incremental_overlap_count || 2),
          refresh_interval_hours: item.refresh_interval_hours == null ? null : Number(item.refresh_interval_hours),
          max_depth: Number(item.max_depth || 1),
          max_links_per_source: Number(item.max_links_per_source || 3),
          same_domain_only: item.same_domain_only !== false,
          link_selector: String(item.link_selector || "a[href]").trim() || "a[href]",
        article_url_patterns: (item.article_url_patterns || []).filter(Boolean),
        exclude_url_patterns: (item.exclude_url_patterns || []).filter(Boolean),
        article_wait_for_selector: item.article_wait_for_selector ? String(item.article_wait_for_selector).trim() : null,
        article_content_selector: String(item.article_content_selector || "body").trim() || "body"
      };
    }

    function compactSourceRule(item) {
      const normalized = normalizeSourceLink(item);
      if (!normalized) return null;
      const out = { url: normalized.url };
      if (normalized.type !== "auto") out.type = normalized.type;
      if (normalized.name) out.name = normalized.name;
      if (normalized.force_playwright === true) out.force_playwright = true;
      if (normalized.force_playwright === false) out.force_playwright = false;
      if (normalized.follow_links_enabled) out.follow_links_enabled = true;
      if (normalized.incremental_overlap_count !== 2) out.incremental_overlap_count = normalized.incremental_overlap_count;
      if (normalized.refresh_interval_hours != null) out.refresh_interval_hours = normalized.refresh_interval_hours;
      if (normalized.max_depth !== 1) out.max_depth = normalized.max_depth;
      if (normalized.max_links_per_source !== 3) out.max_links_per_source = normalized.max_links_per_source;
      if (normalized.same_domain_only !== true) out.same_domain_only = normalized.same_domain_only;
      if (normalized.link_selector !== "a[href]") out.link_selector = normalized.link_selector;
      if (normalized.article_url_patterns.length) out.article_url_patterns = normalized.article_url_patterns;
      if (normalized.exclude_url_patterns.length) out.exclude_url_patterns = normalized.exclude_url_patterns;
      if (normalized.article_wait_for_selector) out.article_wait_for_selector = normalized.article_wait_for_selector;
      if (normalized.article_content_selector !== "body") out.article_content_selector = normalized.article_content_selector;
      return out;
    }

    function normalizeSourceType(token) {
      const value = String(token || "").trim().toLowerCase();
      if (["playwright", "pw", "browser", "page"].includes(value)) return "playwright";
      if (["rss", "feed", "atom"].includes(value)) return "rss";
      if (["auto"].includes(value)) return "auto";
      return "";
    }

    function parseSourceLine(line) {
      const raw = String(line || "").trim();
      if (!raw) return null;

      const lower = raw.toLowerCase();
      for (const prefix of ["playwright:", "pw:", "rss:", "auto:"]) {
        if (lower.startsWith(prefix)) {
          const sourceType = normalizeSourceType(prefix.replace(":", ""));
          const url = raw.slice(prefix.length).trim();
          if (!url) {
            throw new Error(t("sourceLineSyntaxError", { line: raw }));
          }
          return { url, type: sourceType };
        }
      }

      if (raw.includes("|")) {
        const parts = raw.split("|").map(v => v.trim());
        const url = parts[0] || "";
        const sourceType = normalizeSourceType(parts[1] || "");
        if (!url) {
          throw new Error(t("sourceLineSyntaxError", { line: raw }));
        }
        if (sourceType) {
          const out = { url, type: sourceType };
          const name = (parts[2] || "").trim();
          if (name) out.name = name;
          return out;
        }
        return url;
      }

      return raw;
    }

    function sourceTypeOptions(selected) {
      const value = String(selected || "auto");
      return [
        `<option value="auto"${value === "auto" ? " selected" : ""}>auto</option>`,
        `<option value="rss"${value === "rss" ? " selected" : ""}>rss</option>`,
        `<option value="playwright"${value === "playwright" ? " selected" : ""}>playwright</option>`
      ].join("");
    }

    function forcePlaywrightOptions(value) {
      const token = value === true ? "true" : (value === false ? "false" : "auto");
      return [
        `<option value="auto"${token === "auto" ? " selected" : ""}>auto</option>`,
        `<option value="true"${token === "true" ? " selected" : ""}>true</option>`,
        `<option value="false"${token === "false" ? " selected" : ""}>false</option>`
      ].join("");
    }

    function advisoryChip(severity) {
      const token = String(severity || "warning").toLowerCase();
      const cls = token === "error" ? "error" : "warning";
      const label = cls === "error" ? t("advisoryError") : t("advisoryWarning");
      return `<span class="advisory-chip ${cls}">${label}</span>`;
    }

    function normalizeUrlKey(url) {
      const token = String(url || "").trim();
      if (!token) return "";
      try {
        const parsed = new URL(token);
        parsed.hash = "";
        let normalized = parsed.toString();
        if (normalized.endsWith("/")) normalized = normalized.slice(0, -1);
        return normalized.toLowerCase();
      } catch (err) {
        return token.toLowerCase().replace(/\\/+$/, "");
      }
    }

    function advisoryForUrl(url) {
      const key = normalizeUrlKey(url);
      if (!key) return null;
      return (latestSourceAdvisories || []).find((row) => normalizeUrlKey(row.source_url) === key) || null;
    }

    function profileAdvisories(profile) {
      const matches = [];
      const seen = new Set();
      for (const item of (profile.source_links || [])) {
        const normalized = normalizeSourceLink(item);
        if (!normalized || !normalized.url) continue;
        const advisory = advisoryForUrl(normalized.url);
        if (!advisory) continue;
        const advisoryKey = String(advisory.source_key || normalizeUrlKey(advisory.source_url));
        if (seen.has(advisoryKey)) continue;
        seen.add(advisoryKey);
        matches.push(advisory);
      }
      return matches;
    }

    function advisoryCellTemplate(advisory) {
      if (!advisory) {
        return `<span class="muted-cell">${escapeHtml(t("sourceHealthy"))}</span>`;
      }
      const summary = escapeHtml(advisory.summary || "");
      const suggested = advisory.suggested_source_link && advisory.suggested_source_link.url
        ? `<div><strong>${escapeHtml(t("suggestedAlt"))}：</strong><a href="${escapeHtml(advisory.suggested_source_link.url)}" target="_blank" rel="noreferrer">${escapeHtml(advisory.suggested_source_link.url)}</a></div>
           <div style="margin-top:6px;"><button class="secondary" type="button" onclick="adoptSuggestedSource(this, '${escapeHtml(String(advisory.source_url || ""))}')">${escapeHtml(t("adoptSuggestion"))}</button></div>`
        : "";
      return `
        <div class="advisory-cell">
          ${advisoryChip(advisory.severity)}
          <div>${summary}</div>
          ${suggested}
        </div>
      `;
    }

    function advisoryListTemplate(profile) {
      const advisories = profileAdvisories(profile);
      if (!advisories.length) {
        return `<div class="advisory-list"><div class="hint-line">${escapeHtml(t("advisoryNone"))}</div></div>`;
      }
      const items = advisories.map((advisory) => {
        const fixes = Array.isArray(advisory.fixes) && advisory.fixes.length
          ? `<div><strong>${escapeHtml(t("advisoryFixes"))}：</strong>${escapeHtml(advisory.fixes.join("；"))}</div>`
          : "";
        const suggested = advisory.suggested_source_link && advisory.suggested_source_link.url
          ? `<div><strong>${escapeHtml(t("suggestedEntry"))}：</strong><a href="${escapeHtml(advisory.suggested_source_link.url)}" target="_blank" rel="noreferrer">${escapeHtml(advisory.suggested_source_link.url)}</a></div>
             <div style="margin-top:6px;"><button class="secondary" type="button" onclick="adoptSuggestedSource(this, '${escapeHtml(String(advisory.source_url || ""))}')">${escapeHtml(t("adoptSuggestion"))}</button></div>`
          : "";
        return `
          <div class="advisory-item">
            <div class="advisory-title">
              ${advisoryChip(advisory.severity)}
              <span>${escapeHtml(advisory.source_name || advisory.source_url || "")}</span>
            </div>
            <div class="advisory-body">
              <div>${escapeHtml(advisory.summary || "")}</div>
              ${suggested}
              ${fixes}
            </div>
          </div>
        `;
      }).join("");
      return `
        <details class="advisory-list">
          <summary>${escapeHtml(t("advisoryTitle", { count: advisories.length }))}</summary>
          <div class="hint-line">${escapeHtml(t("advisoryHint"))}</div>
          ${items}
        </details>
      `;
    }

    function sourceRowTemplate(item) {
      const link = normalizeSourceLink(item) || { ...DEFAULT_SOURCE_RULE, url: "", type: "auto" };
      const url = escapeHtml(link.url || "");
      const name = escapeHtml(link.name || "");
      const maxLinks = Number(link.max_links_per_source || 3);
      const advisory = advisoryForUrl(link.url || "");
      const metaJson = escapeHtml(JSON.stringify({
        incremental_overlap_count: link.incremental_overlap_count,
        refresh_interval_hours: link.refresh_interval_hours,
        max_depth: link.max_depth,
        same_domain_only: link.same_domain_only,
        link_selector: link.link_selector,
        article_url_patterns: link.article_url_patterns,
        exclude_url_patterns: link.exclude_url_patterns,
        article_wait_for_selector: link.article_wait_for_selector,
        article_content_selector: link.article_content_selector
      }));
      return `
        <tr class="p-source-row" data-source-meta="${metaJson}">
          <td><input class="s-url" value="${url}" placeholder="https://example.com/news/" /></td>
          <td><select class="s-type">${sourceTypeOptions(link.type)}</select></td>
          <td><input class="s-name" value="${name}" placeholder="${escapeHtml(t("optionalName"))}" /></td>
          <td><input class="s-follow" type="checkbox" ${link.follow_links_enabled ? "checked" : ""} /></td>
          <td><input class="s-max-links" type="number" min="1" max="20" value="${maxLinks}" /></td>
          <td><select class="s-force">${forcePlaywrightOptions(link.force_playwright)}</select></td>
          <td>
            <select class="s-template">
              <option value="">${escapeHtml(t("templatePlaceholder"))}</option>
              <option value="force_html">${escapeHtml(t("templateForceHtml"))}</option>
              <option value="force_playwright">${escapeHtml(t("templateForcePlaywright"))}</option>
              <option value="conservative">${escapeHtml(t("templateConservative"))}</option>
              <option value="list_page">${escapeHtml(t("templateListPage"))}</option>
            </select>
            <div style="margin-top:6px;"><button class="secondary" type="button" onclick="applySourceTemplate(this)">${escapeHtml(t("apply"))}</button></div>
          </td>
          <td>${advisoryCellTemplate(advisory)}</td>
          <td><button class="danger" type="button" onclick="removeSourceRow(this)">${escapeHtml(t("remove"))}</button></td>
        </tr>
      `;
    }

    function sourceTableRows(profile) {
      const rows = [];
      for (const item of (profile.source_links || [])) {
        const link = normalizeSourceLink(item);
        if (!link || !link.url) continue;
        rows.push(sourceRowTemplate(link));
      }
      if (!rows.length) {
        rows.push(sourceRowTemplate({ ...DEFAULT_SOURCE_RULE, url: "", type: "auto" }));
      }
      return rows.join("");
    }

  function profileSummaryLabel(profile) {
    const domain = String(profile.domain || "").trim() || t("unnamedTopic");
    const sources = (profile.source_links || []).length;
    return `${domain} | ${t("sourceCount", { count: sources })}`;
  }

    function profileCardTemplate(profile, index) {
      return `
        <div class="profile" data-index="${index}">
          <div class="profile-header">
            <div class="profile-summary">${escapeHtml(profileSummaryLabel(profile))}</div>
            <button class="danger" type="button" onclick="removeProfile(this)">${escapeHtml(t("deleteTopic"))}</button>
          </div>
          <div class="row">
            <div>
              <label>${escapeHtml(t("topic"))}</label>
              <input class="p-domain" value="${escapeHtml(profile.domain || "")}" />
            </div>
          </div>

          <details class="source-details" style="margin-top: 16px;">
            <summary>${escapeHtml(t("topicRefine"))}</summary>
            <div class="source-body">
              <div class="hint-line">${escapeHtml(t("topicRefineHint"))}</div>
              <div class="row">
                <div>
                  <label>${escapeHtml(t("focusAreas"))}</label>
                  <textarea class="p-focus">${escapeHtml(toLines(profile.focus_areas || []))}</textarea>
                </div>
                <div>
                  <label>${escapeHtml(t("entities"))}</label>
                  <textarea class="p-entities">${escapeHtml(toLines(profile.entities || []))}</textarea>
                </div>
              </div>
              <label>${escapeHtml(t("keywords"))}</label>
              <textarea class="p-keywords">${escapeHtml(toLines(profile.keywords || []))}</textarea>
            </div>
          </details>

          <details class="source-details">
            <summary>${escapeHtml(t("sourceStrategy"))}</summary>
            <div class="source-body">
              <div class="hint-line">${escapeHtml(t("sourceStrategyHint"))}</div>
              <div class="source-table-wrap">
                <table class="source-table">
                  <thead>
                    <tr>
                      <th>${escapeHtml(t("sourceTableUrl"))}</th>
                      <th>${escapeHtml(t("sourceTableType"))}</th>
                      <th>${escapeHtml(t("sourceTableName"))}</th>
                      <th>${escapeHtml(t("sourceTableFollow"))}</th>
                      <th>${escapeHtml(t("sourceTableMaxLinks"))}</th>
                      <th>${escapeHtml(t("sourceTableForcePw"))}</th>
                      <th>${escapeHtml(t("sourceTableTemplate"))}</th>
                      <th>${escapeHtml(t("sourceTableAdvice"))}</th>
                      <th>${escapeHtml(t("sourceTableAction"))}</th>
                    </tr>
                  </thead>
                  <tbody class="p-source-table">${sourceTableRows(profile)}</tbody>
                </table>
              </div>

              <div class="row">
                <div>
                  <label>${escapeHtml(t("addLink"))}</label>
                  <input class="p-add-link" placeholder="https://example.com/news/" />
                </div>
                <div>
                  <label>${escapeHtml(t("typeOverride"))}</label>
                  <select class="p-add-type">
                    <option value="auto">auto</option>
                    <option value="rss">rss</option>
                    <option value="playwright">playwright</option>
                  </select>
                </div>
              </div>
              <div class="actions">
                <button class="secondary" type="button" onclick="addLinkWithDiagnosis(this)">${escapeHtml(t("addAndProbe"))}</button>
                <button class="secondary" type="button" onclick="addManualSourceRow(this)">${escapeHtml(t("addSourceRow"))}</button>
              </div>
              <div class="hint p-diagnosis"></div>
              ${advisoryListTemplate(profile)}
            </div>
          </details>
        </div>
      `;
    }

    function escapeHtml(str) {
      return String(str)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function renderProfiles(profiles) {
      const root = document.getElementById("profiles");
      root.innerHTML = "";
      const rows = profiles && profiles.length ? profiles : [emptyProfile()];
      rows.forEach((profile, index) => {
        root.insertAdjacentHTML("beforeend", profileCardTemplate(profile, index));
      });
    }

    function emptyProfile() {
      return {
        domain: "",
        focus_areas: [],
        entities: [],
        keywords: [],
        source_links: []
      };
    }

    function addProfile() {
      const existing = collectProfiles();
      existing.push(emptyProfile());
      renderProfiles(existing);
    }

    function removeProfile(button) {
      const card = button.closest(".profile");
      if (!card) return;
      card.remove();
      if (!document.querySelector("#profiles .profile")) {
        renderProfiles([emptyProfile()]);
      }
    }

    function setAllSourceStrategiesExpanded(opened) {
      const details = Array.from(document.querySelectorAll(".source-details"));
      for (const d of details) {
        d.open = Boolean(opened);
      }
    }

    function readProfileFromCard(card) {
      const rows = Array.from(card.querySelectorAll(".p-source-row"));
      const links = [];
      for (const row of rows) {
        const url = (row.querySelector(".s-url")?.value || "").trim();
        if (!url) continue;
        const type = normalizeSourceType(row.querySelector(".s-type")?.value || "") || "auto";
        const name = (row.querySelector(".s-name")?.value || "").trim();
        const follow = row.querySelector(".s-follow")?.checked === true;
        const maxLinks = Number(row.querySelector(".s-max-links")?.value || "3");
        const forceToken = (row.querySelector(".s-force")?.value || "auto").trim().toLowerCase();
        const forceValue = forceToken === "true" ? true : (forceToken === "false" ? false : null);
        let meta = {};
        try {
          meta = JSON.parse(row.dataset.sourceMeta || "{}");
        } catch (err) {
          meta = {};
        }
        const normalized = normalizeSourceLink({
          ...meta,
          url,
          type,
          name,
          follow_links_enabled: follow,
          max_links_per_source: maxLinks,
          force_playwright: forceValue,
        });
        if (!normalized) continue;
        const entry = sourceEntryFromNormalized(normalized);
        if (entry) links.push(entry);
      }
      return {
        domain: card.querySelector(".p-domain")?.value?.trim() || "",
        focus_areas: fromLines(card.querySelector(".p-focus")?.value || ""),
        entities: fromLines(card.querySelector(".p-entities")?.value || ""),
        keywords: fromLines(card.querySelector(".p-keywords")?.value || ""),
        source_links: links,
      };
    }

    function sourceEntryFromNormalized(normalized) {
      const compact = compactSourceRule(normalized);
      if (!compact) return null;
      const keys = Object.keys(compact);
      if (keys.length === 1 && compact.url) {
        return compact.url;
      }
      return compact;
    }

    function upsertSourceLink(profile, rawLink) {
      const normalized = normalizeSourceLink(rawLink);
      if (!normalized || !normalized.url) return profile;
      const key = normalized.url.trim().toLowerCase();
      const next = [];
      let replaced = false;
      for (const item of (profile.source_links || [])) {
        const existing = normalizeSourceLink(item);
        if (existing && existing.url.trim().toLowerCase() === key) {
          if (!replaced) {
            const entry = sourceEntryFromNormalized(normalized);
            if (entry) next.push(entry);
            replaced = true;
          }
          continue;
        }
        next.push(item);
      }
      if (!replaced) {
        const entry = sourceEntryFromNormalized(normalized);
        if (entry) next.push(entry);
      }
      profile.source_links = next;
      return profile;
    }

    function updateSourceViews(card, profile) {
      const body = card.querySelector(".p-source-table");
      if (body) {
        body.innerHTML = sourceTableRows(profile);
      }
      const advisoryMount = card.querySelector(".advisory-list");
      if (advisoryMount) {
        advisoryMount.outerHTML = advisoryListTemplate(profile);
      }
      const summary = card.querySelector(".profile-summary");
      if (summary) {
        summary.textContent = profileSummaryLabel(profile);
      }
    }

    function addManualSourceRow(button) {
      const card = button.closest(".profile");
      if (!card) return;
      const body = card.querySelector(".p-source-table");
      if (!body) return;
      body.insertAdjacentHTML("beforeend", sourceRowTemplate({ ...DEFAULT_SOURCE_RULE, url: "", type: "auto" }));
    }

    function removeSourceRow(button) {
      const row = button.closest(".p-source-row");
      if (!row) return;
      const body = row.parentElement;
      row.remove();
      if (body && !body.querySelector(".p-source-row")) {
        body.insertAdjacentHTML("beforeend", sourceRowTemplate({ ...DEFAULT_SOURCE_RULE, url: "", type: "auto" }));
      }
    }

    function rowToNormalized(row) {
      if (!row) return null;
      let meta = {};
      try {
        meta = JSON.parse(row.dataset.sourceMeta || "{}");
      } catch (err) {
        meta = {};
      }
      return normalizeSourceLink({
        ...meta,
        url: (row.querySelector(".s-url")?.value || "").trim(),
        type: normalizeSourceType(row.querySelector(".s-type")?.value || "") || "auto",
        name: (row.querySelector(".s-name")?.value || "").trim(),
        follow_links_enabled: row.querySelector(".s-follow")?.checked === true,
        max_links_per_source: Number(row.querySelector(".s-max-links")?.value || "3"),
        force_playwright: (() => {
          const token = (row.querySelector(".s-force")?.value || "auto").trim().toLowerCase();
          return token === "true" ? true : (token === "false" ? false : null);
        })(),
      });
    }

    function writeNormalizedToRow(row, normalized) {
      const next = normalizeSourceLink(normalized);
      if (!row || !next) return;
      row.querySelector(".s-url").value = next.url || "";
      row.querySelector(".s-type").value = next.type || "auto";
      row.querySelector(".s-name").value = next.name || "";
      row.querySelector(".s-follow").checked = Boolean(next.follow_links_enabled);
      row.querySelector(".s-max-links").value = String(next.max_links_per_source || 3);
      row.querySelector(".s-force").value = next.force_playwright === true ? "true" : (next.force_playwright === false ? "false" : "auto");
      row.dataset.sourceMeta = JSON.stringify({
        incremental_overlap_count: next.incremental_overlap_count,
        refresh_interval_hours: next.refresh_interval_hours,
        max_depth: next.max_depth,
        same_domain_only: next.same_domain_only,
        link_selector: next.link_selector,
        article_url_patterns: next.article_url_patterns,
        exclude_url_patterns: next.exclude_url_patterns,
        article_wait_for_selector: next.article_wait_for_selector,
        article_content_selector: next.article_content_selector
      });
      const advisoryCell = row.children[7];
      if (advisoryCell) {
        advisoryCell.innerHTML = advisoryCellTemplate(advisoryForUrl(next.url || ""));
      }
    }

    function applyTemplateRule(base, templateName) {
      const next = normalizeSourceLink(base);
      if (!next) return null;
      if (templateName === "force_html") {
        next.type = "playwright";
        next.force_playwright = false;
        next.follow_links_enabled = false;
      } else if (templateName === "force_playwright") {
        next.type = "playwright";
        next.force_playwright = true;
        next.follow_links_enabled = true;
        next.max_links_per_source = Math.max(3, Number(next.max_links_per_source || 3));
      } else if (templateName === "conservative") {
        next.follow_links_enabled = false;
        next.max_links_per_source = 2;
        next.refresh_interval_hours = next.refresh_interval_hours || 48;
        next.incremental_overlap_count = 1;
      } else if (templateName === "list_page") {
        next.type = next.type === "rss" ? "playwright" : next.type;
        next.follow_links_enabled = true;
        next.max_depth = 1;
        next.max_links_per_source = Math.max(3, Number(next.max_links_per_source || 3));
        next.same_domain_only = true;
      }
      return next;
    }

    function applySourceTemplate(button) {
      const row = button.closest(".p-source-row");
      if (!row) return;
      const template = (row.querySelector(".s-template")?.value || "").trim();
      if (!template) return;
      const currentRule = rowToNormalized(row);
      const nextRule = applyTemplateRule(currentRule, template);
      if (!nextRule) return;
      writeNormalizedToRow(row, nextRule);
      row.querySelector(".s-template").value = "";
      const card = row.closest(".profile");
      if (card) {
        const profile = readProfileFromCard(card);
        updateSourceViews(card, profile);
      }
      setStatus(t("templateApplied", { template }));
    }

    function adoptSuggestedSource(button, sourceUrl) {
      const advisory = advisoryForUrl(sourceUrl);
      if (!advisory || !advisory.suggested_source_link) {
        setStatus(t("noSuggestion"), true);
        return;
      }
      const card = button.closest(".profile");
      if (!card) return;
      const profile = readProfileFromCard(card);
      const normalized = normalizeSourceLink(advisory.suggested_source_link);
      if (!normalized) {
        setStatus(t("invalidSuggestion"), true);
        return;
      }
      upsertSourceLink(profile, normalized);
      updateSourceViews(card, profile);
      setStatus(t("suggestionAdded", { url: normalized.url }));
    }

    async function addLinkWithDiagnosis(button) {
      try {
        const card = button.closest(".profile");
        if (!card) {
          throw new Error(t("unableLocateTopic"));
        }
        const profile = readProfileFromCard(card);
        const url = (card.querySelector(".p-add-link")?.value || "").trim();
        const forceType = (card.querySelector(".p-add-type")?.value || "auto").trim().toLowerCase();
        if (!url) {
          throw new Error(t("inputLinkFirst"));
        }

        setStatus(t("probing", { url }));
        const resp = await fetch("/strategy/source/suggest", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            urls: [url],
            force_refresh: true,
            refresh_interval_days: 14
          })
        });
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data.detail || "来源体检失败");
        }
        const row = (data.suggestions || [])[0];
        if (!row) {
          throw new Error(t("emptySourceSuggestion"));
        }

        const normalized = normalizeSourceLink((row.normalized_source_link || { url }));
        if (!normalized) {
          throw new Error(t("invalidSourceStrategy"));
        }
        if (forceType === "rss" || forceType === "playwright") {
          normalized.type = forceType;
          if (forceType === "playwright") {
            normalized.force_playwright = true;
          }
          if (forceType === "rss") {
            normalized.force_playwright = null;
          }
        }
        upsertSourceLink(profile, normalized);
        updateSourceViews(card, profile);

        const issues = (row.issues || []).join(" ; ");
        const fixes = (row.fixes || []).join(" ; ");
        const diag = card.querySelector(".p-diagnosis");
        if (diag) {
          diag.textContent = [
            `[${row.probe_status || "ok"}] ${row.parser_recommendation || ""} / ${row.configured_type || ""}`,
            issues ? `issues: ${issues}` : "",
            fixes ? `fixes: ${fixes}` : ""
          ].filter(Boolean).join(" | ");
        }
        const addInput = card.querySelector(".p-add-link");
        if (addInput) {
          addInput.value = "";
        }
        setStatus(t("addLinkDone", { url: normalized.url }));
      } catch (err) {
        setStatus(t("addLinkFailed", { message: err.message }), true);
      }
    }

    function collectProfiles() {
      const cards = Array.from(document.querySelectorAll("#profiles .profile"));
      return cards.map(readProfileFromCard).filter(p => p.domain);
    }

    async function loadConfig() {
      try {
        setStatus(t("loading"));
        const [configResp, advisoryResp] = await Promise.all([
          fetch("/config/editor/load"),
          fetch("/sources/advisories/latest")
        ]);
        const data = await configResp.json();
        const advisoryData = advisoryResp.ok ? await advisoryResp.json() : { advisories: [] };
        latestSourceAdvisories = Array.isArray(advisoryData.advisories) ? advisoryData.advisories : [];
        current = data;

        renderProfiles(data.domain_profiles || []);

        document.getElementById("schedule_timezone").value = (data.schedule || {}).timezone || "UTC";
        document.getElementById("schedule_enabled").value = String((data.schedule || {}).enabled ?? true);
        document.getElementById("schedule_times").value = toLines((data.schedule || {}).times || ["07:00"]);

        document.getElementById("api_host").value = (data.api || {}).host || "127.0.0.1";
        document.getElementById("api_port").value = String((data.api || {}).port ?? 8080);
        document.getElementById("api_scheduler_enabled").value = String((data.api || {}).scheduler_enabled ?? false);
        document.getElementById("api_auto_run_on_user_ingest").value = String((data.api || {}).auto_run_on_user_ingest ?? true);
        document.getElementById("api_telegram_ingest_enabled").value = String((data.api || {}).telegram_ingest_enabled ?? false);
        document.getElementById("api_telegram_poll_interval").value = String((data.api || {}).telegram_ingest_poll_interval_seconds ?? 2.0);
        document.getElementById("brief_language").value = (data.briefing || {}).language || "zh";

        document.getElementById("llm_base_url").value = (data.llm || {}).base_url || "";
        document.getElementById("llm_model").value = (data.llm || {}).model || "";
        document.getElementById("llm_dedup_model").value = (data.llm || {}).dedup_model || "";
        document.getElementById("llm_temperature").value = (data.llm || {}).temperature ?? 0.1;
        document.getElementById("embedding_base_url").value = (data.llm || {}).embedding_base_url || "";
        document.getElementById("embedding_model").value = (data.llm || {}).embedding_model || "";

        document.getElementById("tts_enabled").value = String((data.tts || {}).enabled ?? false);
        document.getElementById("tts_provider").value = (data.tts || {}).provider || "gtts";
        document.getElementById("tts_model").value = (data.tts || {}).model || "";
        document.getElementById("tts_voice").value = (data.tts || {}).voice || "";
        document.getElementById("tts_language").value = (data.tts || {}).language || "zh-CN";
        document.getElementById("tts_base_url").value = (data.tts || {}).base_url || "";
        const channels = Array.isArray((data.notifications || {}).channels)
          ? (data.notifications || {}).channels
          : (((data.notifications || {}).channel && (data.notifications || {}).channel !== "none") ? [(data.notifications || {}).channel] : []);
        document.getElementById("notify_channel_telegram").checked = channels.includes("telegram");
        document.getElementById("notify_channel_dingtalk").checked = channels.includes("dingtalk");
        document.getElementById("dingtalk_ingest_enabled").value = String(((data.notifications || {}).dingtalk || {}).ingest_enabled ?? false);
        document.getElementById("pw_channel").value = (data.playwright || {}).channel || "";
        document.getElementById("pw_headless").value = String((data.playwright || {}).headless ?? true);
        document.getElementById("pw_extensions").value = toLines((data.playwright || {}).extension_paths || []);
        document.getElementById("pw_args").value = toLines((data.playwright || {}).launch_args || []);

        document.getElementById("openai_api_key").value = "";
        document.getElementById("embedding_api_key").value = "";
        document.getElementById("telegram_bot_token").value = "";
        document.getElementById("telegram_chat_id").value = "";
        document.getElementById("dingtalk_app_key").value = "";
        document.getElementById("dingtalk_app_secret").value = "";
        document.getElementById("dingtalk_webhook").value = "";
        document.getElementById("dingtalk_secret").value = "";
        document.getElementById("tts_api_key").value = "";
        resetSecretClearControls();

        renderFieldGuide(data.field_guide || []);

        const src = data.resolved_sources || {};
        const advisoryCount = latestSourceAdvisories.length;
        setStatus(t("loaded", {
          rss: src.rss_count ?? 0,
          web: src.playwright_count ?? 0,
          advisories: advisoryCount ? `, advisories=${advisoryCount}` : ""
        }));
      } catch (err) {
        setStatus(t("loadFailed", { message: err.message }), true);
      }
    }

    async function saveConfig() {
      try {
        const profiles = collectProfiles();
        if (!profiles.length) {
          throw new Error(t("missingTopic"));
        }

        const selectedChannels = [
          document.getElementById("notify_channel_telegram").checked ? "telegram" : null,
          document.getElementById("notify_channel_dingtalk").checked ? "dingtalk" : null,
        ].filter(Boolean);

        const payload = {
          domain_profiles: profiles,
          schedule: {
            timezone: document.getElementById("schedule_timezone").value.trim() || "UTC",
            enabled: document.getElementById("schedule_enabled").value === "true",
            times: (() => {
              const values = fromLines(document.getElementById("schedule_times").value || "07:00");
              return values.length ? values : ["07:00"];
            })()
          },
          api: {
            host: document.getElementById("api_host").value.trim() || "127.0.0.1",
            port: Number(document.getElementById("api_port").value || "8080") || 8080,
            scheduler_enabled: document.getElementById("api_scheduler_enabled").value === "true",
            auto_run_on_user_ingest: document.getElementById("api_auto_run_on_user_ingest").value === "true",
            telegram_ingest_enabled: document.getElementById("api_telegram_ingest_enabled").value === "true",
            telegram_ingest_poll_interval_seconds: Number(document.getElementById("api_telegram_poll_interval").value || "2.0")
          },
          briefing: {
            language: document.getElementById("brief_language").value || "zh"
          },
          llm: {
            provider: "openai",
            model: document.getElementById("llm_model").value.trim(),
            dedup_model: document.getElementById("llm_dedup_model").value.trim() || null,
            embedding_model: document.getElementById("embedding_model").value.trim(),
            embedding_base_url: document.getElementById("embedding_base_url").value.trim() || null,
            base_url: document.getElementById("llm_base_url").value.trim() || null,
            temperature: Number(document.getElementById("llm_temperature").value || "0.1"),
            dedup_temperature: (current?.llm?.dedup_temperature ?? 0.0),
            max_input_items: (current?.llm?.max_input_items ?? 40)
          },
          playwright: {
            headless: document.getElementById("pw_headless").value === "true",
            channel: document.getElementById("pw_channel").value.trim() || null,
            extension_paths: fromLines(document.getElementById("pw_extensions").value),
            launch_args: fromLines(document.getElementById("pw_args").value)
          },
          tts: {
            enabled: document.getElementById("tts_enabled").value === "true",
            provider: document.getElementById("tts_provider").value,
            model: document.getElementById("tts_model").value.trim() || "gpt-4o-mini-tts",
            voice: document.getElementById("tts_voice").value.trim() || "alloy",
            language: document.getElementById("tts_language").value.trim() || "zh-CN",
            base_url: document.getElementById("tts_base_url").value.trim() || null
          },
          notifications: {
            channel: selectedChannels[0] || "none",
            channels: selectedChannels,
            telegram: {
              enabled: selectedChannels.includes("telegram")
            },
            dingtalk: {
              enabled: selectedChannels.includes("dingtalk"),
              ingest_enabled: document.getElementById("dingtalk_ingest_enabled").value === "true"
            }
          },
          secrets: {
            openai_api_key: document.getElementById("openai_api_key").value.trim() || null,
            embedding_api_key: document.getElementById("embedding_api_key").value.trim() || null,
            telegram_bot_token: document.getElementById("telegram_bot_token").value.trim() || null,
            telegram_chat_id: document.getElementById("telegram_chat_id").value.trim() || null,
            dingtalk_app_key: document.getElementById("dingtalk_app_key").value.trim() || null,
            dingtalk_app_secret: document.getElementById("dingtalk_app_secret").value.trim() || null,
            dingtalk_webhook: document.getElementById("dingtalk_webhook").value.trim() || null,
            dingtalk_secret: document.getElementById("dingtalk_secret").value.trim() || null,
            tts_api_key: document.getElementById("tts_api_key").value.trim() || null
          },
          clear_secrets: Array.from(document.querySelectorAll("[data-clear-key]:checked")).map((el) => el.dataset.clearKey)
        };

        setStatus("保存中...");
        const resp = await fetch("/config/editor/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });

        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data.detail || "保存失败");
        }

        current = data;
        let saveMessage = t("saveSuccess");
        if (data.restart_required) {
          const reasons = (data.restart_reasons || []).join("、");
          saveMessage = t("saveRestart", { reasons });
        }
        await loadConfig();
        setStatus(saveMessage);
      } catch (err) {
        setStatus(t("saveFailed", { message: err.message }), true);
      }
    }

    decorateSecretInputs();
    applyUiLanguage();
    loadConfig();
  </script>
</body>
</html>
"""
