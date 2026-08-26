/**
 * One service in the health grid: status, the four numbers that matter, and a sparkline.
 *
 * Rendered as a button because clicking drills into the service. The status is conveyed
 * three ways (left border colour, badge text, and the reasons tooltip) so it survives
 * both colour-blindness and a screen reader.
 */

import { Badge, Bar } from './primitives';
import { Sparkline } from './charts';
import { formatCompact, formatDuration, formatNumber, formatPercent, formatRelative } from '../lib/format';
import { levelOf, statusLabel, statusTone } from '../lib/status';
import type { ServiceHealth } from '../api/types';

export function ServiceTile({
  service,
  onSelect,
}: {
  service: ServiceHealth;
  onSelect?: (name: string) => void;
}) {
  const tone = statusTone(service.status);
  const errorTone = levelOf(service.error_ratio, 0.01, 0.05);
  const latencyTone = levelOf(service.latency_p99_seconds, 1, 3);

  const sparkColour =
    tone === 'crit' ? '#f87171' : tone === 'warn' ? '#fbbf24' : tone === 'ok' ? '#34d399' : '#7c8aa5';

  return (
    <button
      type="button"
      className={`tile tile--${service.status}`}
      onClick={() => onSelect?.(service.service)}
      aria-label={`${service.service}: ${statusLabel(service.status)}. ${formatNumber(service.tps, 1)} transactions per second. Open details.`}
    >
      <div className="row row--between">
        <span className="tile__name">{service.service}</span>
        <Badge tone={tone} dot pulse={service.status === 'critical'}>
          {statusLabel(service.status)}
        </Badge>
      </div>

      <Sparkline data={service.sparkline} colour={sparkColour} />

      <div className="tile__metrics">
        <div>
          <div className="metric__label">TPS</div>
          <div className="metric__value">{formatNumber(service.tps, 1)}</div>
        </div>
        <div>
          <div className="metric__label">Errors</div>
          <div className={`metric__value${errorTone === 'ok' ? '' : ` metric__value--${errorTone}`}`}>
            {formatPercent(service.error_ratio, 2)}
          </div>
        </div>
        <div>
          <div className="metric__label">p99</div>
          <div className={`metric__value${latencyTone === 'ok' ? '' : ` metric__value--${latencyTone}`}`}>
            {formatDuration(service.latency_p99_seconds)}
          </div>
        </div>
        <div>
          <div className="metric__label">Replicas</div>
          <div className="metric__value">{service.replicas}</div>
        </div>
      </div>

      <div className="stack" style={{ gap: 6 }}>
        <div className="row row--between" style={{ gap: 8 }}>
          <span className="metric__label" style={{ minWidth: 42 }}>
            CPU
          </span>
          <div style={{ flex: 1 }}>
            <Bar value={service.cpu_utilisation_ratio} warning={0.75} critical={0.9} label={`${service.service} CPU`} />
          </div>
          <span className="feature__value">{formatPercent(service.cpu_utilisation_ratio, 0)}</span>
        </div>
        <div className="row row--between" style={{ gap: 8 }}>
          <span className="metric__label" style={{ minWidth: 42 }}>
            Memory
          </span>
          <div style={{ flex: 1 }}>
            <Bar value={service.memory_utilisation_ratio} warning={0.8} critical={0.92} label={`${service.service} memory`} />
          </div>
          <span className="feature__value">{formatPercent(service.memory_utilisation_ratio, 0)}</span>
        </div>
      </div>

      <div className="row row--between" style={{ fontSize: 11, color: 'var(--text-dim)' }}>
        <span>
          {service.queue_depth > 0 && <>queue {formatCompact(service.queue_depth)} · </>}
          {service.settlement_lag_seconds > 0 && <>settle {formatDuration(service.settlement_lag_seconds)} · </>}
          {service.open_anomalies > 0 ? `${service.open_anomalies} anomaly(s) 1h` : 'no anomalies 1h'}
        </span>
        <span>{formatRelative(service.last_seen)}</span>
      </div>

      {service.reasons.length > 0 && (
        <div style={{ fontSize: 11, color: `var(--${tone === 'crit' ? 'crit' : 'warn'})` }}>
          {service.reasons[0]}
          {service.reasons.length > 1 && ` (+${service.reasons.length - 1} more)`}
        </div>
      )}
    </button>
  );
}
