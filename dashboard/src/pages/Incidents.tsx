/**
 * Incident list and detail.
 *
 * The detail view is the payoff of the whole platform: several anomalies grouped into one
 * problem, a ranked explanation with the evidence behind it, and the remediation that
 * followed. Evidence is shown verbatim rather than summarised, because an operator has to
 * be able to disagree with the machine.
 */

import { useState } from 'react';

import { api } from '../api/client';
import { useApi } from '../api/hooks';
import { useLive } from '../api/live';
import { Async, Badge, Card, Empty, Kpi } from '../components/primitives';
import { formatDateTime, formatDuration, formatNumber, formatPercent, formatRelative, humanise } from '../lib/format';
import { actionLabel, actionTone, causeTone, incidentTone, severityTone } from '../lib/status';

export function IncidentsPage({
  selectedId,
  onSelect,
  onSelectService,
}: {
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onSelectService: (service: string) => void;
}) {
  const live = useLive();
  const [statusFilter, setStatusFilter] = useState('');

  const incidents = useApi(() => api.incidents({ status: statusFilter || undefined, limit: 100 }), {
    intervalMs: 20_000,
    deps: [statusFilter],
    refreshKey: live.counters.incident,
  });

  if (selectedId) {
    return <IncidentDetailView id={selectedId} onBack={() => onSelect(null)} onSelectService={onSelectService} />;
  }

  const rows = incidents.data ?? [];
  const open = rows.filter((row) => row.status === 'open' || row.status === 'mitigating').length;
  const resolved = rows.filter((row) => row.status === 'resolved');
  const meanTime =
    resolved.length > 0
      ? resolved.reduce((total, row) => total + (row.duration_seconds ?? 0), 0) / resolved.length
      : null;

  return (
    <div className="stack">
      <div className="row row--between">
        <label className="control">
          Status
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">all</option>
            <option value="open">open</option>
            <option value="mitigating">mitigating</option>
            <option value="resolved">resolved</option>
            <option value="acknowledged">acknowledged</option>
          </select>
        </label>
        <span className="card__hint">updated {formatRelative(incidents.lastUpdated)}</span>
      </div>

      <div className="grid grid--kpi">
        <Kpi label="Active" value={String(open)} tone={open > 0 ? 'crit' : 'ok'} />
        <Kpi label="Resolved" value={String(resolved.length)} />
        <Kpi
          label="Mean time to resolve"
          value={meanTime === null ? '—' : formatDuration(meanTime)}
          foot="from detection to resolution"
        />
        <Kpi label="Total in view" value={String(rows.length)} />
      </div>

      <Card title="Incidents" hint="each row is one problem, however many services it touched">
        <Async state={incidents} emptyWhen={(data) => data.length === 0} emptyMessage="No incidents recorded. The RCA engine (module 7) is not running yet.">
          {(data) => (
            <div className="table-wrap">
              <table>
                <caption className="sr-only">Incidents with status, severity, cause and duration</caption>
                <thead>
                  <tr>
                    <th scope="col">Status</th>
                    <th scope="col">Severity</th>
                    <th scope="col">Incident</th>
                    <th scope="col">Root cause</th>
                    <th scope="col" className="num">Anomalies</th>
                    <th scope="col" className="num">Duration</th>
                    <th scope="col">Opened</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((incident) => {
                    const cause = incident.hypotheses[0];
                    return (
                      <tr key={incident.incident_id} onClick={() => onSelect(incident.incident_id)} style={{ cursor: 'pointer' }}>
                        <td>
                          <Badge tone={incidentTone(incident.status)} dot pulse={incident.status === 'open'}>
                            {incident.status}
                          </Badge>
                        </td>
                        <td>
                          <Badge tone={severityTone(incident.severity)}>{incident.severity}</Badge>
                        </td>
                        <td>
                          <span className="row-button">{incident.title || incident.incident_id}</span>
                          <div className="mono">{incident.affected_services.join(', ') || '—'}</div>
                        </td>
                        <td>
                          {cause ? (
                            <>
                              <Badge tone={causeTone(cause.category)}>{cause.category}</Badge>
                              <div className="mono">
                                {cause.target} · {formatPercent(cause.confidence, 0)}
                              </div>
                            </>
                          ) : (
                            <span className="mono">unknown</span>
                          )}
                        </td>
                        <td className="num">{incident.anomaly_ids.length}</td>
                        <td className="num">{incident.duration_seconds === null ? 'ongoing' : formatDuration(incident.duration_seconds)}</td>
                        <td className="mono" title={formatDateTime(incident.opened_at)}>
                          {formatRelative(incident.opened_at)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </Card>
    </div>
  );
}

function IncidentDetailView({
  id,
  onBack,
  onSelectService,
}: {
  id: string;
  onBack: () => void;
  onSelectService: (service: string) => void;
}) {
  const detail = useApi(() => api.incidentDetail(id), { intervalMs: 20_000, deps: [id] });

  return (
    <div className="stack">
      <button type="button" className="button" style={{ alignSelf: 'flex-start' }} onClick={onBack}>
        ← All incidents
      </button>

      <Async state={detail} loadingRows={6}>
        {(data) => {
          const { incident } = data;
          return (
            <div className="stack">
              <Card>
                <div className="stack">
                  <div className="row row--between">
                    <div className="row">
                      <Badge tone={incidentTone(incident.status)} dot pulse={incident.status === 'open'}>
                        {incident.status}
                      </Badge>
                      <Badge tone={severityTone(incident.severity)}>{incident.severity}</Badge>
                      <span className="mono">{incident.incident_id}</span>
                    </div>
                    <span className="card__hint">
                      opened {formatDateTime(incident.opened_at)}
                      {incident.closed_at && ` · closed ${formatDateTime(incident.closed_at)}`}
                    </span>
                  </div>

                  <h2 style={{ margin: 0, fontSize: 18, fontWeight: 620, letterSpacing: '-0.01em' }}>
                    {incident.title || 'Untitled incident'}
                  </h2>

                  <div className="card__hint" style={{ fontSize: 12 }}>
                    {incident.status === 'resolved'
                      ? '✓ Resolution verified: Service healthy and telemetry stable within SLO thresholds.'
                      : '⚠ Incident active: Problem ongoing or recovery pending verification.'}
                  </div>

                  <div className="row">
                    {incident.affected_services.map((service) => (
                      <button key={service} type="button" className="button" onClick={() => onSelectService(service)}>
                        {service}
                      </button>
                    ))}
                  </div>

                  <div className="scorecard">
                    <div className="scorecard__cell">
                      <div className="metric__label">Duration</div>
                      <div className="metric__value">
                        {incident.duration_seconds === null ? 'ongoing' : formatDuration(incident.duration_seconds)}
                      </div>
                    </div>
                    <div className="scorecard__cell">
                      <div className="metric__label">Anomalies</div>
                      <div className="metric__value">{data.anomalies.length}</div>
                    </div>
                    <div className="scorecard__cell">
                      <div className="metric__label">Actions</div>
                      <div className="metric__value">{data.actions.length}</div>
                    </div>
                    <div className="scorecard__cell">
                      <div className="metric__label">Services</div>
                      <div className="metric__value">{incident.affected_services.length}</div>
                    </div>
                  </div>
                </div>
              </Card>

              {incident.ai_summary && (
                <Card title="Analysis" hint="generated from the correlated evidence below">
                  <p className="summary-text">{incident.ai_summary}</p>
                </Card>
              )}

              <Card title="Root cause hypotheses" hint="ranked by confidence, with the evidence for each">
                {incident.hypotheses.length === 0 ? (
                  <Empty>No hypotheses recorded.</Empty>
                ) : (
                  <div className="stack">
                    {incident.hypotheses.map((hypothesis, index) => (
                      <div key={`${hypothesis.category}-${hypothesis.target}`} className={`hypothesis${index === 0 ? ' hypothesis--primary' : ''}`}>
                        <div className="row row--between">
                          <div className="row">
                            <Badge tone={causeTone(hypothesis.category)}>{hypothesis.category}</Badge>
                            {index === 0 && <Badge tone="accent">most likely</Badge>}
                            <span className="mono">{hypothesis.target}</span>
                          </div>
                          <span className="kpi__value" style={{ fontSize: 17 }}>
                            {formatPercent(hypothesis.confidence, 0)}
                          </span>
                        </div>
                        <p style={{ margin: '8px 0 0', fontSize: 13 }}>{hypothesis.summary}</p>
                        {hypothesis.evidence.length > 0 && (
                          <ul className="evidence">
                            {hypothesis.evidence.map((item) => (
                              <li key={item}>{item}</li>
                            ))}
                          </ul>
                        )}
                        {Object.keys(hypothesis.correlated_metrics).length > 0 && (
                          <div className="row" style={{ marginTop: 8 }}>
                            {Object.entries(hypothesis.correlated_metrics).map(([name, value]) => (
                              <Badge key={name} tone="idle">
                                {humanise(name)}: {formatNumber(value, 3)}
                              </Badge>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </Card>

              <div className="grid grid--halves">
                <Card title="Correlated anomalies" hint="grouped into this one incident">
                  {data.anomalies.length === 0 ? (
                    <Empty>No anomalies linked.</Empty>
                  ) : (
                    <div className="table-wrap">
                      <table>
                        <caption className="sr-only">Anomalies correlated into this incident</caption>
                        <thead>
                          <tr>
                            <th scope="col">Service</th>
                            <th scope="col">Kind</th>
                            <th scope="col" className="num">Score</th>
                            <th scope="col">Detected</th>
                          </tr>
                        </thead>
                        <tbody>
                          {data.anomalies.map((anomaly) => (
                            <tr key={anomaly.anomaly_id}>
                              <td className="mono">{anomaly.service}</td>
                              <td>
                                <Badge tone={severityTone(anomaly.severity)}>{anomaly.kind}</Badge>
                              </td>
                              <td className="num">{formatNumber(anomaly.score, 2)}</td>
                              <td className="mono">{formatDateTime(anomaly.detected_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Card>

                <Card title="Remediation" hint="what the platform did about it">
                  {data.actions.length === 0 ? (
                    <Empty>No actions taken.</Empty>
                  ) : (
                    <div className="stack">
                      {data.actions.map((action) => (
                        <div key={action.action_id} className="hypothesis">
                          <div className="row row--between">
                            <div className="row">
                              <span className="mono">{action.playbook}</span>
                              {Boolean(action.parameters?.attempt_number) && (
                                <Badge tone="info">Attempt {String(action.parameters?.attempt_number)}/2</Badge>
                              )}
                              {Boolean(action.parameters?.root_cause) && (
                                <Badge tone="idle">Cause: {String(action.parameters?.root_cause)}</Badge>
                              )}
                              {action.actual_replicas_before !== undefined && action.actual_replicas_before !== null && action.actual_replicas_after !== undefined && action.actual_replicas_after !== null && (
                                <Badge tone="accent">
                                  Replicas: {action.actual_replicas_before} → {action.actual_replicas_after}
                                </Badge>
                              )}
                            </div>
                            <div className="row">
                              {Boolean(action.parameters?.requires_manual_intervention) && (
                                <Badge tone="crit">Manual Intervention Required</Badge>
                              )}
                              <Badge tone={action.execution_mode === 'real' ? 'ok' : 'idle'}>
                                {action.execution_mode === 'real' ? 'Real Execution' : 'Simulated'}
                              </Badge>
                              <Badge tone={actionTone(action.status)}>{actionLabel(action.status)}</Badge>
                            </div>
                          </div>
                          <p style={{ margin: '6px 0 0', fontSize: 12.5, color: 'var(--text-muted)' }}>
                            {action.trigger_reason}
                          </p>
                          {action.result_message && (
                            <p style={{ margin: '4px 0 0', fontSize: 12 }}>{action.result_message}</p>
                          )}
                          {Object.keys(action.before_snapshot).length > 0 && (
                            <div className="table-wrap" style={{ marginTop: 8 }}>
                              <table>
                                <caption className="sr-only">Metric values before and after the action</caption>
                                <thead>
                                  <tr>
                                    <th scope="col">Metric</th>
                                    <th scope="col" className="num">Before</th>
                                    <th scope="col" className="num">After</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {Object.entries(action.before_snapshot).map(([name, before]) => (
                                    <tr key={name}>
                                      <td>{humanise(name)}</td>
                                      <td className="num" style={{ color: 'var(--crit)' }}>
                                        {formatNumber(before, 3)}
                                      </td>
                                      <td className="num" style={{ color: 'var(--ok)' }}>
                                        {action.after_snapshot[name] === undefined
                                          ? '—'
                                          : formatNumber(action.after_snapshot[name]!, 3)}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </Card>
              </div>

              {data.deployments.length > 0 && (
                <Card title="Changes to affected services" hint="the change axis of root cause analysis">
                  <div className="table-wrap">
                    <table>
                      <caption className="sr-only">Deployments and config changes near this incident</caption>
                      <thead>
                        <tr>
                          <th scope="col">When</th>
                          <th scope="col">Service</th>
                          <th scope="col">Change</th>
                          <th scope="col">Version</th>
                          <th scope="col">By</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.deployments.map((deployment) => (
                          <tr key={deployment.deployment_id}>
                            <td className="mono">{formatDateTime(deployment.occurred_at)}</td>
                            <td className="mono">{deployment.service}</td>
                            <td>
                              <Badge tone="warn">{deployment.change_type}</Badge>
                            </td>
                            <td className="mono">
                              {deployment.from_version ?? '?'} → {deployment.to_version}
                            </td>
                            <td className="mono">{deployment.triggered_by}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
