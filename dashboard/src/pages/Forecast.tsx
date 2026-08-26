/**
 * Traffic forecasting and the scaling decisions that follow from it.
 *
 * The chart deliberately shows actual and predicted on one continuous axis, with history
 * to the left of "now" and the forecast to the right. Splitting them into two panels
 * would hide the only thing that matters: whether the forecast is tracking reality.
 */

import { useMemo, useState } from 'react';

import { api } from '../api/client';
import { useApi } from '../api/hooks';
import { useLive } from '../api/live';
import { ForecastChart } from '../components/charts';
import { Async, Badge, Card, Empty, Kpi, Notice } from '../components/primitives';
import { formatDateTime, formatNumber, formatRelative } from '../lib/format';
import { scalingLabel } from '../lib/status';

export function ForecastPage() {
  const live = useLive();
  const [horizon, setHorizon] = useState(6);
  const [service, setService] = useState<string | null>(null);

  const predictions = useApi(() => api.predictions(horizon), {
    intervalMs: 60_000,
    deps: [horizon],
    refreshKey: live.counters.prediction,
  });

  // History for the selected service, so the forecast can be judged against reality.
  const activeService = service ?? predictions.data?.[0]?.service ?? null;
  const history = useApi(
    () => api.serviceDetail(activeService!, Math.max(horizon, 6), 300),
    { intervalMs: 60_000, deps: [activeService, horizon], enabled: activeService !== null },
  );

  const chartData = useMemo(() => {
    const prediction = (predictions.data ?? []).find((row) => row.service === activeService);
    if (!prediction) return { rows: [], nowX: undefined as string | undefined };

    // Merge on the timestamp axis: past rows carry `actual`, future rows carry the
    // forecast and its bounds. Recharts renders the gap correctly with connectNulls.
    const rows: Record<string, unknown>[] = (history.data?.metrics ?? []).map((point) => ({
      timestamp: point.bucket,
      actual: point.tps,
    }));

    const nowX = rows.at(-1)?.timestamp as string | undefined;

    for (const point of prediction.points) {
      rows.push({
        timestamp: point.timestamp,
        predicted_tps: point.predicted_tps,
        lower_bound: point.lower_bound,
        upper_bound: point.upper_bound,
      });
    }

    return { rows, nowX };
  }, [predictions.data, history.data, activeService]);

  const scaleOutCount = (predictions.data ?? []).filter((row) => row.scaling_action === 'scale_out').length;
  const scaleInCount = (predictions.data ?? []).filter((row) => row.scaling_action === 'scale_in').length;
  const worstMape = (predictions.data ?? []).reduce<number | null>(
    (worst, row) => (row.backtest_mape === null ? worst : Math.max(worst ?? 0, row.backtest_mape)),
    null,
  );

  return (
    <div className="stack">
      <Notice tone="info">
        Forecasts drive <strong>pre-emptive</strong> scaling: capacity is added before the peak arrives,
        not after a queue has already formed. Accuracy is reported as MAPE from the most recent backtest,
        so a forecast you should not trust says so.
      </Notice>

      <div className="row row--between">
        <div className="row">
          <div className="segmented" role="group" aria-label="Forecast horizon">
            {[1, 6, 24].map((hours) => (
              <button key={hours} type="button" aria-pressed={hours === horizon} onClick={() => setHorizon(hours)}>
                {hours}h
              </button>
            ))}
          </div>
          <label className="control">
            Service
            <select value={activeService ?? ''} onChange={(event) => setService(event.target.value || null)}>
              {(predictions.data ?? []).map((row) => (
                <option key={row.service} value={row.service}>
                  {row.service}
                </option>
              ))}
            </select>
          </label>
        </div>
        <span className="card__hint">updated {formatRelative(predictions.lastUpdated)}</span>
      </div>

      <div className="grid grid--kpi">
        <Kpi label="Horizon" value={`${horizon}h`} foot="forecast window" />
        <Kpi label="Scale-out recommended" value={String(scaleOutCount)} tone={scaleOutCount > 0 ? 'warn' : 'ok'} foot="services needing capacity" />
        <Kpi label="Scale-in possible" value={String(scaleInCount)} tone="info" foot="services over-provisioned" />
        <Kpi
          label="Worst backtest MAPE"
          value={worstMape === null ? '—' : `${formatNumber(worstMape, 1)}%`}
          tone={worstMape !== null && worstMape > 15 ? 'warn' : 'ok'}
          foot="lower is better"
        />
      </div>

      <Card
        title={activeService ? `Actual vs forecast · ${activeService}` : 'Actual vs forecast'}
        hint="solid green is measured traffic, dashed violet is the forecast with its confidence band"
      >
        <Async state={predictions} emptyWhen={(rows) => rows.length === 0} emptyMessage="No forecasts yet. The traffic predictor (module 5) is not running.">
          {() =>
            chartData.rows.length === 0 ? (
              <Empty>No data for this service.</Empty>
            ) : (
              <ForecastChart data={chartData.rows} nowX={chartData.nowX} />
            )
          }
        </Async>
      </Card>

      <Card title="Scaling recommendations" hint={`all services at the ${horizon}h horizon`}>
        <Async state={predictions} emptyWhen={(rows) => rows.length === 0} emptyMessage="No forecasts available.">
          {(rows) => (
            <div className="table-wrap">
              <table>
                <caption className="sr-only">Forecast peak load and replica recommendation per service</caption>
                <thead>
                  <tr>
                    <th scope="col">Service</th>
                    <th scope="col" className="num">Current TPS</th>
                    <th scope="col" className="num">Predicted peak</th>
                    <th scope="col">Peak expected</th>
                    <th scope="col" className="num">Capacity/replica</th>
                    <th scope="col" className="num">Replicas</th>
                    <th scope="col">Recommendation</th>
                    <th scope="col" className="num">MAPE</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((prediction) => {
                    const scaling = scalingLabel(prediction.scaling_action);
                    const capacity = prediction.tps_per_replica * prediction.current_replicas;
                    const willExceed = prediction.peak_predicted_tps > capacity;
                    return (
                      <tr
                        key={prediction.prediction_id}
                        onClick={() => setService(prediction.service)}
                        style={{ cursor: 'pointer', background: prediction.service === activeService ? 'var(--bg-hover)' : undefined }}
                      >
                        <td className="mono">{prediction.service}</td>
                        <td className="num">{formatNumber(prediction.current_tps, 1)}</td>
                        <td className="num" style={{ color: willExceed ? 'var(--warn)' : undefined }}>
                          {formatNumber(prediction.peak_predicted_tps, 1)}
                        </td>
                        <td className="mono">{prediction.peak_at ? formatDateTime(prediction.peak_at) : '—'}</td>
                        <td className="num">{formatNumber(prediction.tps_per_replica, 0)}</td>
                        <td className="num">
                          {prediction.current_replicas} → {prediction.recommended_replicas}
                        </td>
                        <td>
                          <Badge tone={scaling.tone}>{scaling.label}</Badge>
                          {willExceed && (
                            <div className="mono" style={{ color: 'var(--warn)' }}>
                              peak exceeds {formatNumber(capacity, 0)} TPS capacity
                            </div>
                          )}
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
        </Async>
      </Card>
    </div>
  );
}
