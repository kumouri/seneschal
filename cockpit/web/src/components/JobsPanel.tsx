import { useState } from 'react'
import { getJobs } from '../api'
import { formatDuration, formatTimestamp } from '../format'
import type { Job } from '../types'
import { usePolling } from '../usePolling'
import { browserStorage, partitionRecent, readCollapsed, writeCollapsed } from './jobsCollapse'
import { AuthGate, Panel } from './Panel'

// Durable background jobs (seneschal/docs/background-jobs-spec.md) — the mechanism behind "I'll tell you
// when it's done". Strictly read-only, like every other v1 monitor panel: no start, no cancel. A
// running job is the daemon's to finish and `jobs.py cancel`'s to stop; a dashboard button would make
// killing work a one-click accident.
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
        <span className="job-title">{job.title}</span>
        <span className="row-sub">
          {duration(job)}
          {job.exit_code != null && job.exit_code !== 0 ? ` · exit ${job.exit_code}` : ''}
          {job.lease ? ' · lease' : ''}
          {job.wake ? ' · wake' : ''}
        </span>
      </button>

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

export function JobsPanel() {
  const result = usePolling(getJobs, 5000)
  // Read once on mount, not on every render: a lazy initializer keeps the storage hit off the 5 s
  // polling path, and the stored value is only ever changed from this component anyway.
  const [collapsed, setCollapsed] = useState(() => readCollapsed(browserStorage()))

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
  const shownRecent = collapsed ? pinned : recent

  return (
    <Panel title="Jobs" right={right}>
      {awaiting_push > 0 && (
        <div className="job-alarm">
          {awaiting_push} finished {awaiting_push === 1 ? 'job has' : 'jobs have'} an undelivered
          completion push — the daemon is retrying.
        </div>
      )}

      {active.length === 0 && recent.length === 0 && (
        <p className="empty-state">Nothing running, nothing finished recently.</p>
      )}

      {active.map((job) => (
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
