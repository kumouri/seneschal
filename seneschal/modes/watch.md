# Watch mode — the cheap comms-peek gate (headless)

This file is read **only when the router in `../SKILL.md` dispatches to this mode**. Watch runs on a
cadence, so its bytes are hot-path — the detail lives in the leaves named below.

**Read this whole file before acting, then work its steps in order.**

---

**Advisors:** *trimmed* — `[orientation(light), oikonomos(envelope-only), gate, prioritize(push)]`. No
heavy Retrieval; Trace only on escalation (the escalated mode owns the trace); on escalation push only
the **vital few**. Oikonomos still contributes its lightweight budget-envelope line even in the trimmed
chain — a cheap peek is exactly the path a runaway push-rate cap needs visibility into
(`references/advisor-chain.md`).

Watch is the periodic **comms peek**: a short, cheap pass (run on a **cheap model**, Haiku-tier) that the
presence daemon spawns on a cadence (`--peek-interval-min`, default ~5 min; 0 = off) to glance at comms
and decide whether anything warrants the full brain. (Live Telegram chat is *not* Watch's job — the
presence daemon handles that directly in Chat mode; Watch only covers email/Slack/calendar.)

On a peek, Watch:

1. **Looks *lightly*** at unread/flagged email, Slack DMs/mentions, and imminent calendar — not a full
   triage.
2. If something is genuinely hot, **escalate to Triage** (the owning subagent) or push the owner a short
   Telegram nudge (act-low). Otherwise let it wait for the next Brief.
3. **Exit cheaply** when nothing warrants the full brain. Watch never sends outbound to third parties
   (ask-high); it writes a Run Log entry only when it escalates into substantive work — the escalated
   mode owns the trace.

Watch is a gate, not a doer — keep it short. Model tier and cadence are knobs on the presence daemon
(`scripts/presence.py --peek-interval-min` / `--watch-cmd`).

## Thread reconciliation needs the source fields

**Escalating something read from email?** Pass `--source-sender` (its From), `--source-subject` (its
Subject) and `--source-received-at` (its Date, passed through verbatim exactly as read — never compute
or convert a timezone by hand) to `telegram_send.py` alongside `--text`. These are fields already in
hand from reading the email; `watch_reconcile.classify` uses them to ask whether the source itself later
resolved the fact. Omit any one and reconciliation reads UNKNOWN (`references/reminders-policy.md` →
"Thread reconciliation").

**Never invent these for a Slack or calendar finding** — neither has an email thread to reconcile
against. Pass none of the three flags for a non-email escalation.

## The source fields are ALSO the fact's identity

`--source-sender` + `--source-subject` do a second job: they are what the escalation is **keyed on** —
`reminders_acks.escalation_fact_key` = sender + normalized subject + account token, derived from the
EMAIL, never from the prose you wrote. Every re-escalation of one sender's alert family shares that key
however you re-word it, so the runtime ack (`watch_ack.ack_blocks`) and dedupe both recognise the
family. **Omit them and the key is a bag of your own words** — which is exactly how one recurring alert
mints dozens of distinct keys and re-escalates after the owner has already said it's handled. So: for
an email-backed escalation the two flags are **not optional**, and the subject is the source's, passed
verbatim, never paraphrased.

**When the owner answers an alert — a chat turn, not this peek — the ack goes to `watch_ack.py`, with
the key of THAT alert.** The warm session sees `(replying to: "<the alert>") <their words>` when they
swipe-reply; it MUST run `python scripts/watch_ack.py ack "<their words>" --replied-to "<the quoted
alert>"` (or `--key <fact_key>` if it already holds the key) in that turn. Their words alone will
usually refuse — an exasperated "I know, stop" names no fact, and the refusal is correct — and **a
carry-over note is NOT where the watcher looks**: `state/watch-acks.json` is the ONLY store this peek's
gate consults (`chat.md` rule 15).

## Reminders are NOT Watch's lane

**Never escalate a Reminders row.** The queue has its own delivery path (`sentinel.check_reminders`),
its own fire-time ack gate, and its own presence/quiet/stagger rules. A peek that reads the Reminders
domain and pushes what it finds is a **second sender with none of that** — it sees `importance` and
`nag_until_done` but not `last_acknowledged`, not `status`, not `state/acks.json`. If a reminder
genuinely warrants attention, escalate to **Reminders** mode; don't push it yourself.

**And this rule is not carried by this file alone.** `telegram_send.py` refuses (exit 3) any send from a
Watch surface that chases a reminder row acked today — a cheap Haiku one-shot is exactly where prose
intent goes quiet. It **fails open**: what it can't determine still goes out. Read it as a backstop,
never as permission to push reminders and let code sort it out. The title-match mechanism:
`references/reminders-policy.md` → "The second sender (the Watch-peek ack gate)"; code in
`scripts/reminders_acks.py`.

## CI status is NOT Watch's lane either

A peek's stated scope is email, Slack DMs/mentions, and imminent calendar — **never CI.** A peek that
wanders out of that lane and scans recent commits by eye will conflate *"some recent commit has a
failed run"* with *"the integration branch is red"* — two different questions — and can name a commit
that was never on `develop` at all (the old head of a still-open PR) or one already fixed before it
reached `develop`'s tip. The second question already has owners: the daemon's resident PR watch
(`scripts/pr_sweep.py`) and `scripts/watch_pr.py`'s `classify`, the one CI-rollup verdict this repo
trusts.

**A commit may never be described as "on develop" from a `git log` scan.** `git merge-base
--is-ancestor` is cheap and it is not optional — `scripts/develop_ci_status.py`'s `is_ancestor` is that
check, and it returns `True`/`False`/**`None`** (unanswerable, never read as `False`).

**If a peek ever has reason to say anything about `develop`'s own CI, it must call
`scripts/develop_ci_status.py`'s `develop_ci_verdict` and quote its `verdict` verbatim — never restate
the question from memory or from a commit scan.** `green`/`pending`/`unknown` are never an alert
(`should_alert_ci_red` only ever returns `True` on a measured `red` tip); `unknown` is a real answer,
not a value to guess past. Absent that call, Watch has no CI opinion at all, which is the correct
default.

## Router advisor (Watch's sibling — the inbound-chat front door)

The presence daemon also runs a **front-door Router** (`scripts/router.py`, a small local Ollama model —
same daemon-cheap-model shape as Watch) that classifies each **inbound chat message** *trivial* vs
*escalate* **before** the warm session spins. **Phase 1 is shadow-only:** it just **logs** its verdict
to `state/router-log.jsonl` and changes nothing — every message still escalates. It's gathering
accuracy evidence so a later phase can (gated on that evidence, the owner's call) handle
clearly-trivial turns locally. Setup + the whitelist + how to read the log: `scripts/ROUTER_SETUP.md`;
full spec in `references/advisor-chain.md`.
