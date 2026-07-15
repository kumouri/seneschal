import { describe, it, expect } from "vitest";
import { COST, estimateCallCost, overBudget } from "../src/budget";

describe("estimateCallCost", () => {
  it("a reject is free", () => {
    expect(estimateCallCost("reject", 0)).toBe(0);
  });
  it("the gate is a flat ~2 cents", () => {
    expect(estimateCallCost("gate", 5)).toBe(COST.gateUsd);
  });
  it("a conversation bills per started minute", () => {
    expect(estimateCallCost("converse", 30)).toBeCloseTo(COST.conversationPerMinUsd, 5);
    expect(estimateCallCost("converse", 61)).toBeCloseTo(COST.conversationPerMinUsd * 2, 5);
  });
  it("an allowed bridge bills the outbound leg", () => {
    expect(estimateCallCost("allow", 30)).toBeCloseTo(COST.bridgePerMinUsd, 5);
  });
});

describe("overBudget", () => {
  it("trips at or over the cap on the same day", () => {
    expect(overBudget({ dateUtc: "2026-06-26", spentUsd: 2 }, "2026-06-26", 2)).toBe(true);
    expect(overBudget({ dateUtc: "2026-06-26", spentUsd: 2.5 }, "2026-06-26", 2)).toBe(true);
  });
  it("does not trip below the cap", () => {
    expect(overBudget({ dateUtc: "2026-06-26", spentUsd: 1.5 }, "2026-06-26", 2)).toBe(false);
  });
  it("resets on a new day", () => {
    expect(overBudget({ dateUtc: "2026-06-25", spentUsd: 9 }, "2026-06-26", 2)).toBe(false);
  });
  it("is disabled when the cap is 0", () => {
    expect(overBudget({ dateUtc: "2026-06-26", spentUsd: 9 }, "2026-06-26", 0)).toBe(false);
  });
});
