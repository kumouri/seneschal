import { useState } from 'react'
import { getAuthStatus } from './api'
import { ArchonsPanel } from './components/ArchonsPanel'
import { BreakglassPage } from './components/BreakglassPage'
import { ChatPanel } from './components/ChatPanel'
import { DeployHealthPanel } from './components/DeployHealthPanel'
import { DocStatusPanel } from './components/DocStatusPanel'
import { PanelErrorBoundary } from './components/ErrorBoundary'
import { HealthSummaryPanel } from './components/HealthSummaryPanel'
import { JobsPanel } from './components/JobsPanel'
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
import { TracePanel } from './components/TracePanel'
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
            Live chat over the daemon pipe, the session trace, the background-jobs monitor, the
            open-spec ledger, model dials + thresholds, archon tiles, the health/meal panel group,
            and real OIDC auth + break-glass (dev-no-auth remains the default until an IdP is
            configured)
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
        {/* Every panel renders inside its own error boundary: one panel rendering a malformed field
            must never blank the whole dashboard (ErrorBoundary.tsx). */}
        <PanelErrorBoundary name="Chat">
          <ChatPanel />
        </PanelErrorBoundary>

        {/* Trace sits HERE and not in the grid below: it is a two-pane log reader over verbatim
            transcript text, and a 320px-minimum tile starved its content column to zero pixels
            (TracePanel.tsx, decision 1). Sized like the chat pane above it, collapsed by default. */}
        <PanelErrorBoundary name="Trace">
          <TracePanel />
        </PanelErrorBoundary>

        <div className="grid">
          {(
            [
              ['Sessions', SessionsPanel],
              ['Status', StatusPanel],
              ['Jobs', JobsPanel],
              // The open-spec ledger. A TILE rather than a section under the chat pane, unlike the
              // Trace panel: this is a reference list you go looking for, not a live read-along of
              // the conversation. Collapsed by default for the same reason.
              ['Open specs', DocStatusPanel],
              ['Model dials', ModelDialsPanel],
              ['Thresholds', ThresholdsPanel],
              ['Router', RouterPanel],
              ['Archons', ArchonsPanel],
              ['Oneiroi', OneiroiPanel],
              ['Deploy health', DeployHealthPanel],
              ['Presence', PresencePanel],
              ['Reminders', RemindersPanel],
              ['Usage', UsagePanel],
              ['Health', HealthSummaryPanel],
              ['Sleep', SleepPanel],
              ['Workouts', WorkoutsPanel],
              ['Nutrition', NutritionPanel],
              ['Meals', MealsPanel],
            ] as const
          ).map(([name, PanelComponent]) => (
            <PanelErrorBoundary key={name} name={name}>
              <PanelComponent />
            </PanelErrorBoundary>
          ))}
        </div>
      </main>

      <p className="footer-note">
        Reads seneschal/state/* read-only, every ~5s; the chat pane is live over the daemon pipe (GET
        /api/ws). Writes: the restart buttons (daemon + per-archon), model/threshold saves, and anything you send in
        chat.
      </p>
    </>
  )
}
