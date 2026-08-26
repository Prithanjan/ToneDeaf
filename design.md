# Design — Visual Design System

**Status:** Authoritative for colour, type, spacing, motion, and component appearance.
**Implemented by:** [`pwa/src/styles/tokens.css`](pwa/src/styles/tokens.css) (340 custom properties) and
[`pwa/src/styles/global.css`](pwa/src/styles/global.css).
**Companions:** [prd.md](prd.md) · [architecture.md](architecture.md) · [phases.md](phases.md) · [rules.md](rules.md) · [technical-design.md](technical-design.md) · [memory.md](memory.md)

> **Scope note.** Until 2026-08-26 this filename held the low-level engineering specification. That
> document now lives at [technical-design.md](technical-design.md) with its section numbers unchanged, so
> existing citations of the form "§4.1" still resolve — only the filename moved. See
> [memory.md](memory.md) decision D-13. If you are looking for frame layout, the policy state machine,
> or the gRPC contract, you want that file, not this one.

This document is written **from the CSS, not ahead of it.** Every value below was read out of
`tokens.css`; nothing here describes a colour the stylesheet does not implement. Where the two ever
disagree, the stylesheet is correct and this file is the bug.

---

## 1. What the interface has to accomplish

The product is a *voice integrity control plane*: it converts persistent evidence of synthetic speech
into a **proportionate verification control** before a simulated high-risk voice action completes. It
does not decide whether a person is a fraudster, and it has no authority to refuse anyone.

Almost every visual decision below follows from that one sentence, because the default visual grammar
for "risk software" actively contradicts it. Red-amber-green, warning triangles, and words like *denied*
all encode an accusation and a verdict. This interface has neither to offer. It has four actions —
`continue`, `verify`, `hold`, `escalate` — and the strongest of them still routes a call to a human.

Five constraints are therefore not stylistic preferences, and changing one is a product change:

| # | Constraint | Where it comes from |
|---|---|---|
| V-1 | **`high` is never red.** The risk ramp runs cool-cyan → amber → violet → magenta and never enters the stop/danger axis | Red is culturally *stop* and *blame*. `high` means "the evidence threshold was met", and the resulting action is a pause or a hand-off |
| V-2 | **Absence of evidence must look like absence, never like low risk.** Ineligible windows render as a hatched stub, not a short bar | `rules.md` R-09. A dropped or codec-degraded window rendered as a short green bar is an affirmative claim the system never made |
| V-3 | **`continue` is the quietest thing on screen.** It is desaturated steel with no emphasis of any kind | `continue` is the *absence* of a finding. Styling it as a positive result would invite reading it as a clearance |
| V-4 | **The closed action vocabulary is visible nowhere but as itself.** `approve` and `deny` appear in no string, class name, or token name; the only action values that exist are `continue`, `verify`, `hold`, `escalate` | `rules.md` R-07, quoted exactly — the rule names those two tokens and no others. Partially asserted mechanically (`pwa-ci.yml:120`, quoted values in the built bundle); the rest is a review obligation. **See §10 item 2 before adding tokens to this list** — a longer list was tried, and `block` alone appears 29 times in these stylesheets as CSS logical-property grammar |
| V-5 | **Colour is never the only channel.** Every state also carries position, shape, or text | A projector at low contrast, greyscale print, and forced-colors mode all destroy the fills |

---

## 2. Colour

### 2.1 Structure: three layers, not one

Tokens are layered so a theme swap is a re-alias rather than a rewrite:

```
--vi-p-d-*  /  --vi-p-l-*        raw palette, dark and light. Never referenced by a component.
--vi-*                            semantic aliases (--vi-surface, --vi-state-high-fg, …). What
                                  components use, and the only layer they are allowed to touch.
--vi-hatch-ineligible, …          composed values, derived from the aliases so they re-resolve per theme.
```

**Dark is the default**, not a preference: the demo runs on a projector in a lit room, where a
full-white surface blooms and destroys the fine distinctions in the timeline. Light is provided via
`prefers-color-scheme: light` for the analyst handset in daylight.

### 2.2 Neutrals

| Semantic | Dark | Light |
|---|---|---|
| `--vi-bg` | `#0a0e14` | `#eef1f6` |
| `--vi-surface` | `#111823` | `#ffffff` |
| `--vi-surface-raised` | `#18212e` | `#ffffff` |
| `--vi-border` | `#2e3a4a` | `#d3dae4` |
| `--vi-border-strong` | `#62738a` | `#6e7c8c` |
| `--vi-text` | `#e9eef6` | `#131a24` |
| `--vi-text-muted` | `#aab8c9` | `#4a5867` |
| `--vi-text-subtle` | `#8492a4` | `#5d6a78` |
| `--vi-accent` | `#4fa3cc` | `#1a6c90` |
| `--vi-focus` | `#7fd8ff` | `#0b5d7e` |

On dark, elevation is carried by the `surface` → `surface-raised` luminance step rather than by shadow:
a dark shadow on a near-black background is invisible, so the shadow tokens exist for the light theme
and contribute almost nothing on dark. `--vi-text-subtle` is deliberately *lighter* than `--vi-text-muted`
on dark and *darker* on light — the names describe emphasis, not luminance.

### 2.3 The risk ramp — `risk_state`

Three states plus the eligibility escape hatch. The hue sequence is chosen so that no step reads as an
accusation, and so that the ramp remains ordered under greyscale.

| State | Meaning | Hue | `-bg` / `-fg` / `-border` (dark) | (light) |
|---|---|---|---|---|
| `collecting` | Evidence is accumulating; **no finding exists yet** | cool cyan | `#10222e` / `#79d2f2` / `#3e90b8` | `#e6f3fb` / `#0f5273` / `#2e7ea8` |
| `uncertain` | A **reportable finding**, not a loading state | amber | `#2a2010` / `#f4c56a` / `#ad8433` | `#fdf3df` / `#6b4a05` / `#96741a` |
| `high` | The k-of-n evidence threshold was met | violet | `#221733` / `#c6a8ff` / `#7f67cc` | `#f1eafe` / `#4a2a87` / `#7255b8` |
| `ineligible` | **No evidence** — window dropped or quality-flagged | neutral | `#161c25` / `#8b99ab` / `#5e6e80` | `#edf0f5` / `#56626f` / `#77838f` |

Two of these are load-bearing:

* **`uncertain` is amber, and amber must not read as "loading".** It is a finding the operator should
  act on. It gets the same weight and the same border treatment as `high`, differing only in hue and in
  the action it maps to. Nothing in the interface uses amber for a pending or in-flight condition —
  in-flight uses the neutral accent.
* **`ineligible` is chromatically outside the ramp.** It is a grey, it is hatched, and it never
  participates in the ordered sequence. This is V-2 expressed in the palette: a viewer scanning the
  timeline must not be able to mistake it for a low reading.

### 2.4 The action ramp — `action`

Actions inherit their hue from the state that usually produces them, so that a viewer learns one
mapping rather than two.

| Action | Inherits from | Hue | `-bg` / `-fg` / `-border` (dark) |
|---|---|---|---|
| `continue` | — | desaturated steel | `#12202b` / `#9fb6cb` / `#5c7c93` |
| `verify` | `uncertain` | amber | `#2a2010` / `#f4c56a` / `#b48a34` |
| `hold` | `high` | violet | `#221733` / `#c9aeff` / `#8570d2` |
| `escalate` | — | magenta | `#2b1224` / `#f49bc6` / `#c55c93` |

`escalate` is the only action with a hue of its own. It is the top of the attention ramp and still
sits off the red axis, because escalating routes a call to a human reviewer — it is a hand-off, not a
refusal. `continue` is intentionally the lowest-contrast fill in the entire system (V-3).

### 2.5 System faults are not risk

A dropped window, an unavailable Scorer, or a lost socket renders in **neutrals only**
(`--vi-fault-bg` / `-fg` / `-border`), chromatically outside the evidence ramp. A transport failure
styled in amber would be indistinguishable from an `uncertain` finding, which would let an outage read
as a detection. Related: `--vi-stale-opacity` dims a rendered decision once the stream is no longer
live, so a `hold` banner left on screen after the call ends cannot be mistaken for a live state.

### 2.6 Contrast

Body text, labels, and every `-fg` on its paired `-bg` are specified to clear **WCAG 2.2 AA** (4.5:1 for
text, 3:1 for borders and large text) in both themes. The `-fg` values were picked against their own
`-bg`, not against `--vi-surface`, so a chip keeps its contrast when it sits on a raised card. Under
`prefers-contrast: more`, borders go to `--vi-border-width-strong` and the hatch pitch tightens — the
palette does not change, so the states stay recognisable to someone who has learned them.

---

## 3. Typography

### 3.1 Stacks

System fonts only. There is no webfont and no CDN link anywhere in the PWA: a font request is a
third-party dependency on the critical path of a live demo, and a network stall would swap the
projector's type mid-presentation.

```css
--vi-font-sans: ui-sans-serif, system-ui, -apple-system, 'Segoe UI Variable Text', 'Segoe UI',
                Roboto, 'Helvetica Neue', Arial, 'Noto Sans', 'Liberation Sans', sans-serif;
--vi-font-mono: ui-monospace, 'SF Mono', SFMono-Regular, 'Cascadia Mono', 'Cascadia Code', Menlo,
                Consolas, 'Liberation Mono', 'Roboto Mono', 'Noto Sans Mono', 'DejaVu Sans Mono', monospace;
```

The order is set so the three real targets — Windows 11 demo laptop, Android Chromium handset, iOS
Safari — each land on a genuine UI face rather than a synthesised fallback.

### 3.2 Numerals

```css
--vi-font-feature-numeric: 'tnum' 1, 'lnum' 1, 'zero' 1;
```

Applied to every numeric readout, and each of the three does a specific job:

* `tnum` (tabular figures) fixes the advance width, so a `spoof_risk` value updating roughly four times
  a second does not reflow its own container — the number changes without the layout twitching.
* `lnum` prevents old-style figures on faces that default to them.
* `zero` slashes the zero where the face supports it, so `0` and `O` cannot be confused in a 64-hex
  `call_ref` being read aloud from a projector.

### 3.3 Scale and weight

| Token | Size | Used for |
|---|---|---|
| `--vi-text-xs` | 12px | reason codes, version strings, axis ticks |
| `--vi-text-sm` | 14px | labels, mono metadata |
| `--vi-text-base` | 16px | body — **and the input floor**: iOS Safari zooms the viewport on focus below 16px |
| `--vi-text-lg` | 18px | card titles |
| `--vi-text-xl` | 22px | screen titles |
| `--vi-text-2xl` | 28px | the action label, handset |
| `--vi-text-3xl` | 40px | the action label, projector layout (≥1024px) |

Line height: `--vi-leading-tight` 1.15 (headings, action label), `--vi-leading-normal` 1.45 (UI text),
`--vi-leading-relaxed` 1.65 — reserved for consent prose, the one place in this interface where someone
actually reads sentences rather than scanning.

Weights stop at 600 (`regular` 400, `medium` 500, `semibold` 600). **There is no 700.** Above 600 the
Windows and Android fallback faces are commonly synthesised, and synthetic bold on a projector turns
into a smear that costs more legibility than the emphasis is worth.

Tracking is applied in exactly two places: `--vi-tracking-tight` (−0.011em) at `--vi-text-2xl` and
above, and `--vi-tracking-wide` (0.02em) on mono codes at `--vi-text-xs`, where tight default spacing
makes a hex string hard to segment by eye.

---

## 4. Space, radius, elevation, motion

**Space** is a 4px base: `--vi-space-1` 4px through `--vi-space-8` 48px (4, 8, 12, 16, 20, 24, 32, 48).
No component uses an off-scale margin.

**Radius**: `sm` 4px (timeline cells, chips), `md` 8px (inputs, buttons), `lg` 14px (cards, banner),
`full` 999px. The ramp is deliberately shallow — a heavily rounded evidence display reads as
consumer-friendly, which is the wrong register for an audit surface.

**Elevation**: two shadows only (`--vi-shadow-sm`, `--vi-shadow-md`), both effectively light-theme
tokens as noted in §2.2.

**Motion**: `--vi-dur-fast` 120ms, `--vi-dur-base` 200ms, `--vi-dur-slow` 320ms, with
`--vi-ease-out` `cubic-bezier(0.2, 0.8, 0.24, 1)` for entrances and `--vi-ease-spring`
`cubic-bezier(0.34, 1.32, 0.64, 1)` reserved for the action banner's arrival.

Two rules govern motion, both for the same reason — the timeline is an evidence display and animation
can misrepresent evidence:

1. **Nothing loops.** No pulse, no shimmer, no breathing. A looping animation on a risk indicator
   reads as ongoing activity and would keep drawing attention to a finding that has already settled.
2. **A new window's cell appears; it does not slide the row.** Re-animating the whole timeline on every
   hop would make the history look like it changed when only one cell was appended.

Under `prefers-reduced-motion: reduce`, transitions and animations are reduced to near-zero duration
rather than removed, so state changes still register as changes.

---

## 5. Components

### 5.1 `ActionBanner` — the decision surface

The single most important element. It renders the current `action`, its `reason_code`, and the evidence
counts behind it.

* **Four redundant channels**, because the fill is the least reliable one: a **left rail**
  (`--vi-rail-w` 4px, via `border-inline-start`), a **glyph**, the **label text**, and the fill.
  Under greyscale print or `forced-colors: active` the four action fills collapse to the same thing;
  rail, glyph, and label survive. This is V-5 made concrete.
* `border-inline-start`, not `border-left` — logical properties throughout, so an RTL locale does not
  put the rail on the wrong edge.
* Minimum height `--vi-banner-min-h` 96px on the handset, 128px at the projector breakpoint. The
  banner never reflows when the action changes, so a `continue` → `hold` transition does not shift the
  page under the operator's eye.
* The action label is `--vi-text-2xl` / `3xl` with `--vi-tracking-tight`.
* The reason code is mono, `--vi-text-xs`, `uppercase`, `--vi-tracking-wide` — visually marked as a
  machine-readable identifier rather than prose, because it is one.
* Sticky-`high` and its escalation get **their own rail** rather than sitting inside body text, so the
  stickiness is a structural fact on screen and not a sentence someone has to read.

### 5.2 `RiskTimeline` — the evidence display

* **Ordinal, never time-scaled.** Cells are laid out by `window_seq` at a fixed
  `--vi-cell-w` 18px / `--vi-cell-gap` 4px. Scaling by wall-clock time would make a network stall look
  like a gap in the evidence, and the evidence sequence is contiguous by construction — the Gateway
  closes the stream on a sequence gap rather than admitting one.
* Height `--vi-timeline-h` 120px.
* **Ineligible windows are a hatched stub, not a short bar** (`--vi-hatch-ineligible`, pitch
  `--vi-hatch-gap` 4px, line `--vi-hatch-line` 1px). This is V-2. A short bar is a low reading; a hatch
  is a hole. Under `prefers-contrast: more` the pitch tightens; in print the hatch flattens but stays
  distinguishable.
* No cell animates on arrival beyond a fade at `--vi-dur-fast`.

### 5.3 `ConsentNotice`

The one text-heavy surface. Prose is capped at `--vi-measure` 34rem and set at
`--vi-leading-relaxed`, because it is meant to be read rather than scanned. It states plainly that no
raw audio is retained — the claim the rest of the architecture exists to make true — and it is the one
place where longer sentences are correct.

### 5.4 `SessionSetup`

Inputs at `--vi-text-base` (see §3.3 — below 16px iOS zooms), `--vi-radius-md`, and every interactive
target at least `--vi-touch-min` 44px on the handset. `purpose_code` and `context_value_band` are
closed selects, never free text: a free-text purpose field is both unauditable and a place a real
amount or a customer name could be typed into an audit trail.

---

## 6. Focus and keyboard

Focus is `--vi-focus-width` 2px at `--vi-focus-offset` 2px in `--vi-focus`. **The offset is
load-bearing**, not cosmetic: it places the ring against the page rather than flush against the
control, so the ring stays visible on a control whose own border is already `--vi-border-strong`.

`:focus-visible` is used rather than `:focus`, so a pointer user does not get a ring on click while a
keyboard user always does. No element removes the outline without replacing it.

---

## 7. Layout

Two layouts, one breakpoint at 1024px:

* **Handset** (default, single column): capped at `--vi-content-max` 40rem. Vertical order is
  banner → timeline → metadata, so the decision is above the fold on a phone.
* **Projector** (≥1024px): the banner grows to 128px and the action label to `--vi-text-3xl`. The
  timeline widens; cells do not — a wider viewport shows *more windows*, not fatter ones, so the
  timeline reads the same from the back of a room as it does on a phone.

---

## 8. Accessibility beyond colour

Implemented in `global.css`:

| Query | Behaviour |
|---|---|
| `prefers-reduced-motion: reduce` | durations collapse toward zero; no animation is merely disabled halfway |
| `prefers-contrast: more` | borders to `--vi-border-width-strong`; hatch pitch tightens; palette unchanged |
| `prefers-reduced-transparency: reduce` | translucent surfaces become opaque |
| `forced-colors: active` | fills yield to system colours; rail, glyph, and label carry the state (V-5) |
| `print` | dark theme is not printed; hatch flattens; the banner keeps its rail |

The `forced-colors` and `print` blocks are the real test of V-5. If the interface is still readable with
every fill removed, colour was genuinely redundant. If it is not, the fill was doing work it should not
have been doing.

---

## 9. Deliberately absent

Not oversights — each of these was considered and left out:

* **No red anywhere.** See V-1.
* **No warning triangles, shields, padlocks, or sirens.** Iconography from the security-alert genre
  imports an accusation the system does not make.
* **No percentage presented as a confidence.** `spoof_risk` is a calibrated score in [0,1] rendered to
  4 decimal places in mono; it is never dressed up as "87% likely fake", which would claim a precision
  the current placeholder calibration does not have.
* **No score gauge, dial, or speedometer.** These imply a continuously meaningful reading. The policy
  decision is a k-of-n evidence rule over discrete windows, and the timeline shows exactly that.
* **No dark/light toggle.** `prefers-color-scheme` only. A manual toggle is state to persist,
  synchronise, and get wrong on the projector.
* **No webfont, no icon font, no CSS framework.** Every byte of style in the PWA is in these two files.

---

## 10. The token contract

Three rules, and the first two are mechanically checkable:

1. **Components reference semantic aliases only** — never `--vi-p-d-*` / `--vi-p-l-*`, and never a
   literal colour. Verified: there are **zero** hex literals in any component stylesheet or in
   `App.module.css`.
2. **`approve` and `deny` appear in no token name, class name, or string.** Verified by grep over
   `styles/tokens.css`, `styles/global.css`, `App.module.css` and all five component stylesheets: **zero
   hits**, case-insensitive. That is the whole of `rules.md` R-07, quoted exactly — *"`approve` and `deny`
   must not exist in any enum, config value, database CHECK constraint, or API schema."*

   ⚠️ **An earlier draft of this rule listed five tokens — `approve`, `deny`, `allow`, `block`, `reject` —
   and claimed all five were absent. That claim was false, and it is worth recording why rather than
   quietly deleting it.** `block` appears **29 times** across these stylesheets, because CSS logical
   properties are named `block-size`, `margin-block-start`, `border-block-end`, and `display: block`.
   There is no way to write modern CSS without it. So the stricter rule was not merely broader than R-07 —
   it was **unsatisfiable in principle for any stylesheet**, and stating it as verified made this document
   assert something a single grep refutes.

   **Do not "restore" the longer list.** It reads like a tightening and is actually three false positives:
   `block` is CSS grammar, `allow` is unavoidable in web platform vocabulary (`allow` attributes,
   allowlists), and `reject` is the *correct* word for refusing malformed input — `rules.md:320` itself
   writes *"Wrong-shaped input is **rejected**, never coerced."* A rule that cannot hold gets suppressed
   rather than fixed, and suppressing it takes the two tokens that matter down with it.

   **What is actually enforced mechanically, and where it stops.** `.github/workflows/pwa-ci.yml:120`
   greps the **built bundle** for `approve`/`deny` as *quoted string values* — `['\"](approve|deny)['\"]`
   — over `dist/**/*.js` and `*.map`. That is a deliberately narrow check on the surface that matters: an
   action *value* the client could render or send. It does **not** cover CSS class names, token names, or
   unquoted identifiers, so item 2's full scope is a review obligation, not a gate. The server-side
   counterpart is `gateway/tests/test_ws_negative_contract.py::TestCloseCodeTables`, which asserts the
   same vocabulary over WebSocket close reasons and **caught a real violation on its first run**
   (`memory.md` BUG-5) — which is the evidence that this class of check earns its place.
3. **A new state or action needs a token triple** (`-bg`, `-fg`, `-border`) in **both** themes before it
   may be referenced. A component that falls back to `--vi-text` for a missing state renders something
   plausible and wrong, which is worse than failing to render.

Current inventory: **340** `--vi-*` custom properties in `tokens.css`.

---

## 11. Future scope

Explicitly out of scope for Phase 1, recorded here so the tokens are not mistaken for an unfinished
system: a per-tenant brand layer (the alias layer in §2.1 is the seam it would attach to), a printable
evidence report styled from the audit trail rather than the live view, and localisation beyond the
logical-property groundwork already in place.
