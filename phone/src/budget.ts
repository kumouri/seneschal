/**
 * Cost estimation + the daily budget guard. Budget is the project's primary
 * driver, so every call records an estimated cost and a hard daily cap can
 * downgrade the expensive Claude stage to voicemail for the rest of the day.
 *
 * Figures are USD, approximate, and intentionally conservative (round up).
 */
import type { Stage } from "./screener/decision";

export const COST = {
  /** Declined before answer — Twilio does not bill rejected calls. */
  rejectUsd: 0,
  /** A few seconds of answered call for the press-1 <Gather>. */
  gateUsd: 0.02,
  /** ConversationRelay ($0.07) + inbound voice (~$0.0085) per minute. */
  conversationPerMinUsd: 0.0785,
  /** Outbound leg when we bridge to the user's cell, per minute. */
  bridgePerMinUsd: 0.014,
} as const;

/** Estimate the marginal Twilio cost of a call given its terminal stage. */
export function estimateCallCost(stage: Stage, answeredSeconds: number): number {
  switch (stage) {
    case "reject":
      return COST.rejectUsd;
    case "allow":
      // Bridged straight through: just the outbound leg.
      return round(COST.bridgePerMinUsd * minutes(answeredSeconds));
    case "gate":
      return COST.gateUsd;
    case "converse":
      return round(COST.conversationPerMinUsd * minutes(answeredSeconds));
    default:
      return 0;
  }
}

export interface DailySpend {
  /** UTC date, YYYY-MM-DD. */
  dateUtc: string;
  spentUsd: number;
}

/**
 * True when today's spend has reached the cap. When over budget the caller
 * router should downgrade stage 4 (converse) to gate-only + voicemail.
 */
export function overBudget(spend: DailySpend, todayUtc: string, capUsd: number): boolean {
  if (capUsd <= 0) return false; // 0 / negative cap = disabled
  if (spend.dateUtc !== todayUtc) return false; // stale counter, new day
  return spend.spentUsd >= capUsd;
}

function minutes(seconds: number): number {
  // Twilio bills per started minute.
  return Math.max(1, Math.ceil(seconds / 60));
}

function round(usd: number): number {
  // 4 dp = tenth-of-a-cent; enough precision that a 1-minute call equals the
  // per-minute rate exactly (rounding to cents would distort sub-cent rates).
  return Math.round(usd * 10000) / 10000;
}
