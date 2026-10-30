import { useEffect, useState } from 'react'
import { getJobCaps, getJobs, postJobCaps } from '../api'
import { formatDuration, formatTimestamp } from '../format'
import type { Job, JobCapsResponse } from '../types'
import { usePolling } from '../usePolling'
import { LOWERING_HINT, inFlightLine, validateCaps } from './jobCaps'
import {
  RUNTIME_FILTERS,
  countByRuntime,
  filterByRuntime,
  runtimeKind,
  runtimeText,
  type RuntimeFilter,
} from './jobRuntime'
import { browserStorage, partitionRecent, readCollapsed, writeCollapsed } from './jobsCollapse'
import { AuthGate, Panel } from './Panel'

// Durable background jobs (seneschal/docs/background-jobs-spec.md) — the mechanism behind "I'll tell you
// when it's done". Read-only over the JOBS themselves, like every other v1 monitor panel: no start, no
// cancel. A running job is the daemon's to finish and `jobs.py cancel`'s to stop; a dashboard button
// would make killing work a one-click accident. The panel's ONE write is the caps control below
// (the values are the owner's to set) — it sets the numbers `jobs.py` refuses new
// starts at, and by construction it cannot touch a job that is already running.
//
// The state this panel exists to make visible is `awaiting_push` — a job that ENDED but whose
// completion push hasn't landed. jobs.py stamps `notified_at` only on a send that actually
// succeeded, so a non-zero count means the daemon is trying to reach the owner and failing, which is
// precisely the silence the whole feature was built to eliminate. It gets the alarm styling; nothing
// else here does.
//
// "Recently finished" collapses (persisted in localStorage), because on a busy day it is a wall of
// history sitting on top of the live half. What it may hide is decided in `jobsCollapse.ts` and
// pinned by `jobsCollapse.test.ts`: running jobs and undelivered completion pushes are never
// collapsible, so the alarm can never be swallowed by the tidying-up.
//
// Each row says what the job runs on — `subscription · harness · model` — with a non-LLM badge and a
// distinct "LLM?" badge for a job the classifier could not place (background-jobs-spec §3.16). All of
// it is read off the `runtime` stamp `jobs.py` wrote at start; a record without one shows "—", and
// the runtime filter above the list obeys the same never-hide-an-undelivered-push rule
// (`jobRuntime.ts`, pinned by `jobRuntime.test.ts`).

const STATUS_EMOJI: Record<string, string> = {
  running: '⏳',
  done: '✅',
  failed: '❌',
  'timed-out': '⏱️',
  'ended-unknown': '⚠️',
  cancelled: '🛑',
}

function statusClass(status: string): string {
  if (status === 'running') return 'job-status job-status-running'
  if (status === 'done') return 'job-status job-status-ok'
  if (status === 'cancelled') return 'job-status job-status-muted'
  return 'job-status job-status-bad'
}

/** How long it ran / has been running — NOT "…ago". A finished job's `duration_sec` is its total, so
 *  rendering it through `relativeAge` would say "2m ago" for something that merely took two minutes. */
function duration(job: Job): string {
  const d = formatDuration(job.duration_sec)
  if (!d) return ''
  return job.is_running ? `running ${d}` : `took ${d}`
}

function JobRow({ job }: { job: Job }) {
  const [open, setOpen] = useState(false)
  const tail = job.log_tail ?? []
  const hasDetail = tail.length > 0 || !!job.command || !!job.error
  // A terminal job with no notified_at is the alarm case (see the header comment). A RUNNING job
  // having none is simply normal, so it must not be flagged.
  const unpushed = job.is_terminal && !job.notified_at
  const kind = runtimeKind(job.runtime)
  const runsOn = runtimeText(job.runtime)

  return (
    <div className="job-row">
      <button
        type="button"
        className="job-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        disabled={!hasDetail}
      >
        <span className={statusClass(job.status)}>
          {STATUS_EMOJI[job.status] ?? '•'} {job.status}
        </span>
        {kind === 'non-llm' && (
          <span className="job-badge job-badge-nonllm" title="No model call — not counted toward the caps">
            non-LLM
          </span>
        )}
        {kind === 'unknown' && (
          <span
            className="job-badge job-badge-unknown"
            title="Couldn't tell from the command whether this calls a model — counted toward the caps"
          >
            LLM?
          </span>
        )}
        <span className="job-title">{job.title}</span>
        <span className="row-sub">
          {duration(job)}
          {job.exit_code != null && job.exit_code !== 0 ? ` · exit ${job.exit_code}` : ''}
          {job.lease ? ' · lease' : ''}
          {job.wake ? ' · wake' : ''}
          {/* The runtime phrase already names a non-default account as its subscription, so the older
              `account` bit only shows on a record without the stamp. */}
          {job.account && kind === 'unrecorded' ? ` · account ${job.account}` : ''}
        </span>
      </button>
      {runsOn && (
        <div className={kind === 'unrecorded' ? 'row-sub job-runtime job-runtime-none' : 'row-sub job-runtime'}>
          {runsOn}
        </div>
      )}

      {unpushed && (
        <div className="job-unpushed">
          ⚠️ finished, but the completion push hasn't landed yet — the daemon is still retrying it
        </div>
      )}

      {open && (
        <div className="job-detail">
          {job.command && <div className="job-command">{job.command}</div>}
          {job.error && <div className="job-error">{job.error}</div>}
          <div className="row-sub">
            {job.started_at && <>started {formatTimestamp(job.started_at)}</>}
            {job.ended_at && <> · ended {formatTimestamp(job.ended_at)}</>}
            {job.notified_at && <> · owner notified {formatTimestamp(job.notified_at)}</>}
          </div>
          {tail.length > 0 && (
            <pre className="job-log">{tail.join('\n')}</pre>
          )}
        </div>
      )}
    </div>
  )
}

type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'ok' } | { kind: 'error'; message: string }

/** The two concurrency caps — the owner's to set. Polls slower than the job list: the count behind it
 *  is a `jobs.py` subprocess on the backend (cached ~10 s), and the numbers only move when the owner
 *  moves them. */
function JobCapsControl() {
  const polled = usePolling(getJobCaps, 15000)
  const [saved, setSaved] = useState<JobCapsResponse | null>(null)
  const [soft, setSoft] = useState<string | null>(null)
  const [hard, setHard] = useState<string | null>(null)
  const [save, setSave] = useState<SaveState>({ kind: 'idle' })

  // A fresh poll supersedes the POST's own echo, so the in-flight count keeps moving after a save.
  useEffect(() => setSaved(null), [polled])

  if (!polled || !polled.ok) return null // the job list above already renders the auth/error state
  const caps = saved ?? polled.data
  if (!caps.available) {
    return <p className="row-sub">Job caps unavailable: {caps.reason ?? 'unknown reason'}</p>
  }
  const softText = soft ?? String(caps.soft_per_session)
  const hardText = hard ?? String(caps.hard_global)
  const line = inFlightLine(caps.in_flight, caps.hard_global)
  const dirty = softText !== String(caps.soft_per_session) || hardText !== String(caps.hard_global)

  async function onSave() {
    const check = validateCaps(softText, hardText, caps.limits)
    if (!check.ok) {
      setSave({ kind: 'error', message: check.error })
      return
    }
    setSave({ kind: 'saving' })
    const res = await postJobCaps(check.soft, check.hard)
    if (res.ok) {
      setSaved(res.data)
      setSoft(null)
      setHard(null)
      setSave({ kind: 'ok' })
    } else {
      setSave({ kind: 'error', message: res.error })
    }
  }

  return (
    <div className="job-caps">
      <div className="job-caps-head">
        <span className={line.over ? 'job-caps-over' : ''}>{line.text}</span>
        <span className="row-sub">
          {caps.source === 'file' && caps.updated_at
            ? ` · set ${formatTimestamp(caps.updated_at)}`
            : ' · defaults'}
          {caps.uncounted ? ` · ${caps.uncounted} non-agent uncounted` : ''}
        </span>
      </div>
      {caps.error && <p className="error-state">job-caps.json {caps.error} — the defaults are in force.</p>}
      <div className="job-caps-inputs">
        <label className="dial-label" htmlFor="job-caps-soft">
          Soft <span className="row-sub">per session</span>
        </label>
        <input
          id="job-caps-soft"
          className="dial-select job-caps-number"
          type="number"
          min={caps.limits.soft_min}
          max={caps.limits.hard_ceiling}
          value={softText}
          onChange={(e) => setSoft(e.target.value)}
        />
        <label className="dial-label" htmlFor="job-caps-hard">
          Hard <span className="row-sub">total</span>
        </label>
        <input
          id="job-caps-hard"
          className="dial-select job-caps-number"
          type="number"
          min={caps.limits.soft_min}
          max={caps.limits.hard_ceiling}
          value={hardText}
          onChange={(e) => setHard(e.target.value)}
        />
        <button
          type="button"
          className="restart-btn"
          disabled={!dirty || save.kind === 'saving'}
          onClick={() => void onSave()}
        >
          {save.kind === 'saving' ? 'Saving…' : 'Save'}
        </button>
      </div>
      {save.kind === 'error' && <p className="error-state">{save.message}</p>}
      {save.kind === 'ok' && !dirty && <p className="row-sub">Saved — applies to the next start.</p>}
      <p className="row-sub">
        Soft is your desktop headroom per session (a job can pass it with --over-soft-cap); hard has no
        override. {LOWERING_HINT}
      </p>
    </div>
  )
}

export function JobsPanel() {
  const result = usePolling(getJobs, 5000)
  // Read once on mount, not on every render: a lazy initializer keeps the storage hit off the 5 s
  // polling path, and the stored value is only ever changed from this component anyway.
  const [collapsed, setCollapsed] = useState(() => readCollapsed(browserStorage()))
  // Not persisted, deliberately: a filter left on from yesterday would quietly hide today's jobs.
  const [runtimeFilter, setRuntimeFilter] = useState<RuntimeFilter>('all')

  function toggleCollapsed() {
    const next = !collapsed
    setCollapsed(next)
    writeCollapsed(browserStorage(), next)
  }

  if (!result) return <Panel title="Jobs">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Jobs">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { available, active, recent, active_count, lease_held, awaiting_push } = result.data

  // "Can't see the jobs store" and "nothing has run lately" are genuinely different facts, and this
  // panel is about not letting silence be ambiguous — so they get different empty states.
  if (!available) {
    return (
      <Panel title="Jobs">
        <p className="empty-state">No jobs store yet — nothing has been run as a job on this machine.</p>
      </Panel>
    )
  }

  const right = (
    <span className="row-sub">
      {active_count > 0 ? `${active_count} running` : 'idle'}
      {lease_held ? ' · holding the session' : ''}
    </span>
  )

  // `pinned` is what collapsing may never hide; `collapsible` is the quiet history. The panel doesn't
  // decide that — jobsCollapse.ts does, and it errs toward showing (see its header). Note it splits
  // `recent`, never `active`: running jobs live above the divider and aren't part of this group at
  // all, but the rule re-checks `is_running` anyway rather than trusting the backend's partition.
  const { pinned, collapsible } = partitionRecent(recent)
  const recentBase = collapsed ? pinned : recent
  // The runtime filter narrows both groups; `filterByRuntime` keeps an undelivered push whatever its
  // kind, so neither tidying-up control can hide the alarm.
  const shownActive = filterByRuntime(active, runtimeFilter)
  const shownRecent = filterByRuntime(recentBase, runtimeFilter)
  const hiddenByFilter =
    active.length - shownActive.length + (recentBase.length - shownRecent.length)
  const runtimeCounts = countByRuntime([...active, ...recent])

  return (
    <Panel title="Jobs" right={right}>
      {awaiting_push > 0 && (
        <div className="job-alarm">
          {awaiting_push} finished {awaiting_push === 1 ? 'job has' : 'jobs have'} an undelivered
          completion push — the daemon is retrying.
        </div>
      )}

      <JobCapsControl />

      {active.length === 0 && recent.length === 0 && (
        <p className="empty-state">Nothing running, nothing finished recently.</p>
      )}

      {runtimeCounts.all > 0 && (
        <div className="job-filters" role="group" aria-label="Filter jobs by what they run on">
          {/* An empty kind gets no button, except the one currently selected — a filter must never be
              on while its own button has vanished. */}
          {RUNTIME_FILTERS.filter(
            (f) => f.key === 'all' || f.key === runtimeFilter || runtimeCounts[f.key] > 0,
          ).map((f) => (
            <button
              type="button"
              key={f.key}
              className={runtimeFilter === f.key ? 'job-filter is-active' : 'job-filter'}
              aria-pressed={runtimeFilter === f.key}
              onClick={() => setRuntimeFilter(f.key)}
            >
              {f.label} {runtimeCounts[f.key]}
            </button>
          ))}
          {hiddenByFilter > 0 && <span className="row-sub">{hiddenByFilter} hidden by the filter</span>}
        </div>
      )}

      {shownActive.map((job) => (
        <JobRow job={job} key={job.id} />
      ))}

      {/* The divider used to appear only when there was something above it to divide. It is a group
          HEADER now, because it carries the toggle — without it there'd be no way back from a
          collapsed group when nothing is running. The toggle itself is omitted when there is nothing
          collapsible, rather than offered and inert. */}
      {recent.length > 0 && (
        <div className="job-divider job-group-head">
          <span>recently finished</span>
          {collapsible.length > 0 && (
            <button
              type="button"
              className="job-collapse-toggle"
              onClick={toggleCollapsed}
              aria-expanded={!collapsed}
            >
              {collapsed ? `show ${collapsible.length} older` : `hide ${collapsible.length} older`}
            </button>
          )}
        </div>
      )}

      {shownRecent.map((job) => (
        <JobRow job={job} key={job.id} />
      ))}
    </Panel>
  )
}
