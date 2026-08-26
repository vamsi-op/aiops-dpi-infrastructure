/**
 * Small building blocks used across every page.
 *
 * Accessibility notes worth keeping in mind when editing these:
 *  - `Badge` always renders text, never colour alone
 *  - `Bar` carries the ARIA meter role with real min/max/now values
 *  - loading and error states are announced through `aria-live`
 */

import type { ReactNode } from 'react';

import { formatNumber, formatPercent } from '../lib/format';
import type { Tone } from '../lib/status';

export function Badge({
  tone = 'idle',
  children,
  dot = false,
  pulse = false,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  dot?: boolean;
  pulse?: boolean;
  title?: string;
}) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {dot && <span className={`dot${pulse ? ' dot--pulse' : ''}`} aria-hidden="true" />}
      {children}
    </span>
  );
}

export function Card({
  title,
  hint,
  actions,
  children,
  className = '',
}: {
  title?: string;
  hint?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`.trim()}>
      {(title || actions) && (
        <div className="card__head">
          <div>
            {title && <h2 className="card__title">{title}</h2>}
            {hint && <p className="card__hint">{hint}</p>}
          </div>
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function Kpi({
  label,
  value,
  unit,
  foot,
  tone,
}: {
  label: string;
  value: string;
  unit?: string;
  foot?: ReactNode;
  tone?: Tone;
}) {
  const colour =
    tone === 'crit'
      ? 'var(--crit)'
      : tone === 'warn'
        ? 'var(--warn)'
        : tone === 'ok'
          ? 'var(--ok)'
          : 'var(--text)';
  return (
    <div className="card">
      <p className="kpi__label">{label}</p>
      <div className="kpi__value" style={{ color: colour }}>
        {value}
        {unit && <span className="kpi__unit">{unit}</span>}
      </div>
      {foot && <p className="kpi__foot">{foot}</p>}
    </div>
  );
}

/** A labelled utilisation bar. `value` and the thresholds are all 0..1 ratios. */
export function Bar({
  value,
  warning = 0.75,
  critical = 0.9,
  label,
}: {
  value: number;
  warning?: number;
  critical?: number;
  label: string;
}) {
  const clamped = Math.max(0, Math.min(1, value));
  const tone = clamped >= critical ? 'crit' : clamped >= warning ? 'warn' : '';
  return (
    <div
      className="bar"
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped * 100)}
      aria-label={`${label}: ${formatPercent(clamped, 0)}`}
    >
      <div
        className={`bar__fill${tone ? ` bar__fill--${tone}` : ''}`}
        style={{ width: `${clamped * 100}%` }}
      />
    </div>
  );
}

export function Notice({
  tone = 'info',
  children,
}: {
  tone?: 'info' | 'warn' | 'crit';
  children: ReactNode;
}) {
  const icon = tone === 'crit' ? '!' : tone === 'warn' ? '!' : 'i';
  return (
    <div className={`notice notice--${tone}`} role={tone === 'info' ? 'note' : 'alert'}>
      <span className="notice__icon" aria-hidden="true">
        {icon}
      </span>
      <div>{children}</div>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Loading({ label = 'Loading', rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}</span>
      <div className="stack" style={{ gap: 8 }}>
        {Array.from({ length: rows }, (_, index) => (
          <div key={index} className="skeleton" style={{ height: 18, opacity: 1 - index * 0.2 }} />
        ))}
      </div>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert">
      <Notice tone="crit">
        <strong>Could not load this panel.</strong>
        <div style={{ marginTop: 4 }}>{message}</div>
        {onRetry && (
          <button type="button" className="button" style={{ marginTop: 10 }} onClick={onRetry}>
            Retry
          </button>
        )}
      </Notice>
    </div>
  );
}

/**
 * Renders one of loading / error / empty / content, so pages do not each reimplement
 * the same four branches (and forget one).
 */
export function Async<T>({
  state,
  children,
  emptyWhen,
  emptyMessage = 'Nothing to show yet.',
  loadingRows = 3,
}: {
  state: { data: T | null; error: string | null; loading: boolean; reload: () => void };
  children: (data: T) => ReactNode;
  emptyWhen?: (data: T) => boolean;
  emptyMessage?: string;
  loadingRows?: number;
}) {
  if (state.loading && state.data === null) return <Loading rows={loadingRows} />;
  if (state.error && state.data === null)
    return <ErrorState message={state.error} onRetry={state.reload} />;
  if (state.data === null) return <Empty>{emptyMessage}</Empty>;
  if (emptyWhen?.(state.data)) return <Empty>{emptyMessage}</Empty>;
  return <>{children(state.data)}</>;
}

/** Horizontal deviation bar used for anomaly feature attribution. */
export function FeatureBar({
  name,
  sigma,
  max,
}: {
  name: string;
  sigma: number;
  max: number;
}) {
  const magnitude = Math.abs(sigma);
  const width = max > 0 ? Math.min(100, (magnitude / max) * 100) : 0;
  const tone = magnitude >= 4 ? 'crit' : magnitude >= 2 ? 'warn' : '';
  return (
    <div className="feature">
      <span className="feature__name" title={name}>
        {name}
      </span>
      <div className="bar" aria-hidden="true">
        <div
          className={`bar__fill${tone ? ` bar__fill--${tone}` : ''}`}
          style={{ width: `${width}%` }}
        />
      </div>
      <span className="feature__value">{formatNumber(sigma, 1)}σ</span>
    </div>
  );
}
