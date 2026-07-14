import { describe, it, expect } from "vitest";
import { parseInbound, textToken } from "../src/relay/protocol";

describe("ConversationRelay protocol", () => {
  it("parses a setup frame", () => {
    const m = parseInbound(JSON.stringify({ type: "setup", callSid: "CA123" }));
    expect(m.type).toBe("setup");
  });

  it("parses a prompt frame", () => {
    const m = parseInbound(JSON.stringify({ type: "prompt", voicePrompt: "hello there" }));
    expect(m.type).toBe("prompt");
  });

  it("falls back to unknown on bad JSON", () => {
    expect(parseInbound("not json").type).toBe("unknown");
  });

  it("falls back to unknown when type is missing", () => {
    expect(parseInbound(JSON.stringify({ foo: 1 })).type).toBe("unknown");
  });

  it("builds an outbound text token frame", () => {
    expect(textToken("hi", true)).toEqual({ type: "text", token: "hi", last: true });
  });
});
