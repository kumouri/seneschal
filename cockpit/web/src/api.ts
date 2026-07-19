// Thin fetch wrappers over the cockpit backend's /api/* routes. Every gated route 503s with
// {"detail": "auth not configured"} until COCKPIT_DEV_NO_AUTH=1 is set on the backend (v0/v1 stub —
// see cockpit/server/auth.py) — ApiResult surfaces that as a normal (not thrown) error so panels can
// render a clear "set the dev-auth flag" state instead of a blank crash.
import type {
  ArchonsResponse,
  AuthStatusResponse,
  EmotesResponse,
  GovernorConfigResponse,
  HealthResponse,
  HealthSummaryResponse,
  SeneschaldHealthResponse,
  MealsResponse,
  ModelConfigResponse,
  NutritionResponse,
  OneiroiResponse,
  PresenceResponse,
  RemindersResponse,
  RestartResponse,
  RouterStatsResponse,
  SessionsResponse,
  SleepResponse,
  StatusResponse,
  TranscriptResponse,
  UsageResponse,
  WorkoutsResponse,
} from './types'

const BASE = '/api'

// v5: every mutating route depends on `require_csrf` (cockpit/server/auth.py), which is a no-op
// outside real-auth ("oidc") mode but otherwise requires this exact header — see that module's
// docstring for why a custom header + SameSite=Strict is sufficient CSRF protection. Sent on every
// request (harmless on GETs) so callers never have to remember it per mutating call.
const CSRF_HEADER_NAME = 'X-Cockpit-Requested-With'
const CSRF_HEADER_VALUE = 'cockpit'

export type ApiResult<T> = { ok: true; data: T } | { ok: false; status: number; error: string }

async function request<T>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  try {
    const res = await fetch(`${BASE}${path}`, {
      ...init,
      // Merged (not replaced) so a caller's own headers (e.g. putModelConfig's Content-Type) don't
      // silently drop the Accept/CSRF headers a plain object-spread ordering would have cost.
      headers: { Accept: 'application/json', [CSRF_HEADER_NAME]: CSRF_HEADER_VALUE, ...init?.headers },
    })
    if (!res.ok) {
      let detail = res.statusText || `HTTP ${res.status}`
      try {
        const body = (await res.json()) as { detail?: string }
        if (body && typeof body.detail === 'string') detail = body.detail
      } catch {
        // non-JSON error body — keep the statusText fallback
      }
      return { ok: false, status: res.status, error: detail }
    }
    const data = (await res.json()) as T
    return { ok: true, data }
  } catch (err) {
    return { ok: false, status: 0, error: err instanceof Error ? err.message : 'network error' }
  }
}

export const getHealth = () => request<HealthResponse>('/health')
export const getSessions = () => request<SessionsResponse>('/sessions')
export const getOneiroi = (limit = 20) => request<OneiroiResponse>(`/oneiroi?limit=${limit}`)
export const getSeneschaldHealth = () => request<SeneschaldHealthResponse>('/seneschald-health')
export const getPresence = () => request<PresenceResponse>('/presence')
export const getReminders = () => request<RemindersResponse>('/reminders')
export const getUsage = () => request<UsageResponse>('/usage')
export const getStatus = () => request<StatusResponse>('/status')

export const postRestart = () => request<RestartResponse>('/control/restart', { method: 'POST' })

// v2: the daemon pipe / chat pane.
export const getTranscript = (limit = 200) => request<TranscriptResponse>(`/transcript?limit=${limit}`)
export const getEmotes = () => request<EmotesResponse>('/emotes')
export const emoteUrl = (file: string) => `${BASE}/emotes/${encodeURIComponent(file)}`

// v3: model dials + Fable delegation + the router dashboard.
export const getModelConfig = () => request<ModelConfigResponse>('/model-config')
export const putModelConfig = (warmModel: string, maxRoutableModel: string) =>
  request<ModelConfigResponse>('/model-config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ warm_model: warmModel, max_routable_model: maxRoutableModel }),
  })
export const getRouterStats = (limit = 20) => request<RouterStatsResponse>(`/router-stats?limit=${limit}`)

// v3.5: Oikonomos, the budget governor — the Thresholds panel's schema-driven config + spend rollups.
export const getGovernorConfig = () => request<GovernorConfigResponse>('/governor-config')
export const putGovernorConfig = (updates: Record<string, unknown>) =>
  request<GovernorConfigResponse>('/governor-config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ updates }),
  })

// v4: health / workout / meal panels (cockpit-spec.md "Health pipeline extension (v4)").
export const getHealthSleep = (days = 14) => request<SleepResponse>(`/health/sleep?days=${days}`)
export const getHealthWorkouts = (days = 14) => request<WorkoutsResponse>(`/health/workouts?days=${days}`)
export const getHealthNutrition = (days = 14) => request<NutritionResponse>(`/health/nutrition?days=${days}`)
export const getHealthSummary = () => request<HealthSummaryResponse>('/health/summary')
export const getMeals = () => request<MealsResponse>('/meals')

/** ws:// (or wss:// over https) same-origin URL for the chat pane's live socket. */
export function chatSocketUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/ws`
}

// v5: real OIDC auth + archon SSO tiles/proxy (cockpit-spec.md "Auth (Zitadel) & the archon SSO portal").
export const getAuthStatus = () => request<AuthStatusResponse>('/auth/status')
export const getArchons = () => request<ArchonsResponse>('/archons')

/** Opens an archon's proxied UI in a new tab — a plain navigation, not a fetch (it's an HTML page, and
 * the browser needs to actually load it, cookies and all). */
export function archonUrl(id: string): string {
  return `/archons/${encodeURIComponent(id)}/`
}
