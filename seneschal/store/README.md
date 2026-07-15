# store/ — the pluggable system of record

The assistant keeps the owner's durable data — tasks, reminders, projects, flags, goals, people,
journal, run log — in a **store**. Which store is the owner's choice; the framework ships three
backends, and every skill reads/writes them through the same three-step indirection:

1. **`store/config.json`** → which backend is `active` (gitignored; written by `/setup-store`;
   seed: `config.example.json`).
2. **`store/<backend>/schema.md`** → the domain map for that backend: which
   collection/folder/note holds each domain, its fields, and the canonical option values.
3. **`store/<backend>/mapping.md`** → HOW to execute each store verb on that backend (tool
   names, quirks, throughput rules).

Skill prose speaks six backend-neutral **store verbs** — `store-query` (list records in a domain
by filter), `store-get` (one record by ref), `store-create`, `store-update` (set fields by ref),
`store-append` (add body content), `store-search` (fuzzy/semantic) — plus domain nouns and
canonical, emoji-free option values (`importance: critical`, `status: done`). Anything
backend-specific lives ONLY in that backend's two files.

**"Never fetch schemas at runtime" survives per-backend:** on Notion it means never fetch a DB
schema mid-run (trust schema.md); on filesystem backends it means never re-derive vault
conventions by exploring (trust schema.md). Setup-time discovery by `/setup-store` is the
sanctioned exception.

## The backends

| Backend | Where data lives | Needs | Notes |
|---|---|---|---|
| `notion/` | The owner's Notion workspace | Notion MCP | Reference backend, ported from the original system. `schema.template.md` is tracked with placeholder ids; `/setup-store` renders the real, gitignored `schema.md`. Its `mcp.json` (gitignored) is what the presence daemon forwards to headless sessions. |
| `obsidian/` | An Obsidian vault subfolder | nothing (filesystem) | One note per record, YAML frontmatter as source of truth, wikilink relations, refs = vault-relative paths. Dataview-friendly; no plugin required. |
| `markdown/` | A plain folder of Markdown | nothing (filesystem) | The Obsidian engine minus vault conventions — kebab-case files, path-string relations. The zero-dependency default. |

Future backends slot in as new `store/<backend>/` pairs — same verbs, different mapping (Logseq:
block properties; Google Workspace: verbs resolved to the existing `gcal_api.py`/`gmail_api.py`
script bridge). The reserved `domains` key in config.json is for per-domain federation
(one domain's home backend + read-only mirrors) — documented intent in
[`federation.md`](federation.md), no v1 code.

## config.json

```json
{
  "version": 1,
  "active": "markdown",
  "backends": {
    "notion":   { "mcp_config": "seneschal/store/notion/mcp.json" },
    "obsidian": { "vault_path": "C:/Users/you/Documents/Vault", "subfolder": "Seneschal",
                  "search_whole_vault": false },
    "markdown": { "root_path": "~/seneschal-data" }
  },
  "domains": {}
}
```

Paths are forward-slash (Windows-safe in JSON); `~` is expanded by readers. The daemon parses
this file to decide whether to forward an MCP config (`backends[active].mcp_config`) —
filesystem backends need none, and that is healthy.
