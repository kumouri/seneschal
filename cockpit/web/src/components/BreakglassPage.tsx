import { useEffect, useState } from 'react'
import { breakglassStart, breakglassVerify, startBreakglassReauth } from '../breakglassApi'

type Action = 'restart' | 'force-pull'
type Rung = 1 | 3

interface ParsedHash {
  assertion: string
  action: Action
}

/** After rungs 1-2 (the Zitadel re-auth round trip), the cockpit backend redirects back here with
 * `#breakglass-assertion=<token>&breakglass-action=<action>` in the URL FRAGMENT (never sent to any
 * server, never logged) — see cockpit/server/app.py's `auth_callback`. */
function parseBreakglassHash(): ParsedHash | null {
  const hash = window.location.hash
  if (!hash || hash.length < 2) return null
  const params = new URLSearchParams(hash.slice(1))
  const assertion = params.get('breakglass-assertion')
  const action = params.get('breakglass-action')
  if (!assertion || (action !== 'restart' && action !== 'force-pull')) return null
  return { assertion, action }
}

type StartState =
  | { kind: 'idle' }
  | { kind: 'sending' }
  | { kind: 'ok'; attemptId: string }
  | { kind: 'error'; message: string }

type VerifyState =
  | { kind: 'idle' }
  | { kind: 'sending' }
  | { kind: 'ok'; steps: string[] }
  | { kind: 'error'; message: string }

// v5: the break-glass ladder (cockpit-spec.md "Break-glass") — visually distinct (warning styling) on
// purpose, so it never reads as just another dashboard panel. A three-rung stepper mirrors the actual
// trust chain: rungs 1-2 (password + TOTP) happen ENTIRELY as a Zitadel redirect this page triggers
// but never sees the inside of; rung 3 (a Telegram-delivered one-time phrase) is this page talking
// DIRECTLY to the separate break-glass supervisor (cockpit/breakglass/supervisor.py, port 8499) —
// never through the cockpit backend, so it survives that backend being broken.
export function BreakglassPage({ onBack }: { onBack: () => void }) {
  const [action, setAction] = useState<Action>('restart')
  const [forcePullConfirmed, setForcePullConfirmed] = useState(false)
  const [assertion, setAssertion] = useState<string | null>(null)
  const [rung, setRung] = useState<Rung>(1)
  const [start, setStart] = useState<StartState>({ kind: 'idle' })
  const [phrase, setPhrase] = useState('')
  const [verify, setVerify] = useState<VerifyState>({ kind: 'idle' })

  useEffect(() => {
    const parsed = parseBreakglassHash()
    if (parsed) {
      setAssertion(parsed.assertion)
      setAction(parsed.action)
      setRung(3)
      // The assertion is single-use and expires in ~2 minutes regardless, but there's no reason to
      // leave it sitting in the visible URL / browser history any longer than it has to.
      window.history.replaceState(null, '', window.location.pathname + window.location.search)
    }
  }, [])

  function beginReauth() {
    startBreakglassReauth(action) // full-page navigation — this component unmounts here
  }

  async function sendPhraseRequest() {
    if (!assertion) return
    setStart({ kind: 'sending' })
    const res = await breakglassStart(assertion, action)
    setStart(res.ok ? { kind: 'ok', attemptId: res.data.attempt_id } : { kind: 'error', message: res.error })
  }

  async function submitPhrase() {
    if (start.kind !== 'ok') return
    setVerify({ kind: 'sending' })
    const res = await breakglassVerify(start.attemptId, phrase)
    setVerify(res.ok ? { kind: 'ok', steps: res.data.steps } : { kind: 'error', message: res.error })
  }

  const canStartReauth = action === 'restart' || forcePullConfirmed

  return (
    <div className="breakglass-page">
      <div className="breakglass-banner">
        <span className="breakglass-banner-icon" aria-hidden="true">
          ⚠
        </span>
        <div>
          <h1>Break-glass — &ldquo;in case of emergency, break glass&rdquo;</h1>
          <p>
            Recovers a wedged daemon that <code>seneschald-update</code> can&apos;t fix on its own. Every
            rung below — success or failure — is audited and pushed to Telegram.
          </p>
        </div>
      </div>

      <button type="button" className="btn-secondary breakglass-back" onClick={onBack}>
        ← Back to the dashboard
      </button>

      <ol className="breakglass-stepper">
        <li className={rung >= 1 ? 'bg-step bg-step-active' : 'bg-step'}>
          <span className="bg-step-num">1–2</span>
          <span>Fresh re-auth (password + TOTP via Zitadel)</span>
        </li>
        <li className={rung >= 3 ? 'bg-step bg-step-active' : 'bg-step'}>
          <span className="bg-step-num">3a</span>
          <span>Assertion → Telegram phrase</span>
        </li>
        <li className={verify.kind === 'ok' ? 'bg-step bg-step-active' : 'bg-step'}>
          <span className="bg-step-num">3b</span>
          <span>Phrase confirmed → action executes</span>
        </li>
      </ol>

      {rung < 3 && (
        <div className="breakglass-panel">
          <h2>Choose an action</h2>
          <label className="breakglass-radio">
            <input
              type="radio"
              name="bg-action"
              checked={action === 'restart'}
              onChange={() => setAction('restart')}
            />
            <div>
              <strong>Restart</strong>
              <p className="row-sub">
                Kill + relaunch the daemon (the presence.lock PID + run-presence.cmd) — no code change.
              </p>
            </div>
          </label>
          <label className="breakglass-radio">
            <input
              type="radio"
              name="bg-action"
              checked={action === 'force-pull'}
              onChange={() => setAction('force-pull')}
            />
            <div>
              <strong>Force-pull</strong>
              <p className="row-sub">
                <code>git fetch</code> + <code>reset --hard</code> to the deploy branch +{' '}
                <code>uv sync --frozen</code> (best-effort), then the same restart. Discards
                uncommitted tracked changes in the live checkout — <code>state/</code> is gitignored
                and survives.
              </p>
            </div>
          </label>
          {action === 'force-pull' && (
            <label className="breakglass-checkbox">
              <input
                type="checkbox"
                checked={forcePullConfirmed}
                onChange={(e) => setForcePullConfirmed(e.target.checked)}
              />
              I understand this discards local tracked changes in the live checkout.
            </label>
          )}
          <button
            type="button"
            className="breakglass-danger-btn"
            disabled={!canStartReauth}
            onClick={beginReauth}
          >
            Start re-auth (rungs 1–2) →
          </button>
        </div>
      )}

      {rung === 3 && (
        <div className="breakglass-panel">
          <h2>Rung 3 — the Telegram phrase</h2>
          <p className="row-sub">
            Action: <strong>{action}</strong>. Rungs 1–2 just cleared a fresh Zitadel re-auth.
          </p>

          {start.kind !== 'ok' && (
            <button
              type="button"
              className="breakglass-danger-btn"
              disabled={start.kind === 'sending'}
              onClick={() => void sendPhraseRequest()}
            >
              {start.kind === 'sending' ? 'Sending…' : 'Send me the phrase on Telegram'}
            </button>
          )}
          {start.kind === 'error' && <p className="error-state">{start.message}</p>}

          {start.kind === 'ok' && verify.kind !== 'ok' && (
            <div className="breakglass-phrase-form">
              <label htmlFor="bg-phrase">Type the phrase Telegram just sent you</label>
              <input
                id="bg-phrase"
                className="breakglass-phrase-input"
                value={phrase}
                onChange={(e) => setPhrase(e.target.value)}
                placeholder="amber-otter-forty"
                autoComplete="off"
              />
              <button
                type="button"
                className="breakglass-danger-btn"
                disabled={verify.kind === 'sending' || !phrase.trim()}
                onClick={() => void submitPhrase()}
              >
                {verify.kind === 'sending' ? 'Verifying…' : `Verify & execute ${action}`}
              </button>
              {verify.kind === 'error' && <p className="error-state">{verify.message}</p>}
            </div>
          )}

          {verify.kind === 'ok' && (
            <div className="breakglass-result">
              <p className="pill pill-live">Executed</p>
              <ol>
                {verify.steps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
