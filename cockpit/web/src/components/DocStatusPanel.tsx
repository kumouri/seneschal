import { useState } from 'react'
import { getDocStatus } from '../api'
import { usePolling } from '../usePolling'
import { browserStorage, readCollapsedFor, writeCollapsedFor } from './collapseStorage'
import { UNCLASSIFIED, groupByToken, summarise } from './docStatusView'
import { AuthGate, Panel } from './Panel'

// The OPEN-SPEC LEDGER: an inventory of every spec in the design record (`seneschal/docs/`) — what
// HAS and HASN'T been completed — so finished specs read as closed and open ones stay visible.
//
// EVERYTHING HERE IS DERIVED AND NOTHING IS STORED. Each row is computed by
// `cockpit/server/doc_status.py` from that document's own `**Status:**` header, using the same parser
// CI enforces (`seneschal/scripts/check_doc_status.py`). The thing this replaces failed in a specific
// way — a status vocabulary already existed in `seneschal/docs/CLAUDE.md` and almost nothing followed
// it, a dozen spellings across dozens of documents — and a hand-maintained ledger would fail the same way, more
// quietly, because nobody diffs a dashboard. So there is no list in this file, no vocabulary in this
// file and no ordering in this file: the order, the gloss and the definition of "open" all arrive
// from the server (a standing cockpit decision: the browser keeps no copy).
//
// READ-ONLY, like every v1 monitor panel — and here that is more than a house style. A status is
// changed by editing the document that declares it, in the change that changes the thing. A control that
// edited one from a dashboard would recreate exactly the drift this exists to end.
//
// The unclassified group is the one that may never be tidied away: it means CI is red or a header is
// unreadable. `docStatusView.ts` guarantees it renders last and always, and `docStatusView.test.ts`
// pins that, because a comment is not a guarantee.

const COLLAPSE_KEY = 'seneschal.doc-status.collapsed'
// Collapsed by default. It is a long reference list, not a live signal — it should be there when
// the reader goes looking and out of the way when they aren't.
const COLLAPSED_BY_DEFAULT = true

function tokenClass(token: string, open: boolean): string {
  if (token === UNCLASSIFIED) return 'doc-token doc-token-bad'
  return open ? 'doc-token doc-token-open' : 'doc-token doc-token-done'
}

export function DocStatusPanel() {
  // Once a minute, not every five seconds: this reads the working tree, and the design record does
  // not change between page loads. Polling it at chat speed would spend I/O on nothing.
  const result = usePolling(getDocStatus, 60_000)
  const [collapsed, setCollapsed] = useState(() =>
    readCollapsedFor(browserStorage(), COLLAPSE_KEY, COLLAPSED_BY_DEFAULT),
  )

  function toggleCollapsed() {
    const next = !collapsed
    setCollapsed(next)
    writeCollapsedFor(browserStorage(), COLLAPSE_KEY, next)
  }

  if (!result) return <Panel title="Open specs">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Open specs">
        <AuthGate error={result.error} />
      </Panel>
    )
  }

  const data = result.data

  // "Can't derive the ledger" and "the design record is empty" are different facts, and a ledger
  // whose whole job is to stop silence being ambiguous must not blur them.
  if (!data.available) {
    return (
      <Panel title="Open specs">
        <p className="error-state">{data.reason ?? "Couldn't read the design record."}</p>
      </Panel>
    )
  }
  if (!data.documents.length) {
    return (
      <Panel title="Open specs">
        <p className="empty-state">No documents in the design record.</p>
      </Panel>
    )
  }

  const groups = groupByToken(data)
  const right = <span className="row-sub">{summarise(data)}</span>

  return (
    <Panel
      title="Open specs"
      right={right}
      collapsed={collapsed}
      onToggleCollapsed={toggleCollapsed}
    >
      {data.unclassified_count > 0 && (
        <div className="job-alarm">
          {data.unclassified_count}{' '}
          {data.unclassified_count === 1 ? 'document has' : 'documents have'} no readable status —
          CI is red until each one declares it.
        </div>
      )}

      <div className="doc-status">
        {groups.map((group) => (
          <section key={group.token} className="doc-group">
            <h4 className="doc-group-title">
              <span className={tokenClass(group.token, group.open)}>{group.token}</span>
              <span className="row-sub">{group.documents.length}</span>
              {group.gloss ? <span className="doc-group-gloss">{group.gloss}</span> : null}
            </h4>
            <ul className="doc-list">
              {group.documents.map((doc) => (
                <li key={doc.path} className="doc-row">
                  <span className="doc-name">{doc.name}</span>
                  {doc.qualifier ? <span className="doc-qualifier">{doc.qualifier}</span> : null}
                  {doc.token === null ? (
                    <span className="doc-finding">needs a status ({doc.finding ?? 'unreadable'})</span>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </Panel>
  )
}
