#!/usr/bin/env python3
"""**A pull-request description, reduced to a sentence on each thing it did.** Standard library only,
pure text in and text out — no I/O, no network, no state.

## The complaint this exists to answer

An approval picker whose description is *"`ask-provenance-spec.md` phases 2 and 3"* is not missing a
description — `merge_guard.summarize_pr_body` relays the PR body's lead-in faithfully. The trouble is
that every sentence of that lead-in is a **pointer**:

    `ask-provenance-spec.md` phases 2 and 3. **Phase 4 is deliberately left unbuilt …**
    Status header moves `PARTIAL(phase 1)` → `PARTIAL(phases 1-3; phase 4 open …)`.

The lead-in is where a body says *what this is* only when the author wrote one; when the author wrote
a reference instead, the rule *"summarize the lead-in"* faithfully reproduces a reference. **The
description is not absent. It is a citation wearing a description's clothes** — the same defect
`ask_citations.py` refuses one level up, without the `§` syntax that makes it catchable there. The
owner's reasonable response is *"I don't know what those phases are; a sentence on each and I'd
remember."*

## Where the sentences actually are

In that same body, unread:

    ## Phase 2 — a landed picker enters the record of what the assistant has said
    ## Phase 3 — the no-access assertion, then the literal check
    ## Phase 4 — the question, and it is yours

**The headings ARE the sentence on each.** That is not luck about one PR: a heading exists to say
what the thing under it is, so a body that names its parts has already written the gloss its own
lead-in omits. So this module reads the whole body rather than its first section, and returns an
**outline** — each heading plus its first sentence.

## RESOLVE, do not merely flag — and the resolution is the PR's OWN body

`ask_citations.py`'s central move: a validator that merely flags is a heuristic about writing, it is
guessable, and a caller working around it produces a sentence that satisfies the checker and not the
reader. Same move here. A reference to *"phases 2 and 3"* is answered by **pulling the sections it
names into the message**, so the explanation sits under the reference and nobody has to remember to
write one.

**It resolves against the PR body and deliberately not against the repo.** Reading a spec off the
base branch to explain *"phase 2"* would describe the tree **before** the merge — and for a PR that
edits its own spec, which is most of them, that is a confident description of the wrong version. The
body is the one text guaranteed to be about this change.

## Polarity: SUBSTITUTE, then SAY SO. Never refuse.

`ask_citations` fails **closed**: an unresolvable `§8.2` exits 2 and the picker does not send. That is
right there and wrong here, and the asymmetry is not a matter of taste:

  * There, a refusal costs **one edit to a sentence the assistant wrote**, and the caller can make it.
  * Here, the reference lives in a **PR title and a PR body written by someone else**. A refusal
    would cost the merge, and the only repair is editing another author's description — an
    author-side action the sender cannot take. **A merge picker that fails to send is a green
    functionality PR that never gets approved**, which is the wall the guard exists to avoid being.
  * And an ordinal in prose has no reliable referent. `§8.2` names one thing; *"phase 2"* might be a
    phase of the spec, of the PR, or of CI. A literal matcher fails open on every spelling it did not
    predict, so a refusal resting on one would catch the easy half while licensing confidence in the
    rest.

So: **resolve what the body answers; name what it does not.** A reference the body never defines
produces one short guard-written line saying so (`merge_guard.UNEXPLAINED_HEADER`), which converts
*"I don't know what this means and I might be missing something"* into *"there is nothing to find"*.
Relayed prose is never edited or deleted to achieve this — the guard's whole trust model is that the
band below the header is someone else's words, fenced off and unaltered.

**The flag is raised for the WORDED family only** (`phase 4`, `step 3`, `commit 2`) and never for the
coded one (`R1`, `P1`, `T2`). Coded references still drive *selection* — a section titled `R1 — …`
is pulled in ahead of its neighbours — but `T2` in prose might be anything, and a note about it is
precisely the unexplained annotation the reader objects to. Selection failing quietly costs an
ordering; a flag failing loudly costs the reader's attention.
"""
from __future__ import annotations

import re

from ask_citations import _condense as condense

#: An ATX heading. Setext (`===` underlines) is deliberately not matched: it is vanishingly rare in
#: PR bodies and `_SECTION_BREAK_RE` in `merge_guard` already reads a `---` line as a rule rather than
#: as an underline, so honouring it here would make the two disagree about the same byte.
HEADING_RE = re.compile(r"^[ ]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")

#: The structural-plan vocabulary. Every word here names a *numbered part of a plan*, which is the
#: only thing an ordinal reference can usefully point at. Words like "version" or "release" are
#: deliberately absent — they number an artifact, not a part of this change.
_WORDS = ("phase", "step", "part", "stage", "commit", "fix", "round", "tier", "milestone",
          "wave", "arm", "rung")
#: `phase 2`, `phases 2 and 3`, `phases 1-3`, `steps 0, 1 and 2a`, `fixes 1 & 2`.
ORDINAL_RE = re.compile(
    r"\b(" + "|".join(_WORDS) + r")(?:e?s)?\b[ \t]*"
    r"(\d+[a-z]?(?:[ \t]*(?:[-–—,&]|and|to|through)[ \t]*\d+[a-z]?)*)", re.IGNORECASE)
#: `R1`, `P1`, `T2`, `Q3` — a single capital and a number, the shape `seneschal/docs/` uses for a plan
#: whose parts are not called phases. `check_doc_status.py`'s own vocabulary audit found several
#: different numbering schemes in one directory, which is why both families are here.
CODE_RE = re.compile(r"\b([A-Z])(\d+)\b")
#: `R1–R4`. Matched before :data:`CODE_RE`'s individual hits so the interior of a range is not lost.
CODE_RANGE_RE = re.compile(r"\b([A-Z])(\d+)[ \t]*[-–—][ \t]*\1?(\d+)\b")

#: A heading that opens with its own ordinal: `## Phase 2 — …`, `### R1 — …`, `## 1. The vocabulary`.
_HEAD_WORDED_RE = re.compile(r"^\s*(" + "|".join(_WORDS) + r")[ \t]*(\d+[a-z]?)\b", re.IGNORECASE)
_HEAD_CODE_RE = re.compile(r"^\s*([A-Z])(\d+)\b")
_HEAD_NUMBER_RE = re.compile(r"^\s*(\d+)[.)]?[ \t]")

#: Where a sentence ends. A closing quote or bracket may follow the stop before the space, because
#: prose here ends a sentence inside a quotation more often than not.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])[\"'”’)\]]?\s+")
#: Full stops that do not end a sentence. Short and literal on purpose — a cleverer rule here buys a
#: rare correct split and pays for it with a wrong one nobody can see in a Telegram message.
_ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "etc.", "cf.", "approx.", "Dr.", "Mr.", "Ms.", "No.",
                  "Fig.", "Ch.", "St.", "a.m.", "p.m.")
#: A markdown table row or its `|---|---|` rule. **Dropped whole, like `merge_guard`'s fenced
#: blocks.** A table is not a sentence: condensed onto one line it becomes `| Token | Means | |---|---|
#: | BUILT | everything it intends…`, which spends a whole outline entry on punctuation. A section
#: that is nothing but a table renders as its heading alone, and that is the honest answer.
_TABLE_LINE_RE = re.compile(r"^[ \t]*\|")
_TABLE_RULE_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$")

#: A first sentence shorter than this is a fragment — roughly four short words, which cannot describe
#: a section on its own — so the next one is taken too. `## First: does §6's reason to defer still
#: hold? **No.**` opens with exactly that shape. Deliberately low: extending a sentence that did not
#: need it costs characters, while a lone *"No."* costs the entry.
_MIN_SENTENCE = 25


# --------------------------------------------------------------------------- reading the body

def sections(text: str) -> list:
    """Every heading in `text`, in document order, as
    `{"level", "title", "body", "labels", "position"}`.

    `body` is the raw lines under the heading down to the **next heading of any level**, so a
    section's first sentence is always its own. `labels` is what an ordinal reference has to match to
    be answered by this section — see :func:`heading_labels`."""
    if not isinstance(text, str) or not text:
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, current = [], None
    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            title = condense([m.group(2)], 400)
            current = {"level": len(m.group(1)), "title": title, "body": [],
                       "labels": heading_labels(title), "position": len(out)}
            out.append(current)
        elif current is not None:
            current["body"].append(line)
    for sec in out:
        sec["body"] = "\n".join(sec["body"])
    return out


def heading_labels(title: str) -> set:
    """The ordinals a heading DECLARES, as normalised labels. Leading position only.

    `## Phase 2 — a landed picker …` -> `{"phase:2"}`; `### R1 — the phase-2 caveat …` ->
    `{"code:R1"}`; `## 1. The vocabulary …` -> `{"num:1"}`.

    **Leading only, deliberately.** `### R1 — the phase-2 caveat, written where phase 2 is DEFINED`
    is a section about R1 that merely mentions phase 2; harvesting every ordinal in a title would
    make that section the answer to *"what is phase 2?"*, and it is not."""
    title = (title or "").strip()
    m = _HEAD_WORDED_RE.match(title)
    if m:
        return {"%s:%s" % (_singular(m.group(1)), m.group(2).lower())}
    m = _HEAD_CODE_RE.match(title)
    if m:
        return {"code:%s%s" % (m.group(1), m.group(2))}
    m = _HEAD_NUMBER_RE.match(title)
    if m:
        # A bare number heading answers ANY worded ordinal with that number: a document that numbers
        # its sections `1.` `2.` `3.` and then calls them "steps" in prose is one document, and the
        # cost of matching is a section the reader did not need where the cost of missing is the gloss.
        return {"num:%s" % m.group(1)}
    return set()


def first_sentence(body: str, limit: int) -> str:
    """The opening sentence of a section, condensed for a phone and cut on a word boundary.

    Two sentences when the first is a fragment (:data:`_MIN_SENTENCE`), because a heading that ends
    in a question is routinely answered by a two-word sentence and the answer alone says nothing."""
    lines = [l for l in (body or "").split("\n")
             if not _TABLE_LINE_RE.match(l) and not _TABLE_RULE_RE.match(l)]
    text = condense(lines, 10_000)
    if not text:
        return ""
    taken, rest = "", text
    while rest and len(taken) < _MIN_SENTENCE:
        piece, rest = _split_one(rest)
        if not piece:
            break
        taken = (taken + " " + piece).strip()
    return condense([taken or text], limit)


def _split_one(text: str):
    """`(first sentence, remainder)`. The whole string and `""` when it has no interior stop."""
    for m in _SENTENCE_END_RE.finditer(text):
        head = text[:m.start()]
        if any(head.endswith(a) for a in _ABBREVIATIONS):
            continue
        return head.strip(), text[m.end():].strip()
    return text.strip(), ""


# --------------------------------------------------------------------------- the references

def _singular(word: str) -> str:
    word = (word or "").lower()
    for suffix in ("es", "s"):
        if word.endswith(suffix) and word[:-len(suffix)] in _WORDS:
            return word[:-len(suffix)]
    return word


def _expand(numbers: str, word: str) -> list:
    """`"2 and 3"` -> `["2", "3"]`; `"1-3"` -> `["1", "2", "3"]`; `"0, 1 and 2a"` unchanged.

    A range expands only between two plain integers. `2a-3b` is left as its two endpoints — nobody
    can say what lies between them, and inventing `2b` would put a section number in front of the
    reader that the author never wrote."""
    parts = re.split(r"[ \t]*(?:[-–—,&]|and|to|through)[ \t]*", numbers, flags=re.I)
    parts = [p.strip().lower() for p in parts if p.strip()]
    dashed = bool(re.search(r"[-–—]|\bto\b|\bthrough\b", numbers, re.I))
    out = list(parts)
    if dashed and len(parts) == 2 and all(p.isdigit() for p in parts):
        lo, hi = int(parts[0]), int(parts[1])
        if 0 <= hi - lo <= 20:
            out = [str(n) for n in range(lo, hi + 1)]
    return ["%s:%s" % (word, n) for n in out]


def ordinal_references(text: str) -> list:
    """Every ordinal reference in `text`, as `{"raw", "label", "worded"}`, in order and deduplicated.

    `worded` marks the family the unexplained note is allowed to name. See this module's docstring
    for why the coded family drives selection but never a flag."""
    if not isinstance(text, str) or not text:
        return []
    out, seen = [], set()

    def add(raw, label, worded):
        if label in seen:
            return
        seen.add(label)
        out.append({"raw": raw.strip(), "label": label, "worded": worded})

    for m in ORDINAL_RE.finditer(text):
        word = _singular(m.group(1))
        for label in _expand(m.group(2), word):
            add(m.group(0), label, True)
    for m in CODE_RANGE_RE.finditer(text):
        letter, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        if 0 <= hi - lo <= 20:
            for n in range(lo, hi + 1):
                add(m.group(0), "code:%s%d" % (letter, n), False)
    for m in CODE_RE.finditer(text):
        add(m.group(0), "code:%s%s" % (m.group(1), m.group(2)), False)
    return out


def answers(section: dict, label: str) -> bool:
    """Does this section answer that reference? Its own labels, plus the bare-number fallback."""
    if label in section.get("labels", ()):
        return True
    if label.startswith("code:"):
        return False
    _kind, _sep, number = label.partition(":")
    return ("num:%s" % number) in section.get("labels", ())


# --------------------------------------------------------------------------- the outline

def outline(text: str, room: int, entry_chars: int, max_entries: int, wanted=None,
            avoid: str = "") -> tuple:
    """`(lines, dropped)` — the section-by-section band, and how many sections did not fit.

    **Selection and rendering are two different orders, on purpose.** Sections that answer a
    reference in the title or the lead-in are selected first, whatever their depth, because those are
    the ones the picker's own words have already promised to explain. Everything else fills by
    (shallowest heading, earliest). Then the survivors are rendered in **document order**, because
    that is the order the author reasoned in and a reordered outline reads as a different argument.

    `room` is the whole band's character budget; `entry_chars` caps any single line so one section
    with a long first paragraph cannot eat the others.

    `avoid` is text already shown above — in practice the lead-in. A body opening `## Summary\\n…`
    has no lead-in of its own, so `merge_guard._lead_section` steps over the heading and shows that
    section's prose; without this the very next line would repeat it back as `• Summary: …`. The
    section is dropped rather than deduplicated in place, so its budget goes to a section the reader
    has not seen yet."""
    secs = sections(text)
    if not secs:
        return [], 0
    wanted = set(wanted or ())
    # Condensed on both sides of the comparison: the lead-in is relayed verbatim, markup and all,
    # while an outline sentence has been through `condense`. Comparing the two raw would miss every
    # section whose opening happens to contain a `**` or a backtick, which is most of them.
    avoid = condense((avoid or "").split("\n"), 100_000)

    def rank(sec):
        answering = any(answers(sec, label) for label in wanted)
        return (0 if answering else 1, sec["level"], sec["position"])

    eligible = [s for s in secs if not _already_shown(s, avoid)]
    chosen, used = [], 0
    for sec in sorted(eligible, key=rank)[:max_entries]:
        line = _entry(sec, entry_chars)
        cost = len(line) + 1
        if used + cost > room:
            continue
        chosen.append((sec["position"], line))
        used += cost
    chosen.sort()
    return [line for _pos, line in chosen], len(eligible) - len(chosen)


def _already_shown(sec: dict, avoid: str) -> bool:
    """Is this section's opening already above, in the lead-in? Compared on the condensed sentence
    rather than the raw block, because the lead-in is relayed verbatim and the outline is not."""
    if not avoid:
        return False
    # A floor, so a section opening `Yes.` is not dropped because the lead-in happens to contain
    # that word. Low, because the cost of a false match is one outline entry and the cost of a miss
    # is the same paragraph printed twice in a message that is already fighting for room.
    opening = first_sentence(sec["body"], 200)
    return bool(opening) and len(opening) > 12 and opening in avoid


def _entry(sec: dict, limit: int) -> str:
    """`• Heading: first sentence`. The bullet is `•` and not `-` because `merge_guard` rewrites a
    body's own list markers and this line is the guard's, not the body's.

    A heading that already ends in a stop is joined with a space instead — `### R2 — … Deadline
    set.:` is a colon the author did not write, and punctuation the guard invents is exactly the kind
    of small wrongness that makes a relayed line read as paraphrase."""
    title = sec["title"]
    room = limit - len(title) - 4
    sentence = first_sentence(sec["body"], room) if room >= 30 else ""
    if not sentence:
        return "• " + title
    joiner = " " if title.endswith((".", "!", "?", ":", "—", "-")) else ": "
    return "• " + title + joiner + sentence


def explained(lines) -> set:
    """Every label the outline itself accounts for — the headings' own, plus any reference appearing
    in the rendered text.

    **Generous on purpose.** This set decides whether the unexplained note fires, and the note is the
    only part of this module that can be wrong out loud. A section whose first sentence happens to
    say *"phases 1-3 are mechanisms"* has told the reader what phases 1-3 are well enough; flagging it
    would be the annotation noise the whole module is against."""
    got = set()
    for line in lines or ():
        body = line[2:] if line.startswith("• ") else line
        title = body.split(":", 1)[0]
        got |= heading_labels(title)
        got |= {ref["label"] for ref in ordinal_references(body)}
    return got


def unexplained(text: str, lines) -> list:
    """The worded ordinals in `text` that the outline does not account for, in order, deduplicated on
    the words the reader would see. Coded references are never returned — see the module docstring."""
    got = explained(lines)
    out, seen = [], set()
    for ref in ordinal_references(text or ""):
        if not ref["worded"] or ref["label"] in got:
            continue
        raw = " ".join(ref["raw"].split())
        if raw.lower() in seen:
            continue
        seen.add(raw.lower())
        out.append(raw)
    return out
