# Gate 3 — Out-of-distribution (TEMPLATE — NOT YET RUN)

| | |
|---|---|
| **Status** | `not-run` |
| **Blocking** | The scope of every claim. Not a deploy or release blocker. |
| **Owner (playbook §6.1)** | ML + evaluation lead |
| **Pass condition (playbook §6.1, verbatim)** | Report generator-, codec-, language-, and device-held-out results |
| **Failure response (playbook §6.1, verbatim)** | Restrict claim or retain `uncertain` policy; do not hide gap |

**Note the pass condition: it is to REPORT, not to score well.** This gate cannot be failed by a
bad number. It is failed by an unreported cohort. A held-out generator that halves accuracy is a
passing gate and a restricted claim; the same result left out of the table is a failing gate.

That asymmetry is deliberate. The realistic deployment condition is an attack from a generator
nobody trained on, and a system whose OOD degradation is unmeasured is a system whose real-world
accuracy is unknown while being described as measured.

Requires: **gate 1 PASS**.

## 1. What was run — fill BEFORE running

| Field | Value |
|---|---|
| Report id | `___` |
| Date (UTC) | `___` |
| Source commit | `___` |
| Model checkpoint SHA-256 | `___` |
| Manifest SHA-256 | `___` |
| Splits evaluated | `___` |
| Seeds | `___` |

## 2. Predeclared criteria — fill BEFORE running

| Criterion | Value |
|---|---|
| Cohorts that MUST appear in §3 (declare the full list) | `___` |
| Minimum samples for a cohort to be reported rather than suppressed | `___` |
| Metric the worst-group figure is computed on | `___` |
| Degradation that triggers a claim restriction | `___` |
| Degradation that triggers retaining `uncertain` rather than reaching `high` | `___` |

A cohort below the minimum sample count is reported as `insufficient-data`, never merged into
another cohort and never omitted. Merging small cohorts hides exactly the group that is degrading.

## 3. Results — fill AFTER running

### 3.1 Generator held out

`eval_generator_heldout`: entire generator family+version absent from training.

| Held-out generator family+version | Samples | EER | TPR at declared FPR | FNR | Δ vs `eval_locked` |
|---|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` | `___` |
| `___` | `___` | `___` | `___` | `___` | `___` |

### 3.2 Codec and channel held out

| Codec chain | Samples | EER | TPR at declared FPR | FNR | Δ vs clean |
|---|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` | `___` |
| `___` | `___` | `___` | `___` | `___` | `___` |

Report the full chain, not the last transcode. A sample that went through one codec and then
another does not behave like either.

### 3.3 Language and script

| Language | Script | Samples | EER | TPR at declared FPR | FNR | Δ vs best cohort |
|---|---|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` | `___` | `___` |
| `___` | `___` | `___` | `___` | `___` | `___` | `___` |

### 3.4 Device and channel condition

| Capture device | Channel condition | Samples | EER | TPR at declared FPR | FNR | Δ vs best cohort |
|---|---|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` | `___` | `___` |
| `___` | `___` | `___` | `___` | `___` | `___` | `___` |

### 3.5 Attack type

| Attack type | Samples | EER | FNR | Notes |
|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` |

A pooled figure across attack types hides which attack the detector cannot see. Replay and TTS fail
differently and a single EER averages one into the other.

### 3.6 Duration

Short utterances produce fewer eligible windows, so session-level behaviour degrades faster than
window-level metrics suggest.

| Duration band | Samples | EER | Eligible windows per sample | Sessions reaching a decision |
|---|---|---|---|---|
| `___` | `___` | `___` | `___` | `___` |

### 3.7 Worst group and maximum gap

| Field | Value |
|---|---|
| Metric | `___` |
| Best cohort | `___` |
| Best cohort figure | `___` |
| Worst cohort | `___` |
| Worst cohort figure | `___` |
| Maximum gap | `___` |
| Cohorts reported `insufficient-data` | `___` |
| Cohorts omitted (must be zero) | `___` |

Fairness cohorts are built only from consented or legitimately labelled metadata (playbook §6).
`accent_region` is used only where `accent_region_source` is self-reported or dataset metadata —
never inferred from audio, which the manifest schema makes unstateable.

### 3.8 Aggregate, for reference only

| Field | Value |
|---|---|
| Pooled EER across all cohorts | `___` |
| Pooled ROC-AUC | `___` |

Reported last and labelled for reference. The pooled figure is the number that gets quoted and the
one that means least: it is a weighted average over whatever cohort mix this manifest happens to
have, and it moves when the mix moves without the model changing.

## 4. Verdict

- [ ] **PASS** — every declared cohort from §2 appears in §3 with a figure or an explicit
      `insufficient-data`, and the gaps are stated rather than smoothed.
- [ ] **FAIL** — a declared cohort is missing or was merged away. Report it and re-run.
- [ ] **NOT RUN**

### Claim restrictions this gate imposes

Written as sentences somebody can check a slide against.

| Restriction | Value |
|---|---|
| Languages the system may be claimed to work on | `___` |
| Codecs the system may be claimed to work on | `___` |
| Generator families the system may be claimed to detect | `___` |
| Cohorts where policy must retain `uncertain` rather than reaching `high` | `___` |
| Claims explicitly forbidden by this report | `___` |

| Field | Value |
|---|---|
| Artifact state supported | `___` |
| Signed off by (ML lead) | `___` |
| Signed off by (evaluation lead) | `___` |
| Date | `___` |

**Findings:**

`___`

## 5. What this gate does not establish

- It does not measure the generators nobody has heard of. Held-out families are a proxy for unseen
  attacks, and the proxy is optimistic: a family held out of training was still available to be
  chosen, so it resembles the training distribution more than a genuinely novel generator will.
- It says nothing about calibration. A cohort can have good ranking and badly wrong risk values.
- Small cohorts carry wide intervals. A cohort of a few dozen samples cannot distinguish a real gap
  from sampling noise, and the `insufficient-data` marker exists so the difference is not glossed.
- It does not authorise a claim about the field. Every figure here is measured on this manifest.
