import { describe, it, expect } from "vitest";
import { buildOwnerSystemPrompt, ownerGreeting, OWNER_TOOLS } from "../src/relay/owner-prompt";
import { DEFAULT_PERSONA } from "../src/persona";
import { personaFromEnv, type Env } from "../src/config";

const NAMED = personaFromEnv({ ASSISTANT_NAME: "Robin" } as Env);

describe("buildOwnerSystemPrompt", () => {
  it("uses the owner-facing demeanor, not the screener's outsider-facing one", () => {
    const prompt = buildOwnerSystemPrompt("Alex", DEFAULT_PERSONA);
    expect(prompt).toContain("You lead with what matters");
    expect(prompt).not.toContain("fronts this phone line");
  });

  it("substitutes the owner name into the placeholder", () => {
    const prompt = buildOwnerSystemPrompt("Alex", DEFAULT_PERSONA);
    expect(prompt).toContain("You are Alex's assistant, and you are speaking with Alex directly");
    expect(prompt).not.toContain("{owner}");
  });

  it("introduces the assistant by name when the persona has one", () => {
    const prompt = buildOwnerSystemPrompt("Alex", NAMED);
    expect(prompt).toContain("You are Robin, Alex's assistant, and you are speaking with Alex directly");
  });

  it("says this is a real conversation, not a screening call", () => {
    const prompt = buildOwnerSystemPrompt("Alex", DEFAULT_PERSONA);
    expect(prompt).toContain("not a screening call");
  });

  it("tells the model to call end_call when the owner says goodbye", () => {
    const prompt = buildOwnerSystemPrompt("Alex", DEFAULT_PERSONA);
    expect(prompt).toContain("end_call");
  });

  it("includes the spoken-name pronunciation hint when given", () => {
    const prompt = buildOwnerSystemPrompt("Sian", DEFAULT_PERSONA, "Shawn");
    expect(prompt).toContain('write it as "Shawn"');
  });

  it("omits the pronunciation hint when the spoken name matches the written name", () => {
    const prompt = buildOwnerSystemPrompt("Alex", DEFAULT_PERSONA, "Alex");
    expect(prompt).not.toContain("pronounced correctly");
  });

  it("folds in a context snapshot when given, but tells the model never to recite it unasked", () => {
    const prompt = buildOwnerSystemPrompt("Alex", DEFAULT_PERSONA, undefined, "Today's calendar:\n- 3pm dentist");
    expect(prompt).toContain("Today's calendar");
    expect(prompt).toContain("3pm dentist");
    expect(prompt).toContain("never recite it unasked");
  });

  it("omits the context section entirely when no snapshot is given", () => {
    const withoutContext = buildOwnerSystemPrompt("Alex", DEFAULT_PERSONA);
    expect(withoutContext).not.toContain("snapshot");
  });

  it("never contains the screener's owner-auth password bit — the owner is the one being called", () => {
    // Guards against a talk call ever running the screener prompt (which DOES challenge a
    // self-proclaimed owner for a password, see prompt.test.ts).
    const prompt = buildOwnerSystemPrompt("Alex", DEFAULT_PERSONA, "Alex", "some context").toLowerCase();
    expect(prompt).not.toContain("password");
  });
});

describe("OWNER_TOOLS", () => {
  it("exposes only end_call — no screener triage tools", () => {
    expect(OWNER_TOOLS).toHaveLength(1);
    expect(OWNER_TOOLS[0]?.name).toBe("end_call");
  });
});

describe("ownerGreeting", () => {
  it("is a short, non-empty spoken line", () => {
    const g = ownerGreeting(DEFAULT_PERSONA);
    expect(g.length).toBeGreaterThan(0);
    expect(g.length).toBeLessThan(80);
  });

  it("names the assistant when the persona has a name, and reads naturally when it doesn't", () => {
    expect(ownerGreeting(NAMED)).toBe("Hey — it's Robin. Got a minute?");
    expect(ownerGreeting(DEFAULT_PERSONA)).toBe("Hey — it's your assistant. Got a minute?");
  });
});
