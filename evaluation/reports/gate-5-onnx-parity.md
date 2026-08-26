# Gate 5 — ONNX parity (TEMPLATE — NOT YET RUN)

| | |
|---|---|
| **Status** | `not-run` |
| **Blocking** | **DEPLOYMENT. THIS IS A DEPLOY BLOCKER.** |
| **Owner (playbook §6.1)** | ML + platform lead |
| **Pass condition (playbook §6.1, verbatim)** | Output ranking and calibrated decisions match reference within predeclared tolerance |
| **Failure response (playbook §6.1, verbatim)** | Block deployment artifact |

## Why this one blocks deployment

Every number in gates 2, 3, 4, and 6 describes the PyTorch model. The thing that runs in production
is the exported graph. If they disagree, the reports are about a model that is not deployed, and
nothing in the running system will look wrong: the artifact still returns a score in range, the
Gateway still accumulates evidence, the policy still produces proportionate actions, and the audit
chain still verifies. A silently different model produces a silently different risk ordering and a
perfectly clean evidence trail.

The worst case is **class-orientation inversion**: the export flips the head, high risk becomes low
risk, and the system confidently continues on exactly the calls it should hold. That failure is
undetectable from output shape, from range checks, from latency, and from the audit table. It is
detectable only by comparing scores against the reference on the same inputs — this gate.

A failure here does not stop research. The PyTorch model may continue to be evaluated. **The ONNX
artifact may not be deployed, to any tier, including the demo.**

Requires: **gate 2 PASS**, **gate 4 PASS or explicitly not-yet-run**.

## 1. What was run — fill BEFORE running

| Field | Value |
|---|---|
| Report id | `___` |
| Date (UTC) | `___` |
| Source commit | `___` |
| Reference checkpoint SHA-256 | `___` |
| ONNX artifact SHA-256 | `___` |
| Opset version | `___` |
| Exporter and version | `___` |
| `onnxruntime` version (CPU tier) | `___` |
| `onnxruntime-gpu` version (GPU tier) | `___` |
| CUDA / cuDNN versions | `___` |
| Fixture vector set id | `___` |
| Fixture vector set SHA-256 | `___` |
| Fixture vector count | `___` |

The fixture set is hashed and committed. A parity run against inputs that were regenerated between
runs proves nothing, because a disagreement cannot be attributed to the model rather than the data.

## 2. Predeclared tolerance — fill BEFORE running

**Fill this section, commit it, and only then run the comparison.** A tolerance chosen after seeing
the maximum observed deviation is not a tolerance; it is a description of the deviation. The commit
order is the evidence, which is why §1 and §2 go in one commit and §3 in another.

| Criterion | Value |
|---|---|
| Maximum permitted absolute score deviation | `___` |
| Maximum permitted relative score deviation | `___` |
| Permitted rank inversions across the fixture set | `___` |
| Permitted calibrated-decision disagreements | `___` |
| Permitted risk-state disagreements over a replayed session | `___` |
| Permitted action disagreements over a replayed session | `___` |
| Tolerance basis (numeric-precision argument, not a fitted value) | `___` |
| Declared by | `___` |
| Declared at (UTC) | `___` |
| Commit in which §2 was committed, before §3 existed | `___` |

Permitted **action** disagreements should be zero, and if a non-zero value is declared here it needs
an argument in writing. Scores may differ in the last bits; the action a human acts on may not.

## 3. Results — fill AFTER running

### 3.1 Graph contract

| Check | Value |
|---|---|
| Input name | `___` |
| Input shape | `___` |
| Input dtype | `___` |
| Output name | `___` |
| Output shape | `___` |
| Output interpretation documented outside the graph | `___` |
| Preprocessing boundary documented (PCM-to-float, resampling, downmix, clipping) | `___` |
| Nothing left implicit per playbook §7 | `___` |

### 3.2 Score parity on the fixture set

| Statistic | Value | Within tolerance |
|---|---|---|
| Maximum absolute deviation | `___` | `___` |
| Mean absolute deviation | `___` | `___` |
| Maximum relative deviation | `___` | `___` |
| Fixtures exceeding the absolute tolerance | `___` | `___` |
| Fixtures producing NaN or infinity in either implementation | `___` | `___` |

### 3.3 Ranking parity

Ranking matters more than absolute agreement: a constant offset is harmless, a reordering is not.

| Check | Value | Within tolerance |
|---|---|---|
| Spearman rank correlation | `___` | `___` |
| Adjacent-pair inversions | `___` | `___` |
| Inversions crossing the policy threshold | `___` | `___` |

### 3.4 Class orientation

The single check that catches the worst failure. Filled explicitly even when parity is exact.

| Check | Value |
|---|---|
| Highest-scoring reference fixture is also highest-scoring under ONNX | `___` |
| Known-spoof fixtures score above known-bona-fide fixtures under ONNX | `___` |
| Sign of the correlation between reference and ONNX scores | `___` |
| Orientation confirmed as: higher score means more likely synthetic | `___` |

### 3.5 Calibrated decision parity

Parity is required *after* calibration and *after* policy, because that is where the disagreement
reaches a person.

| Check | Reference | ONNX | Disagreements | Within tolerance |
|---|---|---|---|---|
| Windows over the policy threshold | `___` | `___` | `___` | `___` |
| Windows marked eligible | `___` | `___` | `___` | `___` |
| Sessions reaching `uncertain` | `___` | `___` | `___` | `___` |
| Sessions reaching `high` | `___` | `___` | `___` | `___` |
| Final action per replayed session | `___` | `___` | `___` | `___` |
| Time-to-first-decision distribution | `___` | `___` | `___` | `___` |

Replay uses the pure decision path (`gateway/app/policy/engine.py::replay`), so a session can be
re-decided from stored scores with no clock and no network involved.

### 3.6 Cross-provider parity

The two tiers must agree with the reference *and* with each other.

| Comparison | Max absolute deviation | Rank inversions | Action disagreements | Within tolerance |
|---|---|---|---|---|
| Reference vs CUDA provider | `___` | `___` | `___` | `___` |
| Reference vs CPU provider | `___` | `___` | `___` | `___` |
| CUDA provider vs CPU provider | `___` | `___` | `___` | `___` |

| Check | Value |
|---|---|
| Startup log confirms the provider list on each tier | `___` |
| Same model SHA-256 loaded on both tiers | `___` |
| Same calibration SHA-256 loaded on both tiers | `___` |
| Release manifest fails on a hash mismatch | `___` |

## 4. Verdict

- [ ] **PASS** — every §3 row is within the tolerance declared in §2, and class orientation is
      confirmed.
- [ ] **FAIL** — **BLOCK THE DEPLOYMENT ARTIFACT.** The exported model does not ship. Record the
      deviation and the suspected cause below. Do not widen the §2 tolerance to make this pass; a
      widened tolerance must be declared in a fresh report with a fresh predeclaration commit.
- [ ] **NOT RUN** — treated as FAIL for deployment purposes. An unrun parity gate is not a passing
      parity gate, and the artifact does not ship on the strength of an untested export.

| Field | Value |
|---|---|
| Artifact state supported | `___` |
| Deployment authorised | `___` |
| Signed off by (ML lead) | `___` |
| Signed off by (platform lead) | `___` |
| Date | `___` |

**Findings:**

`___`

## 5. What this gate does not establish

- It only covers the fixtures in the set. Parity on a fixture set that lacks a codec, a language, or
  a duration band says nothing about that condition — the set should mirror the OOD cohorts.
- It does not cover the quantized artifact. That is a separate model version and a separate gate (6);
  a quantized graph is never a drop-in for the one measured here.
- It says nothing about throughput or latency. That is gate 8.
- It does not prove the export is *correct*, only that it agrees with the reference. Both can be
  wrong together, which is what gate 2's input-pipeline rows are for.
- Parity today does not survive a runtime upgrade. A provider or `onnxruntime` version bump is a new
  artifact condition and needs a fresh report.
