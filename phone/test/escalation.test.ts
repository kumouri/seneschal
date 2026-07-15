import { describe, it, expect } from "vitest";
import { nextEscalationStep, DEFAULT_MAX_ATTEMPTS } from "../src/escalation/escalation";
import { escalationGather } from "../src/twiml";

describe("nextEscalationStep", () => {
  it("keeps calling while under the cap and not acked", () => {
    expect(nextEscalationStep({ attempt: 1, maxAttempts: 15, acked: false })).toBe("call");
    expect(nextEscalationStep({ attempt: 14, maxAttempts: 15, acked: false })).toBe("call");
  });

  it("stops as soon as the owner has acked, even mid-sequence", () => {
    expect(nextEscalationStep({ attempt: 3, maxAttempts: 15, acked: true })).toBe("stop");
  });

  it("stops once the attempt cap is reached", () => {
    expect(nextEscalationStep({ attempt: 15, maxAttempts: 15, acked: false })).toBe("stop");
    expect(nextEscalationStep({ attempt: 16, maxAttempts: 15, acked: false })).toBe("stop");
  });

  it("defaults to a 15-attempt cap", () => {
    expect(DEFAULT_MAX_ATTEMPTS).toBe(15);
  });
});

describe("escalationGather", () => {
  it("wraps the prompt in a <Gather> wired to the ack URL, with a Hangup fallback", () => {
    const x = escalationGather({ prompt: "Wake up — press 1", actionUrl: "https://host/push-call/ack?id=x" });
    expect(x).toContain("<Gather");
    expect(x).toContain('action="https://host/push-call/ack?id=x"');
    expect(x).toContain("<Say>Wake up");
    // Hangup must follow the Gather so a no-input call ends (the DO's alarm calls back).
    expect(x.indexOf("<Gather")).toBeLessThan(x.indexOf("<Hangup"));
  });

  it("defaults to one digit and a 30s wait, and escapes the action URL", () => {
    const x = escalationGather({ prompt: "hi", actionUrl: "https://host/ack?id=a&b=1" });
    expect(x).toContain('numDigits="1"');
    expect(x).toContain('timeout="30"');
    expect(x).toContain("&amp;b=1"); // ampersand escaped for XML
  });
});
