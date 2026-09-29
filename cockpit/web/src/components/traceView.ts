// The Trace panel's view logic, kept DOM-free so it can be pinned by `traceView.test.ts` rather
// than by comments (cockpit/CLAUDE.md, same posture as `jobsCollapse.ts` and `routerRow.ts`).
//
// Three jobs, one theme: a turn is ONE exchange but MANY events, and reading it as a flat list is
// what made the panel unusable. Grouping restores the exchange; the roll-up puts the turn's real
// cost on its header; the search narrows 374 events to the handful you came for.
//
// TOLERANT THROUGHOUT. This reads a payload written by a backend that may be on a different commit,
// so every field is interrogated rather than trusted, and the failure always lands on "showed the
// row plainly" — never on dropping an event or crashing the panel.

import type { TraceEvent, TraceSaid, TraceSession } from '../types'

/** A run of consecutive events sharing one `turn_id` — or a single event that has none (a spawn
 *  decision, an assertion), which stays exactly where it falls chronologically. */
export interface TurnGroup {
  key: string
  turnId: string | null
  /** Every event in the turn, filtered or not — the roll-up below is computed from all of them. */
  events: TraceEvent[]
  /** The turn's verbatim text, hoisted so a COLLAPSED turn still shows the exchange itself. */
  said: TraceSaid[]
  redacted: boolean
  startedAt: string | null
  durationMs: number | null
  costUsd: number | null
  tokens: number | null
  toolCalls: number
  errors: number
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Hoists a turn's text onto its header, refusing a segment it has already taken.
 *
 *  BELT-AND-BRACES, not the fix. `server/trace.py` now anchors each side's text to one row, so in
 *  practice there is nothing to skip. But this header is built by concatenating every event in the
 *  turn, and before that backend change a 68-row turn carried 68 copies — a header that printed the
 *  owner's sentence sixty-eight times would be a worse bug than the one it replaced. Cheap insurance
 *  against a duplicate arriving from anywhere: a re-send, a resumed turn, a backend rolled back. */
function pushUnique(into: TraceSaid[], incoming: readonly TraceSaid[]): void {
  for (const s of incoming) {
    if (!s || typeof s !== 'object') continue
    const dup = into.some((k) => k.speaker === s.speaker && k.at === s.at && k.text === s.text)
    if (!dup) into.push(s)
  }
}

/** Consecutive runs, NOT a group-by. Events arrive sorted by timestamp, and a spawn row can land
 *  mid-turn; merging non-adjacent runs would reorder the log to make the grouping tidier, which is
 *  the one thing a chronological trace must never do. */
export function groupIntoTurns(events: readonly TraceEvent[] | null | undefined): TurnGroup[] {
  const groups: TurnGroup[] = []
  let current: TurnGroup | null = null

  for (const e of events ?? []) {
    const tid = typeof e?.turn_id === 'string' && e.turn_id ? e.turn_id : null
    if (!tid || !current || current.turnId !== tid) {
      current = {
        key: `${tid ?? 'solo'}-${groups.length}`,
        turnId: tid,
        events: [],
        said: [],
        redacted: false,
        startedAt: null,
        durationMs: null,
        costUsd: null,
        tokens: null,
        toolCalls: 0,
        errors: 0,
      }
      groups.push(current)
    }
    current.events.push(e)

    if (current.startedAt == null && typeof e?.ts === 'string') current.startedAt = e.ts
    if (Array.isArray(e?.said)) pushUnique(current.said, e.said)
    if (e?.redacted) current.redacted = true
    if (e?.is_error) current.errors += 1
    current.toolCalls += Array.isArray(e?.tool_uses) ? e.tool_uses.length : 0

    // The turn's duration/cost/usage ride its `turn_done` row. Take the LAST of each we see rather
    // than the first: a resumed or retried turn can carry more than one, and the closing figure is
    // the turn's actual total.
    const d = num(e?.duration_ms)
    if (d != null) current.durationMs = d
    const c = num(e?.total_cost_usd)
    if (c != null) current.costUsd = c
    const t = usageTokens(e?.usage)
    if (t != null) current.tokens = t

    if (!tid) current = null // a turn-less event never absorbs the events after it
  }
  return groups
}

/** Every token the turn touched — prompt, cache write, cache read, output. Summed rather than
 *  reported as one field because no single field is the whole story, and a partial sum labelled
 *  "tokens" would understate a cache-heavy turn by an order of magnitude. Unknown shape → null,
 *  which renders as nothing rather than as a confident zero. */
export function usageTokens(usage: unknown): number | null {
  if (!usage || typeof usage !== 'object') return null
  const u = usage as Record<string, unknown>
  let total = 0
  let sawOne = false
  for (const k of ['input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens']) {
    const v = num(u[k])
    if (v != null) {
      total += v
      sawOne = true
    }
  }
  return sawOne ? total : null
}

/** Milliseconds → something a human reads at a glance. `null` when there is no honest answer. */
export function formatDuration(ms: unknown): string | null {
  const v = num(ms)
  if (v == null || v < 0) return null
  if (v < 1000) return `${Math.round(v)}ms`
  if (v < 60_000) return `${(v / 1000).toFixed(1)}s`
  const totalSec = Math.round(v / 1000)
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  return h > 0 ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(s).padStart(2, '0')}s`
}

/** 284000 → "284k", 1836000 → "1.8M". Plain number under 1000, so a 6-token turn doesn't read as
 *  "0k"; an M suffix past a million, so a busy turn doesn't read as a four-digit "k" nobody parses. */
export function formatTokens(tokens: unknown): string | null {
  const v = num(tokens)
  if (v == null || v < 0) return null
  if (v < 1000) return `${Math.round(v)}`
  if (v < 1_000_000) return `${Math.round(v / 1000)}k`
  return `${(v / 1_000_000).toFixed(1)}M`
}

/** Every string in an event that a search should look at.
 *
 *  A REDACTED EVENT CONTRIBUTES NOTHING. There is no text on one to search — the backend never
 *  sent any — and this function must never become the place that reconstructs some. */
function haystack(e: TraceEvent | null | undefined): string[] {
  if (!e || typeof e !== 'object') return []
  const parts: string[] = []
  if (typeof e.kind === 'string') parts.push(e.kind)
  if (Array.isArray(e.tool_uses)) {
    for (const t of e.tool_uses) {
      if (typeof t?.name === 'string') parts.push(t.name)
      if (typeof t?.input_preview === 'string') parts.push(t.input_preview)
    }
  }
  if (!e.redacted) {
    if (Array.isArray(e.said)) {
      for (const s of e.said) {
        if (typeof s?.text === 'string') parts.push(s.text)
        if (typeof s?.speaker === 'string') parts.push(s.speaker)
      }
    }
    if (typeof e.reply_preview === 'string') parts.push(e.reply_preview)
  }
  if (typeof e.why === 'string') parts.push(e.why)
  return parts
}

/** Case-insensitive substring across everything an event says. An empty or whitespace-only query
 *  matches everything, so clearing the box restores the list rather than emptying it. */
export function matchesQuery(e: TraceEvent | null | undefined, query: string | null | undefined): boolean {
  const q = (query ?? '').trim().toLowerCase()
  if (!q) return true
  return haystack(e).some((p) => p.toLowerCase().includes(q))
}

/** Flattens a tool's input preview to one line for inline display. Returns `null` for nothing worth
 *  showing, so the caller renders no chip rather than an empty one.
 *
 *  NO LENGTH CAP. This used to cut at 90 characters and ellipsis on top of that, which silently
 *  undid the panel's own §11 decision — "tool input renders inline, not hover-only" — by hiding most
 *  of it behind a hover anyway. `server/trace.py`'s `_preview(..., 200)` is the only cap left, and
 *  raising THAT is phase 2's job, not this reader's. */
export function toolPreview(input: unknown): string | null {
  if (typeof input !== 'string') return null
  const flat = input.replace(/\s+/g, ' ').trim()
  return flat || null
}

/** How the sessions list may be ordered. `recent` matches what the backend already sends —
 *  `last_at` descending — so picking it is a no-op rather than a second sort pass. */
export type SessionSort = 'recent' | 'cost' | 'turns' | 'errors'

const SESSION_SORTS: Record<SessionSort, (a: TraceSession, b: TraceSession) => number> = {
  recent: (a, b) => (b.last_at ?? '').localeCompare(a.last_at ?? ''),
  cost: (a, b) => (b.cost_usd ?? 0) - (a.cost_usd ?? 0),
  turns: (a, b) => (b.turns ?? 0) - (a.turns ?? 0),
  errors: (a, b) => (b.errors ?? 0) - (a.errors ?? 0),
}

/** A COPY, sorted — the backend's own order (newest first) is never mutated, so switching sort and
 *  switching back costs nothing and there is no aliasing hazard with the response cache. */
export function sortSessions(sessions: readonly TraceSession[] | null | undefined, sort: SessionSort): TraceSession[] {
  const list = [...(sessions ?? [])]
  list.sort(SESSION_SORTS[sort] ?? SESSION_SORTS.recent)
  return list
}
