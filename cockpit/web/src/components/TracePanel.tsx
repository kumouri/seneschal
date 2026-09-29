import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { getTraceSession, getTraceSessions } from '../api'
import { formatClockTime, formatTimestamp } from '../format'
import type { TraceEvent, TraceSaid, TraceSession, TraceSessionResponse, TraceToolUse } from '../types'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'
import { browserStorage, readCollapsedFor, readNumberFor, writeCollapsedFor, writeNumberFor } from './collapseStorage'
import { clampSessionsWidth, DEFAULT_SESSIONS_WIDTH } from './traceResize'
import {
  formatDuration,
  formatTokens,
  groupIntoTurns,
  matchesQuery,
  sortSessions,
  toolPreview,
  type SessionSort,
  type TurnGroup,
} from './traceView'

// The session trace (phase 1, served by cockpit/server/trace.py) — an agent debug log for the
// assistant's sessions, after GitHub Copilot's. Read-only, like every v1 monitor panel.
//
// Five things about this panel are decisions rather than defaults:
//
// 1. IT IS NOT A TILE. It sits under the chat pane, full width, sized like it — not in the `.grid`
//    of 320px-minimum tiles with the other panels. The measurement is why: in a tile the two-pane layout below got 346px, which left the event
//    rows' content column resolving to LITERALLY ZERO pixels once the fixed ts/kind columns took
//    their 220px. A two-pane log reader over verbatim transcript text never fit a tile.
//
// 2. A TURN IS THE UNIT, NOT AN EVENT. A single turn runs to 68 transcript rows — one per step —
//    and reading those flat is what made the panel unusable even once it was wide enough. The
//    default view groups them: the header carries the exchange itself plus what the turn actually
//    cost, and the steps are one click away. This is the spec's §4 turn-grouping toggle, built.
//
// 3. THE FILTER IS NOT A NICETY. 1,056 tool calls landed in four days of real traffic, so a flat
//    unfiltered event list is unusable at real volume. Filtering by kind — and searching the
//    text — is what makes the Logs view a tool instead of a wall.
//
// 4. NARROWING OPENS THE TURNS. A filter or a search that left every turn shut would hide its own
//    results behind a second click. So any active narrowing expands the groups to what matched;
//    clearing it returns them to collapsed.
//
// 5. A `!private` TURN SHOWS AS REDACTED AND NOTHING ELSE. The backend already refuses to send text
//    or a preview for one (server/trace.py honours the tombstone in the READER, not just the
//    writer); this renders that absence honestly rather than as an empty row that looks like nothing
//    was said. A redaction that holds in one reader and not another is not a redaction. `traceView`'s
//    search is held to the same rule — it can never be the reader that reconstructs the text.
//
// 6. FOUR THINGS THAT WERE BUGS, NOT DESIGN:
//    the sessions pane is now a drag-resizable column (`traceResize.ts` has the clamping math);
//    `toolPreview` no longer cuts a tool's input to 90 characters on top of the backend's own
//    200-char cap — decision 3 above already said input renders inline, not hover-only, and the cap
//    quietly contradicted it; the sessions list can be sorted by cost/turns/errors as well as
//    recency (`traceView.sortSessions`); and a session's label is now the first thing the owner said in
//    it (`server/trace.py`'s `title`, honouring the same `!private` tombstone as everything else in
//    this panel) rather than eight characters of its id, which named nothing.

const SRC_EMOJI: Record<string, string> = { transcript: '💬', assertion: '📤', spawn: '🌱' }

/** Collapsed on first visit — a debug log should not push twenty tiles below the fold every load. */
const COLLAPSED_BY_DEFAULT = true
const COLLAPSE_STORAGE_KEY = 'seneschal.trace.collapsed'
const SESSIONS_WIDTH_STORAGE_KEY = 'seneschal.trace.sessionsWidth'

const SESSION_SORTS: { id: SessionSort; label: string }[] = [
  { id: 'recent', label: 'Newest' },
  { id: 'cost', label: 'Cost' },
  { id: 'turns', label: 'Turns' },
  { id: 'errors', label: 'Errors' },
]

/** Kinds worth filtering by, in the order they matter when you're looking for something. */
type Filter = { id: string; label: string; match: (e: TraceEvent) => boolean }

const ALL_FILTER: Filter = { id: 'all', label: 'All', match: () => true }

const FILTERS: Filter[] = [
  ALL_FILTER,
  { id: 'tools', label: 'Tool calls', match: (e) => (e.tool_uses?.length ?? 0) > 0 },
  { id: 'said', label: 'Conversation', match: (e) => (e.said?.length ?? 0) > 0 || !!e.redacted },
  { id: 'turns', label: 'Turn boundaries', match: (e) => e.kind === 'turn_started' || e.kind === 'turn_done' },
  { id: 'errors', label: 'Errors', match: (e) => !!e.is_error },
]

/** The session's label: what the owner first said in it, when the backend has one, over the id hash.
 *  `isTitle` tells the caller which it got, so the id-fallback case can stay monospace (it IS a
 *  hash) while a real title reads like the sentence it is. */
function sessionLabel(s: TraceSession): { text: string; isTitle: boolean } {
  const title = s.title?.trim()
  if (title) return { text: title, isTitle: true }
  const id = s.session_id.length > 12 ? `${s.session_id.slice(0, 8)}…` : s.session_id
  return { text: id, isTitle: false }
}

/** Why the session started the way it did — phase 0's refusal class, rendered for a human. */
function startBadge(s: TraceSession) {
  if (s.resumed === true) return <span className="trace-badge trace-badge-ok">resumed</span>
  if (!s.start_class) return null
  return (
    <span className="trace-badge" title={s.start_why ?? undefined}>
      cold · {s.start_class}
    </span>
  )
}

/** A tool call: the name, plus enough of its input to recognise it without hovering 66 times. */
function ToolChips({ tools }: { tools: TraceToolUse[] }) {
  return (
    <span className="trace-tools">
      {tools.map((t, i) => {
        const preview = toolPreview(t.input_preview)
        return (
          <span key={i} className="trace-tool">
            <code>{t.name ?? '?'}</code>
            {preview && (
              <span className="trace-tool-input" title={t.input_preview ?? undefined}>{preview}</span>
            )}
          </span>
        )
      })}
    </span>
  )
}

/** Display names for the trace's speaker roles. A lookup with a passthrough default, same as the
 *  Status panel's respawn labels: a role the backend adds later shows as its raw slug rather than
 *  vanishing. The names themselves live in persona/identity.json, which the browser never reads —
 *  roles are enough to tell the two sides of an exchange apart. */
const SPEAKER_LABELS: Record<string, string> = { assistant: 'Assistant', owner: 'Owner' }

function SaidLines({ said }: { said: TraceSaid[] }) {
  return (
    <>
      {said.map((s, i) => (
        <div key={i} className="trace-said">
          <b>{s.speaker ? (SPEAKER_LABELS[s.speaker] ?? s.speaker) : '?'}:</b>{' '}
          {s.text}
        </div>
      ))}
    </>
  )
}

function EventRow({ e }: { e: TraceEvent }) {
  const tools = e.tool_uses ?? []
  const hasDetail =
    tools.length > 0 || e.redacted || !!e.said?.length || !!e.reply_preview || (e.src === 'spawn' && !!e.why)
  return (
    <div className={`trace-event${e.is_error ? ' is-error' : ''}`}>
      {/* The full timestamp stays reachable on hover — the column shows time-of-day only because
          every row in a session detail shares its date. */}
      <span className="trace-event-ts" title={e.ts ? formatTimestamp(e.ts) : undefined}>
        {formatClockTime(e.ts)}
      </span>
      <span className="trace-event-src" title={e.src}>{SRC_EMOJI[e.src] ?? '•'}</span>
      <span className="trace-event-kind" title={e.kind ?? undefined}>{e.kind ?? ''}</span>
      <span className="trace-event-detail">
        {tools.length > 0 && <ToolChips tools={tools} />}
        {e.redacted && <em className="trace-redacted">redacted (!private) — no text kept</em>}
        {!e.redacted && e.said?.length ? <SaidLines said={e.said} /> : null}
        {!e.redacted && !e.said?.length && e.reply_preview && (
          <span className="trace-preview">{e.reply_preview}</span>
        )}
        {e.src === 'spawn' && e.why && <span className="trace-preview">{e.why}</span>}
        {/* A row with nothing in the detail column is a real thing (a bare turn boundary). Render
            the em-dash so the grid still has a baseline to align to and the row can't look broken. */}
        {!hasDetail && <span className="trace-preview">—</span>}
      </span>
    </div>
  )
}

/** The turn's roll-up: what it cost, in the order you'd ask. Each figure renders only when the
 *  backend actually sent it — an absent duration shows nothing rather than a confident zero. */
function TurnStats({ g }: { g: TurnGroup }) {
  const bits: ReactNode[] = []
  const dur = formatDuration(g.durationMs)
  if (dur) bits.push(<span key="d">{dur}</span>)
  if (g.costUsd != null) bits.push(<span key="c">${g.costUsd.toFixed(2)}</span>)
  if (g.toolCalls > 0) bits.push(<span key="t">{g.toolCalls} tool{g.toolCalls === 1 ? '' : 's'}</span>)
  const tok = formatTokens(g.tokens)
  if (tok) {
    // TITLED, because the bare number invites a wrong reading. This is every token the turn
    // PROCESSED — and a 27-step turn re-reads its cached context on each step, so it runs to
    // millions while the context itself never left ~280k. The sessions list's "peak" right next to
    // it is that other, smaller number, and two unlabelled token figures that differ by 6x would
    // just look like one of them is broken.
    bits.push(
      <span key="k" title="Every token this turn processed — prompt + cache write + cache read + output. A multi-step turn re-reads its cached context each step, so this runs far above the session's context peak.">
        {tok} tok
      </span>,
    )
  }
  if (g.errors > 0) bits.push(<span key="e" className="trace-errors">{g.errors} err</span>)
  return (
    <span className="trace-turn-stats">
      {bits.map((b, i) => <span key={i}>{i > 0 && ' · '}{b}</span>)}
    </span>
  )
}

/** One turn: a header carrying the exchange and its cost, over its steps. The header is the whole
 *  point — collapsed, you are reading the CONVERSATION; expanded, the mechanics that produced it. */
function TurnBlock({
  group,
  visible,
  open,
  onToggle,
}: {
  group: TurnGroup
  visible: TraceEvent[]
  open: boolean
  onToggle: () => void
}) {
  // A turn-less event (a spawn decision, an assertion) has no exchange and no roll-up to show, so
  // it renders as the bare row it is rather than being dressed up as a turn.
  if (!group.turnId) {
    return <>{visible.map((e, i) => <EventRow key={i} e={e} />)}</>
  }
  const hidden = group.events.length - visible.length
  return (
    <div className={`trace-turn${open ? ' is-open' : ''}${group.errors > 0 ? ' has-error' : ''}`}>
      <button type="button" className="trace-turn-head" aria-expanded={open} onClick={onToggle}>
        <span className="trace-turn-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="trace-event-ts" title={group.startedAt ? formatTimestamp(group.startedAt) : undefined}>
          {formatClockTime(group.startedAt)}
        </span>
        <TurnStats g={group} />
        <span className="trace-turn-steps">
          {group.events.length} event{group.events.length === 1 ? '' : 's'}
        </span>
      </button>
      <div className="trace-turn-said">
        {group.redacted
          ? <em className="trace-redacted">redacted (!private) — no text kept</em>
          : group.said.length > 0
            ? <SaidLines said={group.said} />
            : <span className="trace-preview">— no text recorded for this turn</span>}
      </div>
      {open && (
        <div className="trace-turn-events">
          {visible.map((e, i) => <EventRow key={i} e={e} />)}
          {hidden > 0 && (
            <p className="muted trace-turn-hidden">{hidden} more event{hidden === 1 ? '' : 's'} in this turn hidden by the filter.</p>
          )}
        </div>
      )}
    </div>
  )
}

export function TracePanel() {
  const [collapsed, setCollapsed] = useState(() =>
    readCollapsedFor(browserStorage(), COLLAPSE_STORAGE_KEY, COLLAPSED_BY_DEFAULT),
  )
  const result = usePolling(() => getTraceSessions(40), 30_000)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<TraceSessionResponse | null>(null)
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')
  const [grouped, setGrouped] = useState(true)
  const [openTurns, setOpenTurns] = useState<Set<string>>(() => new Set())
  const [sort, setSort] = useState<SessionSort>('recent')
  const [sessionsWidth, setSessionsWidthState] = useState(() =>
    clampSessionsWidth(readNumberFor(browserStorage(), SESSIONS_WIDTH_STORAGE_KEY, DEFAULT_SESSIONS_WIDTH)),
  )
  const traceRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!selected) { setDetail(null); return }
    let live = true
    getTraceSession(selected)
      .then((r) => { if (live) setDetail(r.ok ? r.data : null) })
      .catch(() => { if (live) setDetail(null) })
    return () => { live = false }
  }, [selected])

  // A new session is a new log — carrying the previous one's expanded turns over would open
  // arbitrary rows in a session the reader has not looked at yet.
  useEffect(() => { setOpenTurns(new Set()) }, [selected])

  function toggle() {
    const next = !collapsed
    setCollapsed(next)
    writeCollapsedFor(browserStorage(), COLLAPSE_STORAGE_KEY, next)
  }

  function toggleTurn(key: string) {
    setOpenTurns((prev) => {
      const next = new Set(prev)
      if (!next.delete(key)) next.add(key)
      return next
    })
  }

  // Persists on every commit, not only at drag-end — a resize abandoned by, say, a tab switch mid-
  // drag still keeps the width it reached rather than snapping back to whatever loaded last.
  const commitSessionsWidth = useCallback((px: number) => {
    const clamped = clampSessionsWidth(px, traceRef.current?.clientWidth ?? null)
    setSessionsWidthState(clamped)
    writeNumberFor(browserStorage(), SESSIONS_WIDTH_STORAGE_KEY, clamped)
  }, [])

  // Plain pointer events, not a drag library — this is one axis, one element, and the whole
  // interaction is "clamp a number", which `traceResize.ts` already owns and pins with tests.
  function startResize(e: React.PointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return
    e.preventDefault()
    const startX = e.clientX
    const startWidth = sessionsWidth
    const onMove = (ev: PointerEvent) => {
      setSessionsWidthState(clampSessionsWidth(startWidth + (ev.clientX - startX), traceRef.current?.clientWidth ?? null))
    }
    const onUp = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      commitSessionsWidth(startWidth + (ev.clientX - startX))
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  // Every return below goes through this, so the panel is collapsible in EVERY state — including
  // loading, auth-gated and empty. A section that only collapses once its data arrives is a section
  // that jumps under the cursor.
  const shell = (right: ReactNode, className: string, body: ReactNode) => (
    <Panel
      title="Trace"
      right={right}
      className={`trace-panel ${className}`}
      collapsed={collapsed}
      onToggleCollapsed={toggle}
    >
      {body}
    </Panel>
  )

  if (!result) return shell(null, 'is-message', 'Loading…')
  if (!result.ok) return shell(null, 'is-message', <AuthGate error={result.error} />)

  const data = result.data
  // "No metrics log at all" and "nothing has run" are different facts, and a trace exists to stop
  // silence being ambiguous — so they get different empty states, same as JobsPanel.
  if (!data.available) {
    return shell(
      null,
      'is-message',
      <p className="muted">No session history yet ({data.reason ?? 'nothing recorded'}).</p>,
    )
  }
  if (data.sessions.length === 0) {
    return shell(null, 'is-message', <p className="muted">No sessions recorded yet.</p>)
  }

  const events = detail?.events ?? []
  const active = FILTERS.find((f) => f.id === filter) ?? ALL_FILTER
  const matches = (e: TraceEvent) => active.match(e) && matchesQuery(e, query)
  const shown = events.filter(matches)
  // Decision 4: a filter or a search that left every turn shut would hide its own results.
  const narrowing = filter !== 'all' || query.trim() !== ''
  // Grouping runs over ALL the turn's events so the header's roll-up stays true; only the BODY is
  // filtered. A turn that cost $12.61 still says so while you are looking at one of its tool calls.
  const groups = groupIntoTurns(events)
    .map((g) => ({ group: g, visible: g.events.filter(matches) }))
    .filter(({ visible }) => visible.length > 0)
  const count = data.total ?? data.sessions.length
  const sortedSessions = sortSessions(data.sessions, sort)

  return shell(
    <span className="muted">{count} session{count === 1 ? '' : 's'}</span>,
    '',
    <div
      className="trace"
      ref={traceRef}
      style={{ '--trace-sessions-width': `${sessionsWidth}px` } as CSSProperties}
    >
      <div className="trace-sessions-pane">
        <div className="trace-sessions-head">
          <label className="trace-sort">
            Sort
            <select
              aria-label="Sort sessions by"
              value={sort}
              onChange={(e) => setSort(e.target.value as SessionSort)}
            >
              {SESSION_SORTS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </label>
        </div>
        <div className="trace-sessions">
          {sortedSessions.map((s) => {
            const label = sessionLabel(s)
            return (
              <button
                key={s.session_id}
                type="button"
                className={`trace-session${s.session_id === selected ? ' is-selected' : ''}`}
                aria-pressed={s.session_id === selected}
                onClick={() => setSelected(s.session_id === selected ? null : s.session_id)}
              >
                <span className={`trace-session-id${label.isTitle ? ' is-title' : ''}`} title={s.session_id}>
                  {label.text}
                </span>
                <span className="trace-session-when">{formatTimestamp(s.first_at)}</span>
                <span className="trace-session-meta">
                  {s.turns} turn{s.turns === 1 ? '' : 's'} · ${s.cost_usd.toFixed(2)} ·{' '}
                  {(s.context_peak / 1000).toFixed(0)}k peak
                  {s.errors > 0 && <span className="trace-errors"> · {s.errors} err</span>}
                </span>
                {startBadge(s)}
              </button>
            )
          })}
        </div>
      </div>
      <div
        className="trace-resize-handle"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the sessions list"
        onPointerDown={startResize}
      />
      <div className="trace-detail">
        {!selected ? (
          <p className="muted trace-hint">Pick a session on the left to read its event log.</p>
        ) : (
          <>
            <div className="trace-controls">
              <div className="trace-filters">
                {FILTERS.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    className={`trace-filter${f.id === filter ? ' is-active' : ''}`}
                    aria-pressed={f.id === filter}
                    onClick={() => setFilter(f.id)}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
              <div className="trace-controls-right">
                <input
                  type="search"
                  className="trace-search"
                  placeholder="Search this session…"
                  aria-label="Search this session's events"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
                <button
                  type="button"
                  className={`trace-filter${grouped ? ' is-active' : ''}`}
                  aria-pressed={grouped}
                  onClick={() => setGrouped(!grouped)}
                  title="Group events into the turns they belong to"
                >
                  Turns
                </button>
                <span className="muted trace-count">
                  {shown.length} of {events.length}
                  {detail?.truncated ? ' (truncated)' : ''}
                </span>
              </div>
            </div>
            <div className="trace-events">
              {shown.length === 0 ? (
                <p className="muted">No events match{query.trim() ? ` “${query.trim()}”` : ' this filter'}.</p>
              ) : grouped ? (
                groups.map(({ group, visible }) => (
                  <TurnBlock
                    key={group.key}
                    group={group}
                    visible={visible}
                    open={narrowing || openTurns.has(group.key)}
                    onToggle={() => toggleTurn(group.key)}
                  />
                ))
              ) : (
                shown.map((e, i) => <EventRow key={i} e={e} />)
              )}
            </div>
          </>
        )}
      </div>
    </div>,
  )
}
