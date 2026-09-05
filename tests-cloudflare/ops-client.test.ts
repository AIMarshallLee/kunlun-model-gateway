import {describe, expect, it, vi} from "vitest";
// @ts-expect-error Browser-native module, no build-time dependency.
import {createOpsClient} from "../app/static/ops-client.js";

describe("operator transport", () => {
  it("keeps credentials on same-origin ops paths and does not expose them", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({expires_at: 300, scopes: ["console:read"]})));
    const client = createOpsClient({fetcher, now: () => 100000});
    await client.login("inert-short-lived-token");
    expect(fetcher.mock.calls[0][0]).toBe("/ops/session");
    expect(fetcher.mock.calls[0][1].redirect).toBe("error");
    expect(fetcher.mock.calls[0][1].credentials).toBe("same-origin");
    expect(JSON.stringify(client)).not.toContain("inert-short-lived-token");
    await expect(client.request("https://outside.example/ops/data")).rejects.toThrow();
    await expect(client.request("/ops/../auth/login")).rejects.toThrow();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not repeat a write or accept a late response after logout", async () => {
    let finish: (response: Response) => void = () => {};
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({expires_at: 300, scopes: []})))
      .mockImplementationOnce(() => new Promise((resolve) => {finish = resolve;}));
    const client = createOpsClient({fetcher, now: () => 100000});
    await client.login("inert");
    const pending = client.request("/ops/accounts/test/status", {method: "POST", body: {action: "freeze"}});
    await expect(client.request("/ops/accounts/test/status", {method: "POST", body: {action: "freeze"}})).rejects.toThrow("busy");
    client.logout();
    finish(new Response(JSON.stringify({status: "frozen"})));
    await expect(pending).rejects.toThrow("session");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("expires locally, invalidates on 401 and never retries unknown mutations", async () => {
    let clock = 100000;
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({expires_at: 120, scopes: []})))
      .mockRejectedValueOnce(new TypeError("network lost"));
    const client = createOpsClient({fetcher, now: () => clock});
    await client.login("inert");
    await expect(client.request("/ops/payments/test/refund", {method: "POST", body: {idempotency_key: "stable"}})).rejects.toThrow("unknown");
    expect(fetcher).toHaveBeenCalledTimes(2);
    clock = 120000;
    await expect(client.request("/ops/accounts")).rejects.toThrow("expired");
    expect(fetcher).toHaveBeenCalledTimes(2);
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({expires_at: 300, scopes: []})))
      .mockResolvedValueOnce(new Response("{}", {status: 401}));
    await client.login("second-inert");
    await expect(client.request("/ops/accounts")).rejects.toThrow("401");
    await expect(client.request("/ops/accounts")).rejects.toThrow("expired");
  });
});
