// The chat pane's live connection: GET /api/ws, fed by the cockpit backend's fan-out (which itself
// relays the daemon pipe — see cockpit/server/app.py + seneschal/docs/cockpit-spec.md). Backfills via
// GET /api/transcript on mount, then applies live chat.event/status/chat.ack frames as they arrive.
// Reconnects with backoff on drop — a browser tab losing the WS is normal (sleep, network blip) and
// must never require a manual refresh.
import { useCallback, useEffect, useRef, useState } from 'react'
import { chatSocketUrl, getTranscript } from './api'
import { applyChatEvent, foldEventsIntoTurns, type ChatTurn } from './chatEvents'
import type { ChatAckFrame, ChatStatusFrame, WsFrame } from './types'

const MAX_TURNS = 300
const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 15000

export interface ChatAck {
  id: string | number | null
  via?: 'fallback'
}

export interface ChatSocket {
  turns: ChatTurn[]
  status: ChatStatusFrame | null
  wsConnected: boolean
  pipeDown: boolean
  lastAck: ChatAck | null
  /** Returns false (nothing sent — the socket itself is down) without throwing; the caller decides
   * how to surface that. A true return means the frame reached the backend, which itself falls back
   * to the inbox file when the daemon pipe is down (never lossy either way). */
  sendChat: (id: string, text: string, forceFable: boolean) => boolean
  requestRestart: () => boolean
}

export function useChatSocket(): ChatSocket {
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [status, setStatus] = useState<ChatStatusFrame | null>(null)
  const [wsConnected, setWsConnected] = useState(false)
  const [lastAck, setLastAck] = useState<ChatAck | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const backoffRef = useRef(RECONNECT_BASE_MS)
  const unmountedRef = useRef(false)
  const backfilledRef = useRef(false)

  useEffect(() => {
    unmountedRef.current = false

    void getTranscript(200).then((res) => {
      if (res.ok && !unmountedRef.current && !backfilledRef.current) {
        backfilledRef.current = true
        setTurns((prev) => (prev.length === 0 ? foldEventsIntoTurns(res.data.events).slice(-MAX_TURNS) : prev))
      }
    })

    let reconnectTimer: ReturnType<typeof setTimeout> | undefined

    function connect() {
      if (unmountedRef.current) return
      const ws = new WebSocket(chatSocketUrl())
      wsRef.current = ws

      ws.onopen = () => {
        backoffRef.current = RECONNECT_BASE_MS
        setWsConnected(true)
      }
      ws.onclose = () => {
        setWsConnected(false)
        if (unmountedRef.current) return
        const delay = backoffRef.current
        backoffRef.current = Math.min(delay * 2, RECONNECT_MAX_MS)
        reconnectTimer = setTimeout(connect, delay)
      }
      ws.onerror = () => {
        ws.close()
      }
      ws.onmessage = (event) => {
        let frame: WsFrame
        try {
          frame = JSON.parse(String(event.data)) as WsFrame
        } catch {
          return
        }
        if (frame.type === 'chat.event') {
          setTurns((prev) => applyChatEvent(prev, frame).slice(-MAX_TURNS))
        } else if (frame.type === 'status') {
          setStatus(frame)
        } else if (frame.type === 'chat.ack') {
          const ack = frame as ChatAckFrame
          setLastAck({ id: ack.id ?? null, via: ack.via })
        }
      }
    }
    connect()

    return () => {
      unmountedRef.current = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      wsRef.current?.close()
    }
  }, [])

  const sendChat = useCallback((id: string, text: string, forceFable: boolean): boolean => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    ws.send(JSON.stringify({ type: 'chat.send', id, text, force_fable: forceFable }))
    return true
  }, [])

  const requestRestart = useCallback((): boolean => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    ws.send(JSON.stringify({ type: 'control.restart' }))
    return true
  }, [])

  return {
    turns,
    status,
    wsConnected,
    pipeDown: status?.pipe === 'down',
    lastAck,
    sendChat,
    requestRestart,
  }
}
