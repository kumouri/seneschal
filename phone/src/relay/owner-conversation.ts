/**
 * Talk-mode conversation control — the owner-mode sibling of `screener/conversation.ts`. There is no
 * triage to conclude (connect/message/spam): the call just runs until the owner says goodbye (the
 * model calls `end_call`) or a turn cap is hit, so a stuck model can't run up ConversationRelay
 * minutes indefinitely on the owner's own line either.
 *
 * Pure aside from the injected `LlmClient`, so it is fully unit-testable.
 */
import type { LlmClient, LlmTurn } from "../screener/brain";
import type { ToolSchema } from "../screener/prompt";

export interface OwnerTurnOutcome {
  /** Assistant text to speak back, when the call is still going. */
  reply?: string;
  /** True once the call should end (the owner said goodbye, or the turn cap was hit). */
  ended?: boolean;
  /** Updated history to carry into the next turn. */
  history: LlmTurn[];
}

export async function runOwnerTurn(
  client: LlmClient,
  system: string,
  tools: ToolSchema[],
  history: LlmTurn[],
  ownerText: string,
  maxTurns: number,
): Promise<OwnerTurnOutcome> {
  const withOwner: LlmTurn[] = [...history, { role: "user", content: ownerText }];
  const result = await client.respond(system, tools, withOwner);

  if (result.toolUse?.name === "end_call") {
    return { ended: true, history: withOwner };
  }

  const text = result.text ?? "";
  const assistantTurns = withOwner.filter((t) => t.role === "assistant").length;
  if (assistantTurns >= maxTurns) {
    return { ended: true, history: withOwner };
  }

  return { reply: text, history: [...withOwner, { role: "assistant", content: text }] };
}
