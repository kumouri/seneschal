import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  clampSessionsWidth,
  DEFAULT_SESSIONS_WIDTH,
  MAX_SESSIONS_WIDTH,
  MIN_SESSIONS_WIDTH,
} from './traceResize.ts'

test('a width inside the bounds passes through unchanged', () => {
  assert.equal(clampSessionsWidth(300), 300)
})

test('a width past either bound is clamped to it', () => {
  assert.equal(clampSessionsWidth(10), MIN_SESSIONS_WIDTH)
  assert.equal(clampSessionsWidth(10_000), MAX_SESSIONS_WIDTH)
})

test('a hostile or missing width falls back to the default rather than NaN', () => {
  for (const bad of [null, undefined, 'wide', NaN, Infinity]) {
    assert.equal(clampSessionsWidth(bad as never), DEFAULT_SESSIONS_WIDTH)
  }
})

test('a narrow container caps the max below its own bound, never below the minimum', () => {
  assert.equal(clampSessionsWidth(400, 500), 380, 'leaves 120px for the handle and detail pane')
  assert.equal(clampSessionsWidth(400, 200), MIN_SESSIONS_WIDTH, 'never drops below the floor')
})

test('a hostile container width is ignored, not treated as zero', () => {
  assert.equal(clampSessionsWidth(300, -50), 300)
  assert.equal(clampSessionsWidth(300, NaN as never), 300)
  assert.equal(clampSessionsWidth(300, null), 300)
})
