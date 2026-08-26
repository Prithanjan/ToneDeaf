/**
 * Entry point.
 *
 * Style import order is load-bearing: `tokens.css` defines every `--vi-*` custom property that
 * `global.css` and the component modules consume, and CSS custom properties are not hoisted. Tokens
 * second means every `var(--vi-*)` resolves to its initial value on first paint and the app renders
 * unstyled. `global.css` also `@import`s tokens for the benefit of anyone loading it alone; the
 * explicit import here is the one that guarantees the order in the bundle graph.
 *
 * There is NO service worker registration, deliberately. This directory is called `pwa/` and the app
 * is not installable: there are no icons and no offline shell, so registering a worker would produce
 * an install prompt for something that cannot work offline — the kind of target-presented-as-complete
 * that rules.md R-01 forbids. It is also the safer default for rules.md R-14: a worker that caches
 * responses is one configuration mistake away from caching a response containing a call reference, and
 * it must never cache the WebSocket or any audio. If a worker is added later, it is scoped to the app
 * shell — HTML, JS, CSS, manifest — and to nothing served from `/api/` or `/ws/`.
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles/tokens.css';
import './styles/global.css';

const container = document.getElementById('root');
if (container === null) {
  // A loud, immediate failure. The alternative — a silent no-op — presents as a working deployment
  // serving a blank page, which is the worst thing to debug five minutes before a demo.
  throw new Error('#root is missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
