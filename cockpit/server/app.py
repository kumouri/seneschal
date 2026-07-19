"""Seneschal Cockpit — the seneschald cockpit backend (v0 skeleton + v1 read-only monitor + v2 daemon
pipe/chat + v3 model dials/router + v3.5 Oikonomos/Thresholds + v4 health/workout/meal panels + the
archon tiles/proxy).

FastAPI app, 127.0.0.1-only (enforced twice: bind at the uvicorn layer — see README.md's run
command — AND a belt-and-braces middleware here that rejects any client host that isn't loopback).
Every route under /api except /api/health and /api/auth/status is gated behind `require_auth`
(`auth.py`) — the COCKPIT_DEV_NO_AUTH=1 dev stub, else 503 (see `auth.auth_mode`'s precedence; the
full OIDC auth stack is a deferred follow-up, see "Auth" in ../README.md). Every mutating route ALSO
depends on `require_csrf` (a no-op outside real-auth mode). The writes in this app are the restart
enqueue, the model-config PUT (v3), the governor-config PUT (v3.5), their audit-log lines
(control.py), and the chat-fallback inbox append (_append_inbox_fallback, below) — everything else is
a tolerant read over `seneschal/state/*` (readers.py, transcript.py, governor.py, archons.py).

**v2 (daemon pipe):** a `PipeClient` (pipe_client.py) holds the ONE outbound connection to the daemon's
cockpit pipe as a lifespan-managed background task, reconnecting with backoff. `GET /api/ws` fans every
relayed frame out to N browser tabs (ws_hub.py) and forwards browser-originated chat.send/status.get/
control.restart back — falling back to the `cockpit-inbox.jsonl` file queue (and an honest `pipe:
"down"` status) whenever the pipe is unreachable. Note: `@app.middleware("http")` does not run for
websocket connections (Starlette limitation) — `/api/ws` re-checks `auth.auth_mode()` (and, in `oidc`
mode, the session cookie) itself; the uvicorn-level 127.0.0.1 bind still covers it regardless of route
type.

**v3 (model dials + Fable delegation, cockpit-spec.md):** `GET`/`PUT /api/model-config` read/write
`state/model-config.json` (via `model_config.py`, this app's own duplicated rank/coherence table — the
same one `seneschal/scripts/model_config.py` enforces daemon-side, ruling 3); the PUT validates before
writing and audits every call. `GET /api/router-stats` summarizes `state/router-log.jsonl` (both the
pre-existing triage arm and the v3 fable arm, distinguished by `arm`). The chat composer's `force_fable`
field (already threaded through `_append_inbox_fallback`/the pipe) now actually forces, daemon-side.

**v3.5 (Oikonomos, the budget governor, cockpit-spec.md "Oikonomos — the budget governor"):**
`GET`/`PUT /api/governor-config` read/write `state/governor-config.json` (via `governor.py`, this app's
own duplicated SCHEMA-validation copy — the same one `seneschal/scripts/governor.py` enforces daemon-side,
ruling 3) and return today/this-week spend rollups from `state/governor-ledger.jsonl` alongside the
config so the Thresholds panel's spend meters need no second endpoint. The PUT is a partial update
(only the touched knobs), validates against SCHEMA before writing, and audits every call like the
model-config PUT.

**v4 (health/workout/meal panels, cockpit-spec.md "Health pipeline extension (v4)"):** `GET
/api/health/sleep`, `/api/health/workouts`, `/api/health/nutrition`, `/api/health/summary` are tolerant
read-only endpoints over `<state dir>/health.db` (via `health.py`, over the same live-feed
`workouts`/`nutrition` tables `seneschal/scripts/health_import.py` populates) — a missing db/table never
500s, it returns an honest `"available": false`. `GET /api/meals` reads the Dream-staged meal-plan
snapshot at `<state dir>/meals.json` — same tolerance.

**Archon tiles/proxy:** `GET /api/auth/status` is PUBLIC (no `require_auth`) and reports auth mode +
whether THIS request is authenticated, so the frontend can show a login screen instead of a wall of
401s. `GET /api/archons` + the `GET /archons/{id}/{path:path}` reverse proxy (`archons.py`) expose a
live archon's own UI through this SAME server — archons never face the internet directly. The real
OIDC auth stack (login/callback/logout, the SSO session, break-glass) is a **deferred follow-up**;
until it lands the cockpit runs in dev-no-auth (or unconfigured/503) mode only.

Run (dev): see ../README.md. Serves the built frontend (cockpit/web/dist) as static files when present.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import archons as archons_mod
from . import auth, control, emotes, governor, health, model_config, readers, transcript
from . import session as session_mod
from .auth import require_auth
from .config import (
    WEB_DIST_DIR,
    get_emote_dir,
    get_pipe_host,
    get_pipe_port,
    get_pipe_token_path,
    get_state_dir,
    is_dev_no_auth,
)
from .pipe_client import (
    TYPE_CHAT_ACK,
    TYPE_CHAT_SEND,
    TYPE_CONTROL_ACK,
    TYPE_CONTROL_RESTART,
    TYPE_STATUS,
    TYPE_STATUS_GET,
    PipeClient,
)
from .ws_hub import BrowserHub

require_csrf = auth.require_csrf

browser_hub = BrowserHub()


class _CockpitState:
    """Small module-level holder for the live PipeClient + the last status frame seen from the daemon.
    Plain callables (PipeClient's on_frame/on_connect/on_disconnect) can't close over FastAPI's
    request-scoped `app.state`, so this is the one seam both the lifespan and the frame handlers below
    reach through."""

    pipe_client: Optional[PipeClient] = None
    last_status: Optional[dict] = None


_state = _CockpitState()


def _pipe_up() -> bool:
    return _state.pipe_client is not None and _state.pipe_client.connected


async def _on_pipe_frame(frame: dict) -> None:
    """Every frame the daemon sends: chat.event/status/chat.ack/control.ack alike are simply fanned
    out to every browser tab. A `status` frame is augmented with `pipe: "up"` (we only got here because
    a frame arrived, so the pipe is — at this instant — definitely up) and cached as the last-known
    status for REST reads and freshly-connecting tabs."""
    if frame.get("type") == TYPE_STATUS:
        frame = {**frame, "pipe": "up"}
        _state.last_status = frame
    browser_hub.broadcast(frame)


def _on_pipe_disconnect() -> None:
    """Fired the instant the outbound connection drops — broadcast an honest degraded status
    immediately rather than waiting for a browser tab to poll and find out the hard way."""
    down = {**(_state.last_status or {"type": TYPE_STATUS}), "pipe": "down", "turn_in_flight": None}
    _state.last_status = down
    browser_hub.broadcast(down)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    client = PipeClient(get_pipe_host(), get_pipe_port(), get_pipe_token_path(),
                        on_frame=_on_pipe_frame, on_disconnect=_on_pipe_disconnect)
    _state.pipe_client = client
    client.start()
    try:
        yield
    finally:
        await client.stop()
        _state.pipe_client = None


app = FastAPI(title="Seneschal Cockpit", version="0.1.0", lifespan=lifespan)

# Starlette's TestClient reports its synthetic client as ("testclient", ...) — allow it alongside the
# real loopback hosts so the unit tests exercise this middleware instead of needing to bypass it.
_ALLOWED_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    host = request.client.host if request.client else None
    if host not in _ALLOWED_CLIENT_HOSTS:
        return JSONResponse({"detail": "this cockpit only answers on localhost"}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def api_health():
    state_dir = get_state_dir()
    return {
        "ok": True,
        "service": "cockpit",
        "version": app.version,
        "state_dir": str(state_dir),
        "state_dir_exists": state_dir.is_dir(),
        "dev_no_auth": is_dev_no_auth(),
        "web_dist_present": WEB_DIST_DIR.is_dir(),
    }


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    """PUBLIC — no `require_auth` (that would make an unauthenticated visit 401 before the frontend
    even gets to decide how to react). Reports auth mode + whether THIS request already carries a
    valid session, so the frontend can show a "log in" screen instead of a wall of 401s from every
    panel's own poll."""
    return auth.auth_status(request)


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
def api_sessions():
    return readers.read_sessions(get_state_dir())


@app.get("/api/oneiroi", dependencies=[Depends(require_auth)])
def api_oneiroi(limit: int = 20):
    return readers.read_oneiroi(get_state_dir(), limit)


@app.get("/api/seneschald-health", dependencies=[Depends(require_auth)])
def api_seneschald_health():
    return readers.read_seneschald_health(get_state_dir())


@app.get("/api/presence", dependencies=[Depends(require_auth)])
def api_presence():
    return readers.read_presence(get_state_dir())


@app.get("/api/reminders", dependencies=[Depends(require_auth)])
def api_reminders():
    return readers.read_reminders_summary(get_state_dir())


@app.get("/api/usage", dependencies=[Depends(require_auth)])
def api_usage():
    return readers.read_usage(get_state_dir())


@app.get("/api/status", dependencies=[Depends(require_auth)])
def api_status():
    """v1's registry-derived placeholder, now honestly labeled with the pipe's live state — and, once
    at least one status frame has arrived from the daemon over the pipe, that live snapshot instead
    (turn-in-flight, model, queue depth — v1's placeholder never had these)."""
    pipe_up = _pipe_up()
    if _state.last_status is not None:
        return {**_state.last_status, "pipe": "up" if pipe_up else "down"}
    base = readers.read_status(get_state_dir())
    return {**base, "pipe": "up" if pipe_up else "down"}


@app.get("/api/transcript", dependencies=[Depends(require_auth)])
def api_transcript(limit: int = 200):
    return {"events": transcript.read_transcript_tail(get_state_dir(), limit)}


@app.get("/api/emotes", dependencies=[Depends(require_auth)])
def api_emotes():
    return {"emotes": emotes.list_emotes(get_emote_dir())}


@app.get("/api/emotes/{filename}", dependencies=[Depends(require_auth)])
def api_emote_file(filename: str):
    path = emotes.resolve_emote_file(get_emote_dir(), filename)
    if path is None:
        raise HTTPException(status_code=404, detail="emote not found")
    return FileResponse(str(path))


@app.post("/api/control/restart", dependencies=[Depends(require_auth), Depends(require_csrf)])
def api_control_restart():
    state_dir = get_state_dir()
    reason = "cockpit restart button"
    path, already_queued = control.enqueue_restart(state_dir, reason=reason)
    control.append_audit(state_dir, "control.restart", {"reason": reason, "already_queued": already_queued})
    return {"ok": True, "queued": True, "already_queued": already_queued, "path": str(path)}


# ------------------------------------------------------------------------------ v3: model dials + router

class ModelConfigUpdate(BaseModel):
    warm_model: str
    max_routable_model: str


@app.get("/api/model-config", dependencies=[Depends(require_auth)])
def api_get_model_config():
    """The two dials (cockpit-spec.md "Model dials & Fable delegation") — a tolerant passthrough of
    `state/model-config.json` (missing/corrupt -> both fields null, matching `model_config.load`)."""
    return model_config.load(get_state_dir())


@app.put("/api/model-config", dependencies=[Depends(require_auth), Depends(require_csrf)])
def api_put_model_config(body: ModelConfigUpdate):
    """Validated write of both dials together (same rank/coherence rules `seneschal/scripts/model_config.py`
    enforces — duplicated here per cockpit-spec.md ruling 3). An unrecognized id or an incoherent pair
    (warm outranking the ceiling) 400s instead of writing a bad config. Audited like the restart
    control — every mutating call gets a `cockpit-audit.jsonl` line, this is the one other write this
    app makes."""
    state_dir = get_state_dir()
    try:
        data = model_config.save(state_dir, body.warm_model, body.max_routable_model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    control.append_audit(state_dir, "model_config.update", {
        "warm_model": data["warm_model"], "max_routable_model": data["max_routable_model"],
    })
    return data


@app.get("/api/router-stats", dependencies=[Depends(require_auth)])
def api_router_stats(limit: int = 20):
    """Tolerant summary of `state/router-log.jsonl` — verdict counts by arm (triage/fable) + the last
    `limit` decisions, newest first. See readers.read_router_stats for the schema."""
    return readers.read_router_stats(get_state_dir(), limit)


# --------------------------------------------------------------------- v3.5: Oikonomos / Thresholds

class GovernorConfigUpdate(BaseModel):
    updates: dict[str, Any]


@app.get("/api/governor-config", dependencies=[Depends(require_auth)])
def api_get_governor_config():
    """Oikonomos, the budget governor (v3.5, cockpit-spec.md "Oikonomos — the budget governor"): the
    schema-driven config (tolerant load — missing/corrupt file -> every knob's default) + `SCHEMA`
    itself (so the Thresholds panel renders its whole form from one response) + today/this-week spend
    rollups from `state/governor-ledger.jsonl`."""
    state_dir = get_state_dir()
    return {
        "config": governor.load(state_dir),
        "schema": governor.SCHEMA,
        "rollups": governor.rollups(state_dir),
    }


@app.put("/api/governor-config", dependencies=[Depends(require_auth), Depends(require_csrf)])
def api_put_governor_config(body: GovernorConfigUpdate):
    """SCHEMA-validated partial update — only the knobs in `body.updates` are touched; every other
    stored knob (including one a newer version's SCHEMA added that this one doesn't recognize) is
    preserved untouched. An unknown knob name or an out-of-range/wrong-shaped value 400s with a joined
    message instead of writing a bad config. Audited like the model-config PUT."""
    state_dir = get_state_dir()
    try:
        governor.save(state_dir, body.updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    control.append_audit(state_dir, "governor_config.update", {"updates": body.updates})
    return {
        "config": governor.load(state_dir),
        "schema": governor.SCHEMA,
        "rollups": governor.rollups(state_dir),
    }


# --------------------------------------------------------------------- v4: health/workout/meal panels

@app.get("/api/health/sleep", dependencies=[Depends(require_auth)])
def api_health_sleep(days: int = 14):
    """Recent nights (default 14) from `state/health.db`'s `sleep_session` table, grouped per
    Chicago-local calendar night. See `health.read_sleep` for the tolerance contract."""
    return health.read_sleep(get_state_dir(), days)


@app.get("/api/health/workouts", dependencies=[Depends(require_auth)])
def api_health_workouts(days: int = 14):
    """Recent workout sessions (default 14 days) + a per-week rollup (count/minutes/kcal) from
    `state/health.db`'s `workouts` table (Call Shield v4 live feed only)."""
    return health.read_workouts(get_state_dir(), days)


@app.get("/api/health/nutrition", dependencies=[Depends(require_auth)])
def api_health_nutrition(days: int = 14):
    """Per-day nutrition totals (kcal/protein/carbs/fat) + that day's logged entries (default 14 days)
    from `state/health.db`'s `nutrition` table (Call Shield v4 live feed only)."""
    return health.read_nutrition(get_state_dir(), days)


@app.get("/api/health/summary", dependencies=[Depends(require_auth)])
def api_health_summary():
    """One compact card: last night's sleep, this week's workouts, today's kcal/protein so far."""
    return health.read_summary(get_state_dir())


@app.get("/api/meals", dependencies=[Depends(require_auth)])
def api_meals():
    """The Dream-staged meal-plan/meal-idea snapshot (`state/meals.json` — written when Dream stages
    meal plans from the active store) — absent/corrupt -> `{"available": false, "plans": []}`."""
    return health.read_meals(get_state_dir())


# ------------------------------------------------------------------------------- archon tiles/proxy
# (The real OIDC auth routes — /auth/login, /auth/callback, /auth/logout — are a deferred follow-up;
# see auth.py's mode precedence. In "oidc" mode without them, every gated route just 401s.)

@app.get("/api/archons", dependencies=[Depends(require_auth)])
def api_archons():
    return archons_mod.list_archons()


@app.get("/archons/{archon_id}/{path:path}", dependencies=[Depends(require_auth)])
def archon_proxy(archon_id: str, path: str, request: Request):
    try:
        query = str(request.url.query) or None
        body, status_code, content_type = archons_mod.proxy_get(archon_id, path, query)
    except archons_mod.ProxyError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    return Response(content=body, status_code=status_code, media_type=content_type)


@app.api_route("/archons/{archon_id}/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"],
               dependencies=[Depends(require_auth)])
def archon_proxy_write_blocked(archon_id: str, path: str):
    raise HTTPException(
        status_code=405,
        detail="the archon proxy is GET-only (a documented limitation) — see cockpit/README.md",
    )


# ------------------------------------------------------------------------------- v2: the chat pane's ws

def _append_inbox_fallback(frame: dict) -> None:
    """The fallback write when the pipe is down: one {"id","text","ts"} JSON line to
    state/cockpit-inbox.jsonl, drained by the daemon's scheduler tick into the same chat queue
    Telegram/Discord use (`seneschal/scripts/cockpit_pipe.py`'s drain_inbox on that side). Duplicated
    here (the cockpit is its own dependency world) rather than imported — keep the shape in sync by
    hand."""
    item = {
        "id": frame.get("id"),
        "text": frame.get("text"),
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if "force_fable" in frame:  # v3: the daemon's fallback-inbox drain honors this exactly like the
        item["force_fable"] = frame["force_fable"]  # live pipe path (presence._cockpit_inbox_text)
    path = get_state_dir() / "cockpit-inbox.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def _synth_status_frame() -> dict:
    """What to answer a browser's status.get when the pipe itself is down — the last-known snapshot
    (if any), honestly re-labeled, rather than silently going quiet."""
    base = _state.last_status or {"type": TYPE_STATUS}
    return {**base, "pipe": "down"}


async def _handle_browser_frame(frame: dict) -> None:
    """Route one frame a browser tab sent over /api/ws. chat.send/status.get prefer the live pipe
    (PipeClient.send is itself a bounded, never-blocking enqueue) and fall back to the inbox file /a
    synthesized status when it's down; control.restart ALWAYS uses the same always-available
    control-queue write v1's REST route uses (that file is polled directly by the daemon's control_task
    regardless of whether the cockpit pipe happens to be connected — the more reliable path), audited
    exactly like the REST route."""
    t = frame.get("type")
    if t == TYPE_CHAT_SEND:
        text = frame.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        if _pipe_up():
            _state.pipe_client.send(frame)  # daemon acks with chat.ack; relayed via _on_pipe_frame
        else:
            _append_inbox_fallback(frame)
            browser_hub.broadcast({"type": TYPE_CHAT_ACK, "id": frame.get("id"), "via": "fallback"})
    elif t == TYPE_STATUS_GET:
        if _pipe_up():
            _state.pipe_client.send(frame)
        else:
            browser_hub.broadcast(_synth_status_frame())
    elif t == TYPE_CONTROL_RESTART:
        state_dir = get_state_dir()
        reason = "cockpit chat pane"
        path, already_queued = control.enqueue_restart(state_dir, reason=reason)
        control.append_audit(state_dir, "control.restart",
                             {"reason": reason, "already_queued": already_queued, "via": "ws"})
        browser_hub.broadcast({"type": TYPE_CONTROL_ACK, "action": "restart", "ok": True,
                               "already_queued": already_queued})
    # else: unknown frame type — silently ignored, mirroring the daemon-side PipeHub's tolerance.


@app.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket):
    """Fans daemon-relayed chat.event/status frames out to every connected browser tab, and accepts
    chat.send/status.get/control.restart back (see _handle_browser_frame). Gated by the same auth mode
    every REST route uses (a real session cookie in `oidc` mode — deferred — or the dev stub) —
    `@app.middleware("http")` does not run for websocket connections (Starlette limitation), so this
    re-checks explicitly; the uvicorn 127.0.0.1 bind still covers exposure regardless of route type.
    SameSite=Strict means a cross-site page can't even get this cookie attached to its handshake
    request, so no separate CSRF check is needed here (see auth.py's module docstring)."""
    mode = auth.auth_mode()
    if mode == "unconfigured":
        await websocket.close(code=4401)
        return
    if mode == "oidc":
        cookie = websocket.cookies.get(session_mod.SESSION_COOKIE_NAME)
        if auth.current_session_from_cookie(cookie) is None:
            await websocket.close(code=4401)
            return
    await websocket.accept()
    cid = browser_hub.connect(websocket)
    if _state.last_status is not None:
        try:
            await websocket.send_json({**_state.last_status, "pipe": "up" if _pipe_up() else "down"})
        except Exception:  # noqa: BLE001 — a fresh tab that vanished instantly is not our problem
            pass
    try:
        while True:
            frame = await websocket.receive_json()
            if isinstance(frame, dict):
                await _handle_browser_frame(frame)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — a malformed/broken client must not crash the fan-out for others
        pass
    finally:
        browser_hub.disconnect(cid)


@app.get("/", include_in_schema=False)
def root_page(request: Request):
    """In `oidc` mode with no valid session, redirect the browser to `/auth/login` instead of serving
    the SPA shell to someone who isn't logged in yet. (That route is part of the deferred auth
    follow-up — in this build `oidc` mode is unsupported, so the branch is inert; it's kept so the
    auth PR is a pure re-add.) Registered explicitly (ahead of the StaticFiles mount below, which only
    ever handles paths this route doesn't) so this exact path gets the auth check; every OTHER static
    asset (JS/CSS bundles) stays unauthenticated — they carry no data, only the API calls do, and
    those are already gated."""
    if auth.auth_mode() == "oidc" and auth.current_session(request) is None:
        return RedirectResponse("/auth/login", status_code=302)
    index = WEB_DIST_DIR / "index.html"
    if index.is_file():
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail="frontend not built (npm run build in cockpit/web)")


# Serve the built frontend when present (`npm run build` in cockpit/web -> cockpit/web/dist). Mounted
# last so it never shadows /api/* or the explicit "/" route above; absent in a fresh checkout, which is
# fine — dev runs Vite separately.
if WEB_DIST_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIST_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8760)
