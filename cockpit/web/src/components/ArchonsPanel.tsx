import { archonUrl, getArchons } from '../api'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

// Archon tiles — one card per registry entry (cockpit/server/archon-registry.json, per-install,
// mirroring seneschal/references/archons.md). A "live" archon opens its proxied UI
// (GET /archons/<id>/*) in a new tab through the same cockpit server — archons never face the
// internet directly. A "reserved" (unminted) archon shows a greyed placeholder card instead of
// a link.
export function ArchonsPanel() {
  const result = usePolling(getArchons, 15000)

  if (!result) return <Panel title="Archons">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Archons">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const archons = result.data.archons
  if (archons.length === 0) {
    return (
      <Panel title="Archons">
        <p className="empty-state">No archons registered yet.</p>
      </Panel>
    )
  }

  return (
    <Panel title="Archons">
      <div className="archon-tiles">
        {archons.map((a) => {
          const isLive = a.status === 'live'
          return (
            <div key={a.id} className={`archon-tile ${isLive ? 'archon-tile-live' : 'archon-tile-reserved'}`}>
              <div className="archon-tile-title">{a.title}</div>
              {isLive ? (
                <>
                  <span className={`pill ${a.reachable ? 'pill-live' : 'pill-danger'}`}>
                    {a.reachable ? 'reachable' : 'unreachable'}
                  </span>
                  <a className="archon-open-link" href={archonUrl(a.id)} target="_blank" rel="noreferrer">
                    Open →
                  </a>
                </>
              ) : (
                <span className="pill pill-idle">reserved — unminted</span>
              )}
            </div>
          )
        })}
      </div>
    </Panel>
  )
}
