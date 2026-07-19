import { useState } from 'react'
import { postRestart } from '../api'

type AckState = { kind: 'idle' } | { kind: 'sending' } | { kind: 'ok'; alreadyQueued: boolean } | { kind: 'error'; message: string }

// The one mutating control shipped in v1: a graceful restart, enqueued exactly the way
// seneschal/scripts/request_control.py does (control-queue.json, defer_until_idle=true) — the daemon
// applies it once its warm session goes idle, never mid-conversation. Confirm-gated because it's the
// one button on this page that isn't a pure read.
export function RestartControl() {
  const [confirming, setConfirming] = useState(false)
  const [ack, setAck] = useState<AckState>({ kind: 'idle' })

  async function confirmRestart() {
    setConfirming(false)
    setAck({ kind: 'sending' })
    const result = await postRestart()
    if (result.ok) {
      setAck({ kind: 'ok', alreadyQueued: result.data.already_queued })
    } else {
      setAck({ kind: 'error', message: result.error })
    }
  }

  return (
    <div>
      <button
        type="button"
        className="restart-btn"
        disabled={ack.kind === 'sending'}
        onClick={() => setConfirming(true)}
      >
        {ack.kind === 'sending' ? 'Queuing…' : 'Restart daemon'}
      </button>

      {ack.kind === 'ok' && (
        <p className="row-sub" style={{ marginTop: 8 }}>
          {ack.alreadyQueued
            ? 'A restart was already queued — nothing new to do.'
            : 'Graceful restart queued — applies once the warm session goes idle.'}
        </p>
      )}
      {ack.kind === 'error' && <p className="error-state" style={{ marginTop: 8 }}>{ack.message}</p>}

      {confirming && (
        <div className="dialog-backdrop" role="dialog" aria-modal="true">
          <div className="dialog">
            <h2>Restart the daemon?</h2>
            <p>
              This queues a <strong>graceful</strong> restart — it only applies once presence.py's warm
              chat session is idle, so it won't interrupt a live conversation. No message is dropped.
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
    </div>
  )
}
