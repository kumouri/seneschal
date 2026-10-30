// What each job runs on, for the Jobs panel (JobsPanel.tsx; seneschal/docs/cockpit-spec.md § Jobs panel;
// background-jobs-spec §3.16): which subscription / harness / model each job is on, and which ones are
// non-LLM. Pure and DOM-free so `jobRuntime.test.ts` pins it.
//
// THE BROWSER DECIDES NOTHING ABOUT A JOB'S CLASS. `jobs.py` stamps `runtime` at start (its `llm` is
// `job_agent_class` verbatim) and the backend passes it through; this module only reads that stamp.
// A record without one predates it and reads as `unrecorded` ("—") — never re-guessed from the
// command line here, which would be a third copy of the classifier (cockpit-spec ruling 3).
//
// Four kinds, and `unknown` is deliberately its own: the classifier could not place the job (a wrapper
// script, an inline program), so the caps COUNT it — it must look different from both a confirmed LLM
// job and a confirmed non-LLM one, or the panel would be claiming knowledge the record doesn't have.
//
// The filter may never hide an undelivered completion push — the same rule `jobsCollapse.ts` pins for
// the collapse toggle, for the same reason: that is the one state this panel exists to surface.

import type { JobRuntime } from '../types'
import { isCollapsible, type CollapsibleJobFields } from './jobsCollapse.ts'

export type RuntimeKind = 'llm' | 'non-llm' | 'unknown' | 'unrecorded'
export type RuntimeFilter = 'all' | RuntimeKind

/** Filter order and labels — `all` first, then the three stamped kinds, then the pre-stamp records. */
export const RUNTIME_FILTERS: readonly { key: RuntimeFilter; label: string }[] = [
  { key: 'all', label: 'all' },
  { key: 'llm', label: 'LLM' },
  { key: 'non-llm', label: 'non-LLM' },
  { key: 'unknown', label: 'unknown' },
  { key: 'unrecorded', label: 'not recorded' },
]

/** The kind a stamp says. Typed loosely on input: a newer or older daemon may write anything. A block
 *  whose `llm` is not one of the three legal values is `unknown` — it exists, so it is not
 *  `unrecorded`, but it proves neither LLM nor non-LLM. */
export function runtimeKind(rt: JobRuntime | null | undefined | unknown): RuntimeKind {
  if (!rt || typeof rt !== 'object') return 'unrecorded'
  const llm = (rt as { llm?: unknown }).llm
  if (llm === true) return 'llm'
  if (llm === false) return 'non-llm'
  return 'unknown'
}

function field(rt: object, key: string): string | null {
  const v = (rt as Record<string, unknown>)[key]
  return typeof v === 'string' && v.trim() !== '' ? v : null
}

/** The `subscription · harness · model` phrase for a row, or `—` for a record without the stamp.
 *  Empty for a non-LLM job (the badge says it all) and for an unknown job whose harness is unknown
 *  too (there is nothing true to print beside the badge). */
export function runtimeText(rt: JobRuntime | null | undefined | unknown): string {
  const kind = runtimeKind(rt)
  if (kind === 'unrecorded') return '—'
  if (kind === 'non-llm') return ''
  const r = rt as object
  const harness = field(r, 'harness')
  if (!harness || harness === 'unknown') return kind === 'llm' ? 'harness unknown' : ''
  return [field(r, 'subscription') ?? '?', harness, field(r, 'model') ?? '?'].join(' · ')
}

interface FilterableJob extends CollapsibleJobFields {
  runtime?: unknown
}

/** A finished job whose push has not provably landed. Errs toward TRUE for a shape it cannot read
 *  (anything not positively running and not positively delivered), like `isCollapsible`. */
function mustStayVisible(job: FilterableJob): boolean {
  return job.is_running !== true && !isCollapsible(job)
}

/** The jobs a filter shows, order preserved. `all` is the identity. An undelivered push is always kept,
 *  whatever its kind. */
export function filterByRuntime<T extends FilterableJob>(
  jobs: readonly T[] | null | undefined,
  filter: RuntimeFilter,
): T[] {
  const list = [...(jobs ?? [])]
  if (filter === 'all') return list
  return list.filter((job) => runtimeKind(job.runtime) === filter || mustStayVisible(job))
}

/** How many jobs of each kind — the counts the filter buttons show. */
export function countByRuntime(
  jobs: readonly FilterableJob[] | null | undefined,
): Record<RuntimeFilter, number> {
  const counts: Record<RuntimeFilter, number> = {
    all: 0, llm: 0, 'non-llm': 0, unknown: 0, unrecorded: 0,
  }
  for (const job of jobs ?? []) {
    counts.all += 1
    counts[runtimeKind(job.runtime)] += 1
  }
  return counts
}
