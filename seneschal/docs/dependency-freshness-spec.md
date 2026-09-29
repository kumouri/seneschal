# Spec — dependency and tool freshness: watch what rots, report it, never upgrade it

**Status:** `SPEC-ONLY` — **nothing built.** There is no freshness checker in `seneschal/scripts/`, no
roster file, and no Dependabot config; no behavioural file is touched by this spec.

**Scope (if built):** one new stdlib module, its test module, one tracked roster file, one line
appended to an existing Dream step, and one entry in the scripts index. **No new dependency. No format
change. No migration. NO CHANGE TO THE APPROVAL GATE — §5 is an invariant, and §10's Q3 is the only
place the gate could ever move, which is why it is left open for the owner.**

**Parent, and this spec does not re-argue it:** an evaluation memo of an external agent prompt (not
shipped in this tree) whose `EXTERNAL INTELLIGENCE LOOP` section was read in full and established three
things this spec builds directly on top of, and re-derives nowhere: **(a)** the external version is
*prose instructing a downstream agent to build a scheduled sweep* — no cron, no feed list, no fetcher,
no code; **(b)** the assistant has **nothing equivalent**, grepped and confirmed, with
no Dependabot config and no tracked install or version pin for the tool that broke; and **(c)** the
idea splits in two, and **only the cheap half is worth building** — ecosystem-watching is expensive,
unverifiable and decays unattended, which the source repo of that prompt demonstrates by having gone
months without running its own. **Dependency freshness is the mechanically-checkable half, and it is
the half that actually broke.** §9 below holds the other half out of scope on the record, so a later
reader does not mistake omission for oversight.

**The ask:** the owner sent a video link. The transcription pipeline failed with a persistent HTTP 403
on the media fetch, three identical times. The cause was not the video host and not a stuck player
client: `yt-dlp` on the host was seven weeks old. Nothing was watching, and nothing could have been —
the tool appears in no lockfile and no manifest in this repository. Build the thing that would have
said so first.

---

## The short answer, up front

- **The category that broke is the one no manifest can see.** `yt-dlp` was a `uv` **tool**, installed
  inside WSL under `~/.local/share/uv/tools`, and it appears in no `pyproject.toml`, no `uv.lock`, no
  `package.json` and no Gradle file. §2.3 goes one step further than the brief did: the script that
  *invokes* it was not in the repository either. **A watcher that derives its roster by scanning the
  tree finds nothing.** The roster must be an explicit tracked list, and that is a conclusion from
  the incident rather than a preference.
- **Three obvious staleness metrics, and the incident refutes two of them.** On the morning of the
  break `yt-dlp` was **1 release behind**, that release had been out for **~1.5 days**, and the
  installed build was **48 days old**. A threshold on *releases behind* or on *lag* would have had to
  be set at essentially zero to fire — i.e. would have become a release notifier, the maximum-noise
  design. **Only the age of the installed artifact separates the incident from a quiet day**, by a
  factor of 32. §3.2.
- **But age alone nags during a drought, so the rule is a conjunction.** Measured over `yt-dlp`'s
  last 30 releases, the longest interval with no release at all is **83 days**. An age-only rule
  would have nagged for twelve weeks about a version that was, throughout, the newest one in
  existence. **Flag only when the installed build is older than its threshold AND something newer
  actually exists.** §3.3.
- **A row must have an upgrade path or it must not exist.** Measured: `ffmpeg` from the distro's apt
  repository reported an installed version identical to apt's Candidate. Reporting it against
  upstream's newer major produces a permanent, unactionable line. That is not a threshold problem; it
  is an enrolment criterion. §2.4.
- **It reports. It never upgrades.** §5 is an invariant, not a default. The incident's upgrade
  happened because the owner explicitly asked for it. An auto-upgrade mode is a change to the approval
  gate and is the owner's alone to decide; this spec does not design one.
- **Phase 1 is the roster plus a version check, folded into the Dream digest, with zero Telegram
  pushes.** Over-notification is a known, live failure class in this assistant (reminders re-firing
  many times a day is the familiar shape), so a new push channel is not something this spec grants
  itself. §10 Q1.

---

## 1. The incident, read rather than remembered

### 1.1 What is verified, and what is the owner's account

Everything in the first table was **[measured]** by read-only probe of the host and of upstream release
APIs on the day of the incident; the commands are in §12. The failure narrative — three identical 403s,
the six-client probe, the upgrade and the first-try success afterwards — is **the owner's account plus
the session that ran it**, and is marked as such. This spec does not re-run the break.

| Fact | Value | Source |
|---|---|---|
| Installed `yt-dlp` at time of failure | a date-versioned build (`<yyyy>.07.04`) | the owner's account; corroborated by the upgrade that was approved |
| That release's age at the break | **48 days** | [measured] `gh api repos/yt-dlp/yt-dlp/releases` |
| Newest release that morning | `<yyyy>.08.19` | [measured] same |
| Its age at the break | **~1.5 days** | [measured] same |
| Releases between the two | **none — exactly 1 behind** | [measured] same |
| Installed `yt-dlp` after the fix | the newest release | [measured] `yt-dlp --version` under a WSL login shell |
| Where it lives | the owner's WSL distro, user-level `~/.local/share/uv/tools` | [measured] `uv tool dir`, `uv tool list` |
| Player-client probe (6 clients, same URL, same minute) | `default`/`android_vr` → 403; `tv` → reload; `web_safari`/`ios`/`mweb` → format unavailable; `web_embedded` → bytes | the owner's account / the session that ran it |
| After `uv tool upgrade yt-dlp` | `default` client fetched first try | the owner's account |

The last two rows are the diagnosis: **six clients disagreeing is what staleness looks like from the
inside**, and a single stuck client would not have been repaired by a version bump.

### 1.2 The pipeline's own tolerance is what hid it

The pipeline's garbage-collector module (tracked in the source repository, merged well before the
incident) carried a docstring noting that the video host hands out intermittent 403s on the media URL,
so a re-run inside the hour is a real case — which is why the intermediate audio is kept for an hour
rather than deleted on success.

That sentence is correct. It is also, exactly, the reason a **persistent** 403 read as noise for three
attempts. **The system's documented tolerance for a transient fault is the thing that makes the same
fault, made permanent, invisible.** A freshness watcher does not fix that; it supplies the second
signal — *the tool you are running is seven weeks old* — that turns an ambiguous error into a
diagnosis. This is the same shape as [`background-jobs-spec.md`](background-jobs-spec.md) §3.11, where
a job's `exit 0` had to be read against its own log because neither datum was sufficient alone.

### 1.3 Why "seven weeks stale" is not hyperbole for this particular tool

`yt-dlp`'s job is tracking a hostile, deliberately-moving target. Its release history says so:
**[measured]** over its last 30 releases (roughly seventeen months) the median interval is **16 days**
and the minimum is **1 day**. A project that sometimes ships twice in 48 hours is not shipping
features at that cadence; it is shipping repairs. Seven weeks of that is not "slightly behind."

---

## 2. Inventory — what is even being watched

### 2.1 The surfaces, and which of them a watcher can already see

| # | Surface | Manifest | Pinned by | Is it *already* watched? |
|---|---|---|---|---|
| 1 | Daemon runtime | `pyproject.toml` + `uv.lock` | `uv sync --frozen` in `seneschald-control.ps1 -Action Update` | **Held still on purpose.** Exactly two runtime deps (`websockets>=15,<17`, `tzdata`). |
| 2 | Cockpit extras | `pyproject.toml` `[project.optional-dependencies].cockpit` | same lockfile; installed only with `--extra cockpit` | Same. `fastapi`, `uvicorn`. |
| 3 | Dev group | `[dependency-groups].test` | same lockfile; never in a `--frozen` prod sync | Same. `httpx`. |
| 4 | `cockpit/web` | `package.json` + lock | npm ranges (`react ^18.3.1`, `vite ^5.4.0`, `typescript ^5.6.0`) | No. |
| 5 | `phone/` | `package.json` + lock, `wrangler.toml` | wrangler + Cloudflare `workers-types` | No. |
| 6 | `phone/android` | `build.gradle.kts` + wrapper | AGP / Kotlin / `compileSdk 36` | No. |
| 7 | **Host-side tools in no manifest at all** | **none — this is the category** | nothing | **No. This is the incident.** |

Rows 1–3 deserve a note that keeps the spec honest: **`seneschald-update` runs `uv sync --frozen` after
every ff-pull, which is the mechanism that *keeps* those dependencies where they are.** They are not
unwatched by neglect; they are pinned by design, and a lockfile is a deliberate refusal to drift. A
freshness watcher pointed at them is answering a question nobody asked. §8's phasing puts them last
for that reason, and §10's Q2 leaves whether they are ever in scope to the owner.

### 2.2 Row 7 in full — and it is first-class, not an appendix

**[measured]** on the source host, read-only, both namespaces. Versions are omitted — they are one
machine's snapshot — but the shape is what every install of this framework on Windows + WSL will
share:

| Tool | Namespace | Install mechanism | In any manifest? |
|---|---|---|---|
| `yt-dlp` | WSL | `uv tool`, `~/.local/share/uv/tools` | **no** |
| `ffmpeg` | WSL | apt, the distro's universe repository | **no** |
| a whisperx venv | WSL | plain venv under the user's home | **no** |
| `uv` | WSL | standalone installer | **no** |
| `uv` | **Windows** | standalone installer | **no** |
| `deno` | WSL | standalone, `~/.deno/bin` | **no** |
| `gh` | Windows | winget/choco | **no** |
| `git` | Windows | installer | **no** |
| `claude` CLI | Windows | self-updating installer | **no** |
| `python` | Windows and WSL | two installs | **no** |
| `node` | Windows (absent in WSL) | installer | **no** |

Two things fall straight out of that table and both shape the design.

**A tool's identity is `(namespace, name)`, never `name`.** On the measured host `uv` was one minor
version apart across the WSL boundary — the Windows copy ~100 days old, behind its own Linux twin on
the same machine — and `python` differed by two minor versions. A roster keyed on the bare name would
collapse two different installs, on two different upgrade paths, into one row and report whichever it
happened to find first. This is [`background-jobs-spec.md`](background-jobs-spec.md) §3.9.1 recurring
in a new place: *the preflight checked the path from Windows while the command was about to ask WSL.*
The namespace is not a detail; it is half the key.

**A probe run under the wrong shell reports NOT INSTALLED for an installed tool.** [measured] Under
`wsl -u <user> -- bash` (non-login), `command -v yt-dlp` fails and the honest answer is
`<not on PATH>`; under `bash -lc`, the same tool answers with its version. `~/.local/bin` and
`~/.deno/bin` are added by the login profile. **This is a false negative that a naive watcher would
render as a reassuring blank**, which is worse than silence — §6.4 makes it a named verdict instead.

### 2.3 The finding the brief did not have: the *caller* is unmanifested too

The brief established that `yt-dlp` is in no manifest. **[measured]** it was worse than that:

- In the source repository, `git ls-files` found `yt-dlp` in **exactly one** tracked file — a
  context-pointer ledger row that existed **to record its absence**, a reviewed row whose reason said
  a resolving pointer would mean the finding was wrong. (In this tree it appears only as a name in
  `jobs.py`'s list of known long-running commands — a label, not an install or a version.)
- The pipeline's garbage collector named a shell script as the pipeline's driver. That file was
  **neither tracked nor gitignored — it was not in the repository at all.** `git ls-files
  --error-unmatch` errored; `git check-ignore` returned nothing.

So the failing pipeline consisted of an untracked driver invoking an unmanifested tool, with only its
*garbage collector* tracked. **No scan of the tree — of manifests, of lockfiles, or of source for
`subprocess` calls — can enumerate what to watch, because neither the tool nor its caller is there.**

That closes the enrolment question by elimination rather than by taste. **Enrolment is an explicit
tracked list.** It is the only mechanism that survives the founding incident.

### 2.4 Enrolment criteria — a row must earn its place, twice

A roster that accretes every binary on the machine becomes the noise problem in a different costume.
Two tests, and **both** must pass:

> **(a) Staleness has a plausible breaking mode.** The tool's correctness depends on something
> outside this machine that moves — a hostile web API, a remote schema, a signing rule.
>
> **(b) This host can actually take the upgrade.** There exists a command that moves the installed
> version, and it is one the owner could run today.

**(b) is not hypothetical.** [measured] `apt-cache policy ffmpeg` reported `Installed:` and
`Candidate:` as the identical string. An LTS distro ships one `ffmpeg` major for its whole life and
will keep shipping it. Measured against upstream's newer major, `ffmpeg` would produce a row reading
*"a major version behind"* every single night, forever, with the only honest remediation being
*"change distributions."* **A row whose answer to "how do I fix this" is "you can't" is noise by
construction** — which is why §6.2 requires every line to carry its own upgrade command, and why a
tool that cannot produce one is refused at enrolment rather than filtered at report time.

Applying both tests to §2.2:

| Tool | (a) breaks when stale? | (b) upgrade path? | Enrolled? |
|---|---|---|---|
| `yt-dlp` (WSL) | **yes** — adversarial target, the founding incident | `uv tool upgrade yt-dlp` | **yes** |
| `gh` (Windows) | partly — GitHub's API deprecates, slowly | `winget upgrade GitHub.cli` | **yes**, loose threshold |
| `uv` (Windows) | no — resolver, not a network client | `uv self update` | **yes**, loose threshold |
| `uv` (WSL) | no | `uv self update` | **yes**, loose threshold |
| `deno` (WSL) | no | `deno upgrade` | proposed **no** — §10 Q2 |
| `ffmpeg` (WSL) | no — local codec work | **none** (apt Candidate == Installed) | **no**, fails (b) |
| whisperx venv | no — local model inference | `uv pip install -U` in that venv | proposed **no** — §10 Q2 |
| `claude` CLI | no | self-updating; the version moves under the probe | **no**, §2.6 |
| `git`, `python`, `node` | no | installer-managed, coupled to everything | **no** |

### 2.5 A frozen component is the case for reading status before reading a manifest

The source repository carried a superseded component — an SDK-based runner replaced by the
subscription CLI daemon, kept functional and explicitly marked *don't invest there* — whose
`package.json` pinned an agent SDK that was certainly stale. Reporting its drift would have been
technically true and operationally worthless. **Enrolment is a judgement about whether the answer
would change anything, and that judgement is not derivable from the manifest.** One more reason the
roster is hand-written.

### 2.6 Why the fastest-moving tool on the host gets no row

**[measured]** the `claude` CLI's upstream released **30 times in about five weeks** — median interval
**0 days**. Under any age threshold expressible in days, that row is red permanently. It also
self-updates, so the number the watcher reports is one it does not control and cannot predict. **The
tool that moves fastest is the one where freshness is least worth watching**, which is the cleanest
available demonstration that *rate of change is not the enrolment criterion* — §2.4(a) is.

---

## 3. What "stale" means, and the NOISE problem

### 3.1 The noise budget is real and it is nearly spent

Over-notification is the standing failure this assistant already fights: reminders that re-fire many
times in one day are the familiar shape, and each is a reason for the owner to stop reading. This repo
has already written the general form down twice: `dream_steps.py`'s docstring on trained deafness, and
`worktree_gc.py`'s *"a worker that [pings nightly about nothing] is worse than no worker."* **A
freshness watcher that speaks every day becomes wallpaper and deserves to.** Every choice below is made
against that constraint, and §10 Q1 refuses to spend the owner's push budget without their decision.

### 3.2 Three candidate metrics — the incident refutes two

For `yt-dlp` on the morning of the break, all three are **[measured]**:

| Metric | Value that morning | Threshold needed to have fired | Verdict |
|---|---|---|---|
| **Releases behind** | **1** | `≥ 1` — i.e. fire on every release | **Refuted.** That is a release notifier. |
| **Lag** — days since something newer existed | **~1.5** | `≤ 1 day` | **Refuted.** Same thing, worse: it fires on *every* release for *every* tool, and `uv` alone would fire ~120×/year at a 3-day median. |
| **Installed age** — days since the running build was published | **48** | anything under ~45 | **Survives.** |

**The lag and the installed age differ by a factor of 32 on the same tool at the same instant**, and
that is the whole argument. The intuitive metrics — *how far behind am I*, *how long has the fix been
sitting there* — both say "barely at all", and both are right, and the pipeline was broken anyway.
**What breaks a tool is calendar time spent running against a target that moved, not the number of
tags upstream happened to cut.** Age of the installed artifact is the only one of the three that
measures that.

This also disposes of a fourth idea worth naming so nobody re-proposes it: **semantic-version
distance** (major/minor/patch). `yt-dlp` versions **are** dates; there is no semantic distance to
compute, and a seven-week jump is a "patch bump" under every parser that would accept it.

### 3.3 Age alone nags through a drought — so the rule is a conjunction

**[measured]** across `yt-dlp`'s last 30 releases the maximum interval with no release at all is
**83 days**. Under an age-only rule with any threshold under 83, the watcher would have spoken every
night for roughly twelve weeks about a build that was, the entire time, **the newest one in
existence** — with no upgrade to offer. That is the wallpaper failure arriving by the front door.

> **THE RULE.** A tool is stale iff **both**:
> **(1)** a newer release exists upstream, **and**
> **(2)** the installed release was published more than `max_age_days` ago.
>
> (1) alone is a release notifier (§3.2). (2) alone nags through a drought. **Neither clause is
> redundant, and each is the other's silencer.**

Checked against the record: during the 83-day drought, clause (1) is false → **silent**, correctly.
On the morning of the break, clause (1) is true and 48 > 30 → **fires**, correctly. The rule is not
fitted to the incident; it is fitted to the incident *and* to the longest quiet stretch in the same
history.

### 3.4 Per-tool, configurable, with a justified default

**Yes, per-tool, and the default is 30 days.** Measured release cadences (each over the upstream's
last 30 non-draft, non-prerelease releases), which is what the numbers are argued from rather than
asserted against:

| Upstream | Releases sampled | Median gap | Max gap | Min gap |
|---|---|---|---|---|
| [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) | 30 (~17 months) | **16 d** | 83 d | 1 d |
| [`gh`](https://github.com/cli/cli) | 30 (~13 months) | 13 d | 35 d | 0 d |
| [`uv`](https://github.com/astral-sh/uv) | 30 (~3 months) | **3 d** | 7 d | 0 d |
| [`deno`](https://github.com/denoland/deno) | 30 (~6 months) | 6 d | 23 d | 0 d |
| [`claude` CLI](https://github.com/anthropics/claude-code) | 30 (~5 weeks) | **0 d** | 9 d | 0 d |
| [`ffmpeg`](https://github.com/FFmpeg/FFmpeg) | — | **no readable releases** (tags only) | — | — |

**Why 30 days as the default:**

1. **It clears the founding incident with margin.** The break was at 48 days; 30 gives 18 days of
   warning — 37% of the way back — rather than firing on the day of the failure.
2. **It is ~2 median release cycles for the tool it was chosen for** (`yt-dlp`, 16 d), which is the
   smallest multiple that is not simply "one release behind" in disguise.
3. **It is expressed in days, not cycles, deliberately.** A cycle-based threshold is refuted by §3.2:
   the incident was one cycle behind. The unit has to be the one that measures exposure.
4. **It bounds the noise arithmetically.** With clause (1) usually satisfied within a median cycle, a
   row fires roughly every `max_age_days` after each upgrade. At 30 days across four enrolled rows,
   that is ~40 digest lines a year — **about one line every nine days, in a file the owner already
   reads**, and zero interruptions. The `ffmpeg` row above is not a gap in the measurement; it is
   §2.4(b) showing up in the version-source layer too, and §6.3 handles it.

**Per-tool overrides, and why each differs from the default:**

| Enrolled row | `max_age_days` | Why not the default |
|---|---|---|
| `yt-dlp` (WSL) | **30** | The default *is* this tool's number; everything else is calibrated off it. |
| `gh` (Windows) | **90** | Nothing breaks when it is old — GitHub deprecates over quarters. 90 d ≈ 7 median cycles. On the measured host it was 86 d old, so it would fire soon, once. |
| `uv` (Windows) | **120** | A resolver, not a network client. At a 3-day median cadence anything tighter is a subscription to `uv`'s changelog. On the measured host it was **101 d** old. |
| `uv` (WSL) | **120** | Same tool, same reasoning, **separate row** — §2.2. |

The threshold is a per-row field in the roster with the 30-day default applied when absent, so
re-tuning a tool is a one-line tracked diff with a reason beside it — the same affordance
`seneschal/context-budget.json`'s `raises[]` gives a byte number, and for the same reason: **the number
and its justification stay in the same file.**

---

## 4. Cadence and owner

### 4.1 It belongs to Dream, and no new scheduler is invented

Read against `modes/dream.md`, [`asyncio-daemon-design.md`](asyncio-daemon-design.md) and the scripts
index, four mechanisms could carry this. The comparison, not the preference:

| Candidate | Fit | Verdict |
|---|---|---|
| **New Task Scheduler entry** | Would work | **No.** The house position, stated in more than one module docstring: no resident process, no new Task Scheduler entry, nothing that can go stale without anyone noticing. Another scheduled task is another thing to be deploy-blind about. |
| **A new daemon task** in `presence.py` | Poor | **No.** The daemon is **reactive by design** — the asyncio design memo is explicit that polling was the rejected alternative. A once-a-day version check has no event to react to, and `worktree_gc.py` was placed in Dream over this exact choice. |
| **Watch mode** | Poor | **No.** *Watch is a gate, not a doer* — comms only, explicitly not system state (memo §2.4). |
| **A Dream step** | **Good** | **Yes.** Dream runs nightly, unattended, after the day is done. It already has a digest the owner reads, an alarm ledger for steps that go stale, and a step-2 precedent for exactly this shape. |

### 4.2 Step 2, beside `worktree_gc`, and not a new numbered step

The parent memo proposed *a new Dream step*. **Reading `modes/dream.md` as it stands argues for
something smaller: an entry inside step 2, not a new numbered step.** Step 2 is already the
housekeeping sweep, and it already carries many items, nearly all of which sweep and **one of which
sweeps nothing at all**:

> *"Read the transcript archive's size — **the one item here that sweeps NOTHING**:
> `python scripts/transcript_size_watch.py check`"*

That sentence is the precedent, and it is exact: step 2 already hosts a pure sensor that deletes
nothing, exits 0 always, and folds into the digest only on a real result. `worktree_gc.py` sits in the
same step on the same reasoning — it reports and folds in only on a real result. **A freshness check is
a third instance of a pattern step 2 already holds twice**, and making it its own numbered step would
assert a difference that does not exist.

There is a cost to name rather than hide: **only steps with an owning script get a live
`dream_steps.py` alarm row**, and an entry inside step 2 does not automatically get one. §8 phase 2
adds the row deliberately rather than assuming it — because the failure this whole subsystem exists to
prevent is *a watcher nobody notices has stopped*, and shipping one without its own staleness alarm
would be the joke writing itself.

### 4.3 Nightly, not weekly — and the reason is cost, not urgency

A weekly cadence is the intuitive answer for a number that moves slowly, and it is wrong here for two
reasons.

1. **The check is nearly free and idempotent.** Five HTTPS GETs and five `--version` invocations. It
   carries no state that a skipped night corrupts and no work that accumulates.
2. **Weekly needs a scheduler that nightly does not.** Dream has no weekly slot; its weekly items are
   *"Weekly, also run…"* prose that a run must remember, which is precisely the improvisation
   `dream_steps.py` exists because of — a skip recorded only in prose, night after night, is how an
   index went unwritten for weeks. **Choosing weekly would mean inventing the cadence mechanism the
   brief forbids inventing.**

**The reporting cadence and the checking cadence are different questions, and separating them is
what makes nightly safe.** It checks nightly; it *speaks* only when §3.3's conjunction holds. A tool
inside its threshold produces no line at all.

---

## 5. INVARIANT — it must not upgrade anything

> **The watcher reports. It never installs, never upgrades, never pins, never edits a manifest,
> never edits a lockfile, and never runs a package manager in any mode other than a read-only
> query.** There is no flag, no config key and no environment variable that makes it act. Its output
> is evidence for a decision the owner makes.

**Why this is an invariant and not a default.** Upgrading a tool changes what runs on the host, which
is squarely ask-high under `references/autonomy-policy.md`. The incident is the demonstration:
`uv tool upgrade yt-dlp` ran **only after the owner explicitly asked for it.** The watcher would have
said *"yt-dlp is 48 days old and a newer release is available"* — and stopped there, which is the
correct amount for a sensor to do.

**Reading is act-low; that is the whole grant.** Running `yt-dlp --version`, `gh api`, `apt-cache
policy` and a PyPI GET is a sensor reading the host's own state and public release metadata. It
matches the grant `transcript_size_watch.py` and `worktree_gc.py` already hold, and it widens nothing.

**A future auto-upgrade mode would be a change to the approval gate, and therefore the owner's to
decide.** It is not designed here, not phased here, and not costed here. §10 Q3 states the question
and stops. If it is ever raised, it inherits the parent memo's warning about the recommendations in
that external prompt which would widen this gate — one of them a verbal-authorization path this
assistant has already declined.

**One consequence worth stating so it is not mistaken for an oversight:** the watcher deliberately
takes no credit for a fix. It does not detect that an upgrade happened, does not close its own row,
and does not remember that it mentioned a tool yesterday beyond the latch in §6.5. A row disappears
because the installed version changed — which is a fact about the host, not a fact about the watcher.

---

## 6. Reporting

### 6.1 Silence is the default, and it follows the existing convention

`worktree_gc.py`'s verdict split is the model, adapted:

| Verdict | Meaning | Digest? | Push? |
|---|---|---|---|
| `fresh` | Inside threshold, or nothing newer exists | **no** | no |
| `stale` | §3.3's conjunction holds | **yes** | §10 Q1 — **no in phase 1** |
| `unknown` | Could not determine installed or latest (§6.4) | **yes, named** | no |

Dream folds the report in **only if `stale` or `unknown` is non-empty.** A run where everything is
current produces `nothing to report`, which goes to the log and nowhere else — the same rule
`modes/dream.md` already states for `worktree_gc`: fold its output in only if it removed or refused
something, because a nightly ping about routine holds is worse than no sweeper.

### 6.2 What a line says when it does speak

Five fields, and the fifth is the one that makes it useful rather than merely true:

1. **the tool** — including its namespace, because §2.2 proved the name alone is ambiguous
2. **the installed version**
3. **the latest version**
4. **the age of the gap** — days since the *installed* build was published (§3.2), with releases-behind
   in parentheses as context rather than as the trigger
5. **how to upgrade it** — a literal command, and its rollback

Shaped as it would have printed on the morning of the break:

```
FRESHNESS — 1 stale, 0 unknown

  yt-dlp  (wsl:<distro>)     <yyyy>.07.04  ->  <yyyy>.08.19
      installed build is 48d old (threshold 30d); 1 release behind, out 1.5d
      upgrade:  wsl -d <distro> -u <user> -- bash -lc 'uv tool upgrade yt-dlp'
      rollback: wsl -d <distro> -u <user> -- bash -lc 'uv tool install yt-dlp==<yyyy>.07.04 --force'
```

**The rollback line is not decoration.** It is the shape the incident session itself produced, and it
is what converts *"you should upgrade"* into a reversible decision: the pin is exact, it names the
version being left behind, and it is copyable at the moment the owner is deciding rather than
reconstructible later from a version string they no longer have. A watcher that reports drift without
naming the way back is asking the owner to take a one-way step on the watcher's word.

**Every command is per-row roster data, never inferred.** Deriving *"it's a uv tool, so
`uv tool upgrade`"* is a guess that will eventually print a command that damages something, and the
roster is hand-written anyway (§2.3) — so the command is typed once, by a human, beside the tool it
belongs to. `bash -lc` is not incidental: §2.2 measured that a non-login shell cannot find this tool.

### 6.3 Where "latest" comes from — per row, and it is not one source

| Install mechanism | Latest from | Note |
|---|---|---|
| `uv tool` (PyPI-backed) | PyPI JSON API | [measured] PyPI reports a date version with the leading zeros stripped (`YYYY.8.19`) where `--version` reports `YYYY.08.19`. **Normalisation is required, and it is the one place a naive `!=` produces a permanent false positive.** |
| GitHub-released binary | `gh api repos/<r>/releases/latest` | Uses the already-authenticated `gh` on the host. |
| apt package | `apt-cache policy` → `Candidate` | **Compares against the distro, not upstream** — §2.4(b). Nothing is enrolled on this path. |
| self-updating | — | Not enrolled — §2.6. |

Publication *dates* come from the same source as the version, because §3.3's clause (2) needs a date
and a bare version string does not carry one. PyPI's `releases[<v>][0].upload_time_iso_8601` and
GitHub's `published_at` both supply it.

### 6.4 Unknown is a named verdict, never a blank

§2.2's measured false negative is the reason this is a rule rather than an error path. A row resolves
to `unknown` — reported, not silently dropped and **not** rendered as fresh — when the tool is not on
the probe's PATH, the version output does not parse, the network is unavailable, or the upstream
query fails or times out. **A watcher that renders "I could not look" as "everything is fine" is the
worker that recorded nothing about what it didn't find** — a failure shape this repo has already paid
for, where an empty result read as a quiet day.

`unknown` is reported but never pushed, because being offline overnight is routine and is not a fact
about staleness.

### 6.5 Never cost a Dream run

Inherited wholesale from `transcript_size_watch.py`, and not re-argued: every entry point is wrapped
and returns a verdict; nothing raises into its caller; the process always exits 0; a missing roster,
an unreachable network and an unparseable version are each a quiet verdict rather than a failure.
**A sensor that can break the nightly run it rides on is a worse bug than the staleness it watches.**

If §10 Q1 ever authorises a push, it inherits the rest of that module's contract too — **latch per
crossing** (a fired row goes quiet until the version or the threshold changes), and **the marker is
written only after the send lands.**

---

## 7. Evidence — what this would have said on the morning of the break

Every installed version below was **[measured] on the source host**; every upstream figure is
**[measured]** from the release APIs. Ages are computed to the morning of the break. `yt-dlp`'s
installed version is the owner's account of the pre-upgrade state (§1.1); the rest are as they stood,
because nothing else was touched that day. Versions are shown only where the shape matters.

| Row | Age of installed build | Newer exists? | Threshold | **Verdict** |
|---|---|---|---|---|
| **`yt-dlp` (wsl)** | **48 d** | **yes** (1.5 d out) | 30 d | **STALE — FIRES** |
| `uv` (windows) | 101 d | yes | 120 d | fresh (fires at 120 d) |
| `uv` (wsl) | 7 d | no | 120 d | fresh |
| `gh` (windows) | 86 d | yes | 90 d | fresh — **fires in 4 days** |

**The founding incident fires, with 18 days to spare.** Clause (1) was satisfied from the moment the
newer release was published; clause (2) had been satisfied for 18 days before that, since the installed
build turned 30 days old. So the row went stale the moment the newer release was published, and the
morning report — the first Dream digest after that publication — carries it. **The break happened the
next day. The line was available the day before.**

**And the report is one line long.** Not four, not twenty-two. The other three enrolled rows are
inside their thresholds and produce nothing, which is the noise policy of §3 doing its job on real
data rather than in the abstract.

### 7.1 The uncomfortable case, stated rather than smoothed over

Two rows above are close to their thresholds, and one of them crosses within the week: **`gh` fires in
four days** and `uv` (windows) at 120 days would fire about nineteen days later. **These are exactly
the rows §2.4 called "loose threshold" and nothing about them is urgent.** So the first month of
operation produces roughly three lines, two of which are shrugs.

That is the honest cost, and it is the argument for phase 1 being digest-only. **A shrug in a file the
owner already reads costs nothing; a shrug that buzzes their phone costs the whole subsystem.** If after
a month the shrug rows are pure noise, the fix is a one-line threshold raise with a reason beside it
(§3.4) or un-enrolling them (§2.4) — both cheap, both tracked, both reversible. §8's phase 1 exit
criterion is that measurement rather than a prediction of it.

### 7.2 Did the thresholds get fitted to the answer?

A design tuned on one incident will pass that incident by construction, so the check has to come from
outside it. Three tests it was not fitted to, and it survives all three:

1. **The 83-day drought** — **silent**, by clause (1). Not a case the threshold could have been tuned
   for, because no threshold on age would have passed it.
2. **`claude-code`, 30 releases in about five weeks** — **not enrolled**, by §2.4(a), not by
   threshold. A design that had to defend itself with a large number here would be admitting the
   criterion was wrong.
3. **`ffmpeg`, permanently behind upstream with no upgrade path** — **not enrolled**, by §2.4(b),
   measured from `apt-cache policy` rather than assumed.

**Where it does remain fitted, and this is a real limitation:** `max_age_days = 30` clears 48 with
margin, but the margin was chosen knowing 48. A second independent breakage would test it properly,
and there is exactly one data point. §8's phase 1 does not claim otherwise — it records what fires
and lets the second incident, if it comes, be the calibration.

---

## 8. Phasing — the smallest useful version ships alone

**Phase 1 is the unmanifested roster plus a version check.** The brief's guess is right, and §2.3 is
why: that category is the only one where nothing else is watching, and it is where the incident lives.

| Phase | What ships | Depends on | Exit criterion |
|---|---|---|---|
| **1** | A `freshness_watch.py` in `seneschal/scripts/` (stdlib, report-only) + its test module + **a `tool-freshness.json` roster** (a new file under `seneschal/references/`), tracked, seeded with the four §7 rows + one line appended to `modes/dream.md` step 2 + one entry in the scripts index + a `references/CLAUDE.md` router row for the roster. **Digest only. Zero pushes.** | nothing | **A month of digests read.** Count the lines, and how many were worth reading. That number decides Q1 and re-tunes §3.4 if it needs it. |
| **2** | A `dream_steps.py` alarm row so the watcher's own staleness is visible (§4.2). | phase 1 having an owning script that can stamp it | `status` shows a real age for the row. |
| **3** | **Only if Q1 says yes:** one Telegram push on a newly-crossed `stale` row for a tool whose roster entry marks it as breaking-when-stale. Latched per crossing, marker after send. | **Q1 decided** | — |
| **4** | **Only if Q2 says yes:** the manifest surfaces — `uv pip list --outdated`, `npm outdated` per workspace. | **Q2 decided** | — |

**Phase 1 is genuinely standalone.** It ships with no push channel, no alarm row and no manifest
support, and it would have caught the founding incident on its own — §7 is that claim, checked. Each
later phase is gated on either a measurement phase 1 produces or a decision only the owner can make,
and neither gate is one this spec may open for them.

**Why the roster is tracked rather than in `state/`.** It is hand-written, reviewable, and it carries
per-row justifications and literal shell commands that a human should have to read in a diff before
they run on the host. It holds no secret — a tool name, a namespace, an upgrade command. `state/` is
for what the runtime generates; this is source. **One framework consequence the source repo did not
have to face:** a roster seeded with one host's WSL distro and user is host-specific. In seneschal the
tracked file should be a `tool-freshness.example.json` template with `<distro>`/`<user>` placeholders,
and the live roster a per-install copy — the same tracked-example / gitignored-local split every other
per-install file here follows.

---

## 9. Deliberately out of scope — the ecosystem watch

**The other half of the external prompt's `EXTERNAL INTELLIGENCE LOOP` — monitoring agent repos,
papers, model releases, protocol changes and benchmark updates — is out of scope, on purpose, and this
section exists so a later reader does not read that as an oversight.**

The argument belongs to the parent memo and is not re-run here. In one line each: it is **expensive**
(sources, judgement, a digest, and a human to read it), **unverifiable** (a version string is a fact;
*"is this idea relevant"* is a taste call, and a watcher whose output cannot be checked cannot be known
to have degraded), and **it decays unattended** — for which the memo supplies the measurement rather
than the intuition: the repo that mandates a recurring watch for warnings that existing assumptions
may be stale **had not been pushed for about five months**. That is the failure mode measuring itself.

This assistant has its own instance of the same lesson: *an observability contract that only exists in
a prompt is not an observability contract* — learned when an advisor produced zero rows for a month
while the cockpit read its file on every poll.

**What would change this:** nothing in this spec. The memo already holds the contingent piece — a
typed external-knowledge record, free to copy when and if there is ever anything to put in it. It is
not needed to build any phase above, and building it now would be a schema with no writer.

---

## 10. Open questions — undecided, and the owner's

**Q1 — May the watcher ever send a Telegram push, or is it digest-only forever?**
Phase 1 assumes **digest-only**, deliberately, and will not send one line without an answer here. The
case for a push: the founding incident cost a working pipeline on a day the owner wanted a transcript,
and a digest read the next morning is slower than a buzz. The case against: over-notification is
already the owner's standing complaint about this assistant, and a subsystem that earns a mute is worse
than one that never spoke. Phase 1's month of digest lines is meant to make this answerable with a
number instead of an intuition. **Not the assistant's to decide — it is the owner's attention budget.**

**Q2 — Which rows are enrolled, and do the manifest surfaces ever come into scope?**
§2.4 proposes four rows and gives reasons for every inclusion and every exclusion, but the roster is a
statement about which of the owner's tools matter, and §2.5 (a frozen but stale component) shows the
judgement is not derivable from the code. Specifically open: **`deno`** and the **whisperx venv** —
both fail §2.4(a) as written, both are things an owner may actually run; and whether phase 4's
lockfile surfaces are wanted at all, given `uv sync --frozen` pins them **on purpose** (§2.1) and
*"outdated"* against a deliberate pin may be a category error rather than a finding.

**Q3 — Auto-upgrade: is there ever a version of this that acts?**
§5 makes report-only an invariant and this spec designs no alternative. But the question is real —
there is a conceivable world where a `breaks_when_stale` tool with a pinned rollback command is
act-low to upgrade, because the rollback is exact and the blast radius is one tool. **That is a change
to the approval gate, so it is the owner's and nobody else's.** Recorded here so that a future session
reading §5 knows the question was seen and left standing, not that it went unnoticed. **The default
if it is never decided: report-only, forever.**

---

## 11. Where reading the host contradicted the brief

Three things the brief got right in substance and understated in degree, each **[measured]** rather
than reasoned:

1. **"It appears in no manifest of any kind" is true, and the caller is missing too.** §2.3 — the
   pipeline's driver script was neither tracked nor ignored; it was absent. The single tracked mention
   of `yt-dlp` in that tree was a pointer-ledger row that existed *to record the absence*. This
   strengthens the brief's conclusion: an explicit list is not merely the obvious answer, it is the
   only one that survives, because no scan of the repository can find either end of the pipeline.
2. **"Different ecosystems rot at different rates" is right, and rate is the wrong axis anyway.**
   §2.6 — the fastest-moving tool on the host (`claude-code`, median gap **0 days**) is the one least
   worth watching, and the enrolment criterion has to be *does staleness break it*, not *does it move*.
3. **A version check is not one probe.** §2.2 — `uv` differed across the WSL boundary, so identity is
   `(namespace, name)`; and a non-login shell reports `<not on PATH>` for a tool that is installed,
   which is a false **negative** rendered as reassurance. Neither is visible without running the probe
   both ways, and both would have shipped as bugs in a spec written from the manifests alone.

One place the parent memo was superseded rather than contradicted: it proposed a **new numbered Dream
step**. §4.2 argues for an entry **inside step 2** instead, because step 2 already hosts two sensors of
exactly this shape — one of which, `transcript_size_watch.py`, is described in `modes/dream.md` as
*"the one item here that sweeps NOTHING."* A third instance is not a new category.

---

## 12. Sources

- **The host, probed read-only on the day of the incident.** `uv tool list` / `uv tool dir` /
  `yt-dlp --version` / `ffmpeg -version` / `apt-cache policy ffmpeg` / `deno --version` /
  `python3 --version` under `wsl -d <distro> -u <user> -- bash -lc`, and again under a non-login `bash`
  to establish §2.2's false negative; `uv --version` / `gh --version` / `git --version` /
  `python --version` / `node --version` / `claude --version` on the Windows side. **Nothing was
  installed, upgraded or modified.**
- **Upstream release metadata**, `gh api repos/<repo>/releases?per_page=40` filtered to non-draft,
  non-prerelease, for yt-dlp, GitHub's `gh`, `uv`, `deno`, FFmpeg and the `claude` CLI (repo slugs as
  linked in §3.4); plus `https://pypi.org/pypi/yt-dlp/json` for the normalisation finding in §6.3.
- **This repo**, read for the inventory: `pyproject.toml`, `uv.lock`, `cockpit/web/package.json`,
  `phone/package.json`, `phone/android/app/build.gradle.kts`, `phone/wrangler.toml`, `modes/dream.md`,
  `scripts/dream_steps.py`, `scripts/worktree_gc.py`, `scripts/transcript_size_watch.py`,
  `scripts/jobs.py`.
- **Parent memo:** the external-agent-prompt evaluation memo (not shipped in this tree), its sections
  on the intelligence loop, the approval-gate warning, and the typed external-knowledge record.
- **The failure narrative** — three identical 403s, the six-client probe, the approved upgrade and the
  first-try success after it — is **the owner's account and the session that ran it**, and is marked
  as such wherever it is used.

## Router entry

**Router status:** **SPEC ONLY — nothing built**. **What it decided:** The cheap half of an external
agent prompt's intelligence loop: watch the host's externally-moving tools, **report, NEVER upgrade** —
§5 is an invariant, and auto-upgrade is a gate change left **open for the owner** (§10 Q3). **The
founding incident refutes the two obvious metrics** — at the break `yt-dlp` was **1 release behind**
and that release had been out **1.5 days**, so only the **48-day age of the installed build** separates
it from a quiet day (32×), and a threshold on *lag* or *releases-behind* is just a release notifier.
Age alone then nags through the **83-day drought** in the same history, so **the rule is a conjunction
— newer exists AND installed is past threshold — and neither clause is redundant.** Enrolment is an
**explicit tracked roster**, forced not preferred: `yt-dlp` is in no manifest **and its caller was not
in the repo at all**, so no tree scan finds either end. Two rules that read as arbitrary until the
measurement: **a row needs an upgrade path or it must not exist** (`ffmpeg`'s apt Candidate ==
Installed ⇒ permanent unactionable noise), and **rate of change is NOT the criterion** — `claude-code`
moves fastest (median gap **0 d**) and is deliberately unenrolled. Identity is **`(namespace, name)`**
— `uv` differed across the WSL boundary on one machine — and a non-login shell reports a *false*
`<not on PATH>`, so `unknown` is a named verdict, never a blank. Rides **inside Dream step 2**, not as
a new numbered step; phase 1 is **digest-only, zero pushes** (the owner's attention budget, **open**,
§10 Q1). §9 holds the ecosystem-watch half out of scope on the record.
