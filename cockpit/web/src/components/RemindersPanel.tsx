import { getReminders } from '../api'
import { formatTimestamp, relativeFuture, secondsUntil } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

export function RemindersPanel() {
  const result = usePolling(getReminders, 5000)

  if (!result) return <Panel title="Reminders">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Reminders">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { pending_count, next } = result.data

  return (
    <Panel title="Reminders" right={<span className="pill pill-info">{pending_count} pending</span>}>
      {next.length === 0 ? (
        <p className="empty-state">Nothing pending.</p>
      ) : (
        next.map((r, i) => (
          <div className="row" key={r.id ?? i}>
            <div>
              <div className="row-main">{r.text ?? '(no text)'}</div>
              <div className="row-sub">{r.channel}</div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div className="row-sub">{relativeFuture(secondsUntil(r.due_at))}</div>
              <div className="row-sub">{formatTimestamp(r.due_at)}</div>
            </div>
          </div>
        ))
      )}
    </Panel>
  )
}
