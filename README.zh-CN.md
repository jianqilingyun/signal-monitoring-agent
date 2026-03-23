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

## 主要能力

- 按主题组织的监控桶
- RSS 抓取 + 二跳正文抓取
- 网页抓取（HTML parser 优先，Playwright 回退）
- 时间感知的 freshness 过滤（fresh / recent / stale / unknown）
- embedding + LLM 的事件级去重
- 用户输入收件箱（Telegram inbound / API ingest）
- Markdown 简报、结构化 JSON、可选音频
- Config UI 与 Brief UI
- 本地单机运行与持久化存储

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

本地界面：
- Config UI: [http://127.0.0.1:8080/config/ui](http://127.0.0.1:8080/config/ui)
- Brief UI: [http://127.0.0.1:8080/brief/ui](http://127.0.0.1:8080/brief/ui)

## 架构

```text
monitor_agent/
  api/
  briefing/
  core/
  ingestion_layer/
  inbound/
  notifier/
  signal_engine/
  strategy_engine/
  tts/
  filter_engine/
```

## 输入 / 输出

输入：
- RSS
- 网页
- Telegram / API 用户提交的链接与文本

输出：
- Markdown 简报
- Signals JSON
- Telegram / 钉钉通知
- 可选 MP3 音频

## 运行模式

```bash
scripts/run_local.sh preflight
scripts/run_local.sh once
scripts/run_local.sh api
scripts/run_local.sh scheduled
scripts/run_local.sh pw-login --url https://example.com/login
```

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
