// Stdlib `node --test`, no test dependency (see ../node-test-shim.d.ts). Run with `npm test`.
//
// Pins what the Jobs panel says a job runs on (jobRuntime.ts): the browser only READS the stamp —
// an old record is "—", never re-guessed; `unknown` is distinct from both LLM and non-LLM; and the
// filter can never hide an undelivered completion push.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  RUNTIME_FILTERS,
  countByRuntime,
  filterByRuntime,
  runtimeKind,
  runtimeText,
} from './jobRuntime.ts'

const LLM = { subscription: 'main', harness: 'claude-cli', model: 'default', llm: true }
const NON = { subscription: null, harness: 'none', model: null, llm: false }
const UNK = { subscription: null, harness: 'unknown', model: null, llm: 'unknown' }

function job(id: string, runtime: unknown, over: Record<string, unknown> = {}) {
  return { id, runtime, is_running: false, notified_at: '2026-09-30T12:00:00Z', ...over }
}

test('kinds come straight off the stamp', () => {
  assert.equal(runtimeKind(LLM), 'llm')
  assert.equal(runtimeKind(NON), 'non-llm')
  assert.equal(runtimeKind(UNK), 'unknown')
})

test('a record without the stamp is unrecorded, never inferred', () => {
  assert.equal(runtimeKind(null), 'unrecorded')
  assert.equal(runtimeKind(undefined), 'unrecorded')
  assert.equal(runtimeKind('claude'), 'unrecorded')
  assert.equal(runtimeText(null), '—')
})

test('a block with an illegal llm value is unknown — it exists but proves nothing', () => {
  assert.equal(runtimeKind({ ...LLM, llm: 'yes' }), 'unknown')
  assert.equal(runtimeKind({ ...LLM, llm: null }), 'unknown')
})

test('unknown is distinct from both LLM and non-LLM', () => {
  const kinds = new Set([runtimeKind(LLM), runtimeKind(NON), runtimeKind(UNK), runtimeKind(null)])
  assert.equal(kinds.size, 4)
})

test('the phrase is subscription · harness · model', () => {
  assert.equal(runtimeText(LLM), 'main · claude-cli · default')
  assert.equal(runtimeText({ subscription: 'ChatGPT', harness: 'codex', model: 'gpt-6-sol', llm: true }),
    'ChatGPT · codex · gpt-6-sol')
  assert.equal(runtimeText(NON), '')
  assert.equal(runtimeText(UNK), '')
  assert.equal(runtimeText({ ...UNK, llm: true }), 'harness unknown')
  // A named harness under an unknown class still says what it knows.
  assert.equal(runtimeText({ subscription: 'ChatGPT', harness: 'codex', model: null, llm: 'unknown' }),
    'ChatGPT · codex · ?')
})

test('filters select by kind and all is the identity, order preserved', () => {
  const jobs = [job('a', LLM), job('b', NON), job('c', UNK), job('d', null), job('e', LLM)]
  assert.deepEqual(filterByRuntime(jobs, 'all').map((j) => j.id), ['a', 'b', 'c', 'd', 'e'])
  assert.deepEqual(filterByRuntime(jobs, 'llm').map((j) => j.id), ['a', 'e'])
  assert.deepEqual(filterByRuntime(jobs, 'non-llm').map((j) => j.id), ['b'])
  assert.deepEqual(filterByRuntime(jobs, 'unknown').map((j) => j.id), ['c'])
  assert.deepEqual(filterByRuntime(jobs, 'unrecorded').map((j) => j.id), ['d'])
})

test('a running job is filterable like any other', () => {
  const jobs = [job('run', LLM, { is_running: true, notified_at: null })]
  assert.deepEqual(filterByRuntime(jobs, 'non-llm'), [])
})

test('the filter never hides an undelivered completion push', () => {
  const jobs = [
    job('unpushed', LLM, { notified_at: null }),
    job('garbled', LLM, { is_running: 'false', notified_at: null }),
    job('delivered', LLM),
  ]
  assert.deepEqual(filterByRuntime(jobs, 'non-llm').map((j) => j.id), ['unpushed', 'garbled'])
})

test('counts cover every kind and sum to all', () => {
  const counts = countByRuntime([job('a', LLM), job('b', NON), job('c', UNK), job('d', null)])
  assert.deepEqual(counts, { all: 4, llm: 1, 'non-llm': 1, unknown: 1, unrecorded: 1 })
  assert.deepEqual(countByRuntime(null).all, 0)
  assert.deepEqual(RUNTIME_FILTERS.map((f) => f.key).sort(), Object.keys(counts).sort())
})
