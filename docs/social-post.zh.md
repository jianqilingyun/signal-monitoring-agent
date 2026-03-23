# 社交媒体宣传文案（简版）

我开源了一个本地优先的 Signal Monitoring Agent。

它的目标很直接：
- 你定义一个关注主题
- 挂上 RSS / 网页来源
- 系统自动抓取、去重、筛选、总结
- 最后通过 Telegram / API 等方式把简报发给你

当前已经支持：
- RSS + 网页抓取（HTML parser / Playwright）
- 时间感知的新旧过滤
- embedding + LLM 的事件级去重
- 用户收件箱输入（Telegram inbound）
- 配置 UI + Brief 阅读 UI
- REST API + tool-ready schema，可接到外部 agent workflow
- 本地单机运行与持久化存储

我比较看重的是：
- 本地可控
- 模块化
- 可解释
- 不是把一堆“抓来的噪音”直接塞给你

如果你也在做：
- AI / infra / OSS / 行业情报监控
- 主题化 briefing
- OpenClaw / agent workflow 集成
- 本地 agent workflow

欢迎交流。
