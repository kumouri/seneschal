import { describe, it, expect } from "vitest";
import { RelaySession, resolveSessionMode } from "../src/relay/session";
import type { Env } from "../src/config";

function fakeEnv(): Env {
  return {
    ANTHROPIC_API_KEY: "key",
    OWNER_NAME: "Alex",
    USER_CELL_E164: "+15551112222",
  } as unknown as Env;
}

/**
 * Minimal fake `DurableObjectState`. `store` is a plain Map the test can share across two
 * `RelaySession` instances to simulate the SAME Durable Object surviving an eviction (a fresh
 * instance, same underlying `state.storage`). `blockConcurrencyWhile` captures the promise so the
 * test can await it — standing in for the real runtime's "no fetch() until this resolves".
 */
function fakeState(store: Map<string, unknown> = new Map()) {
  let ready: Promise<unknown> = Promise.resolve();
  const state = {
    storage: {
      get: async (key: string) => store.get(key),
      put: async (arg: string | Record<string, unknown>, value?: unknown) => {
        if (typeof arg === "string") {
          store.set(arg, value);
        } else {
          for (const [k, v] of Object.entries(arg)) store.set(k, v);
        }
      },
    },
    blockConcurrencyWhile: (fn: () => Promise<unknown>) => {
      ready = fn();
      return ready;
    },
  };
  return { state: state as unknown as DurableObjectState, store, ready: () => ready };
}

/** Reach the private mode/context fields — TS `private` is compile-time only. */
function modeOf(session: RelaySession): { mode: string; ownerContext: string } {
  return session as unknown as { mode: string; ownerContext: string };
}

describe("resolveSessionMode", () => {
  it("is owner only when both the persisted seed and the TwiML param agree", () => {
    expect(resolveSessionMode("owner", "owner")).toBe("owner");
  });

  it("falls back to screener when the seed says owner but the param disagrees (or is missing)", () => {
    expect(resolveSessionMode("owner", undefined)).toBe("screener");
    expect(resolveSessionMode("owner", "screener")).toBe("screener");
  });

  it("falls back to screener when the param says owner but there is no real seed", () => {
    expect(resolveSessionMode("screener", "owner")).toBe("screener");
  });

  it("stays screener when neither signal claims owner", () => {
    expect(resolveSessionMode("screener", undefined)).toBe("screener");
  });
});

describe("RelaySession — seed persistence across an eviction", () => {
  it("a fresh instance defaults to screener mode when nothing was ever seeded", () => {
    const { state } = fakeState();
    const session = new RelaySession(state, fakeEnv());
    expect(modeOf(session).mode).toBe("screener");
  });

  it("POST /seed persists mode+context to storage, not just instance fields", async () => {
    const { state, store } = fakeState();
    const session = new RelaySession(state, fakeEnv());
    const res = await session.fetch(
      new Request("https://relay/seed", {
        method: "POST",
        body: JSON.stringify({ mode: "owner", context: "3pm dentist" }),
      }),
    );
    expect(res.status).toBe(200);
    expect(store.get("mode")).toBe("owner");
    expect(store.get("ownerContext")).toBe("3pm dentist");
    expect(modeOf(session).mode).toBe("owner");
  });

  it("a NEW instance over the SAME storage (simulated eviction) still loads owner mode + context", async () => {
    const store = new Map<string, unknown>();
    const first = fakeState(store);
    const seeder = new RelaySession(first.state, fakeEnv());
    await seeder.fetch(
      new Request("https://relay/seed", {
        method: "POST",
        body: JSON.stringify({ mode: "owner", context: "today's calendar: 3pm dentist" }),
      }),
    );

    // A fresh RelaySession instance, as the runtime would construct after evicting the first —
    // same underlying storage, no in-memory state carried over.
    const second = fakeState(store);
    const revived = new RelaySession(second.state, fakeEnv());
    await second.ready();

    expect(modeOf(revived).mode).toBe("owner");
    expect(modeOf(revived).ownerContext).toBe("today's calendar: 3pm dentist");
  });

  it("an inbound (screener) call never seeds owner mode — /seed only ever POSTs mode: owner from handlePushCallTalk", async () => {
    const { state, store } = fakeState();
    const session = new RelaySession(state, fakeEnv());
    await session.fetch(
      new Request("https://relay/seed", {
        method: "POST",
        body: JSON.stringify({ mode: "screener" }),
      }),
    );
    expect(store.has("mode")).toBe(false);
    expect(modeOf(session).mode).toBe("screener");
  });

  it("rejects a non-JSON seed body", async () => {
    const { state, store } = fakeState();
    const session = new RelaySession(state, fakeEnv());
    const res = await session.fetch(new Request("https://relay/seed", { method: "POST", body: "not json" }));
    expect(res.status).toBe(400);
    expect(store.has("mode")).toBe(false);
  });
});
