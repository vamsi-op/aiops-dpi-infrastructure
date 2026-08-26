/**
 * Chaos engineering: the scorecard that proves the platform works.
 *
 * Detection and remediation times are the honest measure of an AIOps system. A dashboard
 * full of green tiles proves nothing; "we broke it deliberately and the platform noticed
 * in 38 seconds and fixed it in 2 minutes 41" is evidence.
 *
 * The experiment launcher is wired to the gateway now and reports the 501 it currently
 * returns, so the contract is visible before module 9 fills it in.
 */

import { useState } from 'react';

import { api } from '../api/client';
import { useApi } from '../api/hooks';
import { useLive } from '../api/live';
import { Async, Badge, Card, Kpi, Notice } from '../components/primitives';
import { formatDateTime, formatDuration, formatNumber, formatRelative } from '../lib/format';
import type { Tone } from '../lib/status';

const EXPERIMENTS: { id: string; name: string; description: string; expects: string }[] = [
  {
    id: 'kill_service',
    name: 'Kill service',
    description: 'Terminate a container outright.',
    expects: 'Traffic drop anomaly, then a restart playbook.',
  },
  {
    id: 'memory_hog',
    name: 'Memory hog',
    description: 'Allocate memory until the limit is near.',
    expects: 'Memory pressure anomaly, then restart before OOM.',
  },
  {
    id: 'cpu_hog',
    name: 'CPU hog',
    description: 'Saturate CPU cores.',
    expects: 'CPU saturation anomaly with latency knock-on.',
  },
  {
    id: 'network_latency',
    name: 'Network latency',
    description: 'Inject delay on a dependency path.',
    expects: 'Latency spike that RCA should trace upstream.',
  },
  {
    id: 'error_injection',
    name: 'Error injection',
    description: 'Force a percentage of responses to fail.',
    expects: 'Error burst anomaly, circuit breaker if it cascades.',
  },
  {
    id: 'traffic_spike',
    name: 'Traffic spike',
    description: 'Multiply the offered load.',
    expects: 'Traffic surge anomaly, pre-emptive scale-out.',
  },
  {
    id: 'bad_deploy',
    name: 'Bad deploy',
    description: 'Record a deployment then degrade the service.',
    expects: 'Change-correlated RCA, then automatic rollback.',
  },
];

function statusTone(status: string): Tone {
  switch (status) {
    case 'running':
      return 'accent';
    case 'completed':
      return 'ok';
    case 'aborted':
      return 'warn';
    case 'failed':
      return 'crit';
    default:
      return 'idle';
  }
}

export function ChaosPage({ onSelectIncident }: { onSelectIncident: (id: string) => void }) {
  const live = useLive();
  const [launchMessage, setLaunchMessage] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);

  const runs = useApi(() => api.chaos(50), { intervalMs: 15_000, refreshKey: live.counters.chaos });

  const completed = (runs.data ?? []).filter((run) => run.time_to_detect_seconds !== null);
  const meanDetect =
    completed.length > 0
      ? completed.reduce((total, run) => total + (run.time_to_detect_seconds ?? 0), 0) / completed.length
      : null;
  const remediated = (runs.data ?? []).filter((run) => run.time_to_remediate_seconds !== null);
  const meanRemediate =
    remediated.length > 0
      ? remediated.reduce((total, run) => total + (run.time_to_remediate_seconds ?? 0), 0) / remediated.length
      : null;
  const detectionRate = runs.data?.length ? completed.length / runs.data.length : null;

  const launch = async (experiment: string) => {
    setLaunching(true);
    setLaunchMessage(null);
    try {
      await fetch('/api/chaos', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ experiment }),
      }).then(async (response) => {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        if (!response.ok) throw new Error(body.detail ?? `Request failed with ${response.status}`);
      });
      setLaunchMessage(`Experiment ${experiment} started.`);
      runs.reload();
    } catch (error) {
      setLaunchMessage(error instanceof Error ? error.message : 'Could not start the experiment.');
    } finally {
      setLaunching(false);
    }
  };

  return (
    <div className="stack">
      <Notice tone="info">
        Chaos experiments deliberately break a service so the platform's detection and remediation can be
        measured rather than assumed. Every run records how long the platform took to notice and to act.
      </Notice>

      <div className="grid grid--kpi">
        <Kpi label="Experiments run" value={String(runs.data?.length ?? 0)} />
        <Kpi
          label="Detection rate"
          value={detectionRate === null ? '—' : `${Math.round(detectionRate * 100)}%`}
          tone={detectionRate !== null && detectionRate < 1 ? 'warn' : 'ok'}
          foot="experiments the platform noticed"
        />
        <Kpi
          label="Mean time to detect"
          value={meanDetect === null ? '—' : formatDuration(meanDetect)}
          tone={meanDetect !== null && meanDetect > 120 ? 'warn' : 'ok'}
          foot="injection to anomaly"
        />
        <Kpi
          label="Mean time to remediate"
          value={meanRemediate === null ? '—' : formatDuration(meanRemediate)}
          foot="injection to recovery"
        />
      </div>

      <Card title="Run an experiment" hint="each experiment targets a specific failure mode and expected response">
        {launchMessage && (
          <div style={{ marginBottom: 12 }}>
            <Notice tone={launchMessage.startsWith('Experiment') ? 'info' : 'warn'}>{launchMessage}</Notice>
          </div>
        )}
        <div className="table-wrap">
          <table>
            <caption className="sr-only">Available chaos experiments and what each should trigger</caption>
            <thead>
              <tr>
                <th scope="col">Experiment</th>
                <th scope="col">Injects</th>
                <th scope="col">Expected platform response</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {EXPERIMENTS.map((experiment) => (
                <tr key={experiment.id}>
                  <td className="mono">{experiment.name}</td>
                  <td>{experiment.description}</td>
                  <td style={{ color: 'var(--text-muted)' }}>{experiment.expects}</td>
                  <td>
                    <button type="button" className="button" disabled={launching} onClick={() => void launch(experiment.id)}>
                      Run
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Experiment history" hint="the scorecard">
        <Async state={runs} emptyWhen={(rows) => rows.length === 0} emptyMessage="No experiments recorded. The chaos agent (module 9) is not running yet.">
          {(rows) => (
            <div className="table-wrap">
              <table>
                <caption className="sr-only">Chaos runs with detection and remediation timings</caption>
                <thead>
                  <tr>
                    <th scope="col">Started</th>
                    <th scope="col">Experiment</th>
                    <th scope="col">Target</th>
                    <th scope="col">Status</th>
                    <th scope="col" className="num">Detected in</th>
                    <th scope="col" className="num">Remediated in</th>
                    <th scope="col" className="num">Duration</th>
                    <th scope="col">Incident</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((run) => (
                    <tr key={run.run_id}>
                      <td className="mono" title={formatDateTime(run.started_at)}>
                        {formatRelative(run.started_at)}
                      </td>
                      <td className="mono">{run.experiment}</td>
                      <td className="mono">{run.target_service}</td>
                      <td>
                        <Badge tone={statusTone(run.status)} dot pulse={run.status === 'running'}>
                          {run.status}
                        </Badge>
                      </td>
                      <td
                        className="num"
                        style={{
                          color:
                            run.time_to_detect_seconds === null
                              ? 'var(--crit)'
                              : run.time_to_detect_seconds < 60
                                ? 'var(--ok)'
                                : 'var(--warn)',
                        }}
                      >
                        {run.time_to_detect_seconds === null ? 'missed' : formatDuration(run.time_to_detect_seconds)}
                      </td>
                      <td className="num">
                        {run.time_to_remediate_seconds === null ? '—' : formatDuration(run.time_to_remediate_seconds)}
                      </td>
                      <td className="num">{formatNumber(run.duration_seconds, 0)}s</td>
                      <td>
                        {run.incident_id ? (
                          <button type="button" className="row-button" onClick={() => onSelectIncident(run.incident_id!)}>
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
    </div>
  );
}
