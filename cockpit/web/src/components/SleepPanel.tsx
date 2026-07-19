import { getHealthSleep } from '../api'
import { formatLocalDate, formatMinutes } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

const RECENT_NIGHTS = 14

// v4 (cockpit-spec.md "Health pipeline extension (v4)"): a simple bar-per-night sparkline of recent
// sleep duration + the last-night stat row. Source: state/health.db's sleep_session table (see
// cockpit/server/health.py::read_sleep — sessions are already summed per night).
export function SleepPanel() {
  const result = usePolling(() => getHealthSleep(RECENT_NIGHTS), 5000)

  if (!result) return <Panel title="Sleep">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Sleep">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { available, nights } = result.data
  const lastNight = nights[0]
  if (!available || !lastNight) {
    return (
      <Panel title="Sleep">
        <p className="empty-state">No sleep sessions in health.db yet.</p>
      </Panel>
    )
  }

  const maxMinutes = Math.max(1, ...nights.map((n) => n.total_duration_min ?? 0))
  // oldest -> newest, left to right, matching a normal calendar reading order
  const chronological = [...nights].reverse()

  return (
    <Panel title="Sleep">
      <div className="stat-row">
        <div className="stat">
          <span className="value">{formatMinutes(lastNight.total_duration_min)}</span>
          <span className="label">last night</span>
        </div>
        {lastNight.efficiency != null && (
          <div className="stat">
            <span className="value">{Math.round(lastNight.efficiency)}%</span>
            <span className="label">efficiency</span>
          </div>
        )}
        {lastNight.sleep_score != null && (
          <div className="stat">
            <span className="value">{Math.round(lastNight.sleep_score)}</span>
            <span className="label">sleep score</span>
          </div>
        )}
      </div>

      {chronological.map((n) => (
        <div className="health-bar-row" key={n.night}>
          <span className="health-bar-label">{formatLocalDate(n.night)}</span>
          <div className="health-bar-track">
            <div
              className="health-bar-fill"
              style={{ width: `${((n.total_duration_min ?? 0) / maxMinutes) * 100}%` }}
            />
          </div>
          <span className="row-sub">{formatMinutes(n.total_duration_min)}</span>
        </div>
      ))}
    </Panel>
  )
}
