/**
 * Twilio ConversationRelay WebSocket protocol (JSON text frames, not audio).
 *
 * Inbound (Twilio -> us):
 *   setup    — once, with the callSid and call metadata
 *   prompt   — a chunk of transcribed caller speech (voicePrompt)
 *   interrupt— the caller barged in over our TTS
 *
 * Outbound (us -> Twilio):
 *   text     — a token of the assistant's reply; `last: true` ends the turn
 */

export interface SetupMessage {
  type: "setup";
  callSid: string;
  from?: string;
  to?: string;
  /** Values from the <Parameter> tags we put on <ConversationRelay>. */
  customParameters?: Record<string, string>;
}

export interface PromptMessage {
  type: "prompt";
  voicePrompt: string;
  last?: boolean;
}

export interface InterruptMessage {
  type: "interrupt";
  utteranceUntilInterrupt?: string;
}

export type InboundMessage =
  | SetupMessage
  | PromptMessage
  | InterruptMessage
  | { type: string; [key: string]: unknown };

export interface TextMessage {
  type: "text";
  token: string;
  last: boolean;
}

/** Build an outbound text-token frame. */
export function textToken(token: string, last: boolean): TextMessage {
  return { type: "text", token, last };
}

/** Parse an inbound frame; returns a `{ type: "unknown" }`-shaped object on bad JSON. */
export function parseInbound(raw: string): InboundMessage {
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (typeof parsed === "object" && parsed !== null && typeof (parsed as { type?: unknown }).type === "string") {
      return parsed as InboundMessage;
    }
    return { type: "unknown" };
  } catch {
    return { type: "unknown" };
  }
}
