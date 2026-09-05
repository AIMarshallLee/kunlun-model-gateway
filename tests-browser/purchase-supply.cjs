// Run a fresh checkout_fixture.py --no-supply. All network remains loopback.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 390, height: 844}});
  const errors = [];
  try {
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, "https://gateway.example");
      const response = await page.request.fetch(`http://127.0.0.1:8796${url.pathname}${url.search}`, {
        method: request.method(), headers: request.headers(), data: request.postDataBuffer(),
      });
      await route.fulfill({response});
    });
    async function login() {
      await page.goto("https://gateway.example/console?lang=zh");
      await page.locator("#login-form [name=email]").fill("managed@example.com");
      await page.locator("#login-form [name=password]").fill("a sufficiently long password");
      await page.locator("#login-form button[type=submit]").click();
      await page.locator("#logout-button").waitFor({state: "visible"});
    }
    const supply = enabled => page.request.post(`http://127.0.0.1:8796/__fixture__/supply/${enabled}`);
    const paymentCalls = async () => (await (await page.request.get("http://127.0.0.1:8796/__fixture__/payment-calls")).json()).count;
    await login();
    await page.waitForFunction(() => document.querySelector("#payment-boundary").textContent.includes("新购买暂不可用"));
    assert.equal(await page.locator("#checkout-form").isVisible(), false);
    assert.equal(await page.locator("#new-checkout").isVisible(), false);
    assert.equal(await paymentCalls(), 0);
    await supply(true); await login();
    await page.locator("#checkout-form").waitFor({state: "visible"});
    // The page is now stale: only the server guard can prevent a new payment.
    await supply(false);
    await page.locator("#checkout-form button").click();
    await page.waitForFunction(() => document.querySelector("#checkout-result").textContent.includes("购买暂不可用"));
    const key = await page.locator("#checkout-request-id").innerText();
    assert.equal(await paymentCalls(), 0);
    await supply(true);
    await page.locator("#checkout-form button").click();
    await page.locator("#resume-checkout").waitFor({state: "visible"});
    assert.equal(await page.locator("#checkout-request-id").innerText(), key);
    assert.equal(await paymentCalls(), 1);
    await supply(false);
    await page.locator("#lookup-checkout").click();
    await page.locator("#resume-checkout").waitFor({state: "visible"});
    assert.equal(await paymentCalls(), 1);
    const webhook = await page.request.post("http://127.0.0.1:8796/billing/live/webhook", {data: "signed-provider-event"});
    assert.equal(webhook.status(), 200);
    await login();
    await page.waitForFunction(() => document.querySelector("#payment-boundary").textContent.includes("新购买暂不可用"));
    assert.equal(await page.locator("#checkout-form").isVisible(), false);
    await page.locator("#topup-list button").first().click();
    await page.waitForFunction(() => document.querySelector("#checkout-result").textContent.includes("paid"));
    assert.equal(await page.locator("#resume-checkout").isVisible(), false);
    fs.mkdirSync(path.resolve("release-artifacts"), {recursive: true});
    await page.locator("#payment-boundary").scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.resolve("release-artifacts/purchase-paused-mobile-zh.png")});
    await page.locator("#console-language").click();
    await page.waitForFunction(() => document.querySelector("#payment-boundary").textContent.includes("New purchases"));
    assert.equal(await page.locator("#checkout-form").isVisible(), false);
    await page.setViewportSize({width: 1365, height: 1000});
    await page.locator("#payment-boundary").scrollIntoViewIfNeeded();
    await page.screenshot({path: path.resolve("release-artifacts/purchase-paused-desktop-en.png")});
    assert.equal(await paymentCalls(), 1);
    assert.deepEqual(errors, []);
    console.log("PASS: paused purchase UI, stale-page server rejection, original-key recovery, one synthetic checkout, outage webhook/order access, EN/ZH and mobile.");
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
