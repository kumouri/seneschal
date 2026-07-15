/**
 * The assistant persona — the public-facing character that fronts the owner's
 * communications. The call screener is its first home; the same persona can
 * later front email/text. Kept separate from the owner's personal profile (the
 * OWNER_PROFILE secret): this describes the *assistant*, that describes the *owner*.
 *
 * DEFAULT_PERSONA below is the persona the Worker ships with: a nameless,
 * competent default that works out of the box. The canonical persona for the
 * whole assistant lives in `persona/persona.md` at the repo root, and the setup
 * wizard emits the Worker's env values (ASSISTANT_NAME / ASSISTANT_VOICE_ID /
 * ASSISTANT_TTS_PROVIDER — see src/config.ts `personaFromEnv`) to override it.
 */
export interface Persona {
  /** First name the assistant introduces itself with. Absent = nameless (the greeting adapts). */
  name?: string;
  /** Short role label, e.g. "assistant". */
  role: string;
  /** Personality/demeanor for the system prompt. Use the literal `{owner}` placeholder. */
  demeanor: string;
  /** Spoken greeting when answering a screened call. Pass the owner's (spoken) name, or nothing. */
  greeting: (owner?: string) => string;
  /** ConversationRelay TTS provider + voice. Absent = the platform's default voice. */
  ttsProvider?: string;
  voice?: string;
}

/**
 * Render the screener's opening line for any name/owner combination:
 *   name + owner  -> "Hi, this is Robin, Alex's assistant. …"
 *   nameless      -> "Hi, this is Alex's assistant. …"
 *   no owner      -> "Hi, this is Robin, the assistant on this line. …"
 *   neither       -> "Hi, this is the assistant on this line. …"
 */
export function greetingLine(name?: string, owner?: string): string {
  const ask = "And who do I have the pleasure of speaking with?";
  const self = name !== undefined && name !== "" ? `${name}, ` : "";
  const role = owner !== undefined && owner !== "" ? `${owner}'s assistant` : "the assistant on this line";
  return `Hi, this is ${self}${role}. ${ask}`;
}

/** The default screener persona: warm, efficient, courteous — and hard to fast-talk. */
export const DEFAULT_PERSONA: Persona = {
  role: "assistant",
  demeanor:
    "You are {owner}'s assistant — the chief of staff who fronts this phone line. You are warm, " +
    "efficient, and unfailingly courteous, but hard to fast-talk or fluster: you stay calm and " +
    "level-headed, you see through sales scripts and scams, and you never let anyone push past you.",
  greeting: (owner) => greetingLine(undefined, owner),
  // ttsProvider/voice intentionally absent: ConversationRelay uses its default
  // voice until ASSISTANT_TTS_PROVIDER / ASSISTANT_VOICE_ID are set.
};
