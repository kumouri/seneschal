import { useEffect, useState } from 'react'
import { getModelConfig, postRestart, putModelConfig } from '../api'
import { KNOWN_MODELS } from '../types'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'ok' } | { kind: 'error'; message: string }
type ApplyState =
  | { kind: 'idle' }
  | { kind: 'confirming' }
  | { kind: 'sending' }
  | { kind: 'ok'; alreadyQueued: boolean }
  | { kind: 'error'; message: string }

const DEFAULT_WARM = KNOWN_MODELS[2].id // opus — a reasonable pre-selection before the first load resolves
const DEFAULT_CEILING = KNOWN_MODELS[2].id // opus — NOT fable, so a fresh cockpit never defaults to
                                          // enabling delegation by accident

// The two model dials (cockpit-spec.md "Model dials & Fable delegation", v3): warm_model (read at
// warm-session SPAWN — "apply now" queues a graceful restart to pick it up promptly) and
// max_routable_model (the live ceiling on every Fable delegation, by any trigger).
export function ModelDialsPanel() {
  const result = usePolling(getModelConfig, 5000)
  const [warm, setWarm] = useState<string>(DEFAULT_WARM)
  const [ceiling, setCeiling] = useState<string>(DEFAULT_CEILING)
  const [loadedOnce, setLoadedOnce] = useState(false)
  const [save, setSave] = useState<SaveState>({ kind: 'idle' })
  const [apply, setApply] = useState<ApplyState>({ kind: 'idle' })

  // Seed the selects from the backend exactly once (the first successful poll) — afterward the
  // selects are the user's own editing state, not re-clobbered by the ~5s background poll.
  useEffect(() => {
    if (!loadedOnce && result?.ok) {
      if (result.data.warm_model) setWarm(result.data.warm_model)
      if (result.data.max_routable_model) setCeiling(result.data.max_routable_model)
      setLoadedOnce(true)
    }
  }, [result, loadedOnce])

  async function save_() {
    setSave({ kind: 'saving' })
    const res = await putModelConfig(warm, ceiling)
    if (res.ok) {
      setWarm(res.data.warm_model ?? warm)
      setCeiling(res.data.max_routable_model ?? ceiling)
      setSave({ kind: 'ok' })
    } else {
      setSave({ kind: 'error', message: res.error })
    }
  }

  async function confirmApply() {
    setApply({ kind: 'sending' })
    const res = await postRestart()
    setApply(res.ok ? { kind: 'ok', alreadyQueued: res.data.already_queued } : { kind: 'error', message: res.error })
  }

  if (!result) return <Panel title="Model dials">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Model dials">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const ceilingAdmitsFable = ceiling === 'claude-fable-5'

  return (
    <Panel
      title="Model dials"
      right={
        <span className={`pill ${ceilingAdmitsFable ? 'pill-live' : 'pill-idle'}`}>
          {ceilingAdmitsFable ? 'Fable enabled' : 'Fable off'}
        </span>
      }
    >
      <div className="dial-row">
        <label className="dial-label" htmlFor="dial-warm">
          Warm model <span className="row-sub">(the resident session — applies at next spawn)</span>
        </label>
        <select id="dial-warm" className="dial-select" value={warm} onChange={(e) => setWarm(e.target.value)}>
          {KNOWN_MODELS.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
      </div>
      <div className="dial-row">
        <label className="dial-label" htmlFor="dial-ceiling">
          Max routable model{' '}
          <span className="row-sub">(the ceiling on every Fable delegation, by any trigger)</span>
        </label>
        <select
          id="dial-ceiling"
          className="dial-select"
          value={ceiling}
          onChange={(e) => setCeiling(e.target.value)}
        >
          {KNOWN_MODELS.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
      </div>

      <div className="dial-actions">
        <button type="button" className="restart-btn" disabled={save.kind === 'saving'} onClick={() => void save_()}>
          {save.kind === 'saving' ? 'Saving…' : 'Save'}
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={apply.kind === 'sending'}
          onClick={() => setApply({ kind: 'confirming' })}
        >
          Apply now
        </button>
      </div>
      {save.kind === 'ok' && <p className="row-sub">Saved.</p>}
      {save.kind === 'error' && <p className="error-state">{save.message}</p>}
      {apply.kind === 'ok' && (
        <p className="row-sub">
          {apply.kind === 'ok' && apply.alreadyQueued
            ? 'A restart was already queued.'
            : 'Graceful restart queued — the warm session picks up the new warm_model once it applies.'}
        </p>
      )}
      {apply.kind === 'error' && <p className="error-state">{apply.message}</p>}
      {result.data.updated_at && (
        <p className="row-sub">last saved {new Date(result.data.updated_at).toLocaleString()}</p>
      )}

      {apply.kind === 'confirming' && (
        <div className="dialog-backdrop" role="dialog" aria-modal="true">
          <div className="dialog">
            <h2>Apply the warm model now?</h2>
            <p>
              This queues the SAME graceful restart as the header's restart button — it only applies once
              the warm session is idle, so it won't interrupt a live conversation. Save your changes first
              if you haven't already.
            </p>
            <div className="dialog-actions">
              <button type="button" className="btn-secondary" onClick={() => setApply({ kind: 'idle' })}>
                Cancel
              </button>
              <button type="button" className="restart-btn" onClick={() => void confirmApply()}>
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </Panel>
  )
}
