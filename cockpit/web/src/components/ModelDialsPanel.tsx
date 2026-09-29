import { useEffect, useState } from 'react'
import { getModelConfig, postRestart, putModelConfig } from '../api'
import { KNOWN_MODELS, type BackendOption, type ModelOption } from '../types'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

// The pluggable-backend axis — a hardcoded FALLBACK ONLY, for the same reason KNOWN_MODELS
// below is one: an older backend that predates `known_backends` still renders something sane. The
// live source of truth is always `GET /api/model-config`'s own `known_backends`.
const FALLBACK_BACKENDS: BackendOption[] = [
  { id: 'claude-cli', label: 'Claude Code CLI', models: KNOWN_MODELS.map((m) => ({ ...m })) },
]

function modelsForBackend(known: BackendOption[] | null | undefined, backend: string): ModelOption[] {
  const list = known && known.length > 0 ? known : FALLBACK_BACKENDS
  return list.find((b) => b.id === backend)?.models ?? []
}

/**
 * Build the option list for a dial, and GUARANTEE the current value is in it.
 *
 * This is the fix for a silent downgrade. The frontend used to own the model list, it never gained
 * `claude-opus-5`, and a `<select>` whose value matches no option renders the FIRST option instead —
 * so the cockpit displayed "Haiku" while the daemon genuinely ran opus-5, and the only option labelled
 * "Opus" wrote back opus-4-8 on save. The list now comes from the backend (derived from its RANK), but
 * the deeper rule is the second half: **never render a value we can't represent.** An unrecognized
 * value gets its own explicit option, labelled as unrecognized, so a future drift is visible and
 * cannot be silently rewritten by saving.
 */
export function dialOptions(served: ModelOption[] | null | undefined, current: string): ModelOption[] {
  const base = served && served.length > 0 ? served : KNOWN_MODELS.map((m) => ({ id: m.id, label: m.label }))
  return base.some((m) => m.id === current) ? base : [...base, { id: current, label: `${current} (unrecognized)` }]
}

type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'ok' } | { kind: 'error'; message: string }
type ApplyState =
  | { kind: 'idle' }
  | { kind: 'confirming' }
  | { kind: 'sending' }
  | { kind: 'ok'; alreadyQueued: boolean }
  | { kind: 'error'; message: string }

// Named explicitly, NOT indexed into KNOWN_MODELS. `KNOWN_MODELS[2]` silently meant a different model
// the moment the list changed — the same class of fragility that produced the opus-5 bug.
const DEFAULT_BACKEND = 'claude-cli' // the live daemon's own default backend;
                                     // switching is a deliberate tap in this panel, never a default
const DEFAULT_WARM = 'claude-opus-4-8' // a safe pre-selection before the first load resolves
const DEFAULT_CEILING = 'claude-opus-4-8' // NOT fable, so a fresh cockpit never defaults to
                                          // enabling delegation by accident

// The model dials (cockpit-spec.md "Model dials & Fable delegation", v3; the `backend` axis since
// the pluggable-backend work): which BACKEND runs the warm session, warm_model (read at
// warm-session SPAWN — "apply now" queues a graceful restart to pick it up promptly) and
// max_routable_model (the live ceiling on every Fable delegation, by any trigger — a claude-cli-only
// concept; Fable never admits on any other backend, by construction, not by this panel's own logic).
export function ModelDialsPanel() {
  const result = usePolling(getModelConfig, 5000)
  const [backend, setBackend] = useState<string>(DEFAULT_BACKEND)
  const [warm, setWarm] = useState<string>(DEFAULT_WARM)
  const [ceiling, setCeiling] = useState<string>(DEFAULT_CEILING)
  const [loadedOnce, setLoadedOnce] = useState(false)
  const [save, setSave] = useState<SaveState>({ kind: 'idle' })
  const [apply, setApply] = useState<ApplyState>({ kind: 'idle' })

  // Seed the selects from the backend exactly once (the first successful poll) — afterward the
  // selects are the user's own editing state, not re-clobbered by the ~5s background poll.
  useEffect(() => {
    if (!loadedOnce && result?.ok) {
      if (result.data.backend) setBackend(result.data.backend)
      if (result.data.warm_model) setWarm(result.data.warm_model)
      if (result.data.max_routable_model) setCeiling(result.data.max_routable_model)
      setLoadedOnce(true)
    }
  }, [result, loadedOnce])

  // Switching backend mid-edit: the previous warm/ceiling ids almost certainly don't exist on the
  // new backend's own table, so they'd render via dialOptions' "(unrecognized)" fallback until
  // Saved — snap both to the new backend's own weakest model instead, a safe, always-valid default
  // the user can then raise, mirroring DEFAULT_WARM/DEFAULT_CEILING's own "never default to the top
  // tier" posture.
  function changeBackend(next: string) {
    setBackend(next)
    const options = modelsForBackend(result?.ok ? result.data.known_backends : null, next)
    const first = options[0]
    if (first) {
      setWarm(first.id)
      setCeiling(first.id)
    }
  }

  async function save_() {
    setSave({ kind: 'saving' })
    const res = await putModelConfig(warm, ceiling, backend)
    if (res.ok) {
      if (res.data.backend) setBackend(res.data.backend)
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

  // Fable is a claude-cli-only concept — the pill reflects
  // that structurally rather than just happening to read False on another backend's model ids.
  const ceilingAdmitsFable = backend === 'claude-cli' && ceiling === 'claude-fable-5'
  const backendOptions = result.data.known_backends?.length ? result.data.known_backends : FALLBACK_BACKENDS
  const backendModels = modelsForBackend(result.data.known_backends, backend)
  const warmOptions = dialOptions(backendModels, warm)
  const ceilingOptions = dialOptions(backendModels, ceiling)

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
        <label className="dial-label" htmlFor="dial-backend">
          Backend <span className="row-sub">(which CLI runs the warm session — applies at next spawn)</span>
        </label>
        <select
          id="dial-backend"
          className="dial-select"
          value={backend}
          onChange={(e) => changeBackend(e.target.value)}
        >
          {backendOptions.map((b) => (
            <option key={b.id} value={b.id}>
              {b.label}
            </option>
          ))}
        </select>
      </div>
      <div className="dial-row">
        <label className="dial-label" htmlFor="dial-warm">
          Warm model <span className="row-sub">(the resident session — applies at next spawn)</span>
        </label>
        <select id="dial-warm" className="dial-select" value={warm} onChange={(e) => setWarm(e.target.value)}>
          {warmOptions.map((m) => (
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
          {ceilingOptions.map((m) => (
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
