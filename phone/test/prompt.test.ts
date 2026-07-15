import { describe, it, expect } from "vitest";
import { buildSystemPrompt, SCREENER_TOOLS } from "../src/screener/prompt";
import { DEFAULT_PERSONA, type Persona } from "../src/persona";

const NAMED: Persona = { ...DEFAULT_PERSONA, name: "Robin" };

describe("buildSystemPrompt", () => {
  it("includes the owner name", () => {
    expect(buildSystemPrompt("Alex")).toContain("Alex");
  });

  it("injects the persona identity and fills the {owner} placeholder", () => {
    const p = buildSystemPrompt("Alex", undefined, DEFAULT_PERSONA);
    expect(p).toContain("Alex's assistant");
    expect(p).not.toContain("{owner}");
  });

  it("stays coherent for the nameless default persona", () => {
    const p = buildSystemPrompt("Alex", undefined, DEFAULT_PERSONA);
    expect(p).not.toContain("Your name is");
    expect(p).not.toContain("undefined");
  });

  it("tells the model its name when the persona has one", () => {
    const p = buildSystemPrompt("Alex", undefined, NAMED);
    expect(p).toContain("Your name is Robin");
  });

  it("adds a pronunciation note when a spoken name is given", () => {
    const p = buildSystemPrompt("Sian", undefined, DEFAULT_PERSONA, "Shawn");
    expect(p).toContain('write it as "Shawn"');
  });

  it("omits the pronunciation note when the spoken name matches", () => {
    expect(buildSystemPrompt("Alex", undefined, DEFAULT_PERSONA, "Alex")).not.toContain("write it as");
  });

  it("challenges a caller who seems to be the owner for a password", () => {
    expect(buildSystemPrompt("Alex", undefined, DEFAULT_PERSONA).toLowerCase()).toContain("password");
  });

  it("embeds the configured password when one is set", () => {
    expect(buildSystemPrompt("Sian", undefined, DEFAULT_PERSONA, "Shawn", "swordfish")).toContain("swordfish");
  });

  it("injects the owner profile when provided", () => {
    const p = buildSystemPrompt("Alex", "is a software developer who wants recruiter calls");
    expect(p).toContain("About Alex:");
    expect(p).toContain("software developer who wants recruiter calls");
  });

  it("omits the profile line when not provided or blank", () => {
    expect(buildSystemPrompt("Alex")).not.toContain("About Alex:");
    expect(buildSystemPrompt("Alex", "   ")).not.toContain("About Alex:");
  });
});

describe("SCREENER_TOOLS", () => {
  it("exposes the three screening tools", () => {
    expect(SCREENER_TOOLS.map((t) => t.name).sort()).toEqual(["connect_call", "mark_spam", "take_message"]);
  });
});
