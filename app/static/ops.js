import {createOpsClient} from "./ops-client.js";

const byId = (id) => document.getElementById(id);
const client = createOpsClient();
let language = "en", identity = null, active = null, offset = 0, selected = null, actions = [], command = null;
let epoch = 0, expiryTimer = null, executing = false;
let returnOrderFilter = "";
let commandSecret = "", channelQueryEpoch = 0;
const modules = [
  {id: "alerts", scope: "alerts:read", en: "Operational alerts", zh: "运营告警", path: "/ops/alerts", detail: "/ops/alerts/", field: "items"},
  {id: "notifications", scope: "alerts:read", en: "Notification records", zh: "通知投递记录", path: "/ops/notifications", field: "items"},
  {id: "accounts", scope: "accounts:read", en: "Accounts & keys", zh: "客户与 Key", path: "/ops/accounts", detail: "/ops/accounts/", field: "items"},
  {id: "orders", scope: "payments:read", en: "Orders & refunds", zh: "订单与退款", path: "/ops/orders", detail: "/ops/orders/", field: "items"},
  {id: "chargebacks", scope: "payments:read", en: "Chargebacks", zh: "拒付处理", path: "/ops/chargebacks", detail: "/ops/chargebacks/", field: "items"},
  {id: "returns", scope: "payments:read", en: "Chargeback returns", zh: "拒付资金返还", path: "/ops/chargeback-returns", detail: "/ops/chargeback-returns/", field: "items"},
  {id: "requests", scope: "reconciliation:read", en: "Model reconciliation", zh: "模型对账", path: "/ops/reconciliation", detail: "/ops/requests/", field: "requests"},
  {id: "models", scope: "models:read", en: "Models & retail prices", zh: "模型与售价", path: "/ops/models", detail: "/ops/models/", field: "items"},
  {id: "channels", scope: "channels:read", en: "Supply status", zh: "渠道状态", path: "/ops/channels", detail: "/ops/channels/", field: "catalog"},
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
  chargeback_risk: ["Chargeback shortfall or reconciliation", "拒付差额或待对账"],
  platform_budget: ["Platform cost budget threshold", "平台成本预算触阈"],
  supply_observation_failed: ["Supply state could not be read", "供给状态无法读取"],
  supply_unavailable: ["Listed models have no active supply", "已上架模型缺少启用供给"],
  price_below_supply: ["Retail price is below configured cost", "售价低于配置成本"],
};
const alertTitle = (id) => alertLabels[id]?.[language === "en" ? 0 : 1] || id;
const say = (en, zh) => language === "en" ? en : zh;
const has = (scope) => identity?.scopes.includes(scope);
const chargebackAmounts = ["payment_amount_minor", "credit_amount_microusd", "recovered_microusd", "outstanding_microusd", "written_off_microusd"];
const exactChargeback = (row) => chargebackAmounts.every((key) => Number.isSafeInteger(row[key]) && row[key] >= 0);
const returnAmounts = ["payment_amount_minor", "restored_microusd", "canceled_risk_microusd", "reversed_loss_microusd"];
const returnStatus = (status) => status === "applied" ? say("Restoration applied — account not auto-unfrozen", "返还已入账 — 不自动解冻") : status === "pending_reconciliation" ? say("Reconciliation required", "待对账") : status;
const chargebackStatus = (status) => ({risk: say("Confirmed shortfall", "已确认差额"),
  pending_reconciliation: say("Reconciliation required", "待对账"), recovered: say("Original credit recovered", "原额度已追回"),
  resolved: say("Shortfall disposed — account not auto-unfrozen", "差额已处置 — 不自动解冻")})[status] || status;
const notice = (text) => { byId("notice").textContent = text; };
const showJSON = (id, data) => { byId(id).textContent = JSON.stringify(data, null, 2); };
function setActionBusy(value) {
  executing = value;
  document.querySelectorAll("#action-form input, #action-form textarea, #action-form select, #action-form button").forEach((element) => { element.disabled = value; });
  byId("cancel").disabled = value;
  byId("channel-operation-lookup").querySelectorAll("input,button").forEach((element) => { element.disabled = value; });
  byId("key-history").querySelectorAll("input,button").forEach((element) => { element.disabled = value; });
}
function cancelCommand() { command = null; commandSecret = ""; byId("confirmation").hidden = true; byId("command-preview").textContent = ""; }
function clearSelection() {
  selected = null; actions = []; cancelCommand();
  byId("snapshot").textContent = ""; byId("result").textContent = "";
  byId("object-facts").replaceChildren();
  byId("key-history").hidden = true; byId("key-filter").reset(); byId("key-page").textContent = "";
  byId("alert-context").hidden = true;
  byId("chargeback-context").hidden = true; byId("chargeback-state").textContent = "";
  byId("return-context").hidden = true; byId("return-state").textContent = "";
  byId("financial-links").replaceChildren();
  byId("channel-context").hidden = true; byId("channel-state").textContent = "";
  channelQueryEpoch += 1; byId("channel-operation-result").textContent = "";
  byId("action-form").hidden = true; byId("action-form").reset(); byId("object-id").value = "";
}
function lock() {
  client.logout(); identity = null; active = null; epoch += 1; setActionBusy(false); clearTimeout(expiryTimer);
  clearSelection(); byId("records").replaceChildren(); byId("modules").replaceChildren();
  returnOrderFilter = ""; byId("return-order-id").value = ""; byId("return-filter").hidden = true;
  byId("channel-operation-id").value = ""; byId("channel-operation-lookup").hidden = true;
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
    button.addEventListener("click", () => { active = item; offset = 0; returnOrderFilter = ""; load(); }); return button;
  }));
  if (active) byId("module-title").textContent = active[language];
  renderFacts();
  renderActions();
}
function renderFacts() {
  byId("key-history").hidden = selected?.kind !== "accounts";
  if (!selected) return;
  const {data, kind} = selected, row = data.account || data.order || data.request || data.model || data.alert || data.chargeback || data.return || data.channel;
  const facts = [[say("Object ID", "对象 ID"), row.id], [say("Status", "状态"), kind === "models" ? (row.active ? say("Listed", "已上架") : say("Unlisted", "已下架")) : row.status]];
  byId("alert-context").hidden = kind !== "alerts";
  byId("chargeback-context").hidden = kind !== "chargebacks";
  byId("return-context").hidden = kind !== "returns";
  byId("channel-context").hidden = kind !== "channels";
  byId("general-action-context").hidden = ["chargebacks", "returns", "channels"].includes(kind);
  if (kind === "channels") {
    facts[0][1] = row.provider;
    facts[1][1] = ({enabled: say("Enabled credential — health unverified", "凭据已启用 — 健康未验证"),
      disabled: say("Disabled", "已禁用"), unconfigured: say("Not configured", "尚未配置"),
      pending_cleanup: say("Disabled — Vault cleanup pending", "已禁用 — Vault 清理待处理")})[row.status] || row.status;
    facts.push([say("Credential version", "凭据版本"), Number.isSafeInteger(row.version) ? row.version : say("Verify exact version", "请核对精确版本")],
      [say("Channel ID", "渠道 ID"), row.id || "—"], [say("Configured priority", "配置优先级"), row.priority],
      [say("Allowed upstream host", "允许的上游主机"), row.upstream_host],
      [say("Configured models", "配置模型"), row.models.join(", ")]);
    byId("channel-state").textContent = row.pending_cleanup
      ? say("Credential use is disabled, but Vault cleanup is incomplete. Inspect the original operation before an explicitly approved cleanup attempt. Do not provision another key here yet.", "凭据已停止使用，但 Vault 清理未完成。先查原操作，再执行另行批准的清理；此状态暂不配置新密钥。")
      : say("Configuration is not a health probe or supplier approval. Priority and models come from the server allowlist. A submitted version is a snapshot, not a version lock; coordinate concurrent operators and inspect current state after every change.", "配置不代表健康探测或供应商批准。优先级和模型来自服务端允许目录。所见版本是快照，不是版本锁；请协调并行运维，每次变更后核查当前状态。");
  }
  const links = [];
  const related = (en, zh, destination, objectId = null, orderFilter = "") => {
    if (!modules.some((item) => item.id === destination && has(item.scope))) return;
    const button = document.createElement("button"); button.type = "button"; button.textContent = say(en, zh);
    button.dataset.destination = destination; button.disabled = executing;
    button.addEventListener("click", () => navigateRelated(destination, objectId, orderFilter)); links.push(button);
  };
  if (["chargebacks", "returns"].includes(kind)) related("Inspect source order", "核查原订单", "orders", row.order_id);
  if (kind === "returns" && row.chargeback_id) related("Inspect linked chargeback", "核查关联拒付", "chargebacks", row.chargeback_id);
  if (["orders", "chargebacks"].includes(kind)) related("View returns for this order", "查看此订单的返还", "returns", null, kind === "orders" ? row.id : row.order_id);
  byId("financial-links").replaceChildren(...links);
  if (kind === "returns") {
    const amount = (field, unit = "microUSD") => Number.isSafeInteger(row[field]) && row[field] >= 0 ? `${row[field]} ${unit}` : say("Cannot display exactly — verify ledger", "无法精确展示 — 请核对账本");
    facts[1][1] = returnStatus(row.status);
    facts.push([say("Source order", "原订单"), row.order_id], [say("Customer ID", "客户 ID"), row.user_id],
      [say("Payment provider / dispute", "支付渠道 / 争议号"), `${row.provider} / ${row.provider_dispute_id}`],
      [say("Provider funds-return ID", "支付方资金返还号"), row.provider_return_id],
      [say("Linked chargeback", "关联拒付"), row.chargeback_id || say("Not matched", "尚未匹配")],
      [say("Returned cash principal (minor units)", "返还现金本金（最小货币单位）"), amount("payment_amount_minor", `${row.payment_currency} ${say("minor units", "最小单位")}`)],
      [say("Restored available credit", "已恢复可用额度"), amount("restored_microusd")],
      [say("Canceled outstanding risk", "已取消风险差额"), amount("canceled_risk_microusd")],
      [say("Reversed platform loss", "已冲回平台损失"), amount("reversed_loss_microusd")],
      [say("Risk reason", "风险原因"), row.risk_reason || "—"],
      [say("Recorded at (UTC)", "记录时间（UTC）"), row.created_at],
      [say("Applied at (UTC)", "入账时间（UTC）"), row.applied_at || "—"]);
    byId("return-state").textContent = !returnAmounts.every((field) => Number.isSafeInteger(row[field]) && row[field] >= 0)
      ? say("An amount exceeds safe browser integer precision. Verify the exact ledger using approved tooling; rounded browser metadata is not financial evidence.", "金额超出浏览器安全整数精度。请用受控工具核对精确账本，浏览器舍入后的元数据不能作为财务依据。")
      : row.status === "applied"
        ? say("The recorded return has been applied to the credit ledger. Only previously recovered credit was restored; consumed credit is not granted again. This does not unfreeze the account or revive keys.", "此返还已按记录计入额度账本。仅补回此前实际追回的额度，不重复赠送已消费额度；不会解冻账户或恢复旧 Key。")
        : say("Reconciliation required. Zero restored credit is not proof that no cash returned. Verify the original payment, debit and return records; do not issue another refund or manually add credit.", "此记录待对账。恢复额度为零，不代表现金未返还；请核对原支付、扣款及返还记录，不要再发起退款或手工加额度。");
  }
  if (kind === "chargebacks") {
    const amount = (field, unit = "microUSD") => Number.isSafeInteger(row[field]) && row[field] >= 0 ? `${row[field]} ${unit}` : say("Cannot display exactly — do not act", "无法精确展示 — 禁止操作");
    facts[1][1] = chargebackStatus(row.status);
    facts.push([say("Source order", "原订单"), row.order_id], [say("Customer ID", "客户 ID"), row.user_id],
      [say("Payment provider / dispute", "支付渠道 / 争议号"), `${row.provider} / ${row.provider_dispute_id}`],
      [say("Cash debit principal (minor units)", "现金扣款本金（最小货币单位）"), amount("payment_amount_minor", `${row.payment_currency} ${say("minor units", "最小单位")}`)],
      [say("Original service credit", "原购买服务额度"), amount("credit_amount_microusd")],
      [say("Recovered credit", "累计追回额度"), amount("recovered_microusd")],
      [say("Confirmed outstanding shortfall", "已确认未处置差额"), amount("outstanding_microusd")],
      [say("Written off as platform loss", "已核销为平台损失"), amount("written_off_microusd")],
      [say("Risk reason", "风险原因"), row.risk_reason || "—"]);
    byId("chargeback-state").textContent = !exactChargeback(row) ? say("An amount exceeds safe browser integer precision. Actions are blocked; verify the exact ledger using approved tooling.", "金额超出浏览器安全整数精度，已禁止操作；请用受控工具核对精确账本。") : row.status === "pending_reconciliation" ? say("Reconciliation required. Zero recorded credit shortfall is not zero cash loss. Partial or overlapping events cannot be written off here.", "此记录待对账。已记录额度差额为零，不等于现金损失为零；部分或重叠事件不能在此直接核销。") : row.status === "risk" ? say("Only confirmed shortfall can be disposed here. The server must verify that model holds are cleared and the ledger agrees; this page is a snapshot, not permission to bypass those checks.", "此处仅处理已确认差额。服务端仍须核验模型占用已清空、账本一致；页面快照不能绕过这些检查。") : say("No further financial action is available for this state. Account unfreeze is a separate audited decision and never restores old keys.", "此状态无后续财务操作。账户解冻须另行审核，不会恢复旧 Key。");
  }
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
  if (kind === "accounts") {
    const pagination = data.keys_pagination;
    byId("key-history").hidden = false;
    byId("key-id-filter").value = selected.keyId || "";
    byId("key-page").textContent = `${pagination.total ? pagination.offset + 1 : 0}–${pagination.offset + data.keys.length} / ${pagination.total}`;
    byId("key-previous").disabled = executing || pagination.offset === 0;
    byId("key-next").disabled = executing || pagination.offset + data.keys.length >= pagination.total;
    facts.push(
    [say("Customer", "客户"), row.email], [say("Email verified", "邮箱已验证"), row.email_verified_at ? say("Yes", "是") : say("No", "否")],
    [say("Available credit", "可用额度"), `${data.wallet?.balance_microusd ?? "—"} microUSD`],
    [say("Reserved credit", "占用额度"), `${data.wallet?.reserved_microusd ?? "—"} microUSD`],
    ["Keys", data.keys.map((key) => `${key.name} · ${key.status} · ••••${key.last_four}`).join(" / ") || "—"],
  );
  }
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
  byId("return-filter").hidden = active.id !== "returns";
  byId("return-order-id").value = returnOrderFilter;
  byId("channel-operation-lookup").hidden = active.id !== "channels";
  if (active.id !== "channels") byId("channel-operation-id").value = "";
  byId("records").replaceChildren(); byId("lookup").hidden = !active.detail;
  byId("next").disabled = true; byId("previous").disabled = true; byId("page").textContent = "";
  try {
    const paginated = ["accounts", "orders", "chargebacks", "returns", "requests", "models", "notifications", "audit"].includes(active.id);
    const query = paginated ? new URLSearchParams({limit: "20", offset: String(offset)}) : new URLSearchParams();
    if (active.id === "returns" && returnOrderFilter) query.set("order_id", returnOrderFilter);
    const data = await client.request(active.path + (query.size ? `?${query}` : ""));
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
      if (active.id === "channels") title.textContent = row.provider;
      summary.textContent = [row.status, row.action, row.model, row.created_at].filter(Boolean).join(" · ");
      if (active.id === "channels") {
        button.dataset.channelProvider = row.provider; summary.textContent = `${row.status} · v${row.version}`;
      }
      if (active.id === "alerts") {
        title.textContent = alertTitle(row.id); title.dataset.alertId = row.id;
        summary.textContent = `${row.severity} · ${row.count}`;
        button.dataset.severity = row.severity;
      }
      button.append(title, summary);
      button.addEventListener("click", () => active.detail ? inspect(active.id === "channels" ? row.provider : row.id || row.request_id) : showJSON("snapshot", row));
      return button;
    }));
    return true;
  } catch (error) { if (current === epoch) errorNotice(error); }
}
async function navigateRelated(destinationId, objectId, orderFilter) {
  if (executing) return;
  const destination = modules.find((item) => item.id === destinationId && has(item.scope));
  if (!destination) return;
  active = destination; offset = 0; returnOrderFilter = orderFilter;
  if (await load() && objectId) await inspect(objectId);
}
async function inspect(id, {keyOffset = 0, keyId = ""} = {}) {
  if (!active?.detail || executing) return;
  if (active.id === "accounts" && keyId && !/^[A-Za-z0-9_-]{1,64}$/.test(keyId)) {
    notice(say("Enter a Key ID, not the full API key.", "请输入 Key ID，不要输入完整 API 密钥。")); return;
  }
  const current = ++epoch; clearSelection(); notice(""); byId("object-id").value = id;
  try {
    const query = active.id === "accounts" ? "?" + new URLSearchParams({key_limit: "20", key_offset: String(keyOffset), ...(keyId ? {key_id: keyId} : {})}) : "";
    const response = await client.request(active.detail + encodeURIComponent(id) + query);
    if (current !== epoch) return;
    const data = active.id === "chargebacks" ? {chargeback: response} : active.id === "returns" ? {return: response} : response;
    selected = {id, kind: active.id, data, keyId}; showJSON("snapshot", data); renderFacts(); renderActions();
    if (selected.kind === "channels") {
      const card = [...byId("records").children].find((element) => element.dataset.channelProvider === data.channel.provider);
      if (card) card.querySelector("small").textContent = `${data.channel.status} · v${data.channel.version}`;
    }
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
  if (kind === "channels" && has("channels:write")) {
    const row = data.channel, state = `${row.status}:v${row.version}`;
    if (!row.pending_cleanup) {
      add(row.active ? "Rotate platform key" : "Configure platform key", row.active ? "轮换平台密钥" : "配置平台密钥",
        `/ops/channels/${encodeURIComponent(row.provider)}`, {operation_id: crypto.randomUUID()}, row.provider, state);
      list.at(-1).needsSecret = true;
    }
    if (row.active || row.pending_cleanup) add(row.pending_cleanup ? "Retry approved Vault cleanup" : "Disable platform credential",
      row.pending_cleanup ? "重试已批准的 Vault 清理" : "禁用平台凭据", `/ops/channels/${encodeURIComponent(row.provider)}/revoke`,
      {operation_id: crypto.randomUUID()}, row.provider, state);
  }
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
  if (kind === "chargebacks" && has("payments:risk:write") && data.chargeback.status === "risk" && exactChargeback(data.chargeback) && data.chargeback.outstanding_microusd > 0) {
    const path = `/ops/chargebacks/${encodeURIComponent(id)}/risk-disposition`;
    add("Recover confirmed shortfall", "全额追回已确认差额", path,
      {action: "recover_available", idempotency_key: crypto.randomUUID()}, id, data.chargeback.status);
    add("Recover available and write off remainder", "追回可用额度并核销剩余差额", path,
      {action: "write_off", idempotency_key: crypto.randomUUID()}, id, data.chargeback.status);
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
    if (has("payments:write") && data.order.risk_reason !== "chargeback_return_reconciliation") {
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
  byId("channel-secret-field").hidden = !actions[Number(byId("action").value)]?.needsSecret;
  byId("channel-secret").disabled = executing || byId("channel-secret-field").hidden;
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
  if (destination) { active = destination; offset = 0; returnOrderFilter = ""; load(); }
});
byId("return-filter").addEventListener("submit", (event) => {
  event.preventDefault(); if (executing || active?.id !== "returns") return;
  returnOrderFilter = byId("return-order-id").value.trim(); offset = 0; load();
});
byId("return-filter-clear").addEventListener("click", () => {
  if (executing || active?.id !== "returns") return;
  returnOrderFilter = ""; offset = 0; load();
});
byId("channel-operation-lookup").addEventListener("submit", async (event) => {
  event.preventDefault(); if (executing || active?.id !== "channels") return;
  const operationId = byId("channel-operation-id").value.trim(), current = epoch, queryEpoch = ++channelQueryEpoch;
  byId("channel-operation-result").textContent = "";
  try {
    const result = await client.request(`/ops/channel-operations/${encodeURIComponent(operationId)}`);
    if (current === epoch && queryEpoch === channelQueryEpoch) showJSON("channel-operation-result", result);
  } catch (error) { if (current === epoch && queryEpoch === channelQueryEpoch) errorNotice(error); }
});
byId("refresh").addEventListener("click", load);
byId("previous").addEventListener("click", () => { offset = Math.max(0, offset - 20); load(); });
byId("next").addEventListener("click", () => { offset += 20; load(); });
byId("lookup").addEventListener("submit", (event) => { event.preventDefault(); inspect(byId("object-id").value.trim()); });
byId("key-filter").addEventListener("submit", (event) => {
  event.preventDefault(); if (executing || selected?.kind !== "accounts") return;
  inspect(selected.id, {keyId: byId("key-id-filter").value.trim()});
});
byId("key-id-filter").addEventListener("input", cancelCommand);
byId("key-filter-clear").addEventListener("click", () => {
  if (!executing && selected?.kind === "accounts") inspect(selected.id);
});
for (const [id, direction] of [["key-previous", -1], ["key-next", 1]]) byId(id).addEventListener("click", () => {
  if (executing || selected?.kind !== "accounts") return;
  const page = selected.data.keys_pagination;
  inspect(selected.id, {keyOffset: Math.max(0, page.offset + direction * page.limit), keyId: selected.keyId});
});
byId("action").addEventListener("change", () => { cancelCommand(); byId("channel-secret").value = ""; renderActions(); });
byId("reason").addEventListener("input", cancelCommand);
byId("usage-fields").addEventListener("input", cancelCommand);
byId("price-fields").addEventListener("input", cancelCommand);
byId("channel-secret").addEventListener("input", cancelCommand);
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
  if (action.needsSecret) {
    commandSecret = byId("channel-secret").value; byId("channel-secret").value = "";
    if (!commandSecret.trim()) { cancelCommand(); notice(say("Enter the approved platform key again.", "请重新输入已批准的平台密钥。")); return; }
    command.method = "PUT"; command.credential = say("Supplied separately; never displayed", "单独提供；不予展示");
  }
  if (selected?.kind === "chargebacks") command.observed_financials = {payment_currency: selected.data.chargeback.payment_currency,
    source_order_id: selected.data.chargeback.order_id, ...Object.fromEntries(chargebackAmounts.map((key) => [key, selected.data.chargeback[key]]))};
  showJSON("command-preview", command); byId("confirmation").hidden = false; byId("confirm").disabled = false;
});
byId("cancel").addEventListener("click", () => { cancelCommand(); byId("channel-secret").value = ""; });
byId("confirm").addEventListener("click", async () => {
  if (!command || executing) return;
  const current = epoch, frozenCommand = command;
  let secretForSend = commandSecret; commandSecret = "";
  if (selected?.kind === "channels") {
    byId("channel-operation-id").value = frozenCommand.body.operation_id;
    channelQueryEpoch += 1; byId("channel-operation-result").textContent = "";
  }
  setActionBusy(true); byId("confirm").disabled = true; renderChrome();
  try {
    const result = await client.request(frozenCommand.path, {method: frozenCommand.method || "POST",
      body: frozenCommand.method === "PUT" ? {...frozenCommand.body, secret: secretForSend} : frozenCommand.body});
    if (current !== epoch) return;
    showJSON("result", {command: frozenCommand, result});
    notice(say("Response recorded. Refresh the object before another action.", "已记录响应。下次操作前请刷新对象核查。"));
  } catch (error) { if (current === epoch) errorNotice(error); }
  finally {
    secretForSend = "";
    if (current === epoch) {
      setActionBusy(false); command = null; actions = []; byId("action-form").hidden = true;
      byId("confirm").disabled = true; byId("refresh").disabled = false;
      // Keep the submitted command/reference visible, including after timeout.
      byId("object-facts").replaceChildren(); byId("snapshot").textContent = "";
      byId("alert-context").hidden = true;
      byId("chargeback-context").hidden = true;
      byId("return-context").hidden = true; byId("financial-links").replaceChildren();
      byId("channel-context").hidden = true; byId("channel-secret").value = "";
      selected = null; renderChrome();
    }
  }
});
window.addEventListener("pagehide", lock);
renderChrome();
