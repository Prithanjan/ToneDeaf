# Gate 4 — Calibration (TEMPLATE — NOT YET RUN)

| | |
|---|---|
| **Status** | `not-run` |
| **Blocking** | Probability language everywhere, and `policy_eligible`. Not a deploy or release blocker. |
| **Owner (playbook §6.1)** | ML lead |
| **Pass condition (playbook §6.1, verbatim)** | Improved ECE/Brier on dev without harmful locked-set regression |
| **Failure response (playbook §6.1, verbatim)** | Freeze simpler calibrator or retrain |

This is the gate that decides whether `spoof_risk` may be *described* as probability-like. Until it
passes, the score is a score: no document, slide, UI string, log line, or spoken sentence may call
it a probability, a likelihood, a confidence, or a percentage chance (rules.md R-11). The current
`policy/calibration.json` carries `status: placeholder-not-policy-eligible` and an identity Platt
transform, so as of the template's writing that restriction is in force.

It is also the gate that gates the threshold. `policy/policy.yaml`'s `high_window_risk` carries
`derivation: placeholder`, and re-deriving it requires a fitted calibration plus a cost matrix —
see `policy/README.md`.

Requires: **gate 1 PASS**, **gate 2 PASS**.

## 1. What was run — fill BEFORE running

| Field | Value |
|---|---|
| Report id | `___` |
| Date (UTC) | `___` |
| Source commit | `___` |
| Model checkpoint SHA-256 | `___` |
| Manifest SHA-256 | `___` |
| Fit split | `___` |
| Fit split sample count | `___` |
| Method | `___` |
| Candidate methods compared | `___` |

### The split discipline (rules.md R-37)

| Check | Value |
|---|---|
| Split the calibrator was fitted on (must be `dev_calibration`) | `___` |
| Confirmed NOT `eval_locked` | `___` |
| `eval_locked` opened for reporting only, once | `___` |
| Number of times `eval_locked` has been read for this candidate | `___` |
| Fit split grouped by speaker / root audio / generator before augmentation | `___` |
| Manifest grouping keys straddling the fit and locked splits (must be zero) | `___` |

`dev_calibration` is the only split a calibrator or a threshold may be fitted on. Fitting anything
on `eval_locked` — a model, a threshold, or this mapping — turns a held-out estimate into a training
estimate. The damage is silent and permanent: once the locked split has informed a choice, no re-run
restores its independence, and the honest response is to build a new locked split rather than to
reuse the compromised one. Read count is recorded because "we peeked once to check" is how it
happens.

## 2. Predeclared criteria — fill BEFORE running

| Criterion | Value |
|---|---|
| Baseline the improvement is measured against (uncalibrated, or the previous artifact) | `___` |
| ECE improvement required to pass | `___` |
| Brier improvement required to pass | `___` |
| Number of bins, and the binning scheme | `___` |
| Locked-set regression that counts as "harmful" | `___` |
| Simpler calibrator to freeze if the candidate fails | `___` |
| Cohorts calibration must be reported per, not just globally | `___` |

The binning scheme is declared in advance because ECE is sensitive to it: equal-width and
equal-mass bins on the same scores give different numbers, and choosing afterwards means choosing
the one that passes.

## 3. Results — fill AFTER running

### 3.1 Global calibration on the fit split

| Metric | Uncalibrated | Candidate | Simpler alternative | Required improvement met |
|---|---|---|---|---|
| Expected calibration error | `___` | `___` | `___` | `___` |
| Maximum calibration error | `___` | `___` | `___` | `___` |
| Brier score | `___` | `___` | `___` | `___` |
| Negative log-likelihood | `___` | `___` | `___` | `___` |

### 3.2 Reliability diagram

| Bin | Predicted range | Mean predicted | Observed frequency | Count |
|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` |

Attach the plot alongside this report. The table is the primary record; a plot without the counts
per bin hides that the extreme bins hold a handful of samples.

### 3.3 Locked-set behaviour

Read once. Reported, not optimised against.

| Metric | Previous artifact | Candidate | Δ | Harmful per §2 |
|---|---|---|---|---|
| Expected calibration error | `___` | `___` | `___` | `___` |
| Brier score | `___` | `___` | `___` | `___` |
| EER | `___` | `___` | `___` | `___` |
| TPR at declared FPR | `___` | `___` | `___` | `___` |

### 3.4 Calibration per cohort

Global calibration can be good while a language or codec cohort is badly miscalibrated, and the
cohort is where the harm lands.

| Cohort | Samples | ECE | Brier | Δ vs global |
|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` |

### 3.5 The fitted artifact

| Field | Value |
|---|---|
| Method | `___` |
| Slope / `a` | `___` |
| Intercept / `b` | `___` |
| Isotonic bin edges, if applicable | `___` |
| `fitted` flag set true | `___` |
| Identity transform confirmed replaced | `___` |
| `calibration.json` SHA-256 after the edit | `___` |
| `calibration_version` bumped | `___` |
| Paired `model_sha256` | `___` |
| Scorer `HealthResponse.model_sha256` matches | `___` |

`policy/calibration.json`'s promotion checklist lists these in order. Every one of them must be
done in the same commit; a fitted coefficient with `fitted: false` still reads as a placeholder to
every consumer, and a bumped version with stale coefficients is worse.

### 3.6 Threshold implications

Filled if this report is also being used to derive an operating point. If the threshold is not
being re-derived here, every row reads `not-derived-in-this-report`.

| Field | Value |
|---|---|
| Cost of a false hold on a legitimate caller | `___` |
| Cost of a missed synthetic caller | `___` |
| Cost matrix owner (a named person) | `___` |
| Operating point selected | `___` |
| Split it was selected on | `___` |
| Purpose codes it was approved for | `___` |
| Uncertain band around it | `___` |
| Session-level false hold rate at that point | `___` |
| `policy.yaml` `derivation` value after the change | `___` |

A single operating point approved for all purposes is not a derivation. The playbook requires
approval per use case, and `policy.yaml` already maps purposes to different actions at the same
evidence level.

## 4. Verdict

- [ ] **PASS** — ECE and Brier improved on the fit split by at least the declared margins, with no
      harmful locked-set regression, and every row of §3.5 is complete.
- [ ] **FAIL** — freeze the simpler calibrator or retrain. Record which, and why, below.
- [ ] **NOT RUN**

### Language permitted after this gate

| Question | Value |
|---|---|
| May `spoof_risk` be described as probability-like? | `___` |
| Wording approved for the UI | `___` |
| Wording explicitly forbidden | `___` |
| Cohorts where probability language is NOT permitted | `___` |

| Field | Value |
|---|---|
| Artifact state supported | `___` |
| Signed off by (ML lead) | `___` |
| Date | `___` |

**Findings:**

`___`

## 5. What this gate does not establish

- Calibration on the fit split does not transfer to a cohort the fit split did not contain. A
  calibrator fitted on clean studio audio is miscalibrated on a speakerphone in a call centre, and
  the miscalibration is invisible in the global figure.
- A well-calibrated score is still not a verdict. Calibration licenses probability *language*, not
  an authorization outcome; the action vocabulary stays closed (rules.md R-07).
- It does not survive a model change. A calibrator is fitted to one model's score distribution;
  swapping the model invalidates it silently, which is why `model_version` must match on load.
- It says nothing about the exported artifact. Calibrated decisions matching between PyTorch and
  ONNX is gate 5, and that is where a calibration can be undone by an export.
