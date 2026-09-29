// The sliver of Node's stdlib test runner that `*.test.ts` uses, declared by hand.
//
// `cockpit/web` has no test dependency and isn't getting one: the tests run on `node --test` (built
// in since Node 18; TypeScript type-stripping since 22.18), so the only thing missing was types.
// Pulling in `@types/node` to describe two imports would add a dependency to the one dependency world
// that has stayed at react + vite, and would drag Node's globals into a browser bundle's typechecking
// besides. This is ~15 lines instead.
//
// If `@types/node` is ever legitimately needed here, DELETE THIS FILE in the same change — leaving it
// would collide with the real declarations, loudly, which is the failure mode you want.

declare module 'node:test' {
  export function test(name: string, fn: () => void | Promise<void>): void
}

declare module 'node:assert/strict' {
  interface Assert {
    (value: unknown, message?: string): asserts value
    equal(actual: unknown, expected: unknown, message?: string): void
    deepEqual(actual: unknown, expected: unknown, message?: string): void
    ok(value: unknown, message?: string): asserts value
    throws(fn: () => unknown, message?: string): void
    doesNotThrow(fn: () => unknown, message?: string): void
  }
  const assert: Assert
  export default assert
}
