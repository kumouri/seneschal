# Chapter: env-walker

One generic chapter that walks **every** env surface in
`seneschal/setup/env-manifest.json` — the single source of truth for what's configurable,
what each var means, how to discover a value, and how to verify the result. The walker adds
no per-integration knowledge of its own: **read the manifest fresh at chapter start** and let
it drive. One ledger chapter per entry, id `env:<id>`.

## Walk order

Manifest **file order** — required entries first (the manifest's ordering contract,
CI-enforced), then optionals. The required-before-optional rule from the SKILL applies:
`env:telegram` is settled (done or explicitly declined) before any optional entry opens.

## Per-entry algorithm

For each entry, in order:

**1 — Skip if owned elsewhere.** `handled_by` present (`cockpit`, `slack-mcp`/`notion-mcp` →
`mcp`) → not this chapter's entry. One board-pointer line ("cockpit.env — the cockpit
chapter's") and move on. Never write a `handled_by` entry here.

**2 — Ledger check.** `python seneschal/scripts/setup_state.py get env:<id>` — `done` →
summarize + offer redo (SKILL rule); `declined`/`blocked` → one line, move on unless the
owner asks; otherwise `mark env:<id> in-progress`.

**3 — Enable question** (optional entries only; required entries have none). Ask the
manifest's `enable_question` **verbatim** — it was written to carry the honest trade-off.
Decline →

```
python seneschal/scripts/setup_state.py mark env:<id> declined --summary "<the degradation, from the entry's notes>"
```

— the summary states what *won't* work, lifted from the entry's `notes` (e.g. proton:
"email-triage falls back to Gmail, draft-only"). Then next entry.

**4 — Dependency check** (`depends[]`):

- **`ollama`** (hard dep of router / rag / sentiment): if the ledger's `deps.ollama.status`
  isn't `ready`, run `chapters/ollama.md` **inline now** — it's the on-demand sub-chapter,
  triggered by the first enabled entry that needs it, never run speculatively. If it ends
  not-ready, mark this entry per its outcome (declined if the owner declined Ollama, blocked
  if install/serve failed) with the entry's honest degradation note, and continue the walk.
  When Ollama is ready on a **non-default URL**, use `deps.ollama.url` as the default for
  this entry's `OLLAMA_URL` var.
- **`worker:deployed`** (push-call / push-sms): ask whether the `phone/` call-screener Worker
  is deployed. If not, `mark env:<id> blocked --summary "phone/ Worker not deployed"` and
  point at the entry's `docs` — deploying the Worker is its own task, never an inline step
  here.
- **`proton-bridge-running`**: ask whether Proton Mail Bridge is installed and signed in;
  if not, offer to hold the entry (`blocked`, docs pointer) — the verify step is the real
  test, so "I think so" is enough to proceed.
- **Soft deps** (`{soft: true}`, e.g. discord's `venv-websockets`): state the `why`
  degradation in one line and **continue** — soft deps never gate.

**5 — The vars, one at a time.** Walk the entry's `vars` in order, prompt-with-default:

- Show the var's `prompt` and its `default` (defaults may reference another var as
  `{VAR_NAME}` — `setup_env.py` expands those; just say "defaults to your `<VAR>`").
  Enter/skip keeps the default; skipping a non-required var without a default omits it.
- **Secret vars** (`secret: true`): the owner pastes the value into the chat. Acknowledge
  receipt **without repeating any part of it**, and never place it in a command line, a
  summary, or a log.
- **`discover` flows**: when the owner doesn't know a value and the var has a `discover`
  block, relay its `say` text verbatim, run its `cmd`, and read the value out per `extract`
  — confirm it with the owner before treating it as answered.
- `recommended: true` vars get a gentle push ("strongly recommended — this is the inbound
  allowlist"), still skippable.

**6 — Write** via `setup_env.py`'s stdin payload — never values-on-argv. Assemble
`{"manifest_id": "<id>", "values": {...}}` with **only the vars the owner actually
provided** (merge mode keeps everything else: existing hand-set values, then manifest
defaults, then the example's own values). To keep secrets out of the shell command string:
write the payload to gitignored `seneschal/state/setup-payload.json` with a file-write tool
(not a shell echo), then

```
python seneschal/scripts/setup_env.py < seneschal/state/setup-payload.json
```

and **delete the payload file immediately after**, success or failure. The tool validates
every value against the var's `validate` regex (rejections name the var; secret values print
as `<redacted>`), preserves the example's comments and ordering, and writes atomically
(0600 on POSIX). A validation rejection → re-ask that one var, rebuild the payload, retry.

**7 — Post steps** (`post[]`, when present): offer each one as its own confirmed step
(google's per-account OAuth consent opens a browser; rag's initial index build churns for a
bit). Skipping a post step is allowed — say what stays incomplete.

**8 — Verify.** Run the entry's `verify.cmd` with `{path}` expanded to the entry's `path`.
Success = **exit 0 AND `verify.expect` appearing in stdout**. When `verify.user_confirm`
exists, ask that question too — the command can't see the out-of-band effect (did the
Telegram message actually arrive?); only a yes completes the verify. Mind the entry's
`notes` for verify caveats (router/sentiment pass even with Ollama down — the notes say
which probe is the real reachability check).

- Verified → `mark env:<id> done --artifacts <path> --summary "<one line>"`. (No
  `--hash-artifacts` for env entries on purpose: hand-editing an env file later is a
  documented, supported path — the merge contract — and shouldn't flag the chapter stale;
  the verify command and the doctor are the ground truth for env health.)
- Failed → show the (secret-safe) error, offer: fix a var and retry / consult the entry's
  `docs` / hold it. Held → `mark env:<id> blocked --summary "<what failed, no secrets>"`.

## Close (after the last entry)

One **transcript-hygiene note**, verbatim in spirit: chat transcripts live locally on this
machine, and the secrets pasted during this chapter are in this conversation's local
history — on a shared machine, rotate any token you'd mind another local user reading
(BotFather can reissue a Telegram token in seconds; every provider here can rotate).
