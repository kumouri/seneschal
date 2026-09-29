/**
 * System prompt + tool schema for a talk-mode call: the assistant phoning the owner for a live
 * conversation, not screening a caller on the owner's behalf. Kept separate from
 * `screener/prompt.ts` — different persona register (`Persona.ownerDemeanor`), different shape (no
 * connect/message/spam triage, just talk until the owner is done), different tool. Deliberately no
 * owner-password challenge: the owner is the one being called, there is nothing to prove.
 */
import type { Persona } from "../persona";
import type { ToolSchema } from "../screener/prompt";

function named(persona: Persona): string | undefined {
  const n = persona.name?.trim();
  return n !== undefined && n !== "" ? n : undefined;
}

/**
 * Spoken the instant the call connects, before ConversationRelay sends the first `prompt`.
 * Introduces the assistant by name when the persona has one (`ASSISTANT_NAME`).
 */
export function ownerGreeting(persona: Persona): string {
  const name = named(persona);
  return name !== undefined ? `Hey — it's ${name}. Got a minute?` : "Hey — it's your assistant. Got a minute?";
}

export function buildOwnerSystemPrompt(
  ownerName: string,
  persona: Persona,
  spokenName?: string,
  contextSnapshot?: string,
): string {
  const name = named(persona);
  const self = name !== undefined ? `You are ${name}, ${ownerName}'s assistant` : `You are ${ownerName}'s assistant`;
  const identity = `${self}, and you are speaking with ${ownerName} directly, live, on the phone. ${persona.ownerDemeanor.replaceAll("{owner}", ownerName)}`;
  const pronounce =
    spokenName !== undefined && spokenName.trim() !== "" && spokenName !== ownerName
      ? `When you say ${ownerName}'s name aloud, write it as "${spokenName}" so it is pronounced correctly.`
      : "";
  const context =
    contextSnapshot !== undefined && contextSnapshot.trim() !== ""
      ? `Here is a snapshot of ${ownerName}'s day (calendar, pending reminders, open loops) — draw on it if it's relevant, but never recite it unasked: ${contextSnapshot.trim()}`
      : "";
  return [
    identity,
    pronounce,
    context,
    `This is a real conversation, not a screening call — there is nobody to screen and nothing to decide about who ${ownerName} is.`,
    `Keep replies short and conversational, the way a real phone call sounds — a sentence or two per turn, never a briefing or a monologue.`,
    `When the conversation is naturally over — ${ownerName} says bye, thanks, talk later, or anything like it — call end_call rather than trailing off or waiting to be asked again.`,
  ]
    .filter((line) => line !== "")
    .join(" ");
}

export const OWNER_TOOLS: ToolSchema[] = [
  {
    name: "end_call",
    description: "End the call because the conversation is naturally over (the owner said goodbye or similar).",
    input_schema: {
      type: "object",
      properties: {},
    },
  },
];
