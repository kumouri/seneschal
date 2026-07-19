import { useEffect, useState } from 'react'
import { getGovernorConfig, putGovernorConfig } from '../api'
import type { GovernorConfig, GovernorRollups, GovernorSchema, GovernorSchemaEntry } from '../types'
import { usePolling } from '../usePolling'
import { AuthGate, Panel } from './Panel'

type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'ok' } | { kind: 'error'; message: string }

// Oikonomos, the budget governor (cockpit-spec.md "Oikonomos — the budget governor", v3.5). The whole
// form renders straight from GET /api/governor-config's `schema` — a future knob needs a SCHEMA entry
// server-side, never a change here. Design-honesty rule this panel exists to enforce: every knob is
// labeled `rail` (code-enforced — see seneschal/scripts/governor.py's check()/append_spend/rollups) or
// `advisory` (prompt/doc-side guidance the reasoning loop honors, never a hard block in code) — never
// let the two blur together.

function thresholdSuffix(entry: GovernorSchemaEntry) {
  if (entry.alert_at_pct === undefined) return null
  return (
    <span className="row-sub">
      {' '}
      · alert at {entry.alert_at_pct}% · {entry.hard_stop ? 'hard-stop' : 'soft-ask'}
    </span>
  )
}

function IntKnob({
  name,
  entry,
  value,
  onChange,
}: {
  name: string
  entry: GovernorSchemaEntry
  value: number
  onChange: (v: number) => void
}) {
  return (
    <div className="dial-row" key={name}>
      <label className="dial-label" htmlFor={`gov-${name}`}>
        {entry.label} <span className="row-sub">({entry.unit})</span>
        {thresholdSuffix(entry)}
      </label>
      <input
        id={`gov-${name}`}
        type="number"
        className="dial-select gov-input"
        value={value}
        min={entry.min}
        max={entry.max}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  )
}

function DictIntKnob({
  name,
  entry,
  value,
  onChange,
}: {
  name: string
  entry: GovernorSchemaEntry
  value: Record<string, number>
  onChange: (key: string, v: number) => void
}) {
  return (
    <div className="dial-row" key={name}>
      <div className="dial-label">
        {entry.label} <span className="row-sub">({entry.unit})</span>
        {thresholdSuffix(entry)}
      </div>
      {Object.entries(value).map(([model, n]) => (
        <div className="row" key={model}>
          <span className="row-main mono">{model}</span>
          <input
            type="number"
            className="dial-select gov-input"
            value={n}
            min={entry.min}
            max={entry.max}
            onChange={(e) => onChange(model, Number(e.target.value))}
          />
        </div>
      ))}
    </div>
  )
}

function DictEnumKnob({
  name,
  entry,
  value,
  onChange,
}: {
  name: string
  entry: GovernorSchemaEntry
  value: Record<string, string>
  onChange: (key: string, v: string) => void
}) {
  return (
    <div className="dial-row" key={name}>
      <div className="dial-label">{entry.label}</div>
      {Object.entries(value).map(([mode, tier]) => (
        <div className="row" key={mode}>
          <span className="row-main">{mode}</span>
          <select className="dial-select gov-input" value={tier} onChange={(e) => onChange(mode, e.target.value)}>
            {(entry.options ?? []).map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
        </div>
      ))}
    </div>
  )
}

function SpendMeters({ config, rollups }: { config: GovernorConfig; rollups: GovernorRollups }) {
  const fableUsed = rollups.fable_oneshots.day
  const fableLimitRaw = config.fable_oneshots_per_day
  const fableLimit = typeof fableLimitRaw === 'number' ? fableLimitRaw : 0
  const fableRemaining = Math.max(0, fableLimit - fableUsed)
  const models = Array.from(
    new Set([...Object.keys(rollups.tokens_by_model.day), ...Object.keys(rollups.tokens_by_model.week)]),
  ).sort()

  return (
    <>
      <div className="stat-row">
        <div className="stat">
          <span className="value">
            {fableUsed}/{fableLimit}
          </span>
          <span className="label">Fable one-shots today</span>
        </div>
        <div className="stat">
          <span className="value">{fableRemaining}</span>
          <span className="label">remaining today</span>
        </div>
      </div>

      <div className="row-main">Metered spend (tokens)</div>
      {models.length === 0 ? (
        <p className="empty-state">No metered spend yet.</p>
      ) : (
        models.map((model) => (
          <div className="row" key={model}>
            <span className="row-main mono">{model}</span>
            <span className="row-sub">
              {(rollups.tokens_by_model.day[model] ?? 0).toLocaleString()} today ·{' '}
              {(rollups.tokens_by_model.week[model] ?? 0).toLocaleString()} this week
            </span>
          </div>
        ))
      )}
    </>
  )
}

function KnobGroup({
  title,
  keys,
  schema,
  form,
  onIntChange,
  onDictIntChange,
  onDictEnumChange,
}: {
  title: string
  keys: string[]
  schema: GovernorSchema
  form: GovernorConfig
  onIntChange: (name: string, v: number) => void
  onDictIntChange: (name: string, key: string, v: number) => void
  onDictEnumChange: (name: string, key: string, v: string) => void
}) {
  if (keys.length === 0) return null
  return (
    <div className="threshold-group">
      <div className="row-main threshold-group-title">{title}</div>
      {keys.map((name) => {
        const entry = schema[name]
        const value = form[name]
        if (!entry || value === undefined) return null
        if (entry.type === 'int' && typeof value === 'number') {
          return <IntKnob key={name} name={name} entry={entry} value={value} onChange={(v) => onIntChange(name, v)} />
        }
        if (entry.type === 'dict_int') {
          return (
            <DictIntKnob
              key={name}
              name={name}
              entry={entry}
              value={value as Record<string, number>}
              onChange={(k, v) => onDictIntChange(name, k, v)}
            />
          )
        }
        if (entry.type === 'dict_enum') {
          return (
            <DictEnumKnob
              key={name}
              name={name}
              entry={entry}
              value={value as Record<string, string>}
              onChange={(k, v) => onDictEnumChange(name, k, v)}
            />
          )
        }
        return null
      })}
    </div>
  )
}

export function ThresholdsPanel() {
  const result = usePolling(getGovernorConfig, 5000)
  const [form, setForm] = useState<GovernorConfig | null>(null)
  const [loadedOnce, setLoadedOnce] = useState(false)
  const [save, setSave] = useState<SaveState>({ kind: 'idle' })

  // Seed the editable form from the backend exactly once (the first successful poll) — afterward the
  // form is the user's own editing state, not re-clobbered by the ~5s background poll (same pattern as
  // ModelDialsPanel).
  useEffect(() => {
    if (!loadedOnce && result?.ok) {
      setForm(result.data.config)
      setLoadedOnce(true)
    }
  }, [result, loadedOnce])

  if (!result) return <Panel title="Thresholds">Loading…</Panel>
  if (!result.ok) {
    return (
      <Panel title="Thresholds">
        <AuthGate error={result.error} />
      </Panel>
    )
  }
  if (!form) return <Panel title="Thresholds">Loading…</Panel>

  const { schema, rollups } = result.data
  const railKeys = Object.keys(schema).filter((k) => schema[k]?.kind === 'rail')
  const advisoryKeys = Object.keys(schema).filter((k) => schema[k]?.kind === 'advisory')

  function setInt(name: string, v: number) {
    setForm((f) => (f ? { ...f, [name]: v } : f))
  }

  function setDictInt(name: string, key: string, v: number) {
    setForm((f) => {
      if (!f) return f
      const current = f[name]
      const base: Record<string, number> = typeof current === 'object' && current !== null ? { ...(current as Record<string, number>) } : {}
      base[key] = v
      return { ...f, [name]: base }
    })
  }

  function setDictEnum(name: string, key: string, v: string) {
    setForm((f) => {
      if (!f) return f
      const current = f[name]
      const base: Record<string, string> = typeof current === 'object' && current !== null ? { ...(current as Record<string, string>) } : {}
      base[key] = v
      return { ...f, [name]: base }
    })
  }

  async function save_() {
    if (!form) return
    setSave({ kind: 'saving' })
    const res = await putGovernorConfig(form)
    if (res.ok) {
      setForm(res.data.config)
      setSave({ kind: 'ok' })
    } else {
      setSave({ kind: 'error', message: res.error })
    }
  }

  return (
    <Panel title="Thresholds" right={<span className="pill pill-info">Oikonomos</span>}>
      <p className="row-sub">
        Rail knobs are code-enforced (metered, gated, or hard-refused); advisory knobs are prompt/doc-side
        guidance the reasoning loop honors — never a hard block in code. See references/advisor-chain.md.
      </p>

      <SpendMeters config={form} rollups={rollups} />

      <KnobGroup
        title="Rail knobs"
        keys={railKeys}
        schema={schema}
        form={form}
        onIntChange={setInt}
        onDictIntChange={setDictInt}
        onDictEnumChange={setDictEnum}
      />
      <KnobGroup
        title="Advisory knobs"
        keys={advisoryKeys}
        schema={schema}
        form={form}
        onIntChange={setInt}
        onDictIntChange={setDictInt}
        onDictEnumChange={setDictEnum}
      />

      <div className="dial-actions">
        <button type="button" className="restart-btn" disabled={save.kind === 'saving'} onClick={() => void save_()}>
          {save.kind === 'saving' ? 'Saving…' : 'Save'}
        </button>
      </div>
      {save.kind === 'ok' && <p className="row-sub">Saved.</p>}
      {save.kind === 'error' && <p className="error-state">{save.message}</p>}
    </Panel>
  )
}
