import { getHealthWorkouts } from '../api'
import { formatLocalDate, formatMinutes, thisIsoWeekKey } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

const RECENT_DAYS = 14
const RECENT_SESSIONS_SHOWN = 8

// v4 (cockpit-spec.md "Health pipeline extension (v4)"): this week's rollup (count/minutes/kcal) +
// a recent-sessions list. Source: state/health.db's workouts table (Health Connect
// ExerciseSessionRecord, Call Shield v4 live feed only — see cockpit/server/health.py::read_workouts).
export function WorkoutsPanel() {
  const result = usePolling(() => getHealthWorkouts(RECENT_DAYS), 5000)

  if (!result) return <Panel title="Workouts">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Workouts">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { available, sessions, weekly } = result.data
  if (!available) {
    return (
      <Panel title="Workouts">
        <p className="empty-state">No workouts in health.db yet.</p>
      </Panel>
    )
  }

  // "this week" is looked up by ISO week key rather than assumed to be weekly[0] — weekly is sorted
  // newest-first, but if nothing's logged yet THIS week, weekly[0] would silently be last week's
  // totals mislabeled as "this week".
  const thisWeek = weekly.find((w) => w.week === thisIsoWeekKey())

  return (
    <Panel title="Workouts">
      <div className="stat-row">
        <div className="stat">
          <span className="value">{thisWeek?.count ?? 0}</span>
          <span className="label">this week</span>
        </div>
        <div className="stat">
          <span className="value">{formatMinutes(thisWeek?.minutes ?? 0)}</span>
          <span className="label">minutes</span>
        </div>
        <div className="stat">
          <span className="value">{Math.round(thisWeek?.kcal ?? 0)}</span>
          <span className="label">kcal</span>
        </div>
      </div>

      {sessions.length === 0 ? (
        <p className="empty-state">No sessions in the last {RECENT_DAYS} days.</p>
      ) : (
        sessions.slice(0, RECENT_SESSIONS_SHOWN).map((s) => (
          <div className="row" key={s.uuid}>
            <div>
              <div className="row-main">{s.title || s.exercise_type || 'workout'}</div>
              <div className="row-sub">{formatLocalDate(s.local_date)}</div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div className="row-main">{formatMinutes(s.duration_min)}</div>
              <div className="row-sub">{s.energy_kcal != null ? `${Math.round(s.energy_kcal)} kcal` : '—'}</div>
            </div>
          </div>
        ))
      )}
    </Panel>
  )
}
