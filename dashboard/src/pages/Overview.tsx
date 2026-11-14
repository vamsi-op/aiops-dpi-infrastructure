/**
 * Landing page: platform state at a glance, then the service grid, then live activity.
 *
 * Ordering is deliberate. An operator arriving mid-incident needs, in this order: is
 * anything broken, which service, and what has the platform already done about it.
 */

import { api } from '../api/client';
import { useApi } from '../api/hooks';
import { useLive } from '../api/live';
import { Async, Badge, Card, Empty, Kpi, Notice } from '../components/primitives';
import { ServiceTile } from '../components/ServiceTile';
import { formatDuration, formatNumber, formatPercent, formatRelative } from '../lib/format';
import { actionLabel, actionTone, incidentTone, levelOf, severityTone } from '../lib/status';

const REFRESH_MS = 10_000;

export function OverviewPage({ onSelectService, onSelectIncident, onNavigate }: {
  onSelectService: (service: string) => void;
  onSelectIncident: (id: string) => void;
  onNavigate: (page: string) => void;
}) {
  const live = useLive();

  // Metric events nudge the refresh, so the grid updates as soon as the agent publishes
  // rather than waiting out the full poll interval.
  const overview = useApi(() => api.overview(), {
    intervalMs: REFRESH_MS,
    refreshKey: live.counters.metrics + live.counters.anomaly,
  });
  const incidents = useApi(() => api.incidents({ limit: 6 }), {
    intervalMs: 20_000,
    refreshKey: live.counters.incident,
  });
  const anomalies = useApi(() => api.anomalies({ hours: 24, limit: 8 }), {
    intervalMs: 20_000,
    refreshKey: live.counters.anomaly,
  });
  const actions = useApi(() => api.actions({ hours: 24, limit: 8 }), {
    intervalMs: 20_000,
    refreshKey: live.counters.action,
  });

  return (
    <div className="stack">
      {overview.data?.pipeline.notes.map((note) => (
        <Notice key={note} tone={note.includes('ENABLED') ? 'warn' : 'info'}>
          {note}
        </Notice>
      ))}

      <Async state={overview} loadingRows={4}>
        {(data) => {
          const errorTone = levelOf(data.totals.error_ratio, 0.01, 0.05);
          const latencyTone = levelOf(data.totals.latency_p99_seconds, 1, 3);
          return (
            <>
              <div className="grid grid--kpi">
                <Kpi
                  label="Throughput"
                  value={formatNumber(data.totals.tps, 1)}
                  unit="tps"
                  foot={`across ${data.totals.services_total} services`}
                />
                <Kpi
                  label="Error rate"
                  value={formatPercent(data.totals.error_ratio, 3)}
                  tone={errorTone}
                  foot="traffic-weighted across services"
                />
                <Kpi
                  label="Worst p99 latency"
                  value={formatDuration(data.totals.latency_p99_seconds)}
                  tone={latencyTone}
                  foot="slowest service right now"
                />
                <Kpi
                  label="Open incidents"
                  value={String(data.counts.open_incidents)}
                  tone={data.counts.open_incidents > 0 ? 'crit' : 'ok'}
                  foot={`${data.counts.anomalies_24h} anomalies in 24h`}
                />
                <Kpi
                  label="Remediations 24h"
                  value={String(data.counts.actions_24h)}
                  foot={`${data.counts.actions_succeeded_24h} succeeded · ${data.counts.actions_blocked_24h} blocked by safety rails`}
                />
              </div>

              <Card
                title="Service health"
                hint={`${data.totals.services_healthy} healthy · ${data.totals.services_degraded} degraded · ${data.totals.services_critical} critical · ${data.totals.services_stale} no data`}
                actions={
                  <span className="card__hint">
                    updated {formatRelative(overview.lastUpdated)}
                    {overview.refreshing && ' · refreshing'}
                  </span>
                }
              >
                {data.services.length === 0 ? (
                  <Empty>
                    No service metrics yet. Start the simulator and metrics agent; the first
                    samples arrive within a minute.
                  </Empty>
                ) : (
                  <div className="grid grid--services">
                    {data.services.map((service) => (
                      <ServiceTile key={service.service} service={service} onSelect={onSelectService} />
                    ))}
                  </div>
                )}
              </Card>
            </>
          );
        }}
      </Async>

      <div className="grid grid--halves">
        <Card
          title="Active and recent incidents"
          hint="correlated anomalies with a ranked root cause"
          actions={
            <button type="button" className="row-button" onClick={() => onNavigate('incidents')}>
              view all
            </button>
          }
        >
          <Async state={incidents} emptyWhen={(rows) => rows.length === 0} emptyMessage="No incidents recorded. The RCA engine (module 7) is not running yet.">
            {(rows) => (
              <div className="table-wrap">
                <table>
                  <caption className="sr-only">Recent incidents with status, cause and affected services</caption>
                  <thead>
                    <tr>
                      <th scope="col">Status</th>
                      <th scope="col">Incident</th>
                      <th scope="col">Cause</th>
                      <th scope="col">Opened</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((incident) => {
                      const cause = incident.hypotheses[0];
                      return (
                        <tr key={incident.incident_id}>
                          <td>
                            <Badge tone={incidentTone(incident.status)}>{incident.status}</Badge>
                          </td>
                          <td>
                            <button type="button" className="row-button" onClick={() => onSelectIncident(incident.incident_id)}>
                              {incident.title || incident.incident_id}
                            </button>
                            <div className="mono">{incident.affected_services.join(', ')}</div>
                          </td>
                          <td>
                            {cause ? (
                              <>
                                <Badge tone={severityTone(incident.severity)}>{cause.category}</Badge>
                                <div className="mono">{formatPercent(cause.confidence, 0)} confidence</div>
                              </>
                            ) : (
                              <span className="mono">unknown</span>
                            )}
                          </td>
                          <td className="mono">{formatRelative(incident.opened_at)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Async>
        </Card>

        <Card title="Live event stream" hint="pushed from the platform as it happens">
          {live.activity.length === 0 ? (
            <Empty>
              {live.state === 'open'
                ? 'Connected and waiting. Events appear here the moment a service publishes one.'
                : 'Not connected to the event stream.'}
            </Empty>
          ) : (
            <div className="activity">
              {live.activity.map((entry) => (
                <div key={entry.id} className="activity__item">
                  <span className="activity__time">{entry.at.toLocaleTimeString(undefined, { hour12: false })}</span>
                  <div>
                    <div className="activity__label">{entry.label}</div>
                    <div className="activity__detail">
                      {entry.topic} · {entry.detail}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <div className="grid grid--halves">
        <Card
          title="Recent anomalies"
          hint="detected by the Isolation Forest model"
          actions={
            <button type="button" className="row-button" onClick={() => onNavigate('anomalies')}>
              view all
            </button>
          }
        >
          <Async state={anomalies} emptyWhen={(rows) => rows.length === 0} emptyMessage="No anomalies detected in the last 24 hours.">
            {(rows) => (
              <div className="table-wrap">
                <table>
                  <caption className="sr-only">Recent anomalies by service, kind and score</caption>
                  <thead>
                    <tr>
                      <th scope="col">Service</th>
                      <th scope="col">Kind</th>
                      <th scope="col" className="num">Score</th>
                      <th scope="col">Detected</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((anomaly) => (
                      <tr key={anomaly.anomaly_id}>
                        <td className="mono">{anomaly.service}</td>
                        <td>
                          <Badge tone={severityTone(anomaly.severity)}>{anomaly.kind}</Badge>
                        </td>
                        <td className="num">{formatNumber(anomaly.score, 2)}</td>
                        <td className="mono">{formatRelative(anomaly.detected_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Async>
        </Card>

        <Card
          title="Remediation activity"
          hint="what the platform did, and what it deliberately did not do"
          actions={
            <button type="button" className="row-button" onClick={() => onNavigate('automation')}>
              view all
            </button>
          }
        >
          <Async state={actions} emptyWhen={(rows) => rows.length === 0} emptyMessage="No remediation actions in the last 24 hours.">
            {(rows) => (
              <div className="table-wrap">
                <table>
                  <caption className="sr-only">Recent healing actions with playbook, target and outcome</caption>
                  <thead>
                    <tr>
                      <th scope="col">Playbook</th>
                      <th scope="col">Target</th>
                      <th scope="col">Outcome</th>
                      <th scope="col">When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((action) => (
                      <tr key={action.action_id}>
                        <td className="mono">{action.playbook}</td>
                        <td className="mono">{action.target_service}</td>
                        <td>
                          <Badge tone={actionTone(action.status)}>{actionLabel(action.status)}</Badge>
                        </td>
                        <td className="mono">{formatRelative(action.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Async>
        </Card>
      </div>
    </div>
  );
}
