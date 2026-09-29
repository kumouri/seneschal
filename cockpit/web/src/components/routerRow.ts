// How one "Recent decisions" row is worded in the Router panel (RouterPanel.tsx; cockpit-spec.md
// § "Model dials & Fable delegation"). Pure and DOM-free on purpose, so the rule below is pinned by
// `routerRow.test.ts` rather than by a comment nobody re-reads.
//
// THE RULE — the arm must never be readable as the verdict.
//
// A `router-log.jsonl` row carries two different things: `arm` is WHICH CLASSIFIER RAN, `verdict` is
// WHAT IT DECIDED. They share a vocabulary — the fable arm's two verdicts are "standard" and "fable" —
// so a fable-arm row rendered as `telegram · fable · ` put the word *fable* exactly where a reader
// parses "what happened", next to a STANDARD badge. The panel read as reporting heavy Fable usage
// while the summary said a handful in over a thousand. Both numbers were right; the row was lying.
// Every one of those rows is the fable arm DECLINING to delegate.
//
// So the two facts are split across the row's two columns and each is made self-describing:
//
//   context (left, dim)   `telegram · fable arm`     — where it came in, and who classified it
//   decision (right, pill) `→ standard`              — what that classifier decided
//
// The arm always carries the literal word "arm", matching the section headers above it ("Fable arm
// (standard vs fable-level)"), and the verdict always carries the arrow. Neither can be mistaken for
// the other at a glance, which is the whole point.
//
// TOLERANT READER (cockpit/CLAUDE.md). This renders a log written by a daemon on a different commit.
// Every field is interrogated rather than trusted, and a segment with nothing to say is DROPPED
// rather than emitted empty — the dangling `·` on category-less fable rows was itself misread as a
// truncated value. Nothing here can throw, and nothing here can produce an empty row.

/** The four fields a row's wording reads — `unknown` deliberately, because the point is to
 *  interrogate them rather than to trust the declared shape. `RouterLogEntry` satisfies this. */
export interface RouterRowFields {
  channel?: unknown
  arm?: unknown
  verdict?: unknown
  category?: unknown
}

/** A field is usable only if it is a non-empty string once trimmed. `null`, `undefined`, `''`, a
 *  number, an object — all mean "nothing to show here", never a rendered placeholder. */
function text(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

/** Joins the parts that actually have something to say. This is where the dangling separator dies:
 *  an absent segment contributes nothing at all rather than an empty one. */
function joinSegments(parts: (string | null)[]): string {
  return parts.filter((p): p is string => p !== null).join(' · ')
}

/** `"fable"` → `"fable arm"`. Always names an arm, so the value can never be read as a verdict —
 *  including an arm this build has never heard of, which passes through labelled rather than being
 *  dropped or normalised away. An unusable `arm` reads `"unknown arm"`: the classifier ran (the row
 *  exists), we just can't say which one. */
export function armLabel(arm: unknown): string {
  const name = text(arm)
  return name === null ? 'unknown arm' : `${name} arm`
}

/** The left column: where the message came in, and who classified it. Never empty — `armLabel`
 *  always yields something, so a row missing every other field still renders as `unknown arm`. */
export function contextLabel(entry: RouterRowFields | null | undefined): string {
  const row = entry ?? {}
  return joinSegments([text(row.channel), armLabel(row.arm)])
}

/** The right column: what that classifier decided. The arrow is load-bearing — it is what makes the
 *  badge read as an outcome rather than as another label. `category` (the triage arm's sub-facet of
 *  an escalate/trivial verdict; the fable arm doesn't log one) rides WITH the verdict, because it is
 *  part of what was decided, not part of who decided it. */
export function decisionLabel(entry: RouterRowFields | null | undefined): string {
  const row = entry ?? {}
  const verdict = text(row.verdict) ?? 'unknown'
  const category = text(row.category)
  return category === null ? `→ ${verdict}` : `→ ${verdict} (${category})`
}

/** The whole sentence, for the badge's hover/assistive text — the pair spelled out in one place so
 *  it survives being read a column at a time. */
export function decisionTitle(entry: RouterRowFields | null | undefined): string {
  const row = entry ?? {}
  return `${armLabel(row.arm)} ${decisionLabel(row)}`
}
