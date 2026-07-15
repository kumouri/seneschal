/**
 * Core types for the tiered screening funnel.
 *
 * A `Stage` is *what we do next* with a call; a `Verdict` is *how it ended up*
 * (recorded for the call log). Keeping these as plain data makes the funnel
 * logic in `funnel.ts` pure and trivially unit-testable.
 */

/** The next action the funnel chooses for an inbound call. */
export type Stage = "allow" | "reject" | "gate" | "converse";

/** Terminal outcome recorded in the call log. */
export type Verdict =
  | "allowed" // allowlisted contact, dialed straight through
  | "bridged" // screened, then connected to the user
  | "message" // caller left a message
  | "spam" // identified as spam / robocall
  | "gate_fail" // never pressed 1 — almost certainly a robodialer
  | "rejected"; // blocklisted, declined before answering ($0)

/** What happens to a caller who passes the press-1 gate. */
export type PostGateAction = "converse" | "ring_through";

/** Minimal facts about an inbound call, derived from Twilio webhook params. */
export interface CallerInfo {
  /** Caller's number in E.164, or "" when anonymous / withheld. */
  fromE164: string;
  /** Our Twilio number that was dialed, E.164. */
  toE164: string;
  /** False when the call arrived with no usable caller ID. */
  hasCallerId: boolean;
}

/** Result of the two free D1 list lookups. */
export interface ListLookup {
  isAllowlisted: boolean;
  isBlocklisted: boolean;
}

/** Operator-tunable settings (merged from env defaults + the `settings` row). */
export interface ScreenerSettings {
  userCellE164: string;
  gatePrompt: string;
  postGateAction: PostGateAction;
  reputationLookupEnabled: boolean;
  dailyBudgetUsd: number;
}

/** A funnel decision plus a short machine-readable reason (for logs). */
export interface FunnelDecision {
  stage: Stage;
  reason: string;
}
