"""The decoy steward — the public decoy chat (`seneschal/docs/cockpit-spec.md`, "The decoy").

A **completely separate process** from the authed cockpit backend (`cockpit/server/`): its own
FastAPI app, its own uvicorn entry point, its own default port (127.0.0.1:8490). It imports NOTHING
from `cockpit.server`, reads no file under `seneschal/state`, and shares no secret or config with the
authed backend — that isolation IS the security control. Prompt-injection against this page is inert
because there is nothing behind the counter: no tools, no data, nothing to steal. See the spec's "The
decoy" and "Threat model" sections.

Talks to exactly one external thing: a local Ollama instance (`ollama_client.call_ollama`), honoring
`OLLAMA_URL` (some machines run Ollama on a non-default port — see README.md). If Ollama is
unreachable, unresponsive, or answers with something unparseable, the reply is the static
in-character line `ollama_client.OLLAMA_DOWN_LINE` — never a stack trace, never an HTTP 5xx with
detail.

Run (dev):
  uv sync --extra cockpit
  uv run uvicorn cockpit.decoy.server:app --host 127.0.0.1 --port 8490

See README.md for the full run/env story and cockpit-spec.md ruling 12 (local-only — no tunnel yet).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config
from .ollama_client import OLLAMA_DOWN_LINE, OllamaUnavailable, call_ollama
from .persona import build_system_prompt
from .ratelimit import Counter, GlobalLimiter, TokenBucket

# Deliberately no `%(message)s`-style content logging anywhere in this module — see the module
# docstring's isolation claim. Every log call below carries only structural fields (ip, counts,
# booleans), never the visitor's message or the model's reply.
logger = logging.getLogger("cockpit.decoy")

app = FastAPI(title="seneschald — reception", version="0.1.0")

# --- rate limiting -------------------------------------------------------------------------------
# Per-IP token bucket + a global concurrent/per-minute cap, both process-local in-memory state (see
# ratelimit.py). Built once at import time from config so `DECOY_RATE_*` env vars are read at process
# start, matching how uvicorn itself is configured (a running process doesn't hot-reload its own
# rate-limit knobs).
_per_ip = TokenBucket(capacity=config.get_rate_burst(), rate=config.get_rate_per_minute() / 60.0)
_global = GlobalLimiter(
    max_concurrent=config.get_global_concurrent(),
    max_per_minute=config.get_global_per_minute(),
)
# The ONLY thing this process remembers about visitors across requests: an anonymized in-memory
# tally. No chat content, no per-visitor identity, never written to disk — cockpit-spec.md's "no
# persistence of visitor chats beyond an anonymized counter."
_visits = Counter()

# A request body bigger than this is rejected before it's even JSON-parsed. Generous for a short
# chat turn plus ~10 trimmed exchanges of history, stingy for anything else.
_MAX_BODY_BYTES = 32_000


class ChatMessage(BaseModel):
    role: str
    content: str = Field(default="", max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=20_000)
    # The real, env-tunable cap is config.get_max_message_chars(), applied in the handler; this Field
    # cap is just a hard ceiling so a pathological payload can't reach that logic at all.
    history: list[ChatMessage] = Field(default_factory=list, max_length=200)


def _cap_message(message: str, max_chars: int) -> str:
    """Trim (never reject) an over-length message. A public decoy should degrade gracefully, not
    error out on a chatty visitor — truncating keeps the turn cheap without a hard failure."""
    text = (message or "").strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return text


def _trim_history(history: list[ChatMessage], max_turns: int) -> list[dict]:
    """Keep only the last `max_turns` *exchanges* (user+assistant pairs, i.e. 2 messages each),
    oldest-first, coerced to plain ``{"role", "content"}`` dicts. Also drops anything with an
    unrecognized role or empty content — never trust the client's history beyond that. Excess is
    dropped from the FRONT (oldest first): recent context is what matters, and this also bounds the
    size of every request sent to Ollama regardless of how much history a visitor's browser hands
    back."""
    cleaned = [
        {"role": m.role, "content": m.content}
        for m in history
        if m.role in ("user", "assistant") and m.content
    ]
    max_messages = max(0, max_turns) * 2
    if max_messages and len(cleaned) > max_messages:
        cleaned = cleaned[-max_messages:]
    return cleaned


@app.middleware("http")
async def _security(request: Request, call_next):
    """Belt-and-braces request hygiene: reject oversized bodies before parsing, and stamp every
    response with a small set of standard security headers. No CORS middleware is registered
    anywhere in this app — with none configured, browsers enforce same-origin by default, which is
    exactly the posture this page wants (it has no API consumers other than its own page). No cookie
    is ever set by any route in this module."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            too_big = int(content_length) > _MAX_BODY_BYTES
        except ValueError:
            too_big = False
        if too_big:
            return JSONResponse({"reply": "That's too much for reception to carry."}, status_code=413)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    # 'unsafe-inline' is needed because the page below is one self-contained inline <style>/<script>
    # document by design (no build step, no external assets) — there is nothing else on this origin
    # for a stricter policy to protect.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self'; connect-src 'self'"
    )
    return response


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE_HTML


@app.get("/api/health")
def api_health() -> dict:
    """Public liveness probe. No state dir, no secrets, no per-visitor detail — just proof the
    process is up and the anonymized visit counter (see Counter's docstring)."""
    return {"ok": True, "service": "seneschald-decoy", "visits": _visits.value}


@app.post("/api/chat")
def api_chat(payload: ChatRequest, request: Request):
    """The one real endpoint. Deliberately a plain `def` (not `async def`) — FastAPI runs sync path
    functions in a worker thread automatically, which is exactly what a blocking `urllib` call to
    Ollama needs, with no extra asyncio plumbing (mirrors seneschal/scripts/router.py's stdlib-only
    Ollama pattern, just reached from a thread instead of the daemon's own event loop)."""
    ip = request.client.host if request.client else "unknown"

    if not _per_ip.allow(ip):
        logger.info("rate-limited: per-ip bucket empty")
        return JSONResponse(
            {"reply": "One moment. Reception has a queue, and you're in it."}, status_code=429
        )

    if not _global.try_acquire():
        logger.info("rate-limited: global cap reached")
        return JSONResponse(
            {"reply": "Reception is at capacity just now. Try again shortly."}, status_code=429
        )

    try:
        message = _cap_message(payload.message, config.get_max_message_chars())
        if not message:
            return {"reply": "Yes? I do need something to work with."}

        history = _trim_history(payload.history, config.get_max_history_turns())
        _visits.increment()

        try:
            reply = call_ollama(
                base_url=config.get_ollama_url(),
                model=config.get_model(),
                system_prompt=build_system_prompt(),
                history=history,
                message=message,
                max_output_tokens=config.get_max_output_tokens(),
                timeout=config.get_request_timeout(),
            )
        except OllamaUnavailable:
            logger.warning("ollama unavailable; serving the reception-closed line")
            reply = OLLAMA_DOWN_LINE

        return {"reply": reply}
    finally:
        _global.release()


_PAGE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>seneschald — reception</title>
<style>
  :root {
    --purple: #8e00ff;
    --green: #00ff0f;
    --bg: #0b0a10;
    --panel: #14121c;
    --border: #2a2438;
    --text: #e8e6f0;
    --muted: #8a8698;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    background: radial-gradient(circle at 50% 0%, #1a1626 0%, var(--bg) 60%);
    color: var(--text);
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .desk {
    width: 100%;
    max-width: 640px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 0 40px rgba(142, 0, 255, 0.15);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    height: 80vh;
    max-height: 720px;
  }
  header {
    padding: 18px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: baseline;
    gap: 10px;
  }
  header .dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 8px var(--green);
  }
  header h1 { font-size: 15px; margin: 0; letter-spacing: 0.04em; font-weight: 600; }
  header span.sub { font-size: 12px; color: var(--muted); }
  #log {
    flex: 1;
    overflow-y: auto;
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .msg { max-width: 85%; line-height: 1.5; font-size: 14px; white-space: pre-wrap; }
  .msg.steward { align-self: flex-start; }
  .msg.steward .who { color: var(--purple); font-weight: 600; font-size: 12px; display: block; margin-bottom: 3px; }
  .msg.you { align-self: flex-end; color: var(--muted); }
  .msg.you .who { color: var(--green); font-weight: 600; font-size: 12px; display: block; margin-bottom: 3px; text-align: right; }
  form { display: flex; gap: 8px; padding: 14px 16px; border-top: 1px solid var(--border); }
  input {
    flex: 1;
    background: #0f0d16;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    padding: 10px 12px;
    font-size: 14px;
    outline: none;
  }
  input:focus { border-color: var(--purple); }
  button {
    background: var(--purple);
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 10px 16px;
    font-size: 14px;
    cursor: pointer;
  }
  button:hover { filter: brightness(1.1); }
  button:disabled { opacity: 0.5; cursor: default; }
  footer { padding: 8px 20px 14px; font-size: 11px; color: var(--muted); text-align: center; }
</style>
</head>
<body>
  <main class="desk">
    <header>
      <span class="dot"></span>
      <h1>seneschald — reception</h1>
      <span class="sub">The owner is unavailable. I am not.</span>
    </header>
    <div id="log"></div>
    <form id="composer">
      <input id="input" type="text" autocomplete="off" placeholder="Say something. I've heard it all." maxlength="2000">
      <button id="send" type="submit">Send</button>
    </form>
    <footer>Public demo. Nothing you type reaches anyone or anything real.</footer>
  </main>
<script>
(function () {
  var log = document.getElementById("log");
  var form = document.getElementById("composer");
  var input = document.getElementById("input");
  var button = document.getElementById("send");
  var history = [];

  function addMessage(role, text) {
    var div = document.createElement("div");
    div.className = "msg " + (role === "user" ? "you" : "steward");
    var who = document.createElement("span");
    who.className = "who";
    who.textContent = role === "user" ? "you" : "steward";
    div.appendChild(who);
    div.appendChild(document.createTextNode(text));
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  addMessage("assistant", "Reception. What can I not do for you?");

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    button.disabled = true;
    addMessage("user", text);
    var sentHistory = history.slice();
    history.push({ role: "user", content: text });

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, history: sentHistory }),
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        var reply = (data && data.reply) || "Reception is closed. Leave no message.";
        addMessage("assistant", reply);
        history.push({ role: "assistant", content: reply });
      })
      .catch(function () {
        addMessage("assistant", "Reception is closed. Leave no message.");
      })
      .then(function () {
        button.disabled = false;
        input.focus();
      });
  });
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    # Convenience entry point (`uv run python -m cockpit.decoy.server` / `python cockpit/decoy/server.py`)
    # that actually honors DECOY_HOST/DECOY_PORT — the README's `uvicorn cockpit.decoy.server:app
    # --host ... --port ...` invocation is the primary documented path and doesn't need this, but this
    # keeps those two env vars meaningful for anyone who'd rather not repeat the host/port on the
    # command line (e.g. a scheduled-task wrapper).
    import uvicorn

    uvicorn.run(app, host=config.get_host(), port=config.get_port())
