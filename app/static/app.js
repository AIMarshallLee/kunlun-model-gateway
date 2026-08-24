"use strict";

(() => {
const state = {
  token: "",
  identityToken: "",
  ready: null,
  captchaTokens: { register: "", forgot: "", resend: "" },
  captchaWidgets: { register: null, forgot: null, resend: null },
  turnstilePromise: null,
};
const byId = (id) => document.getElementById(id);

function loadTurnstile() {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  if (state.turnstilePromise) return state.turnstilePromise;
  state.turnstilePromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    script.defer = true;
    script.onload = () => window.turnstile ? resolve(window.turnstile) : reject(new Error("人机验证组件不可用"));
    script.onerror = () => reject(new Error("人机验证组件加载失败"));
    document.head.append(script);
  });
  return state.turnstilePromise;
}

function renderCaptcha(kind) {
  if (!state.ready || state.ready.captcha_provider !== "turnstile" || !window.turnstile) return;
  if (state.captchaWidgets[kind] !== null) return;
  const container = byId(`${kind}-captcha`);
  const actions = {
    register: "register",
    forgot: "password_reset",
    resend: "resend_verification",
  };
  if (!actions[kind]) return;
  container.hidden = false;
  state.captchaWidgets[kind] = window.turnstile.render(container, {
    sitekey: state.ready.captcha_site_key,
    theme: "light",
    size: "flexible",
    action: actions[kind],
    callback: (token) => { state.captchaTokens[kind] = token; },
    "expired-callback": () => { state.captchaTokens[kind] = ""; },
    "error-callback": () => { state.captchaTokens[kind] = ""; },
  });
}

function resetCaptcha(kind) {
  state.captchaTokens[kind] = "";
  if (window.turnstile && state.captchaWidgets[kind] !== null) {
    window.turnstile.reset(state.captchaWidgets[kind]);
  }
}

function showToast(message) {
  const node = byId("toast");
  node.textContent = message;
  node.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { node.hidden = true; }, 4200);
}

function errorMessage(body, status) {
  if (body && body.error && body.error.message) return body.error.message;
  if (body && typeof body.detail === "string") return body.detail;
  if (body && Array.isArray(body.detail)) return body.detail.map((item) => item.msg).join("；");
  return `请求失败（HTTP ${status}）`;
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (options.auth !== false && state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(body, response.status));
  return body;
}

function formData(form) {
  return Object.fromEntries([...new FormData(form).entries()].filter(([, value]) => value !== ""));
}

function consumeIdentityFragment() {
  const fragment = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "";
  const parameters = new URLSearchParams(fragment);
  const token = parameters.get("token") || "";
  if (parameters.has("token")) {
    // Remove the one-time token from browser history immediately after it has
    // been copied into memory. Fragments never reach Caddy/Uvicorn access logs.
    window.history.replaceState({}, "", `${window.location.pathname}${window.location.search}`);
  }
  return token;
}

// Consume the one-time credential synchronously, before the first fetch and
// before any third-party CAPTCHA script can execute. The surrounding closure
// also keeps the in-memory credential outside the global script environment.
state.identityToken = consumeIdentityFragment();
const isIdentityRoute = ["/verify-email", "/reset-password"].includes(window.location.pathname);

function amount(value) {
  const number = Number(value || 0);
  return `${number.toLocaleString("zh-CN")} µUSD`;
}

function dateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

function empty(node, message) {
  const item = document.createElement("p");
  item.className = "empty";
  item.textContent = message;
  node.replaceChildren(item);
}

function record(title, subtitle, actionLabel, action) {
  const row = document.createElement("div");
  row.className = "record";
  const copy = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const small = document.createElement("small");
  small.textContent = subtitle;
  copy.append(strong, small);
  row.append(copy);
  if (actionLabel && action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = actionLabel;
    button.addEventListener("click", action);
    row.append(button);
  }
  return row;
}

function cell(text, className = "") {
  const node = document.createElement("td");
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function loadReady() {
  state.ready = await api("/readyz", { auth: false });
  const flags = [
    state.ready.public_signup ? "注册开启" : "注册关闭",
    state.ready.live_payments ? "正式支付" : (state.ready.test_payments ? "测试支付" : "支付关闭"),
    state.ready.live_upstream ? "真实上游" : "上游关闭",
  ];
  byId("environment-strip").textContent = flags.join(" / ");
  byId("signup-state").textContent = state.ready.public_signup ? "当前已开启" : "当前未开放";
  byId("register-form").querySelector("button").disabled = !state.ready.public_signup;
  byId("topup-form").hidden = !state.ready.test_payments;
  byId("checkout-form").hidden = !state.ready.live_payments;
  document.querySelectorAll(".captcha-widget").forEach((node) => { node.hidden = true; });
  if (state.ready.captcha_required && !isIdentityRoute) {
    if (state.ready.captcha_provider !== "turnstile" || !state.ready.captcha_site_key) {
      byId("register-form").querySelector("button").disabled = true;
      throw new Error("浏览器人机验证组件未配置，注册已安全关闭");
    }
    await loadTurnstile();
    renderCaptcha("register");
  }
  byId("mode-value").textContent = state.ready.live_payments ? "正式桥接" : (state.ready.test_payments ? "受控测试" : "安全关闭");
  byId("provider-value").textContent = `${state.ready.providers} 个可用 Provider`;
  if (state.ready.live_payments) {
    const data = await api("/billing/packages", { auth: false });
    const options = data.packages.map((item) => {
      const option = document.createElement("option");
      option.value = item.sku;
      option.textContent = `${item.sku} · ${item.payment_amount_minor} ${item.payment_currency} → ${amount(item.credit_amount_microusd)}`;
      return option;
    });
    byId("package-select").replaceChildren(...options);
    byId("payment-boundary").textContent = "现金金额与服务额度分别记账；支付由正式桥接服务验签并异步入账。";
  }
}

async function loadKeys() {
  const data = await api("/v1/keys");
  const target = byId("key-list");
  if (!data.keys.length) return empty(target, "尚未创建 API Key。");
  target.replaceChildren(...data.keys.map((item) => record(
    `${item.name} · ••••${item.last_four}`,
    `${item.status} · 创建于 ${dateTime(item.created_at)}`,
    item.status === "active" ? "吊销" : "",
    item.status === "active" ? async () => {
      if (!window.confirm(`确认吊销 ${item.name}？此操作立即生效。`)) return;
      await api("/v1/keys/revoke", { method: "POST", body: { key_id: item.id } });
      showToast("API Key 已吊销");
      await loadKeys();
    } : null,
  )));
}

async function loadBalanceAndLedger() {
  const [wallet, ledger] = await Promise.all([api("/billing/balance"), api("/billing/ledger")]);
  byId("balance-value").textContent = amount(wallet.balance);
  byId("reserved-value").textContent = amount(wallet.reserved);
  const body = byId("ledger-table");
  if (!ledger.entries.length) {
    const row = document.createElement("tr");
    const message = cell("暂无余额流水");
    message.colSpan = 4;
    row.append(message);
    return body.replaceChildren(row);
  }
  body.replaceChildren(...ledger.entries.slice().reverse().map((item) => {
    const row = document.createElement("tr");
    row.append(
      cell(dateTime(item.created_at)),
      cell(item.kind),
      cell(item.reference),
      cell(amount(item.amount), item.amount >= 0 ? "positive" : "negative"),
    );
    return row;
  }));
}

async function loadTopups() {
  const data = await api("/billing/topups");
  const target = byId("topup-list");
  if (!data.orders.length) return empty(target, "暂无充值订单。");
  target.replaceChildren(...data.orders.map((item) => record(
    `${amount(item.credit_amount_microusd ?? item.amount)} · ${item.status}`,
    `${item.payment_amount_minor ? `${item.payment_amount_minor} ${item.payment_currency} · ` : ""}${item.payment_mode} · ${dateTime(item.created_at)}`,
  )));
}

async function loadBudgets() {
  const data = await api("/budgets");
  const target = byId("budget-list");
  const active = data.budgets.find((item) => item.status === "active");
  byId("budget-value").textContent = active ? amount(active.available) : "未设置";
  if (!data.budgets.length) return empty(target, "尚未设置月度预算。");
  target.replaceChildren(...data.budgets.map((item) => record(
    `${item.status} · 可用 ${amount(item.available)}`,
    `上限 ${amount(item.amount)} / 已用 ${amount(item.spent)} / 预留 ${amount(item.reserved)}`,
  )));
}

async function loadUsage() {
  const data = await api("/billing/usage");
  const body = byId("usage-table");
  if (!data.entries.length) {
    const row = document.createElement("tr");
    const message = cell("暂无模型调用记录");
    message.colSpan = 5;
    row.append(message);
    return body.replaceChildren(row);
  }
  body.replaceChildren(...data.entries.slice().reverse().map((item) => {
    const row = document.createElement("tr");
    row.append(
      cell(item.request_id.slice(0, 8)),
      cell(item.status, item.status.includes("pending") || item.status.includes("failed") ? "danger" : ""),
      cell(`${item.model} / ${item.provider || "—"}`),
      cell(`${amount(item.amount)}${item.usage_estimated ? "（估算）" : ""}`),
      cell(`${item.input_tokens} in / ${item.output_tokens} out`),
    );
    return row;
  }));
}

async function refreshConsole() {
  await Promise.all([loadKeys(), loadBalanceAndLedger(), loadTopups(), loadBudgets(), loadUsage()]);
}

function enterConsole(token) {
  state.token = token;
  byId("auth-shell").hidden = true;
  byId("console").hidden = false;
  refreshConsole().catch((error) => showToast(error.message));
}

async function logout() {
  try {
    await api("/auth/logout-all", { method: "POST" });
  } catch (_error) {
    // Local logout still clears the memory-only token if the session expired.
  } finally {
    state.token = "";
    byId("secret-value").textContent = "";
    byId("one-time-secret").hidden = true;
    byId("console").hidden = true;
    byId("auth-shell").hidden = false;
    showToast("所有网页登录会话已退出；页面未持久化令牌");
  }
}

byId("register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formData(event.currentTarget);
  if (state.ready && state.ready.captcha_required) {
    if (!state.captchaTokens.register) return showToast("请先完成人机验证");
    data.captcha_token = state.captchaTokens.register;
  }
  try {
    const result = await api("/auth/register", { method: "POST", body: data, auth: false });
    event.currentTarget.reset();
    showToast(result.accepted ? "如该邮箱可注册或仍待验证，系统会发送验证邮件" : "账户已创建，请使用左侧表单登录");
  } catch (error) { showToast(error.message); }
  finally { if (state.ready && state.ready.captcha_required) resetCaptcha("register"); }
});

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formData(event.currentTarget);
  try {
    const result = await api("/auth/login", { method: "POST", body: data, auth: false });
    event.currentTarget.reset();
    enterConsole(result.access_token);
  } catch (error) { showToast(error.message); }
});

byId("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/v1/keys", { method: "POST", body: formData(event.currentTarget) });
    byId("secret-value").textContent = result.key;
    byId("one-time-secret").hidden = false;
    event.currentTarget.reset();
    await loadKeys();
  } catch (error) { showToast(error.message); }
});

byId("hide-secret").addEventListener("click", () => {
  byId("secret-value").textContent = "";
  byId("one-time-secret").hidden = true;
});

byId("topup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formData(event.currentTarget);
  try {
    const order = await api("/billing/topups", { method: "POST", body: { amount: Number(data.amount) } });
    showToast(`测试订单 ${order.id.slice(0, 8)} 已创建；需由测试回调入账`);
    await loadTopups();
  } catch (error) { showToast(error.message); }
});

byId("checkout-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formData(event.currentTarget);
  try {
    const idempotencyKey = window.crypto.randomUUID();
    const order = await api("/billing/checkout", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: { sku: data.sku, return_url: `${window.location.origin}/` },
    });
    window.location.assign(order.checkout_url);
  } catch (error) { showToast(error.message); }
});

byId("forgot-toggle").addEventListener("click", () => {
  byId("forgot-fields").hidden = !byId("forgot-fields").hidden;
  if (!byId("forgot-fields").hidden && state.ready && state.ready.captcha_required) renderCaptcha("forgot");
});

byId("forgot-submit").addEventListener("click", async () => {
  const body = { email: byId("forgot-email").value };
  if (!body.email) return showToast("请填写找回邮箱");
  if (state.ready && state.ready.captcha_required) {
    if (!state.captchaTokens.forgot) return showToast("请先完成人机验证");
    body.captcha_token = state.captchaTokens.forgot;
  }
  try {
    await api("/auth/forgot-password", { method: "POST", body, auth: false });
    showToast("如账户存在且可用，重置邮件将发送到该邮箱");
  } catch (error) { showToast(error.message); }
  finally { if (state.ready && state.ready.captcha_required) resetCaptcha("forgot"); }
});

byId("resend-toggle").addEventListener("click", () => {
  byId("resend-fields").hidden = !byId("resend-fields").hidden;
  if (!byId("resend-fields").hidden && state.ready && state.ready.captcha_required) {
    renderCaptcha("resend");
  }
});

byId("resend-submit").addEventListener("click", async () => {
  const body = { email: byId("resend-email").value };
  if (!body.email) return showToast("请填写注册邮箱");
  if (state.ready && state.ready.captcha_required) {
    if (!state.captchaTokens.resend) return showToast("请先完成人机验证");
    body.captcha_token = state.captchaTokens.resend;
  }
  try {
    await api("/auth/resend-verification", { method: "POST", body, auth: false });
    showToast("如账户存在且仍待验证，系统会重新发送验证邮件");
  } catch (error) { showToast(error.message); }
  finally { if (state.ready && state.ready.captcha_required) resetCaptcha("resend"); }
});

byId("reset-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = state.identityToken;
  if (!token) return showToast("重置链接无效或已过期");
  try {
    await api("/auth/reset-password", {
      method: "POST",
      body: { token, ...formData(event.currentTarget) },
      auth: false,
    });
    window.history.replaceState({}, "", "/");
    state.identityToken = "";
    byId("recovery-shell").hidden = true;
    byId("auth-shell").hidden = false;
    showToast("密码已重置；旧会话和 API Key 已吊销");
  } catch (error) { showToast(error.message); }
});

byId("budget-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formData(event.currentTarget);
  try {
    await api("/budgets", { method: "POST", body: { amount: Number(data.amount) } });
    showToast("新月度预算已生效；旧预算保留为历史记录");
    await loadBudgets();
  } catch (error) { showToast(error.message); }
});

byId("logout-button").addEventListener("click", logout);

async function handleIdentityLink() {
  const token = state.identityToken;
  if (window.location.pathname === "/verify-email" && token) {
    try {
      await api("/auth/verify-email", { method: "POST", body: { token }, auth: false });
      state.identityToken = "";
      window.history.replaceState({}, "", "/");
      showToast("邮箱验证完成，现在可以登录");
    } catch (error) { showToast(error.message); }
  } else if (window.location.pathname === "/reset-password") {
    byId("auth-shell").hidden = true;
    byId("recovery-shell").hidden = false;
  }
}

loadReady()
  .then(handleIdentityLink)
  .catch((error) => showToast(error.message));
})();
