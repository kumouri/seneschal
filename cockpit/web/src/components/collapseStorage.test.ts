// Only the numeric helpers are pinned here — added for the Trace panel's resizable sessions pane.
// The boolean pair predates this file's
// tests and is exercised indirectly through the Jobs/Trace panels that already call it.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { readNumberFor, writeNumberFor, type StorageLike } from './collapseStorage.ts'

function memoryStorage(): StorageLike {
  const data = new Map<string, string>()
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => { data.set(k, v) },
  }
}

test('a written number reads back exactly', () => {
  const s = memoryStorage()
  writeNumberFor(s, 'w', 312)
  assert.equal(readNumberFor(s, 'w', -1), 312)
})

test('no preference recorded falls back to the default', () => {
  assert.equal(readNumberFor(memoryStorage(), 'missing', 260), 260)
  assert.equal(readNumberFor(null, 'w', 260), 260)
  assert.equal(readNumberFor(undefined, 'w', 260), 260)
})

test('a non-numeric or corrupt value falls back rather than reading as NaN', () => {
  const s = memoryStorage()
  s.setItem('w', 'not a number')
  assert.equal(readNumberFor(s, 'w', 260), 260)
})

test('storage that throws on read or write degrades to the default, never a crash', () => {
  const angry: StorageLike = {
    getItem: () => { throw new Error('nope') },
    setItem: () => { throw new Error('nope') },
  }
  assert.equal(readNumberFor(angry, 'w', 260), 260)
  assert.doesNotThrow(() => writeNumberFor(angry, 'w', 300))
})
