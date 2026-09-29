import { useEffect, useRef, useState } from 'react'
import { getEmotes } from '../api'
import type { ChatTurn } from '../chatEvents'
import { turnText, turnToolUses } from '../chatEvents'
import { renderWithEmotes } from '../emotes'
import { formatTimestamp } from '../format'
import { useChatSocket } from '../useChatSocket'
import { ChatComposer } from './ChatComposer'

const SOURCE_LABEL: Record<string, string> = {
  telegram: 'Telegram',
  discord: 'Discord',
  cockpit: 'Cockpit',
}

function ToolUses({ turn }: { turn: ChatTurn }) {
  const toolUses = turnToolUses(turn)
  const [open, setOpen] = useState(false)
  if (toolUses.length === 0) return null
  return (
    <details className="chat-tool-uses" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        {toolUses.length} tool call{toolUses.length === 1 ? '' : 's'}
      </summary>
      <ul>
        {toolUses.map((t, i) => (
          <li key={i} className="mono">
            {t.name ?? 'unknown'}
            {t.input_preview ? `: ${t.input_preview}` : ''}
          </li>
        ))}
      </ul>
    </details>
  )
}

function TurnBubble({ turn, emotes }: { turn: ChatTurn; emotes: Map<string, string> }) {
  const source = turn.source in SOURCE_LABEL ? turn.source : 'unknown'
  return (
    <div className={`chat-turn chat-turn-${source}`}>
      <div className="chat-turn-meta">
        <span className={`pill pill-source-${source}`}>{SOURCE_LABEL[turn.source] ?? turn.source}</span>
        {turn.model && <span className="pill pill-model">{turn.model}</span>}
        <span className="row-sub">{formatTimestamp(turn.startedAt)}</span>
        {!turn.done && <span className="pill pill-live">working…</span>}
        {turn.done && turn.isError && <span className="pill pill-danger">error</span>}
      </div>
      {turn.textPreview && <p className="chat-turn-prompt">{renderWithEmotes(turn.textPreview, emotes)}</p>}
      <p className="chat-turn-text">{renderWithEmotes(turnText(turn), emotes)}</p>
      <ToolUses turn={turn} />
    </div>
  )
}

// How close to the bottom (px) still counts as "at the bottom" — loose enough that a trackpad
// wobble or a scrollbar drag doesn't disengage auto-follow, tight enough that scrolling up even one
// turn does.
const AUTO_SCROLL_THRESHOLD_PX = 48

export function ChatPanel() {
  const { turns, status, wsConnected, pipeDown, lastAck, sendChat } = useChatSocket()
  const [emotes, setEmotes] = useState<Map<string, string>>(new Map())
  const scrollRef = useRef<HTMLDivElement>(null)
  // Defaults to true so the very first backfill (newest message = the bottom) lands there too.
  const stickToBottomRef = useRef(true)

  useEffect(() => {
    let cancelled = false
    void getEmotes().then((res) => {
      if (!cancelled && res.ok) {
        setEmotes(new Map(res.data.emotes.map((e) => [e.shortcode, e.file])))
      }
    })
    return () => {
      cancelled = true
    }
  }, [])

  // `turns` gets a new array reference on every chat.event — including mid-stream token deltas, not
  // just new turns (see applyChatEvent) — so this fires continuously while a reply is streaming in.
  // Only follow it to the bottom if that's where the reader already was; otherwise a streaming
  // answer would yank the viewport out from under someone scrolled up reading history.
  useEffect(() => {
    const el = scrollRef.current
    if (el && stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [turns])

  function handleScroll() {
    const el = scrollRef.current
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    stickToBottomRef.current = distanceFromBottom < AUTO_SCROLL_THRESHOLD_PX
  }

  return (
    <section className="panel chat-panel">
      <div className="panel-title">
        <span>
          <span className="accent-dot" aria-hidden="true" /> Chat
        </span>
        <span className={`pill ${status?.turn_in_flight ? 'pill-live' : 'pill-idle'}`}>
          {status?.turn_in_flight ? 'Assistant is working…' : 'idle'}
        </span>
      </div>
      <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
        {turns.length === 0 && <p className="empty-state">No turns yet — say something below.</p>}
        {turns.map((t) => (
          <TurnBubble key={`${t.turnId}-${t.startedAt}`} turn={t} emotes={emotes} />
        ))}
      </div>
      <ChatComposer wsConnected={wsConnected} pipeDown={pipeDown} lastAck={lastAck} onSend={sendChat} />
    </section>
  )
}
