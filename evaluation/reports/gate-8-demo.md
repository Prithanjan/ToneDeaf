# Gate 8 — Demo (TEMPLATE — NOT YET RUN)

| | |
|---|---|
| **Status** | `not-run` |
| **Blocking** | Two-tier claims. Not a deploy or release blocker on its own. |
| **Owner (playbook §6.1)** | Team lead |
| **Pass condition (playbook §6.1, verbatim)** | AWS GPU and local CPU run same test trace with recorded latency |
| **Failure response (playbook §6.1, verbatim)** | Fix parity or present one tier only, truthfully |

Note the failure response: **presenting one tier is a legitimate outcome.** The thing that is not
permitted is presenting two tiers as equivalent when only one was measured, or quoting a GPU latency
while running on the laptop. "We measured the GPU tier; the fallback tier is untested" costs a
sentence. A fallback latency nobody measured, quoted from the GPU tier, is a fabricated number.

Requires: **gate 5 PASS** (the artifact must be deployable at all), **gate 7 PASS** (there is no
demo without it).

## 1. What was run — fill BEFORE running

| Field | Value |
|---|---|
| Report id | `___` |
| Date (UTC) | `___` |
| Source commit | `___` |
| Test trace id | `___` |
| Test trace SHA-256 | `___` |
| Sessions in the trace | `___` |
| Purpose codes exercised | `___` |
| Both tiers run the identical trace | `___` |

### Tier identification

| Field | GPU tier | CPU fallback tier |
|---|---|---|
| Host / instance type | `___` | `___` |
| CPU model and core count | `___` | `___` |
| GPU model | `___` | — |
| Available memory | `___` | `___` |
| Execution provider | `___` | `___` |
| `onnxruntime` package and version | `___` | `___` |
| CUDA / cuDNN versions | `___` | — |
| Thread configuration (`intra_op`, `inter_op`, execution mode) | `___` | `___` |
| Container digest | `___` | `___` |
| Model SHA-256 loaded | `___` | `___` |
| Calibration SHA-256 loaded | `___` | `___` |
| Policy bundle SHA-256 loaded | `___` | `___` |
| Detector mode reported at startup | `___` | `___` |

The fallback tier is a **named laptop**, recorded in the host row. A p95 is a measurement from a
named host, not a portability promise (playbook §7).

The three artifact hashes must match across tiers. A demo where the two tiers loaded different
policy bundles is a demo of two different systems, and the audit rows would attribute decisions to
whichever bundle that tier happened to hold.

## 2. Predeclared criteria — fill BEFORE running

| Criterion | Value |
|---|---|
| Permitted action disagreements between tiers over the trace | `___` |
| Permitted risk-state disagreements between tiers | `___` |
| Latency budget for the GPU tier | `___` |
| Latency budget for the CPU tier | `___` |
| First-decision latency budget | `___` |
| Backlog depth that counts as failing to keep up | `___` |
| Dropped-window count that counts as failing | `___` |
| What will be said if only one tier meets its budget | `___` |

The last row is the one that matters, and it is filled in before the numbers exist so it is not
written under demo pressure.

## 3. Results — fill AFTER running

### 3.1 Decision parity across tiers

| Metric | GPU tier | CPU tier | Disagreements | Within §2 |
|---|---|---|---|---|
| Sessions completed | `___` | `___` | `___` | `___` |
| Windows scored | `___` | `___` | `___` | `___` |
| Windows marked ineligible | `___` | `___` | `___` | `___` |
| Sessions reaching `collecting` only | `___` | `___` | `___` | `___` |
| Sessions reaching `uncertain` | `___` | `___` | `___` | `___` |
| Sessions reaching `high` | `___` | `___` | `___` | `___` |
| Final action per session | `___` | `___` | `___` | `___` |
| Reason codes emitted | `___` | `___` | `___` | `___` |

Identical trace, identical bundle, so an action disagreement is a parity defect, not a difference in
conditions.

### 3.2 Latency

Report the distribution, not a mean. A mean latency hides the tail that a caller actually waits
through, and the tail is the demo.

| Measurement | GPU tier | CPU tier | Budget met |
|---|---|---|---|
| Scorer latency p50 | `___` | `___` | `___` |
| Scorer latency p95 | `___` | `___` | `___` |
| Scorer latency p99 | `___` | `___` | `___` |
| Scorer latency max | `___` | `___` | `___` |
| End-to-end window latency p95 | `___` | `___` | `___` |
| First-decision latency p50 | `___` | `___` | `___` |
| First-decision latency p95 | `___` | `___` | `___` |
| Time from first high window to action p95 | `___` | `___` | `___` |

### 3.3 Throughput and backlog

| Measurement | GPU tier | CPU tier | Within §2 |
|---|---|---|---|
| Concurrent sessions sustained | `___` | `___` | `___` |
| Maximum backlog depth | `___` | `___` | `___` |
| Windows dropped under load | `___` | `___` | `___` |
| Sessions that failed to reach a decision | `___` | `___` | `___` |
| Scorer errors | `___` | `___` | `___` |
| Reconnects during the trace | `___` | `___` | `___` |

Dropped windows are reported and never counted as low risk. An ineligible or missing window is
skipped by the evidence bar, not treated as clean (rules.md R-09) — if this row is non-zero, say so
during the demo rather than after it.

### 3.4 Audit evidence produced by the run

The run is only demonstrable if it left a verifiable trail.

| Check | GPU tier | CPU tier |
|---|---|---|
| Audit rows written | `___` | `___` |
| Chain verification passes over every session | `___` | `___` |
| Sessions failing verification | `___` | `___` |
| Rows attributing the decision to the correct bundle hash | `___` | `___` |
| Rows attributing the decision to the correct model hash | `___` | `___` |
| Replay from the audit table reproduces the live actions | `___` | `___` |

Replay reproducing the live actions is the strongest single claim available from this gate: the
decision path is pure, so a session can be re-decided from stored scores and the result compared.

### 3.5 What will actually be shown

| Field | Value |
|---|---|
| Tier used in the live demo | `___` |
| Tier described as measured but not shown | `___` |
| Tier NOT measured, and stated as such | `___` |
| Detector mode during the demo | `___` |
| Artifact state disclosed to the audience | `___` |
| Numbers that will be quoted on screen | `___` |
| Source of each quoted number | `___` |
| Numbers explicitly labelled as placeholders | `___` |

If the detector is running in a mock mode, that is stated to the audience before the first score
appears, not in a footnote. A mock scorer producing a convincing `high` state is a demo of the
control plane, which is a real thing to have built, and describing it as a detection is not.

## 4. Verdict

- [ ] **PASS (both tiers)** — the same trace ran on both, parity is within §2, and both budgets met.
- [ ] **PASS (one tier, stated truthfully)** — one tier met its budget; the other is presented as
      untested or as failing, in the words recorded in §2's last row.
- [ ] **FAIL** — fix parity, or present one tier only. Record which below.
- [ ] **NOT RUN**

| Field | Value |
|---|---|
| Claims permitted on stage | `___` |
| Claims explicitly forbidden | `___` |
| Signed off by (team lead) | `___` |
| Date | `___` |

**Findings:**

`___`

## 5. What this gate does not establish

- It does not show the detector works. It shows two tiers agree and how fast they are; accuracy is
  gates 2 to 4, and a mock scorer passes this gate completely.
- The latency figures belong to the named hosts in §1 and to no others. A different laptop, a
  different thread configuration, or a busy machine invalidates them.
- A scripted trace is not a live call. Real audio arrives with jitter, packet loss, and silence that
  a replayed trace does not reproduce.
- Passing on the trace says nothing about the session that goes wrong on the day. The backlog and
  dropped-window rows are the ones to watch live, not the latency percentiles.
