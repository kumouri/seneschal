import { getGovernorConfig, getStatus } from '../api'
import { formatMinutes, relativeAge, secondsUntil } from '../format'
import type { StatusResponseLive } from '../types'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

/** Mirrors presence.py's RESPAWN_* constants / _RESPAWN_REASON_LABELS. Deliberately a lookup with a
 * passthrough default: a new reason added daemon-side shows its raw slug rather than vanishing. */
const RESPAWN_LABELS: Record<string, string> = {
  idle_winddown: 'idle wind-down',
  turn_error: 'turn error',
  undelivered_reply: 'undelivered reply',
  daemon_shutdown: 'daemon restart',
}

function formatTokens(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`
}

/**
 * Context-fill gauge (cockpit-spec.md's "Context gauge", listed since v1 and unbuilt until now).
 *
 * Two honesty rules are baked in:
 *  - It is always labelled `est.`. The daemon derives it from the CLI's turn-AGGREGATED usage divided
 *    by that turn's iteration count — exact for single-iteration turns, a lower bound otherwise — and
 *    a gauge that hides its own error bars is worse than no gauge.
 *  - The marker is Oikonomos's `context_fill_winddown_pct` — the one advisory knob that would govern a
 *    long-lived session, and until this gauge the one knob nothing measured. This is that
 *    measurement; the knob itself stays advisory — nothing in code blocks a turn on it.
 */
function ContextGauge({ s, thresholdPct }: { s: StatusResponseLive; thresholdPct: number | null }) {
  const tokens = s.context_tokens
  if (tokens == null) return null
  const pct = s.context_pct ?? null
  const width = pct == null ? 0 : Math.min(100, Math.max(0, pct))
  const hot = thresholdPct != null && pct != null && pct >= thresholdPct
  const windowTokens = s.context_window_tokens
  return (
    <div className="ctx-gauge">
      <div className="ctx-gauge-track">
        <div className={`ctx-gauge-fill${hot ? ' hot' : ''}`} style={{ width: `${width}%` }} />
        {thresholdPct != null && (
          <div
            className="ctx-gauge-marker"
            style={{ left: `${Math.min(100, Math.max(0, thresholdPct))}%` }}
            title={`Oikonomos context-fill wind-down threshold: ${thresholdPct}%`}
          />
        )}
      </div>
      <span className="row-sub">
        context ~{formatTokens(tokens)}
        {windowTokens ? ` / ${formatTokens(windowTokens)}` : ''}
        {pct != null ? ` · ${pct}%` : ''} est.
      </span>
    </div>
  )
}

export function StatusPanel() {
  const result = usePolling(getStatus, 5000)
  // The same config the Thresholds panel edits — read here only for the gauge's marker. Polled slowly
  // and entirely optional: an absent/failed read just means an unmarked gauge, never a broken panel.
  const governor = usePolling(getGovernorConfig, 30000)
  const thresholdRaw = governor?.ok ? governor.data.config?.context_fill_winddown_pct : null
  const thresholdPct = typeof thresholdRaw === 'number' ? thresholdRaw : null

  if (!result) return <Panel title="Warm-session status">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Warm-session status">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const s = result.data
  const pipePill = <span className={`pill ${s.pipe === 'up' ? 'pill-live' : 'pill-idle'}`}>pipe {s.pipe}</span>

  // Live shape (cockpit-spec.md v2): once the daemon's cockpit pipe has ever sent a status frame this
  // backend process's lifetime, /api/status serves this instead of the registry placeholder below —
  // permanently, even across a pipe reconnect. Prefer it: it's real turn-in-flight/model/queue data,
  // not a guess from the session registry.
  if ('session_up' in s) {
    // Lifetime block (session age / turns served / cost). Every field is optional because a
    // daemon on older code sends the v2 shape — render what's there, omit what isn't.
    const vitals: string[] = []
    if (s.session_age_sec != null) vitals.push(`age ${formatMinutes(s.session_age_sec / 60)}`)
    if (s.turns_served != null) vitals.push(`${s.turns_served} turn${s.turns_served === 1 ? '' : 's'}`)
    if (s.session_cost_usd != null) vitals.push(`$${s.session_cost_usd.toFixed(2)}`)
    const respawn = s.last_respawn_reason
      ? (RESPAWN_LABELS[s.last_respawn_reason] ?? s.last_respawn_reason)
      : null

    return (
      <Panel title="Warm-session status" right={pipePill}>
        <div className="row">
          <span className="row-main">
            {s.session_up ? (s.turn_in_flight ? 'Assistant is working…' : 'idle, session warm') : 'no warm session'}
          </span>
          <span className="row-sub">{s.model ?? '—'}</span>
        </div>
        {s.session_up && vitals.length > 0 && <p className="row-sub">{vitals.join(' · ')}</p>}
        {s.session_up && <ContextGauge s={s} thresholdPct={thresholdPct} />}
        {s.spawn_fallback_used ? (
          <p className="row-sub">⚠️ spawn-fallback used — the warm_model dial wouldn&apos;t spawn</p>
        ) : null}
        {/* "Silence is ambiguous": a clean wind-down and a session erroring out on every
            turn look identical from out here without this line. */}
        {!s.session_up && respawn ? <p className="row-sub">last session ended: {respawn}</p> : null}
        <p className="row-sub">
          queue depth {s.queue_depth ?? 0}
          {/* Background jobs outlive the warm session by design, so this is the one line here that
              stays meaningful when there's no session at all — "no warm session" does not mean
              "nothing is running". Detail lives in the Jobs panel. Optional field: an older daemon
              doesn't send it, and `0 running` would be a lie in that case, so it's simply omitted. */}
          {s.jobs_active != null ? ` · ${s.jobs_active} background job${s.jobs_active === 1 ? '' : 's'}` : ''}
        </p>
        {/* Un-landed Notion writes (the write-behind outbox). Shown ONLY when there are some: a backlog
            can grow for most of a day with no surface reporting it, and a permanently-visible
            "0 pending" is a gauge you learn to stop reading. Notion backend only — filesystem backends
            write direct and never send these. Optional fields — an older daemon doesn't send them. */}
        {(s.outbox_pending || s.outbox_dead) ? (
          <p className="row-sub">
            ⚠️ outbox: {s.outbox_pending ?? 0} un-landed Notion write
            {s.outbox_pending === 1 ? '' : 's'}
            {s.outbox_oldest_sec != null ? ` (oldest ${(s.outbox_oldest_sec / 3600).toFixed(1)} h)` : ''}
            {s.outbox_dead ? ` · ${s.outbox_dead} dead-letter${s.outbox_dead === 1 ? '' : 's'}` : ''}
          </p>
        ) : null}
      </Panel>
    )
  }

  // Registry fallback: the daemon's pipe hasn't sent a status frame yet this backend's lifetime (a
  // fresh backend start racing the daemon's own boot, or the pipe genuinely never having connected).
  // The backend asks for one on every pipe connect, so with the pipe UP this is a brief transient —
  // and it is NOT evidence that the warm session is down. Say which of the two it actually is rather
  // than reporting an absent registry entry as if the daemon were missing.
  if (!s.available) {
    return (
      <Panel title="Warm-session status" right={pipePill}>
        <p className="empty-state">
          {s.pipe === 'up'
            ? 'Connected to the daemon — waiting for its first status frame.'
            : 'No status from the daemon: its pipe is down and no session-registry entry was found.'}
        </p>
      </Panel>
    )
  }

  const ageSec = s.last_seen ? -(secondsUntil(s.last_seen) ?? 0) : null

  return (
    <Panel
      title="Warm-session status"
      right={
        <>
          <span className={`pill ${s.live ? 'pill-live' : 'pill-idle'}`}>{s.live ? 'live' : 'idle'}</span>
          {pipePill}
        </>
      }
    >
      <div className="row">
        <span className="row-main">{s.working_on ?? 'idle'}</span>
        <span className="row-sub">{s.phase ?? '—'}</span>
      </div>
      <p className="row-sub">last seen {relativeAge(ageSec)}</p>
      <p className="empty-state">{s.note}</p>
    </Panel>
  )
}
