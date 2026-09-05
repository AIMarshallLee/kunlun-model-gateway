// Same-origin, memory-only operator transport. Never exports credential state.
export function createOpsClient({fetcher = fetch, now = Date.now} = {}) {
  let token = "", expires = 0, generation = 0, writing = false;
  function logout() { token = ""; expires = 0; generation += 1; writing = false; }
  async function send(path, options, supplied, version) {
    if (!/^\/ops\/[A-Za-z0-9/_?=&%.:-]+$/.test(path) || path.includes("..") || /%2e|%2f|%5c/i.test(path)) {
      throw new Error("Invalid operator path");
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      // Same-origin cookies may authenticate the private SSO ingress. The
      // application still requires its separate scoped header, never a cookie.
      const response = await fetcher(path, {method: options.method || "GET", cache: "no-store", credentials: "same-origin",
        redirect: "error", signal: controller.signal,
        headers: {"X-Kunlun-Ops-Token": supplied, "Content-Type": "application/json"},
        body: options.body === undefined ? undefined : JSON.stringify(options.body)});
      if (version !== generation) throw new Error("Operator session changed");
      if (response.status === 401) logout();
      if (!response.ok) throw new Error(`HTTP ${response.status}; inspect current state before another action`);
      const result = await response.json();
      if (version !== generation) throw new Error("Operator session changed");
      return result;
    } catch (error) {
      if (version !== generation) throw new Error("Operator session changed / HTTP 401");
      if (error instanceof TypeError || error.name === "AbortError") {
        throw new Error(options.method && options.method !== "GET" ? "Operation result unknown; do not repeat. Read the original object and audit." : "Read failed; refresh to query again.");
      }
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function login(supplied) {
    logout();
    const version = generation;
    const info = await send("/ops/session", {}, supplied, version);
    if (!Number.isSafeInteger(info.expires_at) || info.expires_at * 1000 <= now()) throw new Error("Operator session expired");
    token = supplied; expires = info.expires_at * 1000;
    return info;
  }
  async function request(path, options = {}) {
    if (!token || expires <= now()) { logout(); throw new Error("Operator session expired"); }
    const write = options.method && options.method !== "GET";
    if (write && writing) throw new Error("Operator action busy");
    if (write) writing = true;
    const version = generation;
    try { return await send(path, options, token, version); }
    finally { if (version === generation && write) writing = false; }
  }
  return {login, logout, request};
}
