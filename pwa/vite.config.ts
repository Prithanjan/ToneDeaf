/**
 * Vite configuration.
 *
 * Three decisions worth writing down, because each looks like an omission:
 *
 * **No `@vitejs/plugin-react`.** Vite's own transform pipeline reads `jsx: "react-jsx"` from
 * `tsconfig.json` and compiles TSX without help, so the plugin's only contribution here would be React
 * Fast Refresh. The cost of not having it is that editing a component full-reloads the page, which
 * resets the app to the consent screen and closes the microphone. That is a mildly annoying dev loop
 * and a completely accurate one — and it keeps the dependency list to React, Vite, and TypeScript,
 * with no fourth package whose compatibility with this Vite line has to be re-checked at every bump.
 *
 * **No PWA/service-worker plugin.** Nothing here generates a worker. See `src/main.tsx`: an app with
 * no offline shell must not advertise one (rules.md R-01), and a caching worker is a standing hazard to
 * the no-audio-and-no-identifiers-at-rest guarantee (rules.md R-14, R-16).
 *
 * **The dev proxy exists so the browser stays same-origin.** With no `VITE_API_BASE_URL`,
 * `lib/api.ts` targets `window.location.origin`, so `/api` and `/ws` are proxied to a locally running
 * Gateway. That mirrors both real deployments — CloudFront in front of the ALB, Caddy in front of
 * uvicorn — where the PWA and the API share an origin. It is also the only arrangement in which the
 * `Origin` header on the WSS handshake equals the site the operator is actually on, which is the value
 * the Gateway's origin permit list is written against (technical-design.md §2.1). Setting
 * `VITE_API_BASE_URL` bypasses this proxy entirely and makes the handshake cross-origin, so the
 * Gateway has to be configured to expect it.
 */

import { defineConfig } from 'vite';

/**
 * Where a locally running Gateway is assumed to be listening.
 *
 * This port is a convention, not a contract: no document in the repo pins the Gateway's dev port, and
 * nothing in the wire protocol depends on it. 8000 is uvicorn's default. Change it here, or set
 * `VITE_API_BASE_URL` and skip the proxy.
 */
const GATEWAY_DEV_ORIGIN = 'http://127.0.0.1:8080';
const GATEWAY_DEV_WS_ORIGIN = 'ws://127.0.0.1:8080';

export default defineConfig({
  server: {
    port: 5173,
    // Fail rather than drift. A silent hop to 5174 leaves the operator on an origin the Gateway's
    // permit list does not contain, and the resulting handshake refusal looks like an auth bug.
    strictPort: true,
    proxy: {
      '/api': {
        target: GATEWAY_DEV_ORIGIN,
        changeOrigin: false,
      },
      '/ws': {
        target: GATEWAY_DEV_WS_ORIGIN,
        ws: true,
        changeOrigin: false,
      },
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
    proxy: {
      '/api': {
        target: GATEWAY_DEV_ORIGIN,
        changeOrigin: false,
      },
      '/ws': {
        target: GATEWAY_DEV_WS_ORIGIN,
        ws: true,
        changeOrigin: false,
      },
    },
  },
  build: {
    target: 'es2022',
    // Kept on. The client's job in an incident is to be auditable, and a stack trace that points at
    // `lib/stream.ts` instead of a minified chunk is worth more than the bytes. There is nothing
    // sensitive in this bundle to protect: no keys, no secrets (rules.md R-34).
    sourcemap: true,
  },
});
