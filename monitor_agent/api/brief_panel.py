from __future__ import annotations


BRIEF_PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Monitor Brief Viewer</title>
  <style>
    :root {
      --bg: #f6f7fb;
      --card: #ffffff;
      --ink: #182033;
      --muted: #5f6c86;
      --line: #d7deea;
      --accent: #155eef;
      --ok: #067647;
    }
    body {
      margin: 0;
      font-family: "SF Pro Text","Avenir Next","PingFang SC","Segoe UI",sans-serif;
      background: radial-gradient(circle at 0% 0%, #e7eeff 0, var(--bg) 38%) fixed;
      color: var(--ink);
      padding: 20px;
    }
    .wrap { max-width: 1440px; margin: 0 auto; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
    }
    .title h1 { margin: 0 0 4px; font-size: 24px; }
    .title .hint { color: var(--muted); font-size: 13px; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; }
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
    button, a.button {
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
      font-weight: 600;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }
    button.secondary, a.button.secondary { background: #344054; }
    .layout {
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 6px 20px rgba(21, 94, 239, 0.07);
    }
    .sidebar {
      position: sticky;
      top: 18px;
      max-height: calc(100vh - 36px);
      overflow: auto;
    }
    .status { font-size: 13px; color: var(--muted); min-height: 18px; margin-top: 8px; }
    .meta-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .meta-box {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      background: #fbfcff;
    }
    .meta-box .label { color: var(--muted); font-size: 12px; margin-bottom: 4px; }
    .meta-box .value {
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .history-list {
      margin-top: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .history-item {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      background: #fff;
      cursor: pointer;
    }
    .history-item.active {
      border-color: var(--accent);
      box-shadow: inset 0 0 0 1px rgba(21, 94, 239, 0.18);
      background: #f8faff;
    }
    .history-item .row1 {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 4px;
      font-size: 13px;
      font-weight: 700;
    }
    .history-item .row2, .history-item .row3 {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .brief-header {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .brief-meta {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }
    .brief-links {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .brief-links a {
      color: var(--accent);
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
    }
    .brief-links a:hover { text-decoration: underline; }
    .brief-reader {
      border-top: 1px solid var(--line);
      padding-top: 16px;
      min-height: 280px;
      line-height: 1.8;
    }
    .brief-reader h1, .brief-reader h2, .brief-reader h3 { line-height: 1.4; margin-top: 1.2em; }
    .brief-reader h1 { font-size: 28px; margin-top: 0; }
    .brief-reader h2 { font-size: 20px; }
    .brief-reader h3 { font-size: 17px; }
    .brief-reader p { margin: 0.5em 0; }
    .brief-reader ul { margin: 0.5em 0 0.8em 1.2em; padding: 0; }
    .brief-reader li { margin: 0.25em 0; }
    .advisory-panel {
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fbfcff;
      overflow: hidden;
    }
    .advisory-panel summary {
      cursor: pointer;
      padding: 10px 12px;
      font-size: 13px;
      font-weight: 700;
      color: var(--ink);
      background: #fff;
    }
    .advisory-panel-body { padding: 10px 12px 12px; }
    .advisory-mini {
      border-top: 1px dashed var(--line);
      padding-top: 8px;
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.5;
    }
    .advisory-mini:first-child {
      border-top: 0;
      padding-top: 0;
      margin-top: 0;
    }
    .advisory-badge {
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 700;
      margin-right: 6px;
    }
    .advisory-badge.warning { background: #fff7e6; color: #b54708; }
    .advisory-badge.error { background: #fef3f2; color: #b42318; }
    .muted { color: var(--muted); }
    @media (max-width: 1024px) {
      .layout { grid-template-columns: 1fr; }
      .sidebar { position: static; max-height: none; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">
        <h1 id="page_title">Brief</h1>
        <div id="page_hint" class="hint">阅读最近简报与历史简报。音频播放界面暂不在这一版实现。</div>
      </div>
      <div class="actions">
        <div class="lang-switch" aria-label="UI language switch">
          <button id="lang_zh" type="button" class="secondary" onclick="setUiLang('zh')">中文</button>
          <button id="lang_en" type="button" class="secondary" onclick="setUiLang('en')">EN</button>
        </div>
        <a id="back_to_config" class="button secondary" href="/config/ui">返回 Config</a>
        <button id="refresh_button" onclick="loadBriefPage()">刷新</button>
      </div>
    </div>

    <div class="layout">
      <aside class="sidebar">
        <div class="card">
          <h2 id="latest_overview_title">最新概览</h2>
          <div id="latest_meta" class="meta-grid"></div>
          <div id="advisory_panel_mount"></div>
          <div id="brief_status" class="status"></div>
        </div>
        <div class="card" style="margin-top: 12px;">
          <h2 id="history_title">历史简报</h2>
          <div id="history_hint" class="muted" style="font-size:13px;">最近 20 条运行结果。</div>
          <div id="history_list" class="history-list"></div>
        </div>
      </aside>

      <main>
        <div class="card">
          <div class="brief-header">
            <div>
              <h2 id="brief_title">最新简报</h2>
              <div id="brief_meta" class="brief-meta"></div>
              <div id="brief_links" class="brief-links"></div>
            </div>
          </div>
          <div id="brief_reader" class="brief-reader muted">加载中…</div>
        </div>
      </main>
    </div>
  </div>

  <script>
    let briefHistory = [];
    let selectedRunId = null;
    let latestAdvisories = [];
    let uiLang = localStorage.getItem("monitor_ui_lang") || "zh";

    const I18N = {
      zh: {
        pageTitle: "Brief",
        pageHint: "阅读最近简报与历史简报。音频播放界面暂不在这一版实现。",
        backToConfig: "返回 Config",
        refresh: "刷新",
        latestOverview: "最新概览",
        historyTitle: "历史简报",
        historyHint: "最近 20 条运行结果。",
        domain: "Domain",
        signals: "Signals",
        generated: "Generated",
        audio: "Audio",
        audioYes: "已生成",
        audioNo: "未生成",
        noBrief: "暂无简报",
        noHistory: "暂无历史简报",
        advisorySummary: "来源诊断建议（{count}）",
        severityWarning: "提示",
        severityError: "异常",
        rowSignalAudio: "signals={signals} | audio={audio}",
        audioYesShort: "yes",
        audioNoShort: "no",
        latestBrief: "最新简报",
        runId: "Run ID",
        openMarkdown: "打开 Markdown 文件",
        openSignals: "打开 Signals JSON",
        openBriefJson: "打开 Brief JSON",
        audioUiNotPlayed: "已生成（UI 暂未播放）",
        loaded: "已加载 {runId}",
        loading: "加载中...",
        loadFailed: "加载失败: {message}",
        noReadableBrief: "暂无可阅读的简报。",
        noBriefStatus: "暂无简报",
      },
      en: {
        pageTitle: "Brief",
        pageHint: "Read the latest and historical briefings. Audio playback is not included in this version.",
        backToConfig: "Back to Config",
        refresh: "Refresh",
        latestOverview: "Latest Overview",
        historyTitle: "Brief History",
        historyHint: "Latest 20 runs.",
        domain: "Domain",
        signals: "Signals",
        generated: "Generated",
        audio: "Audio",
        audioYes: "Available",
        audioNo: "Not generated",
        noBrief: "No briefing yet",
        noHistory: "No briefing history yet",
        advisorySummary: "Source advisories ({count})",
        severityWarning: "Notice",
        severityError: "Error",
        rowSignalAudio: "signals={signals} | audio={audio}",
        audioYesShort: "yes",
        audioNoShort: "no",
        latestBrief: "Latest Brief",
        runId: "Run ID",
        openMarkdown: "Open Markdown",
        openSignals: "Open Signals JSON",
        openBriefJson: "Open Brief JSON",
        audioUiNotPlayed: "Available (player not enabled in UI)",
        loaded: "Loaded {runId}",
        loading: "Loading...",
        loadFailed: "Load failed: {message}",
        noReadableBrief: "No readable brief is available.",
        noBriefStatus: "No briefs yet",
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

    function applyUiLanguage() {
      document.documentElement.lang = uiLang === "en" ? "en" : "zh-CN";
      document.getElementById("page_title").textContent = t("pageTitle");
      document.getElementById("page_hint").textContent = t("pageHint");
      document.getElementById("back_to_config").textContent = t("backToConfig");
      document.getElementById("refresh_button").textContent = t("refresh");
      document.getElementById("latest_overview_title").textContent = t("latestOverview");
      document.getElementById("history_title").textContent = t("historyTitle");
      document.getElementById("history_hint").textContent = t("historyHint");
      document.getElementById("lang_zh").classList.toggle("active", uiLang === "zh");
      document.getElementById("lang_en").classList.toggle("active", uiLang === "en");
    }

    function setUiLang(next) {
      uiLang = next === "en" ? "en" : "zh";
      localStorage.setItem("monitor_ui_lang", uiLang);
      applyUiLanguage();
      renderLatestMeta(briefHistory[0] || null);
      renderAdvisoryPanel(latestAdvisories);
      renderHistory(briefHistory);
      const current = briefHistory.find((item) => item.run_id === selectedRunId) || briefHistory[0] || null;
      if (current) {
        renderBriefDetail(current);
      }
    }

    function setBriefStatus(message, isError = false) {
      const el = document.getElementById("brief_status");
      el.style.color = isError ? "#b42318" : "#5f6c86";
      el.textContent = message;
    }

    function escapeHtml(str) {
      return String(str || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function renderInline(text) {
      let out = escapeHtml(text);
      out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
      out = out.replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>");
      out = out.replace(/_([^_]+)_/g, "<em>$1</em>");
      out = out.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^)]+)\\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
      return out;
    }

    function markdownToHtml(markdown) {
      const lines = String(markdown || "").split(/\\r?\\n/);
      const html = [];
      let inList = false;
      for (const rawLine of lines) {
        const line = rawLine.trimEnd();
        const trimmed = line.trim();
        if (!trimmed) {
          if (inList) {
            html.push("</ul>");
            inList = false;
          }
          continue;
        }
        if (trimmed.startsWith("# ")) {
          if (inList) { html.push("</ul>"); inList = false; }
          html.push(`<h1>${renderInline(trimmed.slice(2))}</h1>`);
          continue;
        }
        if (trimmed.startsWith("## ")) {
          if (inList) { html.push("</ul>"); inList = false; }
          html.push(`<h2>${renderInline(trimmed.slice(3))}</h2>`);
          continue;
        }
        if (trimmed.startsWith("### ")) {
          if (inList) { html.push("</ul>"); inList = false; }
          html.push(`<h3>${renderInline(trimmed.slice(4))}</h3>`);
          continue;
        }
        if (trimmed.startsWith("- ")) {
          if (!inList) {
            html.push("<ul>");
            inList = true;
          }
          html.push(`<li>${renderInline(trimmed.slice(2))}</li>`);
          continue;
        }
        if (inList) {
          html.push("</ul>");
          inList = false;
        }
        html.push(`<p>${renderInline(trimmed)}</p>`);
      }
      if (inList) html.push("</ul>");
      return html.join("\\n");
    }

    function renderLatestMeta(item) {
      const root = document.getElementById("latest_meta");
      if (!item) {
        root.innerHTML = `<div class="muted">${escapeHtml(t("noBrief"))}</div>`;
        return;
      }
      root.innerHTML = [
        metaBox(t("domain"), item.domain || "-"),
        metaBox(t("signals"), String(item.signal_count ?? "-")),
        metaBox(t("generated"), item.generated_at || "-"),
        metaBox(t("audio"), item.audio_available ? t("audioYes") : t("audioNo")),
      ].join("");
    }

    function renderAdvisoryPanel(items) {
      const root = document.getElementById("advisory_panel_mount");
      const rows = Array.isArray(items) ? items.slice(0, 4) : [];
      if (!rows.length) {
        root.innerHTML = "";
        return;
      }
      root.innerHTML = `
        <details class="advisory-panel">
          <summary>${escapeHtml(t("advisorySummary", { count: rows.length }))}</summary>
          <div class="advisory-panel-body">
            ${rows.map((row) => `
              <div class="advisory-mini">
                <div><span class="advisory-badge ${String(row.severity || "warning").toLowerCase() === "error" ? "error" : "warning"}">${escapeHtml(String(row.severity || "warning").toLowerCase() === "error" ? t("severityError") : t("severityWarning"))}</span><strong>${escapeHtml(row.source_name || row.source_url || "")}</strong></div>
                <div>${escapeHtml(row.summary || "")}</div>
              </div>
            `).join("")}
          </div>
        </details>
      `;
    }

    function metaBox(label, value) {
      return `<div class="meta-box"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div></div>`;
    }

    function renderHistory(items) {
      const root = document.getElementById("history_list");
      if (!items.length) {
        root.innerHTML = `<div class="muted">${escapeHtml(t("noHistory"))}</div>`;
        return;
      }
      root.innerHTML = items.map((item) => {
        const active = item.run_id === selectedRunId ? "active" : "";
        return `
          <div class="history-item ${active}" onclick="openHistoryItem('${escapeHtml(item.run_id)}')">
            <div class="row1">
              <span>${escapeHtml(item.domain || "Brief")}</span>
              <span>${escapeHtml(item.slot || "")}</span>
            </div>
            <div class="row2">${escapeHtml(item.generated_at || "")}</div>
            <div class="row3">${escapeHtml(t("rowSignalAudio", { signals: String(item.signal_count ?? 0), audio: item.audio_available ? t("audioYesShort") : t("audioNoShort") }))}</div>
          </div>
        `;
      }).join("");
    }

    function renderBriefDetail(item) {
      document.getElementById("brief_title").textContent = item.domain || t("latestBrief");
      document.getElementById("brief_meta").innerHTML = [
        `${escapeHtml(t("runId"))}: ${escapeHtml(item.run_id || "-")}`,
        `${escapeHtml(t("generated"))}: ${escapeHtml(item.generated_at || "-")}`,
        `${escapeHtml(t("signals"))}: ${escapeHtml(String(item.signal_count ?? 0))}`,
        `${escapeHtml(t("audio"))}: ${item.audio_available ? escapeHtml(t("audioUiNotPlayed")) : escapeHtml(t("audioNo"))}`,
      ].join("<br/>");

      const links = [];
      if (item.brief_md_path) links.push(`<a href="${escapeHtml(item.brief_md_path)}" target="_blank" rel="noreferrer">${escapeHtml(t("openMarkdown"))}</a>`);
      if (item.signals_json_path) links.push(`<a href="${escapeHtml(item.signals_json_path)}" target="_blank" rel="noreferrer">${escapeHtml(t("openSignals"))}</a>`);
      if (item.brief_json_path) links.push(`<a href="${escapeHtml(item.brief_json_path)}" target="_blank" rel="noreferrer">${escapeHtml(t("openBriefJson"))}</a>`);
      document.getElementById("brief_links").innerHTML = links.join("");
      document.getElementById("brief_reader").innerHTML = markdownToHtml(item.brief_text || "");
    }

    async function openHistoryItem(runId) {
      try {
        selectedRunId = runId;
        renderHistory(briefHistory);
        const resp = await fetch(`/brief/history/${encodeURIComponent(runId)}`);
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data.detail || "历史简报加载失败");
        }
        renderBriefDetail(data);
        setBriefStatus(t("loaded", { runId }));
      } catch (err) {
        setBriefStatus(t("loadFailed", { message: err.message }), true);
      }
    }

    async function loadBriefPage() {
      try {
        setBriefStatus(t("loading"));
        const [historyResp, advisoryResp] = await Promise.all([
          fetch("/brief/history?limit=20"),
          fetch("/sources/advisories/latest")
        ]);
        const data = await historyResp.json();
        const advisoryData = advisoryResp.ok ? await advisoryResp.json() : { advisories: [] };
        if (!historyResp.ok) {
          throw new Error(data.detail || "历史简报加载失败");
        }
        latestAdvisories = Array.isArray(advisoryData.advisories) ? advisoryData.advisories : [];
        briefHistory = Array.isArray(data.items) ? data.items : [];
        const latest = briefHistory[0] || null;
        selectedRunId = latest ? latest.run_id : null;
        renderLatestMeta(latest);
        renderAdvisoryPanel(latestAdvisories);
        renderHistory(briefHistory);
        if (latest) {
          await openHistoryItem(latest.run_id);
        } else {
          document.getElementById("brief_reader").textContent = t("noReadableBrief");
          setBriefStatus(t("noBriefStatus"));
        }
      } catch (err) {
        setBriefStatus(t("loadFailed", { message: err.message }), true);
      }
    }

    applyUiLanguage();
    loadBriefPage();
  </script>
</body>
</html>
"""
