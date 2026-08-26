# Evaluation gate reports

Templates for the eight gates in `PS104_AI_Training_and_Evaluation_Playbook.md` §6.1. One file per
gate. Copy a template to `<gate>-<YYYYMMDD>-<commit>.md` in this directory, fill it in, and commit
it with the artifacts it judges. **The templates themselves stay blank.**

## Why these are templates and not a spreadsheet

A gate is a decision made in advance and applied afterwards. Written down beforehand, "we will
block deployment if parity is worse than the tolerance we declared" is a control; written down
afterwards, it is a description of whatever happened. Each template therefore separates
**predeclared criteria** (filled in *before* the run) from **results** (filled in after), and asks
for both in the same file so the gap between them is visible to a reviewer.

## No pre-filled numbers. Not one.

Every value cell in every template is `___`. There are no example figures, no "typical" ranges, no
placeholder EERs, and no illustrative latencies — because a number in a template survives being
copied. Someone fills in three cells, leaves the rest, and the untouched illustrative values are
read as measurements. `audit/tests/test_evaluation_templates.py` enforces the blankness
mechanically: a digit in a value cell, a percentage, a millisecond figure, or a multi-decimal
constant fails the test suite.

This is the same rule as `policy/policy.yaml`'s threshold and `policy/calibration.json`'s null
reliability metrics, applied to reports (rules.md R-01, R-02, R-03, R-04). Where a real number is
not yet known, the artifact says so in a form that cannot be misread as a measurement.

## The eight gates

| # | Gate | File | Blocks |
|---|---|---|---|
| 1 | Data | `gate-1-data.md` | Training |
| 2 | Baseline | `gate-2-baseline.md` | Feature work |
| 3 | OOD | `gate-3-ood.md` | Claim scope |
| 4 | Calibration | `gate-4-calibration.md` | Probability language, `policy_eligible` |
| 5 | ONNX parity | `gate-5-onnx-parity.md` | **DEPLOYMENT — deploy blocker** |
| 6 | Quantization | `gate-6-quantization.md` | The quantized artifact only |
| 7 | Privacy | `gate-7-privacy.md` | **RELEASE — release blocker** |
| 8 | Demo | `gate-8-demo.md` | Two-tier claims |

### The two blockers, and why they are different

**Gate 5 (ONNX parity) is a DEPLOY BLOCKER.** A failure stops the deployment artifact from
shipping. The failure it prevents: the exported graph scores differently from the model that was
evaluated, so every number in gates 2, 3, 4, and 6 describes a model that is not the one running.
Class-orientation flips are the worst case — the artifact still returns scores in `[0,1]`, the
system still produces actions, and the risk ordering is inverted. Nothing downstream looks wrong.
The PyTorch model may keep being evaluated after a parity failure; the ONNX artifact may not be
deployed.

**Gate 7 (Privacy) is a RELEASE BLOCKER.** A failure stops the release entirely — including the
demo, including a locally-hosted showing. It is broader than gate 5 because the harm is not
recoverable by rolling back: raw audio, a transcript, or an embedding that reached a log or an
export has already left, and deleting the row afterwards does not undo it. There is no partial
pass and no "ship it and fix the logging after the demo" (rules.md R-14, R-15, R-16).

The other six gates restrict what may be *claimed*, or which artifact may be used, rather than
halting the pipeline.

## Filling one in

1. Copy the template. Do not edit the template in place — the blank version is the control.
2. Fill §1 (what was run) and §2 (predeclared criteria) **before** running anything. Commit that.
3. Run. Fill §3 (results) and §4 (verdict) in a second commit, so the diff shows the criteria were
   fixed before the numbers existed.
4. A gate with a filled §3 and an empty §2 is not a gate. Reviewers should reject it.
5. `not-run` and `fail` are both legitimate statuses to commit. An unfilled gate reported honestly
   costs a claim; an unfilled gate reported as passing costs the project its evidence.

## Artifact state

Every gate report states the `artifact_state` it supports: `research_only`, `demo_eligible`, or
`policy_eligible` (playbook §8). No single gate promotes an artifact. `policy_eligible` requires
gates 1, 3, 4, 5, and 7 to have passed on the artifact in question, plus a derived threshold — see
`policy/README.md`. Today's committed bundle is `demo_eligible` and nothing here changes that: the
`derivation: placeholder` string in `policy/policy.yaml` and the
`placeholder-not-policy-eligible` status in `policy/calibration.json` hold the ceiling shut
regardless of what any report says.
