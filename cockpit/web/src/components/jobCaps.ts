// The Jobs panel's caps control, its DOM-free half (the cap values are the owner's to set —
// seneschal/docs/background-jobs-spec.md §3.12). Pinned by jobCaps.test.ts.
//
// Two things a tidy-up could undo, which is why they live here rather than inline in the component:
//   * the bounds come FROM THE SERVER (`limits`), never a constant in this file — the backend's
//     `job_caps.validate` is the one judge, and the browser only pre-checks against what it was told
//     (cockpit ruling 3: no third copy). A pair the server would reject is refused here with the same
//     rules, never clamped into range;
//   * the in-flight line says "unknown" when the backend could not count, never 0 — an unknown count
//     shown as zero reads as headroom that may not exist.

export interface CapsLimits {
  soft_min: number
  hard_ceiling: number
}

export type CapsCheck = { ok: true; soft: number; hard: number } | { ok: false; error: string }

/** The pair, parsed and checked against the server's own bounds — or the first reason it fails.
 *  Rejects; never clamps. */
export function validateCaps(softText: string, hardText: string, limits: CapsLimits): CapsCheck {
  const parse = (text: string): number | null => {
    const t = text.trim()
    return /^\d+$/.test(t) ? Number(t) : null
  }
  const soft = parse(softText)
  const hard = parse(hardText)
  if (soft === null) return { ok: false, error: 'The soft cap must be a whole number.' }
  if (hard === null) return { ok: false, error: 'The hard cap must be a whole number.' }
  if (soft < limits.soft_min) return { ok: false, error: `The soft cap must be at least ${limits.soft_min}.` }
  if (hard < soft) return { ok: false, error: 'The hard cap must be at least the soft cap.' }
  if (hard > limits.hard_ceiling) {
    return { ok: false, error: `The hard cap must be at most ${limits.hard_ceiling}.` }
  }
  return { ok: true, soft, hard }
}

/** "2 of 5 agent jobs in flight" — or an honest unknown. `over` is true when the live count already
 *  exceeds the hard cap (it was lowered under running work), which the panel says plainly. */
export function inFlightLine(inFlight: number | null, hard: number): { text: string; over: boolean } {
  if (inFlight === null || inFlight === undefined) {
    return { text: `in flight: unknown (couldn't count) · hard cap ${hard}`, over: false }
  }
  return {
    text: `${inFlight} of ${hard} agent job${hard === 1 ? '' : 's'} in flight`,
    over: inFlight > hard,
  }
}

/** Shown under the inputs, always — lowering a cap is the one edit whose effect is easy to misread. */
export const LOWERING_HINT =
  'Lowering a cap below what is already running cancels nothing — running jobs finish normally; ' +
  'new agent starts are refused until the count drops.'
