import { getStatus } from '../api'
import { relativeAge, secondsUntil } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

export function StatusPanel() {
  const result = usePolling(getStatus, 5000)

  if (!result) return <Panel title="Warm-session status">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Warm-session status">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const s = result.data
  if (!s.available) {
    return (
      <Panel title="Warm-session status">
        <p className="empty-state">No daemon session found. {s.note}</p>
      </Panel>
    )
  }

  const ageSec = s.last_seen ? -(secondsUntil(s.last_seen) ?? 0) : null

  return (
    <Panel
      title="Warm-session status"
      right={<span className={`pill ${s.live ? 'pill-live' : 'pill-idle'}`}>{s.live ? 'live' : 'idle'}</span>}
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
