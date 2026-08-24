const CLIENT_IP_HEADER = "X-Kunlun-Client-IP";
const PROXY_SECRET_HEADER = "X-Kunlun-Proxy-Secret";

const REQUIRED_BINDINGS = [
  "KUNLUN_DATABASE_URL",
  "KUNLUN_API_KEY_PEPPER",
  "KUNLUN_SESSION_PEPPER",
  "KUNLUN_TRUSTED_PROXY_SECRET",
] as const;

export type RequiredBinding = (typeof REQUIRED_BINDINGS)[number];

export function missingRequiredBindings(
  values: Partial<Record<RequiredBinding, unknown>>,
): RequiredBinding[] {
  return REQUIRED_BINDINGS.filter((name) => {
    const value = values[name];
    if (typeof value !== "string") return true;
    if (name === "KUNLUN_DATABASE_URL") {
      try {
        const url = new URL(value);
        const sslModes = url.searchParams.getAll("sslmode");
        const sslMode = sslModes[0]?.toLowerCase();
        return (
          url.protocol !== "postgresql+psycopg:" ||
          !url.hostname ||
          !url.username ||
          url.pathname.length < 2 ||
          sslModes.length !== 1 ||
          !sslMode ||
          !["require", "verify-ca", "verify-full"].includes(sslMode)
        );
      } catch {
        return true;
      }
    }
    return value.length < 32;
  });
}

function decodedPath(pathname: string): string | null {
  let value = pathname;
  let stable = false;
  try {
    for (let count = 0; count < 8; count += 1) {
      const decoded = decodeURIComponent(value);
      if (decoded === value) {
        stable = true;
        break;
      }
      value = decoded;
    }
  } catch {
    return null;
  }
  if (!stable) return null;
  if ([...value].some((character) => character.charCodeAt(0) < 32)) return null;
  const segments: string[] = [];
  for (const segment of value.replaceAll("\\", "/").split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  return `/${segments.join("/")}`.toLowerCase();
}

export function publicRouteAllowed(pathname: string): boolean {
  const path = decodedPath(pathname);
  if (path === null) return false;
  if (path === "/metrics" || path.startsWith("/metrics/")) return false;
  return path !== "/ops" && !path.startsWith("/ops/");
}

export function prepareContainerRequest(
  request: Request,
  proxySecret: string,
  fallbackClientIp = "0.0.0.0",
): Request {
  const headers = new Headers(request.headers);
  const clientIp = headers.get("CF-Connecting-IP")?.trim() || fallbackClientIp;
  for (const name of [
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Port",
    "X-Forwarded-Proto",
    "X-Real-IP",
  ]) {
    headers.delete(name);
  }
  headers.set(CLIENT_IP_HEADER, clientIp);
  headers.set(PROXY_SECRET_HEADER, proxySecret);
  return new Request(request, { headers });
}
