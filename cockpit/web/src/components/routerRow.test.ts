// Stdlib `node --test`, no test dependency (see ../node-test-shim.d.ts for why). Run with
// `npm test` in cockpit/web; CI runs it in the cockpit-web job.
//
// These exist for ONE reason: a Router row must never let the ARM be read as the VERDICT. That
// misreading already happened once, on the panel, to the person the panel is for — and "we wrote a
// comment saying not to" is not a guarantee. Everything below is that guarantee.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { armLabel, contextLabel, decisionLabel, decisionTitle } from './routerRow.ts'

/** The row that started this: the fable arm declining to delegate. `arm` and `verdict` share the
 *  word "fable", and there is no `category` — router.py's fable arm never logs one. */
function fableArmRow(over: Record<string, unknown> = {}) {
  return { ts: '2026-08-09T23:12:00Z', channel: 'telegram', arm: 'fable', verdict: 'standard', ...over }
}

/** A triage-arm row, which does carry a category. */
function triageArmRow(over: Record<string, unknown> = {}) {
  return {
    ts: '2026-08-09T23:12:00Z',
    channel: 'telegram',
    arm: 'triage',
    verdict: 'escalate',
    category: 'other',
    ...over,
  }
}

// ------------------------------------------------------- THE RULE: arm is never read as a verdict

test('the arm is always labelled as an arm, never left as a bare word', () => {
  assert.equal(armLabel('fable'), 'fable arm')
  assert.equal(armLabel('triage'), 'triage arm')
})

test('a fable-arm STANDARD row cannot be read as "went to Fable"', () => {
  const row = fableArmRow()
  // The word "fable" appears only as an arm...
  assert.equal(contextLabel(row), 'telegram · fable arm')
  // ...and the decision, which is what a reader parses as "what happened", says standard.
  assert.equal(decisionLabel(row), '→ standard')
  assert.equal(decisionTitle(row), 'fable arm → standard')
})

test('a fable-arm FABLE row still separates who decided from what they decided', () => {
  const row = fableArmRow({ verdict: 'fable' })
  assert.equal(contextLabel(row), 'telegram · fable arm')
  assert.equal(decisionLabel(row), '→ fable')
  // The two occurrences are distinguishable: one is suffixed "arm", the other prefixed with the arrow.
  assert.equal(decisionTitle(row), 'fable arm → fable')
})

test('the verdict is never rendered without its arrow, and the arm never with one', () => {
  for (const row of [fableArmRow(), triageArmRow(), {}]) {
    assert.ok(decisionLabel(row).startsWith('→ '), `missing arrow: ${decisionLabel(row)}`)
    assert.ok(!contextLabel(row).includes('→'), `arm rendered as a decision: ${contextLabel(row)}`)
    assert.ok(contextLabel(row).includes('arm'), `arm not labelled: ${contextLabel(row)}`)
  }
})

// --------------------------------------------------- NO EMPTY SEGMENTS: the dangling separator dies

test('a row with no category ends cleanly — no dangling separator anywhere', () => {
  const row = fableArmRow()
  for (const rendered of [contextLabel(row), decisionLabel(row), decisionTitle(row)]) {
    assert.ok(!rendered.includes('·  '), `doubled separator: ${JSON.stringify(rendered)}`)
    assert.ok(!/[·(]\s*$/.test(rendered), `trailing separator: ${JSON.stringify(rendered)}`)
    assert.equal(rendered, rendered.trim())
  }
})

test('an explicitly null or blank category is dropped, not rendered as ()', () => {
  for (const category of [null, undefined, '', '   ']) {
    assert.equal(decisionLabel(fableArmRow({ category })), '→ standard')
  }
})

test('a present category rides with the verdict, not with the arm', () => {
  const row = triageArmRow()
  assert.equal(contextLabel(row), 'telegram · triage arm')
  assert.equal(decisionLabel(row), '→ escalate (other)')
  assert.equal(decisionTitle(row), 'triage arm → escalate (other)')
})

test('a missing channel drops its segment rather than emitting a placeholder', () => {
  for (const channel of [null, undefined, '', '  ']) {
    assert.equal(contextLabel(fableArmRow({ channel })), 'fable arm')
  }
})

// ------------------------------------------------------------------------------ TOLERANT READER
// The panel reads a log the daemon writes on a different commit. A renamed, missing, or
// wrongly-typed field must degrade to an honest word — never blank, never a crash, and never a
// shape that reads as a verdict.

test('an unusable arm still says "arm"', () => {
  for (const arm of [null, undefined, '', '   ', 42, {}, ['fable']]) {
    assert.equal(armLabel(arm), 'unknown arm')
  }
})

test('an arm this build has never heard of passes through, labelled', () => {
  assert.equal(contextLabel(fableArmRow({ arm: 'sentiment' })), 'telegram · sentiment arm')
})

test('an unusable verdict reads "unknown", never blank', () => {
  for (const verdict of [null, undefined, '', '  ', 7, {}]) {
    assert.equal(decisionLabel(fableArmRow({ verdict })), '→ unknown')
  }
})

test('non-string channel and category are dropped rather than stringified', () => {
  const row = fableArmRow({ channel: 12, category: { name: 'other' } })
  assert.equal(contextLabel(row), 'fable arm')
  assert.equal(decisionLabel(row), '→ standard')
})

test('an empty, null, or undefined row renders something honest instead of throwing', () => {
  for (const row of [{}, null, undefined]) {
    assert.equal(contextLabel(row), 'unknown arm')
    assert.equal(decisionLabel(row), '→ unknown')
    assert.equal(decisionTitle(row), 'unknown arm → unknown')
  }
})

test('surrounding whitespace in the log never leaks into the rendered row', () => {
  const row = fableArmRow({ channel: ' telegram ', arm: ' fable ', verdict: ' standard ' })
  assert.equal(contextLabel(row), 'telegram · fable arm')
  assert.equal(decisionLabel(row), '→ standard')
})
