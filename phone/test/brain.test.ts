import { describe, it, expect } from "vitest";
import { interpretToolUse, screenTurn } from "../src/screener/brain";
import type { LlmClient } from "../src/screener/brain";

describe("interpretToolUse", () => {
  it("maps connect_call", () => {
    expect(interpretToolUse({ name: "connect_call", input: { caller_name: "Dr. Lee's office", reason: "appointment" } })).toEqual({
      kind: "connect",
      callerName: "Dr. Lee's office",
      reason: "appointment",
    });
  });

  it("maps take_message and keeps a callback number", () => {
    expect(
      interpretToolUse({ name: "take_message", input: { caller_name: "Sam", summary: "call back re: invoice", callback_number: "+14155550100" } }),
    ).toMatchObject({ kind: "message", callerName: "Sam", callbackNumber: "+14155550100" });
  });

  it("omits callback number when absent", () => {
    expect(interpretToolUse({ name: "take_message", input: { caller_name: "Sam", summary: "hi" } })).toEqual({
      kind: "message",
      callerName: "Sam",
      summary: "hi",
    });
  });

  it("maps mark_spam", () => {
    expect(interpretToolUse({ name: "mark_spam", input: { reason: "auto warranty robocall" } })).toEqual({
      kind: "spam",
      reason: "auto warranty robocall",
    });
  });

  it("falls back to a clarifying reply for an unknown tool", () => {
    expect(interpretToolUse({ name: "???", input: {} }).kind).toBe("say");
  });
});

describe("screenTurn", () => {
  it("acts on a tool use", async () => {
    const client: LlmClient = { respond: async () => ({ toolUse: { name: "mark_spam", input: { reason: "robo" } } }) };
    expect(await screenTurn(client, "sys", [], [])).toEqual({ kind: "spam", reason: "robo" });
  });

  it("speaks when the model returns plain text", async () => {
    const client: LlmClient = { respond: async () => ({ text: "Who may I say is calling?" }) };
    expect(await screenTurn(client, "sys", [], [])).toEqual({ kind: "say", text: "Who may I say is calling?" });
  });
});
