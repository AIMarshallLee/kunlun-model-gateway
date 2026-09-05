import {createOpsClient} from "./ops-client.js";

const byId = (id) => document.getElementById(id);
const client = createOpsClient();
let language = "en", identity = null, active = null, offset = 0, selected = null, actions = [], command = null;
let epoch = 0, expiryTimer = null, executing = false;
const modules = [
  {id: "alerts", scope: "alerts:read", en: "Operational alerts", zh: "运营告警", path: "/ops/alerts", detail: "/ops/alerts/", field: "items"},
  {id: "notifications", scope: "alerts:read", en: "Notification records", zh: "通知投递记录", path: "/ops/notifications", field: "items"},
  {id: "accounts", scope: "accounts:read", en: "Accounts & keys", zh: "客户与 Key", path: "/ops/accounts", detail: "/ops/accounts/", field: "items"},
  {id: "orders", scope: "payments:read", en: "Orders & refunds", zh: "订单与退款", path: "/ops/orders", detail: "/ops/orders/", field: "items"},
  {id: "requests", scope: "reconciliation:read", en: "Model reconciliation", zh: "模型对账", path: "/ops/reconciliation", detail: "/ops/requests/", field: "requests"},
  {id: "models", scope: "models:read", en: "Models & retail prices", zh: "模型与售价", path: "/ops/models", detail: "/ops/models/", field: "items"},
  {id: "channels", scope: "channels:read", en: "Supply status", zh: "渠道状态", path: "/ops/channels", field: "channels"},
  {id: "budget", scope: "metrics:read", en: "Platform budget", zh: "平台预算", path: "/ops/platform-budget"},
  {id: "audit", scope: "audit:read", en: "Audit trail", zh: "操作审计", path: "/ops/audit", field: "items"},
];
const alertLabels = {
  model_reconciliation: ["Model costs need reconciliation", "模型成本待对账"],
  stale_reservations: ["Reservation lease expired", "预授权占用已超时"],
  payment_reconciliation: ["Payment outcome needs verification", "支付结果待核验"],
  refund_reconciliation: ["Refund outcome needs verification", "退款结果待核验"],
  payment_risk: ["Payment risk notes need review", "订单风险标记待核查"],
  refund_risk: ["Refund credit shortfall", "退款额度不足风险"],
  platform_budget: ["Platform cost budget threshold", "平台成本预算触阈"],
  supply_observation_failed: ["Supply state could not be read", "供给状态无法读取"],
  supply_unavailable: ["Listed models have no active supply", "已上架模型缺少启用供给"],
  price_below_supply: ["Retail price is below configured cost", "售价低于配置成本"],
};
const alertTitle = (id) => alertLabels[id]?.[language === "en" ? 0 : 1] || id;
const say = (en, zh) => language === "en" ? en : zh;
const has = (scope) => identity?.scopes.includes(scope);
const notice = (text) => { byId("notice").textContent = text; };
const showJSON = (id, data) => { byId(id).textContent = JSON.stringify(data, null, 2); };
function setActionBusy(value) {
  executing = value;
  document.querySelectorAll("#action-form input, #action-form textarea, #action-form select, #action-form button").forEach((element) => { element.disabled = value; });
  byId("cancel").disabled = value;
}
function cancelCommand() { command = null; byId("confirmation").hidden = true; byId("command-preview").textContent = ""; }
function clearSelection() {
  selected = null; actions = []; cancelCommand();
  byId("snapshot").textContent = ""; byId("result").textContent = "";
  byId("object-facts").replaceChildren();
  byId("alert-context").hidden = true;
  byId("action-form").hidden = true; byId("action-form").reset(); byId("object-id").value = "";
}
function lock() {
  client.logout(); identity = null; active = null; epoch += 1; setActionBusy(false); clearTimeout(expiryTimer);
  clearSelection(); byId("records").replaceChildren(); byId("modules").replaceChildren();
  byId("identity").textContent = ""; byId("environment").textContent = ""; byId("operator-token").value = "";
  byId("desk").hidden = true; byId("access").hidden = false;
}
function errorNotice(error) {
  if (/session changed|expired|HTTP 401/.test(error.message)) lock();
  notice(error.message);
}
function renderChrome() {
  document.documentElement.lang = language === "en" ? "en" : "zh-CN";
  document.querySelectorAll("[data-en]").forEach((element) => { element.textContent = element.dataset[language]; });
  byId("language").textContent = language === "en" ? "中文" : "English";
  if (!identity) return;
  byId("identity").textContent = `${identity.subject} · ${say("Expires", "到期")} ${new Date(identity.expires_at * 1000).toISOString()}`;
  byId("environment").textContent = `${identity.environment} / ${identity.mode} · ${say("Live-payment flag", "正式支付开关")}: ${identity.live_payments}. ${say("Configuration is not proof of merchant approval or a successful payment.", "配置不等于商户批准或真实支付成功。")}`;
  byId("modules").replaceChildren(...modules.filter((item) => has(item.scope)).map((item) => {
    const button = document.createElement("button"); button.type = "button"; button.textContent = item[language];
    if (active?.id === item.id) button.setAttribute("aria-current", "page");
    button.disabled = executing;
    button.addEventListener("click", () => { active = item; offset = 0; load(); }); return button;
  }));
  if (active) byId("module-title").textContent = active[language];
  renderFacts();
  renderActions();
}
function renderFacts() {
  if (!selected) return;
  const {data, kind} = selected, row = data.account || data.order || data.request || data.model || data.alert;
  const facts = [[say("Object ID", "对象 ID"), row.id], [say("Status", "状态"), kind === "models" ? (row.active ? say("Listed", "已上架") : say("Unlisted", "已下架")) : row.status]];
  byId("alert-context").hidden = kind !== "alerts";
  if (kind === "alerts") {
    facts.push([say("Condition", "告警条件"), alertTitle(row.id)], [say("Severity", "级别"), row.severity],
      [say("Affected count", "涉及数量"), row.count], [say("Observed at", "观察时间"), data.observed_at],
      [say("Matching aggregate receipt", "相同汇总的确认记录"), row.acknowledgement ? `${row.acknowledgement.actor} · ${row.acknowledgement.at}` : say("None", "无")]);
    byId("alert-destination").disabled = !modules.some((item) => item.id === row.destination && has(item.scope)) || executing;
  }
  if (kind === "models") facts.push(
    [say("Model", "模型"), row.model], [say("Price version", "售价版本"), `v${row.version}`],
    [say("Input / 1M tokens", "输入 / 百万 Token"), `${row.input_microusd_per_million} microUSD`],
    [say("Output / 1M tokens", "输出 / 百万 Token"), `${row.output_microusd_per_million} microUSD`],
    [say("Output token limit", "输出 Token 上限"), row.max_output_tokens],
    [say("Effective at (UTC)", "生效时间（UTC）"), row.effective_at],
  );
  if (kind === "accounts") facts.push(
    [say("Customer", "客户"), row.email], [say("Email verified", "邮箱已验证"), row.email_verified_at ? say("Yes", "是") : say("No", "否")],
    [say("Available credit", "可用额度"), `${data.wallet?.balance_microusd ?? "—"} microUSD`],
    [say("Reserved credit", "占用额度"), `${data.wallet?.reserved_microusd ?? "—"} microUSD`],
    ["Keys", data.keys.map((key) => `${key.name} · ${key.status} · ••••${key.last_four}`).join(" / ") || "—"],
  );
  if (kind === "orders") facts.push(
    [say("Customer ID", "客户 ID"), row.user_id], [say("Purchased credit", "购买额度"), `${row.credit_amount_microusd} microUSD`],
    [say("Cash amount (minor units)", "实付金额（最小货币单位）"), `${row.payment_amount_minor ?? "—"} ${row.payment_currency ?? "—"}`],
    [say("Payment provider", "支付渠道"), row.provider], [say("Refund state", "退款状态"), data.refunds.map((refund) => refund.status).join(", ") || "—"],
    [say("Risk", "风险"), row.risk_reason || "—"],
  );
  if (kind === "requests") facts.push(
    [say("Model", "模型"), row.requested_model], [say("Accounting state", "账务状态"), row.cost_state],
    [say("Customer charge", "客户扣费"), `${row.charged_microusd} microUSD`],
    [say("Upstream cost", "上游成本"), row.cost_state === "pending_reconciliation" ? say("Unconfirmed — inspect attempts", "待核验 — 请查逐次尝试") : `${row.upstream_cost_microusd} microUSD`],
    [say("Original reservation", "原始预授权"), `${row.reserved_microusd} microUSD`], [say("Failure", "失败原因"), row.failure_category || "—"],
  );
  byId("object-facts").replaceChildren(...facts.flatMap(([label, value]) => {
    const term = document.createElement("dt"), detail = document.createElement("dd"); term.textContent = label; detail.textContent = value; return [term, detail];
  }));
}
async function load() {
  if (!active || executing) return;
  const current = ++epoch; clearSelection(); notice(""); renderChrome();
  byId("records").replaceChildren(); byId("lookup").hidden = !active.detail;
  byId("next").disabled = true; byId("previous").disabled = true; byId("page").textContent = "";
  try {
    const paginated = ["accounts", "orders", "requests", "models", "notifications", "audit"].includes(active.id);
    const data = await client.request(active.path + (paginated ? `?limit=20&offset=${offset}` : ""));
    if (current !== epoch) return;
    const rows = active.field ? data[active.field] : [data];
    const total = data.pagination?.total ?? rows.length;
    byId("page").textContent = `${offset}–${offset + rows.length} / ${total}`;
    byId("previous").disabled = !paginated || offset === 0;
    byId("next").disabled = !paginated || offset + 20 >= total;
    if (!rows.length) notice(active.id === "alerts" ? say("No active conditions in evaluated rules. This is not readiness or notification-delivery proof.", "已评估规则暂无告警；这不证明生产就绪或通知送达。") : say("No records in this view.", "此视图暂无记录。"));
    if (active.id === "notifications") notice(say("Accepted = SMTP accepted, not inbox delivery. Unconfirmed may already have been sent. No automatic resend or web send button.", "accepted 只代表 SMTP 接受，不代表进入收件箱；unconfirmed 可能已发出。不自动重发，页面没有发信按钮。"));
    byId("records").replaceChildren(...rows.map((row) => {
      const button = document.createElement("button"); button.className = "record"; button.type = "button";
      const title = document.createElement("strong"), summary = document.createElement("small");
      title.textContent = row.email || row.model || row.id || row.request_id || row.provider || row.period || active[language];
      summary.textContent = [row.status, row.action, row.model, row.created_at].filter(Boolean).join(" · ");
      if (active.id === "alerts") {
        title.textContent = alertTitle(row.id); title.dataset.alertId = row.id;
        summary.textContent = `${row.severity} · ${row.count}`;
        button.dataset.severity = row.severity;
      }
      button.append(title, summary);
      button.addEventListener("click", () => active.detail ? inspect(row.id || row.request_id) : showJSON("snapshot", row));
      return button;
    }));
  } catch (error) { if (current === epoch) errorNotice(error); }
}
async function inspect(id) {
  if (!active?.detail || executing) return;
  const current = ++epoch; clearSelection(); notice(""); byId("object-id").value = id;
  try {
    const data = await client.request(active.detail + encodeURIComponent(id));
    if (current !== epoch) return;
    selected = {id, kind: active.id, data}; showJSON("snapshot", data); renderFacts(); renderActions();
    if (selected.kind === "models") {
      byId("retail-input").value = data.model.input_microusd_per_million;
      byId("retail-output").value = data.model.output_microusd_per_million;
      byId("retail-max-output").value = data.model.max_output_tokens;
    }
  } catch (error) { if (current === epoch) errorNotice(error); }
}
function buildActions() {
  if (!selected) return [];
  const {data, id, kind} = selected, list = [];
  const add = (en, zh, path, body, target, state, settle = false, price = false) => list.push({en, zh, path, body, target, state, settle, price});
  if (kind === "alerts" && has("alerts:write")) add("Acknowledge observation (not resolve)", "确认已知悉（不解除告警）",
    `/ops/alerts/${encodeURIComponent(id)}/ack`, {expected_revision: data.alert.revision, operation_id: crypto.randomUUID()}, id, data.alert.revision);
  if (kind === "models" && has("models:write")) {
    const row = data.model, path = `/ops/models/${encodeURIComponent(id)}/price`;
    const state = `${row.active ? "listed" : "unlisted"}:v${row.version}`;
    add("Publish a new price version", "发布新售价版本并上架", path,
      {action: "publish", expected_version: row.version, operation_id: crypto.randomUUID()}, row.model, state, false, true);
    if (row.active) add("Unlist model", "下架模型", path,
      {action: "unpublish", expected_version: row.version, operation_id: crypto.randomUUID()}, row.model, state);
  }
  if (kind === "accounts" && has("accounts:write")) {
    const account = data.account;
    if (["active", "frozen"].includes(account.status)) {
      const action = account.status === "active" ? "freeze" : "unfreeze";
      add(`${action} account`, `${action === "freeze" ? "冻结" : "解冻"}账户`, `/ops/accounts/${encodeURIComponent(id)}/status`, {action, expected_status: account.status}, id, account.status);
    }
    for (const key of data.keys) {
      if (!["active", "frozen"].includes(key.status)) continue;
      const action = key.status === "active" ? "freeze" : "unfreeze";
      add(`${action} key ${key.name} (${key.id})`, `${action === "freeze" ? "冻结" : "解冻"} Key ${key.name} (${key.id})`,
        `/ops/keys/${encodeURIComponent(key.id)}/status`, {action, expected_status: key.status}, key.id, key.status);
    }
  }
  if (kind === "orders") {
    if (has("payments:write")) {
      add("Query payment provider", "向支付方核查", `/ops/payments/${encodeURIComponent(id)}/reconcile`, {}, id, data.order.status);
      if (data.order.status === "paid" || data.refunds.some((item) => ["requesting", "retrying", "pending_reconciliation"].includes(item.status))) {
        const retry = data.refunds.find((item) => ["requesting", "retrying", "pending_reconciliation"].includes(item.status));
        add(retry ? "Retry original full refund" : "Request full refund", retry ? "重试原全额退款" : "申请全额退款",
          `/ops/payments/${encodeURIComponent(id)}/refund`, {idempotency_key: retry?.idempotency_key || crypto.randomUUID()}, id, data.order.status);
      }
    }
    if (has("payments:risk:write")) for (const refund of data.refunds.filter((item) => item.status === "risk")) {
      for (const action of ["recover_available", "write_off"]) add(`${action} refund risk`, `${action === "write_off" ? "核销" : "回收可用额度抵扣"}退款风险`,
        `/ops/refunds/${encodeURIComponent(refund.id)}/risk-disposition`, {action, idempotency_key: crypto.randomUUID()}, refund.id, refund.status);
    }
  }
  if (kind === "requests" && has("reconciliation:write") && data.request.status === "pending_reconciliation") {
    add("Release: verified not billed", "释放：已核实未计费", `/ops/reconciliation/${encodeURIComponent(id)}`, {action: "release"}, id, data.request.status);
    add("Settle verified usage and cost", "结算已核实用量与成本", `/ops/reconciliation/${encodeURIComponent(id)}`, {action: "settle"}, id, data.request.status, true);
  }
  return list;
}
function renderActions() {
  const previous = byId("action").value;
  // Rebuilding display must not replace an already prepared command/idempotency key.
  actions = buildActions();
  byId("action").replaceChildren(...actions.map((action, index) => {
    const option = document.createElement("option"); option.value = String(index); option.textContent = action[language]; return option;
  }));
  if (previous !== "" && actions[Number(previous)]) byId("action").value = previous;
  byId("action-form").hidden = !actions.length;
  byId("usage-fields").hidden = !actions[Number(byId("action").value)]?.settle;
  byId("price-fields").hidden = !actions[Number(byId("action").value)]?.price;
  // Hidden price fields must not block an unlist command via native validation.
  byId("price-fields").querySelectorAll("input").forEach((element) => { element.disabled = executing || byId("price-fields").hidden; });
}
byId("operator-login").addEventListener("submit", async (event) => {
  event.preventDefault(); const raw = byId("operator-token").value.trim(); byId("operator-token").value = "";
  lock(); notice(""); const current = epoch;
  const button = event.currentTarget.querySelector("button"); button.disabled = true;
  try {
    const info = await client.login(raw); if (current !== epoch) return;
    identity = info; byId("access").hidden = true; byId("desk").hidden = false;
    expiryTimer = setTimeout(() => { lock(); notice(say("Operator session expired.", "运维凭证已过期。")); }, Math.max(0, identity.expires_at * 1000 - Date.now()));
    active = modules.find((item) => has(item.scope)); offset = 0; renderChrome(); await load();
  } catch (error) { if (current === epoch) errorNotice(error); }
  finally { button.disabled = false; }
});
byId("logout").addEventListener("click", () => { lock(); notice(say("Desk locked. The issued token remains valid until its expiry.", "工作台已锁定；已签发凭证仍有效至到期时间。")); });
byId("language").addEventListener("click", () => {
  language = language === "en" ? "zh" : "en"; renderChrome();
  document.querySelectorAll("[data-alert-id]").forEach((element) => { element.textContent = alertTitle(element.dataset.alertId); });
});
byId("alert-destination").addEventListener("click", () => {
  if (executing || !selected?.data.alert) return;
  const destination = modules.find((item) => item.id === selected.data.alert.destination && has(item.scope));
  if (destination) { active = destination; offset = 0; load(); }
});
byId("refresh").addEventListener("click", load);
byId("previous").addEventListener("click", () => { offset = Math.max(0, offset - 20); load(); });
byId("next").addEventListener("click", () => { offset += 20; load(); });
byId("lookup").addEventListener("submit", (event) => { event.preventDefault(); inspect(byId("object-id").value.trim()); });
byId("action").addEventListener("change", () => { cancelCommand(); renderActions(); });
byId("reason").addEventListener("input", cancelCommand);
byId("usage-fields").addEventListener("input", cancelCommand);
byId("price-fields").addEventListener("input", cancelCommand);
byId("action-form").addEventListener("submit", (event) => {
  event.preventDefault(); if (executing) return;
  const action = actions[Number(byId("action").value)]; if (!action) return;
  const body = {...action.body, reason: byId("reason").value.trim()};
  if (action.settle) {
    for (const [field, id] of [["input_tokens", "input-tokens"], ["output_tokens", "output-tokens"], ["upstream_cost_microusd", "upstream-cost"]]) {
      const raw = byId(id).value, value = Number(raw);
      if (!raw || !Number.isSafeInteger(value) || value < 0) { notice(say("Verified usage and cost must be nonnegative integers.", "核实后的用量和成本必须为非负整数。")); return; }
      body[field] = value;
    }
  }
  if (action.price) {
    for (const [field, id] of [["input_microusd_per_million", "retail-input"], ["output_microusd_per_million", "retail-output"], ["max_output_tokens", "retail-max-output"]]) {
      const raw = byId(id).value, value = Number(raw);
      if (!raw || !Number.isSafeInteger(value) || value < 1) { notice(say("Retail prices and output limit must be positive integers.", "售价和输出上限必须为正整数。")); return; }
      body[field] = value;
    }
  }
  command = {path: action.path, body, target: action.target, observed_state: action.state};
  showJSON("command-preview", command); byId("confirmation").hidden = false; byId("confirm").disabled = false;
});
byId("cancel").addEventListener("click", cancelCommand);
byId("confirm").addEventListener("click", async () => {
  if (!command || executing) return;
  const current = epoch, frozenCommand = command;
  setActionBusy(true); byId("confirm").disabled = true; renderChrome();
  try {
    const result = await client.request(frozenCommand.path, {method: "POST", body: frozenCommand.body});
    if (current !== epoch) return;
    showJSON("result", {command: frozenCommand, result});
    notice(say("Response recorded. Refresh the object before another action.", "已记录响应。下次操作前请刷新对象核查。"));
  } catch (error) { if (current === epoch) errorNotice(error); }
  finally {
    if (current === epoch) {
      setActionBusy(false); command = null; actions = []; byId("action-form").hidden = true;
      byId("confirm").disabled = true; byId("refresh").disabled = false;
      // Keep the submitted command/reference visible, including after timeout.
      byId("object-facts").replaceChildren(); byId("snapshot").textContent = "";
      byId("alert-context").hidden = true;
      selected = null; renderChrome();
    }
  }
});
window.addEventListener("pagehide", lock);
renderChrome();
