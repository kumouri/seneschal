import { getHealthNutrition } from '../api'
import { formatLocalDate } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

const RECENT_DAYS = 14

// v4 (cockpit-spec.md "Health pipeline extension (v4)"): today's kcal/protein tiles + a short
// per-day history. Source: state/health.db's nutrition table (Health Connect NutritionRecord, Call
// Shield v4 live feed only — see cockpit/server/health.py::read_nutrition). "Today" is days[0] —
// the freshest date the backend returned, honestly labeled with its own date rather than assumed.
export function NutritionPanel() {
  const result = usePolling(() => getHealthNutrition(RECENT_DAYS), 5000)

  if (!result) return <Panel title="Nutrition">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Nutrition">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { available, days } = result.data
  const today = days[0]
  if (!available || !today) {
    return (
      <Panel title="Nutrition">
        <p className="empty-state">No nutrition entries in health.db yet.</p>
      </Panel>
    )
  }

  const history = days.slice(1)

  return (
    <Panel title="Nutrition">
      <div className="health-tile-row">
        <div className="health-tile">
          <span className="value">{Math.round(today.total_kcal)}</span>
          <span className="label">kcal · {formatLocalDate(today.date)}</span>
        </div>
        <div className="health-tile">
          <span className="value">{Math.round(today.total_protein_g)}g</span>
          <span className="label">protein</span>
        </div>
        <div className="health-tile">
          <span className="value">{Math.round(today.total_carbs_g)}g</span>
          <span className="label">carbs</span>
        </div>
        <div className="health-tile">
          <span className="value">{Math.round(today.total_fat_g)}g</span>
          <span className="label">fat</span>
        </div>
      </div>

      {history.length === 0 ? (
        <p className="empty-state">No earlier days logged in the last {RECENT_DAYS} days.</p>
      ) : (
        history.map((d) => (
          <div className="row" key={d.date}>
            <span className="row-main">{formatLocalDate(d.date)}</span>
            <span className="row-sub">
              {Math.round(d.total_kcal)} kcal · {Math.round(d.total_protein_g)}g protein ·{' '}
              {d.entries.length} entr{d.entries.length === 1 ? 'y' : 'ies'}
            </span>
          </div>
        ))
      )}
    </Panel>
  )
}
