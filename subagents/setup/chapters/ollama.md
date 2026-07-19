# Sub-chapter: ollama (on-demand)

Not a chapter of its own on the board — the env walker runs this **inline** the first time an
enabled entry declares the `ollama` dependency (router / rag / sentiment), and its outcome
lives in the ledger's `deps.ollama` fact, not a chapter row. It exists so the walker stays
generic and the "install a local model server" conversation happens at most once.
**Nothing here ever blocks the wizard** — every exit path is a recorded fact plus honest
degradation notes on the features that wanted it.

## 1 — Where would it be?

Ask **before** probing: "Is Ollama already running somewhere non-default?" — existing
installs commonly serve on a moved port or another host. Default `http://localhost:11434`;
the owner's answer is `<url>` below.

## 2 — Detect

```
python -c "import urllib.request; urllib.request.urlopen('<url>/api/tags', timeout=5); print('ollama reachable')"
```

- **Reachable** → step 4.
- **Unreachable** → is it installed but not serving? `ollama --version`. Installed → step 3's
  serve check. Not installed → offer to install, per-OS, **each command individually
  confirmed** before running (the SKILL's installs-need-confirmation rule):
  - **Windows:** `winget install Ollama.Ollama`
  - **macOS:** `brew install ollama`
  - **Linux:** the official script — `curl -fsSL https://ollama.com/install.sh | sh` — with
    an explicit warning **before** offering it: *the script invokes `sudo` internally* (it
    installs a system service). The no-sudo alternative: download the release tarball from
    Ollama's GitHub releases, unpack under the home directory, and run `ollama serve`
    user-level — offer both and let the owner pick.

  Declined install → record `deps.ollama = {status: "declined", url: "<url>", models: []}`
  (the ledger-facts pattern from the SKILL) and return: the walker marks each dependent
  entry `declined` with its manifest degradation note (router: feature inert, runtime
  abstains to escalate anyway; rag: retrieval falls back to store search; sentiment:
  abstains to neutral).

## 3 — Serve check, with retries

The desktop apps (Windows/macOS) start a background service on launch; on Linux the installer
registers a service, or the owner runs `ollama serve` themselves. After install/start, retry
the step-2 probe a few times over ~30 seconds. Still unreachable → record
`deps.ollama = {status: "unreachable", url: "<url>", models: []}`, tell the owner what to
check (is the app running? firewall? the URL?), and return — dependent entries get
`blocked` (not declined — the owner said yes) with the same degradation notes.

## 4 — Pull the models

Compute the **union of `ollama_models` across the enabled entries that depend on ollama**
(read from the manifest — e.g. router + sentiment share one model, rag brings the embedder;
one pull covers sharers). For each model not already in the `/api/tags` listing:

- Say the size class honestly before pulling (an embedding model is a few hundred MB; a 4B
  chat model is a few GB — the pull output shows the real number), and **confirm each pull**:

  ```
  ollama pull <model>
  ```

- A failed pull (disk, network) → keep going with the rest; the entries whose model is
  missing get `blocked` with a "model not pulled" summary.

Re-list `/api/tags` afterward and record the outcome:

```
python -c "import sys; sys.path.insert(0, 'seneschal/scripts'); import setup_state; s = setup_state.load(); s['deps']['ollama'] = {'status': 'ready', 'url': '<url>', 'models': ['<pulled>', '...']}; setup_state.save(s)"
```

## Return

Hand control straight back to the env walker at the entry that triggered this. If `<url>` is
non-default, the walker uses it as the default for every dependent entry's `OLLAMA_URL` var
— set once here, inherited everywhere.
