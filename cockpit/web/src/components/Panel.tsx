import type { ReactNode } from 'react'

export function Panel({
  title,
  right,
  children,
}: {
  title: string
  right?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="panel">
      <div className="panel-title">
        <span>
          <span className="accent-dot" aria-hidden="true" /> {title}
        </span>
        {right}
      </div>
      <div className="panel-body">{children}</div>
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
