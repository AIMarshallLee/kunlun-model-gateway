/**
 * OpenCode V1 plugin for a stable per-provider-turn gateway idempotency key.
 *
 * OpenCode treats every exported function as a plugin entry, so this module
 * deliberately exports only the default plugin. The key hashes opaque IDs and
 * never reads prompt content.
 */

const PROVIDER_ID = "kunlun-gateway";
const MAX_SESSIONS = 512;
const NO_ASSISTANT_MESSAGE = "no-assistant-message";
const NO_USER_MESSAGE = "no-user-message";

function stringValue(value, fallback) {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function eventAssistant(event) {
  if (event?.type !== "message.updated") return undefined;

  const properties = event.properties ?? {};
  const info = properties.info ?? properties.message ?? {};
  if (info.role !== "assistant") return undefined;

  const sessionID = stringValue(info.sessionID ?? properties.sessionID, "");
  const assistantID = stringValue(info.id ?? properties.messageID, "");
  if (!sessionID || !assistantID) return undefined;
  return { sessionID, assistantID };
}

function deletedSessionID(event) {
  if (event?.type !== "session.deleted") return undefined;
  const properties = event.properties ?? {};
  return stringValue(properties.info?.id ?? properties.sessionID ?? properties.id, "") || undefined;
}

function createAssistantTurnStore(maxSessions = MAX_SESSIONS) {
  const assistantBySession = new Map();

  return {
    remember(sessionID, assistantID) {
      assistantBySession.delete(sessionID);
      assistantBySession.set(sessionID, assistantID);
      while (assistantBySession.size > maxSessions) {
        assistantBySession.delete(assistantBySession.keys().next().value);
      }
    },
    latest(sessionID) {
      const assistantID = assistantBySession.get(sessionID);
      if (assistantID) {
        assistantBySession.delete(sessionID);
        assistantBySession.set(sessionID, assistantID);
      }
      return assistantID;
    },
    forget(sessionID) {
      assistantBySession.delete(sessionID);
    },
  };
}

async function createIdempotencyKey({
  sessionID,
  assistantID,
  userMessageID,
  agent,
  providerID,
  modelID,
}) {
  const payload = JSON.stringify([
    "kunlun-opencode-idempotency-v1",
    stringValue(sessionID, "no-session"),
    stringValue(assistantID, NO_ASSISTANT_MESSAGE),
    stringValue(userMessageID, NO_USER_MESSAGE),
    stringValue(agent, "no-agent"),
    stringValue(providerID, "no-provider"),
    stringValue(modelID, "no-model"),
  ]);
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(payload),
  );
  const hex = Array.from(
    new Uint8Array(digest),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  return `oc_${hex}`;
}

const KunlunGatewayIdempotency = async () => {
  const assistantTurns = createAssistantTurnStore();

  return {
    event: async ({ event }) => {
      const assistant = eventAssistant(event);
      if (assistant) assistantTurns.remember(assistant.sessionID, assistant.assistantID);

      const sessionID = deletedSessionID(event);
      if (sessionID) assistantTurns.forget(sessionID);
    },

    "chat.headers": async (input, output) => {
      if (input.model?.providerID !== PROVIDER_ID) return;

      output.headers ??= {};
      output.headers["Idempotency-Key"] = await createIdempotencyKey({
        sessionID: input.sessionID,
        assistantID: assistantTurns.latest(input.sessionID),
        userMessageID: input.message?.id,
        agent: input.agent,
        providerID: input.model.providerID,
        modelID: input.model.modelID,
      });
    },
  };
};

export default KunlunGatewayIdempotency;
