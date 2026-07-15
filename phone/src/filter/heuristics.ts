/**
 * Free, no-network heuristics that flag a caller as suspicious. These never
 * *hard-reject* on their own (too risky for false positives) — they route the
 * caller to the cheap press-1 gate and add color to the call log / Claude's
 * context. The only deterministic free reject is an explicit blocklist hit.
 */
import type { CallerInfo } from "../screener/decision";

export interface HeuristicResult {
  suspicious: boolean;
  reasons: string[];
}

interface NanpParts {
  area: string; // NPA — 3 digits
  prefix: string; // NXX — 3 digits
  line: string; // 4 digits
}

/** Parse a US/Canada E.164 number (+1 + 10 digits) into its parts, or null. */
export function parseNanp(e164: string): NanpParts | null {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164);
  if (m === null) return null;
  return { area: m[1]!, prefix: m[2]!, line: m[3]! };
}

/**
 * Classic "neighbor spoofing": the caller shares the user's area code AND
 * 3-digit prefix but is a different line. Spammers do this so the call looks
 * local. Same exact number is excluded (that's not neighbor-spoofing).
 */
export function isNeighborSpoof(fromE164: string, userCellE164: string): boolean {
  const a = parseNanp(fromE164);
  const b = parseNanp(userCellE164);
  if (a === null || b === null) return false;
  return a.area === b.area && a.prefix === b.prefix && a.line !== b.line;
}

export function evaluateHeuristics(caller: CallerInfo, userCellE164: string): HeuristicResult {
  const reasons: string[] = [];
  if (!caller.hasCallerId || caller.fromE164 === "") reasons.push("anonymous_or_no_caller_id");
  if (isNeighborSpoof(caller.fromE164, userCellE164)) reasons.push("neighbor_spoof");
  return { suspicious: reasons.length > 0, reasons };
}
