/**
 * Format the SMS the owner receives after a screened call. Pure + tested so the
 * wording/cost line is reliable; the actual send lives in `sms.ts`.
 */
import type { Verdict } from "../screener/decision";

export interface CallSummary {
  verdict: Verdict;
  fromE164: string;
  callerName?: string;
  reason?: string;
  callbackNumber?: string;
  costEstimateUsd: number;
}

function nonEmpty(s: string | undefined): boolean {
  return s !== undefined && s.trim() !== "";
}

export function formatVerdictSms(s: CallSummary): string {
  const who = nonEmpty(s.callerName) ? (s.callerName as string) : s.fromE164 !== "" ? s.fromE164 : "Unknown caller";
  const lines: string[] = [];

  switch (s.verdict) {
    case "bridged":
      lines.push(`Connected: ${who}`);
      break;
    case "message":
      lines.push(`Message from ${who}`);
      break;
    case "spam":
      lines.push(`Blocked spam: ${who}`);
      break;
    case "gate_fail":
      lines.push(`Dropped at gate: ${who}`);
      break;
    case "rejected":
      lines.push(`Rejected (blocklisted): ${who}`);
      break;
    default:
      lines.push(`Call from ${who}`);
  }

  if (nonEmpty(s.reason)) lines.push((s.reason as string).trim());
  if (nonEmpty(s.callbackNumber)) lines.push(`Callback: ${(s.callbackNumber as string).trim()}`);
  lines.push(`(~$${s.costEstimateUsd.toFixed(2)})`);

  return lines.join("\n");
}
