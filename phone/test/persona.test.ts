import { describe, it, expect } from "vitest";
import { DEFAULT_PERSONA, greetingLine } from "../src/persona";
import { configured, personaFromEnv, type Env } from "../src/config";

function envWith(vars: Partial<Env>): Env {
  return vars as Env;
}

describe("greetingLine", () => {
  it("introduces a named assistant with the owner", () => {
    expect(greetingLine("Robin", "Alex")).toBe(
      "Hi, this is Robin, Alex's assistant. And who do I have the pleasure of speaking with?",
    );
  });

  it("reads naturally when the assistant is nameless", () => {
    expect(greetingLine(undefined, "Alex")).toBe(
      "Hi, this is Alex's assistant. And who do I have the pleasure of speaking with?",
    );
  });

  it("reads naturally when the owner is also unset", () => {
    expect(greetingLine()).toBe(
      "Hi, this is the assistant on this line. And who do I have the pleasure of speaking with?",
    );
  });

  it("names the assistant even without an owner", () => {
    expect(greetingLine("Robin")).toBe(
      "Hi, this is Robin, the assistant on this line. And who do I have the pleasure of speaking with?",
    );
  });
});

describe("DEFAULT_PERSONA", () => {
  it("ships nameless with no fixed voice (platform default)", () => {
    expect(DEFAULT_PERSONA.name).toBeUndefined();
    expect(DEFAULT_PERSONA.voice).toBeUndefined();
    expect(DEFAULT_PERSONA.ttsProvider).toBeUndefined();
  });

  it("keeps the {owner} placeholder in the demeanor", () => {
    expect(DEFAULT_PERSONA.demeanor).toContain("{owner}");
  });

  it("greets as the owner's assistant", () => {
    expect(DEFAULT_PERSONA.greeting("Alex")).toBe(greetingLine(undefined, "Alex"));
  });
});

describe("personaFromEnv", () => {
  it("falls back to the default persona when nothing is set", () => {
    const p = personaFromEnv(envWith({}));
    expect(p.name).toBeUndefined();
    expect(p.voice).toBeUndefined();
    expect(p.ttsProvider).toBeUndefined();
    expect(p.demeanor).toBe(DEFAULT_PERSONA.demeanor);
    expect(p.greeting("Alex")).toBe("Hi, this is Alex's assistant. And who do I have the pleasure of speaking with?");
  });

  it("treats blank vars as unset (wrangler [vars] default to empty strings)", () => {
    const p = personaFromEnv(envWith({ ASSISTANT_NAME: "  ", ASSISTANT_VOICE_ID: "", ASSISTANT_TTS_PROVIDER: "" }));
    expect(p.name).toBeUndefined();
    expect(p.voice).toBeUndefined();
    expect(p.ttsProvider).toBeUndefined();
  });

  it("overrides name, voice, and provider from the env — greeting included", () => {
    const p = personaFromEnv(
      envWith({ ASSISTANT_NAME: "Robin", ASSISTANT_VOICE_ID: "voice-123", ASSISTANT_TTS_PROVIDER: "ElevenLabs" }),
    );
    expect(p.name).toBe("Robin");
    expect(p.voice).toBe("voice-123");
    expect(p.ttsProvider).toBe("ElevenLabs");
    expect(p.greeting("Alex")).toBe("Hi, this is Robin, Alex's assistant. And who do I have the pleasure of speaking with?");
  });
});

describe("configured", () => {
  it("returns trimmed values and undefined for blank/unset", () => {
    expect(configured("  x  ")).toBe("x");
    expect(configured("")).toBeUndefined();
    expect(configured("   ")).toBeUndefined();
    expect(configured(undefined)).toBeUndefined();
  });
});
