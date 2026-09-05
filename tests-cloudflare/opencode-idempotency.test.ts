import { describe, expect, it } from "vitest";

// The installer intentionally ships this as a plain .js file because OpenCode
// auto-discovers only .js and .ts plugin files.
// @ts-expect-error Vite loads the packaged asset without a declaration file.
const pluginModule = await import("../scripts/assets/kunlun_gateway_idempotency.js");

const model = { providerID: "kunlun-gateway", modelID: "test-model" };

async function keyFor(
  hooks: Awaited<ReturnType<typeof pluginModule.default>>,
  sessionID = "session-PRIVATE-XYZ",
) {
  const output: { headers?: Record<string, string> } = { headers: {} };
  await hooks["chat.headers"](
    {
      sessionID,
      message: { id: "user-PRIVATE-XYZ", content: "sensitive prompt content" },
      agent: "build-agent",
      model,
    },
    output,
  );
  return output.headers?.["Idempotency-Key"];
}

async function assistantUpdate(hooks: Awaited<ReturnType<typeof pluginModule.default>>, id: string) {
  await hooks.event({
    event: {
      type: "message.updated",
      properties: {
        info: { id, role: "assistant", sessionID: "session-PRIVATE-XYZ" },
      },
    },
  });
}

describe("Kunlun OpenCode idempotency plugin", () => {
  it("keeps retries of one provider turn stable and never exposes source identifiers", async () => {
    const hooks = await pluginModule.default();
    await assistantUpdate(hooks, "assistant-PRIVATE-XYZ");

    const first = await keyFor(hooks);
    const retry = await keyFor(hooks);

    expect(first).toMatch(/^oc_[a-f0-9]{64}$/);
    expect(retry).toBe(first);
    for (const secret of ["session-PRIVATE-XYZ", "assistant-PRIVATE-XYZ", "user-PRIVATE-XYZ", "sensitive prompt content"]) {
      expect(first).not.toContain(secret);
    }
  });

  it("uses a distinct key for a new assistant tool step and for a different model", async () => {
    const hooks = await pluginModule.default();
    await assistantUpdate(hooks, "assistant-step-one-PRIVATE");
    const first = await keyFor(hooks);
    await assistantUpdate(hooks, "assistant-step-two-PRIVATE");
    const second = await keyFor(hooks);

    const differentModelOutput: { headers?: Record<string, string> } = { headers: {} };
    await hooks["chat.headers"](
      {
        sessionID: "session-PRIVATE-XYZ",
        message: { id: "user-PRIVATE-XYZ" },
        agent: "build-agent",
        model: { providerID: "kunlun-gateway", modelID: "different-model" },
      },
      differentModelOutput,
    );

    expect(second).not.toBe(first);
    expect(differentModelOutput.headers?.["Idempotency-Key"]).not.toBe(second);

    const differentSession = await keyFor(hooks, "other-session-PRIVATE-XYZ");
    expect(differentSession).not.toBe(second);
  });

  it("does not touch headers for another provider and bounds session state", async () => {
    const hooks = await pluginModule.default();
    const output = { headers: { Existing: "keep" } };
    await hooks["chat.headers"](
      {
        sessionID: "session-PRIVATE-XYZ",
        message: { id: "user-PRIVATE-XYZ" },
        agent: "build-agent",
        model: { providerID: "other-provider", modelID: "other-model" },
      },
      output,
    );
    expect(output.headers).toEqual({ Existing: "keep" });

    for (const sessionID of Array.from({ length: 513 }, (_, index) => `session-${index}`)) {
      await hooks.event({
        event: {
          type: "message.updated",
          properties: { info: { id: `assistant-${sessionID}`, role: "assistant", sessionID } },
        },
      });
    }
    const evictedSession = await keyFor(hooks, "session-0");
    const freshHooks = await pluginModule.default();
    expect(evictedSession).toBe(await keyFor(freshHooks, "session-0"));
  });
});
