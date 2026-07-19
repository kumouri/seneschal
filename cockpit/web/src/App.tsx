import { getAuthStatus } from './api'
import { ArchonsPanel } from './components/ArchonsPanel'
import { ChatPanel } from './components/ChatPanel'
import { DeployHealthPanel } from './components/DeployHealthPanel'
import { HealthSummaryPanel } from './components/HealthSummaryPanel'
import { MealsPanel } from './components/MealsPanel'
import { ModelDialsPanel } from './components/ModelDialsPanel'
import { NutritionPanel } from './components/NutritionPanel'
import { OneiroiPanel } from './components/OneiroiPanel'
import { PresencePanel } from './components/PresencePanel'
import { RemindersPanel } from './components/RemindersPanel'
import { RestartControl } from './components/RestartControl'
import { RouterPanel } from './components/RouterPanel'
import { SessionsPanel } from './components/SessionsPanel'
import { SleepPanel } from './components/SleepPanel'
import { StatusPanel } from './components/StatusPanel'
import { ThresholdsPanel } from './components/ThresholdsPanel'
import { UsagePanel } from './components/UsagePanel'
import { WorkoutsPanel } from './components/WorkoutsPanel'
import { usePolling } from './usePolling'

export default function App() {
  const authStatus = usePolling(getAuthStatus, 30000)

  // In real-auth ("oidc") mode with no valid session, show a login screen instead of a dashboard
  // that would just 401 on every panel. `GET /api/auth/status` is the one PUBLIC endpoint that can
  // answer this without itself needing a session. (The full OIDC auth stack is a deferred follow-up —
  // in this build the backend runs dev-no-auth or unconfigured, so this branch is normally inert.)
  if (authStatus?.ok && authStatus.data.mode === 'oidc' && !authStatus.data.authenticated) {
    return (
      <div className="login-screen">
        <h1>Seneschal Cockpit</h1>
        <p>Sign in to continue.</p>
        <a className="restart-btn" href="/auth/login">
          Log in
        </a>
      </div>
    )
  }

  return (
    <>
      <header className="header">
        <div>
          <h1>Seneschal Cockpit</h1>
          <div className="subtitle">
            Live chat over the daemon pipe, model dials + thresholds, archon tiles, and the
            health/meal panel group — dev-no-auth build (real auth is a deferred follow-up)
          </div>
        </div>
        <div className="header-actions">
          {authStatus?.ok && authStatus.data.mode === 'oidc' && (
            <a className="btn-secondary" href="/auth/logout">
              Log out{authStatus.data.user ? ` (${authStatus.data.user})` : ''}
            </a>
          )}
          <RestartControl />
        </div>
      </header>

      <main className="layout">
        <ChatPanel />

        <div className="grid">
          <SessionsPanel />
          <StatusPanel />
          <ModelDialsPanel />
          <ThresholdsPanel />
          <RouterPanel />
          <ArchonsPanel />
          <OneiroiPanel />
          <DeployHealthPanel />
          <PresencePanel />
          <RemindersPanel />
          <UsagePanel />
          <HealthSummaryPanel />
          <SleepPanel />
          <WorkoutsPanel />
          <NutritionPanel />
          <MealsPanel />
        </div>
      </main>

      <p className="footer-note">
        Reads seneschal/state/* read-only, every ~5s; the chat pane is live over the daemon pipe (GET
        /api/ws). Writes: the restart button, model/threshold saves, and anything you send in chat.
      </p>
    </>
  )
}
