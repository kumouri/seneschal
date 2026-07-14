/**
 * RelaySession — the Durable Object that holds one ConversationRelay WebSocket
 * (Twilio <-> us) and the conversation state for a single screened call.
 *
 * IMPORTANT: this uses the *non-hibernating* WebSocket API (`server.accept()`),
 * not `state.acceptWebSocket()`. Hibernation evicts the instance between
 * messages, which would reset `callSid` and `history` every turn — breaking
 * both call transfer (empty callSid) and conversation continuity. Calls are
 * short, so keeping the DO in memory for the call's duration is the right trade.
 *
 * Loop: caller speech -> Claude (turn engine) -> spoken reply, until Claude
 * reaches a terminal decision, which transfers the live call (REST), persists
 * the verdict to D1, and texts the owner.
 */
import type { Env } from "../config";
import type { SetupMessage } from "./protocol";
import { parseInbound, textToken } from "./protocol";
import type { LlmClient, LlmTurn } from "../screener/brain";
import { createAnthropicClient } from "../screener/anthropic-client";
import { runCallerTurn, type TerminalAction } from "../screener/conversation";
import { buildSystemPrompt, SCREENER_TOOLS } from "../screener/prompt";
import { configured, personaFromEnv } from "../config";
import { estimateCallCost } from "../budget";
import { addToBlocklist, recordCall } from "../data/db";
import { sendSms } from "../notify/sms";
import { formatVerdictSms } from "../notify/format";
import { redirectToDial, redirectToHangup } from "../twilio/calls";

const MODEL = "claude-haiku-4-5-20251001"; // Haiku 4.5: fast + cheap for real-time turns
const MAX_TURNS = 6; // hard cap so a stalling caller can't run up minutes

export class RelaySession {
  private readonly env: Env;
  private readonly client: LlmClient;
  private readonly system: string;
  private callSid = "";
  private fromE164 = "";
  private toE164 = "";
  private base = "";
  private startedAtMs = 0;
  private history: LlmTurn[] = [];
  private done = false;

  constructor(_state: DurableObjectState, env: Env) {
    this.env = env;
    this.client = createAnthropicClient({ apiKey: env.ANTHROPIC_API_KEY, model: MODEL });
    this.system = buildSystemPrompt(
      configured(env.OWNER_NAME) ?? "the owner",
      env.OWNER_PROFILE,
      personaFromEnv(env),
      env.OWNER_NAME_SPOKEN,
      env.OWNER_PASSWORD,
    );
  }

  async fetch(request: Request): Promise<Response> {
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("expected websocket upgrade", { status: 426 });
    }
    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    server.accept(); // non-hibernating: instance stays in memory for the call
    server.addEventListener("message", (event: MessageEvent) => {
      void this.onMessage(server, event.data);
    });
    return new Response(null, { status: 101, webSocket: client });
  }

  private async onMessage(ws: WebSocket, data: string | ArrayBuffer): Promise<void> {
    try {
      const raw = typeof data === "string" ? data : new TextDecoder().decode(data);
      const msg = parseInbound(raw);

      if (msg.type === "setup") {
        const setup = msg as SetupMessage;
        this.callSid = typeof setup.callSid === "string" ? setup.callSid : "";
        const params = setup.customParameters ?? {};
        this.fromE164 = pick(params["from"], setup.from);
        this.toE164 = pick(params["to"], setup.to);
        this.base = pick(params["base"], undefined);
        this.startedAtMs = Date.now();
        console.log(`setup: callSid=${this.callSid} from=${this.fromE164} to=${this.toE164}`);
        return;
      }

      if (msg.type === "prompt") {
        if (this.done) return;
        const voicePrompt = typeof (msg as { voicePrompt?: unknown }).voicePrompt === "string" ? (msg as { voicePrompt: string }).voicePrompt : "";
        console.log(`prompt: "${voicePrompt}"`);
        const outcome = await runCallerTurn(this.client, this.system, SCREENER_TOOLS, this.history, voicePrompt, MAX_TURNS);
        this.history = outcome.history;
        if (outcome.terminal !== undefined) {
          console.log(`decision: ${outcome.terminal.kind}`);
          this.done = true;
          await this.handleTerminal(outcome.terminal);
        } else if (outcome.reply !== undefined) {
          console.log(`reply: "${outcome.reply}"`);
          ws.send(JSON.stringify(textToken(outcome.reply, true)));
        }
        return;
      }
      // interrupt / other frames: ignore
    } catch (err) {
      console.log(`onMessage error: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  private async handleTerminal(terminal: TerminalAction): Promise<void> {
    if (this.callSid === "") {
      console.log("handleTerminal: missing callSid — cannot transfer/hang up");
      return;
    }
    const id = crypto.randomUUID();
    const started = this.startedAtMs > 0 ? this.startedAtMs : Date.now();
    const startedAt = new Date(started).toISOString();
    const endedAt = new Date().toISOString();
    const elapsedSec = Math.max(1, Math.round((Date.now() - started) / 1000));
    const cost = estimateCallCost("converse", elapsedSec);
    const transcript = this.history.map((t) => `${t.role}: ${t.content}`).join("\n");

    try {
      if (terminal.kind === "connect") {
        await this.alertConnecting(terminal.callerName, terminal.reason);
        await redirectToDial(this.env, this.callSid, this.env.USER_CELL_E164, this.base);
        await recordCall(this.env.DB, {
          id, fromE164: this.fromE164, toE164: this.toE164, startedAt, endedAt,
          outcomeStage: "conversation", verdict: "bridged",
          callerName: terminal.callerName, reason: terminal.reason, transcript, costEstimateUsd: cost,
        });
        return;
      }
      if (terminal.kind === "message") {
        await redirectToHangup(this.env, this.callSid, "Thanks — I'll pass your message along. Goodbye.");
        await recordCall(this.env.DB, {
          id, fromE164: this.fromE164, toE164: this.toE164, startedAt, endedAt,
          outcomeStage: "conversation", verdict: "message",
          callerName: terminal.callerName, reason: terminal.summary, transcript, costEstimateUsd: cost,
        });
        await this.notifyOwner({ verdict: "message", callerName: terminal.callerName, reason: terminal.summary, callbackNumber: terminal.callbackNumber, cost });
        return;
      }
      // spam
      await redirectToHangup(this.env, this.callSid, "This number isn't taking calls. Goodbye.");
      await addToBlocklist(this.env.DB, this.fromE164, `claude_spam:${terminal.reason}`);
      await recordCall(this.env.DB, {
        id, fromE164: this.fromE164, toE164: this.toE164, startedAt, endedAt,
        outcomeStage: "conversation", verdict: "spam",
        reason: terminal.reason, transcript, costEstimateUsd: cost,
      });
      await this.notifyOwner({ verdict: "spam", reason: terminal.reason, cost });
    } catch (err) {
      console.log(`handleTerminal error: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  /** Real-time "pick up!" text so the owner knows who's being transferred to them. */
  private async alertConnecting(callerName: string, reason: string): Promise<void> {
    const who = callerName.trim() !== "" ? callerName.trim() : "a caller";
    const body = reason.trim() !== "" ? `📞 Connecting ${who} — ${reason.trim()}. Pick up!` : `📞 Connecting ${who}. Pick up!`;
    try {
      await sendSms(this.env, this.env.USER_CELL_E164, body);
    } catch {
      // best-effort — don't fail the transfer on an SMS hiccup
    }
  }

  private async notifyOwner(args: {
    verdict: "message" | "spam";
    callerName?: string;
    reason?: string;
    callbackNumber?: string;
    cost: number;
  }): Promise<void> {
    const body = formatVerdictSms({
      verdict: args.verdict,
      fromE164: this.fromE164,
      callerName: args.callerName,
      reason: args.reason,
      callbackNumber: args.callbackNumber,
      costEstimateUsd: args.cost,
    });
    try {
      await sendSms(this.env, this.env.USER_CELL_E164, body);
    } catch (err) {
      console.log(`sms error: ${err instanceof Error ? err.message : String(err)}`);
    }
  }
}

function pick(a: string | undefined, b: string | undefined): string {
  if (typeof a === "string" && a !== "") return a;
  if (typeof b === "string" && b !== "") return b;
  return "";
}
