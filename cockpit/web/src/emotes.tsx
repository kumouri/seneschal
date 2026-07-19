// Minimal `:shortcode:` emote rendering (cockpit-spec.md "Chat pane extra: custom emoji"). Off
// entirely (plain text, no lookups) when no emote pack is configured — GET /api/emotes returns an
// empty list in that case, so `emotes.size === 0` is the fast, correct no-op path.
import type { ReactNode } from 'react'
import { emoteUrl } from './api'

const SHORTCODE_RE = /:([a-z0-9_+-]+):/gi

export function renderWithEmotes(text: string, emotes: Map<string, string>): ReactNode[] {
  if (!text || emotes.size === 0) return [text]
  const parts: ReactNode[] = []
  let lastIndex = 0
  let key = 0
  SHORTCODE_RE.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = SHORTCODE_RE.exec(text)) !== null) {
    const code = match[1]?.toLowerCase()
    const file = code ? emotes.get(code) : undefined
    if (!file) continue
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index))
    parts.push(
      <img
        key={`emote-${key++}`}
        className="chat-emote"
        src={emoteUrl(file)}
        alt={`:${code}:`}
        title={`:${code}:`}
      />,
    )
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex))
  return parts.length > 0 ? parts : [text]
}
