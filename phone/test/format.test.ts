import { describe, it, expect } from "vitest";
import { formatVerdictSms } from "../src/notify/format";

describe("formatVerdictSms", () => {
  it("formats a message verdict with reason, callback, and cost", () => {
    const s = formatVerdictSms({
      verdict: "message",
      fromE164: "+14155550100",
      callerName: "Sam",
      reason: "re: invoice",
      callbackNumber: "+14155550100",
      costEstimateUsd: 0.1,
    });
    expect(s).toContain("Message from Sam");
    expect(s).toContain("re: invoice");
    expect(s).toContain("Callback: +14155550100");
    expect(s).toContain("$0.10");
  });

  it("falls back to the number when there is no name", () => {
    const s = formatVerdictSms({ verdict: "spam", fromE164: "+14155550100", costEstimateUsd: 0 });
    expect(s).toContain("Blocked spam: +14155550100");
    expect(s).toContain("$0.00");
  });

  it("says Unknown caller when nothing identifies the caller", () => {
    const s = formatVerdictSms({ verdict: "gate_fail", fromE164: "", costEstimateUsd: 0.02 });
    expect(s).toContain("Unknown caller");
  });
});
