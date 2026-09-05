import {createOpsClient} from "./ops-client.js";

const byId = (id) => document.getElementById(id);
const client = createOpsClient();
let language = "en", identity = null, active = null, offset = 0, selected = null, actions = [], command = null;
let epoch = 0, expiryTimer = null, executing = false;
const modules = [
  {id: "accounts", scope: "accounts:read", en: "Accounts & keys", zh: "客户与 Key", path: "/ops/accounts", detail: "/ops/accounts/", field: "items"},
  {id: "orders", scope: "payments:read", en: "Orders & refunds", zh: "订单与退款", path: "/ops/orders", detail: "/ops/orders/", field: "items"},
  {id: "requests", scope: "reconciliation:read", en: "Model reconciliation", zh: "模型对账", path: "/ops/reconciliation", detail: "/ops/requests/", field: "requests"},
  {id: "channels", scope: "channels:read", en: "Supply status", zh: "渠道状态", path: "/ops/channels", field: "channels"},
  {id: "budget", scope: "metrics:read", en: "Platform budget", zh: "平台预算", path: "/ops/platform-budget"},
  {id: "audit", scope: "audit:read", en: "Audit trail", zh: "操作审计", path: "/ops/audit", field: "items"},
];
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
  const {data, kind} = selected, row = data.account || data.order || data.request;
  const facts = [[say("Object ID", "对象 ID"), row.id], [say("Status", "状态"), row.status]];
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
    const paginated = ["accounts", "orders", "requests", "audit"].includes(active.id);
    const data = await client.request(active.path + (paginated ? `?limit=20&offset=${offset}` : ""));
    if (current !== epoch) return;
    const rows = active.field ? data[active.field] : [data];
    const total = data.pagination?.total ?? rows.length;
    byId("page").textContent = `${offset}–${offset + rows.length} / ${total}`;
    byId("previous").disabled = !paginated || offset === 0;
    byId("next").disabled = !paginated || offset + 20 >= total;
    if (!rows.length) notice(say("No records in this view.", "此视图暂无记录。"));
    byId("records").replaceChildren(...rows.map((row) => {
      const button = document.createElement("button"); button.className = "record"; button.type = "button";
      const title = document.createElement("strong"), summary = document.createElement("small");
      title.textContent = row.email || row.id || row.request_id || row.provider || row.period || active[language];
      summary.textContent = [row.status, row.action, row.model, row.created_at].filter(Boolean).join(" · ");
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
  } catch (error) { if (current === epoch) errorNotice(error); }
}
function buildActions() {
  if (!selected) return [];
  const {data, id, kind} = selected, list = [];
  const add = (en, zh, path, body, target, state, settle = false) => list.push({en, zh, path, body, target, state, settle});
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
  if (actions[Number(previous)]) byId("action").value = previous;
  byId("action-form").hidden = !actions.length;
  byId("usage-fields").hidden = !actions[Number(byId("action").value)]?.settle;
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
byId("language").addEventListener("click", () => { language = language === "en" ? "zh" : "en"; renderChrome(); });
byId("refresh").addEventListener("click", load);
byId("previous").addEventListener("click", () => { offset = Math.max(0, offset - 20); load(); });
byId("next").addEventListener("click", () => { offset += 20; load(); });
byId("lookup").addEventListener("submit", (event) => { event.preventDefault(); inspect(byId("object-id").value.trim()); });
byId("action").addEventListener("change", () => { cancelCommand(); byId("usage-fields").hidden = !actions[Number(byId("action").value)]?.settle; });
byId("reason").addEventListener("input", cancelCommand);
byId("usage-fields").addEventListener("input", cancelCommand);
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
      selected = null; renderChrome();
    }
  }
});
window.addEventListener("pagehide", lock);
renderChrome();
