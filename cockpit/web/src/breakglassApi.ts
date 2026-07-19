// The break-glass supervisor (cockpit/breakglass/supervisor.py) runs as a SEPARATE stdlib-only
// process on its own port (default 127.0.0.1:8499) — never proxied through this app's /api/*. That
// separation is the whole point of break-glass: it has to keep working even when the cockpit backend
// itself is broken. The frontend therefore calls it DIRECTLY, cross-origin; the supervisor sends
// permissive CORS headers for exactly this (see supervisor.py's module docstring) — the ladder itself
// (fresh Zitadel re-auth + TOTP + a Telegram-delivered one-time phrase) is the real gate here, not an
// origin check, and the process only ever binds 127.0.0.1 in the first place.
const SUPERVISOR_BASE = 'http://127.0.0.1:8499'

export type BreakglassResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string }

async function post<T>(path: string, body: unknown): Promise<BreakglassResult<T>> {
  try {
    const res = await fetch(`${SUPERVISOR_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    })
    let data: Record<string, unknown> = {}
    try {
      data = (await res.json()) as Record<string, unknown>
    } catch {
      // non-JSON body — fall through to the statusText-based error below
    }
    if (!res.ok) {
      const detail = typeof data.error === 'string' ? data.error : res.statusText || `HTTP ${res.status}`
      return { ok: false, status: res.status, error: detail }
    }
    return { ok: true, data: data as T }
  } catch (err) {
    return {
      ok: false,
      status: 0,
      error: err instanceof Error ? err.message : 'could not reach the break-glass supervisor',
    }
  }
}

export interface BreakglassStartResponse {
  ok: boolean
  attempt_id: string
  expires_in: number
}

export interface BreakglassVerifyResponse {
  ok: boolean
  action: string
  steps: string[]
}

/** Rung 3, step 1: hands the assertion (minted by rungs 1-2's fresh Zitadel re-auth) to the
 * supervisor, which sends a one-time phrase to the owner over Telegram. */
export const breakglassStart = (assertion: string, action: string) =>
  post<BreakglassStartResponse>('/start', { assertion, action })

/** Rung 3, step 2: the phrase typed back. On a match, the supervisor executes the action and returns
 * the exact step-by-step command sequence it ran. */
export const breakglassVerify = (attemptId: string, phrase: string) =>
  post<BreakglassVerifyResponse>('/verify', { attempt_id: attemptId, phrase })

/** Full-page navigation (NOT a fetch) that starts the break-glass ladder's rungs 1-2 — a fresh
 * Zitadel re-auth round trip through the cockpit backend. Zitadel's login page can't be reached via
 * fetch/XHR (it's a real cross-site redirect chain), so this must be a genuine browser navigation. */
export function startBreakglassReauth(action: string): void {
  window.location.href = `/api/breakglass/reauth/start?action=${encodeURIComponent(action)}`
}
