---
name: persona-wizard
description: >-
  The persona wizard. Interviews the owner to build their assistant's persona — name, role
  framing, demeanor, channel identities, boundaries — and generates persona/persona.md +
  persona/identity.json from the tracked template. Use for "/setup-persona", "set up the
  persona", "rename the assistant", "change the assistant's voice/personality", or as the
  persona chapter of the unified /setup flow. Re-runnable: detects existing config and asks
  only for what changes.
compatibility: >-
  Writes persona/persona.md and persona/identity.json (both gitignored). No MCP required. The
  phone Worker cannot read local files, so phone values are printed for the owner to apply via
  wrangler.
---

# Persona wizard (`/setup-persona`)

Build the assistant the owner actually wants. The framework ships with a default persona
(`persona/persona.default.md` — helpful, warm, direct, nameless); this wizard replaces it with a
custom character by interviewing the owner and generating two artifacts from one set of answers:

- `persona/persona.md` — the character the *model* reads (generated from
  `persona/persona.template.md`)
- `persona/identity.json` — the structured identity *code* reads (schema:
  `persona/identity.example.json`)

**Ownership contract:** this wizard owns `assistant.*` in identity.json and persona.md. The
store-onboarding flow (`/setup-store`) owns `owner.*` and `owner-profile.md`. Whichever runs
second **confirms** shared fields rather than re-asking. Never overwrite the other flow's
answers without showing the owner what would change.

## Ground rules

- **Every question is skippable.** Skipping everything yields the default-Claude persona
  (persona.md becomes a copy of the default with any known identity values filled). Say so up
  front: "skip anything — the default is good."
- **One question at a time**, with a concrete default shown. This is a conversation, not a form.
- **Show before writing.** Assemble the full persona.md draft, show it, get one confirmation,
  then write both files together.
- **Re-runs are edits.** If `persona/identity.json` or `persona/persona.md` exists, read them
  first, summarize the current persona in two lines, and ask what to change — only walk the full
  interview on request or first run.

## The interview

**0 — Detect.** Read `persona/identity.json` (if present), `persona/persona.md` (if present),
and `persona/owner-profile.md` (if present, for owner names the store flow already captured).
Existing config → the re-run edit path above.

**1 — Name.** "What should your assistant be called?" Offer: a name of their choosing / stay
nameless (works fine on every channel — 'this is {owner}'s assistant'). If named: ask whether
it's pronounced the way it's spelled; capture a phonetic spelling only if not
(`assistant.nameSpoken`).

**2 — Role framing.** "How should it describe its job?" Default **assistant / chief of staff**;
alternatives: executive assistant, aide, secretary, or their own phrase. This is the label used
in greetings and sign-offs.

**3 — Demeanor.** The big one. Offer four archetypes, each with a two-line sample of the same
message rendered in that voice (draft the samples live, using the reminder "the quarterly report
is due tomorrow" as the test line):

| Archetype | Register |
|---|---|
| **Default Claude** *(default)* | helpful, warm, direct — no invented character |
| **Crisp chief-of-staff** | dry, economical, understated; leads with the decision; a touch of wit, never cute |
| **Warm concierge** | friendly, encouraging, upbeat without being saccharine |
| **Freeform** | the owner describes the character; draft the Demeanor section from their description and iterate until they approve it |

Whatever the pick, write BOTH registers into the template (to-outsiders is always professional
and unflappable; the archetype mainly shapes the to-owner register and the nudge tone).

**4 — Pronouns.** The assistant's (default they/them). Owner pronouns: only if identity.json
lacks them (otherwise confirm silently by using them correctly).

**5 — Channel identities** (each skippable; skip the whole step if they use neither channel):
- **Email:** the assistant's send-from address (`assistant.email`) and sign-off (default
  "— {name}, {owner}'s assistant").
- **Phone:** greeting line (draft one from name+role, show it), TTS provider + voice id if they
  have one (`assistant.ttsProvider` / `assistant.ttsVoiceId`; skipping keeps the platform
  default voice).

**6 — Boundaries (one confirmation, not a quiz).** State the two standing rules and confirm:
the assistant never impersonates the owner (always signs/identifies as itself), and anything
outbound or destructive is draft-and-hold (`seneschal/references/autonomy-policy.md`). These are
framework policy; the wizard records assent, it doesn't renegotiate them.

**7 — Owner basics** (only for fields absent from identity.json): name (+ phonetic if needed),
pronouns, IANA timezone (offer the machine's current zone as the default). These seed `owner.*`
minimally; the store onboarding enriches them later.

## Generate

1. Fill `persona/persona.template.md` → assemble `persona/persona.md`. Token map: names,
   pronouns, role label, the demeanor sections (from the archetype or the approved freeform
   draft), greeting, sign-off, TTS line ("none — platform default" when unset), timezone label
   ("the owner's configured timezone (identity.json)" when null), `{generated_date}` = today.
   Empty optional lines (nameSpoken) collapse cleanly — no dangling clauses.
2. Show the assembled persona.md. One approval → write **both** files (persona.md +
   identity.json). Never write on a skipped confirmation.
3. **Phone block (only if they use the phone stack):** print, ready to paste —
   the `[vars]` lines (`OWNER_NAME`, `OWNER_NAME_SPOKEN`, `ASSISTANT_NAME`,
   `ASSISTANT_VOICE_ID`, `ASSISTANT_TTS_PROVIDER`) for `phone/wrangler.toml`, plus
   `wrangler secret put OWNER_PROFILE` guidance. Explain why: Workers read env, not local files,
   so persona changes mean re-running this wizard and re-applying the block.
4. **Named alias command (offer, optional):** if the assistant got a name, offer to generate
   `.claude/commands/<name>.md` — a two-line command that invokes the same Chat mode as
   `/assistant` (so the owner can type their assistant's actual name as a command). Keep
   `/assistant` as the canonical tracked command.
5. Close with what was written, and that re-running the wizard is the way to change any of it.

## Guardrails

- Act-low throughout (local gitignored files + printed instructions). Nothing outbound.
- Never invent personal facts about the owner — every owner field is asked or confirmed.
- Don't edit `persona.default.md` or the template — they're the tracked framework defaults.
- If the daemon is running, note that the warm session picks the new persona up on its next
  re-ground (or `reseneschald` to restart it now).
