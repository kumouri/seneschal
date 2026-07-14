import { describe, it, expect } from "vitest";
import { decideFunnel, decidePostGate } from "../src/screener/funnel";
import type { CallerInfo, ListLookup, ScreenerSettings } from "../src/screener/decision";

const settings: ScreenerSettings = {
  userCellE164: "+14155550100",
  gatePrompt: "Press 1 to connect.",
  postGateAction: "converse",
  reputationLookupEnabled: false,
  dailyBudgetUsd: 2,
};

const caller = (fromE164: string, hasCallerId = true): CallerInfo => ({
  fromE164,
  toE164: "+14155550111",
  hasCallerId,
});
const lists = (isAllowlisted: boolean, isBlocklisted: boolean): ListLookup => ({ isAllowlisted, isBlocklisted });

describe("decideFunnel", () => {
  it("allowlisted contact -> allow (ring through, free)", () => {
    expect(decideFunnel(caller("+12025550123"), lists(true, false), settings)).toEqual({
      stage: "allow",
      reason: "allowlisted_contact",
    });
  });

  it("blocklisted number -> reject (declined before answer, $0)", () => {
    expect(decideFunnel(caller("+12025550123"), lists(false, true), settings).stage).toBe("reject");
  });

  it("allowlist wins when a number is somehow on both lists", () => {
    expect(decideFunnel(caller("+12025550123"), lists(true, true), settings).stage).toBe("allow");
  });

  it("clean unknown -> gate", () => {
    const d = decideFunnel(caller("+12025550123"), lists(false, false), settings);
    expect(d.stage).toBe("gate");
    expect(d.reason).toContain("unknown_caller");
  });

  it("self-call (from our own bridge target) -> gate, never allow/dial-to-self", () => {
    const d = decideFunnel(caller(settings.userCellE164), lists(false, false), settings);
    expect(d.stage).toBe("gate");
    expect(d.reason).toBe("gate:self_call");
  });

  it("self-call guard wins even when the cell is allowlisted", () => {
    // The exact bug we hit: own cell was synced into the allowlist, so calling
    // from it dialed back to a busy line and rolled to voicemail.
    const d = decideFunnel(caller(settings.userCellE164), lists(true, false), settings);
    expect(d.stage).toBe("gate");
    expect(d.reason).toBe("gate:self_call");
  });

  it("empty userCellE164 disables the self-call guard (no false match on \"\")", () => {
    const d = decideFunnel(caller(""), lists(false, false), { ...settings, userCellE164: "" });
    expect(d.stage).toBe("gate");
    expect(d.reason).not.toBe("gate:self_call");
  });

  it("neighbor-spoof -> gate, reason recorded", () => {
    const d = decideFunnel(caller("+14155550199"), lists(false, false), settings);
    expect(d.stage).toBe("gate");
    expect(d.reason).toContain("neighbor_spoof");
  });

  it("anonymous caller -> gate, flagged", () => {
    const d = decideFunnel(caller("", false), lists(false, false), settings);
    expect(d.stage).toBe("gate");
    expect(d.reason).toContain("anonymous");
  });
});

describe("decidePostGate", () => {
  it("converse setting routes a gate-passer to Claude", () => {
    expect(decidePostGate(settings).stage).toBe("converse");
  });

  it("ring_through setting just dials the user (no AI cost)", () => {
    expect(decidePostGate({ ...settings, postGateAction: "ring_through" }).stage).toBe("allow");
  });
});
