// Tolerant persistence for a panel's collapsed/expanded choice.
//
// Extracted from `jobsCollapse.ts` when the Trace panel became the second collapsible section. Only
// the "did the reader leave it open?" half lives here. The *collapse rule* for Jobs — which
// jobs may be hidden at all — deliberately stays in `jobsCollapse.ts`, because that is a safety
// invariant about undelivered completion pushes, not a storage concern.
//
// Everything here is tolerant in the same direction as the backend readers (cockpit/CLAUDE.md):
// storage that is absent, that throws, or that holds a value written by a future version all read as
// "no preference recorded" and fall back to the caller's default. A panel must never fail to render
// because a browser refused a `localStorage` read.

/** The slice of `Storage` these helpers use, so the rules can be tested without a DOM. */
export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/** `localStorage` is absent under a non-browser runtime and *throws on access* in a hardened profile
 *  or Safari private mode, so it is only ever reached behind a guard. */
export function browserStorage(): StorageLike | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

/** Tolerant read: only the two values we write mean anything. Absent, corrupt, or written by a
 *  future version all read as "no preference recorded" and fall back to `fallback`. */
export function readCollapsedFor(
  storage: StorageLike | null | undefined,
  key: string,
  fallback: boolean,
): boolean {
  try {
    const raw = storage?.getItem(key)
    if (raw === '1') return true
    if (raw === '0') return false
  } catch {
    // Storage that throws on read is storage we don't have. Fall through to the default.
  }
  return fallback
}

/** A preference we can't persist is a preference that doesn't survive reload — never a crash. */
export function writeCollapsedFor(
  storage: StorageLike | null | undefined,
  key: string,
  collapsed: boolean,
): void {
  try {
    storage?.setItem(key, collapsed ? '1' : '0')
  } catch {
    // Quota exceeded / storage disabled mid-session. The toggle still works for this page load.
  }
}

/** The numeric sibling of the two functions above — added for the Trace panel's resizable sessions
 *  pane width, which is a persisted number rather than a persisted boolean. Same tolerance rule:
 *  absent, corrupt, or non-finite all read as "no preference recorded". */
export function readNumberFor(
  storage: StorageLike | null | undefined,
  key: string,
  fallback: number,
): number {
  try {
    const raw = storage?.getItem(key)
    if (raw != null) {
      const n = Number(raw)
      if (Number.isFinite(n)) return n
    }
  } catch {
    // Storage that throws on read is storage we don't have. Fall through to the default.
  }
  return fallback
}

export function writeNumberFor(
  storage: StorageLike | null | undefined,
  key: string,
  value: number,
): void {
  try {
    storage?.setItem(key, String(value))
  } catch {
    // Quota exceeded / storage disabled mid-session. The value still works for this page load.
  }
}
