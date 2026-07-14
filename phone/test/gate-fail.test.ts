import { describe, it, expect } from "vitest";
import { GATE_FAIL_BLOCK_AFTER, recordGateFail } from "../src/data/db";

/**
 * Minimal in-memory stand-in for the few D1 statements recordGateFail issues.
 * Routes on SQL substrings — crude, but keeps the learning loop testable
 * without a Workers runtime (same offline posture as the rest of the suite).
 */
function fakeD1(opts: { contacts?: string[] } = {}) {
  const contacts = new Set(opts.contacts ?? []);
  const calls: Array<{ from: string; stage: string }> = [];
  const blocklist = new Map<string, { reason: string; hits: number }>();

  const db = {
    prepare(sql: string) {
      return {
        bind(...args: unknown[]) {
          return {
            async first() {
              if (sql.includes("SELECT 1 FROM contacts")) {
                return contacts.has(String(args[0])) ? { 1: 1 } : null;
              }
              if (sql.includes("SELECT COUNT(*) AS n FROM calls")) {
                const n = calls.filter((c) => c.from === String(args[0]) && c.stage === "gate_fail").length;
                return { n };
              }
              throw new Error(`unexpected first(): ${sql}`);
            },
            async run() {
              if (sql.includes("INSERT INTO calls")) {
                // recordCall binds (id, from_e164, to_e164, ...outcome_stage at index 5)
                calls.push({ from: String(args[1]), stage: String(args[5]) });
                return;
              }
              if (sql.includes("INSERT INTO blocklist")) {
                const num = String(args[0]);
                const prior = blocklist.get(num);
                blocklist.set(num, { reason: String(args[1]), hits: (prior?.hits ?? 0) + 1 });
                return;
              }
              throw new Error(`unexpected run(): ${sql}`);
            },
          };
        },
      };
    },
  } as unknown as D1Database;

  return { db, calls, blocklist };
}

describe("recordGateFail", () => {
  it("does nothing for anonymous callers", async () => {
    const { db, calls, blocklist } = fakeD1();
    expect(await recordGateFail(db, "", "+15550000000", true)).toBe(0);
    expect(calls).toHaveLength(0);
    expect(blocklist.size).toBe(0);
  });

  it("never counts allowlisted contacts, even silent ones", async () => {
    const { db, calls, blocklist } = fakeD1({ contacts: ["+15551234567"] });
    expect(await recordGateFail(db, "+15551234567", "+15550000000", true)).toBe(0);
    expect(calls).toHaveLength(0);
    expect(blocklist.size).toBe(0);
  });

  it("blocklists a silent timeout on the FIRST strike", async () => {
    const { db, calls, blocklist } = fakeD1();
    const failures = await recordGateFail(db, "+15559990000", "+15550000000", true);
    expect(failures).toBe(1);
    expect(calls).toEqual([{ from: "+15559990000", stage: "gate_fail" }]);
    expect(blocklist.get("+15559990000")?.reason).toBe("gate_fail");
  });

  it("records a wrong-key failure without blocklisting", async () => {
    const { db, calls, blocklist } = fakeD1();
    const failures = await recordGateFail(db, "+15559990000", "+15550000000", false);
    expect(failures).toBe(1);
    expect(calls).toEqual([{ from: "+15559990000", stage: "gate_fail" }]);
    expect(blocklist.size).toBe(0);
  });

  it(`blocklists wrong-key pressers on failure #${GATE_FAIL_BLOCK_AFTER}`, async () => {
    const { db, blocklist } = fakeD1();
    await recordGateFail(db, "+15559990000", "+15550000000", false);
    const failures = await recordGateFail(db, "+15559990000", "+15550000000", false);
    expect(failures).toBe(GATE_FAIL_BLOCK_AFTER);
    expect(blocklist.get("+15559990000")?.reason).toBe("gate_fail");
  });

  it("keeps counting per number, not globally", async () => {
    const { db, blocklist } = fakeD1();
    await recordGateFail(db, "+15551110000", "+15550000000", false);
    await recordGateFail(db, "+15552220000", "+15550000000", false);
    expect(blocklist.size).toBe(0); // one wrong-key strike each — nobody blocked yet
  });
});
