// Shared response shapes for the cockpit backend (cockpit/server/*). Kept hand-written and loose
// (nullable/optional almost everywhere) because the backend readers are deliberately tolerant of
// missing/partial state files — the frontend should be too.

export interface HealthResponse {
  ok: boolean
  service: string
  version: string
  state_dir: string
  state_dir_exists: boolean
  dev_no_auth: boolean
  web_dist_present: boolean
}

export interface SessionEntry {
  file: string
  session_id: string | null
  source: string
  pid: number | null
  started_at: string | null
  last_seen: string | null
  working_on: string | null
  cwd: string | null
  branch: string | null
  phase: string | null
  age_seconds: number | null
  gates_delivery: boolean
  ttl_seconds: number
  live: boolean
}

export interface SessionsResponse {
  sessions: SessionEntry[]
  count: number
}

export interface OneirosRecord {
  id?: string
  session_id?: string
  ended_at?: string
  cwd?: string
  branch?: string
  title?: string
  engine?: string
  user_turns?: number
  files_touched?: string[]
  distillate?: string
  [key: string]: unknown
}

export interface OneiroiResponse {
  oneiroi: OneirosRecord[]
  count: number
}

export interface SeneschaldHealthResponse {
  available: boolean
  status?: string
  reason?: string
  detail?: string
  branch?: string
  head?: string
  consecutive_blocked?: number
  blocked_since?: string | null
  last_ok?: string | null
  last_ok_age_seconds?: number
  last_alert?: string | null
  updated_at?: string
}

export interface PresenceResponse {
  available: boolean
  at_place?: string | null
  activity?: string | null
  asleep?: boolean | null
  since?: string | null
  updated_at?: string | null
}

export interface ReminderPreview {
  id: string | null
  text: string | null
  due_at: string | null
  channel: string
}

export interface RemindersResponse {
  pending_count: number
  total_count: number
  next: ReminderPreview[]
}

export interface UsageDay {
  date: string
  turns: number
  tokens: number
  by_model: Record<string, number>
}

export interface UsageResponse {
  estimated: boolean
  tokens_available: boolean
  days: UsageDay[]
  totals: { turns: number; tokens: number }
}

// GET /api/status is deliberately two shapes from one endpoint (cockpit/server/app.py::api_status):
// the registry-derived placeholder until the daemon's cockpit pipe has EVER sent a status frame this
// backend process's lifetime, then — permanently, from that point on — the richer pipe-derived
// snapshot instead (which the placeholder was always standing in for). Discriminate on `type`: only
// the live shape carries `"type": "status"` (cockpit_pipe.TYPE_STATUS); the registry shape never sets
// it. A StatusPanel that only modeled the first shape would read `undefined` from every field the
// instant the pipe connected — and report "no daemon session" while one was plainly running.
export interface StatusResponseRegistry {
  available: boolean
  phase?: string | null
  working_on?: string | null
  last_seen?: string | null
  live?: boolean
  note?: string
  pipe: 'up' | 'down'
}

export interface StatusResponseLive {
  type: 'status'
  session_up: boolean
  turn_in_flight: boolean | null
  model?: string | null
  queue_depth?: number
  pipe: 'up' | 'down'
  // Warm-session lifetime block. Every field is OPTIONAL on purpose: a daemon running older code
  // sends the v2 shape, and the panel must still render rather than blanking out. `context_*` is an
  // ESTIMATE (derived daemon-side from the CLI's turn-aggregated usage); the UI must label it as such
  // wherever it's shown.
  session_age_sec?: number | null
  turns_served?: number | null
  context_tokens?: number | null
  context_pct?: number | null
  context_window_tokens?: number | null
  context_estimated?: boolean | null
  session_cost_usd?: number | null
  last_respawn_reason?: string | null
  spawn_fallback_used?: boolean | null
  /** Running background jobs (seneschal/docs/background-jobs-spec.md). Tick-cached daemon-side, so it
   *  can lag the truth by up to one ~5 s scheduler tick — fine for a gauge, and it keeps the status
   *  frame free of disk I/O. Optional like the rest: an older daemon simply doesn't send it. */
  jobs_active?: number | null
  /** The Notion write-behind outbox (seneschal/docs/notion-write-behind-outbox-spec.md; Notion backend
   *  only): act-low writes journaled locally and not yet landed, plus dead-letters that have stopped
   *  retrying. Same tick-cached, optional contract as `jobs_active` — here because a backlog can grow
   *  for hours with no other surface reporting it. */
  outbox_pending?: number | null
  outbox_dead?: number | null
  outbox_oldest_sec?: number | null
}

export type StatusResponse = StatusResponseRegistry | StatusResponseLive

export interface RestartResponse {
  ok: boolean
  queued: boolean
  already_queued: boolean
  path: string
}

// ------------------------------------------------------------------------------------------------
// v2: the daemon pipe / chat pane. These mirror the wire protocol seneschal/scripts/cockpit_pipe.py and
// cockpit/server/pipe_client.py define — kept loose (nullable/optional almost everywhere) for the same
// reason as the rest of this file: every producer is tolerant, so the frontend should be too.

export interface ToolUsePreview {
  name?: string | null
  input_preview?: string
}

/** One line of the live transcript / GET /api/transcript backfill. `turn_id` correlates
 * turn_started -> assistant_output/tool_use -> turn_done for one turn (see presence.py's
 * `_make_stream_tee`) — events from before that field existed may be missing it. */
export interface ChatEventFrame {
  type: 'chat.event'
  kind: 'turn_started' | 'assistant_output' | 'tool_use' | 'turn_done' | string
  ts: string
  turn_id?: string
  source?: 'telegram' | 'discord' | 'cockpit' | string
  model?: string | null
  text_preview?: string
  text?: string
  tool_uses?: ToolUsePreview[]
  is_error?: boolean
  reply_preview?: string
  duration_ms?: number
  num_turns?: number
  total_cost_usd?: number
  usage?: Record<string, unknown>
}

export interface ChatStatusFrame {
  type: 'status'
  session_up?: boolean | null
  turn_in_flight?: boolean | null
  model?: string | null
  queue_depth?: number | null
  pipe?: 'up' | 'down'
}

export interface ChatAckFrame {
  type: 'chat.ack'
  id?: string | number | null
  via?: 'fallback'
}

export interface ControlAckFrame {
  type: 'control.ack'
  action?: string
  ok?: boolean
  already_queued?: boolean
}

export type WsFrame = ChatEventFrame | ChatStatusFrame | ChatAckFrame | ControlAckFrame

export interface TranscriptResponse {
  events: ChatEventFrame[]
}

export interface EmoteEntry {
  shortcode: string
  file: string
}

export interface EmotesResponse {
  emotes: EmoteEntry[]
}

// ------------------------------------------------------------------------------------------------
// v3: model dials + Fable delegation + the router dashboard (cockpit-spec.md "Model dials & Fable
// delegation"). Mirrors seneschal/scripts/model_config.py / cockpit/server/model_config.py's schema.

export interface ModelOption {
  id: string
  label: string
}

/** One selectable backend (the pluggable-backend axis) plus its own model option list — so the
 * panel's backend selector never needs a hardcoded copy of the backend list either. */
export interface BackendOption {
  id: string
  label: string
  models: ModelOption[]
}

export interface ModelConfigResponse {
  /** "claude-cli" | "codex-cli" — which backend `warm_model`/`max_routable_model` are checked
   * against. Optional so an older backend still renders — the panel treats an absent value as
   * "claude-cli", matching `model_config.load`'s own tolerant default. */
  backend?: string | null
  warm_model: string | null
  max_routable_model: string | null
  updated_at: string | null
  /** The CURRENTLY STORED backend's dial options, in RANK order, served by the backend. Optional so
   * an older backend (which sent only the two dials) still renders — the panel falls back to
   * KNOWN_MODELS below. */
  known_models?: ModelOption[] | null
  /** Every backend's id/label/model-list in one shot — lets the panel show the RIGHT model list the
   * moment the backend selector changes, without a second round-trip. Optional for the same
   * back-compat reason as `known_models`. */
  known_backends?: BackendOption[] | null
}

/**
 * FALLBACK ONLY. The dial options come from the backend (`GET /api/model-config` → `known_models`,
 * derived from `cockpit/server/model_config.py`'s RANK); this list is used only when an older backend
 * doesn't send them.
 *
 * It exists in this reduced role because the version of it that WAS authoritative caused a silent
 * downgrade: it never gained `claude-opus-5`, so a `<select>` whose value was opus-5 matched no
 * option and the browser rendered the first one — the cockpit showed "Haiku" while the daemon
 * genuinely ran opus-5 — and the only option labelled "Opus" wrote back `claude-opus-4-8` on save.
 * Keep it in sync with RANK, but prefer never to depend on it.
 */
export const KNOWN_MODELS = [
  { id: 'claude-haiku-4-5', label: 'Haiku 4.5' },
  { id: 'claude-sonnet-5', label: 'Sonnet 5' },
  { id: 'claude-opus-4-8', label: 'Opus 4.8' },
  { id: 'claude-opus-5', label: 'Opus 5' },
  { id: 'claude-opus-5-5', label: 'Opus 5.5' },
  { id: 'claude-fable-5', label: 'Fable 5' },
  { id: 'claude-fable-5-1', label: 'Fable 5.1' },
] as const

export interface RouterLogEntry {
  ts: string | null
  channel: string | null
  arm: 'triage' | 'fable' | string
  verdict: string
  category?: string | null
  confidence?: number | null
  reason?: string | null
  text_preview: string
}

export interface RouterStatsResponse {
  counts: Record<string, Record<string, number>>
  recent: RouterLogEntry[]
  total: number
}

// ------------------------------------------------------------------------------------------------
// v3.5: Oikonomos, the budget governor (cockpit-spec.md "Oikonomos — the budget governor"). Mirrors
// seneschal/scripts/governor.py / cockpit/server/governor.py's SCHEMA + config + rollups shapes.

/** One knob's schema entry — everything the Thresholds panel needs to render its control without a
 * second round-trip. `min`/`max` apply to "int" and the values of "dict_int"; `options` applies to
 * "dict_enum". `alert_at_pct`/`hard_stop` are absent on knobs with no meaningful threshold/behavior
 * (e.g. the effort-tier knob). */
export interface GovernorSchemaEntry {
  type: 'int' | 'dict_int' | 'dict_enum'
  label: string
  unit: string
  kind: 'rail' | 'advisory'
  default: number | Record<string, number> | Record<string, string>
  min?: number
  max?: number
  options?: string[]
  alert_at_pct?: number
  hard_stop?: boolean
}

export type GovernorSchema = Record<string, GovernorSchemaEntry>

/** The resolved config — every SCHEMA knob resolves to a value (its stored value if valid, else its
 * default; see governor.load()). Loose typing (mirrors the rest of this file) since knob VALUE shapes
 * vary by `type`. */
export type GovernorConfig = Record<string, number | Record<string, number> | Record<string, string>>

/** Which unit a model's `billable_by_model` figure is actually in. `mixed` is the honest answer for a
 * window that spans the switch to billable metering — part of it metered with a cache-read discount,
 * part of it on the old flat sum. A panel labels it rather than presenting two units as one number. */
export type GovernorBasis = 'billable' | 'raw' | 'mixed'

export interface GovernorRollups {
  day: string
  week: string
  fable_oneshots: { day: number; week: number }
  /** RAW flat sum — every token field at full weight. Kept for continuity with older history; NOT
   * what the rails decide on, because it counts a cache read like a fresh input token. */
  tokens_by_model: {
    day: Record<string, number>
    week: Record<string, number>
  }
  /** The basis every rail and alert reads: per-row `billable_tokens` where present, raw `tokens` as
   * the legacy fallback. Absent from an older backend, so treat it as optional. */
  billable_by_model?: {
    day: Record<string, number>
    week: Record<string, number>
  }
  basis_by_model?: {
    day: Record<string, GovernorBasis | null>
    week: Record<string, GovernorBasis | null>
  }
  /** Spend events that happened but could not be metered — surfaced so a gap never renders as zero. */
  unmetered_by_model?: {
    day: Record<string, number>
    week: Record<string, number>
  }
}

export interface GovernorConfigResponse {
  config: GovernorConfig
  schema: GovernorSchema
  rollups: GovernorRollups
}

// ------------------------------------------------------------------------------------------------
// v4: health / workout / meal panels (cockpit-spec.md "Health pipeline extension (v4)"). Mirrors
// cockpit/server/health.py's tolerant read shapes over state/health.db + state/meals.json — kept
// loose (nullable almost everywhere) for the same reason as the rest of this file: the backend
// readers are deliberately tolerant of missing/partial data, so the frontend should be too.

export interface SleepNight {
  night: string
  total_duration_min: number | null
  session_count: number
  efficiency: number | null
  sleep_score: number | null
  start_local: string | null
  end_local: string | null
}

export interface SleepResponse {
  available: boolean
  nights: SleepNight[]
  count?: number
}

export interface WorkoutSession {
  uuid: string
  start_local: string | null
  end_local: string | null
  local_date: string
  duration_min: number | null
  exercise_type: string | null
  title: string | null
  notes: string | null
  energy_kcal: number | null
  distance_m: number | null
}

export interface WorkoutWeekRollup {
  week: string
  count: number
  minutes: number
  kcal: number
}

export interface WorkoutsResponse {
  available: boolean
  sessions: WorkoutSession[]
  weekly: WorkoutWeekRollup[]
}

export interface NutritionEntry {
  meal_type: string | null
  name: string | null
  energy_kcal: number | null
  protein_g: number | null
  carbs_g: number | null
  fat_g: number | null
}

export interface NutritionDay {
  date: string
  total_kcal: number
  total_protein_g: number
  total_carbs_g: number
  total_fat_g: number
  entries: NutritionEntry[]
}

export interface NutritionResponse {
  available: boolean
  days: NutritionDay[]
}

export interface HealthSummaryResponse {
  available: boolean
  sleep: { available: boolean; last_night: SleepNight | null }
  workouts: { available: boolean; this_week: WorkoutWeekRollup | null }
  nutrition: { available: boolean; today: NutritionDay | null }
}

export interface MealPlan {
  title: string
  url?: string | null
  summary?: string | null
  tags?: string[]
}

export interface MealsResponse {
  available: boolean
  staged_at: string | null
  plans: MealPlan[]
}

// ------------------------------------------------------------------------------------------------
// v5: real OIDC auth + archon SSO tiles/proxy (cockpit-spec.md "Auth (Zitadel) & the archon SSO
// portal" / "Archon SSO tiles + proxy"). Mirrors cockpit/server/auth.py::auth_status +
// cockpit/server/archons.py's shapes.

export interface AuthStatusResponse {
  mode: 'oidc' | 'dev' | 'unconfigured' | string
  authenticated: boolean
  user?: string | null
}

export interface ArchonEntry {
  id: string
  title: string
  /**
   * `live` = serves a web UI now; `admitted` = minted + admitted, but no UI to open;
   * `specced` = minted, admission gate not yet run; `reserved` = not yet minted.
   */
  status: string
  /** The port the cockpit probes/proxies — an entry's `ui_port` when it has one, else its A2A `port`. */
  port: number | null
  /** `null` for any non-"live" entry — there's no UI, so there's nothing to probe. */
  reachable: boolean | null
}

export interface ArchonsResponse {
  archons: ArchonEntry[]
}

// ------------------------------------------------------------------------------------------------
// Jobs panel — the durable background jobs seneschal/scripts/jobs.py writes to state/jobs/
// (seneschal/docs/background-jobs-spec.md). Mirrors cockpit/server/jobs.py's shapes. Read-only: there
// is deliberately no cancel action here, because cancelling is `jobs.py cancel`, a decision rather
// than a dashboard click.

export interface Job {
  id: string
  title: string
  /** `running` | `done` | `failed` | `timed-out` | `ended-unknown` | `cancelled` — but typed loosely
   *  on purpose: the backend passes an unrecognized status straight through rather than coercing it,
   *  so a daemon on newer code can't blank this panel. */
  status: string
  is_running: boolean
  is_terminal: boolean
  exit_code: number | null
  created_at: string | null
  started_at: string | null
  ended_at: string | null
  duration_sec: number | null
  deadline_sec: number | null
  lease: boolean
  wake: boolean
  /** Stamped ONLY once the completion push actually landed. A terminal job with `null` here is a ping
   *  still being retried — the one genuinely alarming state this panel can show. */
  notified_at: string | null
  notify_channel: string | null
  pid: number | null
  error: string | null
  command: string | null
  log_path: string
  log_tail: string[]
}

export interface JobsResponse {
  /** `false` means the jobs store isn't readable at all — deliberately distinct from an empty list,
   *  which honestly means "nothing has run recently". */
  available: boolean
  active: Job[]
  recent: Job[]
  counts: Record<string, number>
  active_count: number
  lease_held: boolean
  awaiting_push: number
}

export interface JobDetailResponse {
  available: boolean
  job: Job | null
}

// ------------------------------------------------------------------------------------------------
// The open-spec ledger (GET /api/doc-status, cockpit/server/doc_status.py) — derived from each
// `seneschal/docs/*.md`'s own `**Status:**` header by the same parser CI enforces.

/** One document in the design record, as `cockpit/server/doc_status.py` derives it from that
 *  document's own `**Status:** \`TOKEN\`` header. The browser keeps no vocabulary of its own. */
export interface DocStatusDocument {
  path: string
  name: string
  /** `null` means no readable status — CI is red, or a header could not be parsed. Never hidden. */
  token: string | null
  /** For PARTIAL, which parts, in the document's own numbering. Also carries the distinctions the
   *  five values cannot: out-of-tree, host-side-pending, prose-shipped, a superseded section. */
  qualifier: string | null
  /** The document's own prose after the token. Never rewritten server-side. */
  summary: string
  /** Why it could not be classified (`missing` / `malformed` / ...), else null. */
  finding: string | null
}

/** The open-spec ledger. Entirely DERIVED — there is no stored list anywhere, because a
 *  hand-maintained ledger drifts exactly the way the prose vocabulary it replaces did. */
export interface DocStatusResponse {
  /** `false` means the ledger could not be derived at all; `reason` says why. Distinct from an
   *  empty `documents`, which honestly means "the design record is empty". */
  available: boolean
  reason: string | null
  documents: DocStatusDocument[]
  counts: Record<string, number>
  /** Render order, most-done to least. From the server; the browser has no copy. */
  order: string[]
  /** One line per token explaining what it means. Served rather than written here, so the legend
   *  cannot become a third copy of the vocabulary. */
  gloss: Record<string, string>
  /** Which tokens count as outstanding work. Computed server-side so nothing can disagree. */
  open_tokens: string[]
  open_count: number
  unclassified_count: number
  total: number
}

// ------------------------------------------------------------------------------------------------
// The session trace (GET /api/trace/sessions[/{id}], cockpit/server/trace.py, phase 1).

/** One warm session in the Trace panel's list. Built from metrics.jsonl, NOT the transcript ring — a
 *  session that aged out of a capped buffer still happened. */
export interface TraceSession {
  session_id: string
  turns: number
  cost_usd: number
  context_peak: number
  errors: number
  first_at?: string | null
  last_at?: string | null
  model?: string | null
  sources: string[]
  /** null when the spawn decision for this session isn't on file (rows predate phase 0). */
  resumed?: boolean | null
  start_class?: string | null
  start_why?: string | null
  /** The first thing the owner said in the session — `null` when nothing is on file, or the first
   *  turn is `!private` (the tombstone is honoured server-side; this is never backfilled from the id). */
  title?: string | null
}

export interface TraceSessionsResponse {
  available: boolean
  reason?: string
  sessions: TraceSession[]
  total?: number
}

export interface TraceToolUse { name?: string; input_preview?: string }

export interface TraceSaid {
  /** A role, not a name: `owner` | `assistant` (anything else passes through as its raw slug). */
  speaker?: string | null
  origin?: string | null
  text: string
  at?: string | null
}

/** One row in the Logs view. `redacted` means a `!private` turn: there is no text and no preview, and
 *  the panel must not imply otherwise. */
export interface TraceEvent {
  src: 'transcript' | 'assertion' | 'spawn'
  ts?: string | null
  kind?: string | null
  turn_id?: string | null
  model?: string | null
  source?: string | null
  surface?: string | null
  tool_uses?: TraceToolUse[]
  said?: TraceSaid[]
  reply_preview?: string | null
  redacted?: boolean
  why?: string | null
  resumed?: boolean | null
  is_error?: boolean
  /** The turn's roll-up, carried on its `turn_done` row. Declared optional and still runtime-checked
   *  in `traceView.ts` — the tolerant reader rule (cockpit/CLAUDE.md) applies to a declared type just
   *  as much as an undeclared one, because the backend may be on a different commit than this build. */
  duration_ms?: number | null
  total_cost_usd?: number | null
  num_turns?: number | null
  /** Anthropic's usage block. Deliberately `unknown`: it carries nested/varying keys, and
   *  `usageTokens()` interrogates it rather than trusting a shape we'd have to keep in sync. */
  usage?: unknown
}

export interface TraceSessionResponse {
  available: boolean
  reason?: string
  session_id?: string
  events: TraceEvent[]
  total?: number
  tool_calls?: number
  truncated?: boolean
}
