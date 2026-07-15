import { describe, it, expect } from "vitest";
import { runCallerTurn } from "../src/screener/conversation";
import type { LlmClient, LlmTurn } from "../src/screener/brain";

const sayClient = (text: string): LlmClient => ({ respond: async () => ({ text }) });
const toolClient = (name: string, input: Record<string, unknown>): LlmClient => ({
  respond: async () => ({ toolUse: { name, input } }),
});

describe("runCallerTurn", () => {
  it("returns a spoken reply and grows the history", async () => {
    const out = await runCallerTurn(sayClient("Who's calling?"), "sys", [], [], "hello", 5);
    expect(out.reply).toBe("Who's calling?");
    expect(out.terminal).toBeUndefined();
    expect(out.history.map((t) => t.role)).toEqual(["user", "assistant"]);
  });

  it("surfaces a terminal action from a tool use", async () => {
    const out = await runCallerTurn(toolClient("mark_spam", { reason: "robo" }), "sys", [], [], "buy now", 5);
    expect(out.terminal).toEqual({ kind: "spam", reason: "robo" });
    expect(out.reply).toBeUndefined();
  });

  it("forces a take-message once the turn cap is reached", async () => {
    const history: LlmTurn[] = [
      { role: "assistant", content: "a" },
      { role: "assistant", content: "b" },
    ];
    const out = await runCallerTurn(sayClient("still rambling"), "sys", [], history, "uh", 2);
    expect(out.terminal?.kind).toBe("message");
  });
});
