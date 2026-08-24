import { describe, expect, it } from "vitest";

import {
  deploymentUnavailableResponse,
  missingRequiredBindings,
  prepareContainerRequest,
  publicRouteAllowed,
} from "../src/request";

describe("publicRouteAllowed", () => {
  it.each([
    "/ops",
    "/ops/",
    "/ops/reconciliation",
    "/ops%2Freconciliation",
    "/%6f%70%73/reconciliation",
    "/%2Fops/reconciliation",
    "/foo/%2e%2e/ops/reconciliation",
    "/OPS/reconciliation",
    "/%25256f%252570%252573/reconciliation",
    "/metrics",
    "/metrics/",
    "/metrics/private",
  ])(
    "hides private route %s",
    (path) => expect(publicRouteAllowed(path)).toBe(false),
  );

  it.each(["/", "/healthz", "/readyz", "/v1/models", "/v1/chat/completions"])(
    "allows public route %s",
    (path) => expect(publicRouteAllowed(path)).toBe(true),
  );

  it("does not overmatch lookalike paths", () => {
    expect(publicRouteAllowed("/operations")).toBe(true);
    expect(publicRouteAllowed("/metrics-public")).toBe(true);
  });
});

describe("prepareContainerRequest", () => {
  it("overwrites untrusted forwarding headers with Cloudflare values", () => {
    const request = new Request("https://gateway.example/v1/models", {
      headers: {
        "CF-Connecting-IP": "203.0.113.9",
        "X-Kunlun-Client-IP": "198.51.100.2",
        "X-Kunlun-Proxy-Secret": "attacker-value",
        Forwarded: "for=198.51.100.2",
        "X-Forwarded-For": "198.51.100.2",
        "X-Forwarded-Host": "attacker.example",
        "X-Forwarded-Proto": "http",
        "X-Real-IP": "198.51.100.2",
      },
    });

    const prepared = prepareContainerRequest(request, "shared-secret-123", "198.51.100.1");

    expect(prepared.headers.get("X-Kunlun-Client-IP")).toBe("203.0.113.9");
    expect(prepared.headers.get("X-Kunlun-Proxy-Secret")).toBe("shared-secret-123");
    expect(prepared.headers.get("Forwarded")).toBeNull();
    expect(prepared.headers.get("X-Forwarded-For")).toBeNull();
    expect(prepared.headers.get("X-Forwarded-Host")).toBeNull();
    expect(prepared.headers.get("X-Forwarded-Proto")).toBeNull();
    expect(prepared.headers.get("X-Real-IP")).toBeNull();
  });

  it("falls back to the Worker supplied connecting address", () => {
    const request = new Request("https://gateway.example/healthz");
    const prepared = prepareContainerRequest(request, "shared-secret-123", "198.51.100.1");

    expect(prepared.headers.get("X-Kunlun-Client-IP")).toBe("198.51.100.1");
  });
});

describe("missingRequiredBindings", () => {
  it("requires PostgreSQL and three persisted secrets", () => {
    expect(missingRequiredBindings({})).toEqual([
      "KUNLUN_DATABASE_URL",
      "KUNLUN_API_KEY_PEPPER",
      "KUNLUN_SESSION_PEPPER",
      "KUNLUN_TRUSTED_PROXY_SECRET",
    ]);
  });

  it("accepts a complete production binding set", () => {
    expect(
      missingRequiredBindings({
        KUNLUN_DATABASE_URL:
          "postgresql+psycopg://runtime:secret@db.example/gateway?sslmode=verify-full&sslrootcert=%2Fapp%2Fcerts%2Fsupabase-prod-ca-2021.crt",
        KUNLUN_API_KEY_PEPPER: "a".repeat(32),
        KUNLUN_SESSION_PEPPER: "b".repeat(32),
        KUNLUN_TRUSTED_PROXY_SECRET: "c".repeat(32),
      }),
    ).toEqual([]);
  });

  it("rejects malformed values without returning their contents", () => {
    expect(
      missingRequiredBindings({
        KUNLUN_DATABASE_URL: "sqlite:///local.sqlite3",
        KUNLUN_API_KEY_PEPPER: "short",
        KUNLUN_SESSION_PEPPER: "b".repeat(32),
        KUNLUN_TRUSTED_PROXY_SECRET: "c".repeat(32),
      }),
    ).toEqual(["KUNLUN_DATABASE_URL", "KUNLUN_API_KEY_PEPPER"]);
  });

  it("rejects a PostgreSQL URL without verified TLS mode", () => {
    expect(
      missingRequiredBindings({
        KUNLUN_DATABASE_URL: "postgresql+psycopg://runtime:secret@db.example/gateway",
        KUNLUN_API_KEY_PEPPER: "a".repeat(32),
        KUNLUN_SESSION_PEPPER: "b".repeat(32),
        KUNLUN_TRUSTED_PROXY_SECRET: "c".repeat(32),
      }),
    ).toEqual(["KUNLUN_DATABASE_URL"]);
  });

  it.each([
    "postgresql+psycopg://runtime:secret@db.example/gateway?sslmode=disable",
    "postgresql+psycopg://runtime:secret@db.example/?sslmode=require",
    "postgresql+psycopg://runtime:secret@/gateway?sslmode=require",
    "postgresql+psycopg://db.example/gateway?sslmode=require",
    "postgresql+psycopg://runtime@db.example/gateway?sslmode=verify-full&sslrootcert=%2Fapp%2Fcerts%2Fsupabase-prod-ca-2021.crt",
    "postgresql://runtime:secret@db.example/gateway?sslmode=require",
    "postgresql+psycopg://runtime:secret@db.example/gateway?sslmode=require&sslmode=disable",
    "postgresql+psycopg://runtime:secret@db.example/gateway?sslmode=verify-full",
    "postgresql+psycopg://runtime:secret@db.example/gateway?sslmode=verify-full&sslrootcert=%2Ftmp%2Funtrusted.crt",
  ])("rejects unsafe database URL shape %s", (databaseUrl) => {
    expect(
      missingRequiredBindings({
        KUNLUN_DATABASE_URL: databaseUrl,
        KUNLUN_API_KEY_PEPPER: "a".repeat(32),
        KUNLUN_SESSION_PEPPER: "b".repeat(32),
        KUNLUN_TRUSTED_PROXY_SECRET: "c".repeat(32),
      }),
    ).toEqual(["KUNLUN_DATABASE_URL"]);
  });
});

describe("deploymentUnavailableResponse", () => {
  it("does not reveal binding or secret names to public callers", async () => {
    const response = deploymentUnavailableResponse();
    expect(response.status).toBe(503);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(await response.json()).toEqual({ error: "service_unavailable" });
  });
});
