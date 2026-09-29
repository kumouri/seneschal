// The collapse rule for the Jobs panel's "recently finished" group (JobsPanel.tsx;
// seneschal/docs/cockpit-spec.md § Jobs panel). Kept pure and DOM-free on purpose, so the two invariants
// below are pinned by `jobsCollapse.test.ts` rather than by a comment nobody re-reads.
//
// INVARIANT 1 — a running job is never collapsible.
// INVARIANT 2 — a finished job whose completion push hasn't landed is never collapsible.
//
// Invariant 2 is the load-bearing one. `awaiting_push` is the single state this entire panel exists
// to surface — jobs.py stamps `notified_at` only on a push that actually landed, so a terminal job
// without one means the daemon is trying to reach the owner and failing. A collapse that could swallow
// that would undo the feature, so the rule is written as a positive proof of "safe to hide" rather
// than as a search for "unsafe": anything we cannot POSITIVELY establish as finished-AND-delivered
// stays on screen.
//
// That inversion is the tolerant-reader rule (cockpit/CLAUDE.md) pointed at the UI instead of at the
// backend. The panel reads a ledger written by a daemon on a different commit; if a field is renamed,
// dropped, or arrives with an unexpected type, the failure lands on "showed too much", never on
// "quietly hid an undelivered ping".

/** The only two fields the collapse rule reads — typed as `unknown` deliberately, because the point
 *  is to interrogate them rather than to trust the declared shape. `Job` satisfies this. */
export interface CollapsibleJobFields {
  is_running?: unknown
  notified_at?: unknown
}

/** True only when the job is provably finished AND its completion push provably landed. Every other
 *  answer — including "I don't recognize this shape" — is `false`, i.e. keep it visible. */
export function isCollapsible(job: CollapsibleJobFields | null | undefined): boolean {
  if (!job || typeof job !== 'object') return false
  // `!== false` and not `!job.is_running`: a missing field, a string "false", or a renamed field all
  // have to read as "might still be running" (INVARIANT 1).
  if (job.is_running !== false) return false
  // A real timestamp is the only evidence the push landed. `null`, `''`, and a non-string are all
  // "not delivered" (INVARIANT 2).
  return typeof job.notified_at === 'string' && job.notified_at !== ''
}

/** Splits the `recent` list into what collapsing must always show and what it may hide. Relative
 *  order is preserved inside each bucket, so the backend's ended-at-descending sort survives. */
export function partitionRecent<T extends CollapsibleJobFields>(
  recent: readonly T[] | null | undefined,
): { pinned: T[]; collapsible: T[] } {
  const pinned: T[] = []
  const collapsible: T[] = []
  for (const job of recent ?? []) {
    if (isCollapsible(job)) collapsible.push(job)
    else pinned.push(job)
  }
  return { pinned, collapsible }
}

// ------------------------------------------------------------------------------------------------
// Persisting the choice
//
// The tolerant read/write plumbing lives in `collapseStorage.ts`, shared since the Trace panel
// became the second collapsible section; this half is now just Jobs' key and Jobs' default. The
// *rule* above — what may be hidden at all — stays here, because it is a safety invariant rather
// than a storage concern, and its tests below still exercise it through these same four names.

// The `.ts` extension is required, not stylistic: this module is loaded directly by `node --test`
// (via `jobsCollapse.test.ts`), and Node's ESM resolver does not guess extensions. Vite and
// `moduleResolution: "Bundler"` both accept it.
import {
  browserStorage,
  readCollapsedFor,
  writeCollapsedFor,
  type StorageLike,
} from './collapseStorage.ts'

export { browserStorage, type StorageLike }

/** **Flip this one line to change the default.** Collapsed on load: the group is history, and the
 *  panel's live half — running jobs and the `awaiting_push` alarm — is what earns the space. */
export const COLLAPSED_BY_DEFAULT = true

export const COLLAPSE_STORAGE_KEY = 'seneschal.jobs.recent-collapsed'

/** Tolerant read: only the two values we write mean anything. Absent, corrupt, or written by a
 *  future version all read as "no preference recorded" and fall back to the default. */
export function readCollapsed(storage: StorageLike | null | undefined): boolean {
  return readCollapsedFor(storage, COLLAPSE_STORAGE_KEY, COLLAPSED_BY_DEFAULT)
}

/** A preference we can't persist is a preference that doesn't survive reload — never a crash. */
export function writeCollapsed(storage: StorageLike | null | undefined, collapsed: boolean): void {
  writeCollapsedFor(storage, COLLAPSE_STORAGE_KEY, collapsed)
}
