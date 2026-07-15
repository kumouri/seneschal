import { describe, it, expect } from "vitest";
import { connectRelay, dial, escapeXml, gate, reject, say } from "../src/twiml";

describe("twiml builders", () => {
  it("dial bridges to the target with a callerId", () => {
    expect(dial("+14155550100", "+14155550111")).toContain(
      '<Dial callerId="+14155550111">+14155550100</Dial>',
    );
  });

  it("dial supports a ring timeout and a no-answer fallback", () => {
    const x = dial("+14155550100", "+14155550111", { timeoutSec: 18, fallbackMessage: "try later" });
    expect(x).toContain('<Dial timeout="18" callerId="+14155550111">+14155550100</Dial>');
    expect(x).toContain("<Say>try later</Say><Hangup />");
  });

  it("reject declines without answering", () => {
    expect(reject()).toContain('<Reject reason="rejected" />');
  });

  it("gate gathers one digit then hangs up on no input", () => {
    const x = gate({ prompt: "Press 1", actionUrl: "https://host/gate" });
    expect(x).toContain("<Gather");
    expect(x).toContain('action="https://host/gate"');
    // The Hangup fallback must come after the Gather so silent robodialers drop.
    expect(x.indexOf("<Gather")).toBeLessThan(x.indexOf("<Hangup"));
  });

  it("gate reports silent timeouts to the action URL so /gate can count them", () => {
    const x = gate({ prompt: "Press 1", actionUrl: "https://host/gate" });
    expect(x).toContain('actionOnEmptyResult="true"');
  });

  it("connectRelay wires the ConversationRelay websocket url", () => {
    const x = connectRelay({ wsUrl: "wss://host/ws", welcomeGreeting: "hi" });
    expect(x).toContain('<ConversationRelay url="wss://host/ws"');
    expect(x).toContain('welcomeGreeting="hi"');
  });

  it("connectRelay passes custom parameters through to setup", () => {
    const x = connectRelay({
      wsUrl: "wss://host/ws",
      welcomeGreeting: "hi",
      parameters: [{ name: "from", value: "+15551234567" }],
    });
    expect(x).toContain('<Parameter name="from" value="+15551234567" />');
    expect(x).toContain("</ConversationRelay>");
  });

  it("connectRelay sets the persona TTS voice when provided", () => {
    const x = connectRelay({ wsUrl: "wss://h/ws", welcomeGreeting: "hi", ttsProvider: "Amazon", voice: "Joanna-Neural" });
    expect(x).toContain('ttsProvider="Amazon"');
    expect(x).toContain('voice="Joanna-Neural"');
  });

  it("escapes XML-special characters in dynamic text", () => {
    expect(escapeXml(`a&b<c>"'`)).toBe("a&amp;b&lt;c&gt;&quot;&apos;");
    expect(say("Tom & Jerry")).toContain("Tom &amp; Jerry");
  });
});
