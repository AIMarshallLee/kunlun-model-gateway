// Run after checkout_fixture.py. Requires locally available Playwright/Chromium.
// No installed browser profile, external website, or real payment is used.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 390, height: 844}});
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let checkoutRequests = 0;
  let lostResponse = true;
  const keys = [];
  try {
    // The browser keeps the configured HTTPS origin. Every request is served by
    // the local fixture, preserving payloads and headers without a DNS lookup.
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      assert.equal(url.origin, "https://gateway.example", "unexpected external browser request");
      const response = await page.request.fetch(`http://127.0.0.1:8796${url.pathname}${url.search}`, {
        method: request.method(), headers: request.headers(), data: request.postDataBuffer(),
      });
      if (url.pathname === "/billing/checkout") {
        checkoutRequests += 1;
        keys.push(request.headers()["idempotency-key"]);
        if (lostResponse) {
          lostResponse = false;
          // Provider accepted the request, but the browser never got its result.
          await route.abort("failed");
          return;
        }
      }
      await route.fulfill({response});
    });
    await page.goto("https://gateway.example/console?lang=zh");
    await page.locator("#login-form [name=email]").fill("managed@example.com");
    await page.locator("#login-form [name=password]").fill("a sufficiently long password");
    await page.locator("#login-form button[type=submit]").click();
    await page.locator("#checkout-form").waitFor({state: "visible"});
    await page.locator("#checkout-form button").click();
    await page.waitForFunction(() => document.querySelector("#checkout-result").textContent.includes("请保留原请求编号"));
    const firstKey = await page.locator("#checkout-request-id").innerText();
    assert.ok(firstKey.length > 20);
    assert.equal(await page.locator("#resume-checkout").isVisible(), false);

    // An explicit same-purchase retry retains the original idempotency key.
    await page.locator("#checkout-form button").click();
    await page.locator("#resume-checkout").waitFor({state: "visible"});
    assert.equal(checkoutRequests, 2);
    assert.deepEqual(keys, [firstKey, firstKey]);
    const stats = await page.request.get("http://127.0.0.1:8796/__fixture__/payment-calls");
    assert.equal((await stats.json()).count, 1);
    assert.equal(await page.locator("#resume-checkout").getAttribute("href"), "https://pay.example.test/checkout/1");
    await page.locator("#lookup-checkout").click();
    await page.locator("#resume-checkout").waitFor({state: "visible"});
    assert.equal(checkoutRequests, 2, "lookup must not create a payment");
    await page.locator("#checkout-recovery").scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    fs.mkdirSync(path.resolve("release-artifacts"), {recursive: true});
    await page.screenshot({path: path.resolve("release-artifacts/checkout-recovery-mobile.png")});

    // Only the synthetic verified webhook credits this fixture, never a return URL.
    const webhook = await page.request.post("http://127.0.0.1:8796/billing/live/webhook", {data: "signed-provider-event"});
    assert.equal(webhook.status(), 200);
    await page.locator("#lookup-checkout").click();
    await page.waitForFunction(() => document.querySelector("#checkout-result").textContent.includes("状态：paid"));
    assert.equal(await page.locator("#resume-checkout").isVisible(), false);

    // A refresh destroys the session. A fresh login can recover by order ID.
    await page.reload();
    assert.equal(await page.locator("#auth-shell").isVisible(), true);
    await page.locator("#login-form [name=email]").fill("managed@example.com");
    await page.locator("#login-form [name=password]").fill("a sufficiently long password");
    await page.locator("#login-form button[type=submit]").click();
    await page.locator("#topup-list button").first().click();
    await page.waitForFunction(() => document.querySelector("#checkout-result").textContent.includes("状态：paid"));
    assert.equal(await page.locator("#lookup-checkout").isVisible(), false);
    assert.equal(await page.locator("#resume-checkout").isVisible(), false);
    await page.locator("#logout-button").click();
    await page.locator("#auth-shell").waitFor({state: "visible"});
    assert.equal(await page.locator("#checkout-result").innerText(), "");
    assert.equal(await page.locator("#resume-checkout").getAttribute("href"), null);
    assert.deepEqual(errors, []);
    console.log("PASS: response loss, original-key retry, one provider checkout, read-only recovery, webhook credit, refresh/login recovery, logout cleanup, mobile overflow.");
  } finally { await browser.close(); }
})().catch((error) => {console.error(error); process.exitCode = 1;});
