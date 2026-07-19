import { useEffect, useRef, useState } from 'react'
import type { ApiResult } from './api'

// Simple ~5s polling (per the brief: "simple polling (~5s) is fine" — no need for websockets until
// the daemon pipe, v2). Keeps the latest ApiResult<T> in state; a stale closure over `fetcher` is
// avoided via a ref so callers can pass an inline arrow function without re-triggering the interval.
export function usePolling<T>(fetcher: () => Promise<ApiResult<T>>, intervalMs = 5000) {
  const [result, setResult] = useState<ApiResult<T> | null>(null)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    async function tick() {
      const r = await fetcherRef.current()
      if (!cancelled) {
        setResult(r)
        timer = setTimeout(tick, intervalMs)
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs])

  return result
}
