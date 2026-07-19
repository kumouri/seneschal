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

export interface StatusResponse {
  available: boolean
  phase?: string | null
  working_on?: string | null
  last_seen?: string | null
  live?: boolean
  note?: string
}

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

export interface ModelConfigResponse {
  warm_model: string | null
  max_routable_model: string | null
  updated_at: string | null
}

/** The known-model list for the two dial selects, low -> high capability (must match
 * cockpit/server/model_config.py's RANK). */
export const KNOWN_MODELS = [
  { id: 'claude-haiku-4-5', label: 'Haiku' },
  { id: 'claude-sonnet-5', label: 'Sonnet' },
  { id: 'claude-opus-4-8', label: 'Opus' },
  { id: 'claude-fable-5', label: 'Fable' },
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

export interface GovernorRollups {
  day: string
  week: string
  fable_oneshots: { day: number; week: number }
  tokens_by_model: {
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
  status: string
  port: number | null
  /** `null` for a non-"live" entry (reserved/unminted) — there's nothing to probe. */
  reachable: boolean | null
}

export interface ArchonsResponse {
  archons: ArchonEntry[]
}
