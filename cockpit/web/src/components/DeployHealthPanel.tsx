import { getSeneschaldHealth } from '../api'
import { relativeAge } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

// seneschald-health.json: Path A's "watch the watcher" file. last_ok is the field that matters most —
// if it stops advancing, seneschald-update itself is dead and no Telegram alert will ever come (see
// state/README.md + repo CLAUDE.md's "Repo state" section), so it's surfaced first and loudest here.
export function DeployHealthPanel() {
  const result = usePolling(getSeneschaldHealth, 5000)

  if (!result) return <Panel title="Deploy health">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Deploy health">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const h = result.data
  if (!h.available) {
    return (
      <Panel title="Deploy health">
        <p className="empty-state">No seneschald-health.json yet (seneschald-update hasn't run).</p>
      </Panel>
    )
  }

  const isOk = h.status === 'ok'
  const staleLastOk = h.last_ok_age_seconds != null && h.last_ok_age_seconds > 30 * 60

  return (
    <Panel
      title="Deploy health"
      right={
        <span className={`pill ${isOk ? 'pill-live' : 'pill-danger'}`}>{h.status ?? 'unknown'}</span>
      }
    >
      <div className="stat-row">
        <div className="stat">
          <span className="value" style={staleLastOk ? { color: 'var(--warn)' } : undefined}>
            {relativeAge(h.last_ok_age_seconds)}
          </span>
          <span className="label">last_ok</span>
        </div>
        {!isOk && (
          <div className="stat">
            <span className="value" style={{ color: 'var(--danger)' }}>
              {h.consecutive_blocked ?? '—'}
            </span>
            <span className="label">blocked cycles</span>
          </div>
        )}
      </div>
      {!isOk && h.detail ? <p className="row-sub">{h.detail}</p> : null}
      {h.branch ? (
        <p className="row-sub mono">
          {h.branch} @ {h.head?.slice(0, 10) ?? '—'}
        </p>
      ) : null}
      {staleLastOk && (
        <p className="error-state">last_ok hasn't advanced in a while — seneschald-update may be dead.</p>
      )}
    </Panel>
  )
}
