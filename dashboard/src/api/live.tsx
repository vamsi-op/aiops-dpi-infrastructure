/**
 * WebSocket connection to the gateway, shared across the app through context.
 *
 * Responsibilities:
 *  - keep one socket open, reconnecting with backoff
 *  - expose connection state so the UI can say so honestly instead of looking stale
 *  - bump a per-topic counter when events arrive, which hooks use as a refresh trigger
 *  - keep a short activity log, so an operator can see the platform is alive
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { liveSocketUrl } from './client';
import type { LiveMessage } from './types';

export type ConnectionState = 'connecting' | 'open' | 'closed';

export type LiveTopic = 'metrics' | 'anomaly' | 'incident' | 'action' | 'prediction' | 'deployment' | 'chaos';

export interface ActivityEntry {
  id: string;
  topic: LiveTopic | 'hello';
  at: Date;
  label: string;
  detail: string;
}

interface LiveContextValue {
  state: ConnectionState;
  /** Increments each time an event of that topic arrives. Use as a `refreshKey`. */
  counters: Record<LiveTopic, number>;
  activity: ActivityEntry[];
  eventsReceived: number;
  reconnect: () => void;
}

const EMPTY_COUNTERS: Record<LiveTopic, number> = {
  metrics: 0,
  anomaly: 0,
  incident: 0,
  action: 0,
  prediction: 0,
  deployment: 0,
  chaos: 0,
};

const LiveContext = createContext<LiveContextValue>({
  state: 'closed',
  counters: EMPTY_COUNTERS,
  activity: [],
  eventsReceived: 0,
  reconnect: () => {},
});

const MAX_ACTIVITY = 40;
const MAX_BACKOFF_MS = 15_000;
const PING_INTERVAL_MS = 25_000;

function describe(message: LiveMessage): { label: string; detail: string } | null {
  switch (message.type) {
    case 'anomaly':
      return {
        label: `Anomaly on ${message.data.service}`,
        detail: `${message.data.kind} · score ${message.data.score.toFixed(2)}`,
      };
    case 'incident':
      return {
        label: message.data.title || `Incident ${message.data.incident_id}`,
        detail: `${message.data.status} · ${message.data.severity}`,
      };
    case 'action':
      return {
        label: `${message.data.playbook} on ${message.data.target_service}`,
        detail: message.data.status,
      };
    case 'prediction':
      return {
        label: `Forecast for ${message.data.service}`,
        detail: `${message.data.horizon_hours}h · ${message.data.scaling_action}`,
      };
    case 'deployment':
      return {
        label: `Deploy ${message.data.service}`,
        detail: `${message.data.from_version ?? '?'} -> ${message.data.to_version}`,
      };
    case 'chaos':
      return {
        label: `Chaos ${message.data.experiment}`,
        detail: `${message.data.target_service} · ${message.data.status}`,
      };
    case 'metrics': {
      // Individual samples are far too frequent to log, but showing the coalesced batch
      // is worth it: an operator can see at a glance that telemetry is still arriving.
      const count = message.data.samples?.length ?? 0;
      return { label: 'Metrics received', detail: `${count} service${count === 1 ? '' : 's'}` };
    }
    default:
      return null;
  }
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<ConnectionState>('connecting');
  const [counters, setCounters] = useState<Record<LiveTopic, number>>(EMPTY_COUNTERS);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [eventsReceived, setEventsReceived] = useState(0);
  const [attempt, setAttempt] = useState(0);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<number | null>(null);
  const pingTimer = useRef<number | null>(null);

  const reconnect = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    let closedByUs = false;
    let retries = 0;

    const clearTimers = () => {
      if (reconnectTimer.current !== null) window.clearTimeout(reconnectTimer.current);
      if (pingTimer.current !== null) window.clearInterval(pingTimer.current);
      reconnectTimer.current = null;
      pingTimer.current = null;
    };

    const connect = () => {
      setState('connecting');
      let socket: WebSocket;
      try {
        socket = new WebSocket(liveSocketUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        retries = 0;
        setState('open');
        // A periodic ping keeps intermediaries from idling the connection out.
        pingTimer.current = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send('ping');
        }, PING_INTERVAL_MS);
      };

      socket.onmessage = (event) => {
        let message: LiveMessage;
        try {
          message = JSON.parse(event.data as string) as LiveMessage;
        } catch {
          return; // A malformed frame must not break the stream.
        }

        if (message.type === 'pong') return;

        setEventsReceived((value) => value + 1);

        if (message.type === 'hello') {
          setActivity((entries) =>
            [
              {
                id: `hello-${Date.now()}`,
                topic: 'hello' as const,
                at: new Date(),
                label: 'Connected to live event stream',
                detail: `${message.data.streams.length} streams · ${message.data.clients} client(s)`,
              },
              ...entries,
            ].slice(0, MAX_ACTIVITY),
          );
          return;
        }

        const topic = message.type as LiveTopic;
        setCounters((current) => ({ ...current, [topic]: (current[topic] ?? 0) + 1 }));

        const described = describe(message);
        if (described) {
          setActivity((entries) =>
            [
              {
                id: `${topic}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
                topic,
                at: new Date(),
                ...described,
              },
              ...entries,
            ].slice(0, MAX_ACTIVITY),
          );
        }
      };

      socket.onerror = () => {
        // `onclose` always follows, so reconnection is handled there.
      };

      socket.onclose = () => {
        if (pingTimer.current !== null) window.clearInterval(pingTimer.current);
        pingTimer.current = null;
        setState('closed');
        if (!closedByUs) scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      retries += 1;
      const delay = Math.min(1_000 * 2 ** (retries - 1), MAX_BACKOFF_MS);
      reconnectTimer.current = window.setTimeout(connect, delay);
    };

    connect();

    return () => {
      closedByUs = true;
      clearTimers();
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [attempt]);

  const value = useMemo<LiveContextValue>(
    () => ({ state, counters, activity, eventsReceived, reconnect }),
    [state, counters, activity, eventsReceived, reconnect],
  );

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveContextValue {
  return useContext(LiveContext);
}
