/**
 * Application shell: navigation, header, and hash-based routing.
 *
 * Routing is hand-rolled on `location.hash` rather than pulling in a router. The app has
 * seven views and no nested layouts, so a router would be more configuration than code.
 * Using the hash means deep links work and the back button behaves, without any server
 * rewrite rules when the built assets are served statically.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from './api/client';
import { useApi } from './api/hooks';
import { useLive } from './api/live';
import { Badge } from './components/primitives';
import { ServiceTile } from './components/ServiceTile';
import { formatPercent, formatRelative } from './lib/format';
import { statusLabel, statusTone } from './lib/status';
import { AnomaliesPage } from './pages/Anomalies';
import { AutomationPage } from './pages/Automation';
import { ChaosPage } from './pages/Chaos';
import { ForecastPage } from './pages/Forecast';
import { IncidentsPage } from './pages/Incidents';
import { OverviewPage } from './pages/Overview';
import { ServiceDetailPage } from './pages/ServiceDetail';

type PageId = 'overview' | 'services' | 'anomalies' | 'incidents' | 'forecast' | 'automation' | 'chaos';

interface Route {
  page: PageId;
  service?: string;
  incident?: string;
}

const PAGES: { id: PageId; label: string; title: string; subtitle: string }[] = [
  { id: 'overview', label: 'Overview', title: 'Platform overview', subtitle: 'Live health of every monitored DPI service' },
  { id: 'services', label: 'Services', title: 'Services', subtitle: 'Per-service metrics, forecasts and history' },
  { id: 'anomalies', label: 'Anomalies', title: 'Anomaly detection', subtitle: 'Unusual behaviour, and the signals that gave it away' },
  { id: 'incidents', label: 'Incidents', title: 'Incidents and root cause', subtitle: 'Correlated anomalies with a ranked explanation' },
  { id: 'forecast', label: 'Forecast', title: 'Traffic forecasting', subtitle: 'Predicted load and pre-emptive scaling decisions' },
  // Temporarily hidden - healing engine database persist issue
  // { id: 'automation', label: 'Automation', title: 'Self-healing', subtitle: 'Playbooks, safety rails, and every action taken' },
  // { id: 'chaos', label: 'Chaos', title: 'Chaos engineering', subtitle: 'Deliberate failures and how fast the platform responded' },
];

function parseHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, '');
  if (!raw) return { page: 'overview' };

  const [page, ...rest] = raw.split('/');
  const id = PAGES.find((entry) => entry.id === page)?.id;

  if (id === 'services' && rest[0]) return { page: 'services', service: decodeURIComponent(rest[0]) };
  if (id === 'incidents' && rest[0]) return { page: 'incidents', incident: decodeURIComponent(rest[0]) };
  return { page: id ?? 'overview' };
}

function toHash(route: Route): string {
  if (route.page === 'services' && route.service) return `#/services/${encodeURIComponent(route.service)}`;
  if (route.page === 'incidents' && route.incident) return `#/incidents/${encodeURIComponent(route.incident)}`;
  return `#/${route.page}`;
}

export function App() {
  const [route, setRoute] = useState<Route>(parseHash);
  const live = useLive();

  useEffect(() => {
    const handler = () => setRoute(parseHash());
    window.addEventListener('hashchange', handler);
    // Normalise a bare URL to an explicit hash so the first entry in history is a route.
    if (!window.location.hash) window.location.replace('#/overview');
    return () => window.removeEventListener('hashchange', handler);
  }, []);

  const go = useCallback((next: Route) => {
    window.location.hash = toHash(next);
    setRoute(next);
  }, []);

  const overview = useApi(() => api.overview(), {
    intervalMs: 15_000,
    refreshKey: live.counters.metrics + live.counters.incident,
  });
  const config = useApi(() => api.config(), { intervalMs: 120_000 });

  const meta = useMemo(() => PAGES.find((entry) => entry.id === route.page) ?? PAGES[0]!, [route.page]);
  const counts = overview.data?.counts;

  const connectionBadge =
    live.state === 'open' ? (
      <Badge tone="ok" dot pulse title={`${live.eventsReceived} events received`}>
        Live
      </Badge>
    ) : live.state === 'connecting' ? (
      <Badge tone="warn" dot>
        Connecting
      </Badge>
    ) : (
      <Badge tone="crit" dot title="The dashboard is polling; live push is unavailable.">
        Offline
      </Badge>
    );

  return (
    <div className="app">
      <nav className="sidebar" aria-label="Main navigation">
        <div className="brand">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span className="brand__title">AIOPS Platform</span>
            {config.data && !config.data.healing.enabled && <Badge tone="info">dry run</Badge>}
          </div>
          <span className="brand__subtitle">DPI Services</span>
        </div>

        <div className="nav">
          {PAGES.map((page) => {
            const badge =
              page.id === 'incidents' && counts?.open_incidents
                ? { value: counts.open_incidents, alert: true }
                : page.id === 'anomalies' && counts?.anomalies_24h
                  ? { value: counts.anomalies_24h, alert: (counts.critical_anomalies_24h ?? 0) > 0 }
                  : page.id === 'automation' && counts?.actions_24h
                    ? { value: counts.actions_24h, alert: false }
                    : page.id === 'chaos' && counts?.chaos_active
                      ? { value: counts.chaos_active, alert: true }
                      : null;

            return (
              <button
                key={page.id}
                type="button"
                className="nav__item"
                aria-current={route.page === page.id ? 'page' : undefined}
                onClick={() => go({ page: page.id })}
              >
                <span>{page.label}</span>
                {badge && (
                  <span className={`nav__count${badge.alert ? ' nav__count--alert' : ''}`}>{badge.value}</span>
                )}
              </button>
            );
          })}
        </div>

        <div className="stack" style={{ gap: 8, marginTop: 'auto', padding: '0 8px', fontSize: 11, color: 'var(--text-dim)' }}>
          {config.data && (
            <>
              <div>env: {config.data.env}</div>
              <div>scrape: {config.data.scrape_interval_seconds}s</div>
              <div>events: {live.eventsReceived}</div>
            </>
          )}
          {live.state === 'closed' && (
            <button type="button" className="button" onClick={live.reconnect}>
              Reconnect
            </button>
          )}
        </div>
      </nav>

      <header className="header">
        <div className="header__titles">
          <h1>{meta.title}</h1>
          <p>{meta.subtitle}</p>
        </div>
      </header>

      {/* aria-live so a screen reader is told when the view changes after navigation. */}
      <main className="main" aria-live="polite">
        {route.page === 'overview' && (
          <OverviewPage
            onSelectService={(service) => go({ page: 'services', service })}
            onSelectIncident={(incident) => go({ page: 'incidents', incident })}
            onNavigate={(page) => go({ page: page as PageId })}
          />
        )}

        {route.page === 'services' &&
          (route.service ? (
            <ServiceDetailPage service={route.service} onBack={() => go({ page: 'services' })} />
          ) : (
            <ServicesIndex onSelect={(service) => go({ page: 'services', service })} />
          ))}

        {route.page === 'anomalies' && (
          <AnomaliesPage onSelectIncident={(incident) => go({ page: 'incidents', incident })} />
        )}

        {route.page === 'incidents' && (
          <IncidentsPage
            selectedId={route.incident ?? null}
            onSelect={(incident) => go(incident ? { page: 'incidents', incident } : { page: 'incidents' })}
            onSelectService={(service) => go({ page: 'services', service })}
          />
        )}

        {route.page === 'forecast' && <ForecastPage />}
        {route.page === 'automation' && <AutomationPage />}
        {route.page === 'chaos' && (
          <ChaosPage onSelectIncident={(incident) => go({ page: 'incidents', incident })} />
        )}
      </main>
    </div>
  );
}

/** The services index is just the health grid on its own, so it lives here. */
function ServicesIndex({ onSelect }: { onSelect: (service: string) => void }) {
  const live = useLive();
  const services = useApi(() => api.services(), { intervalMs: 10_000, refreshKey: live.counters.metrics });

  return (
    <div className="stack">
      {services.loading && services.data === null && <p className="empty">Loading services…</p>}
      {services.error && services.data === null && <p className="empty">{services.error}</p>}
      {services.data?.length === 0 && (
        <p className="empty">
          No service metrics yet. Seed sample data with <code>python scripts/seed_demo_data.py --follow</code>.
        </p>
      )}
      <div className="grid grid--services">
        {(services.data ?? []).map((service) => (
          <ServiceTile key={service.service} service={service} onSelect={onSelect} />
        ))}
      </div>
    </div>
  );
}
