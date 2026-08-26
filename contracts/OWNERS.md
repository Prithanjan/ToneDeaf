# OWNERS — contracts/

Review requirements: [CONTRACT_CHANGE_POLICY.md](CONTRACT_CHANGE_POLICY.md).
Team structure: `SIH26104_Phase1-3_Implementation_Runbook.md` §2.

---

## Pairs

| Pair | Scope | Owns in this repo |
|---|---|---|
| **A** | Platform / Infra | `contracts/`, `gateway/`, `infra/`, `.github/workflows/` |
| **B** | AI / ML | `ml/`, `scorer/`, `policy/calibration.json`, `evaluation/` |
| **C** | Integration & Audit | `pwa/`, `audit/`, `policy/policy.yaml`, `docs/runbooks/` |

`contracts/` is **owned by Pair A** and changed only under the two-key rule.

---

## Fill this in before Phase 0 is complete

> ⛔ **Unresolved: human decision H-1.** The runbook §7 names this as one of two decisions that
> cannot be derived from any document and must be made by the team. It is listed as a Phase-0
> exit condition in [phases.md](../phases.md) §2 for a specific reason: an unnamed tie-breaker
> only becomes a problem during a disagreement, and a disagreement is when there is no time to
> hold a meeting about who decides.

| Role | Name | GitHub handle |
|---|---|---|
| Pair A — lead / contract owner | _TBD_ | _TBD_ |
| Pair A — second | _TBD_ | _TBD_ |
| Pair B — lead | _TBD_ | _TBD_ |
| Pair B — second | _TBD_ | _TBD_ |
| Pair C — lead | _TBD_ | _TBD_ |
| Pair C — second | _TBD_ | _TBD_ |
| **Contract tie-breaker (H-1)** | **_TBD — decide first_** | _TBD_ |
| Cost owner (runs `stop-runtime`, R-30) | _TBD_ | _TBD_ |
| Demo laptop owner (H-4, named host for p95 per R-47) | _TBD_ | _TBD_ |

The **cost owner** and the **tie-breaker** should not be the same person. The cost owner's job
runs at the end of every working session; the tie-breaker's job runs during arguments.

---

## Per-file review routing

| File | Owner | Additional keys required |
|---|---|---|
| `frame_contract.md` | Pair A | Pair B **and** Pair C (all three consume it) |
| `voice_scorer.proto` | Pair A | Pair B, plus Pair C if an audit field changes |
| `openapi.yaml` | Pair A | Pair C, plus Pair B if `VersionInfo` or artifact state changes |
| `CONTRACT_CHANGE_POLICY.md` | Pair A | tie-breaker |
| `OWNERS.md` | Pair A | tie-breaker |

---

## Escalation

1. Reviewers disagree → 15-minute timeboxed discussion at the next sync point
   ([phases.md](../phases.md) §7).
2. Still unresolved → **tie-breaker decides**, and records the decision plus the losing argument
   in [memory.md](../memory.md). Recording the rejected option matters as much as the choice: the
   next agent or teammate who hits the same fork needs to know it was already considered.
3. The decision stands for the five-day window. Reopening it requires new evidence, not a new
   opinion.
