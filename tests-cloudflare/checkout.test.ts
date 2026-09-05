import {describe, expect, it, vi} from "vitest";
// @ts-expect-error Plain browser module is intentionally shared without bundling.
import {createCheckoutFlow, checkoutDestination} from "../app/static/checkout.js";

describe("customer checkout recovery", () => {
  it("reuses one identifier and frozen SKU after a lost response", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("network lost")).mockResolvedValue({id: "order-1"});
    const uuid = vi.fn().mockReturnValue("original-id");
    const flow = createCheckoutFlow(request, "https://gateway.example", uuid);
    await expect(flow.start("starter")).rejects.toThrow("network lost");
    expect(flow.snapshot.key).toBe("original-id");
    await flow.start("starter");
    expect(uuid).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[1][1]).toEqual(request.mock.calls[0][1]);
    await expect(flow.start("different")).rejects.toThrow();
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("blocks double submission and forget while in flight; stale lookup cannot restore state after logout", async () => {
    let finish: (value: unknown) => void = () => {};
    const request = vi.fn(() => new Promise((resolve) => {finish = resolve;}));
    const flow = createCheckoutFlow(request, "https://gateway.example", () => "original-id");
    const first = flow.start("starter");
    await expect(flow.start("starter")).rejects.toThrow();
    expect(() => flow.forget()).toThrow();
    flow.clear(); // Logout is allowed; an eventual response belongs to the old account.
    finish({id: "old-account-order"});
    await expect(first).rejects.toThrow();
    expect(flow.snapshot.key).toBe("");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("looks up without creating another checkout and resets only explicitly", async () => {
    const request = vi.fn().mockResolvedValue({id: "order-1"});
    const uuid = vi.fn().mockReturnValueOnce("original-id").mockReturnValueOnce("new-id");
    const flow = createCheckoutFlow(request, "https://gateway.example", uuid);
    await flow.start("starter");
    await flow.lookup();
    expect(request.mock.calls[1]).toEqual(["/billing/checkout/lookup", {method: "POST", headers: {"Idempotency-Key": "original-id"}, timeoutMs: 25000}]);
    flow.forget();
    await flow.start("another");
    expect(flow.snapshot.key).toBe("new-id");
    flow.clear();
    expect(flow.snapshot).toEqual({key: "", sku: "", busy: false});
  });

  it("only offers HTTPS payment links for pending orders, not settled or uncertain ones", () => {
    expect(checkoutDestination({status: "pending", checkout_url: "https://pay.example/session"})).toBe("https://pay.example/session");
    for (const status of ["paid", "pending_reconciliation", "checkout_requesting", "refunded"]) {
      expect(checkoutDestination({status, checkout_url: "https://pay.example/session"})).toBeNull();
    }
    for (const checkout_url of [null, "javascript:alert(1)", "https://user:secret@pay.example/session", "http://pay.example/session"]) {
      expect(checkoutDestination({status: "pending", checkout_url})).toBeNull();
    }
  });
});
