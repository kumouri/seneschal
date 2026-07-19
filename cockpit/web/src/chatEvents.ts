// Folds the flat chat.event stream (transcript backfill AND the live /api/ws socket alike) into
// per-turn groups keyed by `turn_id` — the one correlator the wire protocol carries (see
// seneschal/scripts/presence.py's `_make_stream_tee`). Shared, pure logic so backfill-on-load and the
// live socket can't drift into two different groupings.
import type { ChatEventFrame } from './types'

export interface ChatTurn {
  turnId: string
  source: string
  model: string | null
  startedAt: string
  textPreview: string | null
  blocks: ChatEventFrame[]
  done: boolean
  isError: boolean
  replyPreview: string | null
  doneAt: string | null
}

const FALLBACK_TURN_ID = '__no-turn-id__'

function turnIdOf(ev: ChatEventFrame): string {
  return ev.turn_id ?? FALLBACK_TURN_ID
}

function newTurn(ev: ChatEventFrame): ChatTurn {
  return {
    turnId: turnIdOf(ev),
    source: ev.source ?? 'unknown',
    model: ev.model ?? null,
    startedAt: ev.ts,
    textPreview: ev.text_preview ?? null,
    blocks: [],
    done: false,
    isError: false,
    replyPreview: null,
    doneAt: null,
  }
}

/** The turn this event belongs to: an exact turn_id match (searched newest-first, since a turn_id is
 * only ever reused by coincidence never in practice), or — for an event with no turn_id (older
 * ring-buffer entries from before the correlator existed) — whatever turn is most recent, since chat
 * turns are strictly serialized daemon-side. Returns -1 only when there is no turn to attach to yet. */
function findTurnIndex(turns: ChatTurn[], key: string): number {
  if (key !== FALLBACK_TURN_ID) {
    for (let i = turns.length - 1; i >= 0; i--) {
      if (turns[i]?.turnId === key) return i
    }
  }
  return turns.length - 1
}

/** Apply one chat.event to an existing turn list, returning a NEW array (React-state-friendly). */
export function applyChatEvent(turns: ChatTurn[], ev: ChatEventFrame): ChatTurn[] {
  if (ev.kind === 'turn_started') {
    return [...turns, newTurn(ev)]
  }
  const idx = findTurnIndex(turns, turnIdOf(ev))
  const turn = turns[idx]
  if (idx === -1 || !turn) return turns
  const next = turns.slice()
  if (ev.kind === 'turn_done') {
    next[idx] = {
      ...turn,
      done: true,
      isError: Boolean(ev.is_error),
      replyPreview: ev.reply_preview ?? null,
      doneAt: ev.ts,
      model: turn.model ?? ev.model ?? null,
    }
  } else {
    next[idx] = { ...turn, blocks: [...turn.blocks, ev] }
  }
  return next
}

/** Bulk fold for the transcript backfill (oldest-first order, matching GET /api/transcript). */
export function foldEventsIntoTurns(events: ChatEventFrame[]): ChatTurn[] {
  let turns: ChatTurn[] = []
  for (const ev of events) turns = applyChatEvent(turns, ev)
  return turns
}

/** The plain text of a turn's assistant output — joined blocks, falling back to the turn_done reply
 * preview (covers the common case of exactly one assistant/result pair) or an in-progress placeholder. */
export function turnText(turn: ChatTurn): string {
  const fromBlocks = turn.blocks
    .map((b) => b.text)
    .filter((t): t is string => Boolean(t))
    .join('\n\n')
  if (fromBlocks) return fromBlocks
  if (turn.replyPreview) return turn.replyPreview
  return turn.done ? '(no output)' : '…'
}

export function turnToolUses(turn: ChatTurn) {
  return turn.blocks.flatMap((b) => b.tool_uses ?? [])
}
