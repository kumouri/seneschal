import { useState } from 'react'
import { postArchonRestart } from '../api'

type AckState = { kind: 'idle' } | { kind: 'sending' } | { kind: 'ok'; alreadyQueued: boolean } | { kind: 'error'; message: string }

// The Archons panel's per-site Restart button (daemon-supervised archon sites) — same confirm-gated
// shape as RestartControl.tsx's daemon-wide restart, scoped to ONE archon. Queues a `restart-site`
// control (POST /api/archons/{id}/restart) the daemon's archon_sites_task (the seventh supervised
// task, ~20s reconcile cadence) picks up next pass and applies immediately — unlike the daemon restart,
// this never waits on the warm chat session going idle.
export function ArchonRestartButton({ archonId, archonTitle }: { archonId: string; archonTitle: string }) {
  const [confirming, setConfirming] = useState(false)
  const [ack, setAck] = useState<AckState>({ kind: 'idle' })

  async function confirmRestart() {
    setConfirming(false)
    setAck({ kind: 'sending' })
    const result = await postArchonRestart(archonId)
    if (result.ok) {
      setAck({ kind: 'ok', alreadyQueued: result.data.already_queued })
    } else {
      setAck({ kind: 'error', message: result.error })
    }
  }

  return (
    <>
      <button
        type="button"
        className="restart-btn archon-restart-btn"
        disabled={ack.kind === 'sending'}
        onClick={() => setConfirming(true)}
      >
        {ack.kind === 'sending' ? 'Queuing…' : 'Restart'}
      </button>

      {ack.kind === 'ok' && (
        <p className="row-sub archon-restart-ack">
          {ack.alreadyQueued ? 'Already queued.' : 'Restart queued.'}
        </p>
      )}
      {ack.kind === 'error' && <p className="error-state archon-restart-ack">{ack.message}</p>}

      {confirming && (
        <div className="dialog-backdrop" role="dialog" aria-modal="true">
          <div className="dialog">
            <h2>Restart {archonTitle}?</h2>
            <p>
              This queues a restart of this archon's site process — the daemon applies it on its next
              reconcile pass (usually within ~20s), independent of the warm chat session.
            </p>
            <div className="dialog-actions">
              <button type="button" className="btn-secondary" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button type="button" className="restart-btn" onClick={() => void confirmRestart()}>
                Confirm restart
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
