// Stdlib `node --test`, no test dependency (see ../node-test-shim.d.ts for why). Run with
// `npm test` in cockpit/web; CI runs it in the cockpit-web job.
//
// These exist for ONE reason: **the ledger must never drop a document.** It answers "what has and
// hasn't been completed", and a grouping that quietly loses a row makes that answer wrong in the
// direction nobody notices — which is the failure the whole exercise was commissioned to end. A
// comment saying "don't drop rows" is not a guarantee. Everything below is that guarantee.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { UNCLASSIFIED, groupByToken, summarise } from './docStatusView.ts'
import type { DocStatusResponse } from '../types.ts'

function doc(name: string, token: string | null, qualifier: string | null = null) {
  return {
    path: `seneschal/docs/${name}`,
    name,
    token,
    qualifier,
    summary: '',
    finding: token === null ? 'missing' : null,
  }
}

function payload(over: Partial<DocStatusResponse> = {}): DocStatusResponse {
  return {
    available: true,
    reason: null,
    documents: [],
    counts: {},
    order: ['BUILT', 'PARTIAL', 'SPEC-ONLY', 'MEMO', 'REFERENCE'],
    gloss: { BUILT: 'all of it exists', PARTIAL: 'some parts built' },
    open_tokens: ['PARTIAL', 'SPEC-ONLY'],
    open_count: 0,
    unclassified_count: 0,
    total: 0,
    ...over,
  }
}

test('every document lands in exactly one group', () => {
  const documents = [
    doc('a-spec.md', 'BUILT'),
    doc('b-spec.md', 'PARTIAL', 'phase 0'),
    doc('c-spec.md', 'SPEC-ONLY'),
    doc('d-spec.md', 'MEMO'),
    doc('e-spec.md', null),
  ]
  const groups = groupByToken(payload({ documents }))
  const seen = groups.flatMap((g) => g.documents.map((d) => d.name))
  assert.equal(seen.length, documents.length)
  assert.deepEqual([...seen].sort(), documents.map((d) => d.name).sort())
})

test('groups follow the SERVER order, not an order invented here', () => {
  const documents = [doc('m.md', 'MEMO'), doc('b.md', 'BUILT'), doc('p.md', 'PARTIAL', 'p0')]
  const groups = groupByToken(payload({ documents }))
  assert.deepEqual(
    groups.map((g) => g.token),
    ['BUILT', 'PARTIAL', 'MEMO'],
  )
})

test('a token the server orders but this browser does not know still renders', () => {
  // A browser built against an older vocabulary must not make a document disappear. Falling back to
  // "drop it" here would hide new work behind a stale bundle.
  const documents = [doc('x.md', 'FUTURE-TOKEN')]
  const groups = groupByToken(payload({ documents, order: ['BUILT'] }))
  assert.equal(groups.length, 1)
  const only = groups[0]!
  assert.equal(only.token, 'FUTURE-TOKEN')
  assert.equal(only.documents[0]!.name, 'x.md')
})

test('unclassified documents go LAST and are never tidied away', () => {
  // Unclassified means CI is red or a header is unreadable. It is the one group that must always
  // render — silently omitting it is how an inventory reports itself complete while missing things.
  const documents = [doc('a.md', null), doc('b.md', 'BUILT')]
  const groups = groupByToken(payload({ documents }))
  const last = groups[groups.length - 1]!
  assert.equal(last.token, UNCLASSIFIED)
  assert.equal(last.documents[0]!.name, 'a.md')
})

test('UNCLASSIFIED is not a status token', () => {
  // If it ever appeared in the server's `order`, it would read as a value the vocabulary blesses.
  const groups = groupByToken(payload({ documents: [doc('a.md', null)] }))
  assert.equal(groups.length, 1)
  assert.ok(!payload().order.includes(UNCLASSIFIED))
})

test('open-ness comes from the server, never from the token name', () => {
  const documents = [doc('a.md', 'PARTIAL', 'p0'), doc('b.md', 'MEMO'), doc('c.md', 'BUILT')]
  const groups = groupByToken(payload({ documents }))
  const open = Object.fromEntries(groups.map((g) => [g.token, g.open]))
  assert.equal(open['PARTIAL'], true)
  assert.equal(open['MEMO'], false, 'a memo is complete when written, not backlog')
  assert.equal(open['BUILT'], false)
})

test('an empty ledger groups to nothing rather than throwing', () => {
  assert.deepEqual(groupByToken(payload()), [])
})

test('a payload missing its optional arrays does not throw', () => {
  // The degraded payload has the same SHAPE as a good one, but a browser cached across a deploy may
  // not. One renderer, no crash.
  const bare = { available: true } as unknown as DocStatusResponse
  assert.deepEqual(groupByToken(bare), [])
  assert.equal(typeof summarise(bare), 'string')
})

test('the summary reads off the server counts', () => {
  assert.equal(summarise(payload({ open_count: 30, total: 54 })), '30 open of 54')
  assert.equal(
    summarise(payload({ open_count: 30, total: 54, unclassified_count: 2 })),
    '30 open of 54 · 2 with no status',
  )
  assert.equal(summarise(payload({ available: false })), 'unavailable')
})
