// Small formatting helpers shared across panels.

export function relativeAge(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return 'unknown'
  if (seconds < 0) return 'just now'
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  // Runtime belt-and-braces: this function must NEVER return a non-string — handing a raw object
  // back to JSX is exactly how one malformed field blanked the whole dashboard (React #31).
  if (typeof value !== 'string') return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString()
}

/** Time-of-day only — for a log whose rows all belong to ONE session, where `toLocaleString()`'s
 *  date half is the same on every row and costs the column three times the width it needs. Falls
 *  back to the full string when the value isn't a parsable timestamp, so nothing is ever hidden. */
export function formatClockTime(value: string | null | undefined): string {
  if (!value || typeof value !== 'string') return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleTimeString(undefined, { hour12: false })
}

export function secondsUntil(value: string | null | undefined): number | null {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  return (d.getTime() - Date.now()) / 1000
}

export function relativeFuture(seconds: number | null): string {
  if (seconds == null || Number.isNaN(seconds)) return ''
  if (seconds <= 0) return 'due now'
  if (seconds < 60) return `in ${Math.round(seconds)}s`
  if (seconds < 3600) return `in ${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `in ${Math.round(seconds / 3600)}h`
  return `in ${Math.round(seconds / 86400)}d`
}

/** Minutes -> "7h 12m" (or just "42m" under an hour). v4: sleep/workout durations. */
export function formatMinutes(minutes: number | null | undefined): string {
  if (minutes == null || Number.isNaN(minutes)) return '—'
  const total = Math.max(0, Math.round(minutes))
  const h = Math.floor(total / 60)
  const m = total % 60
  return h === 0 ? `${m}m` : `${h}h ${m}m`
}

/** Seconds -> a DURATION ("45s", "7m", "1h 12m") — how long something took/has been going, which is
 * emphatically not `relativeAge`'s "…ago". Passing a duration to `relativeAge` renders "took 2
 * minutes" as "2m ago", which reads as "finished 2 minutes back" — a different and wrong fact. Used
 * by the Jobs panel (seneschal/docs/background-jobs-spec.md). */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return ''
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) return `${total}s`
  if (total < 3600) return `${Math.round(total / 60)}m`
  const h = Math.floor(total / 3600)
  const m = Math.round((total % 3600) / 60)
  return m === 0 ? `${h}h` : `${h}h ${m}m`
}

/** ISO-8601 week key ("2026-W29") for a calendar date — Monday-start weeks, week 1 contains the
 * year's first Thursday (the standard "nearest Thursday" algorithm). Computed on LOCAL Y/M/D fields,
 * mirroring Python's `date.isocalendar()` (cockpit/server/health.py's `_week_key`, same as
 * governor.py) — a calendar date has no time-of-day to convert, so no UTC juggling is needed. Used to
 * pick "this week" out of a workouts weekly rollup client-side without a second network round-trip. */
export function isoWeekKey(d: Date): string {
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const dayNum = (target.getDay() + 6) % 7 // Mon=0 ... Sun=6
  target.setDate(target.getDate() - dayNum + 3) // nearest Thursday
  const firstThursday = new Date(target.getFullYear(), 0, 4)
  const firstThursdayDayNum = (firstThursday.getDay() + 6) % 7
  firstThursday.setDate(firstThursday.getDate() - firstThursdayDayNum + 3)
  const weekNo = 1 + Math.round((target.getTime() - firstThursday.getTime()) / (7 * 86400000))
  return `${target.getFullYear()}-W${String(weekNo).padStart(2, '0')}`
}

export function thisIsoWeekKey(): string {
  return isoWeekKey(new Date())
}

/** A local calendar date string ("2026-07-17") -> "Fri, Jul 17" — no time-of-day, no timezone
 * conversion (the value is already a local date, not an instant; parsing it as UTC midnight and
 * re-localizing would risk shifting it a day in either direction). */
export function formatLocalDate(value: string | null | undefined): string {
  if (!value) return '—'
  const parts = value.split('-').map(Number)
  const [y, m, d] = parts
  if (y === undefined || m === undefined || d === undefined || [y, m, d].some(Number.isNaN)) {
    return value
  }
  const dt = new Date(y, m - 1, d)
  if (Number.isNaN(dt.getTime())) return value
  return dt.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
}
