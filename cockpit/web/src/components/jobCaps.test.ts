// Stdlib `node --test`, no test dependency (see ../node-test-shim.d.ts for why). Run with
// `npm test` in cockpit/web; CI runs it in the cockpit-web job.
//
// Pins the caps control's two promises: a pair the server would reject is refused here by the
// server's own bounds and NEVER clamped, and an uncountable fleet never reads as zero.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { LOWERING_HINT, inFlightLine, validateCaps } from './jobCaps.ts'

const LIMITS = { soft_min: 1, hard_ceiling: 12 }

test('a valid pair parses to numbers', () => {
  assert.deepEqual(validateCaps('3', '5', LIMITS), { ok: true, soft: 3, hard: 5 })
  assert.deepEqual(validateCaps(' 4 ', '4', LIMITS), { ok: true, soft: 4, hard: 4 })
})

test('rejections never clamp', () => {
  const cases: [string, string][] = [
    ['0', '5'],
    ['6', '5'],
    ['3', '13'],
    ['3.5', '5'],
    ['-1', '5'],
    ['', '5'],
    ['3', 'five'],
  ]
  for (const [soft, hard] of cases) {
    const out = validateCaps(soft, hard, LIMITS)
    assert.equal(out.ok, false, `${soft}/${hard} should be refused`)
  }
})

test('the bounds come from the server, not a constant here', () => {
  assert.equal(validateCaps('3', '13', { soft_min: 1, hard_ceiling: 20 }).ok, true)
  assert.equal(validateCaps('1', '5', { soft_min: 2, hard_ceiling: 12 }).ok, false)
})

test('an unknown count reads as unknown, never as zero', () => {
  const line = inFlightLine(null, 5)
  assert.ok(/unknown/.test(line.text))
  assert.ok(!/\b0 of\b/.test(line.text))
  assert.equal(line.over, false)
})

test('a count above a lowered cap is flagged, and the hint says nothing is cancelled', () => {
  assert.deepEqual(inFlightLine(4, 2), { text: '4 of 2 agent jobs in flight', over: true })
  assert.equal(inFlightLine(2, 5).over, false)
  assert.ok(LOWERING_HINT.includes('cancels nothing'))
})
