import { describe, it, expect } from "vitest";
import { toAllowlistRows, toE164Us } from "../scripts/seed-allowlist";

describe("toE164Us", () => {
  it("normalizes a 10-digit US number", () => {
    expect(toE164Us("(415) 555-0100")).toBe("+14155550100");
  });
  it("normalizes an 11-digit number with country code", () => {
    expect(toE164Us("1-415-555-0100")).toBe("+14155550100");
  });
  it("keeps an explicit + international number", () => {
    expect(toE164Us("+44 20 7183 8750")).toBe("+442071838750");
  });
  it("returns empty for unusable input", () => {
    expect(toE164Us("not a number")).toBe("");
  });
});

describe("toAllowlistRows", () => {
  it("normalizes and drops junk rows", () => {
    const rows = toAllowlistRows([
      { name: "Mom", number: "415-555-0100" },
      { name: "Broken", number: "n/a" },
    ]);
    expect(rows).toEqual([{ numberE164: "+14155550100", name: "Mom" }]);
  });
});
