const $ = (sel) => document.querySelector(sel);

const els = {
  stats: $("#stats"),
  cubes: $("#cubes"),
  cubeTabs: $("#cube-tabs"),
  cubeCount: $("#cube-count"),
  btnCubeLive: $("#btn-cube-live"),
  changes: $("#changes"),
  changesCount: $("#changes-count"),
  changesLive: $("#changes-live"),
  logs: $("#logs"),
  serverTime: $("#server-time"),
  btnRefresh: $("#btn-refresh"),
  btnCheck: $("#btn-check"),
  btnLogin: $("#btn-xueqiu-login"),
  btnLogRefresh: $("#btn-log-refresh"),
  btnLogClear: $("#btn-log-clear"),
  toast: $("#toast"),
  logLive: $("#log-live"),
};

let pollTimer = null;
let logTimer = null;
let checkPollTimer = null;
let lastLogText = "";
let lastLogMtime = null;
let lastStatsHtml = "";
let lastCubesHtml = "";
let lastCubeTabsHtml = "";
let lastCubeCount = "";
let lastChangesHtml = "";
let lastChangesCount = "";
let lastChangeIds = "";
let statusInFlight = false;
let logsInFlight = false;
let liveInFlight = false;
let latestCubes = [];
let latestChanges = [];
let selectedCubeId = localStorage.getItem("selectedCubeId") || "";
let liveCubeCache = {};

const LOG_POLL_MS = 1000;
const STATUS_POLL_MS = 1000;

function formatTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function relativeTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diff = Math.round((Date.now() - d.getTime()) / 1000);
  if (diff < 5) return "刚刚";
  if (diff < 60) return `${diff} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return formatTime(iso);
}

function tokenBadge(token) {
  const map = {
    ok: ["正常", "ok"],
    expiring: ["即将过期", "warn"],
    expired: ["已过期", "danger"],
    unknown: ["未知", "muted"],
  };
  const [text, cls] = map[token?.status] || map.unknown;
  return `<span class="badge ${cls}">${text}</span>`;
}

function showToast(message) {
  els.toast.hidden = false;
  els.toast.textContent = message;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => {
    els.toast.hidden = true;
  }, 2600);
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const detail = err.detail;
    const msg = Array.isArray(detail)
      ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
      : detail || `请求失败 (${res.status})`;
    throw new Error(msg);
  }
  return res.json();
}

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function isNearBottom(el, threshold = 48) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}

function renderStats(data) {
  const checking = data.check?.running;
  const acquired = data.token?.issued_at || data.token?.acquired_at;
  const parts = [];
  if (acquired) parts.push(`获取 ${formatTime(acquired)}`);
  if (data.token?.remaining_hours != null) {
    parts.push(`剩余 ${data.token.remaining_hours} 小时`);
  } else if (data.token?.expiry) {
    parts.push(`过期 ${formatTime(data.token.expiry)}`);
  }
  const tokenHint = parts.length ? parts.join(" · ") : "无法解析获取/过期时间";

  const html = `
    <article class="stat">
      <p class="stat-label">配置状态</p>
      <p class="stat-value">${data.config_ok ? '<span class="badge ok">就绪</span>' : '<span class="badge danger">缺失</span>'}</p>
      <p class="stat-hint">Webhook ${data.webhook_configured ? "已配置" : "未配置"} · Cookie ${escapeHtml(data.cookie_masked || "—")}</p>
    </article>
    <article class="stat">
      <p class="stat-label">调度时段</p>
      <p class="stat-value">${escapeHtml(data.schedule?.label || "—")}</p>
      <p class="stat-hint">下次间隔 ${data.schedule?.interval ?? data.check_interval ?? "—"} 秒 · 盘中 ${data.check_interval || "—"}s</p>
    </article>
    <article class="stat">
      <p class="stat-label">Cookie</p>
      <p class="stat-value">${tokenBadge(data.token)}</p>
      <p class="stat-hint">${tokenHint}</p>
    </article>
    <article class="stat">
      <p class="stat-label">手动检查</p>
      <p class="stat-value">${checking ? '<span class="badge warn">运行中</span>' : '<span class="badge muted">空闲</span>'}</p>
      <p class="stat-hint">${data.check?.last_finished_at ? `上次 ${formatTime(data.check.last_finished_at)}` : "尚未手动触发"}</p>
    </article>
  `;

  if (html !== lastStatsHtml) {
    lastStatsHtml = html;
    els.stats.innerHTML = html;
  }

  els.btnCheck.disabled = Boolean(checking);
  els.btnCheck.textContent = checking ? "检查中…" : "立即检查";
  els.serverTime.textContent = `服务器 ${formatTime(data.server_time)}`;
}

function formatPct(ratio, { unit = "ratio" } = {}) {
  if (ratio == null || Number.isNaN(Number(ratio))) return "—";
  const pct = unit === "percent" ? Number(ratio) : Number(ratio) * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(2)}%`;
}

function gainClass(ratio) {
  const n = Number(ratio);
  if (Number.isNaN(n) || n === 0) return "";
  return n > 0 ? "up" : "down";
}

function ensureSelectedCube(cubes) {
  const ids = (cubes || []).map((c) => c.id);
  if (!ids.length) {
    selectedCubeId = "";
    return;
  }
  if (!selectedCubeId || !ids.includes(selectedCubeId)) {
    selectedCubeId = ids[0];
    localStorage.setItem("selectedCubeId", selectedCubeId);
  }
}

function renderCubeTabs(cubes) {
  if (!els.cubeTabs) return;
  if (!cubes.length) {
    const empty = "";
    if (empty !== lastCubeTabsHtml) {
      lastCubeTabsHtml = empty;
      els.cubeTabs.innerHTML = empty;
    }
    return;
  }
  const html = cubes
    .map((cube) => {
      const active = cube.id === selectedCubeId ? "active" : "";
      const label = cube.name && cube.name !== cube.id ? `${cube.name}` : cube.id;
      return `<button type="button" class="cube-tab ${active}" data-cube-id="${escapeHtml(cube.id)}" role="tab" aria-selected="${cube.id === selectedCubeId}">${escapeHtml(label)} <span class="meta-chip" style="margin-left:6px">${escapeHtml(cube.id)}</span></button>`;
    })
    .join("");
  if (html !== lastCubeTabsHtml) {
    lastCubeTabsHtml = html;
    els.cubeTabs.innerHTML = html;
  }
}

function renderPositions(cube) {
  const positions = cube.positions || [];
  const maxW = Math.max(...positions.map((p) => Number(p.weight) || 0), 1);
  const total = Number(cube.total_weight || 0);
  const cash = Number(cube.cash != null ? cube.cash : Math.max(0, 100 - total));
  if (!positions.length) {
    return `<div class="empty">尚无持仓快照，可点「查询原组合」从雪球拉取，或「立即检查」</div>`;
  }
  return `<div class="positions">${positions
    .map((p) => {
      const w = Number(p.weight) || 0;
      const pct = Math.max(4, (w / maxW) * 100);
      const price = Number(p.price) || 0;
      return `
        <div class="pos">
          <div class="pos-name">
            <strong>${escapeHtml(p.name || p.symbol)}</strong>
            <span>${escapeHtml(p.symbol || "")}${price ? ` · ¥${price.toFixed(2)}` : ""}</span>
          </div>
          <div class="bar"><i style="width:${pct}%"></i></div>
          <div class="pos-weight">${w.toFixed(1)}%</div>
        </div>`;
    })
    .join("")}${
      cash >= 0.1
        ? `<div class="pos cash-row">
            <div class="pos-name"><strong>现金/未分配</strong><span>剩余仓位</span></div>
            <div class="bar"><i class="cash" style="width:${Math.max(4, (cash / maxW) * 100)}%"></i></div>
            <div class="pos-weight">${cash.toFixed(1)}%</div>
          </div>`
        : ""
    }</div>`;
}

function renderQuote(quote) {
  if (!quote) return "";
  const unit = quote.gain_unit === "percent" ? "percent" : "ratio";
  return `
    <div class="cube-quote">
      <div class="quote-item">
        <p class="q-label">单位净值</p>
        <p class="q-value">${quote.net_value != null ? Number(quote.net_value).toFixed(4) : "—"}</p>
      </div>
      <div class="quote-item">
        <p class="q-label">日涨跌</p>
        <p class="q-value ${gainClass(quote.daily_gain)}">${formatPct(quote.daily_gain, { unit })}</p>
      </div>
      <div class="quote-item">
        <p class="q-label">总收益</p>
        <p class="q-value ${gainClass(quote.total_gain)}">${formatPct(quote.total_gain, { unit })}</p>
      </div>
      <div class="quote-item">
        <p class="q-label">年化</p>
        <p class="q-value ${gainClass(quote.annualized_gain_rate)}">${formatPct(quote.annualized_gain_rate, { unit })}</p>
      </div>
    </div>`;
}

function renderCubes(cubes) {
  latestCubes = cubes || [];
  ensureSelectedCube(latestCubes);

  const countText = `${latestCubes.length} 个`;
  if (countText !== lastCubeCount) {
    lastCubeCount = countText;
    els.cubeCount.textContent = countText;
  }

  renderCubeTabs(latestCubes);

  if (els.btnCubeLive) {
    els.btnCubeLive.disabled = !selectedCubeId || liveInFlight;
  }

  if (!latestCubes.length) {
    const empty = `<div class="empty">暂无监控组合，请到<a href="/config">配置页</a>填写监控组合</div>`;
    if (empty !== lastCubesHtml) {
      lastCubesHtml = empty;
      els.cubes.innerHTML = empty;
    }
    return;
  }

  const cube =
    liveCubeCache[selectedCubeId] ||
    latestCubes.find((c) => c.id === selectedCubeId) ||
    latestCubes[0];
  const total = Number(cube.total_weight || 0);
  const cash = Number(cube.cash != null ? cube.cash : Math.max(0, 100 - total));
  const xqUrl = `https://xueqiu.com/P/${encodeURIComponent(cube.id)}`;
  const sourceHint = cube.source === "live"
    ? `雪球实时 · ${formatTime(cube.live_at || cube.quote?.updated_at)}`
    : `本地快照 · 上次检查 ${relativeTime(cube.last_check)}`;

  const html = `
    <article class="cube">
      <div class="cube-head">
        <div>
          <h3 class="cube-title">${escapeHtml(cube.name || cube.id)}</h3>
          <p class="cube-id">
            <a href="${xqUrl}" target="_blank" rel="noopener noreferrer">${escapeHtml(cube.id)}</a>
            · ${cube.position_count || 0} 只 · 持仓 ${total.toFixed(1)}% · 现金 ${cash.toFixed(1)}%
          </p>
        </div>
        <div class="cube-meta">
          <div title="${escapeHtml(formatTime(cube.last_check))}">上次检查 ${relativeTime(cube.last_check)}</div>
          <div>调仓 ID ${cube.last_rb_id ?? "—"}</div>
        </div>
      </div>
      <p class="cube-source">${escapeHtml(sourceHint)}${cube.warning ? ` · ${escapeHtml(cube.warning)}` : ""}</p>
      ${renderQuote(cube.quote)}
      ${renderPositions(cube)}
    </article>`;

  if (html !== lastCubesHtml) {
    lastCubesHtml = html;
    els.cubes.innerHTML = html;
  }
}

function selectCube(cubeId) {
  if (!cubeId || cubeId === selectedCubeId) return;
  selectedCubeId = cubeId;
  localStorage.setItem("selectedCubeId", selectedCubeId);
  lastCubeTabsHtml = "";
  lastCubesHtml = "";
  lastChangesHtml = "";
  lastChangeIds = "";
  renderCubes(latestCubes);
  renderChanges(latestChanges);
}

async function fetchCubeLive() {
  if (!selectedCubeId || liveInFlight) return;
  liveInFlight = true;
  if (els.btnCubeLive) {
    els.btnCubeLive.disabled = true;
    els.btnCubeLive.textContent = "查询中…";
  }
  try {
    const data = await api(`/api/cubes/${encodeURIComponent(selectedCubeId)}/live`);
    liveCubeCache[selectedCubeId] = data;
    const idx = latestCubes.findIndex((c) => c.id === selectedCubeId);
    if (idx >= 0) {
      latestCubes[idx] = { ...latestCubes[idx], ...data };
    }
    lastCubesHtml = "";
    renderCubes(latestCubes);
    showToast(data.warning || `已更新 ${selectedCubeId} 原组合数据`);
  } catch (e) {
    showToast(e.message || "查询失败");
  } finally {
    liveInFlight = false;
    if (els.btnCubeLive) {
      els.btnCubeLive.disabled = !selectedCubeId;
      els.btnCubeLive.textContent = "查询原组合";
    }
  }
}

function colorizeLogs(text) {
  return text
    .split("\n")
    .map((line) => {
      const safe = escapeHtml(line);
      if (/\bCRITICAL\b|已失效|异常/.test(line)) return `<span class="log-critical">${safe}</span>`;
      if (/\bERROR\b/.test(line)) return `<span class="log-error">${safe}</span>`;
      if (/\bWARNING\b|跳过本轮监控/.test(line)) return `<span class="log-warn">${safe}</span>`;
      if (/\bINFO\b/.test(line)) return `<span class="log-info">${safe}</span>`;
      return safe;
    })
    .join("\n");
}

function setLoading() {
  if (!els.stats.innerHTML.trim()) {
    els.stats.innerHTML = `
      <article class="stat"><p class="stat-label">加载中</p><p class="stat-value">…</p><p class="stat-hint">正在拉取状态</p></article>
      <article class="stat"><p class="stat-label">加载中</p><p class="stat-value">…</p><p class="stat-hint">请稍候</p></article>
      <article class="stat"><p class="stat-label">加载中</p><p class="stat-value">…</p><p class="stat-hint">请稍候</p></article>
      <article class="stat"><p class="stat-label">加载中</p><p class="stat-value">…</p><p class="stat-hint">请稍候</p></article>`;
  }
  if (!els.logs.textContent.trim()) {
    els.logs.textContent = "正在加载日志…";
  }
}

function changeTypeClass(type) {
  if (type === "新增" || type === "加仓") return type === "新增" ? "add" : "buy";
  if (type === "卖出") return "sell";
  if (type === "减仓") return "cut";
  return "buy";
}

function renderChanges(events) {
  latestChanges = Array.isArray(events) ? events : latestChanges;
  const all = Array.isArray(events) ? events : latestChanges;
  const list = selectedCubeId
    ? all.filter((e) => (e.cube_id || "").toUpperCase() === selectedCubeId)
    : all;
  const countText = selectedCubeId
    ? `${list.length}/${all.length}`
    : `${list.length} 条`;
  if (countText !== lastChangesCount) {
    lastChangesCount = countText;
    if (els.changesCount) els.changesCount.textContent = countText;
  }
  if (els.changesLive) {
    els.changesLive.textContent = selectedCubeId
      ? `当前组合 ${selectedCubeId}`
      : "与钉钉同步";
  }

  const ids = `${selectedCubeId}|` + list.map((e) => e.id).join("|");
  if (ids === lastChangeIds && lastChangesHtml) {
    return;
  }
  lastChangeIds = ids;

  if (!list.length) {
    const empty = selectedCubeId
      ? `<div class="empty">组合 ${escapeHtml(selectedCubeId)} 暂无变动记录。可切换其他组合，或等待钉钉推送后同步显示。</div>`
      : `<div class="empty">暂无持仓变动。检测到变动并成功推送钉钉后，会在这里实时显示（与钉钉消息同步）。</div>`;
    if (empty !== lastChangesHtml) {
      lastChangesHtml = empty;
      els.changes.innerHTML = empty;
    }
    return;
  }

  const html = list
    .map((ev) => {
      const items = (ev.changes || [])
        .map((c) => {
          const price = Number(c.price) || 0;
          const detail = escapeHtml(c.detail || "");
          return `
            <li class="change-item">
              <span class="change-type ${changeTypeClass(c.type)}">${escapeHtml(c.type || "")}</span>
              <div class="change-main">
                <strong>${escapeHtml(c.name || c.symbol || "")}</strong>
                <span>${escapeHtml(c.symbol || "")}${price ? ` · ¥${price.toFixed(2)}` : ""}</span>
              </div>
              <div class="change-detail">${detail}</div>
            </li>`;
        })
        .join("");

      return `
        <article class="change-card">
          <div class="change-head">
            <div>
              <h3 class="change-title">${escapeHtml(ev.cube_name || ev.cube_id || "组合变动")}</h3>
              <p class="change-sub">${escapeHtml(ev.cube_id || "")}${ev.rb_id != null ? ` · 调仓 ${ev.rb_id}` : ""} · 已推送钉钉${ev.cash != null ? ` · 现金 ${Number(ev.cash).toFixed(1)}%` : ""}</p>
            </div>
            <div class="change-time" title="${escapeHtml(formatTime(ev.time))}">${relativeTime(ev.time)}</div>
          </div>
          <ul class="change-items">${items}</ul>
        </article>`;
    })
    .join("");

  if (html !== lastChangesHtml) {
    lastChangesHtml = html;
    els.changes.innerHTML = html;
  }
}

async function loadStatus() {
  if (statusInFlight) return null;
  statusInFlight = true;
  try {
    const data = await api("/api/status");
    renderStats(data);
    const cubes = (data.cubes || []).map((c) => {
      const live = liveCubeCache[c.id];
      return live ? { ...c, ...live, positions: live.positions?.length ? live.positions : c.positions } : c;
    });
    latestChanges = data.recent_changes || [];
    renderCubes(cubes);
    renderChanges(latestChanges);
    return data;
  } finally {
    statusInFlight = false;
  }
}

async function loadLogs({ silent = false } = {}) {
  if (logsInFlight) return null;
  logsInFlight = true;
  try {
    const data = await api("/api/logs?lines=200");
    const lines = data.lines || [];
    const text = lines.length ? lines.join("\n") : "暂无日志";
    if (data.mtime === lastLogMtime && text === lastLogText) {
      if (els.logLive) els.logLive.textContent = "实时";
      return data;
    }

    const stickBottom = isNearBottom(els.logs);
    lastLogMtime = data.mtime;
    lastLogText = text;
    els.logs.innerHTML = colorizeLogs(text);
    if (stickBottom || !silent) {
      els.logs.scrollTop = els.logs.scrollHeight;
    }
    if (els.logLive) {
      els.logLive.textContent = data.updated_at ? `实时 · ${formatTime(data.updated_at)}` : "实时";
    }
    return data;
  } finally {
    logsInFlight = false;
  }
}

async function refreshAll() {
  try {
    await Promise.all([loadStatus(), loadLogs()]);
  } catch (e) {
    showToast(e.message || "刷新失败");
  }
}

async function triggerCheck() {
  try {
    els.btnCheck.disabled = true;
    const res = await api("/api/check", { method: "POST" });
    showToast(res.message || "已开始检查");
    await loadStatus();
    startCheckPolling();
  } catch (e) {
    showToast(e.message || "触发失败");
    els.btnCheck.disabled = false;
  }
}

function startCheckPolling() {
  clearInterval(checkPollTimer);
  checkPollTimer = setInterval(async () => {
    try {
      const data = await loadStatus();
      if (data && !data.check?.running) {
        clearInterval(checkPollTimer);
        await loadLogs();
        if (data.check?.last_result === "ok") showToast("检查完成");
        else if (data.check?.error) showToast(data.check.error);
      }
    } catch (_) {
      /* ignore */
    }
  }, 1000);
}

function startPolling() {
  clearInterval(pollTimer);
  clearInterval(logTimer);
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    loadStatus().catch(() => {});
  }, STATUS_POLL_MS);
  logTimer = setInterval(() => {
    if (document.hidden) return;
    loadLogs({ silent: true }).catch(() => {});
  }, LOG_POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  clearInterval(logTimer);
  pollTimer = null;
  logTimer = null;
}

els.btnRefresh.addEventListener("click", refreshAll);
els.btnLogRefresh.addEventListener("click", () => loadLogs().catch((e) => showToast(e.message)));

async function clearLogs() {
  if (!window.confirm("确定清空运行日志？此操作不可恢复。")) return;
  try {
    if (els.btnLogClear) els.btnLogClear.disabled = true;
    const res = await api("/api/logs/clear", { method: "POST" });
    lastLogText = "";
    lastLogMtime = null;
    showToast(res.message || "日志已清空");
    await loadLogs();
  } catch (e) {
    showToast(e.message || "清空失败");
  } finally {
    if (els.btnLogClear) els.btnLogClear.disabled = false;
  }
}
if (els.btnLogClear) els.btnLogClear.addEventListener("click", clearLogs);
els.btnCheck.addEventListener("click", triggerCheck);

if (els.cubeTabs) {
  els.cubeTabs.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-cube-id]");
    if (!btn) return;
    selectCube(btn.getAttribute("data-cube-id"));
  });
}
if (els.btnCubeLive) els.btnCubeLive.addEventListener("click", fetchCubeLive);

let loginPollTimer = null;
async function startXueqiuLogin() {
  try {
    if (els.btnLogin) els.btnLogin.disabled = true;
    const res = await api("/api/xueqiu/login/start", { method: "POST" });
    showToast(res.login?.message || "已打开登录窗口");
    clearInterval(loginPollTimer);
    loginPollTimer = setInterval(async () => {
      try {
        const login = await api("/api/xueqiu/login/status");
        if (login.status === "running") {
          if (els.btnLogin) els.btnLogin.textContent = "登录中…";
          return;
        }
        clearInterval(loginPollTimer);
        if (els.btnLogin) {
          els.btnLogin.disabled = false;
          els.btnLogin.textContent = "登录雪球";
        }
        if (login.status === "success") {
          showToast("Cookie 已自动写入");
          await refreshAll();
        } else if (login.message) {
          showToast(login.message);
        }
      } catch (_) {
        /* ignore */
      }
    }, 1000);
  } catch (e) {
    if (els.btnLogin) {
      els.btnLogin.disabled = false;
      els.btnLogin.textContent = "登录雪球";
    }
    showToast(e.message || "无法启动登录");
  }
}
if (els.btnLogin) els.btnLogin.addEventListener("click", startXueqiuLogin);

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPolling();
  } else {
    refreshAll();
    startPolling();
  }
});

setLoading();
refreshAll();
startPolling();
