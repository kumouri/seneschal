// Stdlib `node --test`, no test dependency (see ../node-test-shim.d.ts for why). Run with
// `npm test` in cockpit/web; CI runs it in the cockpit-web job.
//
// These exist for ONE reason: the collapse toggle must never be able to hide an undelivered
// completion push. That is the state the whole Jobs panel was built to surface, and "we wrote a
// comment saying not to" is not a guarantee. Everything below is that guarantee.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  COLLAPSED_BY_DEFAULT,
  COLLAPSE_STORAGE_KEY,
  isCollapsible,
  partitionRecent,
  readCollapsed,
  writeCollapsed,
  type StorageLike,
} from './jobsCollapse.ts'

/** The backend's own definition of the alarm (cockpit/server/jobs.py: `awaiting_push` counts
 *  `is_terminal and not notified_at`). Restated here so the test asserts against the CONTRACT rather
 *  than against the implementation it is guarding. */
function isAwaitingPush(job: { is_terminal: boolean; notified_at: string | null }): boolean {
  return job.is_terminal && !job.notified_at
}

function job(over: Record<string, unknown> = {}) {
  return {
    id: 'j1',
    is_running: false,
    is_terminal: true,
    notified_at: '2026-08-09T12:00:00Z',
    ...over,
  }
}

// --------------------------------------------------------------------------- INVARIANT 1: running

test('a running job is never collapsible', () => {
  assert.equal(isCollapsible(job({ is_running: true, is_terminal: false, notified_at: null })), false)
})

test('a running job is never collapsible even if something stamped notified_at', () => {
  // jobs.py shouldn't produce this, but the panel reads a ledger written by a daemon on a different
  // commit. A stray timestamp must not be enough to hide live work.
  assert.equal(isCollapsible(job({ is_running: true, is_terminal: false })), false)
})

// ---------------------------------------------------------------------- INVARIANT 2: awaiting push

test('a finished job whose completion push has not landed is never collapsible', () => {
  assert.equal(isCollapsible(job({ notified_at: null })), false)
})

test('an empty-string notified_at does not count as delivered', () => {
  assert.equal(isCollapsible(job({ notified_at: '' })), false)
})

test('a non-string notified_at does not count as delivered', () => {
  assert.equal(isCollapsible(job({ notified_at: 1754755200 })), false)
  assert.equal(isCollapsible(job({ notified_at: true })), false)
  assert.equal(isCollapsible(job({ notified_at: {} })), false)
})

test('a finished, delivered job IS collapsible — otherwise the toggle does nothing', () => {
  assert.equal(isCollapsible(job()), true)
  assert.equal(isCollapsible(job({ status: 'failed', exit_code: 1 })), true)
  assert.equal(isCollapsible(job({ status: 'cancelled' })), true)
})

// ------------------------------------------------------- Tolerant reader: unknown shapes stay shown

test('an unrecognized job shape is never collapsible', () => {
  assert.equal(isCollapsible({}), false)
  assert.equal(isCollapsible(null), false)
  assert.equal(isCollapsible(undefined), false)
  assert.equal(isCollapsible({ notified_at: '2026-08-09T12:00:00Z' }), false)
  // A renamed/re-typed field must degrade to "might be running", not to "hide it".
  assert.equal(isCollapsible({ is_running: 'false', notified_at: '2026-08-09T12:00:00Z' }), false)
  assert.equal(isCollapsible({ is_running: 0, notified_at: '2026-08-09T12:00:00Z' }), false)
  assert.equal(isCollapsible({ is_running: null, notified_at: '2026-08-09T12:00:00Z' }), false)
})

// ------------------------------------------------------------------------------ partitionRecent

test('partitionRecent preserves the backend ended-at ordering inside each bucket', () => {
  const recent = [
    job({ id: 'a' }),
    job({ id: 'b', notified_at: null }),
    job({ id: 'c' }),
    job({ id: 'd', is_running: true, is_terminal: false, notified_at: null }),
    job({ id: 'e' }),
  ]
  const { pinned, collapsible } = partitionRecent(recent)
  assert.deepEqual(pinned.map((j) => j.id), ['b', 'd'])
  assert.deepEqual(collapsible.map((j) => j.id), ['a', 'c', 'e'])
})

test('partitionRecent loses nothing — every job lands in exactly one bucket', () => {
  const recent = [job({ id: 'a' }), job({ id: 'b', notified_at: null }), job({ id: 'c' })]
  const { pinned, collapsible } = partitionRecent(recent)
  assert.equal(pinned.length + collapsible.length, recent.length)
  const ids = new Set([...pinned, ...collapsible].map((j) => j.id))
  assert.equal(ids.size, recent.length)
})

test('partitionRecent tolerates a missing or empty list', () => {
  assert.deepEqual(partitionRecent([]), { pinned: [], collapsible: [] })
  assert.deepEqual(partitionRecent(null), { pinned: [], collapsible: [] })
  assert.deepEqual(partitionRecent(undefined), { pinned: [], collapsible: [] })
})

// ------------------------------- THE ONE THAT MATTERS: collapsing cannot swallow an awaiting push

test('a collapsed group still shows every awaiting-push job the alarm banner counts', () => {
  // A realistic mixed ledger: delivered successes, a delivered failure, two undelivered pushes, and
  // one still running. What the banner claims and what the collapsed group shows must agree.
  const recent = [
    job({ id: 'ok-1' }),
    job({ id: 'unpushed-1', notified_at: null }),
    job({ id: 'failed-but-notified', status: 'failed', exit_code: 2 }),
    job({ id: 'ok-2' }),
    job({ id: 'unpushed-2', status: 'timed-out', notified_at: null }),
    job({ id: 'still-running', is_running: true, is_terminal: false, notified_at: null }),
    job({ id: 'ok-3' }),
  ]

  const awaitingPush = recent.filter(isAwaitingPush).map((j) => j.id)
  assert.deepEqual(awaitingPush, ['unpushed-1', 'unpushed-2'], 'fixture sanity')

  // This is what JobsPanel renders while collapsed.
  const shownWhileCollapsed = new Set(partitionRecent(recent).pinned.map((j) => j.id))

  for (const id of awaitingPush) {
    assert.ok(shownWhileCollapsed.has(id), `collapsing hid an awaiting-push job: ${id}`)
  }
  assert.ok(shownWhileCollapsed.has('still-running'), 'collapsing hid a running job')
  // And it does still collapse the quiet history, or the feature is pointless.
  assert.equal(partitionRecent(recent).collapsible.length, 4)
})

test('a group that is nothing but awaiting-push jobs collapses to nothing at all', () => {
  const recent = [job({ id: 'x', notified_at: null }), job({ id: 'y', notified_at: null })]
  const { pinned, collapsible } = partitionRecent(recent)
  assert.deepEqual(pinned.map((j) => j.id), ['x', 'y'])
  assert.equal(collapsible.length, 0)
})

// ------------------------------------------------------------------------------- the stored choice

function fakeStorage(seed: Record<string, string> = {}): StorageLike & { store: Record<string, string> } {
  const store = { ...seed }
  return {
    store,
    getItem: (k) => (k in store ? (store[k] as string) : null),
    setItem: (k, v) => {
      store[k] = v
    },
  }
}

const hostileStorage: StorageLike = {
  getItem() {
    throw new Error('SecurityError: storage is disabled')
  },
  setItem() {
    throw new Error('QuotaExceededError')
  },
}

test('with nothing stored, the default applies', () => {
  assert.equal(readCollapsed(fakeStorage()), COLLAPSED_BY_DEFAULT)
  assert.equal(readCollapsed(null), COLLAPSED_BY_DEFAULT)
  assert.equal(readCollapsed(undefined), COLLAPSED_BY_DEFAULT)
})

test('the choice round-trips, so a reload does not re-expand (or re-collapse)', () => {
  const storage = fakeStorage()
  writeCollapsed(storage, false)
  assert.equal(storage.store[COLLAPSE_STORAGE_KEY], '0')
  assert.equal(readCollapsed(storage), false)

  writeCollapsed(storage, true)
  assert.equal(storage.store[COLLAPSE_STORAGE_KEY], '1')
  assert.equal(readCollapsed(storage), true)
})

test('a corrupt or future stored value falls back to the default instead of guessing', () => {
  assert.equal(readCollapsed(fakeStorage({ [COLLAPSE_STORAGE_KEY]: 'yes' })), COLLAPSED_BY_DEFAULT)
  assert.equal(readCollapsed(fakeStorage({ [COLLAPSE_STORAGE_KEY]: '' })), COLLAPSED_BY_DEFAULT)
  assert.equal(readCollapsed(fakeStorage({ [COLLAPSE_STORAGE_KEY]: '{"collapsed":true}' })), COLLAPSED_BY_DEFAULT)
})

test('storage that throws degrades to the default rather than blanking the panel', () => {
  assert.equal(readCollapsed(hostileStorage), COLLAPSED_BY_DEFAULT)
  assert.doesNotThrow(() => writeCollapsed(hostileStorage, false))
})
