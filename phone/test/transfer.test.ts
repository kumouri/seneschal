import { describe, it, expect } from "vitest";
import {
  LIVE_TRANSFER_ATTEMPTS,
  LIVE_TRANSFER_RING_SEC,
  afterBridgeUrl,
  liveTransferTwiml,
  nextLiveTransferStep,
  parseAttempt,
} from "../src/twilio/transfer";
import { DEFAULT_MAX_ATTEMPTS } from "../src/escalation/escalation";
import type { Env } from "../src/config";

const env = {
  TWILIO_NUMBER_E164: "+15550000000",
  USER_CELL_E164: "+15551112222",
} as unknown as Env;

describe("nextLiveTransferStep — three rings, then a message", () => {
  it("rings the owner three times in total before voicemail", () => {
    expect(LIVE_TRANSFER_ATTEMPTS).toBe(3);
    expect(nextLiveTransferStep("no-answer", 1)).toEqual({ kind: "redial", attempt: 2 });
    expect(nextLiveTransferStep("busy", 2)).toEqual({ kind: "redial", attempt: 3 });
    expect(nextLiveTransferStep("no-answer", 3)).toEqual({ kind: "voicemail" });
  });

  it("stops the moment the owner answers, on any attempt", () => {
    expect(nextLiveTransferStep("completed", 1)).toEqual({ kind: "hangup" });
    expect(nextLiveTransferStep("completed", 2)).toEqual({ kind: "hangup" });
    expect(nextLiveTransferStep("completed", 3)).toEqual({ kind: "hangup" });
  });

  it("never rings past the cap even on a stray attempt number", () => {
    expect(nextLiveTransferStep("failed", 7)).toEqual({ kind: "voicemail" });
  });

  it("treats a missing or garbage attempt as the first one", () => {
    expect(parseAttempt(null)).toBe(1);
    expect(parseAttempt("")).toBe(1);
    expect(parseAttempt("0")).toBe(1);
    expect(parseAttempt("abc")).toBe(1);
    expect(parseAttempt("2")).toBe(2);
    expect(nextLiveTransferStep("no-answer", Number.NaN)).toEqual({ kind: "redial", attempt: 2 });
  });

  it("is a different cap from the reminder escalation's, which is untouched", () => {
    // Reminders keep calling far longer than a caller should be kept waiting.
    expect(DEFAULT_MAX_ATTEMPTS).toBe(15);
    expect(LIVE_TRANSFER_ATTEMPTS).toBeLessThan(DEFAULT_MAX_ATTEMPTS);
  });
});

describe("liveTransferTwiml", () => {
  it("dials the owner's cell with a short ring and an /after-bridge action carrying attempt + caller", () => {
    const x = liveTransferTwiml(env, "https://host", 2, "+15550001111");
    expect(x).toContain(`timeout="${LIVE_TRANSFER_RING_SEC}"`);
    // Below a typical (often user-shortened, 15 s) no-answer-forward timer WITH margin — a
    // longer ring loses that race and loops the bridge back through the screener.
    expect(LIVE_TRANSFER_RING_SEC).toBeLessThanOrEqual(12);
    expect(x).toContain('action="https://host/after-bridge?attempt=2&amp;from=%2B15550001111"');
    expect(x).toContain('callerId="+15550000000">+15551112222</Dial>');
  });

  it("omits the caller from the action URL when there is no caller id", () => {
    expect(afterBridgeUrl("https://host", 1, "")).toBe("https://host/after-bridge?attempt=1");
  });
});
