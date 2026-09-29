# Spec — the hung turn that replays forever

**Status:** `BUILT(P1, P2; P3 is deliberately nothing)` — the per-turn idle-gap deadline (P1) and the
bounded control hold (P2) both ship in the daemon. P3 is deliberately nothing. §4's three questions
are all decided, and the two decided last both confirmed the shipped default, so nothing about how
the daemon behaves changed when they were settled.
**Owner:** `seneschal/scripts/presence.py` — the warm session's `_read_until_result` (the
claude-cli backend's `WarmSession`, in `seneschal/scripts/backends/`), `drainer_task`,
`control_task`. Companion authority: [asyncio-daemon-design.md](asyncio-daemon-design.md) (the
reactive core and its invariants). Tests: `seneschal/scripts/test_presence_hung_turn.py`.

---

## 1. What's actually broken

The symptom is "a prompt takes too long, never finishes, and comes back every time." The cause is
narrower than that: **a warm turn had no deadline anywhere in the stack.**

Verified in code, not inferred:

1. **`WarmSession._read_until_result`**
   ```python
   for raw in self.proc.stdout:  # blocks line-by-line until this turn's result event
   ```
   No timeout, no deadline. If the child never emits a `result` event and never closes stdout, this
   blocks **forever** on a worker thread.

2. **`drainer_task`** wraps the send in `state.session_busy = True` / `finally: state.session_busy =
   False`. The `finally` never runs, so the flag is stuck on.

3. **The message never leaves the queue.** It stays at `state.pending[0]`.

4. **The poison-pill counter can't advance.** `attempts` is incremented and persisted *before* the
   send — which is correct design, and is exactly what makes a mid-turn *kill* recoverable. But on a
   hang the loop never comes back around, so the counter advances **only once per daemon boot**. With
   `MAX_TURN_ATTEMPTS = 3` that means the message needs **three separate restarts** to dead-letter.
   Between them, every boot re-sends the same prompt and hangs again. **That is the "repeating
   forever" the owner sees** — not an unbounded loop, a loop clocked by restarts instead of by seconds.

5. **The bigger blast radius: it blocks deploys.**
   `chat_idle()` is `session is None and not pending and not session_busy` — all three are false
   forever. `control_task` held a `defer_until_idle` control while `not chat_idle()`, with **no
   maximum hold**. So a single hung turn stalled every graceful restart indefinitely:
   `seneschald-update` kept ff-pulling and enqueueing reloads that never applied. Path A silently stops
   deploying — the same failure class as a daemon quietly parked off `main`, which can go unnoticed
   for well over a day.

## 2. What already works — do not rebuild it

- The **poison-pill guard and dead-letter path** are correct and well reasoned. This spec makes the
  counter reachable; it does not replace it.
- **Counting the attempt up front, before the risky send**, is right. Kept.
- **Rolling the count back on a delivery failure** is right — a Telegram outage must never dead-letter
  a good message. That asymmetry is kept **exactly as it was**; nothing in this change touches it.
- The **"session died mid-turn"** branch already resets the session, apologises, and re-enters the
  loop cleanly. **The fix routes into this path rather than paralleling it.**

## 3. The fix

### P1 — a per-turn deadline (the whole fix, really)

Bound the read. On expiry, kill the child; stdout closes, the `for` loop ends, `_read_until_result`
returns `None`, and the **existing** mid-turn-death path takes over. A timeout becomes an
already-tested outcome rather than a new branch.

**The load-bearing design call: an idle-gap deadline, not a total-duration cap.** A legitimate turn
can run long — tool use, a big read, a Fable delegation. What a hang looks like is not *slow*, it is
**silent**. So the deadline resets on **every** stream event, and trips only when the *gap* between
events exceeds it. A flat total cap would kill exactly the good long turns.

**As built:** `TURN_IDLE_GAP_SEC = 600.0` (§4 Q1), and `_TurnWatchdog` — one short-lived daemon thread
per turn, armed at the top of `_read_until_result`, beaten on every line, cancelled in a `finally`.

- **Why a watchdog and not a reader thread + `queue.get(timeout=…)`.** `for raw in self.proc.stdout`
  cannot be interrupted from outside, so the alternative is restructuring the read loop — and that
  loop's ordering is load-bearing and already cost a bug once (the turn's own numbers are booked
  *before* the tee, or every metrics row describes the state before the turn it claims to describe).
  Killing the child instead produces the closed-stdout condition the loop already handles. The draft
  preferred this option; building it found no reason it was wrong.
- **One deliberate widening of the draft's wording: the beat is on every non-empty LINE, not only on
  every parsed event.** The question the watchdog asks is *"is the child still producing output?"*,
  which lines answer and parsing does not — a line we failed to parse is still proof of life. For the
  real CLI (stream-json and nothing else) the two sets are identical; the difference only ever runs in
  a healthy turn's favour, which is the constraint that binds this whole change.
- **A beat is a single scalar store**, deliberately. A `threading.Timer` reset per event would spawn a
  thread *per event* — a real cost levied on exactly the long healthy turns this design protects.
- **Every failure path in the watchdog is a no-op.** A thread that won't spawn, a `kill()` that
  raises, a log that throws: all swallowed, and the read goes on exactly as it did before. A broken
  watchdog must never cost a turn. `gap_sec <= 0` disables it entirely.
- **The `send()` fallback ladder is suppressed on a timeout.** Both rungs — stale resume id, a model
  the CLI won't spawn — exist for causes that fail in ~3 s, and a ten-minute silence is evidence
  against each. Re-sending the prompt would buy two more full deadlines of hanging (30 minutes at the
  shipped gap) before the drainer ever learned the turn was in trouble. A timeout goes straight back
  to the drainer, which owns retry policy.
- **A timeout gets its own respawn reason**, `RESPAWN_TURN_TIMEOUT`, and is **not** in
  `RESUMABLE_REASONS` — a hung session is the one whose state there is the most evidence against
  restoring.

### P2 — bound the control hold

`control_task`'s `defer_until_idle` gets a maximum hold, after which it applies anyway. A deploy that
never lands is worse than a turn that gets cut short. Independent of P1 and worth having even after
it: P1 shrinks the window, P2 removes the class.

**As built:** `CONTROL_MAX_HOLD_SEC = 30 * 60.0`, with `DaemonState.control_held_since` stamping when
the hold began and clearing when the queue empties.

- **Why 30 minutes and not something near P1's 10.** It has to sit far enough above
  `TURN_IDLE_GAP_SEC` that it never decides the fate of a turn P1 already governs. There is a concrete
  ordering behind that, not just tidiness: `asyncio.run` waits for its default executor at exit, so a
  restart requested while a worker thread is still parked on a wedged child would block on the way
  out. By 30 minutes any watchdog-killed turn has long since returned and released its thread. P2 is
  for the class P1 *cannot see* — a queue that never drains, a drainer that never comes back.
- **It is a new trigger, not a new capability.** Applying mid-turn is the same path a
  `defer_until_idle: false` control has always taken, and the durable action queue already survives a
  restart, so the cost is at most the turn in flight. A deploy that never lands has no such backstop.

### P3 — nothing

Once P1 makes the turn return, the drainer's existing loop increments `attempts` naturally on the next
pass. **Do not add a second counter.** Recorded only so nobody adds one.

## 4. The questions, and how they were decided

The draft asked the owner three, and **all three are decided — nothing in this section is
outstanding.** The two decided last both confirmed the shipped default, so no behaviour changed when
they landed. While they were open, each default was flagged here *and* at its call site and each
stayed a one-line flip — which is what made ratifying them cost nothing rather than a rebuild, and is
the point of writing a lean down instead of quietly shipping it. The rationales below are ratified
rather than assumed.

### Q1 — How long is the idle gap? **Decided: 10 minutes.**

`TURN_IDLE_GAP_SEC = 600.0`. The question was worth asking because this is the one number that decides
whether the fix is a fix or a new bug: long enough that a slow tool-using turn is never cut, short
enough that a wedge isn't an outage.

**What the number does NOT have to carry, and why that matters:** because the deadline is a gap rather
than a cap, 10 minutes is not "the longest turn allowed." A turn that streams an event every 30
seconds for two hours never trips it. The number only has to exceed the longest plausible *silence
inside a healthy turn*, which is a much smaller and much safer quantity to guess at. That is the whole
reason the gap/cap distinction is the load-bearing decision here and the duration is not.

### Q2 — Does a timed-out turn count toward the poison pill, or roll back like an undelivered reply? **Decided: it counts.**

The spec's lean was confirmed, so nothing changes. This is the question whose consequence is the
least obvious of the three, which is why the poison-pill / dead-letter mechanism was explained in full
before it was decided. A timed-out turn keeps its already-counted attempt, the message stays at the
head of the durable queue, and the third hang reaches the existing dead-letter — so
`MAX_TURN_ATTEMPTS` is reachable **within a single boot** rather than being clocked by restarts.

A hang is more often a property of the prompt than of the network, and rolling back would re-create
the "clocked by restarts" problem in miniature: a counter that only a restart can advance is the bug
this spec exists to fix.

**What this implies, spelled out because it is the one place the design has a visible consequence:**
a hung turn is the only *delivered* outcome that does not pop the queue. The owner is told the turn
was cut off, but nothing was answered, so the message is retried. That is what makes
`MAX_TURN_ATTEMPTS` reachable **within one boot** — which is the regression that defines this bug and
the thing the counter was always meant to do. A plain mid-turn *death* keeps its existing behaviour
untouched (apologise, pop): that path could always come back around on its own and was not what broke.

**The rollback on a delivery failure is a different case and is not touched.** A produced-but-
undeliverable reply still rolls back, because a Telegram outage must never dead-letter a good message.
That asymmetry is correct and out of scope.

**One boundary that falls out of leaving it alone, named rather than hidden.** The rollback branch is
reached by *any* undelivered outcome, so a turn that hangs **and** whose timeout notice also fails to
send rolls its attempt back — and a hang concurrent with a Telegram outage would loop without the
counter advancing, which is this bug in miniature under a second, independent failure. It is left
that way on purpose: the alternative is making the rollback conditional on `timed_out`, and that is
precisely the asymmetry kept out of scope. It is also much narrower than it sounds — it needs both
failures at once, P2 still bounds any deploy waiting behind it, and the message stays durably queued
throughout, so nothing is lost while it lasts. If it is ever observed in the wild, the fix is one
condition on that branch, and it should be the owner's decision, not a quiet widening of scope.

**To flip this**, if it is ever revisited: in `drainer_task`'s delivered branch, the `if timed_out:`
block that logs and `continue`s is the whole of it — deleting it restores pop-on-timeout, and the
poison pill goes back to being unreachable for hangs. One block, one place.

### Q3 — Is the owner told, and in what words? **Decided: yes, in the timeout notice's own words.**

`turn_timeout_notice()`'s wording was ratified, kept distinct from the mid-turn-death apology, so
nothing changes.

A timeout previously borrowed the mid-turn-death apology ("Sorry — I hit a snag just now"). That line
describes something that went wrong and *finished*; a hang didn't finish. `turn_timeout_notice()`:

> That one hung — 10 minutes without a word out of me, so I cut the turn off rather than leave you
> sitting there waiting on it.

Three things about it are deliberate. **The duration is derived from the live constant**, so changing
the gap can't leave a notice quoting a number that is no longer true. **It promises nothing** — not a
retry, not a set-aside — because on attempts 1 and 2 the retry is silent, and on the last one the
existing dead-letter says *"had to set it aside"* in its own words immediately afterwards. The draft's
suggested phrasing, *"that one hung and I've set it aside,"* is therefore **split across the two
notices**: at the moment of a timeout it is not yet true, and the set-aside half already existed
verbatim. Neither line can be the one that's lying. (The persona may re-voice the notice; the three
properties are what must survive.)

## 5. Tests

`seneschal/scripts/test_presence_hung_turn.py`. Offline throughout — no real `claude` spawn, no
socket, no network, same posture as the existing presence suites; the fake child is a queue-backed
stdout that blocks exactly the way a pipe does.

The cases the draft named, and where each lives:

| The case | Class | What it would catch |
|---|---|---|
| A long but **live** turn is never cut | `IdleGapIsAGapNotACap` | The design going wrong: a turn is driven **6× past its own deadline** with events arriving steadily and must survive. It also asserts the elapsed time, so the test can't silently degrade into a fast one that proves nothing |
| A hang trips the deadline and the turn **ends** | `HangingTurnEnds` | The read blocking forever — the bug itself |
| `attempts` advances **within one boot** | `HungTurnRouting` | The regression that defines this bug: a counter that only a restart can move |
| Three hangs → dead-letter **exactly once** | `HungTurnRouting` | Both a missing dead-letter and a duplicated one |
| A `defer_until_idle` control held past the cap applies **while a turn is in flight** | `ControlHoldIsBounded` | Path A silently not deploying |

Plus, because each is a way this change could quietly cost something: the queue asymmetry pinned in
*both* directions (a hang keeps its message, a death still pops); the fallback ladder not firing on a
timeout; `timed_out` resetting per send; an unparseable line still counting as life; every watchdog
failure path being a no-op; a zero gap disabling it; and a normal turn at the shipped 10-minute gap
behaving exactly as before.

## 6. What this change does not do

- **It cannot make a healthy turn worse.** That was the binding constraint. With no hang, the only
  difference is one parked thread and one scalar store per line; the read loop, its ordering, and its
  return values are untouched. The gap/cap decision, the beat-on-line widening, and the 30-minute
  choice for P2 all fall out of it.
- **It does not detect a *slow* turn**, only a silent one — by design. A child that streams filler
  forever is a different pathology and is not what this bounds.
- **It adds no dependency and no config surface.** Two module constants, stdlib only.

## Router entry

**Router status:** **P1+P2 BUILT; §4 fully decided — Q2 and Q3 both confirmed the shipped default**.
**What it decided:** **A gap between stream events, never a cap** — a hang is silent, not slow; plus
the bounded control hold.
