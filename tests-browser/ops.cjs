// Existing Playwright; local synthetic fixture only, no external browser session.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1365, height: 1000}});
  const errors = [], writes = [];
  let loseRefund = false, losePrice = false;
  try {
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, "http://127.0.0.1:8797", "operator token must never reach an external origin");
      if (request.method() === "POST") {
        writes.push(url.pathname);
        assert.equal(await page.locator("#reason").isDisabled(), true, "executing command fields must be immutable");
        assert.equal(await page.locator("#cancel").isDisabled(), true, "in-flight command reference must not be cleared");
      }
      if (loseRefund && url.pathname.endsWith("/refund")) {
        loseRefund = false; await route.fetch(); await route.abort("failed"); return;
      }
      if (losePrice && url.pathname.endsWith("/price")) {
        losePrice = false; await route.fetch(); await route.abort("failed"); return;
      }
      await route.continue();
    });
    async function login(profile) {
      const info = await (await page.request.get(`http://127.0.0.1:8797/__fixture__/operator?profile=${profile}`)).json();
      await page.locator("#operator-token").fill(info.token);
      await page.locator("#operator-login button").click();
      await page.locator("#desk").waitFor({state: "visible"});
      assert.equal(await page.locator("#operator-token").inputValue(), "");
      return info;
    }
    async function module(name) {
      await page.locator("#modules button").filter({hasText: name}).click();
      await page.waitForFunction(() => document.querySelector("#page").textContent !== "");
    }
    async function inspect(id) {
      await page.locator("#object-id").fill(id);
      await page.locator("#lookup button").click();
      await page.waitForFunction(() => document.querySelector("#snapshot").textContent !== "");
    }
    async function prepare(label) {
      const option = page.locator("#action option").filter({hasText: label}).first();
      await page.locator("#action").selectOption(await option.getAttribute("value"));
      await page.locator("#reason").fill("Verified using synthetic local acceptance evidence.");
      const before = writes.length;
      await page.locator("#action-form button").click();
      await page.locator("#confirmation").waitFor({state: "visible"});
      assert.equal(writes.length, before, "review must not execute the command");
    }
    async function execute() {
      const count = writes.length;
      await page.locator("#confirm").click();
      await page.waitForFunction(() => document.querySelector("#action-form").hidden);
      assert.equal(writes.length, count + 1, "one confirmation must produce exactly one write");
      assert.equal(await page.locator("#confirm").isDisabled(), true);
    }
    await page.goto("http://127.0.0.1:8797/ops/console");
    await login("read");
    await inspect("model_reconciliation");
    assert.equal(await page.locator("#action-form").isVisible(), false);
    await module("Accounts & keys");
    await page.locator("#records .record").first().click();
    await page.waitForFunction(() => document.querySelector("#snapshot").textContent !== "");
    assert.equal(await page.locator("#action-form").isVisible(), false);
    const account = JSON.parse(await page.locator("#snapshot").textContent()).account;
    await module("Models & retail prices");
    await page.locator("#records .record").first().click();
    await page.waitForFunction(() => document.querySelector("#snapshot").textContent !== "");
    const model = JSON.parse(await page.locator("#snapshot").textContent()).model;
    assert.equal(await page.locator("#action-form").isVisible(), false);
    await page.locator("#logout").click();
    const info = await login("write");
    await inspect("model_reconciliation");
    await prepare("Acknowledge observation");
    const alertPreview = await page.locator("#command-preview").innerText(), alertWrites = writes.length;
    await page.locator("#language").click(); await page.locator("#language").click();
    assert.equal(await page.locator("#command-preview").innerText(), alertPreview);
    assert.equal(writes.length, alertWrites);
    await execute(); await inspect("model_reconciliation");
    const receipt = JSON.parse(await page.locator("#snapshot").textContent()).alert;
    assert.equal(await page.locator("#action option:checked").innerText(), "Acknowledge observation (not resolve)");
    assert.equal(receipt.status, "attention"); assert.equal(receipt.count, 2);
    assert.equal(receipt.acknowledgement.actor, "synthetic-operator");
    fs.mkdirSync(path.resolve("release-artifacts"), {recursive: true});
    await page.screenshot({path: path.resolve("release-artifacts/ops-alert-desktop.png")});
    await page.setViewportSize({width: 390, height: 844});
    await page.locator("#language").click();
    await page.locator("#alert-context").scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.resolve("release-artifacts/ops-alert-mobile-zh.png")});
    await page.locator("#language").click();
    await page.setViewportSize({width: 1365, height: 1000});
    await page.locator("#alert-destination").click();
    await page.waitForFunction(() => document.querySelector("#module-title").textContent === "Model reconciliation");
    await module("Accounts & keys");
    await inspect(account.id);
    const state = JSON.parse(await page.locator("#snapshot").textContent());
    const key = state.keys[0];
    await prepare("freeze key");
    const preview = await page.locator("#command-preview").innerText(), beforeLanguage = writes.length;
    await page.locator("#language").click(); await page.locator("#language").click();
    assert.equal(await page.locator("#command-preview").innerText(), preview);
    assert.equal(writes.length, beforeLanguage);
    fs.mkdirSync(path.resolve("release-artifacts"), {recursive: true});
    await page.screenshot({path: path.resolve("release-artifacts/ops-review-desktop.png")});
    await page.locator("#confirmation").scrollIntoViewIfNeeded();
    await page.screenshot({path: path.resolve("release-artifacts/ops-confirm-desktop.png")});
    await execute(); await inspect(account.id);
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).keys.find((row) => row.id === key.id).status, "frozen");
    await prepare("unfreeze key"); await execute();
    await module("Model reconciliation");
    await inspect(info.requests["ops-release-case"]);
    await prepare("Release:"); await execute();
    await inspect(info.requests["ops-release-case"]);
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).request.cost_state, "released");
    await inspect(info.requests["ops-settle-case"]);
    await page.locator("#action").selectOption({label: "Settle verified usage and cost"});
    await page.locator("#input-tokens").fill("4"); await page.locator("#output-tokens").fill("2"); await page.locator("#upstream-cost").fill("3");
    await prepare("Settle verified"); await execute();
    await inspect(info.requests["ops-settle-case"]);
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).request.charged_microusd, 6);
    await module("Orders & refunds"); await inspect(info.order_id);
    await prepare("Request full refund"); loseRefund = true; await execute();
    await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("unknown"));
    assert.ok((await page.locator("#command-preview").innerText()).includes("idempotency_key"));
    await inspect(info.order_id);
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).order.status, "refunded");
    assert.equal((await (await page.request.get("http://127.0.0.1:8797/__fixture__/refund-calls")).json()).count, 1);
    await module("Models & retail prices"); await inspect(model.id);
    await page.locator("#retail-input").fill("2000000");
    await page.locator("#retail-output").fill("3000000");
    await prepare("Publish a new price");
    const pricePreview = await page.locator("#command-preview").innerText(), priceWrites = writes.length;
    await page.locator("#language").click(); await page.locator("#language").click();
    assert.equal(await page.locator("#retail-input").inputValue(), "2000000");
    assert.equal(await page.locator("#command-preview").innerText(), pricePreview);
    assert.equal(writes.length, priceWrites);
    await page.locator("#price-fields").scrollIntoViewIfNeeded();
    await page.screenshot({path: path.resolve("release-artifacts/ops-price-desktop.png")});
    losePrice = true; await execute();
    await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("unknown"));
    assert.equal(await page.locator("#command-preview").innerText(), pricePreview);
    await inspect(model.id);
    let prices = JSON.parse(await page.locator("#snapshot").textContent());
    assert.equal(prices.model.version, 2);
    assert.equal(prices.history.find((row) => row.version === 1).input_microusd_per_million, model.input_microusd_per_million);
    await page.locator("#retail-input").fill("-1"); // irrelevant invalid draft must not block unlisting
    await prepare("Unlist model"); await execute(); await inspect(model.id);
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).model.active, false);
    assert.deepEqual((await (await page.request.get("http://127.0.0.1:8797/public/catalog")).json()).models, []);
    await prepare("Publish a new price"); await execute(); await inspect(model.id);
    prices = JSON.parse(await page.locator("#snapshot").textContent());
    assert.equal(prices.model.version, 4);
    assert.equal(prices.model.active, true);
    await module("Accounts & keys"); await inspect(account.id); await prepare("freeze account"); await execute();
    await inspect(account.id); await prepare("unfreeze account"); await execute(); await inspect(account.id);
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).keys.find((row) => row.id === key.id).status, "revoked");
    await module("Notification records");
    const notificationWrites = writes.length;
    await page.locator("#records .record").first().click();
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).status, "accepted");
    assert.equal(await page.locator("#action-form").isVisible(), false);
    assert.ok((await page.locator("#notice").innerText()).includes("not inbox delivery"));
    assert.equal(writes.length, notificationWrites);
    await page.screenshot({path: path.resolve("release-artifacts/ops-notification-desktop.png")});
    await module("Audit trail");
    assert.ok(await page.locator("#records").innerText().then((text) => text.includes("account_unfreeze")));
    await page.setViewportSize({width: 390, height: 844});
    await page.locator("#language").click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.resolve("release-artifacts/ops-audit-mobile-zh.png")});
    await page.locator("#logout").click();
    assert.equal(await page.locator("#snapshot").textContent(), "");
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
    console.log("PASS: operator read/write scopes; review before write; alert receipt without resolution; bilingual immutable confirmation; key/account freeze; request release/settle; lost refund response -> one simulated refund; price versions/unlist/relist; audit; logout; mobile layout.");
  } finally { await browser.close(); }
})().catch((error) => {console.error(error); process.exitCode = 1;});
