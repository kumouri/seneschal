/**
 * D1 (SQLite) access. The two hot-path lookups (allowlist / blocklist) are the
 * cheap, deterministic rails the funnel runs before spending anything. D1 is a
 * rebuildable index over the canonical call records.
 */
import type { ListLookup, ScreenerSettings, Verdict } from "../screener/decision";

/** Both free list lookups in one place. Anonymous callers ("") match neither. */
export async function lookupLists(db: D1Database, fromE164: string): Promise<ListLookup> {
  if (fromE164 === "") return { isAllowlisted: false, isBlocklisted: false };
  const allow = await db.prepare("SELECT 1 FROM contacts WHERE number_e164 = ? LIMIT 1").bind(fromE164).first();
  const block = await db.prepare("SELECT 1 FROM blocklist WHERE number_e164 = ? LIMIT 1").bind(fromE164).first();
  return { isAllowlisted: allow !== null, isBlocklisted: block !== null };
}

/** Learning loop: remember a spam number so future calls are rejected for $0. */
export async function addToBlocklist(db: D1Database, fromE164: string, reason: string): Promise<void> {
  if (fromE164 === "") return;
  const now = new Date().toISOString();
  await db
    .prepare(
      `INSERT INTO blocklist (number_e164, reason, confidence, first_seen, last_seen, hit_count)
       VALUES (?1, ?2, 1.0, ?3, ?3, 1)
       ON CONFLICT(number_e164) DO UPDATE SET last_seen = ?3, hit_count = hit_count + 1`,
    )
    .bind(fromE164, reason, now)
    .run();
}

/** Wrong-key gate failures before a number is blocklisted. Two, not one — humans fumble keypads. */
export const GATE_FAIL_BLOCK_AFTER = 2;

/**
 * Learning loop for the free tier (the M4 gate-fail escalation): log a press-1
 * gate failure and escalate to the blocklist — rejected for $0 at stage 2 from
 * then on, and served to the on-device blocker via GET /blocklist. How fast
 * depends on HOW they failed: a **silent timeout** (pressed nothing at all)
 * blocklists on the FIRST strike — humans mash keys, only robots say nothing —
 * while a **wrong key** gets GATE_FAIL_BLOCK_AFTER strikes of grace. Anonymous
 * callers can't be counted; allowlisted contacts are never counted
 * (contacts-sync lag shouldn't blocklist a friend on a bad speakerphone day).
 * Returns the running count.
 */
export async function recordGateFail(db: D1Database, fromE164: string, toE164: string, silent: boolean): Promise<number> {
  if (fromE164 === "") return 0;
  const allowed = await db.prepare("SELECT 1 FROM contacts WHERE number_e164 = ? LIMIT 1").bind(fromE164).first();
  if (allowed !== null) return 0;
  const now = new Date().toISOString();
  await recordCall(db, {
    id: crypto.randomUUID(),
    fromE164,
    toE164,
    startedAt: now,
    endedAt: now,
    outcomeStage: "gate_fail",
    verdict: "gate_fail",
    reason: silent ? "silent_timeout" : "wrong_key",
    costEstimateUsd: 0,
  });
  const row = await db
    .prepare("SELECT COUNT(*) AS n FROM calls WHERE from_e164 = ?1 AND outcome_stage = 'gate_fail'")
    .bind(fromE164)
    .first<{ n: number }>();
  const failures = row?.n ?? 1;
  if (silent || failures >= GATE_FAIL_BLOCK_AFTER) await addToBlocklist(db, fromE164, "gate_fail");
  return failures;
}

export interface CallRecord {
  id: string;
  fromE164: string;
  toE164: string;
  startedAt: string;
  endedAt: string;
  outcomeStage: string;
  verdict: Verdict;
  callerName?: string;
  reason?: string;
  transcript?: string;
  costEstimateUsd: number;
}

export async function recordCall(db: D1Database, rec: CallRecord): Promise<void> {
  await db
    .prepare(
      `INSERT INTO calls
        (id, from_e164, to_e164, started_at, ended_at, outcome_stage, verdict, caller_name, reason, transcript, cost_estimate_usd)
       VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
    )
    .bind(
      rec.id,
      rec.fromE164,
      rec.toE164,
      rec.startedAt,
      rec.endedAt,
      rec.outcomeStage,
      rec.verdict,
      rec.callerName ?? null,
      rec.reason ?? null,
      rec.transcript ?? null,
      rec.costEstimateUsd,
    )
    .run();
}

/**
 * Single-row settings overrides. Env supplies defaults today; this lets the
 * owner tweak behavior at runtime in a later milestone.
 */
export async function loadSettingsRow(_db: D1Database): Promise<Partial<ScreenerSettings>> {
  // TODO(M3/M5): read overrides from the `settings` table.
  return {};
}

export interface GoogleContact {
  numberE164: string;
  name: string;
}

/** All blocklisted numbers, for the on-device blocker app to sync. */
export async function listBlocklist(db: D1Database): Promise<string[]> {
  const res = await db.prepare("SELECT number_e164 FROM blocklist ORDER BY number_e164").all<{ number_e164: string }>();
  return (res.results ?? []).map((r) => r.number_e164);
}

/**
 * Pure reconcile step: given the Google-sourced numbers already in D1 and the
 * freshly-pushed contacts, decide what to upsert and which stale Google numbers
 * to remove. Blanks and duplicates are dropped. (manual entries aren't touched.)
 */
export function reconcileContacts(
  existingGoogleNumbers: ReadonlySet<string>,
  incoming: ReadonlyArray<GoogleContact>,
): { toUpsert: GoogleContact[]; toRemove: string[] } {
  const seen = new Set<string>();
  const toUpsert: GoogleContact[] = [];
  for (const c of incoming) {
    if (c.numberE164 !== "" && !seen.has(c.numberE164)) {
      seen.add(c.numberE164);
      toUpsert.push(c);
    }
  }
  const toRemove: string[] = [];
  for (const num of existingGoogleNumbers) {
    if (!seen.has(num)) toRemove.push(num);
  }
  return { toUpsert, toRemove };
}

/**
 * Replace the Google-sourced slice of the allowlist with `contacts`. Upserts are
 * tagged source='google'; numbers no longer in Google are dropped. Entries added
 * by hand (source='manual') are preserved — the upsert's WHERE guard won't
 * overwrite them, and removal only targets source='google'.
 */
export async function syncGoogleContacts(
  db: D1Database,
  contacts: ReadonlyArray<GoogleContact>,
): Promise<{ upserted: number; removed: number }> {
  const existing = await db.prepare("SELECT number_e164 FROM contacts WHERE source = 'google'").all<{ number_e164: string }>();
  const existingNumbers = new Set((existing.results ?? []).map((r) => r.number_e164));
  const { toUpsert, toRemove } = reconcileContacts(existingNumbers, contacts);
  const now = new Date().toISOString();

  const stmts: D1PreparedStatement[] = [
    ...toUpsert.map((c) =>
      db
        .prepare(
          `INSERT INTO contacts (number_e164, name, source, created_at)
           VALUES (?1, ?2, 'google', ?3)
           ON CONFLICT(number_e164) DO UPDATE SET name = ?2, source = 'google' WHERE source = 'google'`,
        )
        .bind(c.numberE164, c.name, now),
    ),
    ...toRemove.map((num) => db.prepare("DELETE FROM contacts WHERE number_e164 = ?1 AND source = 'google'").bind(num)),
  ];
  if (stmts.length > 0) await db.batch(stmts);
  return { upserted: toUpsert.length, removed: toRemove.length };
}
