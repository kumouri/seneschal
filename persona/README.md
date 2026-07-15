# persona/ — who the assistant is, and who it works for

The assistant's character and the owner's context live here. **Resolution order, used by every
skill and script:**

1. `persona.md` — the personalized persona the `/setup-persona` wizard generates. **Gitignored**
   (it names you and your preferences; forks of this repo shouldn't carry it).
2. `persona.default.md` — the tracked, shipped default: the assistant with no invented
   character — helpful, warm, direct. Used whenever `persona.md` is absent. The framework is
   fully functional on the default.

Alongside them:

| File | Tracked? | What it is |
|---|---|---|
| `persona.default.md` | ✅ | The default-Claude persona (see above). |
| `persona.template.md` | ✅ | The generation template `/setup-persona` fills (`{assistant_name}`-style tokens). |
| `identity.example.json` | ✅ | Commented fill-in seed for `identity.json`. |
| `identity.json` | ❌ gitignored | Structured identity **code** reads (assistant name/pronouns/email/TTS; owner name/pronouns/email/timezone). Written by the wizard + store onboarding; read via `seneschal/scripts/identity_common.py`. Every field nullable — the daemon boots fine without it. |
| `persona.md` | ❌ gitignored | The personalized persona **skills** read. Regenerate via the wizard; don't hand-drift it away from `identity.json`. |
| `owner-profile.md` | ❌ gitignored | Who the owner is — the context the assistant uses to judge relevance. Built by the `/setup-store` onboarding interview (confirm-every-fact); keep it short and current. |

**Why the split:** `identity.json` is for *code* (the daemon's grounding prompt, timezone logic,
the phone Worker's env emission); the two Markdown files are for the *model* (voice, demeanor,
judgment). The wizard writes both from one interview so they never disagree.

**Changing the persona later:** re-run `/setup-persona`. It re-reads what exists, asks only for
what you want to change, regenerates `persona.md` + `identity.json`, and re-prints the phone
Worker `[vars]`/secrets block if you use the phone stack.

**Want your persona in version control?** It's your fork — delete the three gitignore lines for
`persona/persona.md`, `persona/identity.json`, and `persona/owner-profile.md`. Just know that
anything you push carries your personal details with it.
