import { getOneiroi } from '../api'
import { formatTimestamp } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

// "Oneiroi" — the per-session distillates mini_dream.py writes ("mini-dreams" in older docs).
// Canonical pronunciation is the ancient Greek: "oh-NAY-roy" (singular oneiros). See README.md.
export function OneiroiPanel() {
  const result = usePolling(() => getOneiroi(10), 5000)

  if (!result) return <Panel title="Oneiroi feed">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Oneiroi feed">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { oneiroi } = result.data

  return (
    <Panel title="Oneiroi feed" right={<span className="pill pill-info">{oneiroi.length}</span>}>
      {oneiroi.length === 0 ? (
        <p className="empty-state">No distillates yet.</p>
      ) : (
        oneiroi.map((rec, i) => (
          <div className="row" key={rec.id ? String(rec.id) : i}>
            <div>
              <div className="row-main">{rec.title || rec.session_id || 'untitled session'}</div>
              <div className="row-sub">
                {rec.distillate ? truncate(String(rec.distillate), 140) : 'no distillate text'}
              </div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div className="row-sub">{formatTimestamp(rec.ended_at)}</div>
              {rec.branch ? <div className="row-sub mono">{rec.branch}</div> : null}
            </div>
          </div>
        ))
      )}
    </Panel>
  )
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}
