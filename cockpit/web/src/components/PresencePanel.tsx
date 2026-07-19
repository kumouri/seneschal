import { getPresence } from '../api'
import { formatTimestamp } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

export function PresencePanel() {
  const result = usePolling(getPresence, 5000)

  if (!result) return <Panel title="Presence">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Presence">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const p = result.data
  if (!p.available) {
    return (
      <Panel title="Presence">
        <p className="empty-state">No presence-context.json yet (Call Shield hasn't reported in).</p>
      </Panel>
    )
  }

  return (
    <Panel title="Presence">
      <div className="stat-row">
        <div className="stat">
          <span className="value">{p.at_place ?? 'unknown'}</span>
          <span className="label">at place</span>
        </div>
        <div className="stat">
          <span className="value">{p.activity ?? 'unknown'}</span>
          <span className="label">activity</span>
        </div>
      </div>
      <p className="row-sub">
        asleep: {p.asleep == null ? 'unknown' : p.asleep ? 'yes' : 'no'}{' '}
        <span className="empty-state">(informational only — see CLAUDE.md)</span>
      </p>
      <p className="row-sub">since {formatTimestamp(p.since)}</p>
    </Panel>
  )
}
