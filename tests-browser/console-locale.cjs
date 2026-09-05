// Presentation + synthetic identity/payment acceptance on checkout_fixture.py.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 390, height: 844}});
  const errors = [], writes = [];
  const email = `locale-${Date.now()}@example.test`;
  const password = "synthetic locale password only";
  const modelText = "登录 <not-html> {0}";
  let failLookup = false;
  try {
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, "https://gateway.example");
      if (request.method() !== "GET") writes.push(url.pathname);
      if (url.pathname === "/billing/checkout/lookup" && failLookup) {
        failLookup = false;
        await route.fulfill({status: 503, json: {detail: "模拟查询暂不可用"}});
        return;
      }
      if (url.pathname === "/v1/chat/completions") {
        // This one response tests display preservation, NOT real model billing.
        await route.fulfill({json: {choices: [{message: {content: modelText}}]}});
        return;
      }
      const response = await page.request.fetch(`http://127.0.0.1:8796${url.pathname}${url.search}`, {
        method: request.method(), headers: request.headers(), data: request.postDataBuffer(),
      });
      await route.fulfill({response});
    });
    async function toggle(expected) {
      const previous = writes.length;
      await page.locator("#console-language").click();
      await page.waitForFunction(() => !document.querySelector("#console-language").disabled);
      assert.equal(await page.locator("html").getAttribute("lang"), expected);
      assert.equal(writes.length, previous, "language change must not repeat any mutation");
    }
    async function mailToken(kind) {
      const response = await page.request.get(`http://127.0.0.1:8796/__fixture__/latest-token?email=${encodeURIComponent(email)}&kind=${kind}`);
      const token = (await response.json()).token;
      assert.ok(token);
      return token;
    }
    async function signIn(currentPassword) {
      await page.locator("#login-form [name=email]").fill(email);
      await page.locator("#login-form [name=password]").fill(currentPassword);
      await page.locator("#login-form button[type=submit]").click();
      await page.locator("#console").waitFor({state: "visible"});
    }
    await page.goto("https://gateway.example/console");
    await page.waitForFunction(() => document.documentElement.lang === "en");
    const body = (await page.locator("body").innerText()).replace("中文", "");
    assert.equal(/[\u3400-\u9fff]/.test(body), false, "initial English UI has untranslated copy");
    await page.locator("#register-form [name=email]").fill(email);
    await page.locator("#register-form [name=password]").fill(password);
    await toggle("zh-CN"); await toggle("en");
    assert.equal(await page.locator("#register-form [name=email]").inputValue(), email);
    assert.equal(await page.locator("#register-form [name=password]").inputValue(), password);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    fs.mkdirSync(path.resolve("release-artifacts"), {recursive: true});
    await page.locator("#auth-shell").scrollIntoViewIfNeeded();
    await page.screenshot({path: path.resolve("release-artifacts/console-auth-en-mobile.png")});
    await page.locator("#register-form button[type=submit]").click();
    await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("verification email will be sent"));
    const verifyToken = await mailToken("email_verification");
    await page.goto(`https://gateway.example/verify-email#token=${verifyToken}`);
    await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Email verified"));
    assert.equal(new URL(page.url()).hash, "");
    // Invalid credentials remain a generic English message.
    await page.locator("#login-form [name=email]").fill(email);
    await page.locator("#login-form [name=password]").fill("incorrect password");
    await page.locator("#login-form button[type=submit]").click();
    await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("HTTP 401"));
    await signIn(password);
    await page.locator("#key-form [name=name]").fill("登录");
    await page.locator("#key-form summary").click();
    await page.locator("#key-form [name=allowed_models]").fill("test-model");
    await page.locator("#key-form [name=max_output_tokens]").fill("16");
    await page.locator("#key-form [name=spend_limit_microusd]").fill("5000");
    await toggle("zh-CN"); await toggle("en");
    assert.equal(await page.locator("#key-form [name=spend_limit_microusd]").inputValue(), "5000");
    const createdPolicy = page.waitForResponse((response) => response.url().endsWith("/v1/keys") && response.request().method() === "POST");
    await page.locator("#key-form button").click();
    const policy = await (await createdPolicy).json();
    assert.deepEqual(policy.allowed_models, ["test-model"]);
    assert.equal(policy.max_output_tokens, 16);
    assert.equal(policy.spend_limit_microusd, 5000);
    await page.locator("#one-time-secret").waitFor({state: "visible"});
    const key = await page.locator("#secret-value").innerText();
    await toggle("zh-CN"); await toggle("en");
    assert.ok(await page.locator("#key-list").innerText().then((text) => text.includes("登录")), "customer key name must stay unchanged");
    assert.equal(await page.locator("#secret-value").innerText(), key);
    await page.locator("#hide-secret").click();
    await page.waitForFunction(() => document.querySelector("#key-list").textContent.includes("Available: 5000"));
    await page.locator("#key-form").scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.resolve("release-artifacts/key-policy-en-mobile.png")});
    await toggle("zh-CN"); await toggle("en");
    assert.equal(await page.locator("#one-time-secret").isVisible(), false);
    assert.equal(await page.locator("#secret-value").innerText(), "");
    await page.locator("#budget-form button").click();
    await page.waitForFunction(() => document.querySelector("#budget-list").textContent.includes("Limit"));
    await page.locator("#model-test-form [name=gateway_key]").fill(key);
    await page.locator("#model-test-form button[type=submit]").click();
    await page.waitForFunction(() => document.querySelector("#model-test-result").textContent.includes("Model response received"));
    await toggle("zh-CN"); await toggle("en");
    assert.equal(await page.locator("#model-test-result").innerText(), `Model response received: ${modelText}`);
    assert.equal(await page.locator("#model-test-result not-html").count(), 0);
    await page.locator("#checkout-form button").click();
    await page.locator("#resume-checkout").waitFor({state: "visible"});
    const requestID = await page.locator("#checkout-request-id").innerText();
    await toggle("zh-CN"); await toggle("en");
    assert.equal(await page.locator("#checkout-request-id").innerText(), requestID);
    assert.ok((await page.locator("#checkout-result").innerText()).includes("Cash:"));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.locator("#checkout-recovery").scrollIntoViewIfNeeded();
    await page.screenshot({path: path.resolve("release-artifacts/console-purchase-en-mobile.png")});
    failLookup = true;
    await page.locator("#lookup-checkout").click();
    await page.waitForFunction(() => document.querySelector("#checkout-result").textContent.includes("HTTP 503"));
    await toggle("zh-CN"); await toggle("en");
    assert.equal(await page.locator("#resume-checkout").isVisible(), false, "language change must not restore a stale payment link after a failed query");
    await page.locator("#logout-button").click();
    await page.locator("#auth-shell").waitFor({state: "visible"});
    assert.equal(await page.locator("#model-test-result").innerText(), "");
    await page.locator("#forgot-toggle").click();
    await page.locator("#forgot-email").fill(email);
    await page.locator("#forgot-submit").click();
    await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("reset email will be sent"));
    const resetToken = await mailToken("password_reset");
    await page.goto(`https://gateway.example/reset-password#token=${resetToken}`);
    await page.locator("#reset-form").waitFor({state: "visible"});
    assert.equal(new URL(page.url()).hash, "");
    assert.ok((await page.locator("#recovery-shell").innerText()).includes("Revoke old credentials"));
    await page.locator("#reset-form [name=new_password]").fill(password + " changed");
    await toggle("zh-CN"); await toggle("en");
    assert.equal(await page.locator("#reset-form [name=new_password]").inputValue(), password + " changed");
    await page.locator("#reset-form button").click();
    await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Password reset"));
    await signIn(password + " changed");
    const revoked = await page.request.get("http://127.0.0.1:8796/v1/models", {headers: {Authorization: `Bearer ${key}`}});
    assert.equal(revoked.status(), 401);
    const noScript = await browser.newContext({javaScriptEnabled: false});
    try {
      const fallback = await noScript.newPage();
      await fallback.goto("http://127.0.0.1:8796/console");
      const submitted = [];
      fallback.on("request", (request) => submitted.push(request.url()));
      await fallback.locator("#login-form [name=email]").fill("noscript@example.test");
      await fallback.locator("#login-form [name=password]").fill("synthetic-noscript-password");
      const blocked = fallback.waitForEvent("console", {predicate: (message) => message.text().includes("form-action"), timeout: 5000});
      await fallback.locator("#login-form button[type=submit]").click({noWaitAfter: true});
      await blocked;
      // A second DOM interaction also verifies the blocked form did not navigate.
      assert.ok((await fallback.locator("noscript").innerText()).includes("native form submission is disabled"));
      assert.equal(new URL(fallback.url()).search, "");
      assert.deepEqual(submitted, [], "without JS, no native credential-bearing request may leave the page");
    } finally { await noScript.close(); }
    assert.deepEqual(errors, []);
    console.log("PASS: English/Chinese console; synthetic signup, email verification, password reset; form/key/answer preservation; no mutations on language switch; English purchase recovery; mobile layout.");
  } finally { await browser.close(); }
})().catch((error) => {console.error(error); process.exitCode = 1;});
