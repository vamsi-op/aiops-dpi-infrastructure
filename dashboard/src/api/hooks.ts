/**
 * Data-fetching hooks: polling plus WebSocket-driven refresh.
 *
 * Why both: polling guarantees the view converges even if the socket drops or a service
 * writes to the database without publishing an event. The socket makes the common case
 * feel instant. Neither alone is sufficient.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from './client';

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** True during a background refresh, so the UI can stay visible instead of flashing. */
  refreshing: boolean;
  lastUpdated: Date | null;
  reload: () => void;
}

/**
 * Fetch on mount, then poll. `deps` re-fetches when a filter changes; `refreshKey`
 * forces a refetch from the outside (used by the live socket).
 */
export function useApi<T>(
  fetcher: () => Promise<T>,
  options: { intervalMs?: number; deps?: unknown[]; refreshKey?: number; enabled?: boolean } = {},
): AsyncState<T> {
  const { intervalMs = 0, deps = [], refreshKey = 0, enabled = true } = options;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [manualKey, setManualKey] = useState(0);

  // Keeping the fetcher in a ref lets callers pass an inline arrow function without
  // restarting the polling timer on every render.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const hasData = useRef(false);

  const reload = useCallback(() => setManualKey((key) => key + 1), []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }

    let cancelled = false;

    const run = async () => {
      if (hasData.current) setRefreshing(true);
      else setLoading(true);

      try {
        const result = await fetcherRef.current();
        if (cancelled) return;
        setData(result);
        setError(null);
        setLastUpdated(new Date());
        hasData.current = true;
      } catch (caught) {
        if (cancelled) return;
        const message =
          caught instanceof ApiError
            ? caught.message
            : caught instanceof Error
              ? caught.message
              : 'Request failed';
        setError(message);
      } finally {
        if (!cancelled) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    };

    void run();

    if (intervalMs <= 0) return () => { cancelled = true; };

    const timer = window.setInterval(() => void run(), intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refreshKey, manualKey, enabled, ...deps]);

  return { data, error, loading, refreshing, lastUpdated, reload };
}

/** Tracks whether the browser tab is visible, so we can stop polling in the background. */
export function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible');

  useEffect(() => {
    const handler = () => setVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', handler);
    return () => document.removeEventListener('visibilitychange', handler);
  }, []);

  return visible;
}
