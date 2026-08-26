# Gate 6 — Quantization (TEMPLATE — NOT YET RUN)

| | |
|---|---|
| **Status** | `not-run` |
| **Blocking** | The quantized artifact only. Not a deploy or release blocker for the FP32 artifact. |
| **Owner (playbook §6.1)** | ML lead |
| **Pass condition (playbook §6.1, verbatim)** | Locked-set metrics, calibration, and temporal policy remain acceptable |
| **Failure response (playbook §6.1, verbatim)** | Retain FP32 model |

Quantization is optional and it is a **separate model version**, never a silent replacement
(playbook §7). The failure this rule prevents: a quantized graph is dropped in to hit a latency
target on the fallback laptop, the model SHA-256 changes, and every metric on record now describes
the FP32 model that is no longer running. Failing this gate costs nothing except the speedup —
retaining FP32 is always a legitimate outcome.

Three things degrade under quantization and they degrade in that order of subtlety: ranking barely
moves, **calibration shifts** (the score distribution compresses, so a fixed threshold sits at a
different operating point), and **temporal policy behaviour** changes because the k-of-n bar is
crossed at different times. A gate that checks only EER will pass a model whose false hold rate has
doubled.

Requires: **gate 4 PASS**, **gate 5 PASS on the FP32 artifact**.

## 1. What was run — fill BEFORE running

| Field | Value |
|---|---|
| Report id | `___` |
| Date (UTC) | `___` |
| Source commit | `___` |
| FP32 artifact SHA-256 | `___` |
| Quantized artifact SHA-256 | `___` |
| Quantized `model_version` (must differ from FP32) | `___` |
| Quantization scheme | `___` |
| Weight / activation precision | `___` |
| Calibration data used for quantization ranges | `___` |
| Split that data came from | `___` |
| Runtime and provider | `___` |
| Host | `___` |

The data used to pick quantization ranges is a fit, so it obeys the same split rule as the
calibrator: `dev_calibration`, never `eval_locked` (rules.md R-37). Recorded here because
quantization range calibration is easy to overlook as "not really training".

## 2. Predeclared criteria — fill BEFORE running

| Criterion | Value |
|---|---|
| Locked-set EER regression that counts as unacceptable | `___` |
| Locked-set TPR regression that counts as unacceptable | `___` |
| ECE degradation that counts as unacceptable | `___` |
| Brier degradation that counts as unacceptable | `___` |
| Permitted change in session-level false hold rate | `___` |
| Permitted change in detection-to-action time | `___` |
| Permitted action disagreements on the replay trace | `___` |
| Latency improvement that would justify accepting any regression at all | `___` |
| Decision if the speedup is below that figure | `___` |

The last two rows are the honest part of this gate. If the speedup is small, no regression is worth
accepting, and declaring the required speedup in advance stops the trade being made after the fact
by whoever is under demo pressure.

## 3. Results — fill AFTER running

### 3.1 Locked-set discrimination

| Metric | FP32 | Quantized | Δ | Acceptable per §2 |
|---|---|---|---|---|
| EER | `___` | `___` | `___` | `___` |
| ROC-AUC | `___` | `___` | `___` | `___` |
| TPR at declared FPR | `___` | `___` | `___` | `___` |
| FNR at the operating point | `___` | `___` | `___` | `___` |

### 3.2 Calibration under quantization

| Metric | FP32 | Quantized | Δ | Acceptable per §2 |
|---|---|---|---|---|
| Expected calibration error | `___` | `___` | `___` | `___` |
| Brier score | `___` | `___` | `___` | `___` |
| Mean score on bona fide | `___` | `___` | `___` | `___` |
| Mean score on spoof | `___` | `___` | `___` | `___` |
| Score standard deviation | `___` | `___` | `___` | `___` |
| Fraction of windows over the policy threshold | `___` | `___` | `___` | `___` |

The last row is the one that catches distribution compression. A quantized model can keep its EER
and move the fraction of windows over a fixed threshold substantially, which changes every action
the system takes without changing any accuracy metric.

| Question | Value |
|---|---|
| Does the existing calibration artifact remain valid for this artifact? | `___` |
| If not, was a separate calibration fitted for the quantized version? | `___` |
| Its `calibration_version` | `___` |
| Its `model_sha256` (must be the quantized artifact's) | `___` |

### 3.3 Temporal policy behaviour

Replayed over the same session traces, through the same decision path.

| Metric | FP32 | Quantized | Δ | Acceptable per §2 |
|---|---|---|---|---|
| Session-level sensitivity | `___` | `___` | `___` | `___` |
| Session-level false hold rate | `___` | `___` | `___` | `___` |
| Detection-to-action time (p50) | `___` | `___` | `___` | `___` |
| Detection-to-action time (p95) | `___` | `___` | `___` | `___` |
| Sessions reaching `uncertain` | `___` | `___` | `___` | `___` |
| Sessions reaching `high` | `___` | `___` | `___` | `___` |
| Sessions whose final action differs | `___` | `___` | `___` | `___` |
| Windows marked ineligible | `___` | `___` | `___` | `___` |

### 3.4 Parity against the FP32 artifact

Gate 5's checks, re-run for this artifact. The quantized graph is a new artifact and inherits
nothing.

| Check | Value | Within gate 5's declared tolerance |
|---|---|---|
| Maximum absolute score deviation from FP32 | `___` | `___` |
| Rank inversions | `___` | `___` |
| Inversions crossing the policy threshold | `___` | `___` |
| Class orientation confirmed unchanged | `___` | `___` |

### 3.5 What was gained

| Metric | FP32 | Quantized | Improvement |
|---|---|---|---|
| Scorer latency p50 | `___` | `___` | `___` |
| Scorer latency p95 | `___` | `___` | `___` |
| First-decision latency | `___` | `___` | `___` |
| Peak resident memory | `___` | `___` | `___` |
| Artifact size on disk | `___` | `___` | `___` |
| Sustained concurrent sessions | `___` | `___` | `___` |
| Host these were measured on | `___` | `___` | — |

Measured on the real fallback laptop, named in the last row. A p95 from a developer workstation is
not a fallback-tier measurement.

### 3.6 Artifact separation

| Check | Value |
|---|---|
| Quantized artifact has its own `model_version` | `___` |
| Both artifacts retained and hashed in the release manifest | `___` |
| Release manifest records which artifact each tier loads | `___` |
| No configuration can silently substitute one for the other | `___` |
| Scorer `HealthResponse` reports the SHA-256 of the artifact actually loaded | `___` |

## 4. Verdict

- [ ] **PASS** — locked-set metrics, calibration, and temporal policy all within the §2 limits, and
      the speedup meets the figure declared there.
- [ ] **FAIL** — retain the FP32 model. This is a normal outcome and costs only the speedup.
- [ ] **NOT RUN** — the FP32 artifact remains the deployed artifact on both tiers.

| Field | Value |
|---|---|
| Artifact deployed after this gate | `___` |
| Artifact state supported | `___` |
| Signed off by (ML lead) | `___` |
| Date | `___` |

**Findings:**

`___`

## 5. What this gate does not establish

- It does not transfer to another host. Quantized speedups depend on instruction-set support, and a
  gain on one laptop can be a loss on another.
- It does not license reusing the FP32 calibration. If §3.2 shows the distribution moved, the
  quantized artifact needs its own fitted calibration and its own gate 4.
- It does not make the quantized artifact the default. Both artifacts stay in the release manifest,
  and which tier loads which is recorded rather than inferred.
- Passing here with a small speedup is still a reason not to ship it. Two artifacts to reason about
  is a real cost, and this gate does not weigh it.
