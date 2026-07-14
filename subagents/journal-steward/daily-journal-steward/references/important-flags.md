# Phase 3 — Important Flags

Database: **⭐ Important Flags** `collection://00000000-0000-0000-0000-000000000003`. The carry-over
**visual treatment** lives in `clear-and-carryover.md`; this file is the DB + lifecycle.

## Recognizing flags (owner-driven only)

Treat these as important flags when they appear in/alongside an entry:
- The word "important" with intent ("important:", "this is important", "flag this", "important to
  remember"), or `⭐` / `🚩` / `📌`.
- An explicit carry-over request ("keep this for tomorrow", "don't forget this").
- An inline importance type ("important work thing:", "important family thing:").

If an entry is clearly significant but **not** explicitly flagged, do **not** auto-create a flag —
surface it through the normal carry-over callout instead. Flags are owner-driven.

## Properties

- `Flag` (title) · `Date Flagged`
- `Type` (`💼 Work`, `👨‍👩‍👧 Family`, `💜 Relationship`, `🧠 Internal / Feelings`, `🩺 Health`, `💰 Finances`,
  `🏠 Home / Life`, `🎨 Hobby / Creative`, `🤝 Friends / Social`, `Other`)
- `Importance Level` (`🚨 Critical`, `⭐ High`, `✨ Notable`, `📌 Reference`)
- `Status` (`Active` default · `Carrying Over` · `Resolved` · `Archived`)
- `Context` · `Why It Matters` · `Resolution / Outcome` (text)
- `Related Task` / `Related Project` (relations) · `Journal Digest Link` (url) · `Tags` (multi, optional)

Always set `Journal Digest Link` to this run's run-log entry URL (provenance — see `databases.md` §2) so
the source context is preserved.

## Lifecycle

- **New flag** in today's journal → create row, `Status = Active`, include in tomorrow's carry-over.
- **Still relevant, already in carry-over** → `Status = Carrying Over`, keep in the callout.
- **Handled** (action taken / decision made / resolved) → `Status = Resolved`, fill
  `Resolution / Outcome`, remove from the callout.
- **No longer relevant** → `Status = Archived`, remove from the callout.

## Carry-over requirement

Every `Active` or `Carrying Over` flag MUST appear in the next day's carry-over callout until `Resolved`
or `Archived`, grouped by Type, ordered Critical → High → Notable → Reference, with a "carrying since
<date>" nudge once it's been carrying 3+ days. See `clear-and-carryover.md` for the exact line format.
