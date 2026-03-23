# Signal Monitoring Agent

[English](./README.md)

Signal Monitoring Agent 是一个面向主题监控与情报整理的模块化系统。

你只需要定义：
- 关注主题
- 信息来源
- 推送渠道

系统会负责：
- 抓取 RSS 和网页内容
- 用 LLM 提取结构化信号
- 做事件级去重与更新识别
- 排序、筛选、生成简报
- 通过 Telegram、钉钉或 API 输出结果

它首先面向本地单机运行，同时保留了后续接入外部 agent 系统的扩展空间。

它既可以独立运行，也可以通过 REST API 和 tool-ready schema 作为外部 agent 系统的监控后端。

## 主要能力

- 按主题组织的监控桶
- RSS 抓取 + 二跳正文抓取
- 网页抓取（HTML parser 优先，Playwright 回退）
- 时间感知的 freshness 过滤（fresh / recent / stale / unknown）
- embedding + LLM 的事件级去重
- 用户输入收件箱（Telegram inbound / API ingest）
- Markdown 简报、结构化 JSON、可选音频
- Config UI 与 Brief UI
- REST API，可用于 ingest、run control、strategy workflow
- 面向 agent 集成的 tool-ready schema
- 本地单机运行与持久化存储

## 接口

- `GET /config/ui`
- `GET /brief/ui`
- `GET /signals/latest`
- `GET /brief/latest`
- `POST /ingest`
- `POST /run_now`
- `POST /strategy/generate`
- `POST /strategy/preview`
- `POST /strategy/source/suggest`
- `POST /strategy/deploy`
- `POST /strategy/patch`
- `POST /strategy/get`
- `POST /strategy/history`
- `GET /sources/advisories/latest`
- `GET /tool/schema`

可用于：
- 独立本地运行
- 作为 OpenClaw 一类外部 agent 系统的监控能力层

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env
cp config/config.example.yaml config/config.local.yaml
scripts/run_local.sh preflight
scripts/run_local.sh api
```

编辑：
- `.env`
- `config/config.local.yaml`

本地界面：
- Config UI: [http://127.0.0.1:8080/config/ui](http://127.0.0.1:8080/config/ui)
- Brief UI: [http://127.0.0.1:8080/brief/ui](http://127.0.0.1:8080/brief/ui)

## 运行模式

```bash
scripts/run_local.sh preflight
scripts/run_local.sh once
scripts/run_local.sh api
scripts/run_local.sh scheduled
scripts/run_local.sh pw-login --url https://example.com/login
```

说明：
- `preflight`：启动前检查
- `once`：立即跑一轮
- `api`：启动本地 API 与 UI
- `scheduled`：跑本地定时调度
- `pw-login`：打开浏览器手动登录一次

## 输出

- `data/briefs/`
- `data/signals/`
- `data/audio/`（启用 TTS 时）
- `data/runs/`
- `data/summaries/`
- `data/events/`
- `data/source_cursors.json`

关键运行特性：
- source 级增量抓取，减少重复抓取
- 小 overlap 窗口，尽量避免漏掉刚发布内容
- source 刷新频率由后端自动调节
- source advisory 只做建议，不会自动删源

## 安全与隐私

默认安全策略：
- API 默认只监听 `127.0.0.1`
- 远程访问需要 `MONITOR_API_TOKEN`
- Telegram / DingTalk 入站有 allowlist
- 用户提交的 URL 会阻止访问私网/本机地址
- source advisory 只做建议，不会自动删除你的来源

不要提交：
- `.env`
- `MONITOR_API_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_ALLOWED_CHAT_IDS`
- `DINGTALK_*`
- `config/config.local.yaml`
- `data/`

更多说明：
- [SECURITY.md](./SECURITY.md)

## 开发

```bash
python -m unittest discover -q
scripts/open_source_audit.sh
```

更多内容见：
- [SECURITY.md](./SECURITY.md)
- [CONTRIBUTING.md](./CONTRIBUTING.md)
