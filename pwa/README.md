# `pwa/` — operator client

React 19 + Vite 8 + TypeScript, strict. This is the surface an operator sees: it acknowledges consent,
binds a session, captures microphone audio, streams 20 ms frames to the Gateway, and renders the window
evidence and the control step that come back.

It does not score. There is no VAD, no windowing, no threshold, and no risk arithmetic in this package —
those live in the Scorer and the Gateway's policy engine, and duplicating any of them here would create
a second, uncalibrated opinion whose disagreements with `policy/thresholds.yaml` nobody could explain.
The client produces frames and draws what it is told.

---

## Run it

```bash
cd pwa
npm install
npm run dev          # http://localhost:5173, proxying /api and /ws to 127.0.0.1:8000
```

| script | what it does |
| --- | --- |
| `npm run dev` | Vite dev server on 5173, `strictPort` |
| `npm run typecheck` | `tsc --noEmit` over both projects (app, and `vite.config.ts`) |
| `npm run build` | typecheck, then `vite build` into `dist/` |
| `npm run preview` | serve `dist/` on 4173 |
| `npm run lint` | ESLint, type-aware |

`npm run build` runs the typecheck first on purpose: a build that emits while the types are broken is a
build that ships the broken thing.

### Configuration

Every variable is optional and none of them holds a secret (rules.md R-34). `import.meta.env` values are
compiled into the bundle and are readable by anyone with the page open, so a key here would be a public
key.

| variable | default | purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | the serving origin | Gateway base. Leave unset in dev to use the proxy below. |
| `VITE_AUTH_MODE` | `local-test-issuer` | `local-test-issuer` \| `cognito-srp`. The Cognito path is not built and says so. |
| `VITE_TEST_ISSUER_TOKEN_URL` | — | Local demo issuer's token endpoint. Without it, sign-in fails with a sentence saying it is unconfigured. |
| `VITE_TEST_ISSUER_SUBJECT` | `demo-operator` | Subject claim to request from the demo issuer. |
| `VITE_JWT_AUDIENCE` | `sih26104-local` | Audience the Gateway expects. |

Put them in `pwa/.env` (gitignored). The WSS URL is **derived** from `VITE_API_BASE_URL` — `http:` →
`ws:`, anything else → `wss:` — and is deliberately not separately configurable, because a hand-written
second variable is exactly where an `ws://`-against-`https://` mixed-content refusal gets introduced.

### The dev proxy, and when to skip it

With `VITE_API_BASE_URL` unset the browser talks to its own origin and Vite proxies `/api` and `/ws` to
`127.0.0.1:8000`. That mirrors both real deployments (CloudFront → ALB; Caddy → uvicorn), and it keeps
the `Origin` header on the WSS handshake equal to the site the operator is on, which is the value the
Gateway's origin permit list is written against.

The port is a convention — nothing in the repo pins the Gateway's dev port — so change
`GATEWAY_DEV_ORIGIN` in `vite.config.ts` if yours differs.

One thing to know if the handshake fails in dev: the stream ticket travels in the
`Sec-WebSocket-Protocol` header, not the URL. If a proxy in the path drops or rewrites subprotocols the
Gateway will close with `AUTH_TICKET_MISSING`, which reads like an auth bug and is a transport bug. Set
`VITE_API_BASE_URL` to the Gateway directly to rule it out.

---

## Layout

```
src/
  main.tsx                    entry; imports tokens.css then global.css; registers no service worker
  App.tsx                     phase machine: consent → setup → live → ended
  lib/
    constants.ts              NOT OURS. Mirrored wire contract, cross-language parity test. Import from it.
    types.ts                  closed enums + pure runtime validators for every server event
    auth.ts                   access token, in memory only
    api.ts                    POST /sessions, POST /stream-ticket, GET /version
    capture.ts                getUserMedia → 320-sample int16 frames. Nothing else imports getUserMedia.
    stream.ts                 WSS, 648-byte frames, sequence, backoff, backpressure
  components/
    ConsentNotice.tsx         the gate
    SessionSetup.tsx          call ref + purpose + value band
    RiskTimeline.tsx          per-window evidence
    ActionBanner.tsx          the control step
  styles/
    tokens.css                NOT OURS. Design system.
    global.css                NOT OURS. Design system.
```

Each component has a colocated `*.module.css`. `global.css` states that components own their own CSS, and
a CSS module cannot collide with another file's selectors, so there is no shared stylesheet to contend
over.

### The wire format, in one paragraph

A binary frame is exactly `WS_FRAME_BYTES` (648) — an 8-byte `uint64` sequence prefix followed by 640
bytes of PCM, which is 320 `int16` samples at 16 kHz mono, 20 ms. **The prefix is big-endian and the PCM
is little-endian.** They disagree deliberately, and `lib/stream.ts` writes them with
`view.setBigUint64(0, seq)` (no third argument — big-endian is the `DataView` default) and
`view.setInt16(off, sample, true)` (`true` for little-endian). Getting these the same way round turns
frame 1 into sequence 72057594037927936 and the Gateway closes the stream on frame 2. Frames are never
padded, the sequence advances strictly by one, and a dropped frame does **not** consume a sequence
number — to the Gateway a gap and a duplicate are the same `PROTO_SEQUENCE` error, so "helpfully"
skipping a number to record a loss closes the stream. Every one of those numbers is imported from
`lib/constants.ts`; none is written as a literal anywhere else (rules.md R-23).

Reconnect resets the sequence counter to zero. A resumed session is a new stream, not a spliced one
(technical-design.md §6).

---

## Deliberate decisions that look like omissions

**`ScriptProcessorNode`, not `AudioWorklet`.** `lib/capture.ts` uses the deprecated main-thread API on
purpose. `AudioWorklet` is an explicit Future Scope item, and the file header carries the full reasoning
and the pointers. There is no "temporary" worklet path and none should be added opportunistically. The
deprecation warnings are silenced in one scoped override in `eslint.config.js` — a single reviewable
exemption with a single reason, which is the override to delete when the worklet is actually built.

**No service worker, and the app is not installable.** There is a `manifest.webmanifest` because the
manifest is where the app's name and scope belong, but it declares `"display": "browser"` and ships no
icons, so no browser will offer to install it. That is accurate: there is no offline shell, and claiming
installability for something that cannot work offline is the overclaim rules.md R-01 forbids. A caching
worker is also a standing hazard to the two absences below, and this app has no cache-shaped problem to
justify one. If one is ever added, it is scoped to the app shell — HTML, JS, CSS, manifest — and to
nothing served from `/api/` or `/ws/`.

**No `@vitejs/plugin-react`.** Vite's transform reads `jsx: "react-jsx"` from `tsconfig.json` and
compiles TSX unaided; the plugin's only contribution here would be Fast Refresh. The cost is that
editing a component full-reloads the page, which resets to the consent screen and closes the microphone.
That is a mildly annoying dev loop and a completely accurate one, and it keeps the dependency list to
React, Vite, TypeScript, and the linter.

**No test runner in this package.** There is none in `pwa/` yet and adding one was outside this
package's scope. The frame encoder was verified by an out-of-tree harness instead — see *Verification*
below — and `lib/stream.ts`'s `encodeFrame` and `backoffDelayMs` are exported as pure functions
specifically so that a real suite can assert against them without a socket.

---

## Two absences that are load-bearing

Both are enforced by `eslint.config.js`, not by everyone remembering. There is no `eslint-disable` for
either rule anywhere in `src/`.

**No audio at rest, anywhere on the client (rules.md R-14).** Frames go from the capture callback to
`socket.send` and are dropped. No `IndexedDB`, no `localStorage`, no `Blob`, no object URL, no cache. The
lint config makes `localStorage`, `sessionStorage`, `indexedDB`, `caches`, `new Blob`,
`URL.createObjectURL`, `navigator.serviceWorker`, and `document.cookie` build failures with a rule ID in
the message.

**The raw call reference never leaves component state (rules.md R-16).** The operator types it, it goes
into one request body over TLS, and the Gateway returns an HMAC pseudonym that everything downstream uses
instead. It is never a query parameter, never a path segment, never in web storage, and never logged —
`no-console` is an error in this package, because the one field that must never reach a log is exactly
what an added `console.log(values)` in a submit handler would capture. `api.ts` asserts the returned
`call_ref` matches `^[0-9a-f]{64}$`, which is the check that would catch a Gateway echoing the raw value
back.

Related: **server text never reaches the DOM (rules.md R-17).** Every message a person sees is chosen
from a static table in the client, keyed by an error *code* validated against a known set. Response
`message` fields are read for nothing. `App.tsx` only renders `error.message` for four error classes it
wrote itself, matched by `name` — and matched by `name` rather than `instanceof` because importing
`CaptureError` as a value would load `lib/capture.ts` at startup and put the `getUserMedia` call site in
memory before consent.

---

## Honesty properties of the UI

These are requirements, not polish. Each one is a specific thing the interface refuses to draw.

- **The action vocabulary is closed**: `continue | verify | hold | escalate`, typed as a union of four
  literals so a fifth value is a compile error (rules.md R-07). No icon carries a verdict either — no
  tick, no cross — because that smuggles the vocabulary back in through the icon set.
- **Before the first decision there is no progress bar.** The first window needs 2.56 s of *voiced*
  audio, which is more than 2.56 s of wall clock by an amount that depends on how much the caller talks.
  A determinate countdown would be a promise the system cannot keep.
- **`uncertain` is a finding, not a loading state.** It means eligible windows were examined and too few
  were high. It is styled and worded as a reportable result.
- **Voicing-ineligible windows are visually distinct from low-risk ones** (rules.md R-09): a hatched stub
  of *fixed* height with no score shown. Drawing an unmeasured window at a height derived from a score
  would invent a measurement out of a refusal to measure.
- **`high` is sticky** for the session (rules.md R-13). Evidence does not expire because a later window
  looked clean, and the banner says that clearing it needs a human resolution step which is backlog.
- **State is never colour alone.** Every state renders a label, a sentence, and a distinct glyph shape,
  so it survives greyscale and Windows forced-colors mode.
- **The score is a fixed-point number, never a percentage.** `spoof_risk` renders to four decimal places
  in mono (design.md §9) — the same four on screen and in the accessible name, because a reader who
  hears `0.512` while the projector shows `0.5118` has been handed two numbers for one window. It is
  never dressed up as "87% likely fake", which would claim a precision the placeholder calibration does
  not have.
- **k and n are shown only as the server reported them.** The evidence bar lives in
  `policy/thresholds.yaml`; restating it as "3 of 5" in the client would be a second definition of a
  policy constant that goes stale silently the day the bar moves.
- **Mock mode and artifact state are loud** wherever they appear (rules.md R-46, R-11). A
  `MOCK_SMOKE_MODE_NOT_A_DETECTOR` badge that only shows up after a stream opens has already missed the
  screenshot, so the parity set is fetched before anything else.

All colours, sizes, spacing, and durations come from the `--vi-*` custom properties in
`styles/tokens.css`. Nothing in this package hardcodes a colour or a font size. Numeric readouts use
`--vi-font-mono` with tabular figures so a digit does not shift horizontally every 640 ms.

---

## Verification

What was actually run, on Windows 11 with Node v24.12.0 and npm 11.6.2:

```
npm install                        → added 127 packages, exit 0
npx tsc --noEmit -p tsconfig.json  → exit 0, no diagnostics
npx tsc --noEmit -p tsconfig.node.json → exit 0, no diagnostics
npx eslint .                       → exit 0, 0 problems
npm run build                      → exit 0, 33 modules, dist/ written
```

Installed and pinned: `react@19.2.8`, `react-dom@19.2.8`, `vite@8.2.2`, `typescript@5.9.3`.

The build output confirms two structural claims that are otherwise only assertions:

- `getUserMedia` appears **only** in the separate `assets/capture-*.js` chunk, which is dynamically
  imported after consent and is not preloaded from `index.html`. The consent gate is structural.
- `dist/index.html` links exactly one module script, one stylesheet, and the manifest. No CDN.

The frame encoder was checked byte-for-byte by an out-of-tree harness — `lib/stream.ts` bundled with
`rolldown` into the OS temp directory and driven by Node, with the expected values written by hand rather
than imported, so the test could not pass by agreeing with itself:

```
frame is exactly 648 bytes                                          PASS
seq prefix is 8 bytes big-endian: 0,0,0,0,0,0,0,1                   PASS
DataView.getBigUint64(0) (BE default) reads back 1n                 PASS
reading the same prefix little-endian gives 72057594037927936n      PASS
PCM sample 0 is little-endian: 0x34 then 0x12                       PASS
PCM sample 1 (-2) is 0xFE 0xFF little-endian                        PASS
PCM sample 319 (0x7FFF) lands in the last two bytes                 PASS
payload is 640 bytes after the prefix                               PASS
a 319-sample frame throws FrameError rather than being padded       PASS
a negative sequence throws FrameError                               PASS
seq 2^64-1 encodes                                                  PASS
frame 0 and frame 1 differ only in the prefix                       PASS
backoff with random()=0 is 0 ms (full jitter, not equal jitter)     PASS
backoff ceiling grows then saturates at the cap                     PASS
```

Also checked mechanically: every `styles.*` reference in all five components resolves to a class that
exists in the matching `*.module.css` (and every defined class is used — 15/15, 11/11, 23/23, 8/8,
10/10); all **73** `var(--vi-*)` references across the component stylesheets resolve to a custom property
that `tokens.css` actually defines, with none missing; every `vi-*` utility class and `data-vi-*`
attribute the components rely on exists in the design system; and none of `648`, `640`, `320`, `40960`,
`81920`, `2560`, `16000` appears as a code literal outside `lib/constants.ts` — the only match is one
prose comment explaining where a byte stride comes from.

One property in these stylesheets is physical rather than logical, and it has to be: `global.css` caps
every `p` at `--vi-measure` with `max-width`, so the seven places a paragraph needs to span its container
reset `max-width`, not `max-inline-size`. A logical reset there sets a different property and the clamp
silently survives — it was written the wrong way round first and the paragraphs were quietly still capped.

**Not verified:** nothing here has been rendered in a browser or run against a live Gateway. There is no
end-to-end evidence that the handshake succeeds, that the `session.open` frame is accepted, that
`ScriptProcessorNode` delivers 16 kHz mono on the target machines, or that the timeline looks right. The
float32→int16 clamp in `capture.ts` is correct by inspection — it clamps in the float domain and again in
the integer domain, because `DataView.setInt16` stores modulo 2^16 rather than saturating, so an
unclamped cast wraps a full-scale positive sample to full-scale negative in exactly the frames where
someone is speaking most clearly — but it is module-private and was not executed.
