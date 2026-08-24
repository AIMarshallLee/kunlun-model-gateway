import { Container, getContainer } from "@cloudflare/containers";

import {
  missingRequiredBindings,
  prepareContainerRequest,
  publicRouteAllowed,
} from "./request";

interface GatewayEnv {
  GATEWAY_CONTAINER: DurableObjectNamespace<GatewayContainer>;
  KUNLUN_DATABASE_URL?: string;
  KUNLUN_API_KEY_PEPPER?: string;
  KUNLUN_SESSION_PEPPER?: string;
  KUNLUN_TRUSTED_PROXY_SECRET?: string;
}

function compactEnvironment(values: Record<string, string | undefined>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values).filter((entry): entry is [string, string] => {
      return typeof entry[1] === "string" && entry[1].length > 0;
    }),
  );
}

function containerEnvironment(env: GatewayEnv): Record<string, string> {
  return compactEnvironment({
    KUNLUN_ENV: "production",
    KUNLUN_PUBLIC_SIGNUP: "false",
    KUNLUN_ENABLE_TEST_PAYMENTS: "false",
    KUNLUN_LIVE_PAYMENTS: "false",
    KUNLUN_LIVE_UPSTREAM: "false",
    KUNLUN_DATABASE_URL: env.KUNLUN_DATABASE_URL,
    KUNLUN_API_KEY_PEPPER: env.KUNLUN_API_KEY_PEPPER,
    KUNLUN_SESSION_PEPPER: env.KUNLUN_SESSION_PEPPER,
    KUNLUN_TRUSTED_PROXY_SECRET: env.KUNLUN_TRUSTED_PROXY_SECRET,
  });
}

export class GatewayContainer extends Container<GatewayEnv> {
  defaultPort = 8787;
  requiredPorts = [8787];
  sleepAfter = "10m";
  pingEndpoint = "localhost/readyz";

  constructor(ctx: DurableObjectState<{}>, env: GatewayEnv) {
    super(ctx, env);
    this.envVars = containerEnvironment(env);
  }
}

const worker = {
  async fetch(request: Request, env: GatewayEnv): Promise<Response> {
    const pathname = new URL(request.url).pathname;
    if (!publicRouteAllowed(pathname)) {
      return new Response("Not Found", {
        status: 404,
        headers: { "Cache-Control": "no-store" },
      });
    }

    const missing = missingRequiredBindings(env);
    if (missing.length > 0) {
      return Response.json(
        { error: "deployment_not_configured", missing_bindings: missing },
        {
          status: 503,
          headers: { "Cache-Control": "no-store" },
        },
      );
    }

    const proxySecret = env.KUNLUN_TRUSTED_PROXY_SECRET as string;
    const clientIp = request.headers.get("CF-Connecting-IP")?.trim() || "0.0.0.0";
    const upstreamRequest = prepareContainerRequest(request, proxySecret, clientIp);
    return getContainer(env.GATEWAY_CONTAINER, "gateway-v1").fetch(upstreamRequest);
  },
  async scheduled(_controller: ScheduledController, env: GatewayEnv): Promise<void> {
    if (missingRequiredBindings(env).length > 0) return;
    await getContainer(env.GATEWAY_CONTAINER, "maintenance-v1").start({
      envVars: containerEnvironment(env),
      entrypoint: ["python", "-m", "scripts.maintenance", "--once"],
    });
  },
} satisfies ExportedHandler<GatewayEnv>;

export default worker;
