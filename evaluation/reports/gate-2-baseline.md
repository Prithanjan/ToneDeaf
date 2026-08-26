# Gate 2 — Baseline (TEMPLATE — NOT YET RUN)

| | |
|---|---|
| **Status** | `not-run` |
| **Blocking** | Feature work. Not a deploy or release blocker. |
| **Owner (playbook §6.1)** | ML lead |
| **Pass condition (playbook §6.1, verbatim)** | AASIST exceeds or matches LFCC-LCNN and RawNet2 on declared dev protocol |
| **Failure response (playbook §6.1, verbatim)** | Investigate input pipeline before adding features |

The point of this gate is diagnostic, not competitive. If the adopted primary scorer cannot match
two well-understood baselines on the same protocol, the most likely cause is the input pipeline —
resampling, channel downmix, normalisation, class orientation — not the model. Adding features on
top of a broken pipeline produces a model that works for reasons nobody can name, and the features
get the credit.

Requires: **gate 1 PASS**. A baseline comparison on a leaked split ranks nothing.

## 1. What was run — fill BEFORE running

| Field | Value |
|---|---|
| Report id | `___` |
| Date (UTC) | `___` |
| Source commit | `___` |
| Manifest SHA-256 | `___` |
| Split hash | `___` |
| Declared dev protocol | `___` |
| Container digest | `___` |
| Python / torch / CUDA / ORT versions | `___` |
| Seeds used | `___` |
| Precision | `___` |
| Hardware | `___` |

Seeds are plural. A single seed reports a lucky draw; playbook §4 requires mean and range.

## 2. Predeclared criteria — fill BEFORE running

| Criterion | Value |
|---|---|
| Protocol the comparison is declared on | `___` |
| Metric the comparison is decided on | `___` |
| Margin treated as "matches" rather than "exceeds" | `___` |
| Number of seeds required per model | `___` |
| What counts as a pipeline defect rather than a model result | `___` |

The margin must be declared here. Deciding after the fact that a gap "is within noise" is how a
regression becomes a tie.

## 3. Results — fill AFTER running

### 3.1 Discrimination on the declared dev protocol

Report mean and range across seeds. A single figure per model is not acceptable.

| Model | EER mean | EER range | ROC-AUC mean | ROC-AUC range | PR-AUC mean | Seeds |
|---|---|---|---|---|---|---|
| AASIST (primary) | `___` | `___` | `___` | `___` | `___` | `___` |
| LFCC-LCNN (baseline) | `___` | `___` | `___` | `___` | `___` | `___` |
| RawNet2 (baseline) | `___` | `___` | `___` | `___` | `___` | `___` |

### 3.2 Benchmark reference figures

Benchmark and product metrics are reported separately and never mixed (playbook §6). This table is
for literature comparability only; it does not describe product behaviour.

| Model | Corpus / protocol | EER | min t-DCF | Notes |
|---|---|---|---|---|
| AASIST | `___` | `___` | `___` | `___` |
| LFCC-LCNN | `___` | `___` | `___` | `___` |
| RawNet2 | `___` | `___` | `___` | `___` |

min t-DCF is reported only under an official protocol with its published ASV parameters. If those
parameters were not used, the cell reads `not-applicable`, never a computed number.

### 3.3 Input pipeline verification

Filled whether or not the comparison passed, because these are the rows that explain a failure.

| Check | Value |
|---|---|
| Input tensor shape and dtype asserted against the contract | `___` |
| Fixture waveform digest identical across all three models | `___` |
| Sample rate normalisation identical across all three models | `___` |
| Channel downmix identical across all three models | `___` |
| Output class orientation confirmed (higher score means more likely synthetic) | `___` |
| Label mapping confirmed against the manifest's `label` field | `___` |
| Score distribution sanity: bona fide and spoof separable at all | `___` |

Class orientation is checked explicitly because an inverted head produces a mirror-image EER that
looks like a competent model with an unlucky threshold.

### 3.4 Cost of the comparison

| Field | Value |
|---|---|
| Training wall-clock per model | `___` |
| GPU hours consumed | `___` |
| Checkpoint SHA-256 per model | `___` |

## 4. Verdict

- [ ] **PASS** — the primary scorer meets or exceeds both baselines on the declared metric, within
      the declared margin, across the declared seeds.
- [ ] **FAIL** — investigate the input pipeline before adding features. Record the suspected defect
      below.
- [ ] **NOT RUN**

| Field | Value |
|---|---|
| Artifact state supported | `___` |
| Signed off by (ML lead) | `___` |
| Date | `___` |

**Findings:**

`___`

## 5. What this gate does not establish

- Nothing about generalisation. This is a dev-protocol comparison; unseen generators, codecs, and
  languages are gate 3, and a model can win here and collapse there.
- Nothing about calibration. A model with the best EER can still have scores that are useless as
  risk values — that is gate 4.
- Nothing about the deployed artifact. This measures the PyTorch model; the exported graph is gate 5.
- It does not license a claim of the form "our model beats X". The comparison is on one declared
  protocol with this project's splits, not a published leaderboard result.
