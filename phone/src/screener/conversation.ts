/**
 * Stage-4 conversation control. Drives one caller turn at a time, keeping the
 * running history and enforcing a hard turn cap so a stalling caller can never
 * run up ConversationRelay minutes — past the cap we wrap up with a message.
 *
 * Pure aside from the injected `LlmClient`, so it is fully unit-testable.
 */
import type { LlmClient, LlmTurn, ScreenAction } from "./brain";
import { screenTurn } from "./brain";
import type { ToolSchema } from "./prompt";

export type TerminalAction = Extract<ScreenAction, { kind: "connect" | "message" | "spam" }>;

export interface TurnOutcome {
  /** Assistant text to speak back, when the screen is not yet concluded. */
  reply?: string;
  /** Terminal action (connect / message / spam) when the screen concluded. */
  terminal?: TerminalAction;
  /** Updated history to carry into the next turn. */
  history: LlmTurn[];
}

export async function runCallerTurn(
  client: LlmClient,
  system: string,
  tools: ToolSchema[],
  history: LlmTurn[],
  callerText: string,
  maxTurns: number,
): Promise<TurnOutcome> {
  const withCaller: LlmTurn[] = [...history, { role: "user", content: callerText }];
  const action = await screenTurn(client, system, tools, withCaller);

  if (action.kind !== "say") {
    return { terminal: action, history: withCaller };
  }

  // If we've already spoken maxTurns times and still have no decision, stop
  // burning minutes — take a message instead of letting the caller ramble.
  const assistantTurns = withCaller.filter((t) => t.role === "assistant").length;
  if (assistantTurns >= maxTurns) {
    return {
      terminal: {
        kind: "message",
        callerName: "Unknown caller",
        summary: "Caller did not state their business within the turn limit.",
      },
      history: withCaller,
    };
  }

  const newHistory: LlmTurn[] = [...withCaller, { role: "assistant", content: action.text }];
  return { reply: action.text, history: newHistory };
}
