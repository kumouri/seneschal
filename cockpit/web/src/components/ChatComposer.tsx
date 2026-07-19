import { useEffect, useRef, useState } from 'react'
import type { ChatAck } from '../useChatSocket'

type SendState =
  | { kind: 'idle' }
  | { kind: 'pending'; id: string }
  | { kind: 'acked'; via?: 'fallback' }
  | { kind: 'error'; message: string }

function newId(): string {
  // crypto.randomUUID() is available in every browser this is realistically used from; a plain
  // fallback keeps this from throwing in an older/embedded webview just in case.
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `c${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export function ChatComposer({
  wsConnected,
  pipeDown,
  lastAck,
  onSend,
}: {
  wsConnected: boolean
  pipeDown: boolean
  lastAck: ChatAck | null
  onSend: (id: string, text: string, forceFable: boolean) => boolean
}) {
  const [text, setText] = useState('')
  const [forceFable, setForceFable] = useState(false)
  const [state, setState] = useState<SendState>({ kind: 'idle' })
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    if (state.kind === 'pending' && lastAck && String(lastAck.id) === state.id) {
      setState({ kind: 'acked', via: lastAck.via })
    }
  }, [lastAck, state])

  function send() {
    const trimmed = text.trim()
    if (!trimmed || !wsConnected) return
    const id = newId()
    const ok = onSend(id, trimmed, forceFable)
    if (ok) {
      setText('')
      setState({ kind: 'pending', id })
      textareaRef.current?.focus()
    } else {
      setState({ kind: 'error', message: 'not connected — try again in a moment' })
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="chat-composer">
      {!wsConnected && (
        <p className="pill pill-danger chat-banner">
          Not connected to the cockpit backend — reconnecting…
        </p>
      )}
      {wsConnected && pipeDown && (
        <p className="pill pill-warn chat-banner">
          Daemon pipe is down — messages queue via the file fallback (a few seconds slower, never lost).
        </p>
      )}
      <div className="chat-composer-row">
        <textarea
          ref={textareaRef}
          className="chat-input"
          placeholder={wsConnected ? 'Message the assistant… (Enter to send, Shift+Enter for a new line)' : 'Reconnecting…'}
          value={text}
          disabled={!wsConnected}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
        />
        <button
          type="button"
          className="restart-btn chat-send-btn"
          disabled={!wsConnected || !text.trim()}
          onClick={send}
        >
          Send
        </button>
      </div>
      <div className="chat-composer-footer">
        <label
          className="chat-fable-toggle"
          title="Force-routes this message to Fable (bypasses the router classifier, never the approval gate). The max-routable-model ceiling still binds — fable_delegate.py refuses if it isn't Fable-tier."
        >
          <input type="checkbox" checked={forceFable} onChange={(e) => setForceFable(e.target.checked)} />
          Send to Fable <span className="row-sub">(force-route — needs the ceiling raised in Model dials)</span>
        </label>
        {state.kind === 'pending' && <span className="row-sub">sending…</span>}
        {state.kind === 'acked' && (
          <span className="row-sub">{state.via === 'fallback' ? 'queued (pipe down)' : 'sent'}</span>
        )}
        {state.kind === 'error' && <span className="error-state">{state.message}</span>}
      </div>
    </div>
  )
}
