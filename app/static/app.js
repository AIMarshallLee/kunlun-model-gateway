"use strict";
import {createCheckoutFlow, checkoutDestination} from "./checkout.js";
import {t, language, setLanguage, localizedError, captureStaticLocale} from "./console-locale.js";

(() => {
const state = {
  token: "",
  identityToken: "",
  ready: null,
  captchaTokens: { register: "", forgot: "", resend: "" },
  captchaWidgets: { register: null, forgot: null, resend: null },
  turnstilePromise: null,
  testKey: "",
  testIdempotency: "",
  lastCheckout: null,
  lastRequest: null,
  lastTestResult: null,
  catalog: null,
  packages: [],
};
const byId = (id) => document.getElementById(id);
const checkout = createCheckoutFlow(api, window.location.origin);

function loadTurnstile() {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  if (state.turnstilePromise) return state.turnstilePromise;
  state.turnstilePromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    script.defer = true;
    script.onload = () => window.turnstile ? resolve(window.turnstile) : reject(new Error(t("人机验证组件不可用")));
    script.onerror = () => reject(new Error(t("人机验证组件加载失败")));
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
  node.textContent = localizedError(message);
  node.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { node.hidden = true; }, 4200);
}

function errorMessage(body, status) {
  if (body && body.error && body.error.message) return localizedError(body.error.message, status);
  if (body && typeof body.detail === "string") return localizedError(body.detail, status);
  if (body && Array.isArray(body.detail)) return localizedError(body.detail.map((item) => item.msg).join("; "), status);
  return t`请求失败（HTTP ${status}）`;
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (options.auth !== false && state.token) headers.Authorization = `Bearer ${state.token}`;
  const controller = options.timeoutMs ? new AbortController() : null;
  const timer = controller ? window.setTimeout(() => controller.abort(), options.timeoutMs) : null;
  let response, body;
  try {
    response = await fetch(path, {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller?.signal,
    });
    body = response.status === 204 ? null : await response.json().catch(() => null);
  } finally { if (timer !== null) window.clearTimeout(timer); }
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
setLanguage(new URL(window.location.href).searchParams.get("lang"));
const renderStaticLocale = captureStaticLocale();
renderStaticLocale();
function renderLanguageLinks() {
  byId("console-language").textContent = language === "zh" ? "English" : "中文";
  document.querySelectorAll("[data-console-link]").forEach((link) => {
    const url = new URL(link.getAttribute("href"), window.location.origin);
    url.searchParams.set("lang", language);
    link.href = url.pathname + url.search + url.hash;
  });
}
renderLanguageLinks();

function amount(value) {
  const number = Number(value || 0);
  return `${number.toLocaleString(language === "zh" ? "zh-CN" : "en-US")} µUSD`;
}

function dateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(language === "zh" ? "zh-CN" : "en-US", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
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

async function loadReady(languageOnly = false) {
  if (!languageOnly) state.ready = await api("/readyz", { auth: false });
  if (!state.ready) return;
  const byok = state.ready.gateway_mode === "byok";
  const managed = state.ready.gateway_mode === "managed_gateway";
  byId("console-guide-links").hidden = !managed;
  byId("register-form").hidden = !state.ready.public_signup;
  byId("invitation-note").hidden = state.ready.public_signup;
  byId("provider-panel").hidden = !byok;
  byId("model-test-panel").hidden = !(byok || managed);
  document.querySelector(".panel-wallet").hidden = byok;
  document.querySelector(".panel-ledger").hidden = byok;
  if (byok) {
    byId("balance-label").textContent = t("供应商累计支出");
    byId("balance-note").textContent = t("按本页最近调用记录汇总；以供应商账单为准");
  }
  if (managed) {
    byId("account-introduction").textContent = t("购买本站调用额度，使用本站 Key 调用平台模型；不需要提供供应商 Key。");
    byId("model-test-introduction").textContent = t("确认本站余额与预算后，使用本站 Key 运行测试。测试会消耗调用额度。");
    if (!languageOnly) state.catalog = await api("/public/catalog", {auth: false});
    if (!languageOnly) byId("test-model-select").replaceChildren(...(state.catalog?.models || []).map((model) => {
      const option = document.createElement("option"); option.value = model.id; option.textContent = model.id; return option;
    }));
  }
  const flags = [
    state.ready.environment === "production" ? t("生产配置（不等于上线验收）") : t("测试环境（不接受真实付款）"),
    state.ready.public_signup ? t("注册开启") : t("注册关闭"),
    state.ready.live_payments ? (state.ready.environment === "production" ? t("支付已配置") : t("模拟支付桥接")) : (state.ready.test_payments ? t("测试支付") : t("支付关闭")),
    byok ? t("客户自带模型账号") : (state.ready.live_upstream ? t("真实上游") : t("上游关闭")),
  ];
  byId("environment-strip").textContent = flags.join(" / ");
  byId("signup-state").textContent = state.ready.public_signup ? t("当前已开启") : t("当前未开放");
  byId("register-form").querySelector("button").disabled = !state.ready.public_signup;
  byId("topup-form").hidden = !state.ready.test_payments;
  byId("checkout-form").hidden = !state.ready.live_payments;
  if (!languageOnly) document.querySelectorAll(".captcha-widget").forEach((node) => { node.hidden = true; });
  if (!languageOnly && state.ready.captcha_required && !isIdentityRoute) {
    if (state.ready.captcha_provider !== "turnstile" || !state.ready.captcha_site_key) {
      byId("register-form").querySelector("button").disabled = true;
      throw new Error(t("浏览器人机验证组件未配置，注册已安全关闭"));
    }
    await loadTurnstile();
    renderCaptcha("register");
  }
  byId("mode-value").textContent = managed ? t("平台供给") : byok ? "BYOK" : (state.ready.live_payments ? t("正式桥接") : (state.ready.test_payments ? t("受控测试") : t("安全关闭")));
  byId("provider-value").textContent = managed ? t("渠道实时可用性以实际任务结果为准") : t`${state.ready.providers} 个可用 Provider`;
  if (state.ready.live_payments) {
    if (!languageOnly) {
      state.packages = (await api("/billing/packages", {auth: false})).packages;
      byId("package-select").replaceChildren(...state.packages.map((item) => {
        const option = document.createElement("option"); option.value = item.sku; return option;
      }));
    }
    for (const option of byId("package-select").options) {
      const item = state.packages.find((entry) => entry.sku === option.value);
      if (item) option.textContent = t`${item.sku} · ${item.payment_amount_minor} ${item.payment_currency}（最小货币单位） → ${amount(item.credit_amount_microusd)}`;
    }
    byId("payment-boundary").textContent = state.ready.environment === "production"
      ? t("现金金额与服务额度分别记账；支付回调验签并确认后入账。网页返回不代表支付成功。")
      : t("仅模拟支付验收，不接受真实款项。测试适配器不能证明正式支付可用。");
  }
}

async function loadKeys() {
  const token = state.token;
  const data = await api("/v1/keys");
  if (token !== state.token) return;
  const target = byId("key-list");
  if (!data.keys.length) return empty(target, t("尚未创建 API Key。"));
  target.replaceChildren(...data.keys.map((item) => record(
    `${item.name} · ••••${item.last_four}`,
    [t`${item.status} · 创建于 ${dateTime(item.created_at)}`,
      t`模型：${item.allowed_models?.join(", ") || t("账户允许的全部模型")} · 输出上限：${item.max_output_tokens ?? t("遵循平台限制")}`,
      t`累计支出：${item.spent_microusd} · 占用：${item.reserved_microusd} · 剩余：${item.available_microusd ?? t("未设置 Key 上限")} microUSD`,
    ].join(" / "),
    item.status === "active" ? t("吊销") : "",
    item.status === "active" ? async () => {
      if (!window.confirm(t`确认吊销 ${item.name}？此操作立即生效。`)) return;
      await api("/v1/keys/revoke", { method: "POST", body: { key_id: item.id } });
      showToast(t("API Key 已吊销"));
      await loadKeys();
    } : null,
  )));
}

async function loadBalanceAndLedger() {
  const token = state.token;
  const [wallet, ledger] = await Promise.all([api("/billing/balance"), api("/billing/ledger")]);
  if (token !== state.token) return;
  byId("balance-value").textContent = amount(wallet.balance);
  byId("reserved-value").textContent = amount(wallet.reserved);
  const body = byId("ledger-table");
  if (!ledger.entries.length) {
    const row = document.createElement("tr");
    const message = cell(t("暂无余额流水"));
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
  const token = state.token;
  const data = await api("/billing/topups");
  if (token !== state.token) return;
  const target = byId("topup-list");
  if (!data.orders.length) return empty(target, t("暂无充值订单。"));
  target.replaceChildren(...data.orders.map((item) => record(
    `${amount(item.credit_amount_microusd ?? item.amount)} · ${item.status}`,
    `${item.id} · ${item.payment_amount_minor ? t`${item.payment_amount_minor} ${item.payment_currency}（最小货币单位） · ` : ""}${item.payment_mode} · ${dateTime(item.created_at)}`,
    t("查询 / 恢复"), async () => {
      try {
        const order = await api(`/billing/topups/${encodeURIComponent(item.id)}`, {timeoutMs: 25000});
        if (token === state.token) showCheckoutOrder(order, false);
      } catch (error) { if (token === state.token) showToast(error.message); }
    },
  )));
}

async function loadBudgets() {
  const token = state.token;
  const data = await api("/budgets");
  if (token !== state.token) return;
  const target = byId("budget-list");
  const expectedKind = state.ready?.gateway_mode === "byok" ? "provider_spend_cap" : "prepaid_credit";
  const active = data.budgets.find((item) => item.status === "active" && item.kind === expectedKind && new Date(item.period_end) > new Date());
  if (state.ready?.gateway_mode === "byok") byId("reserved-value").textContent = amount(active?.reserved);
  byId("budget-value").textContent = active ? amount(active.available) : t("未设置");
  if (!data.budgets.length) return empty(target, t("尚未设置月度预算。"));
  target.replaceChildren(...data.budgets.map((item) => record(
    t`${item.status} · 可用 ${amount(item.available)}`,
    t`上限 ${amount(item.amount)} / 已用 ${amount(item.spent)} / 预留 ${amount(item.reserved)}`,
  )));
}

async function loadUsage() {
  const token = state.token;
  const data = await api("/billing/usage");
  if (token !== state.token) return;
  if (state.ready?.gateway_mode === "byok") byId("balance-value").textContent = amount(data.entries.reduce((total, item) => total + item.upstream_cost, 0));
  const body = byId("usage-table");
  if (!data.entries.length) {
    const row = document.createElement("tr");
    const message = cell(t("暂无模型调用记录"));
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
      cell(`${amount(state.ready?.gateway_mode === "byok" ? item.upstream_cost : item.amount)}${item.usage_estimated ? t("（待对账）") : ""}`),
      cell(`${item.input_tokens} in / ${item.output_tokens} out`),
    );
    const detail = document.createElement("button");
    detail.type = "button";
    detail.textContent = t("查看处理记录");
    detail.addEventListener("click", async () => {
      try {
        const status = await api(`/requests/${encodeURIComponent(item.request_id)}`);
        if (token !== state.token) return;
        state.lastRequest = status;
        byId("request-detail").textContent = describeRequest(status);
      } catch (error) { showToast(error.message); }
    });
    row.firstChild.append(detail);
    return row;
  }));
}

async function refreshConsole(languageOnly = false) {
  const tasks = [loadKeys(), loadBudgets(), loadUsage()];
  if (state.ready?.gateway_mode === "byok") tasks.push(loadConnections(languageOnly));
  else tasks.push(loadBalanceAndLedger(), loadTopups());
  await Promise.all(tasks);
}

function describeRequest(item) {
  const guidance = {
    wait_for_completion: t("任务仍在处理，请稍后查询；不要重新提交。"),
    contact_operator_for_reconciliation: t("费用待确认，请把任务编号交给运维人员核对。"),
    check_client_output_before_explicit_new_task: t("任务已结算，请先检查客户端是否已保存结果。网关不保留回答；再次生成会成为新任务并可能另行计费。"),
    review_failure_before_explicit_new_task: t("请先排查失败原因，确认后再新建任务。"),
  };
  return t`任务：${item.request_id}\n状态：${item.status}\n费用状态：${item.cost_state}\n供应商成本：${amount(item.upstream_cost_microusd)}\n${guidance[item.next_action] || t("请联系运维确认。")}\n` +
    (item.attempts || []).map((a) => t`尝试 ${a.ordinal}：${a.provider} / ${a.status} / ${a.billing_status}${a.failure_category ? " / " + a.failure_category : ""}`).join("\n");
}

async function loadConnections(languageOnly = false) {
  const token = state.token;
  const [connections, catalog] = await Promise.all([api("/v1/provider-connections"), api("/v1/provider-catalog")]);
  if (token !== state.token) return;
  byId("provider-value").textContent = t`${connections.data.filter((item) => item.status === "active").length} 个已连接 Provider（调用权限需测试）`;
  if (!languageOnly) byId("provider-select").replaceChildren(...catalog.data.map((item) => {
    const option = document.createElement("option"); option.value = item.provider; option.textContent = item.provider; return option;
  }));
  if (!languageOnly) byId("test-model-select").replaceChildren(...[...new Set(catalog.data.flatMap((item) => item.models))].map((model) => {
    const option = document.createElement("option"); option.value = model; option.textContent = model; return option;
  }));
  const target = byId("connection-list");
  if (!connections.data.length) return empty(target, t("还未连接模型账号。"));
  target.replaceChildren(...connections.data.map((item) => record(
    `${item.provider} · ${item.status}`, t`密钥版本 ${item.credential_version}`,
    ["active", "revoked_pending_destroy"].includes(item.status) ? t("断开 / 清理连接") : "",
    async () => {
      if (!window.confirm(t("确认断开此模型连接？后续任务将无法使用它。"))) return;
      try {
        const result = await api(`/v1/provider-connections/${encodeURIComponent(item.provider)}`, { method: "DELETE" });
        showToast(result?.status === "revoked_pending_destroy" ? t("连接已停用，密钥清理待重试") : t("连接已断开"));
        await loadConnections();
      } catch (error) { showToast(error.message); }
    },
  )));
}

byId("provider-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = formData(form);
  form.elements.secret.value = "";
  try {
    await api(`/v1/provider-connections/${encodeURIComponent(data.provider)}`, {method: "PUT", body: {secret: data.secret}});
    showToast(t("连接已保存。请设置预算，再运行首次调用验收。"));
    await loadConnections();
  } catch (error) { showToast(error.message); }
  finally { data.secret = ""; }
});

function renderTestResult() {
  const item = state.lastTestResult;
  if (!item) return;
  byId("model-test-result").textContent = item.kind === "status" ? describeRequest(item.value)
    : item.kind === "response" ? t`已收到模型响应：${item.value || t("（无文本）")}`
    : t`${item.value}\n请先查询本次任务状态；不要自动重新生成。`;
}

byId("model-test-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.testIdempotency && !window.confirm(t("这将创建另一项可能计费的新测试，不会恢复上次回答。确认已核对上次任务状态并继续？"))) return;
  const form = event.currentTarget;
  const data = formData(form);
  const button = form.querySelector("button");
  const token = state.token;
  state.testKey = data.gateway_key;
  form.elements.gateway_key.value = "";
  state.testIdempotency = window.crypto.randomUUID();
  button.disabled = true;
  byId("lookup-test").hidden = false;
  try {
    const result = await api("/v1/chat/completions", {auth: false, method: "POST",
      headers: {Authorization: `Bearer ${state.testKey}`, "Idempotency-Key": state.testIdempotency},
      body: {model: data.model, messages: [{role: "user", content: "Reply with OK"}], max_tokens: 16},
    });
    if (token !== state.token) return;
    state.lastTestResult = {kind: "response", value: result.choices?.[0]?.message?.content || ""};
    renderTestResult();
    await Promise.all([loadUsage(), loadBudgets()]);
  } catch (error) {
    if (token === state.token) {
      state.lastTestResult = {kind: "error", value: error.message};
      renderTestResult();
    }
  } finally { data.gateway_key = ""; button.disabled = false; }
});

byId("lookup-test").addEventListener("click", async () => {
  const token = state.token;
  try {
    const status = await api("/v1/requests/lookup", {auth: false, method: "POST",
      headers: {Authorization: `Bearer ${state.testKey}`, "Idempotency-Key": state.testIdempotency},
    });
    if (token !== state.token) return;
    state.lastTestResult = {kind: "status", value: status};
    renderTestResult();
  } catch (error) { showToast(error.message); }
});
byId("refresh-usage").addEventListener("click", () => Promise.all([loadUsage(), loadBudgets()]).catch((e) => showToast(e.message)));

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
    state.testKey = "";
    state.testIdempotency = "";
    state.lastRequest = null;
    state.lastTestResult = null;
    checkout.clear();
    clearCheckoutDisplay();
    byId("model-test-result").textContent = "";
    byId("request-detail").textContent = "";
    byId("lookup-test").hidden = true;
    byId("provider-form").reset();
    byId("model-test-form").reset();
    byId("secret-value").textContent = "";
    byId("one-time-secret").hidden = true;
    byId("console").hidden = true;
    byId("auth-shell").hidden = false;
    showToast(t("所有网页登录会话已退出；页面未持久化令牌"));
  }
}

byId("register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = formData(form);
  if (state.ready && state.ready.captcha_required) {
    if (!state.captchaTokens.register) return showToast(t("请先完成人机验证"));
    data.captcha_token = state.captchaTokens.register;
  }
  try {
    const result = await api("/auth/register", { method: "POST", body: data, auth: false });
    form.reset();
    showToast(result.accepted ? t("如该邮箱可注册或仍待验证，系统会发送验证邮件") : t("账户已创建，请使用左侧表单登录"));
  } catch (error) { showToast(error.message); }
  finally { if (state.ready && state.ready.captcha_required) resetCaptcha("register"); }
});

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = formData(form);
  try {
    const result = await api("/auth/login", { method: "POST", body: data, auth: false });
    form.reset();
    enterConsole(result.access_token);
  } catch (error) { showToast(error.message); }
});

byId("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const fields = formData(form);
    const body = {
      name: fields.name,
      allowed_models: fields.allowed_models.trim() ? fields.allowed_models.split(",").map((value) => value.trim()) : null,
      max_output_tokens: fields.max_output_tokens ? Number(fields.max_output_tokens) : null,
      spend_limit_microusd: fields.spend_limit_microusd ? Number(fields.spend_limit_microusd) : null,
    };
    const result = await api("/v1/keys", { method: "POST", body });
    byId("secret-value").textContent = result.key;
    byId("one-time-secret").hidden = false;
    form.reset();
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
    showToast(t`测试订单 ${order.id.slice(0, 8)} 已创建；需由测试回调入账`);
    await loadTopups();
  } catch (error) { showToast(error.message); }
});

function clearCheckoutDisplay() {
  state.lastCheckout = null;
  byId("checkout-recovery").hidden = true;
  byId("checkout-result").textContent = "";
  byId("checkout-request-id").textContent = "";
  byId("resume-checkout").hidden = true;
  byId("resume-checkout").removeAttribute("href");
  [byId("checkout-form").querySelector("button"), byId("lookup-checkout"), byId("new-checkout")].forEach((button) => {button.disabled = false;});
}

function showCheckoutOrder(order, currentRequest = true) {
  state.lastCheckout = {order, currentRequest};
  byId("checkout-recovery").hidden = false;
  const guidance = {
    resume_checkout: t("订单等待付款。仅在确认未支付后继续原订单；付款后等待确认入账。"),
    wait_and_query: t("支付会话仍在创建，请稍后查询，不要重复购买。"),
    check_balance: t("本站已确认入账，请刷新余额与账本核对。"),
    review_order: t("请核对订单、余额和退款记录，再决定是否新购。"),
    contact_support: t("状态需要人工核对，请将订单编号提供给支持人员。"),
  };
  const destination = checkoutDestination(order);
  byId("checkout-result").textContent = t`订单：${order.id}\n状态：${order.status}\n现金：${order.payment_amount_minor ?? "—"} ${order.payment_currency || ""}（最小货币单位）\n服务额度：${amount(order.credit_amount_microusd)}\n${guidance[order.next_action] || (destination ? guidance.resume_checkout : guidance.contact_support)}`;
  const link = byId("resume-checkout");
  link.hidden = !destination;
  if (destination) link.href = destination;
  else link.removeAttribute("href");
  byId("checkout-request-id").textContent = currentRequest ? checkout.snapshot.key : t("—（按订单编号查询）");
  byId("lookup-checkout").hidden = !currentRequest || !checkout.snapshot.key;
}

async function runCheckout(operation) {
  if (checkout.snapshot.busy) return;
  // A previous payment link is not authoritative while a fresh lookup or
  // recovery is uncertain. Language changes must not resurrect that link.
  state.lastCheckout = null;
  const token = state.token;
  const buttons = [byId("checkout-form").querySelector("button"), byId("lookup-checkout"), byId("new-checkout")];
  buttons.forEach((button) => {button.disabled = true;});
  byId("resume-checkout").hidden = true;
  byId("resume-checkout").removeAttribute("href");
  try {
    const pending = operation();
    byId("checkout-recovery").hidden = false;
    byId("checkout-request-id").textContent = checkout.snapshot.key;
    byId("checkout-result").textContent = t("正在处理原购买请求…");
    const order = await pending;
    if (token !== state.token) return;
    showCheckoutOrder(order);
    await Promise.all([loadTopups(), loadBalanceAndLedger()]);
  } catch (error) {
    if (token === state.token) {
      byId("checkout-result").textContent = t`${error.message}\n请保留原请求编号并查询。未找到或超时不代表未创建；不要自动改用新编号购买。`;
      byId("lookup-checkout").hidden = !checkout.snapshot.key;
    }
  } finally { if (token === state.token) buttons.forEach((button) => {button.disabled = false;}); }
}

byId("checkout-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formData(event.currentTarget);
  await runCheckout(() => checkout.start(data.sku));
});

byId("lookup-checkout").addEventListener("click", () => runCheckout(() => checkout.lookup()));
byId("new-checkout").addEventListener("click", () => {
  if (!window.confirm(t("这不会取消原订单。请先确认原订单未支付或已处理；继续可能产生另一笔独立购买。确认？"))) return;
  try { checkout.forget(); clearCheckoutDisplay(); } catch (error) { showToast(error.message); }
});
byId("refresh-orders").addEventListener("click", () => Promise.all([loadTopups(), loadBalanceAndLedger()]).catch((error) => showToast(error.message)));

byId("console-language").addEventListener("click", async () => {
  const button = byId("console-language");
  button.disabled = true;
  setLanguage(language === "zh" ? "en" : "zh");
  const url = new URL(window.location.href);
  url.searchParams.set("lang", language);
  window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  renderStaticLocale(); renderLanguageLinks();
  byId("toast").hidden = true;
  if (state.lastCheckout) showCheckoutOrder(state.lastCheckout.order, state.lastCheckout.currentRequest);
  if (state.lastRequest) byId("request-detail").textContent = describeRequest(state.lastRequest);
  renderTestResult();
  try {
    await loadReady(true);
    if (state.token) await refreshConsole(true);
  } catch (error) { showToast(error.message); }
  finally {
    button.disabled = false;
  }
});

byId("forgot-toggle").addEventListener("click", () => {
  if (state.ready?.gateway_mode === "byok") return showToast(t("请联系交付负责人，核验身份后签发一次性恢复链接。"));
  byId("forgot-fields").hidden = !byId("forgot-fields").hidden;
  if (!byId("forgot-fields").hidden && state.ready && state.ready.captcha_required) renderCaptcha("forgot");
});

byId("forgot-submit").addEventListener("click", async () => {
  const body = { email: byId("forgot-email").value };
  if (!body.email) return showToast(t("请填写找回邮箱"));
  if (state.ready && state.ready.captcha_required) {
    if (!state.captchaTokens.forgot) return showToast(t("请先完成人机验证"));
    body.captcha_token = state.captchaTokens.forgot;
  }
  try {
    await api("/auth/forgot-password", { method: "POST", body, auth: false });
    showToast(t("如账户存在且可用，重置邮件将发送到该邮箱"));
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
  if (!body.email) return showToast(t("请填写注册邮箱"));
  if (state.ready && state.ready.captcha_required) {
    if (!state.captchaTokens.resend) return showToast(t("请先完成人机验证"));
    body.captcha_token = state.captchaTokens.resend;
  }
  try {
    await api("/auth/resend-verification", { method: "POST", body, auth: false });
    showToast(t("如账户存在且仍待验证，系统会重新发送验证邮件"));
  } catch (error) { showToast(error.message); }
  finally { if (state.ready && state.ready.captcha_required) resetCaptcha("resend"); }
});

byId("reset-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = state.identityToken;
  if (!token) return showToast(t("重置链接无效或已过期"));
  try {
    await api("/auth/reset-password", {
      method: "POST",
      body: { token, ...formData(event.currentTarget) },
      auth: false,
    });
    byId("reset-form").reset();
    window.history.replaceState({}, "", "/");
    state.identityToken = "";
    byId("recovery-shell").hidden = true;
    byId("auth-shell").hidden = false;
    showToast(t("密码已重置；旧会话和 API Key 已吊销"));
  } catch (error) { showToast(error.message); }
});

byId("budget-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formData(event.currentTarget);
  try {
    await api("/budgets", { method: "POST", body: { amount: Number(data.amount),
      kind: state.ready?.gateway_mode === "byok" ? "provider_spend_cap" : "prepaid_credit" } });
    showToast(t("新月度预算已生效；旧预算保留为历史记录"));
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
      showToast(t("邮箱验证完成，现在可以登录"));
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
