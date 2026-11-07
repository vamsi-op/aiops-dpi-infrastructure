/**
 * Chart wrappers around recharts.
 *
 * Every chart here shares one axis style and one tooltip, so a reading means the same
 * thing on every page. Charts are decorative-only for screen readers; the underlying
 * numbers are always available in an adjacent table or KPI.
 */

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { formatClock, formatNumber } from '../lib/format';

const GRID = '#243044';
const AXIS = '#6b7a94';

interface TooltipPayloadEntry {
  name?: string | number;
  value?: number | string;
  color?: string;
  dataKey?: string | number;
}

function ChartTooltip({
  active,
  payload,
  label,
  decimals = 2,
  suffix = '',
}: {
  active?: boolean;
  payload?: TooltipPayloadEntry[];
  label?: string | number;
  decimals?: number;
  suffix?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip__label">{formatClock(String(label))}</div>
      {payload.map((entry) => (
        <div key={String(entry.dataKey)} className="chart-tooltip__row" style={{ color: entry.color }}>
          <span>{entry.name}</span>
          <span>
            {typeof entry.value === 'number' ? formatNumber(entry.value, decimals) : entry.value}
            {suffix}
          </span>
        </div>
      ))}
    </div>
  );
}

const axisProps = {
  stroke: AXIS,
  tick: { fill: AXIS, fontSize: 10.5 },
  tickLine: false,
} as const;

/** Tiny inline chart for the service health tiles. No axes, no interaction. */
export function Sparkline({
  data,
  colour = '#818cf8',
  height = 34,
}: {
  data: { timestamp: string; tps: number }[];
  colour?: string;
  height?: number;
}) {
  if (data.length < 2) {
    return <div style={{ height }} aria-hidden="true" />;
  }
  const gradientId = `spark-${colour.replace('#', '')}`;
  return (
    <div style={{ height }} aria-hidden="true">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colour} stopOpacity={0.45} />
              <stop offset="100%" stopColor={colour} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="tps"
            stroke={colour}
            strokeWidth={1.5}
            fill={`url(#${gradientId})`}
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export interface Series {
  key: string;
  name: string;
  colour: string;
  /** Right-hand axis, for a series on a different scale (a ratio next to a rate). */
  secondary?: boolean;
  dashed?: boolean;
}

export function TimeSeriesChart({
  data,
  series,
  height = 260,
  xKey = 'bucket',
  decimals = 2,
  yLabel,
  secondaryLabel,
  markers = [],
  bands = [],
}: {
  data: Record<string, unknown>[];
  series: Series[];
  height?: number;
  xKey?: string;
  decimals?: number;
  yLabel?: string;
  secondaryLabel?: string;
  /** Vertical lines, e.g. a deployment or "now". */
  markers?: { x: string | number; label: string; colour?: string }[];
  /** Shaded windows, e.g. an anomaly period. */
  bands?: { from: string | number; to: string | number; colour?: string }[];
}) {
  const hasSecondary = series.some((entry) => entry.secondary);
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: hasSecondary ? 8 : 16, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey={xKey} {...axisProps} tickFormatter={(value) => formatClock(String(value))} minTickGap={40} />
          <YAxis
            {...axisProps}
            width={52}
            label={
              yLabel
                ? { value: yLabel, angle: -90, position: 'insideLeft', fill: AXIS, fontSize: 10.5 }
                : undefined
            }
          />
          {hasSecondary && (
            <YAxis
              yAxisId="right"
              orientation="right"
              {...axisProps}
              width={52}
              label={
                secondaryLabel
                  ? {
                      value: secondaryLabel,
                      angle: 90,
                      position: 'insideRight',
                      fill: AXIS,
                      fontSize: 10.5,
                    }
                  : undefined
              }
            />
          )}
          <Tooltip content={<ChartTooltip decimals={decimals} />} />
          {series.length > 1 && (
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 6 }} iconType="plainline" iconSize={14} />
          )}
          {bands.map((band, index) => (
            <ReferenceArea
              key={`band-${index}`}
              x1={band.from}
              x2={band.to}
              fill={band.colour ?? '#f87171'}
              fillOpacity={0.09}
              ifOverflow="extendDomain"
            />
          ))}
          {markers.map((marker, index) => (
            <ReferenceLine
              key={`marker-${index}`}
              x={marker.x}
              stroke={marker.colour ?? '#fbbf24'}
              strokeDasharray="4 3"
              label={{ value: marker.label, fill: marker.colour ?? '#fbbf24', fontSize: 10, position: 'top' }}
            />
          ))}
          {series.map((entry) => (
            <Line
              key={entry.key}
              type="monotone"
              dataKey={entry.key}
              name={entry.name}
              stroke={entry.colour}
              strokeWidth={1.8}
              strokeDasharray={entry.dashed ? '5 4' : undefined}
              dot={false}
              isAnimationActive={false}
              connectNulls
              {...(entry.secondary ? { yAxisId: 'right' } : {})}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * Actual traffic against a forecast, with the confidence band shaded.
 * `actual` and `predicted` are separate keys on the same merged rows.
 */
export function ForecastChart({
  data,
  height = 280,
  nowX,
}: {
  data: Record<string, unknown>[];
  height?: number;
  nowX?: string;
}) {
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
          <defs>
            <linearGradient id="band" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#818cf8" stopOpacity={0.22} />
              <stop offset="100%" stopColor="#818cf8" stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="timestamp" {...axisProps} tickFormatter={(value) => formatClock(String(value))} minTickGap={44} />
          <YAxis {...axisProps} width={52} label={{ value: 'TPS', angle: -90, position: 'insideLeft', fill: AXIS, fontSize: 10.5 }} />
          <Tooltip content={<ChartTooltip decimals={1} />} />
          <Legend wrapperStyle={{ fontSize: 11, paddingTop: 6 }} iconType="plainline" iconSize={14} />
          {/* Upper bound drawn first, then the lower bound in the background colour: a
              cheap way to render a band without a dedicated component. */}
          <Area
            type="monotone"
            dataKey="upper_bound"
            name="Confidence band"
            stroke="none"
            fill="url(#band)"
            isAnimationActive={false}
            connectNulls
          />
          <Area
            type="monotone"
            dataKey="lower_bound"
            name="lower"
            stroke="none"
            fill="#0b0f16"
            fillOpacity={1}
            isAnimationActive={false}
            legendType="none"
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="actual"
            name="Actual"
            stroke="#34d399"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="predicted_tps"
            name="Forecast"
            stroke="#818cf8"
            strokeWidth={2}
            strokeDasharray="5 4"
            dot={false}
            isAnimationActive={false}
            connectNulls
          />
          {nowX && (
            <ReferenceLine
              x={nowX}
              stroke="#9aa8bf"
              strokeDasharray="3 3"
              label={{ value: 'now', fill: '#9aa8bf', fontSize: 10, position: 'top' }}
            />
          )}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Stacked severity counts over time, for the anomaly timeline. */
export function SeverityTimeline({
  data,
  height = 180,
}: {
  data: { bucket: string; critical: number; warning: number; info: number }[];
  height?: number;
}) {
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="bucket" {...axisProps} tickFormatter={(value) => formatClock(String(value))} minTickGap={44} />
          <YAxis {...axisProps} width={40} allowDecimals={false} />
          <Tooltip content={<ChartTooltip decimals={0} />} />
          <Legend wrapperStyle={{ fontSize: 11, paddingTop: 6 }} iconType="square" iconSize={10} />
          <Bar dataKey="critical" name="Critical" stackId="s" fill="#f87171" isAnimationActive={false} />
          <Bar dataKey="warning" name="Warning" stackId="s" fill="#fbbf24" isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
