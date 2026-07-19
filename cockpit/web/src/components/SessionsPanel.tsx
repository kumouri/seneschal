import { getSessions } from '../api'
import { relativeAge } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

export function SessionsPanel() {
  const result = usePolling(getSessions, 5000)

  if (!result) return <Panel title="Sessions">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Sessions">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { sessions, count } = result.data
  const liveCount = sessions.filter((s) => s.live).length

  return (
    <Panel
      title="Sessions"
      right={
        <span className="pill pill-info">
          {liveCount}/{count} live
        </span>
      }
    >
      {sessions.length === 0 ? (
        <p className="empty-state">No session-registry entries.</p>
      ) : (
        sessions.map((s) => (
          <div className="row" key={s.file}>
            <div>
              <div className="row-main">
                {s.source}
                {s.session_id && s.session_id !== s.source ? ` · ${s.session_id}` : ''}
              </div>
              <div className="row-sub">{s.working_on ?? 'idle'}</div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <span className={`pill ${s.live ? 'pill-live' : 'pill-idle'}`}>
                {s.live ? 'live' : 'idle'}
              </span>
              <div className="row-sub">{relativeAge(s.age_seconds)}</div>
            </div>
          </div>
        ))
      )}
    </Panel>
  )
}
