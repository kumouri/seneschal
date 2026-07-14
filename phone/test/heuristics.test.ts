import { describe, it, expect } from "vitest";
import { evaluateHeuristics, isNeighborSpoof, parseNanp } from "../src/filter/heuristics";

describe("parseNanp", () => {
  it("parses a US E.164 number", () => {
    expect(parseNanp("+14155550123")).toEqual({ area: "415", prefix: "555", line: "0123" });
  });
  it("returns null for non-NANP / empty", () => {
    expect(parseNanp("+442071838750")).toBeNull();
    expect(parseNanp("")).toBeNull();
  });
});

describe("isNeighborSpoof", () => {
  it("true when area + prefix match but line differs", () => {
    expect(isNeighborSpoof("+14155550199", "+14155550100")).toBe(true);
  });
  it("false for the exact same number", () => {
    expect(isNeighborSpoof("+14155550100", "+14155550100")).toBe(false);
  });
  it("false for a different prefix", () => {
    expect(isNeighborSpoof("+14155560100", "+14155550100")).toBe(false);
  });
});

describe("evaluateHeuristics", () => {
  it("flags an anonymous caller", () => {
    const r = evaluateHeuristics({ fromE164: "", toE164: "+14155550111", hasCallerId: false }, "+14155550100");
    expect(r.suspicious).toBe(true);
    expect(r.reasons).toContain("anonymous_or_no_caller_id");
  });
  it("leaves a clean out-of-area unknown unflagged", () => {
    const r = evaluateHeuristics({ fromE164: "+12025550123", toE164: "+14155550111", hasCallerId: true }, "+14155550100");
    expect(r.suspicious).toBe(false);
    expect(r.reasons).toHaveLength(0);
  });
});
