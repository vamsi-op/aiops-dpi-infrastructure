/**
 * Types mirroring the gateway's response models (services/api_gateway/api_gateway/models.py).
 *
 * Kept hand-written rather than generated: the surface is small, and being explicit here
 * makes the coupling to the API obvious. If these drift from the backend, `npm run
 * typecheck` will not catch it, so treat the two files as a pair when changing either.
 */

export type ServiceStatus = 'healthy' | 'degraded' | 'critical' | 'stale' | 'unknown';

export type Severity = 'info' | 'warning' | 'critical';

export type ScalingAction = 'scale_out' | 'scale_in' | 'hold';

export type IncidentStatus = 'open' | 'mitigating' | 'resolved' | 'acknowledged';

export interface Point {
  timestamp: string;
  tps: number;
  error_ratio: number;
}

export interface ServiceHealth {
  service: string;
  platform: string;
  status: ServiceStatus;
  reasons: string[];
  last_seen: string | null;
  instances: number;
  replicas: number;
  tps: number;
  request_count: number;
  error_count: number;
  error_ratio: number;
  success_ratio: number;
  latency_p50_seconds: number;
  latency_p95_seconds: number;
  latency_p99_seconds: number;
  cpu_utilisation_ratio: number;
  memory_utilisation_ratio: number;
  queue_depth: number;
  active_connections: number;
  settlement_lag_seconds: number;
  quote_latency_seconds: number;
  verification_latency_seconds: number;
  open_anomalies: number;
  sparkline: Point[];
}

export interface PlatformTotals {
  tps: number;
  error_ratio: number;
  latency_p99_seconds: number;
  services_total: number;
  services_healthy: number;
  services_degraded: number;
  services_critical: number;
  services_stale: number;
}

export interface OverviewCounts {
  open_incidents: number;
  anomalies_24h: number;
  critical_anomalies_24h: number;
  actions_24h: number;
  actions_succeeded_24h: number;
  chaos_active: number;
  deployments_24h: number;
}

export interface PipelineStatus {
  metrics_flowing: boolean;
  anomaly_detection_active: boolean;
  forecasting_active: boolean;
  rca_active: boolean;
  healing_enabled: boolean;
  chaos_available: boolean;
  notes: string[];
}

export interface Overview {
  generated_at: string;
  platform: string;
  platform_status: ServiceStatus;
  totals: PlatformTotals;
  counts: OverviewCounts;
  pipeline: PipelineStatus;
  services: ServiceHealth[];
}

export interface Anomaly {
  anomaly_id: string;
  detected_at: string;
  window_start: string | null;
  service: string;
  platform: string;
  kind: string;
  severity: Severity;
  score: number;
  raw_score: number | null;
  threshold: number;
  contributing_features: string[];
  feature_snapshot: Record<string, number>;
  feature_deviations: Record<string, number>;
  model_name: string;
  model_version: string;
  description: string;
  incident_id: string | null;
}

export interface TimelineBucket {
  bucket: string;
  total: number;
  critical: number;
  warning: number;
  info: number;
  max_score: number;
}

export interface Hypothesis {
  category: 'service' | 'change' | 'event' | 'unknown';
  target: string;
  confidence: number;
  summary: string;
  evidence: string[];
  correlated_metrics: Record<string, number>;
}

export interface Incident {
  incident_id: string;
  opened_at: string;
  closed_at: string | null;
  status: IncidentStatus;
  severity: Severity;
  platform: string;
  title: string;
  affected_services: string[];
  anomaly_ids: string[];
  action_ids: string[];
  hypotheses: Hypothesis[];
  ai_summary: string;
  duration_seconds: number | null;
}

export interface HealingAction {
  action_id: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  playbook: string;
  target_service: string;
  target_instance: string | null;
  platform: string;
  incident_id: string | null;
  anomaly_id: string | null;
  trigger_reason: string;
  confidence: number;
  status: string;
  dry_run: boolean;
  parameters: Record<string, unknown>;
  result_message: string;
  before_snapshot: Record<string, number>;
  after_snapshot: Record<string, number>;
}

export interface Deployment {
  deployment_id: string;
  occurred_at: string;
  service: string;
  platform: string;
  from_version: string | null;
  to_version: string;
  change_type: string;
  triggered_by: string;
  notes: string;
}

export interface IncidentDetail {
  incident: Incident;
  anomalies: Anomaly[];
  actions: HealingAction[];
  deployments: Deployment[];
}

export interface ForecastPoint {
  timestamp: string;
  predicted_tps: number;
  lower_bound: number;
  upper_bound: number;
}

export interface Prediction {
  prediction_id: string;
  created_at: string;
  service: string;
  platform: string;
  horizon_hours: number;
  current_tps: number;
  peak_predicted_tps: number;
  peak_at: string | null;
  points: ForecastPoint[];
  current_replicas: number;
  recommended_replicas: number;
  tps_per_replica: number;
  scaling_action: ScalingAction;
  model_name: string;
  model_version: string;
  backtest_mape: number | null;
}

export interface ChaosRun {
  run_id: string;
  started_at: string;
  ended_at: string | null;
  experiment: string;
  target_service: string;
  platform: string;
  duration_seconds: number;
  parameters: Record<string, unknown>;
  status: string;
  detected_at: string | null;
  detected_anomaly_id: string | null;
  incident_id: string | null;
  remediated_at: string | null;
  notes: string;
  time_to_detect_seconds: number | null;
  time_to_remediate_seconds: number | null;
}

export interface MetricPoint {
  bucket: string;
  tps: number;
  error_ratio: number;
  latency_p50_seconds: number;
  latency_p95_seconds: number;
  latency_p99_seconds: number;
  cpu_utilisation_ratio: number;
  memory_utilisation_ratio: number;
  queue_depth: number;
  settlement_lag_seconds: number;
  replicas: number;
}

export interface ServiceDetail {
  service: string;
  health: ServiceHealth;
  metrics: MetricPoint[];
  anomalies: Anomaly[];
  predictions: Prediction[];
  actions: HealingAction[];
}

export interface AutomationSummary {
  healing_enabled: boolean;
  dry_run_note: string;
  max_actions_per_hour: number;
  cooldown_seconds: number;
  min_confidence: number;
  counts_by_status: Record<string, number>;
  recent: HealingAction[];
}

export interface Topology {
  services: string[];
  edges: { from: string; to: string }[];
}

export interface AppConfig {
  platform: string;
  env: string;
  scrape_interval_seconds: number;
  forecast_horizons_hours: number[];
  anomaly_score_threshold: number;
  healing: {
    enabled: boolean;
    max_actions_per_hour: number;
    cooldown_seconds: number;
    min_confidence: number;
  };
  display_thresholds: Record<string, number>;
  live_clients: number;
}

/** Frames pushed over the WebSocket. */
export type LiveMessage =
  | { type: 'hello'; published_at: string; data: { service: string; platform: string; streams: string[]; clients: number } }
  | { type: 'pong'; published_at: string }
  | { type: 'metrics'; published_at: string; data: { samples: Record<string, unknown>[] } }
  | { type: 'anomaly'; published_at: string; data: Anomaly }
  | { type: 'incident'; published_at: string; data: Incident }
  | { type: 'action'; published_at: string; data: HealingAction }
  | { type: 'prediction'; published_at: string; data: Prediction }
  | { type: 'deployment'; published_at: string; data: Deployment }
  | { type: 'chaos'; published_at: string; data: ChaosRun };
