#!/usr/bin/env python3
"""Markdown → **Telegram HTML**, at the send boundary. Standard library only.

**Why this exists.** With `TELEGRAM_PARSE_MODE` empty every outbound Telegram message goes out as
plain text — deliberately, because *"plain avoids escaping traps"* — but the assistant writes
Markdown, so the owner reads literal `**` around every emphasised phrase. The fix: **convert at the
send boundary, and fall back to plain text if the API rejects it**, so a bad conversion degrades to
plain-text behaviour instead of dropping the message. "Just set MarkdownV2" is rejected on purpose:
an unescaped character there is a 400 and the message is **gone**, which is unacceptable on the
channel that carries the owner's critical reminders.

**THE INVARIANT THAT OUTRANKS EVERYTHING ELSE HERE: a message is never lost to a formatting
failure.** Nothing in this module raises into a send path — :func:`plan_send` wraps the whole
conversion in a try and degrades to the plain original — and the sender retries a rejected chunk
once, as plain text, with the ORIGINAL unconverted string. Prettier output is worth nothing next to
a delivered one.

**WHAT TELEGRAM ACTUALLY SUPPORTS** is a small closed tag set — `b i u s code pre a blockquote
tg-spoiler` — and **nothing else**. An unknown tag is a 400, not a shrug. So everything outside that
set has to degrade to readable plain text rather than to markup:

  ============================ =========================================================
  Markdown                     Telegram
  ============================ =========================================================
  ``**bold**``                 ``<b>``
  ``*italic*`` ``_italic_``    ``<i>``  (see the `_` rule below)
  ``~~strike~~``               ``<s>``
  ```` `code` ````             ``<code>``
  ```` ```lang\\n…\\n``` ````   ``<pre><code class="language-lang">``
  ``[text](url)``              ``<a href="url">`` — http/https/tg/mailto only
  ``> quote``                  ``<blockquote>`` (a run of quoted lines becomes one)
  ``# heading``                a **bold line** — nothing renders a heading
  a table                      ``<pre>`` with the columns padded so they line up
  ``---``                      an em-dash rule; there is no ``<hr>``
  ``- item`` / ``1. item``     left exactly as typed — lists are not marked up
  ============================ =========================================================

**ESCAPE FIRST, THEN EMIT TAGS.** `&` `<` `>` in *text* are escaped as the scanner consumes them, so
the tags this module emits are the only tags in the output. Getting that order wrong is the
classic injection-shaped bug, and it has a concrete cost here: a message containing a bare `<`
sends fine today and must keep doing so.

**THE `_` RULE IS THE ONE THAT EARNS ITS KEEP.** `telegram_send.py`, `state_dir`, `__init__` and
`snake_case` turn up in the assistant's messages constantly. An underscore only opens emphasis when the
character before it is **not** a word character, and only closes when the character after it is
not one either — so every identifier above round-trips literally. `__bold__` is deliberately NOT
supported: it would turn `__init__.py` into a bold *init*, and identifiers like that appear far more
often than double-underscore emphasis does.

**Unmatched markers never break a message.** A lone `*`, a stray `_`, an unclosed backtick, an
unclosed fence — each degrades to a literal character or a `<pre>` that runs to the end of the
message. There is no parse error to report because there is no parse to fail.

**CHUNKING CUTS THE SOURCE, NOT THE HTML** (:func:`split_source`). Telegram's limit is 4096
characters, and a chunk boundary landing inside a tag is invalid HTML → 400 → fallback, which would
make long messages silently plainer than short ones. Cutting the Markdown *before* converting means
each chunk is converted independently and **no tag can straddle a boundary by construction**. A
fenced code block is an atomic segment (and is closed + re-opened if it has to be split anyway); a
single line longer than the budget is split on word boundaries with :func:`_balance` closing and
re-opening any emphasis marker left open at the seam.

**THE KNOB.** `TELEGRAM_PARSE_MODE` keeps meaning exactly what it always meant — the literal
`parse_mode` the Bot API is handed. The new behaviour is a **companion** setting,
`TELEGRAM_FORMAT`, defaulting to `plain`, so anything that does not opt in behaves byte-identically
to before. `TELEGRAM_FORMAT=markdown` converts and sends `parse_mode=HTML`. An unrecognised value
reads as `plain`: a typo in an env file must not be the reason a reminder stops arriving.
"""
from __future__ import annotations

import re

#: The Bot API's own per-message cap. Not configurable — it is Telegram's number, not ours.
TELEGRAM_MESSAGE_LIMIT = 4096

FORMAT_PLAIN = "plain"
FORMAT_MARKDOWN = "markdown"

#: Everything that reads as "leave it alone" — including the empty string, which is what an
#: unset `TELEGRAM_FORMAT` looks like and therefore what today's behaviour is spelled as.
_PLAIN_WORDS = frozenset({"", "plain", "none", "off", "no", "text", "raw"})
_MARKDOWN_WORDS = frozenset({"markdown", "md", "markdown-html", "markdown_html", "html-from-markdown"})

#: `<a href>` is only emitted for schemes Telegram will actually accept. Anything else keeps its
#: literal `[text](url)` form: a link we cannot vouch for is worth less than a message that lands.
_ALLOWED_SCHEMES = ("http://", "https://", "tg://", "mailto:")

_LINK_RE = re.compile(r"\[([^\[\]]*)\]\(([^()\s]+)\)")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^\s`~]*)\s*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$")
_QUOTE_RE = re.compile(r"^ {0,3}>[ \t]?(.*)$")
#: A table's separator row: `|---|:--:|`. At least one pipe, so a bare `---` rule can't match it.
_TABLE_SEP_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$")
_TAG_RE = re.compile(r"</?(?:b|i|s|u|code|pre|a|blockquote|tg-spoiler)(?:\s[^>]*)?>")

#: Head-room :func:`_split_long_line` leaves for the closing markers :func:`_balance` may append and
#: for the tags they render into. Six characters of marker expand to at most ~27 of HTML.
_BALANCE_RESERVE = 64


# --------------------------------------------------------------------------- the knob

def normalize_format(raw) -> str:
    """`"markdown"` or `"plain"`. **An unrecognised value reads as plain** — a typo in an untracked
    `telegram.env` must degrade to today's behaviour, never to a crash or to a silent HTML attempt
    nobody asked for."""
    value = (raw or "").strip().lower()
    if value in _MARKDOWN_WORDS:
        return FORMAT_MARKDOWN
    if value in _PLAIN_WORDS:
        return FORMAT_PLAIN
    return FORMAT_PLAIN


# --------------------------------------------------------------------------- escaping

def escape(text: str) -> str:
    """Escape text content for Telegram HTML. **`&` first** — reversing this order double-escapes
    every entity the later replacements introduce."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(text: str) -> str:
    """Escape a value going into a quoted attribute (`href`, `class`)."""
    return escape(text).replace('"', "&quot;")


def _unescape(text: str) -> str:
    """Inverse of :func:`escape`, for the table renderer, which strips tags back to plain text.
    **`&amp;` last**, mirroring the reasoning above."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch == "_")


def _link_ok(url: str) -> bool:
    return (url or "").lower().startswith(_ALLOWED_SCHEMES)


# --------------------------------------------------------------------------- inline

def _run_length(src: str, start: int, ch: str) -> int:
    end = start
    while end < len(src) and src[end] == ch:
        end += 1
    return end - start


def _find_backtick_close(src: str, start: int, run: int) -> int:
    """Index of the next run of **exactly** `run` backticks at or after `start`; -1 if there is
    none, which is the unclosed-backtick case and renders as a literal."""
    i = start
    while i < len(src):
        if src[i] == "`":
            here = _run_length(src, i, "`")
            if here == run:
                return i
            i += here
            continue
        i += 1
    return -1


def _accept_plain(src: str, start: int, j: int) -> bool:
    """CommonMark's rule for `**` / `~~` / `*`: the content may not be empty and may not begin or
    end with whitespace. It is what keeps `a ** b ** c` and `2 * 3 * 4` literal."""
    content = src[start:j]
    return bool(content) and not content[0].isspace() and not content[-1].isspace()


def _accept_underscore(src: str, start: int, j: int) -> bool:
    """The `_` closer rule the module docstring explains: the character before it is neither
    whitespace nor another `_`, and the character after it is not a word character. `telegram_send.py`
    and `__init__` have no valid closer under this and stay literal."""
    content = src[start:j]
    nxt = src[j + 1] if j + 1 < len(src) else ""
    return (bool(content) and not content[0].isspace()
            and not content[-1].isspace() and content[-1] != "_" and not _is_word_char(nxt))


def _scan_close(src: str, start: int, marker: str, accept=_accept_plain) -> int:
    """Index of `marker`'s closer, or -1. **A scan rather than a `find`**, because two things have
    to be stepped over rather than matched inside:

    * a **code span** is opaque — `` *a `b*c` d* `` must close on the last `*`, not the one in the
      backticks, for the same reason `` `**kwargs` `` is not bold;
    * for a single `*`, a complete ``**…**`` span — otherwise `*a **b** c*` closes the italic on the
      bold's own marker and tears both. That case is why this is not a one-line `str.find` loop."""
    i = start
    while i < len(src):
        if src[i] == "`":
            run = _run_length(src, i, "`")
            close = _find_backtick_close(src, i + run, run)
            i = close + run if close != -1 else i + run
            continue
        if marker == "*" and src.startswith("**", i):
            close = _scan_close(src, i + 2, "**")
            i = close + 2 if close != -1 else i + 2
            continue
        if src.startswith(marker, i):
            if accept(src, start, i):
                return i
            i += len(marker)
            continue
        i += 1
    return -1


def inline(src: str) -> str:
    """Convert one line's worth of inline Markdown. Escapes as it consumes, so every `<` `>` `&`
    in the *text* is neutralised before any tag is emitted.

    Emphasis bodies are re-parsed (so `**bold with _italic_**` nests); a code span's body is not —
    it is escaped verbatim, which is what makes `` `a < b` `` safe and what stops a filename inside
    backticks from picking up emphasis."""
    out: list[str] = []
    i, n = 0, len(src or "")
    src = src or ""
    while i < n:
        ch = src[i]

        if ch == "`":
            run = _run_length(src, i, "`")
            close = _find_backtick_close(src, i + run, run)
            if close != -1:
                body = src[i + run:close]
                if body.strip():
                    out.append("<code>%s</code>" % escape(body.strip("\n")))
                    i = close + run
                    continue
            out.append("`" * run)
            i += run
            continue

        if ch == "[":
            m = _LINK_RE.match(src, i)
            if m and _link_ok(m.group(2)):
                label = inline(m.group(1)) or escape(m.group(2))
                out.append('<a href="%s">%s</a>' % (_attr(m.group(2)), label))
                i = m.end()
                continue
            out.append("[")
            i += 1
            continue

        for marker, tag in (("**", "b"), ("~~", "s")):
            if src.startswith(marker, i):
                close = _scan_close(src, i + len(marker), marker)
                if close != -1:
                    out.append("<%s>%s</%s>" % (tag, inline(src[i + len(marker):close]), tag))
                    i = close + len(marker)
                else:
                    out.append(marker)
                    i += len(marker)
                break
        else:
            if ch == "*":
                nxt = src[i + 1] if i + 1 < n else ""
                close = _scan_close(src, i + 1, "*") if nxt and not nxt.isspace() else -1
                if close != -1:
                    out.append("<i>%s</i>" % inline(src[i + 1:close]))
                    i = close + 1
                else:
                    out.append("*")
                    i += 1
                continue

            if ch == "_":
                prev = src[i - 1] if i else ""
                nxt = src[i + 1] if i + 1 < n else ""
                opens = not _is_word_char(prev) and nxt and not nxt.isspace() and nxt != "_"
                close = _scan_close(src, i + 1, "_", _accept_underscore) if opens else -1
                if close != -1:
                    out.append("<i>%s</i>" % inline(src[i + 1:close]))
                    i = close + 1
                else:
                    out.append("_")
                    i += 1
                continue

            out.append(escape(ch))
            i += 1
    return "".join(out)


def _plain_inline(src: str) -> str:
    """The same marker rules, rendered back to **plain text** — for table cells, which live inside a
    `<pre>` and so cannot carry a single tag. Reusing :func:`inline` rather than a second set of
    regexes is the point: there is one definition of what a marker is.

    A link keeps its URL in parentheses. Losing the href is unavoidable inside a `<pre>`; losing the
    URL entirely would not be."""
    src = _LINK_RE.sub(
        lambda m: "%s (%s)" % (m.group(1), m.group(2)) if _link_ok(m.group(2)) else m.group(0),
        src or "")
    return _unescape(_TAG_RE.sub("", inline(src)))


# --------------------------------------------------------------------------- tables

def _split_row(line: str) -> list[str]:
    cells = line.strip()
    if cells.startswith("|"):
        cells = cells[1:]
    if cells.endswith("|"):
        cells = cells[:-1]
    return [c.strip() for c in cells.split("|")]


def _render_table(block: list[str]) -> str:
    """A Markdown table as a padded `<pre>` block.

    **Why `<pre>` and not flattened labelled lines:** the columns lining up is the entire reason
    anyone writes a table, Telegram renders `<pre>` in a monospace font with horizontal scrolling on
    mobile, and flattening a 5×4 table into 20 labelled lines is longer and harder to scan. The
    cost, named because it is real: a `<pre>` may not contain other tags, so emphasis inside a cell
    is flattened to plain text and a link inside a cell keeps its URL but stops being tappable."""
    rows = [[_plain_inline(c) for c in _split_row(line)]
            for line in block if not _TABLE_SEP_RE.match(line)]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    cols = [max(len(r[k]) for r in rows) for k in range(width)]
    lines = []
    for idx, row in enumerate(rows):
        lines.append(" | ".join(cell.ljust(cols[k]) for k, cell in enumerate(row)).rstrip())
        if idx == 0 and len(rows) > 1:
            lines.append("-+-".join("-" * w for w in cols))
    return "<pre>%s</pre>" % escape("\n".join(lines))


def _is_table_start(lines: list[str], i: int) -> bool:
    return (i + 1 < len(lines) and "|" in lines[i]
            and not _TABLE_SEP_RE.match(lines[i]) and bool(_TABLE_SEP_RE.match(lines[i + 1])))


# --------------------------------------------------------------------------- blocks

def _fence_char(line: str) -> str:
    m = _FENCE_RE.match(line)
    return m.group(1)[0] if m else ""


def to_html(text: str) -> str:
    """One Markdown message → Telegram HTML. Never raises on malformed input: an unclosed fence
    runs to the end of the message, an unmatched marker is a literal character, an unparseable
    table row is just a line of text."""
    lines = (text or "").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        fence = _FENCE_RE.match(line)
        if fence:
            char, lang = fence.group(1)[0], fence.group(2)
            body, i = [], i + 1
            while i < len(lines) and _fence_char(lines[i]) != char:
                body.append(lines[i])
                i += 1
            i += 1  # step over the closer; running off the end is the unclosed-fence case
            code = escape("\n".join(body))
            out.append('<pre><code class="language-%s">%s</code></pre>' % (_attr(lang), code)
                       if lang else "<pre>%s</pre>" % code)
            continue

        if _is_table_start(lines, i):
            block = []
            while i < len(lines) and "|" in lines[i]:
                block.append(lines[i])
                i += 1
            rendered = _render_table(block)
            if rendered:
                out.append(rendered)
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            body = []
            while i < len(lines):
                m = _QUOTE_RE.match(lines[i])
                if not m:
                    break
                body.append(inline(m.group(1)))
                i += 1
            joined = "\n".join(body)
            out.append("<blockquote>%s</blockquote>" % joined if joined.strip() else "")
            continue

        if _HR_RE.match(line):
            out.append("——————")
            i += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            body = inline(heading.group(2))
            # An empty `<b></b>` is an entity with no content; Telegram rejects it. A bare `##`
            # carries nothing anyway, so it becomes the blank line it looks like.
            out.append("<b>%s</b>" % body if body.strip() else "")
            i += 1
            continue

        out.append(inline(line))
        i += 1
    return "\n".join(out)


# --------------------------------------------------------------------------- chunking

def _segment(lines: list[str]) -> list[tuple[str, list[str]]]:
    """`[(kind, lines)]`, where a fenced code block is ONE atomic segment and everything else is one
    line per segment. Making the fence atomic is what keeps the packer below from cutting a code
    block in half without noticing."""
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(lines):
        char = _fence_char(lines[i])
        if char:
            j = i + 1
            while j < len(lines) and _fence_char(lines[j]) != char:
                j += 1
            end = min(j + 1, len(lines))
            out.append(("fence", lines[i:end]))
            i = end
            continue
        out.append(("line", [lines[i]]))
        i += 1
    return out


def _balance(piece: str) -> tuple[str, str]:
    """`(closed_piece, reopen_prefix)` for a cut made **inside a line**. A hard split can leave an
    emphasis marker open; closing it here and re-opening it on the next piece is what stops a long
    message going plainer at the seam. Heuristic by construction — and safe to be, because an
    unmatched marker renders as a literal character rather than as a broken message.

    **The `rstrip` is load-bearing, not tidiness.** A cut usually lands after a space, and emphasis
    whose content ends in whitespace is not emphasis (:func:`_accept_plain`) — so appending the
    closer without trimming produces `**…word ** `, which renders as two literal asterisks and
    silently un-bolds the whole chunk. That is the bug this function exists to prevent, one step
    further along."""
    reopen = ""
    for marker in ("**", "~~", "`"):
        if piece.count(marker) % 2:
            piece = piece.rstrip() + marker
            reopen = marker + reopen
    return piece, reopen


def _fits(text: str, limit: int, render) -> bool:
    return len(text) <= limit and len(render(text)) <= limit


def _fit_prefix(prefix: str, text: str, limit: int, render) -> int:
    """The longest prefix of `text` that still fits after `prefix`, never less than one character —
    a cut that makes no progress is an infinite loop, which on this path would mean the message
    never sends at all."""
    lo, hi, best = 1, len(text), 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _fits(prefix + text[:mid], limit, render):
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def _split_long_line(line: str, limit: int, render) -> list[str]:
    """One over-long line → pieces, cut on word boundaries where possible and mid-word only when a
    single token is itself over budget.

    `fresh` means *`cur` holds nothing but a re-opened marker*. Two things hang off it: the
    whitespace that straddled the seam is dropped rather than left leading the next piece (a `**`
    followed by a space opens nothing), and the flush branch is skipped, because flushing a piece
    that is only a re-opened marker would append it forever."""
    budget = max(1, limit - _BALANCE_RESERVE)
    pieces: list[str] = []
    cur, fresh = "", False
    for token in re.split(r"(\s+)", line):
        while token:
            if fresh and not token.strip():
                break  # the whitespace that overflowed dies with the seam
            if _fits(cur + token, budget, render):
                cur, fresh = cur + token, False
                break
            if cur.strip() and not fresh:
                closed, reopen = _balance(cur)
                pieces.append(closed)
                cur, fresh = reopen, True
                continue
            cut = _fit_prefix(cur, token, budget, render)
            closed, reopen = _balance(cur + token[:cut])
            pieces.append(closed)
            cur, fresh, token = reopen, True, token[cut:]
    if cur.strip():
        pieces.append(cur)
    return pieces or [line[:limit]]


def split_source(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT, render=None) -> list[str]:
    """Cut the **source** Markdown into pieces that each render within `limit`.

    Cutting the source rather than the HTML is the whole trick: every piece is converted
    independently afterwards, so a tag cannot straddle a boundary — there is no tag yet when the
    cut is made. `render` is the conversion the caller will apply (``to_html`` in Markdown mode,
    identity in plain mode); it is measured, not just estimated, because escaping expands `&` to
    five characters and a 4096-character source can render longer than 4096."""
    text = text or ""
    render = render or (lambda s: s)
    if _fits(text, limit, render):
        return [text]

    pieces: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            pieces.append("\n".join(buf))
            buf = []

    for kind, block in _segment(text.split("\n")):
        if buf and _fits("\n".join(buf + block), limit, render):
            buf += block
            continue
        if not buf and _fits("\n".join(block), limit, render):
            buf = list(block)
            continue
        flush()
        if _fits("\n".join(block), limit, render):
            buf = list(block)
            continue
        # The segment alone is over budget. A fence is re-opened around each part so the code stays
        # monospaced across the seam; an ordinary line is split on words.
        if kind == "fence" and len(block) > 2:
            opener, closer = block[0], block[-1]
            subs = []
            run: list[str] = []
            for body_line in block[1:-1]:
                if run and not _fits("\n".join([opener] + run + [body_line, closer]), limit, render):
                    subs.append([opener] + run + [closer])
                    run = []
                run.append(body_line)
            subs.append([opener] + run + [closer])
        else:
            subs = [[p] for p in _split_long_line("\n".join(block), limit, render)]
        for sub in subs[:-1]:
            pieces.append("\n".join(sub))
        buf = list(subs[-1])
    flush()
    return [p for p in pieces if p.strip()] or [text[:limit]]


# --------------------------------------------------------------------------- the send plan

def render_for_api(text: str, fmt) -> str | None:
    """The HTML for `text`, or `None` meaning *send the original as plain text*.

    `None` on: the plain format (the default), a conversion that raised, a conversion that came out
    empty when the source was not, and a conversion that came out over Telegram's cap. Every one of
    those is a case where sending the source unconverted is strictly better than sending anything
    else, so none of them is an error."""
    if normalize_format(fmt) != FORMAT_MARKDOWN:
        return None
    try:
        html = to_html(text)
    except Exception:  # noqa: BLE001 — a converter bug may never cost a message
        return None
    if not html.strip() and (text or "").strip():
        return None
    if len(html) > TELEGRAM_MESSAGE_LIMIT:
        return None
    return html


def plan_send(text: str, fmt=FORMAT_PLAIN, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[dict]:
    """`[{"source": <markdown>, "html": <html or None>}]` — one entry per message to send.

    `source` is always the exact, unconverted text of that chunk, which is what the sender falls
    back to when Telegram rejects the HTML. The whole conversion sits inside one try: if anything
    at all goes wrong the plan degrades to plain chunks of the original, because a converter that
    can stop a send is worse than no converter."""
    text = text or ""
    if normalize_format(fmt) == FORMAT_MARKDOWN:
        try:
            plan = []
            for source in split_source(text, limit, to_html):
                plan.append({"source": source, "html": render_for_api(source, FORMAT_MARKDOWN)})
            if plan:
                return plan
        except Exception:  # noqa: BLE001 — see the docstring; degrade, never raise
            pass
    return [{"source": source, "html": None} for source in split_source(text, limit)]
