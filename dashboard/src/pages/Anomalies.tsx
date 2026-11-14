/**
 * Anomaly explorer: when they happened, and why the model thought so.
 *
 * The feature attribution panel is the important part. An anomaly score on its own is
 * not actionable; "memory is 8.5 standard deviations above its learned baseline while
 * traffic is normal" is. Selecting a row shows exactly which signals drove the score.
 */

import { useMemo, useState } from 'react';

import { api } from '../api/client';
import { useApi } from '../api/hooks';
import { useLive } from '../api/live';
import { SeverityTimeline } from '../components/charts';
import { Async, Badge, Card, Empty, FeatureBar, Kpi } from '../components/primitives';
import { formatDateTime, formatNumber, formatRelative, humanise, shortFeature } from '../lib/format';
import { severityTone } from '../lib/status';
import type { Anomaly } from '../api/types';

const WINDOWS = [
  { hours: 1, bucket: 5, label: '1h' },
  { hours: 6, bucket: 15, label: '6h' },
  { hours: 24, bucket: 30, label: '24h' },
  { hours: 72, bucket: 60, label: '3d' },
  { hours: 168, bucket: 180, label: '7d' },
];

export function AnomaliesPage({ onSelectIncident }: { onSelectIncident: (id: string) => void }) {
  const live = useLive();
  const [windowIndex, setWindowIndex] = useState(2);
  const [serviceFilter, setServiceFilter] = useState('');
  const [minScore, setMinScore] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const chosen = WINDOWS[windowIndex] ?? WINDOWS[2]!;

  const anomalies = useApi(
    () =>
      api.anomalies({
        hours: chosen.hours,
        service: serviceFilter || undefined,
        min_score: minScore,
        limit: 300,
      }),
    {
      intervalMs: 20_000,
      deps: [chosen.hours, serviceFilter, minScore],
      refreshKey: live.counters.anomaly,
    },
  );

  // Same filters as the list. The page's totals come from here: the list is capped at 300
  // rows, and counting it showed "300 anomalies" for a day that had over a thousand.
  const timeline = useApi(
    () => api.anomalyTimeline(chosen.hours, chosen.bucket, { service: serviceFilter || undefined, min_score: minScore }),
    {
    intervalMs: 30_000,
    deps: [chosen.hours, serviceFilter, minScore],
    refreshKey: live.counters.anomaly,
    },
  );

  const services = useMemo(() => {
    const names = new Set((anomalies.data ?? []).map((row) => row.service));
    return [...names].sort();
  }, [anomalies.data]);

  const selected = useMemo(
    () => (anomalies.data ?? []).find((row) => row.anomaly_id === selectedId) ?? null,
    [anomalies.data, selectedId],
  );

  const stats = useMemo(() => {
    const rows = anomalies.data ?? [];
    const buckets = timeline.data ?? [];
    const sum = (key: 'total' | 'critical' | 'info') => buckets.reduce((acc, bucket) => acc + bucket[key], 0);
    return {
      total: sum('total'),
      critical: sum('critical'),
      // Severity "info" is exactly "below the alerting threshold" (see classify._severity).
      actionable: sum('total') - sum('info'),
      topService:
        rows.length > 0
          ? [...rows.reduce((counts, row) => counts.set(row.service, (counts.get(row.service) ?? 0) + 1), new Map<string, number>())].sort(
              (a, b) => b[1] - a[1],
            )[0]?.[0] ?? '—'
          : '—',
    };
  }, [anomalies.data, timeline.data]);

  return (
    <div className="stack">
      <div className="row row--between">
        <div className="row">
          <div className="segmented" role="group" aria-label="Time window">
            {WINDOWS.map((option, index) => (
              <button key={option.label} type="button" aria-pressed={index === windowIndex} onClick={() => setWindowIndex(index)}>
                {option.label}
              </button>
            ))}
          </div>
          <label className="control">
            Service
            <select value={serviceFilter} onChange={(event) => setServiceFilter(event.target.value)}>
              <option value="">all</option>
              {services.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="control">
            Min score
            <select value={minScore} onChange={(event) => setMinScore(Number(event.target.value))}>
              <option value={0}>any</option>
              <option value={0.5}>0.50</option>
              <option value={0.65}>0.65 (threshold)</option>
              <option value={0.8}>0.80</option>
            </select>
          </label>
        </div>
        <span className="card__hint">updated {formatRelative(anomalies.lastUpdated)}</span>
      </div>

      <div className="grid grid--kpi">
        <Kpi label="Anomalies in window" value={String(stats.total)} />
        <Kpi label="Critical" value={String(stats.critical)} tone={stats.critical > 0 ? 'crit' : 'ok'} />
        <Kpi label="Above threshold" value={String(stats.actionable)} foot="escalated to root cause analysis" />
        <Kpi label="Most affected" value={stats.topService} foot="among the latest 300" />
      </div>

      <Card title="Detection timeline" hint="anomaly counts by severity">
        <Async state={timeline} emptyWhen={(rows) => rows.every((row) => row.total === 0)} emptyMessage="No anomalies in this window.">
          {(rows) => <SeverityTimeline data={rows} withDate={chosen.hours > 24} />}
        </Async>
      </Card>

      <div className="grid" style={{ gridTemplateColumns: selected ? 'minmax(0, 1.6fr) minmax(0, 1fr)' : '1fr' }}>
        <Card title="Anomalies" hint="select a row to see what drove the score">
          <Async state={anomalies} emptyWhen={(rows) => rows.length === 0} emptyMessage="No anomalies match these filters.">
            {(rows) => (
              <div className="table-wrap" style={{ maxHeight: 520, overflowY: 'auto' }}>
                <table>
                  <caption className="sr-only">Detected anomalies with service, kind, score and drivers</caption>
                  <thead>
                    <tr>
                      <th scope="col">Detected</th>
                      <th scope="col">Service</th>
                      <th scope="col">Kind</th>
                      <th scope="col" className="num">Score</th>
                      <th scope="col">Top drivers</th>
                      <th scope="col">Incident</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((anomaly) => (
                      <tr
                        key={anomaly.anomaly_id}
                        onClick={() => setSelectedId(anomaly.anomaly_id)}
                        style={{
                          cursor: 'pointer',
                          background: anomaly.anomaly_id === selectedId ? 'var(--bg-hover)' : undefined,
                        }}
                      >
                        <td className="mono" title={formatDateTime(anomaly.detected_at)}>
                          {formatRelative(anomaly.detected_at)}
                        </td>
                        <td className="mono">{anomaly.service}</td>
                        <td>
                          <Badge tone={severityTone(anomaly.severity)}>{anomaly.kind}</Badge>
                        </td>
                        <td className="num" style={{ color: anomaly.score >= anomaly.threshold ? 'var(--crit)' : undefined }}>
                          {formatNumber(anomaly.score, 2)}
                        </td>
                        <td className="mono">{anomaly.contributing_features.slice(0, 2).map(shortFeature).join(', ') || '—'}</td>
                        <td>
                          {anomaly.incident_id ? (
                            <button
                              type="button"
                              className="row-button"
                              onClick={(event) => {
                                event.stopPropagation();
                                onSelectIncident(anomaly.incident_id!);
                              }}
                            >
                              open
                            </button>
                          ) : (
                            <span className="mono">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Async>
        </Card>

        {selected && <AnomalyDetail anomaly={selected} onClose={() => setSelectedId(null)} onSelectIncident={onSelectIncident} />}
      </div>
    </div>
  );
}

function AnomalyDetail({
  anomaly,
  onClose,
  onSelectIncident,
}: {
  anomaly: Anomaly;
  onClose: () => void;
  onSelectIncident: (id: string) => void;
}) {
  const deviations = Object.entries(anomaly.feature_deviations).sort(
    (a, b) => Math.abs(b[1]) - Math.abs(a[1]),
  );
  const maxDeviation = deviations.length > 0 ? Math.abs(deviations[0]![1]) : 1;

  return (
    <Card
      title="Why this was flagged"
      hint={`${anomaly.model_name} ${anomaly.model_version}`}
      actions={
        <button type="button" className="button" onClick={onClose}>
          Close
        </button>
      }
    >
      <div className="stack">
        <div className="row">
          <Badge tone={severityTone(anomaly.severity)}>{anomaly.severity}</Badge>
          <Badge tone="accent">{anomaly.kind}</Badge>
          <span className="mono">{anomaly.service}</span>
        </div>

        <div className="scorecard">
          <div className="scorecard__cell">
            <div className="metric__label">Score</div>
            <div className="kpi__value" style={{ fontSize: 20 }}>
              {formatNumber(anomaly.score, 2)}
            </div>
          </div>
          <div className="scorecard__cell">
            <div className="metric__label">Threshold</div>
            <div className="kpi__value" style={{ fontSize: 20 }}>
              {formatNumber(anomaly.threshold, 2)}
            </div>
          </div>
          <div className="scorecard__cell">
            <div className="metric__label">Raw model score</div>
            <div className="kpi__value" style={{ fontSize: 20 }}>
              {anomaly.raw_score === null ? '—' : formatNumber(anomaly.raw_score, 3)}
            </div>
          </div>
        </div>

        {anomaly.description && <p className="summary-text">{anomaly.description}</p>}

        <div>
          <h3 className="card__title" style={{ marginBottom: 8 }}>
            Feature deviation from baseline
          </h3>
          {deviations.length === 0 ? (
            <Empty>No attribution recorded for this anomaly.</Empty>
          ) : (
            <div className="stack" style={{ gap: 7 }}>
              {deviations.map(([name, sigma]) => (
                <FeatureBar key={name} name={shortFeature(name)} sigma={sigma} max={maxDeviation} />
              ))}
            </div>
          )}
        </div>

        {Object.keys(anomaly.feature_snapshot).length > 0 && (
          <div>
            <h3 className="card__title" style={{ marginBottom: 8 }}>
              Values at detection
            </h3>
            <div className="table-wrap">
              <table>
                <caption className="sr-only">Metric values at the moment of detection</caption>
                <tbody>
                  {Object.entries(anomaly.feature_snapshot).map(([name, value]) => (
                    <tr key={name}>
                      <td>{humanise(name)}</td>
                      <td className="num">{formatNumber(value, 3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {anomaly.incident_id && (
          <button type="button" className="button" onClick={() => onSelectIncident(anomaly.incident_id!)}>
            Open incident {anomaly.incident_id}
          </button>
        )}
      </div>
    </Card>
  );
}
