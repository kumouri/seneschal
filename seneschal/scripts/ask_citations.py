#!/usr/bin/env python3
"""**A picker may not cite what it does not show.** Resolve every document/section reference in a
Telegram question and inline an excerpt of it, or refuse to send. Standard library only.

## The rule, and why it is here instead of written down

*If you reference a document or section, describe the section; an annotation alone is not enough.*
As a written rule that is prose, and prose is exactly the thing that does not bind: a caller under
time pressure cites "§8.2" and moves on, and the owner has to stop and go find out what §8.2 says
before they can decide anything.

So the mechanism is the one `telegram_ask.parse_option` already uses for the other half of the
picker rule — the rule made into a shape a violation cannot fit into. An option with no description
is refused. **A citation with no excerpt is impossible, because this module writes the excerpt
itself.**

## RESOLVE, do not merely check

The cheap version of this would be a validator: *does the message contain a section reference and
also some prose near it?* That is a heuristic about writing, it is guessable, and a caller working
around it produces a sentence that satisfies the checker and not the reader.

This resolves instead. A reference is looked up **in the repo**, its section is found, and an excerpt
is appended to the message body by this code. The caller does not have to remember anything, and
there is no wording that passes while leaving the owner without the text.

## The refusal, and why the polarity is the opposite of `ask()`'s provenance stamp

`ask()`'s `origin` block fails **open**: an exotic environment costs a field, never the question,
because a *suppressed question* is real harm and an unattributed one is merely worse bookkeeping.

**This fails closed.** A picker citing a section that does not exist is worse than no picker: it asks
the owner to decide against a document they cannot read, and the citation is precisely the part that
makes the ask look grounded. A refusal is loud, lands in the caller's face at exit 2, and costs one
edit; a confident citation of a §-number that was renamed last week costs the owner the decision.

## What counts as a reference

Two shapes, both purely syntactic:

  * a **document** — a token ending in `.md`, path-qualified or not (`mouth-spec.md`,
    `seneschal/docs/mouth-spec.md`, `./CLAUDE.md` for one at the repo root);
  * a **section** — `§` followed by a dotted number (`§8`, `§8.2`, `§10.6`).

A section must be **anchored**: it is read against the nearest `.md` document named *before* it in
the same text. **A section reference with no document named anywhere is refused**, and that is not an
edge case — it is the exact failure this module was built for. `§8.2` alone is an annotation with no
referent; even a human who wanted to look it up could not.

Deliberately NOT treated as references: a `.py`/`.json`/`.ps1` path (a file name says what it is; a
section number does not), a bare `#heading`, a URL, and anything inside a quoted span (below).

## Quoted spans — the boundary between what the assistant wrote and what it is relaying

A merge-approval picker (e.g. the optional `merge_guard` module's) is built around a **PR title and
description written by someone else**, and those routinely cite sections and spec files. Refusing on
those would mean a PR whose body mentions a file it is itself deleting could not be approved — the
merge picker would fail on exactly the PRs that most need it.

So a caller passes the reproduced text as a **quoted span** (`--quote`), and references inside it are
neither resolved nor excerpted. This is an exemption for *relayed* text, not an opt-out from the
mechanism: there is no flag that turns the check off for text the assistant wrote itself. **A span
that does not actually occur in the message is refused**, because an exemption that silently fails to
apply is worse than none.

## Why there is no `--no-citations`

The escape hatch is to not cite, or to quote what you are relaying. A flag that disables this is a
rule a caller has to remember not to use, which is the category of thing that does not bind — see
the top of this docstring.

## Resolving against a PR's own head

`resolve_doc` reads the **checkout** — effectively the integration branch for every caller here —
because that is the tree the assistant is actually running against. That is also, permanently, the
wrong tree for one shape of citation: a merge-approval picker lists the PR's own changed `.md` paths
in its header (deliberately, so a blocker can be cited), and a path the PR **creates** does not exist
in the checkout yet. No wording fixes that — the file is genuinely not there — so without a fallback
a PR that adds so much as one `.md` under a guarded directory would be unapprovable by picker,
permanently.

The decision: a cited path may be looked up in the PR's own tree, so a file the PR creates resolves.
That keeps the guard's actual promise — never show a citation it cannot open — because it CAN open
it, from the head.

`PRHead` is the fallback :func:`resolve_doc` and :func:`section_excerpt` consult **only after the
checkout has already missed**, and **only for a path its caller already vouches for** — a closed
allow-list, not a wider "cite anything from the PR" door. A caller builds one from exactly the paths
its own header is about to print, never from the PR's full changed-file list. See `PRHead`'s own
docstring for where the content comes from, the one network call it costs, and every way it fails
closed.

## The deleted-file case

A `.md` a PR adds and then deletes again, all within its own still-open history, never existed on
the checkout and is gone at the PR's own head too — a merge picker still lists it (correctly: a PR
deleting a guarded path is worth flagging), `resolve_doc` still resolves a path for it (`head.has()`
only checks the allow-list, not the content), and the excerpt attempt then fails. That failure would
otherwise read identically to `gh` being offline or rate-limited — "could not be read... not from the
checkout, and not from the PR's own head either" — which is a true statement but not the useful one.
`_fetch_head_content` tells a confirmed HTTP 404 apart from every other failure (`NOT_FOUND_AT_HEAD`,
exposed through `PRHead.deleted`), and `citation_block` says the honest thing instead: this PR deletes
the file, so there is nothing left to excerpt.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

#: A markdown document token. Deliberately narrow: no spaces, must end in `.md`.
#:
#: **The optional leading `./` is the repo-root qualification :func:`resolve_doc` honours, and
#: it has to survive the SCAN to reach the resolver.** If the token were matched from its first
#: alphanumeric, a picker that spelled `./CLAUDE.md` would hand `resolve_doc` a bare, ambiguous
#: `CLAUDE.md` and the send would be refused anyway. Fixing the
#: resolver alone would have left the caller with no spelling that worked.
#:
#: The lookbehind keeps `../docs/x.md` reading exactly as it always has — matched at `docs/`,
#: the whole token unresolvable — because the `./` inside a `../` is the tail of a
#: parent-directory hop and not a root qualification, and quietly reading one as the other
#: would resolve a pointer to a file nobody named.
DOC_RE = re.compile(r"(?:(?<![A-Za-z0-9_./])\./)?[A-Za-z0-9_][A-Za-z0-9_.\-/]*\.md\b")
#: A section reference. `§` plus a dotted number. `§§` (a range) is matched at its first number,
#: which is the one whose text answers "what does that say".
SECTION_RE = re.compile(r"§+\s?(\d+(?:\.\d+)*)")

#: **The one spelling that means *the repo root, exactly this file*.** Two characters, matched
#: literally — see :func:`resolve_doc` for why `lstrip` is not how you take them off.
ROOT_PREFIX = "./"

#: Where a bare document name is looked for, in order. Repo-root first, then the design record —
#: which is where every reference this was built for points.
DOC_SEARCH_DIRS = ("", "seneschal/docs", "seneschal", "seneschal/references", "seneschal/scripts",
                   "seneschal/state")

#: Telegram's own `sendMessage` cap. The body must fit inside one message: `telegram_send` will
#: chunk a long send, and a picker whose keyboard lands on chunk 3 is a broken picker.
TELEGRAM_MESSAGE_CHARS = 4096
#: Head-room under that cap for the HTML expansion `telegram_format` may apply, and for the fact
#: that Telegram counts UTF-16 code units rather than characters. Not tight, deliberately.
CITATION_SAFETY_MARGIN = 400
#: An excerpt shorter than this does not describe a section, it teases one. If the arithmetic cannot
#: give every citation at least this much, the message is refused rather than trimmed into
#: uselessness — which is the failure this whole module exists to prevent, in miniature.
MIN_EXCERPT_CHARS = 200
#: No single excerpt eats the whole budget when there are several.
MAX_EXCERPT_CHARS = 700

CITATION_HEADER = "— what those references say —"
#: What one citation costs on top of its excerpt: the ` — ` between its description and the
#: section title, the newline before the excerpt, and the blank line that separates it from the
#: next block. The section title itself is not budgeted — it is short, and
#: :data:`CITATION_SAFETY_MARGIN` is what absorbs it.
DESCRIBE_OVERHEAD = 6
#: The header line plus the blank lines around it.
HEADER_COST = len(CITATION_HEADER) + 4


def min_room(descriptions) -> int:
    """The smallest `room` :func:`citation_block` will accept for citations described by
    `descriptions` — the header, each description's overhead, and :data:`MIN_EXCERPT_CHARS` for
    every one of them.

    **This is the arithmetic `citation_block` refuses on, exported so a caller can reserve for it
    BEFORE it spends the message on something else.** A caller that cites its own paths in a
    header and then cuts a summary to whatever is left inside the Telegram cap — with nothing held
    back for the excerpts those paths need — gets refused by the gate and sends no picker at all.
    Two copies of this sum would drift; one, here, cannot.

    `descriptions` are the strings :func:`_describe` would produce — for a document citation with
    no section, exactly the path as it appears in the message."""
    descriptions = list(descriptions)
    if not descriptions:
        return 0
    return (HEADER_COST + sum(len(d) + DESCRIBE_OVERHEAD for d in descriptions)
            + MIN_EXCERPT_CHARS * len(descriptions))


#: Returned by :func:`_fetch_head_content` (and, through it, `PRHead.read`) instead of `None` for the
#: one failure this module can name with confidence — the path is genuinely gone at the PR's head,
#: an HTTP 404, not merely unreadable. Compared by identity only; never constructed anywhere else.
NOT_FOUND_AT_HEAD = object()


class CitationError(ValueError):
    """A reference that could not be resolved, or an excerpt that could not fit.

    A `ValueError` so `_cmd_ask`'s existing `parse_option` handling — the other half of the same
    rule, refused the same way — catches it without a second code path."""


# --------------------------------------------------------------------------- finding references

def _mask_spans(text: str, quoted) -> str:
    """Blank out each quoted span so references inside it are invisible to the scanners below.

    Replaced with spaces rather than removed, so every offset in the masked string still lines up
    with the original — the anchoring in :func:`find_references` reads positions."""
    masked = text
    for span in (quoted or []):
        span = (span or "").strip()
        if not span:
            continue
        idx = masked.find(span)
        while idx != -1:
            masked = masked[:idx] + (" " * len(span)) + masked[idx + len(span):]
            idx = masked.find(span, idx + len(span))
    return masked


def missing_spans(text: str, quoted) -> list:
    """Quoted spans that do not occur in the text. An exemption that silently fails to apply is
    worse than no exemption — it reads as protection and is not."""
    return [s for s in (quoted or []) if (s or "").strip() and (s or "").strip() not in text]


def find_references(text: str, quoted=None) -> list:
    """Every reference in `text`, in order, as `{"raw", "doc", "section"}`.

    A document token yields a reference with `section=None`. A section token yields one anchored to
    the nearest document named **before** it; with no document before it anywhere, `doc` is None and
    the caller refuses. Duplicates are collapsed on `(doc, section)`, so citing §8.2 twice costs one
    excerpt."""
    if not isinstance(text, str) or not text:
        return []
    masked = _mask_spans(text, quoted)

    hits = []
    for m in DOC_RE.finditer(masked):
        hits.append((m.start(), "doc", m.group(0)))
    for m in SECTION_RE.finditer(masked):
        hits.append((m.start(), "sec", m.group(1)))
    hits.sort()

    out, seen, current_doc = [], set(), None
    for _pos, kind, value in hits:
        if kind == "doc":
            current_doc = value
            key = (value, None)
            ref = {"raw": value, "doc": value, "section": None}
        else:
            key = (current_doc, value)
            ref = {"raw": "§" + value, "doc": current_doc, "section": value}
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)

    # A document cited AND sectioned is described by its sections. Keeping both would spend the
    # excerpt budget twice on one file and push the specific answer — the section actually
    # asked about — further down the message, which is the opposite of the point.
    sectioned = {r["doc"] for r in out if r["section"] is not None}
    return [r for r in out if r["section"] is not None or r["doc"] not in sectioned]


# --------------------------------------------------------------------------- resolving them

def _norm_head_path(path: str) -> str:
    """The same repo-relative spelling :func:`resolve_doc` produces from a name it read off disk —
    forward slashes, the literal two-character `./` prefix removed and nothing more. Reusing
    `lstrip("./")` here would reintroduce the exact bug `resolve_doc`'s own docstring documents:
    `.keeprc.md` silently becoming `keeprc.md`, a name nobody wrote."""
    text = (path or "").strip().replace("\\", "/")
    if text.startswith(ROOT_PREFIX):
        text = text[len(ROOT_PREFIX):]
    return text


def _fetch_head_content(repo: str, sha: str, path: str, timeout: int = 15, runner=None):
    """`path`'s raw text at commit `sha` in `repo`, via one `gh api repos/<repo>/contents/<path>`
    call, or `None` on **any** failure: `gh` not on PATH, a non-zero exit, a timeout, output that is
    not JSON, a response that is not the base64-encoded-file shape the contents endpoint returns for
    a file (a directory listing is a JSON *array*, not an object, and lands here too), no `content`
    field, or content that will not decode as base64 UTF-8 text. There is deliberately no partial
    answer — a caller that got `None` cannot tell WHY, on purpose, because the only thing it may ever
    do with a failure is refuse the citation exactly as it does for a file that was never there.

    **One exception, and it returns :data:`NOT_FOUND_AT_HEAD` instead of `None`**: `gh` reporting an
    HTTP 404 for this exact `path`/`sha` pair means the path genuinely does not exist at that commit
    — knowable and sayable, unlike a network blip or a missing `gh` binary — and `PRHead.deleted`
    exists so a caller CAN say it instead of folding it into the generic "could not be read" refusal.
    Matched off `gh`'s own stderr shape (`gh: <message> (HTTP <code>)`) rather than parsed from a
    structured field, because `gh api`'s non-zero exit carries no structured error body to read.

    `runner` is the one subprocess seam — defaults to
    `subprocess.run`, so a test can inject a fake `(returncode, stdout)`-shaped result without
    actually calling `gh` or the network."""
    runner = runner or subprocess.run
    try:
        proc = runner(
            ["gh", "api", "repos/%s/contents/%s?ref=%s" % (repo, path, urllib.parse.quote(sha, safe=""))],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        if re.search(r"\bHTTP\s+404\b", getattr(proc, "stderr", "") or ""):
            return NOT_FOUND_AT_HEAD
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    raw = data.get("content")
    if not isinstance(raw, str):
        return None
    try:
        return base64.b64decode(raw, validate=False).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


class PRHead:
    """Where a cited path resolves when the checkout does not have it — the picker's OWN pull
    request, at its own head, and ONLY for a path the caller already vouches for. See the module
    docstring's "Resolving against a PR's own head" section for the decision this implements.

    `paths` is a CLOSED allow-list, normalized with :func:`_norm_head_path` — exactly the paths the
    caller already lists in the picker's own header band (its own changed-file list, never
    the PR's full one). **A path not in this set is never resolved against the head, however
    plausible it looks** — that is the difference between this and a general "cite anything from the
    PR" escape hatch, which this deliberately is not.

    **Content comes from one `gh api` call per path that misses the checkout** (:func:`_fetch_head_content`
    by default, overridable for tests), memoized on this instance so a picker citing the same
    head-only file twice still costs one request and a picker with no head-only citation costs zero —
    this is a miss-only fallback, never a cost paid by every picker. **Every failure closes, never
    opens**: `read()` returns `None` for `gh` missing, a non-zero exit, a timeout, an unparseable
    response, or undecodable content, and :func:`section_excerpt` treats that `None` exactly like a
    file that was never there — refused, not shown partially, not shown from a stale guess."""

    def __init__(self, repo: str, sha: str, paths, fetch=None):
        self.repo = (repo or "").strip()
        self.sha = (sha or "").strip()
        self.paths = frozenset(_norm_head_path(p) for p in (paths or []) if (p or "").strip())
        self._fetch = fetch or _fetch_head_content
        self._cache: dict = {}

    def has(self, path: str) -> bool:
        return bool(self.repo and self.sha) and _norm_head_path(path) in self.paths

    def read(self, path: str):
        """`path`'s text at this head, or `None` (including :data:`NOT_FOUND_AT_HEAD`, a `None`-like
        failure for every caller that just wants content — see :meth:`deleted` for the one caller
        that needs to tell the two apart). Memoized; a failure is cached too, so it is not retried
        once per reference to the same path within one picker."""
        norm = _norm_head_path(path)
        if norm not in self._cache:
            self._cache[norm] = self._fetch(self.repo, self.sha, norm) if self.has(norm) else None
        return self._cache[norm]

    def deleted(self, path: str) -> bool:
        """True iff `path` is one this `PRHead` vouches for AND `gh` confirmed, specifically, that it
        does not exist at this commit (an HTTP 404) — as opposed to any other reason `read` might come
        back empty. The one case :func:`citation_block` can name honestly ("this PR deletes that
        file") instead of the generic "could not be read" it uses for every other failure."""
        return self.has(path) and self.read(path) is NOT_FOUND_AT_HEAD


def resolve_doc(name: str, root: str | None = None, head: PRHead | None = None):
    """A document token -> a repo-relative path, or None. Ambiguity is None too, deliberately.

    A bare `CLAUDE.md` matches a dozen files in this tree, and picking one would make the excerpt a
    guess wearing a citation's clothes. The refusal message says so and the fix is to path-qualify —
    which is what `check_context_pointers.py` asks of every other pointer in the repo anyway.

    **`./NAME` IS THAT QUALIFICATION FOR A FILE AT THE REPO ROOT.** Without it the refusal
    instructed the caller to do a thing this function made impossible: every other file in the tree
    can be qualified with a directory, and a root file has none, so there was NO spelling of
    `CLAUDE.md` that resolved. `./CLAUDE.md` was stripped to `CLAUDE.md` on the first line and went
    down the ambiguous-basename branch beside it.

    That was a hard deadlock rather than an inconvenience. The repo-root `CLAUDE.md` is a guarded
    prompt path, so a PR touching it needs the owner's approval; the picker is the only route to that
    tap, and the picker names its blockers. **Such a PR could be neither merged nor asked about.**

    So a leading `./` means *repo root, exactly this path* — **one place to look, exactly like a
    token containing `/`** — and a miss returns None with **no candidate list**, because the caller
    said which file it meant and that file is not there. It does not fall through to the basename
    search: a fallback would turn the qualification back into the guess it was written to replace.

    **A bare name still refuses, and that is the point rather than an omission.** Preferring the
    root for `CLAUDE.md` would make the excerpt a guess wearing a citation's clothes, which is the
    exact thing the refusal exists to prevent. The dot-slash is the caller SAYING which one it
    means; silence is not a quieter way of saying it.

    **Only the literal two characters come off.** `lstrip("./")` strips any leading run of `.` and
    `/` characters, not the prefix — so `.env.example` silently became `env.example`, a name the
    caller never wrote, and was then hunted for in six directories under it.

    **`head` is consulted ONLY after the checkout has already missed**, never in place of it and
    never before it (see the module docstring). A qualified name still checks
    `head.has(name)` as its one fallback before returning None; a bare name is searched against
    `head`'s allow-list through the same `DOC_SEARCH_DIRS` loop the checkout uses, so a bare name
    that exists in both the checkout and `head`'s list, or in `head`'s list under two directories,
    is still ambiguous rather than a guess. A path the checkout already has is never re-checked
    against `head` at all — this fallback exists for a file the PR CREATES, not to prefer the PR's
    version of one that already exists."""
    root = root or REPO_ROOT
    name = (name or "").strip()
    rooted = name.startswith(ROOT_PREFIX)
    if rooted:
        name = name[len(ROOT_PREFIX):]
    if not name:
        return None, []
    # Path-qualified: exactly one place to look. `./x` qualifies a root file the way `a/x` qualifies
    # one in a directory, so both land here — and an explicit qualification that misses is a miss,
    # never a bare name to go searching for.
    if rooted or "/" in name:
        if os.path.isfile(os.path.join(root, name)):
            return name, []
        if head is not None and head.has(name):
            return name, []
        return None, []
    found = []
    for rel_dir in DOC_SEARCH_DIRS:
        candidate = os.path.join(rel_dir, name) if rel_dir else name
        if os.path.isfile(os.path.join(root, candidate)):
            found.append(candidate.replace("\\", "/"))
    if not found and head is not None:
        # The checkout has nothing under this bare name at all — the shape a PR-created file takes,
        # since it exists nowhere in `develop` yet. Retry the SAME search against `head`'s allow-list
        # instead of the filesystem; still one candidate or ambiguous, never a guess.
        for rel_dir in DOC_SEARCH_DIRS:
            candidate = (os.path.join(rel_dir, name) if rel_dir else name).replace("\\", "/")
            if head.has(candidate):
                found.append(candidate)
    if len(found) == 1:
        return found[0], []
    return None, found


def _heading_number(line: str):
    """The section number a markdown heading declares, or None. `## 8. Recommendation` -> `8`,
    `### 8.2 BUILD the …` -> `8.2`, `### 5.1 Phase 0 …` -> `5.1`."""
    m = re.match(r"^#{1,6}\s+(\d+(?:\.\d+)*)[.)]?\s", line)
    return m.group(1) if m else None


def _read_doc_lines(path: str, root: str, head: PRHead | None = None):
    """The document's lines — from the checkout if it is there, else from `head` when `head` vouches
    for `path` (its `has()` gate is applied inside :meth:`PRHead.read`, not duplicated here). `None`
    when neither has it: a checkout miss with no `head`, or a `head` whose fetch itself failed — the
    one seam that turns EITHER shape of unreadable into the same "not there" :func:`section_excerpt`
    already refuses on, so a network failure degrades to a refusal, never to a partial excerpt."""
    try:
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            return fh.read().replace("\r\n", "\n").split("\n")
    except OSError:
        pass
    if head is not None:
        text = head.read(path)
        # `isinstance` rather than `is not None`: `head.read` can come back `NOT_FOUND_AT_HEAD`
        # (deleted, not merely unreadable) — a non-string failure this function treats exactly like
        # any other "no content" case, leaving the DISTINCTION to `PRHead.deleted` for the one caller
        # that needs it (`citation_block`), so it can say something more honest than this one does.
        if isinstance(text, str):
            return text.replace("\r\n", "\n").split("\n")
    return None


def _section_from_lines(lines: list, section: str | None, path: str):
    """`(title, body_lines)` located within already-read `lines`, or None when the section is not
    there. Pure — no I/O, so it reads identically whether `lines` came from disk or from a PR's head.
    `path` is used only for the document-citation title fallback (its basename)."""
    start = None
    if section is None:
        for i, line in enumerate(lines):
            if line.startswith("# "):
                start = i
                break
        if start is None:
            start = 0
            title = os.path.basename(path)
        else:
            title = lines[start].lstrip("# ").strip()
    else:
        for i, line in enumerate(lines):
            if _heading_number(line) == section:
                start, title = i, lines[i].lstrip("# ").strip()
                break
        if start is None:
            return None

    body = []
    for line in lines[start + 1:]:
        # Break on a REAL heading only. A `> ### …` inside a blockquote is part of this section's
        # own prose, and treating it as a boundary truncates the section at its most important
        # paragraph — which is where a recorded decision usually lives in this repo.
        if line.startswith("#"):
            break
        body.append(line)
    return title, body


def section_excerpt(path: str, section: str | None, limit: int, root: str | None = None,
                     head: PRHead | None = None):
    """`(title, text)` for a section of a document, or None when the section is not there OR the
    document itself could not be read (see :func:`_read_doc_lines` — both shapes return None here,
    identically to before `head` existed).

    With `section=None` the document's own H1 and opening paragraph are the excerpt — a document
    citation describes the document.

    The text stops at the next heading of ANY level, so an excerpt never silently spans into a
    sibling section and attributes its words to the one that was cited.

    `head`, when given, is tried only after the checkout read fails — see `PRHead`'s docstring for
    where its content comes from and every way it fails closed instead."""
    lines = _read_doc_lines(path, root or REPO_ROOT, head)
    if lines is None:
        return None
    got = _section_from_lines(lines, section, path)
    if got is None:
        return None
    title, body = got
    return title, _condense(body, limit)


def _condense(lines, limit: int) -> str:
    """Markdown lines -> something readable in a Telegram message body, cut on a word boundary.

    Only the markup that is noise on a phone is stripped — blockquote markers, heading hashes, list
    bullets, emphasis, link syntax, HTML comments. The words are left alone: this is an excerpt of
    the design record, not a paraphrase of it. Truncation is marked with `…`, because an excerpt
    that silently stops mid-thought is the same lie as a citation with no text at all.

    Line-wise rather than over one joined string, because a blockquote marker and a heading hash
    both live at a LINE start and are invisible once the lines are joined.

    **An HTML comment (e.g. a CI checker's escape-hatch marker) renders as nothing on GitHub and
    must render as nothing here too** — it is CI-facing metadata about the source line, not part of
    what the owner is being asked to read, and counting its
    characters against the excerpt budget can truncate the excerpt before the text the citation was
    for."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    cleaned = []
    for line in lines:
        line = re.sub(r"<!--.*?-->", "", line or "")        # HTML comment, e.g. a CI marker
        line = re.sub(r"^\s*(?:>\s?)+", "", line)           # blockquote, however deep
        line = re.sub(r"^\s*#{1,6}\s*", "", line)           # a heading inside a quote
        line = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", line)  # a list bullet
        cleaned.append(line)
    text = " ".join(cleaned)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)   # links -> their text
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)", r"\1", text)
    # A bold run WRAPPING a nested emphasis (`**a *b* c**`) survives the pair rules above and leaves
    # a stray `**` in the message. Sweep what is left rather than growing a cleverer regex: the
    # marker is noise on a phone however it got here, and the words are untouched either way.
    text = text.replace("**", "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:—-") + "…"


# --------------------------------------------------------------------------- the whole job

def _describe(ref: dict) -> str:
    if ref["section"] is None:
        return ref["doc"]
    return "%s %s" % (ref["doc"] or "(no document named)", ref["raw"])


def citation_block(text: str, quoted=None, room: int = 0, root: str | None = None,
                    head: PRHead | None = None):
    """`text` -> the excerpt block to append, or `""` when it cites nothing.

    **Raises `CitationError` rather than degrading.** Every failure here is a picker that would have
    cited something it could not show: an unresolvable document, an ambiguous bare name, a section
    that is not in the file, a document that resolved (from the checkout or from `head`) but could
    not actually be read, a section with no document named at all, or a message with too little
    room left to describe what it cited.

    `room` is how many characters the finished block may occupy. It is computed by the caller from
    the rendered body, so the arithmetic sees the real message rather than an estimate.

    `head`, when given, is a :class:`PRHead` — consulted only for a path the checkout does not have,
    and only when that path is one `head` was built to vouch for. See its docstring and the module
    docstring's "Resolving against a PR's own head" section."""
    refs = find_references(text, quoted)
    if not refs:
        return ""

    bad_spans = missing_spans(text, quoted)
    if bad_spans:
        raise CitationError(
            "quoted span not found in the message: %r. A --quote that does not occur in the text "
            "exempts nothing, so it is refused rather than ignored." % bad_spans[0][:80])

    unanchored = [r for r in refs if r["section"] is not None and not r["doc"]]
    if unanchored:
        raise CitationError(
            "%s names no document. A section number on its own is an annotation, not a citation — "
            "nobody can look it up, which is the exact thing this refuses. Write "
            "\"some-spec.md %s\"." % (unanchored[0]["raw"], unanchored[0]["raw"]))

    resolved = []
    for ref in refs:
        path, ambiguous = resolve_doc(ref["doc"], root, head)
        if path is None:
            if ambiguous:
                raise CitationError(
                    "%r is ambiguous — it matches %s. Path-qualify it." % (
                        ref["doc"], ", ".join(sorted(ambiguous))))
            raise CitationError(
                "%r does not resolve to a file in this repo, so %s cannot be shown. A picker may "
                "not cite what it cannot show; fix the name, or pass the text as --quote if you are "
                "relaying someone else's words." % (ref["doc"], _describe(ref)))
        resolved.append((ref, path))

    # The same sum :func:`min_room` exports — a caller that reserved `min_room(...)` up front is
    # guaranteed `per >= MIN_EXCERPT_CHARS` here, and that guarantee holds only while the two are
    # one arithmetic.
    budget = room - HEADER_COST - sum(len(_describe(r)) + DESCRIBE_OVERHEAD for r, _ in resolved)
    per = budget // len(resolved) if resolved else 0
    if per < MIN_EXCERPT_CHARS:
        raise CitationError(
            "no room to describe %d reference(s): %d characters each, below the %d-character floor. "
            "An excerpt this short teases a section instead of describing it. Shorten the question, "
            "or cite fewer sections." % (len(resolved), max(per, 0), MIN_EXCERPT_CHARS))
    per = min(per, MAX_EXCERPT_CHARS)

    blocks = []
    for ref, path in resolved:
        got = section_excerpt(path, ref["section"], per, root, head)
        if got is None:
            # THREE different failures land here and they are not the same claim. `resolve_doc`
            # already proved `path` names a real place to look (the checkout or `head`'s allow-list)
            # — so if the CONTENT still can't be read, the document itself is the problem, and the
            # deleted-file case (checked first) is the one of those that is actually knowable: `gh`
            # itself confirmed a 404 at this PR's own head, meaning the checkout never had this path
            # AND the PR's own diff removes it — a file added and then deleted within the same still-
            # open PR, most often. That is worth saying plainly rather than folding into the generic
            # "could not be read" refusal below, which is reserved for a `head` fetch that failed for
            # an unrelated reason (`gh` missing, offline, rate-limited) — genuinely might be transient,
            # unlike a confirmed deletion. Saying "no heading numbered N" about a file nobody could
            # open would be a confident citation of nothing, which is exactly what this module exists
            # to refuse.
            if head is not None and head.deleted(path):
                raise CitationError(
                    "%r resolved to %s, but this PR deletes that file — it will not exist after "
                    "merge, so there is no content left to excerpt. A picker may not cite what it "
                    "cannot show; drop the citation, or pass the mention as --quote if you are "
                    "relaying someone else's words about the deletion."
                    % (ref["doc"], path))
            if _read_doc_lines(path, root or REPO_ROOT, head) is None:
                raise CitationError(
                    "%r resolved to %s but its content could not be read — not from the checkout, "
                    "and not from the PR's own head either. A picker may not cite what it cannot "
                    "open." % (ref["doc"], path))
            raise CitationError(
                "%s is not in %s — no heading numbered %s. Either the section was renumbered or the "
                "reference is wrong; a picker may not cite a section that is not there." % (
                    ref["raw"], path, ref["section"]))
        title, body = got
        blocks.append("%s — %s\n%s" % (_describe(ref), title, body or "(this section has no prose "
                                                                     "of its own)"))
    return "\n\n" + CITATION_HEADER + "\n\n" + "\n\n".join(blocks)


def annotate(body: str, quoted=None, root: str | None = None,
             cap: int = TELEGRAM_MESSAGE_CHARS, head: PRHead | None = None) -> str:
    """The one entry point. A rendered picker body -> the same body with its citations described.

    Returns `body` unchanged when nothing is cited, so a picker that names no document is untouched
    and every existing caller of that kind is unaffected.

    `head` rides straight through to :func:`citation_block` — see `PRHead`'s docstring. `None` (the
    default) means every citation resolves against the checkout only."""
    room = cap - CITATION_SAFETY_MARGIN - len(body or "")
    block = citation_block(body or "", quoted=quoted, room=room, root=root, head=head)
    return (body or "") + block
