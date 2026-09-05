// Local injected Vault only. Never use this script against a real credential service.
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
      const request = route.request();
      assert.equal(new URL(request.url()).origin, "http://127.0.0.1:8797");
      if (request.method() !== "GET") writes.push({method: request.method(), body: request.postDataJSON()});
      if (lose && request.method() === "PUT") {
        lose = false; await route.fetch(); await route.abort("failed"); return;
      }
      await route.continue();
    });
    const observation = async () => (await page.request.get("http://127.0.0.1:8797/__fixture__/channel-observation")).json();
    const before = await observation();
    async function login(profile) {
      const info = await (await page.request.get(`http://127.0.0.1:8797/__fixture__/operator?profile=${profile}`)).json();
      await page.locator("#operator-token").fill(info.token); await page.locator("#operator-login button").click();
      await page.locator("#desk").waitFor({state: "visible"});
      await page.waitForFunction(() => document.querySelector("#page").textContent !== "");
    }
    async function inspect(provider) {
      await page.locator("#object-id").fill(provider); await page.locator("#lookup button").click();
      await page.waitForFunction(() => document.querySelector("#snapshot").textContent !== "");
    }
    async function noSecret(secret) {
      assert.equal((await page.locator("body").innerText()).includes(secret), false);
      assert.equal(await page.locator("#channel-secret").inputValue(), "");
      assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    }
    async function prepare(label, secret) {
      await page.locator("#action").selectOption({label});
      await page.locator("#reason").fill("Approved synthetic channel operation; isolated fixture only.");
      if (secret) await page.locator("#channel-secret").fill(secret);
      await page.locator("#action-form button").click();
      await page.locator("#confirmation").waitFor({state: "visible"});
      const preview = await page.locator("#command-preview").innerText();
      if (secret) await noSecret(secret);
      await page.locator("#language").click(); await page.locator("#language").click();
      assert.equal(await page.locator("#command-preview").innerText(), preview);
      return JSON.parse(preview);
    }
    async function confirmAndQuery(command) {
      const count = writes.length;
      await page.locator("#confirm").click();
      await page.waitForFunction(() => document.querySelector("#action-form").hidden);
      assert.equal(writes.length, count + 1);
      assert.equal(await page.locator("#confirm").isDisabled(), true);
      assert.equal(await page.locator("#channel-operation-id").inputValue(), command.body.operation_id);
      await page.locator("#channel-operation-lookup button").click();
      await page.waitForFunction(() => document.querySelector("#channel-operation-result").textContent !== "");
      const record = JSON.parse(await page.locator("#channel-operation-result").textContent());
      assert.equal(record.operation_id, command.body.operation_id);
      assert.equal(record.provider, command.target);
    }
    await page.goto("http://127.0.0.1:8797/ops/console");
    await login("limited");
    assert.equal(await page.locator("#modules button").filter({hasText: /^Supply status$/}).count(), 0);
    await page.locator("#logout").click(); await login("channel_read");
    assert.equal(await page.locator("#records .record").count(), 2);
    await inspect("openai");
    assert.match(await page.locator("#object-facts").innerText(), /health unverified/);
    assert.equal(await page.locator("#action-form").isVisible(), false);
    await inspect("backup");
    assert.match(await page.locator("#object-facts").innerText(), /Not configured/);
    await page.locator("#logout").click(); await login("channel_write");
    await inspect("backup");
    await prepare("Configure platform key", "inert-cancelled-key");
    await page.locator("#cancel").click();
    assert.equal(writes.length, 0); await noSecret("inert-cancelled-key");
    const provision = await prepare("Configure platform key", "inert-backup-key");
    await confirmAndQuery(provision); await noSecret("inert-backup-key");
    assert.equal(writes[0].method, "PUT"); assert.equal(writes[0].body.secret, "inert-backup-key");
    await inspect("backup");
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).channel.version, 1);
    assert.equal(await page.locator('[data-channel-provider="backup"] small').innerText(), "enabled · v1");
    const rotate = await prepare("Rotate platform key", "inert-rotated-key");
    fs.mkdirSync(path.resolve("release-artifacts"), {recursive: true});
    await page.locator("#channel-context").scrollIntoViewIfNeeded();
    await page.screenshot({path: path.resolve("release-artifacts/ops-channel-desktop.png")});
    await page.setViewportSize({width: 390, height: 844}); await page.locator("#language").click();
    await page.locator("#confirmation").scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.resolve("release-artifacts/ops-channel-mobile-zh.png")});
    await page.locator("#language").click(); await page.setViewportSize({width: 1365, height: 1000});
    lose = true; await confirmAndQuery(rotate);
    assert.match(await page.locator("#notice").innerText(), /unknown/);
    assert.equal(writes.length, 2); await noSecret("inert-rotated-key");
    await inspect("backup");
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).channel.version, 2);
    const revoke = await prepare("Disable platform credential");
    await confirmAndQuery(revoke);
    assert.equal(writes[2].method, "POST"); assert.equal(writes[2].body.secret, undefined);
    await inspect("backup");
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).channel.status, "disabled");
    const detailPath = "http://127.0.0.1:8797/ops/channels/backup";
    await page.route(detailPath, async route => {
      const response = await route.fetch(), data = await response.json();
      data.channel.status = "pending_cleanup"; data.channel.pending_cleanup = true;
      await route.fulfill({response, json: data});
    });
    await inspect("backup");
    assert.equal(await page.locator("#action option").count(), 1);
    assert.match(await page.locator("#action option").innerText(), /cleanup/);
    assert.equal(await page.locator("#channel-secret").isVisible(), false);
    await page.unroute(detailPath); await inspect("backup");
    await page.locator("#channel-operation-id").fill("absent-operation");
    await page.locator("#channel-operation-lookup button").click();
    await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("404"));
    assert.match(await page.locator("#channel-operation-lookup").innerText(), /does not prove/);
    assert.equal(writes.length, 3);
    await prepare("Configure platform key", "inert-locked-key");
    await page.locator("#logout").click(); await noSecret("inert-locked-key");
    assert.equal(await page.locator("#command-preview").textContent(), "");
    assert.equal(await page.locator("#channel-operation-result").textContent(), "");
    assert.equal(await page.locator("#channel-operation-id").inputValue(), "");
    const after = await observation();
    assert.equal(after.model_calls, before.model_calls);
    assert.equal(after.operations, before.operations + 3);
    assert.deepEqual(errors, []);
    console.log("PASS: channel scopes, unconfigured catalog, secret-free preview/cancel/lock, provision/rotate/revoke, lost PUT without retry, original operation query, EN/ZH/mobile and no model calls.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
