/**
 * HTTP client for the gateway.
 *
 * Deliberately small: `fetch` plus typed wrappers. A data-fetching library would add
 * weight for little gain here, because the dashboard's caching needs are covered by the
 * polling interval and WebSocket invalidation in `useApi`.
 */

import type {
  Anomaly,
  AppConfig,
  AutomationSummary,
  ChaosRun,
  Deployment,
  HealingAction,
  Incident,
  IncidentDetail,
  Overview,
  Prediction,
  ServiceDetail,
  ServiceHealth,
  TimelineBucket,
  Topology,
} from './types';

/**
 * Relative by default so the Vite dev-server proxy and the production reverse proxy
 * both work without rebuilding. Override with VITE_API_BASE_URL to point at a gateway
 * on another host.
 */
const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

function buildUrl(path: string, params?: Record<string, string | number | boolean | undefined>): string {
  const url = `${BASE_URL}/api${path}`;
  if (!params) return url;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `${url}?${query}` : url;
}

async function request<T>(
  path: string,
  params?: Record<string, string | number | boolean | undefined>,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(buildUrl(path, params), {
    headers: { Accept: 'application/json' },
    ...init,
  });

  if (!response.ok) {
    // The gateway returns structured errors; surface its message rather than a bare
    // status code, so a failing panel can explain itself.
    let detail: string | undefined;
    try {
      const body = (await response.json()) as { detail?: string; message?: string };
      detail = body.detail ?? body.message;
    } catch {
      detail = undefined;
    }
    throw new ApiError(detail ?? `Request failed with ${response.status}`, response.status, detail);
  }

  return (await response.json()) as T;
}

export const api = {
  overview: () => request<Overview>('/overview'),
  config: () => request<AppConfig>('/config'),
  topology: () => request<Topology>('/topology'),

  services: () => request<ServiceHealth[]>('/services'),
  serviceDetail: (service: string, hours = 6, bucketSeconds = 60) =>
    request<ServiceDetail>(`/services/${encodeURIComponent(service)}`, {
      hours,
      bucket_seconds: bucketSeconds,
    }),

  anomalies: (params: { hours?: number; service?: string; min_score?: number; limit?: number } = {}) =>
    request<Anomaly[]>('/anomalies', params),
  anomalyTimeline: (hours = 24, bucketMinutes = 15, filters: { service?: string; min_score?: number } = {}) =>
    request<TimelineBucket[]>('/anomalies/timeline', { hours, bucket_minutes: bucketMinutes, ...filters }),

  incidents: (params: { status?: string; hours?: number; limit?: number } = {}) =>
    request<Incident[]>('/incidents', params),
  incidentDetail: (id: string) => request<IncidentDetail>(`/incidents/${encodeURIComponent(id)}`),

  predictions: (horizonHours?: number) =>
    request<Prediction[]>('/predictions', { horizon_hours: horizonHours }),

  automation: (hours = 24) => request<AutomationSummary>('/automation', { hours }),
  actions: (params: { hours?: number; service?: string; status?: string; limit?: number } = {}) =>
    request<HealingAction[]>('/actions', params),

  chaos: (limit = 50) => request<ChaosRun[]>('/chaos', { limit }),
  deployments: (params: { hours?: number; service?: string; limit?: number } = {}) =>
    request<Deployment[]>('/deployments', params),
};

/** WebSocket URL, derived from the page origin unless explicitly configured. */
export function liveSocketUrl(): string {
  const configured = import.meta.env.VITE_WS_URL;
  if (configured) return configured;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}
