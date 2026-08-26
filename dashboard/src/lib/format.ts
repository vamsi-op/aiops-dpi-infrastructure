/**
 * Formatting helpers.
 *
 * The rule throughout: never show more precision than the number deserves. A TPS figure
 * of 137.4821 is noise; 137.5 is information. Operators scan these values, so they must
 * be the same width and read the same way everywhere.
 */

export function formatNumber(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  if (Math.abs(value) < 1000) return formatNumber(value, value % 1 === 0 ? 0 : 1);
  return value.toLocaleString(undefined, { notation: 'compact', maximumFractionDigits: 1 });
}

export function formatPercent(ratio: number | null | undefined, decimals = 2): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—';
  return `${(ratio * 100).toFixed(decimals)}%`;
}

/** Latency in the unit that reads best: microseconds are never useful here. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(2)}s`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

export function formatClock(iso: string | Date | null | undefined): string {
  if (!iso) return '—';
  const date = typeof iso === 'string' ? new Date(iso) : iso;
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

export function formatDateTime(iso: string | Date | null | undefined): string {
  if (!iso) return '—';
  const date = typeof iso === 'string' ? new Date(iso) : iso;
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/** "4m ago" style, because operators think in elapsed time during an incident. */
export function formatRelative(iso: string | Date | null | undefined): string {
  if (!iso) return 'never';
  const date = typeof iso === 'string' ? new Date(iso) : iso;
  if (Number.isNaN(date.getTime())) return 'unknown';

  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 0) return `in ${formatDuration(Math.abs(seconds))}`;
  if (seconds < 10) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

/** Turn `memory_utilisation_ratio` into `Memory utilisation ratio`. */
export function humanise(key: string): string {
  const spaced = key.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Short label for a feature name in a tight chart axis or bar list. */
export function shortFeature(key: string): string {
  return key
    .replace(/_seconds$/, '')
    .replace(/_ratio$/, '')
    .replace(/_utilisation/, ' util')
    .replace(/_/g, ' ');
}
