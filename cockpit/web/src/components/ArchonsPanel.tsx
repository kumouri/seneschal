import { archonUrl, getArchons } from '../api'
import { usePolling } from '../usePolling'
import { ArchonRestartButton } from './ArchonRestartButton'
import { AuthGate, Panel } from './Panel'

// Archon tiles — one card per registry entry (cockpit/server/archon-registry.json, per-install,
// mirroring seneschal/references/archons.md). A "live" archon opens its proxied UI
// (GET /archons/<id>/*) in a new tab through the same cockpit server — archons never face the
// internet directly. A "live" archon ALSO gets a confirm-gated Restart button (daemon-supervised
// archon sites — see ArchonRestartButton), since it's the one with an actual site process for the
// daemon to supervise.
//
// Anything else shows a greyed placeholder card, labelled from its actual status: "admitted" = minted
// and working, just has no web UI to open (delegation-on-demand); "specced" = minted but the admission
// gate hasn't run yet; "reserved" = not yet minted. A single hardcoded "reserved — unminted" label
// would read as a flat lie about every admitted archon without a site. Unknown statuses pass through
// as their raw slug rather than vanishing.
const STATUS_LABELS: Record<string, string> = {
  admitted: 'admitted — no web UI',
  specced: 'specced — awaiting admission',
  reserved: 'reserved — unminted',
}

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
                  <ArchonRestartButton archonId={a.id} archonTitle={a.title} />
                </>
              ) : (
                <span className="pill pill-idle">{STATUS_LABELS[a.status] ?? a.status}</span>
              )}
            </div>
          )
        })}
      </div>
    </Panel>
  )
}
