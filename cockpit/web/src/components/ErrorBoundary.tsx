import { Component, type ReactNode } from 'react'

/**
 * One sick panel must never unmount the whole observatory. A single panel rendering a raw object
 * (React #31) blanks every panel at once when nothing between App and the crash can catch it. Each
 * panel renders inside its own bulkhead — a crash shows an honest inline card and the rest of the
 * cockpit sails on. (Class component because error
 * boundaries still have no hook equivalent.)
 */
export class PanelErrorBoundary extends Component<
  { name: string; children: ReactNode },
  { error: Error | null }
> {
  override state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  override render() {
    if (this.state.error) {
      return (
        <section className="panel panel-crashed">
          <div className="panel-title">
            <span>
              <span className="accent-dot" aria-hidden="true" /> {this.props.name}
            </span>
          </div>
          <div className="panel-body">
            <p className="error-state">
              This panel crashed: {String(this.state.error.message || this.state.error)}. The rest
              of the cockpit is unaffected — details in the browser console.
            </p>
          </div>
        </section>
      )
    }
    return this.props.children
  }
}
