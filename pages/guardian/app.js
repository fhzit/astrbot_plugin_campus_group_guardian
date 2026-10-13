// 校园群聊卫士 — 插件页面脚本
// 全部请求都走 window.AstrBotPluginPage bridge（§110、§169、§170），
// 不直接读取 Dashboard cookie/localStorage，也不自建端口。

const bridge = window.AstrBotPluginPage;

const INCIDENT_STATUS_LABELS = {
  open: "待复核",
  confirmed_fraud: "已确认诈骗",
  false_positive: "误判",
  resolved: "已处理",
};

const ACTION_STATUS_LABELS = {
  success: "成功",
  failed: "失败",
  unsupported: "平台不支持",
  skipped: "已跳过",
};

let currentContext = null;

function el(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function toast(message, isError = false) {
  const node = el("gg-toast");
  node.textContent = message;
  node.classList.toggle("gg-toast--error", isError);
  node.classList.remove("is-hidden");
  window.setTimeout(() => node.classList.add("is-hidden"), 3200);
}

function applyTheme(context) {
  const dark = Boolean(context?.isDark);
  document.body.dataset.theme = dark ? "dark" : "light";
}

function errorMessage(error) {
  return error && error.message ? error.message : String(error);
}

// ---------------------------------------------------------------------------
// 总览
// ---------------------------------------------------------------------------
function renderStats(stats) {
  const cards = [
    ["审核消息", stats.moderation_scanned ?? 0],
    ["自动撤回", stats.moderation_violations ?? 0],
    ["反诈事件", stats.incidents_total ?? 0],
    ["待复核", stats.incidents_open ?? 0],
    ["确认诈骗", stats.incidents_confirmed ?? 0],
    ["误报", stats.incidents_false_positive ?? 0],
    ["动作总数", stats.action_total ?? 0],
    ["动作失败", stats.action_failed ?? 0],
    ["平台不支持", stats.action_unsupported ?? 0],
  ];
  el("gg-stats").innerHTML = cards
    .map(
      ([label, value]) =>
        `<div class="gg-card"><div class="gg-card-value">${escapeHtml(value)}</div><div class="gg-card-label">${label}</div></div>`
    )
    .join("");
}

function renderTrend(rows) {
  const node = el("gg-trend");
  if (!rows || !rows.length) {
    node.innerHTML = '<p class="gg-hint">暂无数据</p>';
    return;
  }
  const max = Math.max(...rows.map((row) => Number(row.count ?? 0)), 1);
  node.innerHTML = rows
    .map((row) => {
      const height = Math.max(2, Math.round((Number(row.count ?? 0) / max) * 70));
      return `<div class="gg-trend-day">
        <div class="gg-trend-bar" style="height:${height}px" title="${escapeHtml(row.day)}：${escapeHtml(row.count)}"></div>
        <div>${escapeHtml((row.day || "").slice(5))}</div>
      </div>`;
    })
    .join("");
}

async function loadOverview() {
  try {
    const data = await bridge.apiGet("overview");
    renderStats(data.stats || {});
    renderTrend(data.trend || []);
  } catch (error) {
    toast(`总览加载失败：${errorMessage(error)}`, true);
  }
}

// ---------------------------------------------------------------------------
// 反诈事件
// ---------------------------------------------------------------------------
function renderIncidents(incidents) {
  const node = el("gg-incidents");
  if (!incidents.length) {
    node.innerHTML = '<p class="gg-hint">暂无事件</p>';
    return;
  }
  node.innerHTML = incidents
    .map((incident) => {
      const level = escapeHtml(incident.risk_level || "safe");
      const signals = (incident.signals || [])
        .map((signal) => `<li>${escapeHtml(signal.label || signal.type)}${signal.evidence ? `：${escapeHtml(signal.evidence)}` : ""}</li>`)
        .join("");
      const actions = (incident.actions || [])
        .map((action) => {
          const status = ACTION_STATUS_LABELS[action.status] || action.status;
          return `${escapeHtml(action.action)}（${escapeHtml(status)}）`;
        })
        .join("、");
      return `<div class="gg-item">
        <div class="gg-item-head">
          <strong>风险 ${escapeHtml(Math.round(Number(incident.risk_score ?? 0)))}/100</strong>
          <span class="gg-badge gg-badge--${level}">${level.toUpperCase()}</span>
          <span class="gg-chip">${escapeHtml(INCIDENT_STATUS_LABELS[incident.status] || incident.status)}</span>
        </div>
        <div class="gg-meta">
          平台 ${escapeHtml(incident.platform)}｜群 ${escapeHtml(incident.group_id)}｜
          用户 ${escapeHtml(incident.sender_id)}（${escapeHtml(incident.sender_role)}）｜
          ${escapeHtml(incident.created_at)}
        </div>
        <div class="gg-excerpt">${escapeHtml(incident.message_excerpt)}</div>
        ${signals ? `<ul>${signals}</ul>` : ""}
        <div class="gg-meta">动作：${actions || "—"}</div>
        <div class="gg-toolbar">
          <button class="gg-btn" data-incident="${escapeHtml(incident.id)}" data-status="confirmed_fraud">确认诈骗</button>
          <button class="gg-btn" data-incident="${escapeHtml(incident.id)}" data-status="false_positive">标记误判</button>
          <button class="gg-btn" data-incident="${escapeHtml(incident.id)}" data-status="resolved">标记已处理</button>
        </div>
      </div>`;
    })
    .join("");
}

async function loadIncidents() {
  const status = el("gg-incident-status").value;
  try {
    const query = status ? { status } : {};
    const data = await bridge.apiGet("incidents", { limit: 50, ...query });
    renderIncidents(data.incidents || []);
  } catch (error) {
    toast(`事件加载失败：${errorMessage(error)}`, true);
  }
}

async function updateIncident(id, status) {
  try {
    await bridge.apiPost("incidents/status", { id: Number(id), status });
    toast("已更新事件状态");
    await Promise.all([loadIncidents(), loadOverview()]);
  } catch (error) {
    toast(`更新失败：${errorMessage(error)}`, true);
  }
}

// ---------------------------------------------------------------------------
// 平台能力
// ---------------------------------------------------------------------------
function renderCapabilities(data) {
  const node = el("gg-capabilities");
  const platforms = data.platforms || [];
  if (!platforms.length) {
    node.innerHTML = '<p class="gg-hint">未加载到平台适配器</p>';
    return;
  }
  node.innerHTML = platforms
    .map((platform) => {
      const rows = (platform.capabilities || [])
        .map(
          (capability) =>
            `<tr><td>${escapeHtml(capability.label)}</td><td class="${capability.supported ? "gg-yes" : "gg-no"}">${capability.supported ? "✓" : "— 当前平台不支持"}</td></tr>`
        )
        .join("");
      const limits = Object.entries(platform.limits || {})
        .filter(([, value]) => value !== null && value !== undefined)
        .map(([key, value]) => `${escapeHtml(key)}=${escapeHtml(value)}`)
        .join("，");
      const notes = (platform.notes || []).map((note) => `<li>${escapeHtml(note)}</li>`).join("");
      return `<div class="gg-item">
        <div class="gg-item-head"><strong>${escapeHtml(platform.name)}</strong></div>
        ${limits ? `<div class="gg-meta">限制：${limits}</div>` : ""}
        <table class="gg-table"><tbody>${rows}</tbody></table>
        ${notes ? `<ul class="gg-meta">${notes}</ul>` : ""}
      </div>`;
    })
    .join("");
}

async function loadCapabilities() {
  try {
    const data = await bridge.apiGet("capabilities");
    renderCapabilities(data);
  } catch (error) {
    toast(`能力矩阵加载失败：${errorMessage(error)}`, true);
  }
}

// ---------------------------------------------------------------------------
// 测试器
// ---------------------------------------------------------------------------
function renderTestResult(data) {
  const signals = (data.signals || [])
    .map((signal) => `  · ${signal.label || signal.type}${signal.evidence ? `（${signal.evidence}）` : ""}`)
    .join("\n");
  const planned = (data.planned_actions || data.planned || []).map((item) => `  · ${item}`).join("\n");
  el("gg-test-result").textContent = [
    `风险分：${data.score}`,
    `等级：${String(data.level || "").toUpperCase()}`,
    data.bypassed ? `已放行（${data.bypass_reason}）` : "未放行",
    "风险信号：",
    signals || "  （无）",
    "预计动作（不会执行）：",
    planned || "  （无）",
    "说明：测试器只运行检测器，不会撤回/禁言/踢人/拉黑。",
  ].join("\n");
}

async function runTest() {
  const text = el("gg-test-text").value.trim();
  if (!text) {
    toast("请输入待检测文本", true);
    return;
  }
  const button = el("gg-test-run");
  button.disabled = true;
  try {
    const data = await bridge.apiPost("test", {
      text,
      role: el("gg-test-role").value,
      sender_name: el("gg-test-name").value || null,
      group_key: el("gg-test-group").value.trim() || "global",
    });
    renderTestResult(data);
  } catch (error) {
    toast(`检测失败：${errorMessage(error)}`, true);
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 诊断与迁移
// ---------------------------------------------------------------------------
function renderDiagnostics(data) {
  const node = el("gg-diagnostics");
  const legacy = data.legacy || {};
  const migration = data.migration || {};
  const lexicon = data.lexicon || {};
  const qq = data.qq_official || {};
  const lines = [
    `数据库 schema 版本：${escapeHtml(migration.schema_version ?? "-")}`,
    `词库：关键词 ${escapeHtml(lexicon.keywords ?? 0)} 条、正则 ${escapeHtml(lexicon.regex_rules ?? 0)} 条（${escapeHtml(lexicon.source || "未加载")}）`,
    `旧版数据目录：${escapeHtml(legacy.legacy_dir || "未检测到")}`,
    `旧版数据库：${legacy.database ? "✓ 已找到" : "— 未找到"}`,
    `旧版词库：${legacy.lexicon ? "✓ 已找到" : "— 未找到"}`,
    `QQ 官方全量消息：${escapeHtml(qq.level || "n/a")} — ${escapeHtml(qq.hint || "")}`,
  ];
  node.innerHTML = `<div class="gg-item"><div class="gg-prose">${lines.join("<br>")}</div></div>`;
}

async function loadDiagnostics() {
  try {
    const data = await bridge.apiGet("diagnostics");
    renderDiagnostics(data);
  } catch (error) {
    toast(`诊断加载失败：${errorMessage(error)}`, true);
  }
}

async function requestLegacyMigration(confirm) {
  try {
    const data = await bridge.apiPost("migrate/legacy", { confirm });
    if (data && data.need_confirm) {
      el("gg-migrate-result").textContent =
        "已检测到旧版 GroupGuardian 数据，点击「备份并迁移」会先自动备份当前数据库再导入日志。";
      return;
    }
    el("gg-migrate-result").textContent = `迁移完成：导入 ${data.imported ?? 0} 条记录，备份文件：${data.backup ?? "（无）"}`;
    toast("迁移完成");
  } catch (error) {
    toast(`迁移失败：${errorMessage(error)}`, true);
  }
}

// ---------------------------------------------------------------------------
// 动作日志
// ---------------------------------------------------------------------------
async function loadLogs() {
  try {
    const limit = Number(el("gg-log-limit").value) || 100;
    const data = await bridge.apiGet("logs", { limit });
    const rows = (data.logs || [])
      .map(
        (row) => `<tr>
          <td>${escapeHtml(row.created_at)}</td>
          <td>${escapeHtml(row.action)}</td>
          <td>${escapeHtml(ACTION_STATUS_LABELS[row.status] || row.status)}</td>
          <td>${escapeHtml(row.group_id)}</td>
          <td>${escapeHtml(row.message_text || row.error || "")}</td>
        </tr>`
      )
      .join("");
    el("gg-logs").innerHTML = `<table class="gg-table">
      <thead><tr><th>时间</th><th>动作</th><th>结果</th><th>群</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  } catch (error) {
    toast(`日志加载失败：${errorMessage(error)}`, true);
  }
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
function bindTabs() {
  const tabs = Array.from(document.querySelectorAll(".gg-tab"));
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((item) => item.classList.toggle("is-active", item === tab));
      document.querySelectorAll(".gg-panel").forEach((panel) => {
        panel.classList.toggle("is-hidden", panel.dataset.panel !== tab.dataset.tab);
      });
      const loaders = {
        overview: loadOverview,
        antifraud: loadIncidents,
        capabilities: loadCapabilities,
        diagnostics: loadDiagnostics,
        logs: loadLogs,
      };
      const loader = loaders[tab.dataset.tab];
      if (loader) {
        loader();
      }
    });
  });
}

function bindActions() {
  el("gg-refresh").addEventListener("click", () => {
    loadOverview();
    loadDiagnostics();
  });
  el("gg-incident-reload").addEventListener("click", loadIncidents);
  el("gg-incidents").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-incident]");
    if (target) {
      updateIncident(target.dataset.incident, target.dataset.status);
    }
  });
  el("gg-test-run").addEventListener("click", runTest);
  el("gg-log-reload").addEventListener("click", loadLogs);
  el("gg-migrate-check").addEventListener("click", () => requestLegacyMigration(false));
  el("gg-migrate-run").addEventListener("click", () => requestLegacyMigration(true));
}

async function main() {
  if (!bridge) {
    document.body.innerHTML = "<p>当前页面必须通过 AstrBot 插件页面（Plugin Page）打开。</p>";
    return;
  }
  currentContext = await bridge.ready();
  applyTheme(currentContext);
  el("gg-locale").textContent = bridge.getLocale();
  document.title = bridge.t("pages.guardian.title", "校园群聊卫士");

  bridge.onContext((context) => {
    currentContext = context;
    applyTheme(context);
  });

  bindTabs();
  bindActions();
  await Promise.all([loadOverview(), loadDiagnostics()]);
}

main().catch((error) => toast(`页面初始化失败：${errorMessage(error)}`, true));
