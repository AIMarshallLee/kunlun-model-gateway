// Synthetic loopback data only; financial inspection must issue no writes.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1365, height: 1000}});
  const errors = [], writes = [];
  try {
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", async route => {
      assert.equal(new URL(route.request().url()).origin, "http://127.0.0.1:8797");
      if (route.request().method() !== "GET") writes.push(route.request().url());
      await route.continue();
    });
    async function login(profile) {
      const info = await (await page.request.get(`http://127.0.0.1:8797/__fixture__/operator?profile=${profile}`)).json();
      await page.locator("#operator-token").fill(info.token); await page.locator("#operator-login button").click();
      await page.locator("#desk").waitFor({state: "visible"}); return info;
    }
    async function module(name) {
      await page.locator("#modules button").filter({hasText: new RegExp(`^${name}$`)}).click();
      await page.waitForFunction(() => document.querySelector("#page").textContent !== "");
    }
    async function inspect(id) {
      await page.locator("#object-id").fill(id); await page.locator("#lookup button").click();
      await page.waitForFunction(() => document.querySelector("#snapshot").textContent !== "");
    }
    await page.goto("http://127.0.0.1:8797/ops/console");
    let info = await login("limited");
    assert.equal(await page.locator("#modules button").filter({hasText: /^Chargeback returns$/}).count(), 0);
    await page.locator("#logout").click(); info = await login("read");
    await module("Chargeback returns");
    assert.equal(await page.locator("#records .record").count(), 3);
    await inspect(info.returns.pending.id);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    assert.equal(await page.locator('#financial-links [data-destination="chargebacks"]').count(), 0);
    assert.match(await page.locator("#return-state").innerText(), /Zero restored credit is not proof/);
    await page.locator('#financial-links [data-destination="orders"]').click();
    await page.waitForFunction(id => JSON.parse(document.querySelector("#snapshot").textContent || "{}").order?.id === id, info.returns.pending.order_id);
    await page.locator('#financial-links [data-destination="returns"]').click();
    await page.waitForFunction(() => document.querySelector("#page").textContent === "0–1 / 1");
    assert.equal(await page.locator("#return-order-id").inputValue(), info.returns.pending.order_id);
    await page.locator("#refresh").click();
    await page.waitForFunction(() => document.querySelector("#page").textContent === "0–1 / 1");
    await page.locator("#return-order-id").fill("unknown-order"); await page.locator('#return-filter button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector("#page").textContent === "0–0 / 0");
    assert.match(await page.locator("#notice").innerText(), /No records/);
    await page.locator("#return-filter-clear").click();
    await page.waitForFunction(() => document.querySelector("#page").textContent === "0–3 / 3");
    await inspect(info.returns.loss_reversed.id);
    assert.match(await page.locator("#object-facts").innerText(), /200 microUSD/);
    assert.match(await page.locator("#object-facts").innerText(), /800 microUSD/);
    await page.locator('#financial-links [data-destination="chargebacks"]').click();
    await page.waitForFunction(id => JSON.parse(document.querySelector("#snapshot").textContent || "{}").chargeback?.id === id, info.returns.loss_reversed.chargeback_id);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    await page.locator('#financial-links [data-destination="returns"]').click();
    await page.waitForFunction(() => document.querySelector("#page").textContent === "0–1 / 1");
    await page.locator("#records .record").click();
    await page.locator("#return-context").waitFor({state: "visible"});
    fs.mkdirSync(path.resolve("release-artifacts"), {recursive: true});
    await page.locator("#object-facts").scrollIntoViewIfNeeded();
    await page.screenshot({path: path.resolve("release-artifacts/ops-return-desktop.png")});
    await page.setViewportSize({width: 390, height: 844}); await page.locator("#language").click();
    assert.match(await page.locator("#return-state").innerText(), /不重复赠送/);
    await page.locator("#return-context").scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.resolve("release-artifacts/ops-return-mobile-zh.png")});
    await page.locator("#language").click(); await page.setViewportSize({width: 1365, height: 1000});
    const unsafePath = `http://127.0.0.1:8797/ops/chargeback-returns/${info.returns.applied.id}`;
    await page.route(unsafePath, async route => {
      const response = await route.fetch(), row = await response.json();
      row.restored_microusd = Number.MAX_SAFE_INTEGER + 1;
      row.provider_return_id = '<img src="x" onerror="alert(1)">';
      await route.fulfill({response, json: row});
    });
    await inspect(info.returns.applied.id);
    assert.match(await page.locator("#return-state").innerText(), /precision/);
    assert.equal(await page.locator("#object-facts img").count(), 0);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    await page.unroute(unsafePath);
    await page.locator("#logout").click(); info = await login("write");
    await module("Chargeback returns"); await inspect(info.returns.pending.id);
    await page.locator('#financial-links [data-destination="orders"]').click();
    await page.waitForFunction(id => JSON.parse(document.querySelector("#snapshot").textContent || "{}").order?.id === id, info.returns.pending.order_id);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    await module("Chargeback returns"); await inspect(info.returns.applied.id);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    assert.equal(await page.locator("#confirmation").isVisible(), false);
    // A delayed detail must not repopulate the desk after lock.
    let release, started;
    const received = new Promise(resolve => { started = resolve; });
    const blocked = new Promise(resolve => { release = resolve; });
    const responseReady = page.waitForResponse(unsafePath, {timeout: 10000});
    await page.route(unsafePath, async route => {
      const response = await route.fetch(), json = await response.json();
      started(); await blocked; await route.fulfill({response, json});
    });
    await page.locator("#lookup button").click(); await received;
    await page.locator("#logout").click(); release();
    const lateResponse = await responseReady;
    assert.equal(lateResponse.status(), 200);
    // The client rejects the stale session before consuming the response body.
    // Wait for rendering after response arrival, not body-consumption/networkidle.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.locator("#return-context").isVisible(), false);
    assert.equal(await page.locator("#snapshot").textContent(), "");
    assert.equal(await page.locator("#financial-links button").count(), 0);
    assert.equal(await page.locator("#return-order-id").inputValue(), "");
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    console.log("PASS: returns scope, read-only states, source links, order filter, units, bilingual/mobile, precision, escaping and stale-response logout.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
