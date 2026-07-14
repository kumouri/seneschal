import { describe, it, expect } from "vitest";
import { parseAnthropicResponse } from "../src/screener/anthropic-client";

describe("parseAnthropicResponse", () => {
  it("extracts a tool use", () => {
    const r = parseAnthropicResponse({
      content: [{ type: "tool_use", name: "mark_spam", input: { reason: "robo" } }],
    });
    expect(r.toolUse).toEqual({ name: "mark_spam", input: { reason: "robo" } });
    expect(r.text).toBeUndefined();
  });

  it("prefers a tool use over surrounding text", () => {
    const r = parseAnthropicResponse({
      content: [
        { type: "text", text: "let me connect you" },
        { type: "tool_use", name: "connect_call", input: { caller_name: "Sam", reason: "hi" } },
      ],
    });
    expect(r.toolUse?.name).toBe("connect_call");
  });

  it("concatenates text blocks when there is no tool use", () => {
    const r = parseAnthropicResponse({ content: [{ type: "text", text: "Hi " }, { type: "text", text: "there" }] });
    expect(r.text).toBe("Hi there");
    expect(r.toolUse).toBeUndefined();
  });

  it("handles empty / missing content", () => {
    expect(parseAnthropicResponse({}).text).toBe("");
    expect(parseAnthropicResponse({ content: [] }).text).toBe("");
  });
});
