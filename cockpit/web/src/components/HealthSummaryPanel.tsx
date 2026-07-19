import { getHealthSummary } from '../api'
import { formatMinutes } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

// v4 (cockpit-spec.md "Health pipeline extension (v4)"): one compact card — last night's sleep,
// this week's workouts, today's kcal/protein so far. The detail views (recent nights, the workout
// list, the nutrition history) live in their own panels below this one.
export function HealthSummaryPanel() {
  const result = usePolling(getHealthSummary, 5000)

  if (!result) return <Panel title="Health">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Health">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { available, sleep, workouts, nutrition } = result.data
  if (!available) {
    return (
      <Panel title="Health">
        <p className="empty-state">
          No health.db yet — Call Shield hasn't synced (see seneschal/scripts/HEALTH_SETUP.md).
        </p>
      </Panel>
    )
  }

  return (
    <Panel title="Health">
      <div className="health-tile-row">
        <div className="health-tile">
          <span className="value">
            {sleep.last_night ? formatMinutes(sleep.last_night.total_duration_min) : '—'}
          </span>
          <span className="label">last night</span>
        </div>
        <div className="health-tile">
          <span className="value">{workouts.this_week?.count ?? 0}</span>
          <span className="label">workouts this week</span>
        </div>
        <div className="health-tile">
          <span className="value">{Math.round(nutrition.today?.total_kcal ?? 0)}</span>
          <span className="label">kcal today</span>
        </div>
        <div className="health-tile">
          <span className="value">{Math.round(nutrition.today?.total_protein_g ?? 0)}g</span>
          <span className="label">protein today</span>
        </div>
      </div>
      {!sleep.available && !workouts.available && !nutrition.available && (
        <p className="empty-state">No sleep, workout, or nutrition rows yet.</p>
      )}
    </Panel>
  )
}
