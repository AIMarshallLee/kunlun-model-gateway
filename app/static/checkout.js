// Memory-only purchase state. A lost response is not permission to create a
// second payment intent. Clearing this state never cancels a server-side order.
export function createCheckoutFlow(request, origin, uuid = () => crypto.randomUUID()) {
  let key = "", sku = "", busy = false, generation = 0;
  async function send(path, options) {
    if (busy) throw new Error("已有购买请求正在处理，请等待或查询原订单。");
    busy = true;
    const current = generation;
    try {
      const result = await request(path, {...options, timeoutMs: 25000});
      if (current !== generation) throw new Error("购买会话已结束，请重新登录后查询订单。");
      return result;
    } finally { if (current === generation) busy = false; }
  }
  function clear() { generation += 1; key = ""; sku = ""; busy = false; }
  return {
    get snapshot() { return {key, sku, busy}; },
    async start(selectedSku) {
      if (busy) throw new Error("已有购买请求正在处理，请等待或查询原订单。");
      if (key && sku !== selectedSku) throw new Error("请先核对原购买请求，再明确开始新的购买。");
      if (!key) { key = uuid(); sku = selectedSku; }
      return send("/billing/checkout", {method: "POST", headers: {"Idempotency-Key": key},
        body: {sku, return_url: `${origin}/console`}});
    },
    async lookup() {
      if (!key) throw new Error("当前页面没有购买请求编号，请从订单列表查询。");
      return send("/billing/checkout/lookup", {method: "POST", headers: {"Idempotency-Key": key}});
    },
    forget() {
      if (busy) throw new Error("购买请求仍在处理中，暂不能开始另一笔购买。");
      clear();
    },
    clear,
  };
}

export function checkoutDestination(order) {
  if (order?.status !== "pending" || !order.checkout_url) return null;
  try {
    const url = new URL(order.checkout_url);
    if (url.protocol === "https:" && !url.username && !url.password) return url.href;
  } catch { /* Never navigate to an invalid checkout response. */ }
  return null;
}
