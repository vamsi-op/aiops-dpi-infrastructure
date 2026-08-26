/**
 * Mapping domain values to the visual vocabulary.
 *
 * Centralised so a severity always looks the same wherever it appears. Every mapping
 * returns a text label as well as a tone, because colour alone is not an accessible way
 * to convey state.
 */

import type { ServiceStatus, Severity } from '../api/types';

export type Tone = 'ok' | 'warn' | 'crit' | 'info' | 'idle' | 'accent';

export function statusTone(status: ServiceStatus): Tone {
  switch (status) {
    case 'healthy':
      return 'ok';
    case 'degraded':
      return 'warn';
    case 'critical':
      return 'crit';
    case 'stale':
    case 'unknown':
    default:
      return 'idle';
  }
}

export function statusLabel(status: ServiceStatus): string {
  switch (status) {
    case 'healthy':
      return 'Healthy';
    case 'degraded':
      return 'Degraded';
    case 'critical':
      return 'Critical';
    case 'stale':
      return 'No data';
    default:
      return 'Unknown';
  }
}

export function severityTone(severity: Severity | string): Tone {
  switch (severity) {
    case 'critical':
      return 'crit';
    case 'warning':
      return 'warn';
    case 'info':
      return 'info';
    default:
      return 'idle';
  }
}

export function incidentTone(status: string): Tone {
  switch (status) {
    case 'open':
      return 'crit';
    case 'mitigating':
      return 'warn';
    case 'resolved':
      return 'ok';
    case 'acknowledged':
      return 'info';
    default:
      return 'idle';
  }
}

/**
 * Action statuses split three ways: executed successfully, failed, or deliberately
 * blocked by a safety rail. The blocked ones are shown as informational rather than
 * negative, because a rail doing its job is a good outcome.
 */
export function actionTone(status: string): Tone {
  if (status === 'succeeded') return 'ok';
  if (status === 'failed') return 'crit';
  if (status === 'running') return 'accent';
  if (status.startsWith('skipped')) return 'info';
  return 'idle';
}

export function actionLabel(status: string): string {
  const labels: Record<string, string> = {
    proposed: 'Proposed',
    skipped_dry_run: 'Dry run',
    skipped_cooldown: 'Cooldown',
    skipped_budget: 'Budget',
    skipped_low_confidence: 'Low confidence',
    running: 'Running',
    succeeded: 'Succeeded',
    failed: 'Failed',
  };
  return labels[status] ?? status;
}

export function causeTone(category: string): Tone {
  switch (category) {
    case 'service':
      return 'crit';
    case 'change':
      return 'warn';
    case 'event':
      return 'info';
    default:
      return 'idle';
  }
}

/** Where a value sits against warning/critical trip points, for bars and metric text. */
export function levelOf(value: number, warning: number, critical: number): Tone {
  if (value >= critical) return 'crit';
  if (value >= warning) return 'warn';
  return 'ok';
}

export function scalingLabel(action: string): { label: string; tone: Tone } {
  switch (action) {
    case 'scale_out':
      return { label: 'Scale out', tone: 'warn' };
    case 'scale_in':
      return { label: 'Scale in', tone: 'info' };
    default:
      return { label: 'Hold', tone: 'ok' };
  }
}

/** Stable colour per service so a series keeps its identity across every chart. */
const SERIES_COLOURS = ['#818cf8', '#34d399', '#fbbf24', '#f87171', '#60a5fa', '#f472b6', '#2dd4bf'];

export function seriesColour(index: number): string {
  return SERIES_COLOURS[index % SERIES_COLOURS.length] ?? '#818cf8';
}
