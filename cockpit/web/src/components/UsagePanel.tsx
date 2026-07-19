import { getUsage } from '../api'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

// Best-effort — there's no official Claude plan-quota API, so this is turns/tokens as derivable from
// metrics.jsonl, always labeled `estimated`. See cockpit/server/readers.py::read_usage.
export function UsagePanel() {
  const result = usePolling(getUsage, 5000)

  if (!result) return <Panel title="Plan usage (estimated)">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Plan usage (estimated)">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { days, totals, tokens_available } = result.data
  const recentDays = days.slice(-7)

  return (
    <Panel title="Plan usage (estimated)" right={<span className="pill pill-warn">estimated</span>}>
      <div className="stat-row">
        <div className="stat">
          <span className="value">{totals.turns}</span>
          <span className="label">turns</span>
        </div>
        {tokens_available && (
          <div className="stat">
            <span className="value">{totals.tokens.toLocaleString()}</span>
            <span className="label">tokens</span>
          </div>
        )}
      </div>
      {!tokens_available && (
        <p className="empty-state">No token data in metrics.jsonl yet — turns only, for now.</p>
      )}
      {recentDays.length === 0 ? (
        <p className="empty-state">No metrics recorded yet.</p>
      ) : (
        recentDays.map((d) => (
          <div className="row" key={d.date}>
            <span className="row-main">{d.date}</span>
            <span className="row-sub">
              {d.turns} turn{d.turns === 1 ? '' : 's'} ·{' '}
              {Object.entries(d.by_model)
                .map(([m, n]) => `${m}:${n}`)
                .join(' ')}
            </span>
          </div>
        ))
      )}
    </Panel>
  )
}
