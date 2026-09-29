import { describe, it, expect } from "vitest";
import { runOwnerTurn } from "../src/relay/owner-conversation";
import type { LlmClient, LlmResult, LlmTurn } from "../src/screener/brain";
import { OWNER_TOOLS } from "../src/relay/owner-prompt";

function fakeClient(result: LlmResult): LlmClient {
  return { respond: async () => result };
}

describe("runOwnerTurn", () => {
  it("returns a reply and appends both turns to history when the call continues", async () => {
    const client = fakeClient({ text: "Sure, what's up?" });
    const outcome = await runOwnerTurn(client, "system", OWNER_TOOLS, [], "Hey, got a sec?", 40);
    expect(outcome.ended).toBeUndefined();
    expect(outcome.reply).toBe("Sure, what's up?");
    expect(outcome.history).toEqual<LlmTurn[]>([
      { role: "user", content: "Hey, got a sec?" },
      { role: "assistant", content: "Sure, what's up?" },
    ]);
  });

  it("ends the call when the model calls end_call", async () => {
    const client = fakeClient({ toolUse: { name: "end_call", input: {} } });
    const outcome = await runOwnerTurn(client, "system", OWNER_TOOLS, [], "Bye, talk later!", 40);
    expect(outcome.ended).toBe(true);
    expect(outcome.reply).toBeUndefined();
    // The final user turn is kept (for the transcript record) but no assistant turn is appended —
    // there is no more TTS to speak once the call is ending.
    expect(outcome.history).toEqual<LlmTurn[]>([{ role: "user", content: "Bye, talk later!" }]);
  });

  it("ends the call once the turn cap is hit, even mid-conversation", async () => {
    const client = fakeClient({ text: "still going" });
    const history: LlmTurn[] = [
      { role: "user", content: "a" },
      { role: "assistant", content: "b" },
    ];
    const outcome = await runOwnerTurn(client, "system", OWNER_TOOLS, history, "c", 1);
    expect(outcome.ended).toBe(true);
    expect(outcome.reply).toBeUndefined();
  });

  it("ignores a tool call other than end_call and falls back to text", async () => {
    const client = fakeClient({ toolUse: { name: "connect_call", input: {} }, text: "huh?" });
    const outcome = await runOwnerTurn(client, "system", OWNER_TOOLS, [], "hi", 40);
    expect(outcome.ended).toBeUndefined();
    expect(outcome.reply).toBe("huh?");
  });
});
