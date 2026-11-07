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

export const MICROSERVICES: { id: string; name: string; description: string }[] = [
  { id: 'central-ledger', name: 'Central Ledger', description: 'Core settlement & position ledger' },
  { id: 'quoting-service', name: 'Quoting Service (Payment Service)', description: 'Fee quotes and scheme rule evaluation' },
  { id: 'account-lookup-service', name: 'Account Lookup Service', description: 'Party resolution and directory lookups' },
  { id: 'ml-api-adapter', name: 'ML API Adapter (Payment Gateway)', description: 'Inbound Open API adapter and auth' },
  { id: 'transaction-request-service', name: 'Transaction Request Service (Settlement)', description: 'Payment and transfer requests' },
];

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
  const [selectedService, setSelectedService] = useState<string>('central-ledger');
  const [launchMessage, setLaunchMessage] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);
  const [scaling, setScaling] = useState(false);
  const [scaleResult, setScaleResult] = useState<{
    service: string;
    action: string;
    success: boolean;
    message: string;
    previous_replicas?: number;
    current_replicas?: number;
  } | null>(null);

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

  const handleScale = async (action: 'scale_out' | 'scale_in') => {
    setScaling(true);
    setLaunchMessage(null);
    setScaleResult(null);
    try {
      const response = await fetch('/api/chaos/scale', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ service: selectedService, action }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail ?? `Scaling failed with status ${response.status}`);
      }
      setScaleResult({
        service: selectedService,
        action,
        success: data.success,
        message: data.message,
        previous_replicas: data.previous_replicas,
        current_replicas: data.current_replicas,
      });
      setLaunchMessage(`[${data.success ? 'SUCCESS' : 'NOTICE'}] ${data.message}`);
      runs.reload();
    } catch (error) {
      const msg = error instanceof Error ? error.message : 'Scaling operation failed.';
      setScaleResult({
        service: selectedService,
        action,
        success: false,
        message: msg,
      });
      setLaunchMessage(msg);
    } finally {
      setScaling(false);
    }
  };

  const launch = async (experiment: string) => {
    setLaunching(true);
    setLaunchMessage(null);
    try {
      await fetch('/api/chaos', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          experiment,
          target_service: selectedService,
        }),
      }).then(async (response) => {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        if (!response.ok) throw new Error(body.detail ?? `Request failed with ${response.status}`);
      });
      setLaunchMessage(`Experiment '${experiment}' started on target '${selectedService}'.`);
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
        Chaos experiments deliberately break or load a service so detection, root cause analysis, and self-healing
        scaling can be measured dynamically across all 5 microservices.
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

      <Card
        title="Chaos Experiments & Dynamic Microservice Scaling"
        hint="select any microservice from the dropdown to dynamically trigger container scaling or targeted chaos injection"
      >
        {/* Select Service to Scale Dropdown & Quick Actions */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '16px',
            flexWrap: 'wrap',
            marginBottom: '16px',
            padding: '14px 16px',
            background: 'var(--bg-raised-2, #1e293b)',
            border: '1px solid var(--border-strong)',
            borderRadius: '8px',
          }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            <label
              htmlFor="select-service-to-scale"
              style={{ fontWeight: 600, fontSize: '0.9rem', color: 'var(--text)' }}
            >
              Select Service to Scale:
            </label>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
              <select
                id="select-service-to-scale"
                value={selectedService}
                onChange={(e) => {
                  setSelectedService(e.target.value);
                  setScaleResult(null);
                }}
                className="select"
                style={{
                  minWidth: '280px',
                  padding: '7px 12px',
                  borderRadius: '6px',
                  border: '1px solid var(--border-strong)',
                  background: 'var(--bg)',
                  color: 'var(--text)',
                  fontSize: '0.9rem',
                }}
              >
                {MICROSERVICES.map((svc) => (
                  <option key={svc.id} value={svc.id}>
                    {svc.name} ({svc.id})
                  </option>
                ))}
              </select>
              <Badge tone="accent">
                Selected: {selectedService}
              </Badge>
            </div>
            <span style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
              {MICROSERVICES.find((s) => s.id === selectedService)?.description}
            </span>
          </div>

          <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
            <button
              type="button"
              className="button"
              style={{
                background: 'var(--accent)',
                color: '#fff',
                border: 'none',
                fontWeight: 600,
                padding: '8px 16px',
              }}
              disabled={scaling || launching}
              onClick={() => void handleScale('scale_out')}
            >
              {scaling ? 'Scaling...' : `Scale Out (+1) ${selectedService}`}
            </button>
            <button
              type="button"
              className="button"
              disabled={scaling || launching}
              onClick={() => void handleScale('scale_in')}
            >
              Scale In (-1)
            </button>
          </div>
        </div>

        {/* Dynamic Scale Status & Feedback Banner */}
        {scaleResult && (
          <div
            style={{
              marginBottom: '16px',
              padding: '12px 16px',
              borderRadius: '6px',
              border: `1px solid ${scaleResult.success ? 'var(--ok)' : 'var(--warn)'}`,
              background: 'var(--bg-raised-2)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: '12px',
              flexWrap: 'wrap',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
              <Badge tone={scaleResult.success ? 'ok' : 'warn'} dot>
                {scaleResult.success ? 'SCALE SUCCESS' : 'SCALE NOTICE'}
              </Badge>
              <span style={{ fontWeight: 600 }}>Target: {scaleResult.service}</span>
              <span style={{ color: 'var(--text-muted)' }}>— {scaleResult.message}</span>
            </div>
            {scaleResult.current_replicas !== undefined && (
              <Badge tone="accent">
                Replicas: {scaleResult.previous_replicas ?? '—'} &rarr; {scaleResult.current_replicas}
              </Badge>
            )}
          </div>
        )}

        {launchMessage && (
          <div style={{ marginBottom: 12 }}>
            <Notice tone={launchMessage.includes('SUCCESS') || launchMessage.startsWith('Experiment') ? 'info' : 'warn'}>
              {launchMessage}
            </Notice>
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
                <th scope="col">Active Target</th>
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
                    <Badge tone="accent">
                      {selectedService}
                    </Badge>
                  </td>
                  <td>
                    <button
                      type="button"
                      className="button"
                      disabled={launching || scaling}
                      onClick={() => void launch(experiment.id)}
                    >
                      Run on {selectedService}
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
