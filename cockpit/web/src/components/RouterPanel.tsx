import { getRouterStats } from '../api'
import { formatTimestamp } from '../format'
import type { RouterLogEntry } from '../types'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

const RECENT_LIMIT = 50 // enough to bucket a small fable-arm-over-time placeholder chart client-side

function bucketFableArmByDay(entries: RouterLogEntry[]): { day: string; fable: number; standard: number }[] {
  const byDay = new Map<string, { fable: number; standard: number }>()
  for (const e of entries) {
    if (e.arm !== 'fable') continue
    const day = e.ts ? e.ts.slice(0, 10) : 'unknown'
    const bucket = byDay.get(day) ?? { fable: 0, standard: 0 }
    if (e.verdict === 'fable') bucket.fable += 1
    else bucket.standard += 1
    byDay.set(day, bucket)
  }
  return [...byDay.entries()].map(([day, counts]) => ({ day, ...counts })).sort((a, b) => a.day.localeCompare(b.day))
}

function VerdictBars({ label, counts }: { label: string; counts: Record<string, number> }) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0)
  if (total === 0) return null
  return (
    <div className="router-arm-block">
      <div className="row-main">{label}</div>
      {Object.entries(counts).map(([verdict, n]) => (
        <div className="router-bar-row" key={verdict}>
          <span className="router-bar-label">{verdict}</span>
          <div className="router-bar-track">
            <div className="router-bar-fill" style={{ width: `${(n / total) * 100}%` }} />
          </div>
          <span className="row-sub">{n}</span>
        </div>
      ))}
    </div>
  )
}

export function RouterPanel() {
  const result = usePolling(() => getRouterStats(RECENT_LIMIT), 5000)

  if (!result) return <Panel title="Router">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Router">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const { counts, recent, total } = result.data
  const dayBuckets = bucketFableArmByDay(recent)
  const maxDayTotal = Math.max(1, ...dayBuckets.map((b) => b.fable + b.standard))

  return (
    <Panel title="Router" right={<span className="pill pill-info">{total} logged</span>}>
      {total === 0 ? (
        <p className="empty-state">No router verdicts logged yet.</p>
      ) : (
        <>
          {counts.triage && <VerdictBars label="Triage arm (trivial vs escalate — shadow)" counts={counts.triage} />}
          {counts.fable && <VerdictBars label="Fable arm (standard vs fable-level)" counts={counts.fable} />}

          {dayBuckets.length > 0 && (
            <div className="router-arm-block">
              <div className="row-main">Fable-arm verdicts by day</div>
              <p className="row-sub">
                Volume only — a real accuracy-over-time chart needs human-judged ground truth, which
                doesn't exist yet (placeholder per cockpit-spec.md v3).
              </p>
              {dayBuckets.map((b) => (
                <div className="router-bar-row" key={b.day}>
                  <span className="router-bar-label">{b.day}</span>
                  <div className="router-bar-track">
                    <div
                      className="router-bar-fill router-bar-fable"
                      style={{ width: `${(b.fable / maxDayTotal) * 100}%` }}
                    />
                    <div
                      className="router-bar-fill router-bar-standard"
                      style={{ width: `${(b.standard / maxDayTotal) * 100}%` }}
                    />
                  </div>
                  <span className="row-sub">
                    {b.fable} fable / {b.standard} standard
                  </span>
                </div>
              ))}
            </div>
          )}

          <div className="router-arm-block">
            <div className="row-main">Recent decisions</div>
            {recent.slice(0, 10).map((r, i) => (
              <div className="row" key={i}>
                <div>
                  <div className="row-main">{r.text_preview || '(empty)'}</div>
                  <div className="row-sub">
                    {r.channel ?? '?'} · {r.arm} · {r.category ?? ''}
                  </div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <span className={`pill ${r.verdict === 'fable' ? 'pill-live' : 'pill-idle'}`}>{r.verdict}</span>
                  <div className="row-sub">{formatTimestamp(r.ts)}</div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </Panel>
  )
}
