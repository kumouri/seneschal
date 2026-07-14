/**
 * Stage-4 conversation logic. The Anthropic API call lives behind the
 * `LlmClient` interface so the decision mapping is pure and testable without a
 * network or API key. M2 supplies a real client (Anthropic SDK over fetch);
 * tests supply a fake.
 */
import type { ToolSchema } from "./prompt";

export type ScreenAction =
  | { kind: "say"; text: string }
  | { kind: "connect"; callerName: string; reason: string }
  | { kind: "message"; callerName: string; summary: string; callbackNumber?: string }
  | { kind: "spam"; reason: string };

export interface LlmTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ToolUse {
  name: string;
  input: Record<string, unknown>;
}

/** What the model returned for one turn: either plain text or a tool call. */
export interface LlmResult {
  text?: string;
  toolUse?: ToolUse;
}

export interface LlmClient {
  respond(system: string, tools: ToolSchema[], history: LlmTurn[]): Promise<LlmResult>;
}

function str(input: Record<string, unknown>, key: string, fallback = ""): string {
  const v = input[key];
  return typeof v === "string" ? v : fallback;
}

/** Map a model tool-use into a terminal screener action. Pure. */
export function interpretToolUse(use: ToolUse): ScreenAction {
  switch (use.name) {
    case "connect_call":
      return { kind: "connect", callerName: str(use.input, "caller_name"), reason: str(use.input, "reason") };
    case "take_message": {
      const callback = str(use.input, "callback_number");
      const base = { kind: "message" as const, callerName: str(use.input, "caller_name"), summary: str(use.input, "summary") };
      return callback === "" ? base : { ...base, callbackNumber: callback };
    }
    case "mark_spam":
      return { kind: "spam", reason: str(use.input, "reason", "unspecified") };
    default:
      // Unknown tool — keep the conversation going rather than acting blindly.
      return { kind: "say", text: "Sorry, could you repeat that?" };
  }
}

/** Run one screening turn: ask the model, then map its output to an action. */
export async function screenTurn(
  client: LlmClient,
  system: string,
  tools: ToolSchema[],
  history: LlmTurn[],
): Promise<ScreenAction> {
  const result = await client.respond(system, tools, history);
  if (result.toolUse !== undefined) return interpretToolUse(result.toolUse);
  return { kind: "say", text: result.text ?? "" };
}
