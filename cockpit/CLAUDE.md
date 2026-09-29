# cockpit/ — the Seneschal Cockpit, the web observatory

**Three dependency worlds, and confusing them is the mistake this file exists to prevent.**
`server/` (:8760) + `decoy/` (:8490) need the `cockpit` uv extras (never installed by the daemon's
default `uv sync --frozen`); `web/` is Vite/React; `breakglass/` is **deliberately stdlib-only**, its
own process on its own port (:8499), because the rescuer must not share a dependency with the things
it exists to rescue.

**No third copy.** Where the daemon owns a schema or a table, the backend may duplicate it in
Python and serve it (`server/model_config.py`, `server/governor.py` — `test_parity.py` is the CI
tripwire that they still match the daemon's); the browser never keeps its own. A frontend copy of
the model rank table is exactly how a dial silently downgrades.

**Readers are tolerant, always.** A missing or corrupt state file degrades to `available: false`,
never a 500 — and the tests are written against the on-disk shape rather than by importing the
daemon's writer, which is what proves this backend still works against a daemon on a different
commit.

This file loads when a turn touches this directory, instead of in every session unasked.

---

cockpit/           the Seneschal Cockpit — **v0-v5 BUILT**, plus the Jobs, Trace and Open-specs
panels. **What each phase shipped and every endpoint it added: `seneschal/docs/cockpit-spec.md`** —
that file owns the design and the build history, and its Phases table is the authority on what is in
which. The panel inventory, env vars and the auth modes: `cockpit/README.md`.
Directories: `server/` (FastAPI/uvicorn, the `cockpit` extras) + `web/` (Vite/TypeScript/React) +
`zitadel/` (the self-hosted IdP compose stack + `ZITADEL_SETUP.md`) + `decoy/` (the isolated public
honeypot chat — separate process, zero tools/data/secrets, same extras) + `breakglass/` (DELIBERATELY
stdlib-only — survives a broken venv/daemon/backend; `assertion.py` is the one module it shares with
`server/`). **Supervision** (`seneschal/scripts/cockpit_site.py`) ships unwired — no daemon task
runs it yet, so the README's "Running it locally" steps start the backend.

The panel decisions a future edit could silently undo, which is why they are here rather than only
in the spec: the **Jobs panel is read-only with NO cancel route BY DECISION**; it alarms only on
`awaiting_push` (a finished job whose completion push never landed); running jobs are never
truncated by `limit` and finished ones sort by when they ENDED; and `outbox_*` renders **only when
non-zero** (Notion backend only — filesystem backends have no outbox).

**`web/` tests are stdlib `node --test`, never a framework** (Node ≥ 22.18; a hand-written
`web/src/node-test-shim.d.ts` stands in for Node's types). Testable logic gets extracted into a
DOM-free module beside its component — `jobsCollapse.ts` (never hide an undelivered push),
`routerRow.ts` (the arm never reads as a verdict), `collapseStorage.ts` (tolerant collapsed/expanded
**and numeric** persistence, shared by the Jobs and Trace sections — the numeric half backs the
Trace panel's resizable sessions pane), `traceView.ts` (turn grouping never reorders the log; search
never resurrects a redacted turn; `sortSessions` reorders a copy, never the backend's own
newest-first list), `traceResize.ts` (the sessions pane's drag-resize clamping — never past its own
bounds, never so wide it starves the detail pane), `docStatusView.ts` — pinned, not commented.
**A module `node --test` loads must import with the explicit `.ts` extension** — Node's ESM resolver
does not guess, and Vite accepts it either way, so the extensionless import only fails in the tests.

**The Trace panel's two decisions, both trivially undone by a tidy-up:** it is **NOT a grid tile**
but a collapsible section under the chat pane (in a tile its content column resolved to literally
zero px), and **the TURN is the unit, not the event**. A `!private` turn is redacted **in the
reader** (`server/trace.py`), and `traceView`'s search is held to the same rule — so is a session's
`title`, which `read_sessions` derives from the first thing the owner said in it. Tool input renders
inline and wraps (`session-trace-spec.md` §11), never truncated client-side on top of the backend's
own cap; the sessions/detail split is drag-resizable, not a fixed column.

**The Open-specs ledger is DERIVED and that is the whole design** (`server/doc_status.py` +
`web/src/components/DocStatusPanel.tsx`). Every row is computed from each document's own
`**Status:**` header by `seneschal/scripts/check_doc_status.py` — the same parser CI enforces — so
there is no stored list, no vocabulary in the browser, and no ordering in the browser: the order,
the gloss and the definition of "open" all arrive from the server. A hand-maintained list fails, and
a dashboard copy would fail the same way more quietly, since nobody diffs a dashboard. Three
decisions a tidy-up could undo: it is **READ-ONLY with no edit route** (a status changes by editing
the document, in the PR that changes the thing); the **UNCLASSIFIED group renders last and ALWAYS**,
because it means CI is red and silently omitting it is how an inventory reports itself complete
while missing things; and it is a **grid TILE**, unlike the Trace panel — a reference list the owner
goes looking for, not a read-along of the conversation.

**`doc_status.py` IMPORTS the daemon's parser where `jobs.py` beside it deliberately does not, and
the difference is not an inconsistency.** `jobs.py` reimplements because it parses STATE files a
daemon on a different commit may have written. Here the parser and the documents ship in the SAME
commit — the backend is reading the checkout it runs from — so there is no skew to tolerate, and a
duplicated parser would be exactly the second source of truth the ledger exists to abolish. The
import is guarded: unreachable parser → `available: false` with a reason.

**A panel that measures its own width uses a `@container` query, never `@media`.** A viewport
breakpoint never fires on a wide desktop while the panel's actual container is narrow, and the
content column collapses to zero (`seneschal/docs/session-trace-spec.md` §10). The window is not the
box.

**`GET /api/ws` in `dev` mode is gated on `Origin`, not on "dev mode has no auth to bypass"**
(`server/app.py`'s `_ALLOWED_DEV_WS_ORIGINS`). Every other route's `dev`-mode reasoning — "every
gated route open, there's nothing to hijack" (`auth.py`'s module docstring) — relies on this backend
setting no CORS headers, so a cross-origin `fetch`/XHR can't complete. **A WebSocket Upgrade
handshake is exempt from the Same-Origin Policy**: without the check, any page open in a browser on
this box could open `ws://127.0.0.1:8760/api/ws` and both read the live chat stream and drive
`chat.send`/`control.restart`. A tidy-up that drops the Origin check as redundant reopens exactly
this gap — it is the one gate `dev` mode actually needs. Behind a local reverse proxy (which forwards
the original `Host`), an unrecognized `Origin` is accepted only when it matches that `Host`
(`_dev_ws_origin_same_host` — no per-hostname config, safe with none configured).
