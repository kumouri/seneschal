// Stdlib `node --test`, no test dependency (see ../node-test-shim.d.ts for why). Run with
// `npm test` in cockpit/web; CI runs it in the cockpit-web job.
//
// Two of these exist for a reason stronger than coverage:
//   - grouping must never REORDER a chronological trace to make its grouping tidier;
//   - search must never become the place that reconstructs redacted text.
// The rest pin the roll-up arithmetic the turn headers report as fact.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  formatDuration,
  formatTokens,
  groupIntoTurns,
  matchesQuery,
  sortSessions,
  toolPreview,
  usageTokens,
} from './traceView.ts'
import type { TraceEvent, TraceSession } from '../types.ts'

const ev = (o: Partial<TraceEvent>): TraceEvent => ({ src: 'transcript', ...o }) as TraceEvent

// ------------------------------------------------------------------------------ grouping

test('a turn of many events becomes one group', () => {
  const groups = groupIntoTurns([
    ev({ turn_id: 't1', kind: 'turn_started' }),
    ev({ turn_id: 't1', kind: 'assistant_output' }),
    ev({ turn_id: 't1', kind: 'turn_done' }),
  ])
  assert.equal(groups.length, 1)
  assert.equal(groups[0]?.turnId, 't1')
  assert.equal(groups[0]?.events.length, 3)
})

test('two turns stay two groups', () => {
  const groups = groupIntoTurns([
    ev({ turn_id: 't1' }),
    ev({ turn_id: 't2' }),
  ])
  assert.deepEqual(groups.map((g) => g.turnId), ['t1', 't2'])
})

test('a turn-less event is its own group and does not swallow what follows', () => {
  // A spawn decision landing mid-log must not annex the next turn's events.
  const groups = groupIntoTurns([
    ev({ turn_id: 't1' }),
    ev({ src: 'spawn', kind: 'resumed' }),
    ev({ turn_id: 't2' }),
  ])
  assert.deepEqual(groups.map((g) => g.turnId), ['t1', null, 't2'])
  assert.equal(groups[1]?.events.length, 1)
})

test('NON-ADJACENT runs of the same turn stay separate — grouping never reorders the log', () => {
  // The load-bearing one. Merging these would move the spawn row out of its chronological place
  // purely to make the grouping look neater, and a trace that reorders itself is not a trace.
  const groups = groupIntoTurns([
    ev({ turn_id: 't1', ts: '01:00' }),
    ev({ src: 'spawn', ts: '01:01' }),
    ev({ turn_id: 't1', ts: '01:02' }),
  ])
  assert.deepEqual(groups.map((g) => g.turnId), ['t1', null, 't1'])
})

test('every event lands in exactly one group, and none is lost', () => {
  const events = [
    ev({ turn_id: 't1' }), ev({ turn_id: 't1' }), ev({ src: 'assertion' }), ev({ turn_id: 't2' }),
  ]
  const groups = groupIntoTurns(events)
  assert.equal(groups.reduce((n, g) => n + g.events.length, 0), events.length)
})

test('an empty, null or undefined event list yields no groups rather than throwing', () => {
  assert.deepEqual(groupIntoTurns([]), [])
  assert.deepEqual(groupIntoTurns(null), [])
  assert.deepEqual(groupIntoTurns(undefined), [])
})

test('group keys are unique, so two runs of one turn do not collide as React keys', () => {
  const groups = groupIntoTurns([
    ev({ turn_id: 't1' }), ev({ src: 'spawn' }), ev({ turn_id: 't1' }),
  ])
  assert.equal(new Set(groups.map((g) => g.key)).size, groups.length)
})

// ------------------------------------------------------------------------------ the roll-up

test('the header roll-up sums tools and errors and hoists the turn text', () => {
  const [g] = groupIntoTurns([
    ev({ turn_id: 't1', kind: 'turn_started', ts: '01:00', said: [{ speaker: 'owner', text: 'hi' }] }),
    ev({ turn_id: 't1', tool_uses: [{ name: 'Bash' }, { name: 'Read' }] }),
    ev({ turn_id: 't1', tool_uses: [{ name: 'Edit' }], is_error: true }),
    ev({ turn_id: 't1', kind: 'turn_done', said: [{ speaker: 'assistant', text: 'done' }],
         duration_ms: 90_000, total_cost_usd: 1.5 }),
  ] as TraceEvent[])
  assert.equal(g?.toolCalls, 3)
  assert.equal(g?.errors, 1)
  assert.equal(g?.startedAt, '01:00')
  assert.equal(g?.durationMs, 90_000)
  assert.equal(g?.costUsd, 1.5)
  assert.deepEqual(g?.said.map((s) => s.text), ['hi', 'done'])
})

test('the header never prints the same sentence twice, whatever the backend sent', () => {
  // The shape the OLD backend produced: every row of the turn carrying the whole turn's text. The
  // fix is in server/trace.py, but a header built by concatenation must not be able to print the
  // owner's sentence 68 times if a duplicate ever reaches it again.
  const both = [{ speaker: 'owner', at: 'a', text: 'hi' }, { speaker: 'assistant', at: 'b', text: 'done' }]
  const [g] = groupIntoTurns(Array.from({ length: 68 }, () => ev({ turn_id: 't1', said: both })))
  assert.deepEqual(g?.said.map((s) => s.text), ['hi', 'done'])
})

test('two genuinely distinct segments are both kept, even from the same speaker', () => {
  // Dedupe must not become "one line per speaker" — a turn can legitimately say two things.
  const [g] = groupIntoTurns([
    ev({ turn_id: 't1', said: [{ speaker: 'assistant', at: 'a', text: 'first' }] }),
    ev({ turn_id: 't1', said: [{ speaker: 'assistant', at: 'b', text: 'second' }] }),
  ])
  assert.deepEqual(g?.said.map((s) => s.text), ['first', 'second'])
})

test('a redacted event marks its whole turn redacted', () => {
  const [g] = groupIntoTurns([ev({ turn_id: 't1' }), ev({ turn_id: 't1', redacted: true })])
  assert.equal(g?.redacted, true)
})

test('a turn with no metrics reports null, never a confident zero', () => {
  const [g] = groupIntoTurns([ev({ turn_id: 't1' })])
  assert.equal(g?.durationMs, null)
  assert.equal(g?.costUsd, null)
  assert.equal(g?.tokens, null)
})

test('hostile field types do not become numbers', () => {
  const [g] = groupIntoTurns([
    ev({ turn_id: 't1', duration_ms: 'ages' as never, total_cost_usd: true as never }),
  ])
  assert.equal(g?.durationMs, null, 'a string is not a duration')
  assert.equal(g?.costUsd, null, 'a bool is not a cost and true must not become 1')
})

test('usageTokens sums all four token classes', () => {
  assert.equal(usageTokens({
    input_tokens: 2, cache_creation_input_tokens: 291, cache_read_input_tokens: 71_478, output_tokens: 103,
  }), 71_874)
})

test('usageTokens returns null for a shape it cannot read, not 0', () => {
  // 0 would render as "0 tok" and read as a measured fact. Absence must look like absence.
  assert.equal(usageTokens(null), null)
  assert.equal(usageTokens('lots'), null)
  assert.equal(usageTokens({}), null)
  assert.equal(usageTokens({ input_tokens: 'many' }), null)
})

test('usageTokens still totals a partial shape', () => {
  assert.equal(usageTokens({ output_tokens: 5, cache_read_input_tokens: 'x' }), 5)
})

// ------------------------------------------------------------------------------ formatting

test('formatDuration reads naturally at every scale', () => {
  assert.equal(formatDuration(420), '420ms')
  assert.equal(formatDuration(4828), '4.8s')
  assert.equal(formatDuration(90_000), '1m 30s')
  assert.equal(formatDuration(990_240), '16m 30s')
  assert.equal(formatDuration(3_930_000), '1h 05m', 'minutes floor — 1h 05m 30s is not 1h 06m')
})

test('formatDuration and formatTokens refuse nonsense instead of printing NaN', () => {
  for (const bad of [null, undefined, 'soon', NaN, Infinity, -5]) {
    assert.equal(formatDuration(bad as never), null)
    assert.equal(formatTokens(bad as never), null)
  }
})

test('formatTokens keeps small counts exact and switches to M past a million', () => {
  // A busy turn re-reads its cached context every step and really does process ~1.8M tokens; as
  // "1836k" that is a four-digit k nobody parses at a glance.
  assert.equal(formatTokens(6), '6')
  assert.equal(formatTokens(999), '999')
  assert.equal(formatTokens(284_000), '284k')
  assert.equal(formatTokens(999_000), '999k')
  assert.equal(formatTokens(1_836_000), '1.8M')
})

test('toolPreview flattens whitespace but never truncates', () => {
  // §11 already ruled tool input renders inline, not hover-only — a length cap on top of the
  // backend's own 200-char preview silently undid that. Nothing left to cut here.
  assert.equal(toolPreview('git   status\n  --short'), 'git status --short')
  assert.equal(toolPreview('x'.repeat(200))?.length, 200)
  assert.equal(toolPreview('x'.repeat(200)), 'x'.repeat(200))
})

test('toolPreview returns null for nothing worth showing', () => {
  assert.equal(toolPreview(null), null)
  assert.equal(toolPreview('   '), null)
  assert.equal(toolPreview(42), null)
})

// ------------------------------------------------------------------------------ search

test('search matches tool names, tool input, kind and spoken text', () => {
  const e = ev({
    kind: 'assistant_output',
    tool_uses: [{ name: 'Bash', input_preview: 'git status' }],
    said: [{ speaker: 'owner', text: 'the deploy looks stuck' }],
  } as Partial<TraceEvent>)
  for (const q of ['bash', 'GIT STATUS', 'assistant', 'deploy', 'owner']) {
    assert.equal(matchesQuery(e, q), true, `should have matched ${q}`)
  }
  assert.equal(matchesQuery(e, 'nothing like this'), false)
})

test('an empty or whitespace query matches everything — clearing the box restores the list', () => {
  const e = ev({ kind: 'turn_done' })
  assert.equal(matchesQuery(e, ''), true)
  assert.equal(matchesQuery(e, '   '), true)
  assert.equal(matchesQuery(e, null), true)
})

test('SEARCH NEVER RESURRECTS A REDACTED TURN', () => {
  // A redacted event carries no text — the backend refuses to send any. Search must not become the
  // one reader that reconstructs it, so a redacted event contributes nothing to the haystack and
  // can only ever match on the fields that are not the private part.
  const e = ev({ kind: 'turn_done', redacted: true, said: [{ speaker: 'owner', text: 'SECRET' }],
                 reply_preview: 'ALSO SECRET' } as Partial<TraceEvent>)
  assert.equal(matchesQuery(e, 'secret'), false)
  assert.equal(matchesQuery(e, 'turn_done'), true, 'the row itself is still findable')
})

test('search tolerates a hostile or empty event rather than throwing', () => {
  assert.equal(matchesQuery(null, 'x'), false)
  assert.equal(matchesQuery(undefined, 'x'), false)
  assert.equal(matchesQuery(ev({}), 'x'), false)
  assert.equal(matchesQuery({ tool_uses: 'not an array' } as never, 'x'), false)
})

// ------------------------------------------------------------------------------ sessions list sort

const sess = (o: Partial<TraceSession>): TraceSession =>
  ({ session_id: 'x', turns: 0, cost_usd: 0, context_peak: 0, errors: 0, sources: [], ...o }) as TraceSession

test('sortSessions orders by the chosen field, most-first', () => {
  const list = [
    sess({ session_id: 'a', cost_usd: 1, turns: 9, errors: 0, last_at: '2026-09-01T00:00:00Z' }),
    sess({ session_id: 'b', cost_usd: 9, turns: 1, errors: 3, last_at: '2026-09-03T00:00:00Z' }),
    sess({ session_id: 'c', cost_usd: 4, turns: 4, errors: 1, last_at: '2026-09-02T00:00:00Z' }),
  ]
  assert.deepEqual(sortSessions(list, 'recent').map((s) => s.session_id), ['b', 'c', 'a'])
  assert.deepEqual(sortSessions(list, 'cost').map((s) => s.session_id), ['b', 'c', 'a'])
  assert.deepEqual(sortSessions(list, 'turns').map((s) => s.session_id), ['a', 'c', 'b'])
  assert.deepEqual(sortSessions(list, 'errors').map((s) => s.session_id), ['b', 'c', 'a'])
})

test('sortSessions never mutates the array it was given', () => {
  const list = [sess({ session_id: 'a', last_at: '1' }), sess({ session_id: 'b', last_at: '2' })]
  const original = [...list]
  sortSessions(list, 'recent')
  assert.deepEqual(list, original)
})

test('sortSessions tolerates an empty, null or undefined list', () => {
  assert.deepEqual(sortSessions([], 'cost'), [])
  assert.deepEqual(sortSessions(null, 'cost'), [])
  assert.deepEqual(sortSessions(undefined, 'cost'), [])
})
