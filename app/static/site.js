"use strict";
(() => {
  const byId = (id) => document.getElementById(id);
  const english = new WeakMap();
  let lang = new URL(window.location.href).searchParams.get("lang") === "zh" ? "zh" : "en";
  let catalog = null;
  let failed = false;
  const text = (en, zh) => lang === "zh" ? zh : en;
  const set = (id, value) => { if (byId(id)) byId(id).textContent = value; };
  function translate() {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    document.querySelectorAll("[data-zh]").forEach((node) => {
      if (!english.has(node)) english.set(node, node.innerText || node.textContent);
      // Headings may contain visual line breaks; preserve their original DOM
      // on the first English render and use normal wrapping after switching.
      if (lang === "zh" || node.dataset.translated) {
        node.textContent = lang === "zh" ? node.dataset.zh : english.get(node);
        node.dataset.translated = "true";
      }
    });
    byId("language").textContent = lang === "zh" ? "English" : "中文";
    document.querySelectorAll("a[data-local]").forEach((link) => {
      const url = new URL(link.getAttribute("href"), window.location.origin);
      url.searchParams.set("lang", lang);
      link.setAttribute("href", url.pathname + url.search + url.hash);
    });
    render();
  }
  function render() {
    const status = failed ? text("Service configuration unavailable. Checkout status is unverified.", "无法读取服务配置，收款状态未确认。")
      : !catalog ? text("Reading service configuration…", "正在读取服务配置…")
      : catalog.environment !== "production" ? text("TEST ENVIRONMENT · No real checkout", "测试环境 · 不接受真实付款")
      : catalog.purchasing_enabled ? text("Checkout configured · Review published terms before purchase", "结账已配置 · 购买前请阅读正式条款")
      : text("Checkout is not enabled · No real purchases", "结账未启用 · 不接受真实购买");
    set("service-status", status);
    if (byId("model-prices")) {
      byId("model-prices").replaceChildren();
      for (const model of catalog?.models || []) {
        const row = document.createElement("tr");
        const money = (value) => new Intl.NumberFormat(lang === "zh" ? "zh-CN" : "en-US", {
          style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6,
        }).format(value / 1000000);
        for (const value of [model.id, money(model.input_microusd_per_million), money(model.output_microusd_per_million),
          new Intl.NumberFormat(lang === "zh" ? "zh-CN" : "en-US").format(model.max_output_tokens), `v${model.price_version}`]) {
          const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
        }
        byId("model-prices").append(row);
      }
      set("catalog-note", failed ? text("Prices unavailable. Refresh before making a purchase or call.", "无法加载价格。购买或调用前请先刷新核对。")
        : !catalog ? text("Loading prices…", "正在加载价格…")
        : catalog.environment !== "production" ? text("TEST PRICES ONLY — Not a commercial quote. No real checkout is enabled.", "仅测试价格 — 不是商业报价，未启用真实结账。")
        : catalog.models.length ? text("USD per 1,000,000 tokens. Input and output are billed separately.", "单位：每百万 Token 美元；输入与输出分别计费。")
        : text("No models are currently listed. Do not purchase credit for an unavailable model.", "当前没有已上架模型，请勿为不可用模型购买额度。"));
      set("catalog-timestamp", catalog ? text("Catalog read: ", "目录读取时间：") + new Date(catalog.fetched_at).toLocaleString(lang === "zh" ? "zh-CN" : "en-US") : "");
    }
    const model = catalog?.models?.[0]?.id || "REPLACE_WITH_LISTED_MODEL_ID";
    const body = JSON.stringify({model, messages: [{role: "user", content: "Reply with OK"}], max_tokens: 16});
    const shellBody = body.replace(/'/g, "'\\''");
    set("curl-example", `export KUNLUN_BASE_URL="${window.location.origin}"
export KUNLUN_GATEWAY_API_KEY="REPLACE_WITH_YOUR_GATEWAY_KEY"
export KUNLUN_REQUEST_ID="first-request-001"

curl "$KUNLUN_BASE_URL/v1/chat/completions" \\
  -H "Authorization: Bearer $KUNLUN_GATEWAY_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: $KUNLUN_REQUEST_ID" \\
  -d '${shellBody}'`);
    set("python-example", `import json, os, uuid, urllib.request

request_id = str(uuid.uuid4())  # Save before sending; reuse for retries.
payload = ${body}
request = urllib.request.Request(
    ${JSON.stringify(window.location.origin + "/v1/chat/completions")},
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": "Bearer " + os.environ["KUNLUN_GATEWAY_API_KEY"],
        "Content-Type": "application/json",
        "Idempotency-Key": request_id,
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=100) as response:
    print(response.read().decode("utf-8"))`);
    set("opencode-example", `baseURL: ${window.location.origin}/v1
apiKey: {env:KUNLUN_GATEWAY_API_KEY}
model: ${model}
plugin: bundled OpenCode idempotency plugin (required)`);
    set("recovery-example", `curl -X POST "${window.location.origin}/v1/requests/lookup" \\
  -H "Authorization: Bearer $KUNLUN_GATEWAY_API_KEY" \\
  -H "Idempotency-Key: $KUNLUN_REQUEST_ID"`);
    if (byId("policy-links")) {
      const links = byId("policy-links"); links.replaceChildren();
      for (const [url, label] of [[catalog?.terms_url, text("Terms of service ↗", "服务条款 ↗")], [catalog?.privacy_url, text("Privacy policy ↗", "隐私政策 ↗")]]) {
        if (!url) continue;
        try {
          const parsed = new URL(url);
          if (parsed.protocol !== "https:" || parsed.username || parsed.password) continue;
          const link = document.createElement("a"); link.href = parsed.href; link.rel = "noreferrer"; link.textContent = label; links.append(link);
        } catch { /* Invalid configuration is never converted into a link. */ }
      }
      set("policy-state", links.children.length === 2 ? text("Read the published policies before purchasing. Refund eligibility must be defined there.", "购买前阅读已发布条款；退款资格必须由其中的正式规则明确。")
        : text("Final policy links are incomplete or unavailable. This page is not a substitute for legal terms.", "正式条款链接未完整配置或无法读取。本页不能替代最终法律条款。"));
      set("support-contact", catalog?.support_email ? text("Support: ", "支持邮箱：") + catalog.support_email
        : text("No support contact is currently configured. Do not send sensitive information through an unverified channel.", "当前未配置支持联系方式。不要通过未经核验的渠道发送敏感资料。"));
    }
  }
  async function loadCatalog() {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 10000);
    const button = byId("refresh-catalog");
    if (button) button.disabled = true;
    try {
      const response = await fetch("/public/catalog", {cache: "no-store", signal: controller.signal});
      if (!response.ok) throw new Error("Catalog unavailable");
      const body = await response.json();
      if (!Array.isArray(body.models)) throw new Error("Invalid catalog");
      catalog = body; failed = false;
    } catch { catalog = null; failed = true; }
    finally { window.clearTimeout(timer); if (button) button.disabled = false; render(); }
  }
  byId("language").addEventListener("click", () => {
    lang = lang === "en" ? "zh" : "en";
    const url = new URL(window.location.href); url.searchParams.set("lang", lang);
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    translate();
  });
  byId("refresh-catalog")?.addEventListener("click", loadCatalog);
  document.querySelectorAll("[data-copy]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(byId(button.dataset.copy).textContent);
      set("copy-status", text("Example copied. Replace placeholders and review the price before running it.", "已复制示例。运行前请替换占位符并核对价格。"));
    } catch { set("copy-status", text("Clipboard unavailable. Select and copy the example manually.", "剪贴板不可用，请选中示例手动复制。")); }
  }));
  translate();
  loadCatalog();
})();
