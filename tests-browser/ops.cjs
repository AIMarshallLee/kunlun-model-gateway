// Existing Playwright; local synthetic fixture only, no external browser session.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1365, height: 1000}});
  const errors = [], writes = [];
  let loseRefund = false;
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
    await page.locator("#records .record").first().click();
    await page.waitForFunction(() => document.querySelector("#snapshot").textContent !== "");
    assert.equal(await page.locator("#action-form").isVisible(), false);
    const account = JSON.parse(await page.locator("#snapshot").textContent()).account;
    await page.locator("#logout").click();
    const info = await login("write");
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
    await module("Accounts & keys"); await inspect(account.id); await prepare("freeze account"); await execute();
    await inspect(account.id); await prepare("unfreeze account"); await execute(); await inspect(account.id);
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).keys.find((row) => row.id === key.id).status, "revoked");
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
    console.log("PASS: operator read/write scopes; review before write; bilingual immutable confirmation; key/account freeze; request release/settle; lost refund response -> one simulated refund; audit; logout; mobile layout.");
  } finally { await browser.close(); }
})().catch((error) => {console.error(error); process.exitCode = 1;});
