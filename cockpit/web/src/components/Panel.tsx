import type { ReactNode } from 'react'

export function Panel({
  title,
  right,
  className,
  collapsed,
  onToggleCollapsed,
  children,
}: {
  title: string
  right?: ReactNode
  /** Extra classes on the `<section>` — how a panel opts out of the tile grid's sizing. */
  className?: string
  /** Pass BOTH of these to make the panel collapsible; omit them and the markup is unchanged. */
  collapsed?: boolean
  onToggleCollapsed?: () => void
  children: ReactNode
}) {
  const collapsible = onToggleCollapsed != null && collapsed != null
  const isCollapsed = collapsible && collapsed === true
  const classes = ['panel', className, isCollapsed ? 'is-collapsed' : null]
    .filter(Boolean)
    .join(' ')

  // The heading is a real `<button>` with `aria-expanded` when it can be toggled — a caret glyph on
  // a `<div>` looks the same and is unreachable by keyboard. `right` stays OUTSIDE the button so a
  // summary count (or a future control) is never swallowed by the toggle's hit area.
  const heading = collapsible ? (
    <button
      type="button"
      className="panel-title-toggle"
      aria-expanded={!isCollapsed}
      onClick={onToggleCollapsed}
    >
      <span className="accent-dot" aria-hidden="true" />
      <span className="panel-caret" aria-hidden="true">
        {isCollapsed ? '▸' : '▾'}
      </span>
      {title}
    </button>
  ) : (
    <span>
      <span className="accent-dot" aria-hidden="true" /> {title}
    </span>
  )

  return (
    <section className={classes}>
      <div className="panel-title">
        {heading}
        {right}
      </div>
      {/* Unmounted rather than hidden when collapsed: a display:none body still lays out its
          children on some paths, and this one can hold hundreds of event rows. */}
      {!isCollapsed && <div className="panel-body">{children}</div>}
    </section>
  )
}

export function AuthGate({ error }: { error: string }) {
  const isAuthError = error === 'auth not configured'
  return (
    <p className={isAuthError ? 'empty-state' : 'error-state'}>
      {isAuthError
        ? 'Auth not configured yet — set COCKPIT_DEV_NO_AUTH=1 on the backend for local development.'
        : `Couldn't load: ${error}`}
    </p>
  )
}
