// Local fixture only. No merchant, customer or production data.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1365, height: 1000}});
  const writes = [], errors = [];
  let lose = false;
  try {
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, "http://127.0.0.1:8797");
      if (request.method() === "POST") writes.push(request.postDataJSON());
      if (lose && request.method() === "POST") {
        lose = false; await route.fetch(); await route.abort("failed"); return;
      }
      await route.continue();
    });
    async function login(profile) {
      const info = await (await page.request.get(`http://127.0.0.1:8797/__fixture__/operator?profile=${profile}`)).json();
      await page.locator("#operator-token").fill(info.token);
      await page.locator("#operator-login button").click();
      await page.locator("#desk").waitFor({state: "visible"});
      return info;
    }
    async function inspect(id) {
      await page.locator("#object-id").fill(id); await page.locator("#lookup button").click();
      await page.waitForFunction(() => document.querySelector("#snapshot").textContent !== "");
    }
    async function module() {
      await page.locator("#modules button").filter({hasText: /^Chargebacks$/}).click();
      await page.waitForFunction(() => document.querySelector("#page").textContent !== "");
    }
    await page.goto("http://127.0.0.1:8797/ops/console");
    let info = await login("read");
    await module(); await inspect(info.chargebacks.recover);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    assert.match(await page.locator("#object-facts").innerText(), /800 microUSD/);
    await page.locator("#logout").click(); info = await login("write");
    await inspect("chargeback_risk");
    await page.locator("#alert-destination").click();
    await page.waitForFunction(() => document.querySelector("#module-title").textContent === "Chargebacks");
    await inspect(info.chargebacks.pending);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    assert.match(await page.locator("#chargeback-context").innerText(), /not zero cash loss/);
    await inspect(info.chargebacks.recovered);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    const unsafePath = `http://127.0.0.1:8797/ops/chargebacks/${info.chargebacks.recover}`;
    await page.route(unsafePath, async route => {
      const response = await route.fetch(), row = await response.json();
      row.outstanding_microusd = Number.MAX_SAFE_INTEGER + 1;
      await route.fulfill({response, json: row});
    });
    await inspect(info.chargebacks.recover);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    assert.match(await page.locator("#chargeback-context").innerText(), /precision/);
    await page.unroute(unsafePath);
    for (const [caseName, label] of [["recover", "Recover confirmed shortfall"], ["write_off", "Recover available and write off remainder"]]) {
      await inspect(info.chargebacks[caseName]);
      await page.locator("#action").selectOption({label});
      await page.locator("#reason").fill("Synthetic finance evidence reviewed; no real money.");
      const before = writes.length;
      await page.locator("#action-form button").click();
      await page.locator("#confirmation").waitFor({state: "visible"});
      const command = await page.locator("#command-preview").innerText();
      await page.locator("#language").click(); await page.locator("#language").click();
      assert.equal(await page.locator("#command-preview").innerText(), command);
      assert.equal(writes.length, before);
      if (caseName === "write_off") {
        fs.mkdirSync(path.resolve("release-artifacts"), {recursive: true});
        await page.locator("#chargeback-context").scrollIntoViewIfNeeded();
        await page.screenshot({path: path.resolve("release-artifacts/ops-chargeback-desktop.png")});
        await page.setViewportSize({width: 390, height: 844}); await page.locator("#language").click();
        await page.locator("#chargeback-context").scrollIntoViewIfNeeded();
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.screenshot({path: path.resolve("release-artifacts/ops-chargeback-mobile-zh.png")});
        await page.locator("#language").click(); await page.setViewportSize({width: 1365, height: 1000});
        lose = true;
      }
      await page.locator("#confirm").click();
      await page.waitForFunction(() => document.querySelector("#action-form").hidden);
      assert.equal(writes.length, before + 1);
      assert.equal(await page.locator("#confirm").isDisabled(), true);
      if (caseName === "write_off") {
        assert.match(await page.locator("#notice").innerText(), /unknown/);
        assert.equal(await page.locator("#command-preview").innerText(), command);
      }
      await inspect(info.chargebacks[caseName]);
      const row = JSON.parse(await page.locator("#snapshot").textContent()).chargeback;
      assert.equal(row.status, "resolved");
      assert.equal(row.written_off_microusd, caseName === "write_off" ? 800 : 0);
      assert.equal(await page.locator("#action-form").isVisible(), false);
    }
    await page.locator("#logout").click();
    assert.equal(await page.locator("#chargeback-context").isVisible(), false);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
    console.log("PASS: chargeback scopes, alert navigation, pending/recovered read-only, bilingual fixed confirmation, recover/write-off, lost response without repeat, mobile and logout.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
