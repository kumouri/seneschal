import { describe, it, expect, vi, afterEach } from "vitest";
import { sumSpendToday } from "../src/data/db";
import { overBudget } from "../src/budget";

/** Minimal in-memory D1 stand-in for the one SUM query sumSpendToday issues. */
function fakeD1(total: number) {
  const db = {
    prepare(sql: string) {
      return {
        bind(..._args: unknown[]) {
          return {
            async first() {
              if (sql.includes("SUM(cost_estimate_usd)")) return { total };
              throw new Error(`unexpected first(): ${sql}`);
            },
          };
        },
      };
    },
  } as unknown as D1Database;
  return db;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("sumSpendToday", () => {
  it("returns the summed cost for today", async () => {
    expect(await sumSpendToday(fakeD1(1.2345))).toBe(1.2345);
  });

  it("returns 0 when there is nothing recorded yet (COALESCE)", async () => {
    expect(await sumSpendToday(fakeD1(0))).toBe(0);
  });

  it("scopes the query to today's UTC date prefix", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-15T23:59:00Z"));
    let boundArg = "";
    const db = {
      prepare(_sql: string) {
        return {
          bind(...args: unknown[]) {
            boundArg = String(args[0]);
            return { async first() { return { total: 0 }; } };
          },
        };
      },
    } as unknown as D1Database;
    await sumSpendToday(db);
    expect(boundArg).toBe("2026-01-15%");
  });
});

describe("overBudget feeding sumSpendToday's result (the talk-mode budget gate)", () => {
  it("is over budget once today's spend reaches the cap", async () => {
    const spent = await sumSpendToday(fakeD1(2.0));
    expect(overBudget({ dateUtc: "2026-01-15", spentUsd: spent }, "2026-01-15", 2.0)).toBe(true);
  });

  it("is never over budget when the cap is 0 (disabled)", async () => {
    const spent = await sumSpendToday(fakeD1(999));
    expect(overBudget({ dateUtc: "2026-01-15", spentUsd: spent }, "2026-01-15", 0)).toBe(false);
  });
});
