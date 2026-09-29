# `telegram_send.py --document` — sending the owner a file

**Status:** `BUILT(--document/sendDocument and --photo/sendPhoto shipped in the narrow shape; §3.1's --filename, §3.2's --topic resolution and §4.3's outbound trace are additive follow-ons)` — `telegram_send.send_document` / `--document` and its sibling `send_photo` / `--photo` exist and mirror each other exactly. **Owner:** the assistant.

**Scope is deliberately one method.** The owner scoped it that way on purpose — finish the work in
flight first, and for now spec *only* `sendDocument` in `telegram_send.py`. See §7 for what that
pushes out and why the list is written down rather than dropped.

---

## 1. Why this exists — a verdict being overturned, not a gap being filled

`telegram-capability-map.md` §4 surveyed the **Send-media cluster (17 methods)** and filed it
**"Deliberate"** — surveyed, weighed, consciously not built. That verdict was reasonable on the
evidence available when the survey was written.

**It was falsified by the first real request for a file.** The owner asked for a PDF of a document
the assistant had built. `telegram_send.py` was **text-only** — `--text`, `--text-file`, and nothing
else — so the assistant hand-rolled a `curl` call to `sendDocument` against the raw Bot API.

**Observed facts from that one delivery, which are the whole design input:**

| | |
|---|---|
| It worked | one call, a ~130 KB file |
| It failed first | `ok=false`, because the `document=@/tmp/…` argument was a **POSIX path handed to a Windows `curl.exe`**, which cannot resolve it |
| The token was on the command line | in an interpolated URL, one `set -x` away from a transcript |
| Nothing was recorded | no `state/outbound.jsonl` row, no assertion row — an outbound artefact left no trace |

**The last two rows are the actual argument.** A hand-rolled `curl` for a routine action is not
merely inconvenient: it puts the bot token in an argv string and it bypasses every convention
`telegram_send.py` already implements. **The one-off is more dangerous than the feature.**

---

## 2. Scope

**IN:** `sendDocument`, from `telegram_send.py`, to the owner's own chat.

**OUT, and each for a reason rather than by omission:**

- `sendVideo` / `sendAudio` / `sendMediaGroup` — the rest of the cluster stays `Deliberate` until
  something falsifies it the way the first file request falsified this one. **One falsification, one
  method.**
- Any recipient other than the owner (§5).
- Everything in §7.

**`sendPhoto` was falsified the same way, separately:** a confirmation ask needed to carry an evidence
photo rather than be sent blind, and there was no photo-send door either. `telegram_send.send_photo`
/ `--photo` built it, narrowly — no ack gate, no dedupe, no chunking, no `state/outbound.jsonl` trace
(§4.3's argument for a trace is about a *document*, which nobody re-reads at send time; a photo
attached to a picker the same turn it is sent has no such gap). **This does not widen the verdict on
the rest of the cluster** — `sendVideo`/`sendAudio`/`sendMediaGroup` stay `Deliberate`.

**This spec's own `--document` then shipped — but as narrow as `sendPhoto`, not as designed below.**
`telegram_send.send_document` / `--document` mirrors `send_photo`'s actual (narrower) shape exactly,
not §3's full interface. Concretely, what shipped differs from §3/§4 in three ways — **no
`--filename` override** (§3.1; the basename is always used, same as `--photo`), **no `--topic`
resolution** (§3.2; `--message-thread-id` passes through exactly as `--photo`'s does, but a bare
`--document` has no route to a resolved topic the way a text send does), and **no
`state/outbound.jsonl` trace** (§4.3; `sendPhoto` shipped without one for the same reason — read that
paragraph's carve-out, which was written for a photo but applies just as much to a document attached
to a routine send rather than re-read later). Should any of the three turn out to matter in practice,
they are additive on top of what exists, not a redesign. The 50 MB ceiling (§4.2) *did* ship: the
size is checked before the file is read into memory.

---

## 3. Interface

```
telegram_send.py --document PATH [--caption TEXT] [--filename NAME]
                 [--topic PURPOSE | --message-thread-id N]
                 [--format {plain,markdown}] [--chat-id ID] [--dry-run]
```

| Flag | Behaviour |
|---|---|
| `--document PATH` | The file. **Mutually exclusive with `--text` / `--text-file`** — a document send is not a text send with an attachment, and conflating them makes the failure modes ambiguous. Refuse with exit 2. |
| `--caption TEXT` | Optional. Telegram caps captions at **1024 characters**; refuse locally rather than letting the API truncate silently. |
| `--filename NAME` | Optional override. **Defaults to the basename of PATH.** |
| everything else | Existing flags keep their existing meaning. |

### 3.1 The filename is the one irreversible choice

**The name the file is uploaded under is the name the owner sees in Telegram forever** — in the
chat, in their downloads, in search, on every device. `resume.pdf` is a filename in a working
directory; a dated, descriptive name is what a document in a chat should be called.

**Default to the basename** (predictable, no magic), **and make `--filename` cheap** so a caller
that knows better says so. **No auto-renaming, no date-stamping, no inference** — a helpful rename
the assistant invented is a file the owner cannot find later by the name they expect.

### 3.2 Topic routing is not optional

`--topic` / `--message-thread-id` must apply to a document exactly as they do to text. A document
sent to the main chat when the conversation is in a `projects` topic is the same failure the topic
work was built to end, and `sendDocument` takes `message_thread_id` natively — there is no reason for
it to be a second code path.

---

## 4. Implementation notes

### 4.1 Stdlib multipart, never a subprocess

**`urllib.request` with a hand-built multipart/form-data body.** No `curl`, no `requests`, no new
dependency — `telegram_http.py` already owns the HTTP boundary (`build_multipart`) and this belongs
beside it.

**Three reasons, in order of weight:**

1. **The token stays out of argv.** The `curl` form interpolates it into a URL. In-process it is part
   of a request object that never reaches a shell.
2. **The path bug becomes structurally impossible.** The first failure was a POSIX path handed to a
   Windows binary. Python opens the file itself; there is no second path grammar to get wrong.
3. It is testable without the network, through the same seam the rest of the module uses.

### 4.2 Refuse locally what Telegram would refuse remotely

| Condition | Behaviour |
|---|---|
| File does not exist / not readable | exit 2, name the path |
| Size **> 50 MB** | exit 2, state the limit and the actual size. *(Bot API upload cap for `sendDocument`.)* |
| Caption > 1024 chars | exit 2, state both numbers |
| Both `--document` and `--text` | exit 2 |

**A local refusal is better than a 4xx** — it names the real problem instead of Telegram's
description of a symptom, and it costs no round trip.

### 4.3 Leave a trace

**The hand-rolled send left none, and that is the defect this spec cares most about.** A document is
an *outbound artefact*: it leaves the machine, it is not recallable after 48 h, and it is the exact
class the approval gate exists for.

- Append a row to **`state/outbound.jsonl`** — path, filename, size, sha256, caption, chat, topic,
  `message_id`, timestamp.
- **The sha256 is the load-bearing field.** It answers *"is the file in the chat the same bytes as
  the one on disk now?"* — which, for a document that gets rebuilt from a Markdown source, is a
  question that will be asked and otherwise cannot be answered.
- Never log the token. Never log file *contents*.

### 4.4 Failure output

On `ok: false`, exit non-zero and print Telegram's own `description` verbatim. **Do not paraphrase
it** — the description is the only information the API gives, and a reworded version is a claim
about a failure rather than the failure.

---

## 5. The approval gate

**A document to the owner's own chat is act-low**, on exactly the reasoning text already rests on: it
is the assistant telling the owner something, in the owner's own chat, at their request.

**A document to any other `chat_id` is ask-high and this spec does not authorise it.** The
difference between a text and a document matters here in a way it does not elsewhere: **a file
carries content nobody re-read at send time.** A CV, a health export, a job's log — the caller knows
the path, not necessarily the bytes. **The gate keys on the recipient, and no caller may send a
document to a third party without the owner's explicit approval.**

---

## 6. Testing

- `--dry-run` prints the constructed request — method, chat, topic, filename, size, caption, and
  the sha256 — and sends nothing. **No live call in CI, ever.**
- Unit tests drive the `api=` transport seam with a fake response, the pattern
  `ask_citations._fetch_head_content` already uses.
- The multipart body is asserted structurally (boundary, `Content-Disposition`, filename encoding),
  because a malformed body is the failure mode that produces a confusing server-side error.
- **A test that would have caught the original bug:** a path containing characters the other grammar
  mishandles, asserted to open successfully in-process.

---

## 7. Deferred — written down so it is not lost

The owner raised these in the same request and then scoped them out. **They are parked, not
rejected.**

1. **A full pass over `telegram-capability-map.md`** — going through the survey and specing the
   advanced items.
2. **A free-text field on pickers.** Replying to a picker with an option it did not offer works as a
   side-step, but it leaves the picker open. **Note the real complaint underneath it: an open picker
   with no answer that fits is a picker the owner cannot close.** That is a state problem as much as
   an input problem, and it is worth reading alongside `picker-state-marking-spec.md` when it is
   picked up. `force_reply` (§2.1 ⑤ of the capability map) is the Bot API surface to evaluate first.

**Neither is scheduled.**
