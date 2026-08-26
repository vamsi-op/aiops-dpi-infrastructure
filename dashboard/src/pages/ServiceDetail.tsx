/**
 * Drill-down for one service: traffic, latency, saturation, and everything the platform
 * has said about it.
 *
 * Anomaly windows are shaded onto the charts so the correlation between a metric
 * excursion and a detection is visible rather than something you have to infer by
 * comparing timestamps across two panels.
 */

import { useMemo, useState } from 'react';

import { api } from '../api/client';
import { useApi } from '../api/hooks';
import { useLive } from '../api/live';
import { TimeSeriesChart } from '../components/charts';
import { Async, Badge, Card, Empty, Kpi } from '../components/primitives';
import {
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  formatRelative,
} from '../lib/format';
import { actionLabel, actionTone, levelOf, scalingLabel, severityTone, statusLabel, statusTone } from '../lib/status';

const WINDOWS: { hours: number; bucket: number; label: string }[] = [
  { hours: 1, bucket: 30, label: '1h' },
  { hours: 6, bucket: 60, label: '6h' },
  { hours: 24, bucket: 300, label: '24h' },
  { hours: 72, bucket: 900, label: '3d' },
  { hours: 168, bucket: 1800, label: '7d' },
];

export function ServiceDetailPage({ service, onBack }: { service: string; onBack: () => void }) {
  const live = useLive();
  const [windowIndex, setWindowIndex] = useState(1);
  const chosen = WINDOWS[windowIndex] ?? WINDOWS[1]!;

  const detail = useApi(() => api.serviceDetail(service, chosen.hours, chosen.bucket), {
    intervalMs: 15_000,
    deps: [service, chosen.hours],
    refreshKey: live.counters.metrics,
  });

  // Shade the window each anomaly covers, so an excursion and its detection line up.
  const bands = useMemo(() => {
    const rows = detail.data?.anomalies ?? [];
    return rows.slice(0, 12).map((anomaly) => ({
      from: anomaly.window_start ?? anomaly.detected_at,
      to: anomaly.detected_at,
      colour: anomaly.severity === 'critical' ? '#f87171' : '#fbbf24',
    }));
  }, [detail.data]);

  return (
    <div className="stack">
      <div className="row row--between">
        <button type="button" className="button" onClick={onBack}>
          ← Back
        </button>
        <div className="segmented" role="group" aria-label="Time window">
          {WINDOWS.map((option, index) => (
            <button
              key={option.label}
              type="button"
              aria-pressed={index === windowIndex}
              onClick={() => setWindowIndex(index)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <Async state={detail} loadingRows={5}>
        {(data) => {
          const health = data.health;
          return (
            <div className="stack">
              <div className="grid grid--kpi">
                <Kpi
                  label="Status"
                  value={statusLabel(health.status)}
                  tone={statusTone(health.status)}
                  foot={health.reasons[0] ?? 'all display thresholds nominal'}
                />
                <Kpi label="Throughput" value={formatNumber(health.tps, 1)} unit="tps" foot={`${health.instances} instance(s), ${health.replicas} replica(s)`} />
                <Kpi
                  label="Error rate"
                  value={formatPercent(health.error_ratio, 3)}
                  tone={levelOf(health.error_ratio, 0.01, 0.05)}
                  foot={`${formatNumber(health.error_count, 0)} of ${formatNumber(health.request_count, 0)} requests`}
                />
                <Kpi
                  label="p99 latency"
                  value={formatDuration(health.latency_p99_seconds)}
                  tone={levelOf(health.latency_p99_seconds, 1, 3)}
                  foot={`p50 ${formatDuration(health.latency_p50_seconds)} · p95 ${formatDuration(health.latency_p95_seconds)}`}
                />
                <Kpi
                  label="Saturation"
                  value={formatPercent(Math.max(health.cpu_utilisation_ratio, health.memory_utilisation_ratio), 0)}
                  tone={levelOf(Math.max(health.cpu_utilisation_ratio, health.memory_utilisation_ratio), 0.8, 0.92)}
                  foot={`cpu ${formatPercent(health.cpu_utilisation_ratio, 0)} · mem ${formatPercent(health.memory_utilisation_ratio, 0)}`}
                />
              </div>

              <Card title="Traffic and errors" hint="shaded windows are periods flagged as anomalous">
                {data.metrics.length === 0 ? (
                  <Empty>No samples in this window.</Empty>
                ) : (
                  <TimeSeriesChart
                    data={data.metrics as unknown as Record<string, unknown>[]}
                    bands={bands}
                    decimals={2}
                    yLabel="TPS"
                    secondaryLabel="error ratio"
                    series={[
                      { key: 'tps', name: 'TPS', colour: '#34d399' },
                      { key: 'error_ratio', name: 'Error ratio', colour: '#f87171', secondary: true },
                    ]}
                  />
                )}
              </Card>

              <div className="grid grid--halves">
                <Card title="Latency percentiles" hint="seconds">
                  {data.metrics.length === 0 ? (
                    <Empty>No samples in this window.</Empty>
                  ) : (
                    <TimeSeriesChart
                      data={data.metrics as unknown as Record<string, unknown>[]}
                      bands={bands}
                      decimals={3}
                      yLabel="seconds"
                      height={230}
                      series={[
                        { key: 'latency_p50_seconds', name: 'p50', colour: '#60a5fa' },
                        { key: 'latency_p95_seconds', name: 'p95', colour: '#fbbf24' },
                        { key: 'latency_p99_seconds', name: 'p99', colour: '#f87171' },
                      ]}
                    />
                  )}
                </Card>

                <Card title="Saturation" hint="CPU, memory and queue depth">
                  {data.metrics.length === 0 ? (
                    <Empty>No samples in this window.</Empty>
                  ) : (
                    <TimeSeriesChart
                      data={data.metrics as unknown as Record<string, unknown>[]}
                      bands={bands}
                      decimals={2}
                      yLabel="ratio"
                      secondaryLabel="queue"
                      height={230}
                      series={[
                        { key: 'cpu_utilisation_ratio', name: 'CPU', colour: '#818cf8' },
                        { key: 'memory_utilisation_ratio', name: 'Memory', colour: '#f472b6' },
                        { key: 'queue_depth', name: 'Queue depth', colour: '#fbbf24', secondary: true },
                      ]}
                    />
                  )}
                </Card>
              </div>

              {health.settlement_lag_seconds > 0 && (
                <Card title="Settlement lag" hint="Mojaloop-specific: how far behind funds settlement is running">
                  <TimeSeriesChart
                    data={data.metrics as unknown as Record<string, unknown>[]}
                    decimals={1}
                    yLabel="seconds"
                    height={200}
                    series={[{ key: 'settlement_lag_seconds', name: 'Settlement lag', colour: '#2dd4bf' }]}
                  />
                </Card>
              )}

              <div className="grid grid--halves">
                <Card title="Forecast" hint="predicted load and the replica recommendation">
                  {data.predictions.length === 0 ? (
                    <Empty>No forecasts yet. The traffic predictor (module 5) is not running.</Empty>
                  ) : (
                    <div className="table-wrap">
                      <table>
                        <caption className="sr-only">Forecast horizons with peak load and scaling recommendation</caption>
                        <thead>
                          <tr>
                            <th scope="col">Horizon</th>
                            <th scope="col" className="num">Peak TPS</th>
                            <th scope="col" className="num">Replicas</th>
                            <th scope="col">Action</th>
                            <th scope="col" className="num">MAPE</th>
                          </tr>
                        </thead>
                        <tbody>
                          {data.predictions.map((prediction) => {
                            const scaling = scalingLabel(prediction.scaling_action);
                            return (
                              <tr key={prediction.prediction_id}>
                                <td className="mono">{prediction.horizon_hours}h</td>
                                <td className="num">{formatNumber(prediction.peak_predicted_tps, 1)}</td>
                                <td className="num">
                                  {prediction.current_replicas} → {prediction.recommended_replicas}
                                </td>
                                <td>
                                  <Badge tone={scaling.tone}>{scaling.label}</Badge>
                                </td>
                                <td className="num">
                                  {prediction.backtest_mape === null ? '—' : `${formatNumber(prediction.backtest_mape, 1)}%`}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Card>

                <Card title="Anomalies in this window">
                  {data.anomalies.length === 0 ? (
                    <Empty>No anomalies detected for this service.</Empty>
                  ) : (
                    <div className="table-wrap">
                      <table>
                        <caption className="sr-only">Anomalies for this service</caption>
                        <thead>
                          <tr>
                            <th scope="col">Kind</th>
                            <th scope="col" className="num">Score</th>
                            <th scope="col">Drivers</th>
                            <th scope="col">Detected</th>
                          </tr>
                        </thead>
                        <tbody>
                          {data.anomalies.map((anomaly) => (
                            <tr key={anomaly.anomaly_id}>
                              <td>
                                <Badge tone={severityTone(anomaly.severity)}>{anomaly.kind}</Badge>
                              </td>
                              <td className="num">{formatNumber(anomaly.score, 2)}</td>
                              <td className="mono">{anomaly.contributing_features.slice(0, 2).join(', ') || '—'}</td>
                              <td className="mono" title={formatDateTime(anomaly.detected_at)}>
                                {formatRelative(anomaly.detected_at)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Card>
              </div>

              <Card title="Remediation history for this service">
                {data.actions.length === 0 ? (
                  <Empty>No remediation actions recorded.</Empty>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <caption className="sr-only">Healing actions targeting this service</caption>
                      <thead>
                        <tr>
                          <th scope="col">Playbook</th>
                          <th scope="col">Outcome</th>
                          <th scope="col">Trigger</th>
                          <th scope="col" className="num">Confidence</th>
                          <th scope="col">When</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.actions.map((action) => (
                          <tr key={action.action_id}>
                            <td className="mono">{action.playbook}</td>
                            <td>
                              <Badge tone={actionTone(action.status)}>{actionLabel(action.status)}</Badge>
                            </td>
                            <td>{action.trigger_reason || '—'}</td>
                            <td className="num">{formatPercent(action.confidence, 0)}</td>
                            <td className="mono">{formatRelative(action.created_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </div>
          );
        }}
      </Async>
    </div>
  );
}
