import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import { LiveProvider } from './api/live';
import './styles.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root is missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    {/* One WebSocket for the whole app, provided above the router. */}
    <LiveProvider>
      <App />
    </LiveProvider>
  </StrictMode>,
);
