import { getMeals } from '../api'
import { formatTimestamp } from '../format'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

// v4: meal-plan ideas Dream stages nightly from the active store into state/meals.json — a
// read-only snapshot, no cockpit->store coupling. A future health/nutrition archon could replace
// this feed; the empty state is that desk's placeholder in the meantime.
export function MealsPanel() {
  const result = usePolling(getMeals, 5000)

  if (!result) return <Panel title="Meals">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Meals">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { available, staged_at, plans } = result.data
  if (!available || plans.length === 0) {
    return (
      <Panel title="Meals">
        <p className="empty-state">No meal plans staged — the nutrition desk, reserved.</p>
      </Panel>
    )
  }

  return (
    <Panel title="Meals" right={<span className="row-sub">staged {formatTimestamp(staged_at)}</span>}>
      {plans.map((p, i) => {
        const card = (
          <>
            <div className="meal-card-title">{p.title}</div>
            {p.summary && <div className="meal-card-summary">{p.summary}</div>}
            {p.tags && p.tags.length > 0 && (
              <div className="meal-tags">
                {p.tags.map((t) => (
                  <span className="meal-tag" key={t}>
                    {t}
                  </span>
                ))}
              </div>
            )}
          </>
        )
        return p.url ? (
          <a className="meal-card" href={p.url} target="_blank" rel="noreferrer" key={`${p.title}-${i}`}>
            {card}
          </a>
        ) : (
          <div className="meal-card" key={`${p.title}-${i}`}>
            {card}
          </div>
        )
      })}
    </Panel>
  )
}
