// Pure, DOM-free clamping for the Trace panel's resizable sessions pane
// (the session trace, cockpit/server/trace.py).
//
// The panel used to have no resize handle at all — the sessions column was a fixed
// `minmax(200px, 260px)` and the only way to read a long tool call or session title was to hover for
// the native tooltip. This is the math half of the fix; `TracePanel.tsx` owns the pointer-drag
// wiring, which has no DOM-free logic worth pinning here.

export const MIN_SESSIONS_WIDTH = 180
export const MAX_SESSIONS_WIDTH = 480
export const DEFAULT_SESSIONS_WIDTH = 260

/** Keeps a dragged (or persisted) width sane regardless of what produced it — never past the pane's
 *  own bounds, and never so wide it starves the detail pane of room to be useful. `containerWidth`
 *  is optional because it is only known once the panel has actually mounted; a first render before
 *  that measurement still needs a safe width to start from. */
export function clampSessionsWidth(width: unknown, containerWidth?: number | null): number {
  const w = typeof width === 'number' && Number.isFinite(width) ? width : DEFAULT_SESSIONS_WIDTH
  let max = MAX_SESSIONS_WIDTH
  if (typeof containerWidth === 'number' && Number.isFinite(containerWidth) && containerWidth > 0) {
    // Leave the handle and a usable detail pane room — the drag must never be able to swallow the
    // whole box, however far the pointer travels past the edge.
    max = Math.min(max, containerWidth - 120)
  }
  if (max < MIN_SESSIONS_WIDTH) return MIN_SESSIONS_WIDTH
  return Math.min(max, Math.max(MIN_SESSIONS_WIDTH, w))
}
