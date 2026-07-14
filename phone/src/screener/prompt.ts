/**
 * System prompt + tool schemas for the stage-4 Claude conversation. Kept as
 * plain data (no SDK import) so it is host-agnostic and unit-testable. The
 * Durable Object wires these into the Anthropic API in M2.
 */
import type { Persona } from "../persona";

/** Anthropic tool schema shape (a subset, enough for our three tools). */
export interface ToolSchema {
  name: string;
  description: string;
  input_schema: {
    type: "object";
    properties: Record<string, unknown>;
    required?: string[];
  };
}

export function buildSystemPrompt(
  ownerName: string,
  ownerProfile?: string,
  persona?: Persona,
  spokenName?: string,
  ownerPassword?: string,
): string {
  const identity =
    persona !== undefined
      ? persona.demeanor.replaceAll("{owner}", ownerName)
      : `You are a warm, efficient phone call screener for ${ownerName}.`;
  const assistantName =
    persona?.name !== undefined && persona.name.trim() !== ""
      ? `Your name is ${persona.name.trim()}; introduce yourself by it.`
      : "";
  const profile = ownerProfile !== undefined && ownerProfile.trim() !== "" ? `About ${ownerName}: ${ownerProfile.trim()}` : "";
  const pronounce =
    spokenName !== undefined && spokenName.trim() !== "" && spokenName !== ownerName
      ? `When you say ${ownerName}'s name aloud, write it as "${spokenName}" so it is pronounced correctly.`
      : "";
  const ownerAuth =
    ownerPassword !== undefined && ownerPassword.trim() !== ""
      ? `If a caller claims to be ${ownerName}, or you conclude the caller is ${ownerName} themselves, do not simply believe them — with dry amusement, ask for the password, and only treat them as the real ${ownerName} if they say exactly "${ownerPassword.trim()}". Otherwise keep screening normally.`
      : `If a caller claims to be ${ownerName}, or you conclude the caller is ${ownerName} themselves, play along with dry amusement but make them prove it: ask for the password. No real password is set, so keep them on their toes and never actually confirm it's them — it's a running bit.`;
  return [
    identity,
    assistantName,
    `You are screening an unknown caller who just pressed 1 to reach ${ownerName}.`,
    profile,
    pronounce,
    `Speech-to-text sometimes mangles ${ownerName}'s name; treat any close-sounding variant as ${ownerName}, and never tell a caller that ${ownerName} isn't here or quibble over the name.`,
    ownerAuth,
    `Greet them in character and find out who they are and why they're calling, in as few turns as possible — warm and human, never an interrogation. Don't make promises on ${ownerName}'s behalf.`,
    `Use what you know about ${ownerName} to judge the call, then call exactly one tool:`,
    `- connect_call: someone ${ownerName} would want to talk to now (for example a recruiter about a software role, or a genuine personal or appointment call).`,
    `- take_message: legitimate but it can wait, or you're genuinely unsure — capture a concise message.`,
    `- mark_spam: sales, robocalls, scams, fake "support" or "security" calls, warranty or insurance pitches, or anyone evasive about who they are.`,
    `Keep every reply short and natural — one sentence at most. No filler, no gushing, no exclamations; you are warm but brisk, and you keep the call moving.`,
  ]
    .filter((line) => line !== "")
    .join(" ");
}

export const SCREENER_TOOLS: ToolSchema[] = [
  {
    name: "connect_call",
    description: "Connect this legitimate caller to the owner now.",
    input_schema: {
      type: "object",
      properties: {
        caller_name: { type: "string", description: "Who is calling." },
        reason: { type: "string", description: "Why they are calling, one phrase." },
      },
      required: ["caller_name", "reason"],
    },
  },
  {
    name: "take_message",
    description: "Record a message for the owner to follow up on later.",
    input_schema: {
      type: "object",
      properties: {
        caller_name: { type: "string" },
        summary: { type: "string", description: "Concise summary of the message." },
        callback_number: { type: "string", description: "Callback number if given." },
      },
      required: ["caller_name", "summary"],
    },
  },
  {
    name: "mark_spam",
    description: "Flag this call as spam/robocall/scam and end it.",
    input_schema: {
      type: "object",
      properties: {
        reason: { type: "string", description: "Why this is spam." },
      },
      required: ["reason"],
    },
  },
];
