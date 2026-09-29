/**
 * RelaySession — the Durable Object that holds one ConversationRelay WebSocket
 * (Twilio <-> us) and the conversation state for a single call — a screened
 * inbound call (`mode: "screener"`, the default) or an outbound talk-mode call
 * with the owner (`mode: "owner"`, seeded by a POST before the call is placed;
 * see `seed`, `loadSeed` and `resolveSessionMode`).
 *
 * IMPORTANT: this uses the *non-hibernating* WebSocket API (`server.accept()`),
 * not `state.acceptWebSocket()`. Hibernation evicts the instance between
 * messages, which would reset `callSid` and `history` every turn — breaking
 * both call transfer (empty callSid) and conversation continuity. Calls are
 * short, so keeping the DO in memory for the call's duration is the right trade.
 *
 * Loop: caller speech -> Claude (turn engine) -> spoken reply, until Claude
 * reaches a terminal decision, which transfers the live call (REST), persists
 * the verdict to D1, and notifies the owner (Telegram, or SMS as the fallback —
 * `../notify/owner.ts`).
 */
import type { Env } from "../config";
import type { SetupMessage } from "./protocol";
import { parseInbound, textToken } from "./protocol";
import type { LlmClient, LlmTurn } from "../screener/brain";
import { createAnthropicClient } from "../screener/anthropic-client";
import { runCallerTurn, type TerminalAction } from "../screener/conversation";
import { buildSystemPrompt, SCREENER_TOOLS } from "../screener/prompt";
import { runOwnerTurn } from "./owner-conversation";
import { buildOwnerSystemPrompt, OWNER_TOOLS } from "./owner-prompt";
import { configured, personaFromEnv } from "../config";
import { estimateCallCost } from "../budget";
import { addToBlocklist, recordCall } from "../data/db";
import { notifyOwner } from "../notify/owner";
import { formatVerdictSms } from "../notify/format";
import { redirectToDial, redirectToHangup } from "../twilio/calls";

// Shared by both the screener and owner-mode (talk) conversations. Haiku 4.5: fast + cheap, the
// right call for real-time turns; a larger model would read warmer but costs more per minute.
const MODEL = "claude-haiku-4-5-20251001";
const MAX_TURNS = 6; // hard cap so a stalling caller can't run up minutes
const MAX_OWNER_TURNS = 40; // a real conversation runs much longer than a screening call

export type SessionMode = "screener" | "owner";

/**
 * Owner mode requires BOTH the persisted DO seed AND the talk TwiML's `mode=owner` `<Parameter>`
 * to agree — a lone signal (a stale/evicted seed with no param, or a param with no real seed)
 * falls back to screener rather than risk the owner conversation on a half-signal. Pure (unit-
 * tested) so the two-signal rule can't silently drift from what `onMessage` actually does.
 */
export function resolveSessionMode(storageMode: SessionMode, paramMode: string | undefined): SessionMode {
  return storageMode === "owner" && paramMode === "owner" ? "owner" : "screener";
}

export class RelaySession {
  private readonly env: Env;
  private readonly state: DurableObjectState;
  private readonly client: LlmClient;
  private mode: SessionMode = "screener";
  private ownerContext = "";
  private callSid = "";
  private fromE164 = "";
  private toE164 = "";
  private base = "";
  private startedAtMs = 0;
  private history: LlmTurn[] = [];
  private done = false;

  constructor(state: DurableObjectState, env: Env) {
    this.env = env;
    this.state = state;
    this.client = createAnthropicClient({ apiKey: env.ANTHROPIC_API_KEY, model: MODEL });
    // The phone rings for many seconds between `seed()` and the ConversationRelay handshake; an
    // idle DO can be evicted in between, and a fresh instance's fields start back at the
    // "screener" default. `blockConcurrencyWhile` holds off `fetch()` (both the seed POST and the
    // WS upgrade) until any persisted seed from a prior instance is reloaded, so eviction can't
    // silently drop owner mode back to the screener persona mid-call.
    void state.blockConcurrencyWhile(() => this.loadSeed());
  }

  private async loadSeed(): Promise<void> {
    const mode = await this.state.storage.get<string>("mode");
    if (mode === "owner") {
      this.mode = "owner";
      this.ownerContext = (await this.state.storage.get<string>("ownerContext")) ?? "";
    }
  }

  /** The active system prompt, computed on demand since `mode` may be seeded after construction. */
  private systemPrompt(): string {
    const owner = configured(this.env.OWNER_NAME) ?? "the owner";
    const persona = personaFromEnv(this.env);
    if (this.mode === "owner") {
      return buildOwnerSystemPrompt(owner, persona, this.env.OWNER_NAME_SPOKEN, this.ownerContext);
    }
    return buildSystemPrompt(owner, this.env.OWNER_PROFILE, persona, this.env.OWNER_NAME_SPOKEN, this.env.OWNER_PASSWORD);
  }

  async fetch(request: Request): Promise<Response> {
    if (request.headers.get("Upgrade") !== "websocket") {
      if (request.method === "POST") return this.seed(request);
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

  /**
   * Seed this instance into owner (talk) mode BEFORE the Twilio call is placed, so the
   * ConversationRelay handshake — which arrives moments later, hitting this same Durable Object by
   * `idFromName(sessionId)` — finds the mode and context already set. POSTed by
   * `handlePushCallTalk` in `../index.ts`; never reachable from an inbound (screened) call, whose
   * `/ws` route only ever forwards WebSocket upgrades. Persisted to `state.storage` (not just
   * instance fields) so an eviction between this POST and the WebSocket handshake — `loadSeed()` in
   * the constructor — doesn't lose it.
   */
  private async seed(request: Request): Promise<Response> {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }
    const obj = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
    if (obj.mode === "owner") {
      this.mode = "owner";
      this.ownerContext = typeof obj.context === "string" ? obj.context : "";
      await this.state.storage.put({ mode: "owner", ownerContext: this.ownerContext });
    }
    return new Response("ok");
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
        // Belt and braces on top of `loadSeed()`: the talk TwiML (only the Worker generates it, so
        // it's trusted) also carries a `mode=owner` <Parameter>, which arrives here as
        // `customParameters`. `resolveSessionMode` requires both signals to agree.
        const paramMode = params["mode"];
        const storageWantsOwner = this.mode === "owner";
        if (storageWantsOwner !== (paramMode === "owner")) {
          console.log(
            `setup: owner-mode signals disagree (storage=${storageWantsOwner ? "owner" : "screener"}, param=${paramMode ?? "none"}) — falling back to screener`,
          );
        }
        this.mode = resolveSessionMode(this.mode, paramMode);
        if (this.mode !== "owner") this.ownerContext = "";
        console.log(`setup: callSid=${this.callSid} from=${this.fromE164} to=${this.toE164} mode=${this.mode}`);
        return;
      }

      if (msg.type === "prompt") {
        if (this.done) return;
        const voicePrompt = typeof (msg as { voicePrompt?: unknown }).voicePrompt === "string" ? (msg as { voicePrompt: string }).voicePrompt : "";
        console.log(`prompt: "${voicePrompt}"`);

        if (this.mode === "owner") {
          const outcome = await runOwnerTurn(this.client, this.systemPrompt(), OWNER_TOOLS, this.history, voicePrompt, MAX_OWNER_TURNS);
          this.history = outcome.history;
          if (outcome.ended === true) {
            console.log("owner call: ended");
            this.done = true;
            await this.endOwnerCall();
          } else if (outcome.reply !== undefined) {
            console.log(`reply: "${outcome.reply}"`);
            ws.send(JSON.stringify(textToken(outcome.reply, true)));
          }
          return;
        }

        const outcome = await runCallerTurn(this.client, this.systemPrompt(), SCREENER_TOOLS, this.history, voicePrompt, MAX_TURNS);
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

  /**
   * End a talk-mode (owner) call: say a short goodbye and hang up, then record the call's estimated
   * cost against the same daily budget the screener draws from (`sumSpendToday`/`overBudget`). No
   * screener actions apply here — no transfer, no blocklist, no escalation, no voicemail; the person
   * on the line IS the owner, there is nothing to route them to.
   */
  private async endOwnerCall(): Promise<void> {
    if (this.callSid === "") {
      console.log("endOwnerCall: missing callSid — cannot hang up");
      return;
    }
    const started = this.startedAtMs > 0 ? this.startedAtMs : Date.now();
    const elapsedSec = Math.max(1, Math.round((Date.now() - started) / 1000));
    const cost = estimateCallCost("converse", elapsedSec);
    const transcript = this.history.map((t) => `${t.role}: ${t.content}`).join("\n");
    const owner = this.env.USER_CELL_E164 ?? "";
    try {
      await redirectToHangup(this.env, this.callSid, "Bye — talk soon.");
    } catch (err) {
      console.log(`endOwnerCall hangup error: ${err instanceof Error ? err.message : String(err)}`);
    }
    try {
      await recordCall(this.env.DB, {
        id: crypto.randomUUID(),
        fromE164: owner,
        toE164: owner,
        startedAt: new Date(started).toISOString(),
        endedAt: new Date().toISOString(),
        outcomeStage: "talk",
        verdict: "talk",
        transcript,
        costEstimateUsd: cost,
      });
    } catch (err) {
      console.log(`endOwnerCall recordCall error: ${err instanceof Error ? err.message : String(err)}`);
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
        await redirectToDial(this.env, this.callSid, this.base, this.fromE164);
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
        await this.notifyVerdict({ verdict: "message", callerName: terminal.callerName, reason: terminal.summary, callbackNumber: terminal.callbackNumber, cost });
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
      await this.notifyVerdict({ verdict: "spam", reason: terminal.reason, cost });
    } catch (err) {
      console.log(`handleTerminal error: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  /** Real-time "pick up!" note so the owner knows who's being transferred to them. */
  private async alertConnecting(callerName: string, reason: string): Promise<void> {
    const who = callerName.trim() !== "" ? callerName.trim() : "a caller";
    const body = reason.trim() !== "" ? `📞 Connecting ${who} — ${reason.trim()}. Pick up!` : `📞 Connecting ${who}. Pick up!`;
    try {
      await notifyOwner(this.env, body);
    } catch {
      // best-effort — don't fail the transfer on a notification hiccup
    }
  }

  private async notifyVerdict(args: {
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
      await notifyOwner(this.env, body);
    } catch (err) {
      console.log(`notify error: ${err instanceof Error ? err.message : String(err)}`);
    }
  }
}

function pick(a: string | undefined, b: string | undefined): string {
  if (typeof a === "string" && a !== "") return a;
  if (typeof b === "string" && b !== "") return b;
  return "";
}
