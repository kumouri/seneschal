import { describe, it, expect } from "vitest";
import { reconcileContacts } from "../src/data/db";

describe("reconcileContacts", () => {
  it("upserts incoming and removes stale google numbers", () => {
    const { toUpsert, toRemove } = reconcileContacts(new Set(["+1111", "+2222"]), [
      { numberE164: "+2222", name: "B" },
      { numberE164: "+3333", name: "C" },
    ]);
    expect(toUpsert.map((c) => c.numberE164).sort()).toEqual(["+2222", "+3333"]);
    expect(toRemove).toEqual(["+1111"]);
  });

  it("dedupes incoming and skips blanks", () => {
    const { toUpsert } = reconcileContacts(new Set(), [
      { numberE164: "+1", name: "a" },
      { numberE164: "+1", name: "a-dupe" },
      { numberE164: "", name: "blank" },
    ]);
    expect(toUpsert).toHaveLength(1);
    expect(toUpsert[0]?.numberE164).toBe("+1");
  });

  it("removes nothing when incoming covers all existing google numbers", () => {
    const { toRemove } = reconcileContacts(new Set(["+1"]), [{ numberE164: "+1", name: "a" }]);
    expect(toRemove).toEqual([]);
  });

  it("removes all google numbers when the push is empty", () => {
    const { toUpsert, toRemove } = reconcileContacts(new Set(["+1", "+2"]), []);
    expect(toUpsert).toEqual([]);
    expect(toRemove.sort()).toEqual(["+1", "+2"]);
  });
});
