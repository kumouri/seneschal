/**
 * The heart of the screener: a pure function that decides what to do with an
 * inbound call, cheapest checks first so most spam dies at $0.
 *
 *   1. allowlisted contact  -> allow   (Dial straight through, no screening)
 *   2. blocklisted number   -> reject  (declined before answer => free)
 *   3. everyone else        -> gate    (press-1 challenge; robodialers fail cheap)
 *
 * Stage 4 (the paid Claude conversation) is only reached *after* a caller
 * passes the gate — see `decidePostGate`.
 */
import { evaluateHeuristics } from "../filter/heuristics";
import type { CallerInfo, FunnelDecision, ListLookup, ScreenerSettings } from "./decision";

export function decideFunnel(
  caller: CallerInfo,
  lists: ListLookup,
  settings: ScreenerSettings,
): FunnelDecision {
  // 0. Self-call guard: a call *from* our own bridge target (the user's cell)
  //    must never reach the "allow" path, or we'd <Dial> the very line that's
  //    calling. That line is busy (it's placing the call), so Twilio rolls
  //    straight to its voicemail — which looks exactly like the screener being
  //    "down". Route it through the gate instead, so the owner can still reach
  //    the screener when self-testing.
  if (settings.userCellE164 !== "" && caller.fromE164 === settings.userCellE164) {
    return { stage: "gate", reason: "gate:self_call" };
  }

  // 1. Known-good contact: ring through, never screened.
  if (lists.isAllowlisted) {
    return { stage: "allow", reason: "allowlisted_contact" };
  }

  // 2. Known-bad number: reject *before answering* so Twilio never bills us.
  if (lists.isBlocklisted) {
    return { stage: "reject", reason: "blocklisted" };
  }

  // 3. Unknown: send to the cheap press-1 gate. Heuristics don't hard-reject
  //    (avoid false positives) but are recorded so the gate-fail -> blocklist
  //    learning loop and Claude get the signal.
  const heur = evaluateHeuristics(caller, settings.userCellE164);
  const reason = heur.suspicious ? `gate:${heur.reasons.join(",")}` : "gate:unknown_caller";
  return { stage: "gate", reason };
}

/**
 * After a caller presses 1, decide whether Claude screens them (`converse`)
 * or we simply ring the user (`ring_through`). This is the operator's main
 * cost/richness knob.
 */
export function decidePostGate(settings: ScreenerSettings): FunnelDecision {
  if (settings.postGateAction === "ring_through") {
    return { stage: "allow", reason: "gate_passed_ring_through" };
  }
  return { stage: "converse", reason: "gate_passed_converse" };
}
