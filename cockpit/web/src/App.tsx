import { useState } from 'react'
import { getAuthStatus } from './api'
import { ArchonsPanel } from './components/ArchonsPanel'
import { BreakglassPage } from './components/BreakglassPage'
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

type Page = 'dashboard' | 'breakglass'

// The break-glass ladder redirects back here as `/#breakglass-assertion=...` (an IdP round trip is
// a real page navigation, so the SPA reboots cold) — route straight to the Break-glass page on that
// hash instead of making the user find the nav button again. No router library: one boolean is enough
// for two pages (same "no new npm deps" posture as the plain-CSS panels).
function initialPage(): Page {
  return window.location.hash.startsWith('#breakglass') ? 'breakglass' : 'dashboard'
}

export default function App() {
  const [page, setPage] = useState<Page>(initialPage)
  const authStatus = usePolling(getAuthStatus, 30000)

  if (page === 'breakglass') {
    return <BreakglassPage onBack={() => setPage('dashboard')} />
  }

  // In real-auth ("oidc") mode with no valid session, show a login screen instead of a dashboard
  // that would just 401 on every panel. `GET /api/auth/status` is the one PUBLIC endpoint that can
  // answer this without itself needing a session.
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
            Live chat over the daemon pipe, model dials + thresholds, archon tiles, the
            health/meal panel group, and real OIDC auth + break-glass (dev-no-auth remains the
            default until an IdP is configured)
          </div>
        </div>
        <div className="header-actions">
          <button type="button" className="btn-secondary" onClick={() => setPage('breakglass')}>
            Break-glass
          </button>
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
