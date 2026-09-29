// DOM-free grouping for the open-spec ledger, extracted so it can be pinned by `node --test`
// (cockpit/CLAUDE.md: testable logic lives beside its component in a module the tests can import).
//
// The one rule this file exists to hold: **nothing is dropped and nothing is invented.** The panel
// answers "what has and hasn't been completed", and a grouping that quietly loses a row makes the
// answer wrong in the direction nobody notices. So `groupByToken` is total — every document the
// server sent lands in exactly one group, including the ones with no readable status.
//
// The ORDER and the OPEN set both come from the server (`cockpit/server/doc_status.py`). The browser
// keeps no copy of either: a standing cockpit decision, and the whole point of a derived ledger.

import type { DocStatusDocument, DocStatusResponse } from '../types'

export interface DocStatusGroup {
  token: string
  /** The server's one-line gloss. Empty for the unclassified group, which explains itself. */
  gloss: string
  /** True when this token counts as outstanding work. From the server, never decided here. */
  open: boolean
  documents: DocStatusDocument[]
}

/** The bucket unclassified documents land in. Not a status token — deliberately not in `TOKENS`,
 *  so it can never be mistaken for one the vocabulary blesses. */
export const UNCLASSIFIED = 'UNCLASSIFIED'

export function groupByToken(data: DocStatusResponse): DocStatusGroup[] {
  const openSet = new Set(data.open_tokens ?? [])
  const byToken = new Map<string, DocStatusDocument[]>()
  for (const doc of data.documents ?? []) {
    const key = doc.token ?? UNCLASSIFIED
    const bucket = byToken.get(key)
    if (bucket) bucket.push(doc)
    else byToken.set(key, [doc])
  }

  const groups: DocStatusGroup[] = []
  // Server order first, so the ledger reads most-done to least...
  for (const token of data.order ?? []) {
    const documents = byToken.get(token)
    if (!documents) continue
    groups.push({ token, gloss: data.gloss?.[token] ?? '', open: openSet.has(token), documents })
    byToken.delete(token)
  }
  // ...then anything the server ordered but did not list, so a token added on the daemon's side
  // still renders instead of vanishing from a browser built against an older vocabulary.
  for (const [token, documents] of byToken) {
    if (token === UNCLASSIFIED) continue
    groups.push({ token, gloss: data.gloss?.[token] ?? '', open: openSet.has(token), documents })
  }
  // Unclassified goes LAST and always renders when present. It means CI is red or the parser could
  // not read a header, and it is the one group that must never be tidied away.
  const orphans = byToken.get(UNCLASSIFIED)
  if (orphans && orphans.length) {
    groups.push({ token: UNCLASSIFIED, gloss: '', open: true, documents: orphans })
  }
  return groups
}

/** The headline sentence. Reads off the server's own counts so it cannot disagree with the rows. */
export function summarise(data: DocStatusResponse): string {
  if (!data.available) return 'unavailable'
  const open = data.open_count ?? 0
  const total = data.total ?? 0
  const orphans = data.unclassified_count ?? 0
  const parts = [`${open} open of ${total}`]
  if (orphans) parts.push(`${orphans} with no status`)
  return parts.join(' · ')
}
