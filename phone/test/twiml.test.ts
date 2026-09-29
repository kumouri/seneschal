import { describe, it, expect } from "vitest";
import { connectRelay, dial, escalationGather, escapeXml, gate, reject, say, sayVoiceOf, wssOf } from "../src/twiml";
import { personaFromEnv, type Env } from "../src/config";

describe("wssOf", () => {
  it("converts https:// to wss://", () => {
    expect(wssOf("https://host.example.com")).toBe("wss://host.example.com");
  });

  it("converts http:// to ws://", () => {
    expect(wssOf("http://localhost:8787")).toBe("ws://localhost:8787");
  });
});

describe("twiml builders", () => {
  it("say without a voice emits a plain <Say> (Twilio's default voice)", () => {
    expect(say("hi")).toContain("<Say>hi</Say>");
    expect(say("hi", { hangup: true })).toContain("<Say>hi</Say><Hangup />");
  });

  it("say with a voice sets the <Say voice> attribute", () => {
    expect(say("hi", { voice: "ElevenLabs.abc" })).toContain('<Say voice="ElevenLabs.abc">hi</Say>');
  });

  it("sayVoiceOf derives Twilio's <Provider>.<id> form from the persona, and nothing without one", () => {
    const voiced = personaFromEnv({ ASSISTANT_TTS_PROVIDER: "ElevenLabs", ASSISTANT_VOICE_ID: "voice-123" } as Env);
    expect(sayVoiceOf(voiced)).toBe("ElevenLabs.voice-123");
    // The shipped default persona names no voice: plain <Say>, Twilio's default.
    expect(sayVoiceOf(personaFromEnv({} as Env))).toBeUndefined();
    expect(sayVoiceOf({})).toBeUndefined();
    expect(sayVoiceOf({ ttsProvider: "ElevenLabs" })).toBeUndefined();
    expect(sayVoiceOf({ voice: "abc" })).toBeUndefined();
  });

  it("escalationGather carries the voice inside the <Gather> and keeps the digit ack identical", () => {
    const plain = escalationGather({ prompt: "Wake up", actionUrl: "https://host/push-call/ack?id=1" });
    const voiced = escalationGather({ prompt: "Wake up", actionUrl: "https://host/push-call/ack?id=1", voice: "ElevenLabs.abc" });
    expect(plain).toContain("<Say>Wake up</Say>");
    expect(voiced).toContain('<Say voice="ElevenLabs.abc">Wake up</Say>');
    // Same <Gather> wrapper either way: only the <Say> tag differs.
    expect(voiced.replace('<Say voice="ElevenLabs.abc">', "<Say>")).toBe(plain);
    expect(voiced).toContain('<Gather numDigits="1" timeout="30" action="https://host/push-call/ack?id=1" method="POST">');
    expect(voiced.indexOf("<Gather")).toBeLessThan(voiced.indexOf("<Say"));
    expect(voiced.indexOf("</Gather>")).toBeLessThan(voiced.indexOf("<Hangup"));
  });

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
