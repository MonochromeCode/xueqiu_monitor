const $ = (sel) => document.querySelector(sel);

const els = {
  toast: $("#toast"),
  form: $("#config-form"),
  btnConfigReload: $("#btn-config-reload"),
  btnConfigSave: $("#btn-config-save"),
  cookie: $("#cfg-cookie"),
  webhook: $("#cfg-webhook"),
  cubesInput: $("#cfg-cubes"),
  interval: $("#cfg-interval"),
  threshold: $("#cfg-threshold"),
  logfile: $("#cfg-logfile"),
  atAll: $("#cfg-atall"),
  tradingHours: $("#cfg-trading"),
  offInterval: $("#cfg-off-interval"),
  marketClose: $("#cfg-market-close"),
  preCloseMin: $("#cfg-preclose-min"),
  preCloseInterval: $("#cfg-preclose-interval"),
  liveChip: $("#config-live"),
  dirtyHint: $("#config-dirty-hint"),
  toggleSecrets: $("#btn-toggle-secrets"),
  btnLogin: $("#btn-xueqiu-login"),
  btnLoginCancel: $("#btn-xueqiu-cancel"),
  loginStatus: $("#login-status"),
  cookieAcquired: $("#cookie-acquired"),
};

const CONFIG_POLL_MS = 1000;

let configDirty = false;
let lastMtime = null;
let saving = false;
let pollTimer = null;
let configInFlight = false;
let secretsVisible = true;

function formatTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function showToast(message) {
  els.toast.hidden = false;
  els.toast.textContent = message;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => {
    els.toast.hidden = true;
  }, 2600);
}

function setDirty(dirty) {
  configDirty = dirty;
  if (els.dirtyHint) els.dirtyHint.hidden = !dirty;
}

function applySecretVisibility() {
  const type = secretsVisible ? "text" : "password";
  els.webhook.type = type;
  els.cookie.classList.toggle("secret-masked", !secretsVisible);
  if (els.toggleSecrets) {
    els.toggleSecrets.textContent = secretsVisible ? "隐藏敏感信息" : "显示敏感信息";
  }
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

function fillConfigForm(cfg) {
  els.cookie.value = cfg.xueqiu_cookie || "";
  els.webhook.value = cfg.dingtalk_webhook || "";
  els.cubesInput.value = cfg.monitored_cubes || "";
  els.interval.value = cfg.check_interval ?? 300;
  els.threshold.value = cfg.weight_change_threshold ?? 1.0;
  els.logfile.value = cfg.log_file || "";
  els.atAll.checked = Boolean(cfg.at_all);
  if (els.tradingHours) els.tradingHours.checked = cfg.trading_hours_only !== false;
  if (els.offInterval) els.offInterval.value = cfg.off_hours_interval ?? 1800;
  if (els.marketClose) els.marketClose.value = cfg.market_close || "15:00";
  if (els.preCloseMin) els.preCloseMin.value = cfg.pre_close_minutes ?? 15;
  if (els.preCloseInterval) els.preCloseInterval.value = cfg.pre_close_interval ?? 60;
  lastMtime = cfg.mtime ?? null;
  setDirty(false);
  applySecretVisibility();
  renderCookieAcquired(cfg);
  if (els.liveChip) {
    const sched = cfg.schedule?.label ? ` · ${cfg.schedule.label}` : "";
    els.liveChip.textContent = cfg.updated_at
      ? `实时 · ${formatTime(cfg.updated_at)}${sched}`
      : `实时同步 .env${sched}`;
  }
}

function renderCookieAcquired(cfg) {
  if (!els.cookieAcquired) return;
  const acquired = cfg.token?.issued_at || cfg.token?.acquired_at || cfg.login?.finished_at;
  if (acquired) {
    els.cookieAcquired.textContent = `Token 获取时间：${formatTime(acquired)}（来自雪球登录签发）`;
  } else {
    els.cookieAcquired.textContent =
      "点击上方按钮打开浏览器，完成登录后程序会自动写入 Cookie；也可手动粘贴。";
  }
}

function collectConfigForm() {
  return {
    xueqiu_cookie: els.cookie.value.trim(),
    dingtalk_webhook: els.webhook.value.trim(),
    monitored_cubes: els.cubesInput.value.trim(),
    check_interval: Number(els.interval.value || 300),
    weight_change_threshold: Number(els.threshold.value || 1),
    at_all: els.atAll.checked,
    log_file: els.logfile.value.trim(),
    trading_hours_only: els.tradingHours ? els.tradingHours.checked : true,
    off_hours_interval: Number((els.offInterval && els.offInterval.value) || 1800),
    pre_close_minutes: Number((els.preCloseMin && els.preCloseMin.value) || 15),
    pre_close_interval: Number((els.preCloseInterval && els.preCloseInterval.value) || 60),
    market_close: ((els.marketClose && els.marketClose.value) || "15:00").trim() || "15:00",
  };
}

async function loadConfig({ force = false } = {}) {
  if (configInFlight) return null;
  configInFlight = true;
  try {
    const cfg = await api("/api/config");
    const changed = cfg.mtime !== lastMtime;

    if (force || !configDirty) {
      if (force || changed || lastMtime == null) {
        fillConfigForm(cfg);
      } else if (els.liveChip && cfg.updated_at) {
        els.liveChip.textContent = `实时 · ${formatTime(cfg.updated_at)}`;
      }
    } else if (changed && els.liveChip) {
      els.liveChip.textContent = "文件已更新 · 编辑中暂停覆盖";
    }
    return cfg;
  } finally {
    configInFlight = false;
  }
}

async function saveConfig(event) {
  event.preventDefault();
  const payload = collectConfigForm();
  if (!payload.xueqiu_cookie || !payload.dingtalk_webhook || !payload.monitored_cubes) {
    showToast("请填写必填配置项");
    return;
  }
  if (payload.check_interval < 10) {
    showToast("检查间隔不能小于 10 秒");
    return;
  }
  const buttons = [els.btnConfigSave, els.form.querySelector('button[type="submit"]')].filter(Boolean);
  saving = true;
  try {
    buttons.forEach((btn) => {
      btn.disabled = true;
      btn.textContent = "保存中…";
    });
    const res = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    fillConfigForm(res.config || payload);
    showToast(res.message || "配置已保存");
  } catch (e) {
    showToast(e.message || "保存失败");
  } finally {
    saving = false;
    buttons.forEach((btn) => {
      btn.disabled = false;
      btn.textContent = "保存配置";
    });
  }
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (document.hidden || saving) return;
    loadConfig().catch(() => {});
  }, CONFIG_POLL_MS);
}

let loginPollTimer = null;

function renderLoginStatus(login) {
  if (!els.loginStatus) return;
  const status = login?.status || "idle";
  const map = {
    idle: "未登录抓取",
    running: "等待浏览器登录…",
    success: "已获取 Cookie",
    failed: "登录失败",
    cancelled: "已取消",
  };
  els.loginStatus.textContent = login?.message || map[status] || status;
  if (els.btnLogin) els.btnLogin.disabled = status === "running";
  if (els.btnLoginCancel) els.btnLoginCancel.hidden = status !== "running";
}

async function pollLoginStatus() {
  clearInterval(loginPollTimer);
  loginPollTimer = setInterval(async () => {
    try {
      const login = await api("/api/xueqiu/login/status");
      renderLoginStatus(login);
      if (login.status === "success") {
        clearInterval(loginPollTimer);
        showToast("Cookie 已自动写入");
        await loadConfig({ force: true });
      } else if (login.status === "failed" || login.status === "cancelled") {
        clearInterval(loginPollTimer);
        if (login.message) showToast(login.message);
      }
    } catch (_) {
      /* ignore */
    }
  }, 1000);
}

async function startXueqiuLogin() {
  try {
    const res = await api("/api/xueqiu/login/start", { method: "POST" });
    renderLoginStatus(res.login);
    showToast(res.login?.message || "已打开登录窗口");
    pollLoginStatus();
  } catch (e) {
    showToast(e.message || "无法启动登录");
  }
}

async function cancelXueqiuLogin() {
  try {
    const res = await api("/api/xueqiu/login/cancel", { method: "POST" });
    renderLoginStatus(res.login);
    clearInterval(loginPollTimer);
  } catch (e) {
    showToast(e.message || "取消失败");
  }
}

els.form.addEventListener("input", () => setDirty(true));
els.form.addEventListener("change", () => setDirty(true));
els.form.addEventListener("submit", saveConfig);
els.btnConfigReload.addEventListener("click", () => {
  loadConfig({ force: true })
    .then(() => showToast("已重新加载配置"))
    .catch((e) => showToast(e.message));
});

if (els.toggleSecrets) {
  els.toggleSecrets.addEventListener("click", () => {
    secretsVisible = !secretsVisible;
    applySecretVisibility();
  });
}
if (els.btnLogin) els.btnLogin.addEventListener("click", startXueqiuLogin);
if (els.btnLoginCancel) els.btnLoginCancel.addEventListener("click", cancelXueqiuLogin);

window.addEventListener("beforeunload", (e) => {
  if (!configDirty) return;
  e.preventDefault();
  e.returnValue = "";
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    loadConfig().catch(() => {});
  }
});

applySecretVisibility();
loadConfig({ force: true }).catch((e) => showToast(e.message || "加载配置失败"));
api("/api/xueqiu/login/status").then(renderLoginStatus).catch(() => {});
startPolling();
