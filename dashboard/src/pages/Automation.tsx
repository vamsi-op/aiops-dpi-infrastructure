/**
 * Self-healing: what the platform is allowed to do, what it did, and what it refused.
 *
 * The refusals get equal billing with the successes. An automation system whose brakes
 * you cannot see is not something an operator will trust with production, so cooldown,
 * budget and confidence blocks are surfaced as first-class outcomes rather than hidden.
 */

import { useState } from 'react';

import { api } from '../api/client';
import { useApi } from '../api/hooks';
import { useLive } from '../api/live';
import { Async, Badge, Card, Empty, Kpi, Notice } from '../components/primitives';
import { formatDateTime, formatDuration, formatNumber, formatPercent, formatRelative, humanise } from '../lib/format';
import { actionLabel, actionTone } from '../lib/status';
import type { HealingAction } from '../api/types';

const PLAYBOOK_DESCRIPTIONS: Record<string, string> = {
  restart_on_oom: 'Restart a service whose memory is approaching its limit, before the kernel kills it.',
  circuit_break_cascade: 'Open a circuit breaker on a failing dependency path to stop a cascade spreading upstream.',
  rollback_bad_deploy: 'Revert to the previous version when errors correlate with a recent deployment.',
  scale_out_preemptive: 'Add replicas ahead of a forecast peak, rather than after a queue has formed.',
  scale_in_idle: 'Remove replicas when forecast load no longer justifies them.',
  clear_queue_backlog: 'Drain or redistribute a backed-up work queue.',
};

export function AutomationPage() {
  const live = useLive();
  const [hours, setHours] = useState(24);
  const [selected, setSelected] = useState<HealingAction | null>(null);

  const summary = useApi(() => api.automation(hours), {
    intervalMs: 20_000,
    deps: [hours],
    refreshKey: live.counters.action,
  });

  const counts = summary.data?.counts_by_status ?? {};
  const executed = (counts.succeeded ?? 0) + (counts.failed ?? 0);
  const blocked = Object.entries(counts)
    .filter(([status]) => status.startsWith('skipped'))
    .reduce((total, [, value]) => total + value, 0);

  return (
    <div className="stack">
      <Async state={summary} loadingRows={4}>
        {(data) => (
          <>
            <Notice tone={data.healing_enabled ? 'warn' : 'info'}>
              <strong>{data.healing_enabled ? 'Automation is armed.' : 'Automation is in dry-run mode.'}</strong>{' '}
              {data.dry_run_note}
            </Notice>

            <div className="grid grid--kpi">
              <Kpi
                label="Executed"
                value={String(executed)}
                tone={counts.failed ? 'warn' : 'ok'}
                foot={`${counts.succeeded ?? 0} succeeded · ${counts.failed ?? 0} failed`}
              />
              <Kpi label="Blocked by safety rails" value={String(blocked)} tone="info" foot="cooldown, budget or confidence" />
              <Kpi label="Hourly budget" value={String(data.max_actions_per_hour)} foot="maximum actions per hour" />
              <Kpi label="Cooldown" value={formatDuration(data.cooldown_seconds)} foot="per target, per playbook" />
              <Kpi label="Confidence floor" value={formatPercent(data.min_confidence, 0)} foot="minimum RCA confidence to act" />
            </div>

            <Card title="Safety rails & capacity" hint="every proposed action passes all safety and resource headroom checks before it runs">
              <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))' }}>
                <Rail
                  name="Master switch"
                  state={data.healing_enabled ? 'armed' : 'dry run'}
                  tone={data.healing_enabled ? 'warn' : 'info'}
                  detail="HEALING_ENABLED. Off by default so automation is opt-in."
                />
                <Rail
                  name="Per-target cooldown"
                  state={formatDuration(data.cooldown_seconds)}
                  tone="ok"
                  detail="Stops a flapping service being restarted in a loop."
                />
                <Rail
                  name="Hourly budget"
                  state={`${data.max_actions_per_hour}/hour`}
                  tone="ok"
                  detail="Caps total blast radius if detection misbehaves."
                />
                <Rail
                  name="Confidence floor"
                  state={formatPercent(data.min_confidence, 0)}
                  tone="ok"
                  detail="No action on a cause the RCA engine is unsure about."
                />
                {data.available_ram_mb != null && (
                  <Rail
                    name="Host capacity"
                    state={
                      data.available_ram_mb >= (data.min_available_ram_mb ?? 256) &&
                      (data.host_cpu_percent == null || data.host_cpu_percent <= (data.max_host_cpu_percent ?? 85))
                        ? 'Headroom OK'
                        : 'Constrained'
                    }
                    tone={
                      data.available_ram_mb >= (data.min_available_ram_mb ?? 256) &&
                      (data.host_cpu_percent == null || data.host_cpu_percent <= (data.max_host_cpu_percent ?? 85))
                        ? 'ok'
                        : 'warn'
                    }
                    detail={`${data.available_ram_mb.toFixed(0)} MB RAM free · ${(data.host_cpu_percent ?? 0).toFixed(0)}% CPU (${data.min_replicas ?? 1}-${data.max_replicas ?? 3} replicas)`}
                  />
                )}
              </div>
            </Card>

            <Card title="Playbooks" hint="the remediations available to the platform">
              <div className="table-wrap">
                <table>
                  <caption className="sr-only">Available playbooks and what each does</caption>
                  <thead>
                    <tr>
                      <th scope="col">Playbook</th>
                      <th scope="col">What it does</th>
                      <th scope="col" className="num">Used ({hours}h)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(PLAYBOOK_DESCRIPTIONS).map(([name, description]) => {
                      const used = data.recent.filter((action) => action.playbook === name).length;
                      return (
                        <tr key={name}>
                          <td className="mono">{name}</td>
                          <td>{description}</td>
                          <td className="num">{used || '—'}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>
          </>
        )}
      </Async>

      <div className="row row--between">
        <div className="segmented" role="group" aria-label="Time window">
          {[6, 24, 72, 168].map((option) => (
            <button key={option} type="button" aria-pressed={option === hours} onClick={() => setHours(option)}>
              {option < 24 ? `${option}h` : `${option / 24}d`}
            </button>
          ))}
        </div>
        <span className="card__hint">updated {formatRelative(summary.lastUpdated)}</span>
      </div>

      <Card title="Action log" hint="select a row for the before and after snapshots">
        <Async state={summary} emptyWhen={(data) => data.recent.length === 0} emptyMessage="No remediation actions in this window. The healing engine (module 6) is not running yet.">
          {(data) => (
            <div className="table-wrap" style={{ maxHeight: 460, overflowY: 'auto' }}>
              <table>
                <caption className="sr-only">Healing actions with playbook, target, outcome and trigger</caption>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Playbook</th>
                    <th scope="col">Target</th>
                    <th scope="col">Mode</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Replicas</th>
                    <th scope="col" className="num">Confidence</th>
                    <th scope="col">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent.map((action) => (
                    <tr
                      key={action.action_id}
                      onClick={() => setSelected(action)}
                      style={{ cursor: 'pointer', background: action.action_id === selected?.action_id ? 'var(--bg-hover)' : undefined }}
                    >
                      <td className="mono" title={formatDateTime(action.created_at)}>
                        {formatRelative(action.created_at)}
                      </td>
                      <td className="mono">{action.playbook}</td>
                      <td className="mono">{action.target_service}</td>
                      <td>
                        <Badge tone={action.execution_mode === 'real' ? 'ok' : 'info'}>
                          {action.execution_mode ?? (action.dry_run ? 'simulated' : 'real')}
                        </Badge>
                      </td>
                      <td>
                        <Badge tone={actionTone(action.status)}>{actionLabel(action.status)}</Badge>
                      </td>
                      <td className="mono" style={{ fontSize: 12 }}>
                        {action.actual_replicas_before !== undefined && action.actual_replicas_before !== null ? (
                          <span>
                            {action.actual_replicas_before} &rarr; {action.actual_replicas_after ?? action.actual_replicas_before}
                            {action.simulated_replicas_after !== undefined && action.simulated_replicas_after !== null && action.execution_mode === 'simulated' ? (
                              <span style={{ color: 'var(--text-dim)', marginLeft: 4 }}>(sim: {action.simulated_replicas_after})</span>
                            ) : null}
                          </span>
                        ) : action.playbook === 'scale_out_preemptive' ? (
                          <span>1 &rarr; 2</span>
                        ) : action.playbook === 'scale_in_idle' ? (
                          <span>2 &rarr; 1</span>
                        ) : '—'}
                      </td>
                      <td className="num">{formatPercent(action.confidence, 0)}</td>
                      <td style={{ maxWidth: 300 }}>{action.result_message || action.trigger_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </Card>

      {selected && (
        <Card
          title="Action detail"
          hint={selected.action_id}
          actions={
            <button type="button" className="button" onClick={() => setSelected(null)}>
              Close
            </button>
          }
        >
          <div className="stack">
            <div className="row">
              <Badge tone={actionTone(selected.status)}>{actionLabel(selected.status)}</Badge>
              <Badge tone={selected.execution_mode === 'real' ? 'ok' : 'info'}>
                {selected.execution_mode === 'real' ? 'Real execution' : 'Simulated'}
              </Badge>
              <Badge tone={selected.dry_run ? 'info' : 'warn'}>{selected.dry_run ? 'dry run' : 'armed'}</Badge>
              <span className="mono">{selected.playbook}</span>
              <span className="mono">{selected.target_instance ?? selected.target_service}</span>
            </div>

            {(selected.actual_replicas_before !== undefined && selected.actual_replicas_before !== null) && (
              <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))' }}>
                <div className="hypothesis">
                  <div className="metric__label">Actual Replicas Before</div>
                  <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{selected.actual_replicas_before}</div>
                </div>
                <div className="hypothesis">
                  <div className="metric__label">Actual Replicas After</div>
                  <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{selected.actual_replicas_after ?? selected.actual_replicas_before}</div>
                </div>
                {selected.simulated_replicas_after !== undefined && selected.simulated_replicas_after !== null && (
                  <div className="hypothesis">
                    <div className="metric__label">Simulated Target</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 600, color: 'var(--text-dim)' }}>{selected.simulated_replicas_after}</div>
                  </div>
                )}
              </div>
            )}

            <div>
              <div className="metric__label">Trigger</div>
              <p style={{ margin: '2px 0 0', fontSize: 13 }}>{selected.trigger_reason || '—'}</p>
            </div>
            <div>
              <div className="metric__label">Result</div>
              <p style={{ margin: '2px 0 0', fontSize: 13 }}>{selected.result_message || '—'}</p>
            </div>

            {Object.keys(selected.before_snapshot).length === 0 ? (
              <Empty>No before/after snapshots — this action did not execute.</Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <caption>Metric values immediately before and after the remediation</caption>
                  <thead>
                    <tr>
                      <th scope="col">Metric</th>
                      <th scope="col" className="num">Before</th>
                      <th scope="col" className="num">After</th>
                      <th scope="col" className="num">Change</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(selected.before_snapshot).map(([name, before]) => {
                      const after = selected.after_snapshot[name];
                      const delta = after === undefined ? null : after - before;
                      return (
                        <tr key={name}>
                          <td>{humanise(name)}</td>
                          <td className="num" style={{ color: 'var(--crit)' }}>
                            {formatNumber(before, 4)}
                          </td>
                          <td className="num" style={{ color: after === undefined ? undefined : 'var(--ok)' }}>
                            {after === undefined ? '—' : formatNumber(after, 4)}
                          </td>
                          <td className="num" style={{ color: delta !== null && delta < 0 ? 'var(--ok)' : 'var(--text-muted)' }}>
                            {delta === null ? '—' : `${delta > 0 ? '+' : ''}${formatNumber(delta, 4)}`}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}

function Rail({
  name,
  state,
  tone,
  detail,
}: {
  name: string;
  state: string;
  tone: 'ok' | 'warn' | 'info';
  detail: string;
}) {
  return (
    <div className="hypothesis">
      <div className="row row--between">
        <span style={{ fontSize: 12.5, fontWeight: 600 }}>{name}</span>
        <Badge tone={tone}>{state}</Badge>
      </div>
      <p style={{ margin: '6px 0 0', fontSize: 11.5, color: 'var(--text-dim)' }}>{detail}</p>
    </div>
  );
}
