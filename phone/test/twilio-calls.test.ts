import { describe, it, expect } from "vitest";
import { callResourceUrl } from "../src/twilio/calls";

describe("callResourceUrl", () => {
  it("builds the Twilio live-call resource URL", () => {
    expect(callResourceUrl("AC123", "CA456")).toBe(
      "https://api.twilio.com/2010-04-01/Accounts/AC123/Calls/CA456.json",
    );
  });
});
