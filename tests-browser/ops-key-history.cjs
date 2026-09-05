// Run against a fresh ops_fixture.py --key-history, never an external site.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1365, height: 1000}});
  const errors = [], writes = [];
  let holdPage = false, releasePage;
  try {
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, "http://127.0.0.1:8797");
      assert(!url.href.includes("not-an-id"), "full API key shape must not be sent as a query");
      if (request.method() === "POST") writes.push(url.pathname);
      if (holdPage && url.searchParams.get("key_offset") === "20") {
        holdPage = false;
        const response = await route.fetch();
        await new Promise(resolve => { releasePage = resolve; });
        await route.fulfill({response}); return;
      }
      await route.continue();
    });
    const info = await (await page.request.get("http://127.0.0.1:8797/__fixture__/operator?profile=write")).json();
    await page.goto("http://127.0.0.1:8797/ops/console");
    await page.locator("#operator-token").fill(info.token);
    await page.locator("#operator-login button").click();
    await page.locator("#desk").waitFor({state: "visible"});
    await page.locator("#modules button").filter({hasText: "Accounts & keys"}).click();
    await page.locator("#object-id").fill(info.key_history.user_id);
    await page.locator("#lookup button").click();
    await page.waitForFunction(() => document.querySelector("#key-page").textContent === "1–20 / 206");
    assert.equal(await page.locator("#key-previous").isDisabled(), true);
    await page.locator("#key-id-filter").fill("gw_example.not-an-id");
    await page.locator("#key-filter button[type=submit]").click();
    assert((await page.locator("#notice").textContent()).includes("not the full API key"));
    await page.locator("#key-id-filter").fill("");
    for (let i = 1; i <= 10; i++) {
      await page.locator("#key-next").click();
      await page.waitForFunction(offset => JSON.parse(document.querySelector("#snapshot").textContent || "{}").keys_pagination?.offset === offset, i * 20);
    }
    assert.equal(await page.locator("#key-page").textContent(), "201–206 / 206");
    assert.equal(await page.locator("#key-next").isDisabled(), true);
    assert((await page.locator("#snapshot").textContent()).includes("history-204"));
    await page.locator("#key-id-filter").fill("history-204");
    await page.locator("#key-filter button[type=submit]").click();
    await page.waitForFunction(() => document.querySelector("#key-page").textContent === "1–1 / 1");
    await page.locator("#action").selectOption({label: "freeze key History 204 (history-204)"});
    await page.locator("#reason").fill("Verified synthetic historical key for operator acceptance.");
    await page.locator("#action-form button").click();
    await page.locator("#confirmation").waitFor({state: "visible"});
    assert.equal(writes.length, 0);
    const prepared = await page.locator("#command-preview").textContent();
    await page.locator("#language").click();
    assert.equal(await page.locator("#command-preview").textContent(), prepared);
    await page.locator("#key-filter-clear").click();
    await page.waitForFunction(() => document.querySelector("#key-page").textContent === "1–20 / 206");
    assert.equal(await page.locator("#confirmation").isVisible(), false);
    await page.locator("#key-id-filter").fill("history-204");
    await page.locator("#key-filter button[type=submit]").click();
    await page.waitForFunction(() => document.querySelector("#key-page").textContent === "1–1 / 1");
    fs.mkdirSync("release-artifacts", {recursive: true});
    await page.locator(".inspection").screenshot({path: "release-artifacts/ops-key-history-desktop.png"});
    await page.setViewportSize({width: 390, height: 844});
    await page.locator("#key-history").scrollIntoViewIfNeeded();
    await page.screenshot({path: "release-artifacts/ops-key-history-mobile.png"});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator("#action").selectOption({label: "冻结 Key History 204 (history-204)"});
    await page.locator("#reason").fill("Explicit synthetic confirmation to freeze this historical key.");
    await page.locator("#action-form button").click();
    await page.locator("#confirm").click();
    await page.waitForFunction(() => document.querySelector("#action-form").hidden);
    assert.deepEqual(writes, ["/ops/keys/history-204/status"]);
    const checked = await (await page.request.get(`http://127.0.0.1:8797/ops/accounts/${info.key_history.user_id}?key_id=history-204`, {
      headers: {"X-Kunlun-Ops-Token": info.token}})).json();
    assert.equal(checked.keys[0].status, "frozen");
    await page.locator("#object-id").fill(info.key_history.user_id);
    await page.locator("#lookup button").click();
    // A late page response after logout must never restore customer data.
    await page.waitForFunction(() => document.querySelector("#key-page").textContent === "1–20 / 206");
    holdPage = true;
    await page.locator("#key-next").click();
    for (let i = 0; !releasePage && i < 250; i++) await page.waitForTimeout(20);
    assert(releasePage, "expected delayed pagination request");
    await page.locator("#logout").click();
    releasePage();
    await page.waitForTimeout(250);
    assert.equal(await page.locator("#key-history").isVisible(), false);
    assert.equal(await page.locator("#snapshot").textContent(), "");
    assert.equal(writes.length, 1);
    assert.deepEqual(errors, []);
    console.log("Historical keys: 206 records, 11 pages, exact ID, language stability, cancellation, logout race, mobile layout passed; one explicitly confirmed historical-key freeze.");
  } finally {
    if (releasePage) releasePage();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
