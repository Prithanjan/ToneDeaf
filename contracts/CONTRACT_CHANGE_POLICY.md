# Contract Change Policy — the two-key rule

**Applies to every file in `contracts/`.** Owner: **Pair A (Platform/Infra)**.
Binding rule: [rules.md](../rules.md) R-22, R-23, R-24, R-25, R-27.

> Why this file exists: `SIH26104_Phase1-3_Implementation_Runbook.md` §2 assigns `contracts/`
> to Pair A "with two-key review." Three pairs build against these four files in parallel. A
> unilateral change to a byte layout at 2 a.m. on Day 3 costs two other pairs a day.

---

## 1. What is under contract

| File | Governs | Broken by |
|---|---|---|
| [frame_contract.md](frame_contract.md) | WS frame bytes, window assembly, PCM→float32 | Pair A ↔ Pair C ↔ Pair B all three |
| [voice_scorer.proto](voice_scorer.proto) | Gateway ↔ Scorer gRPC | Pair A ↔ Pair B |
| [openapi.yaml](openapi.yaml) | REST + WSS JSON messages | Pair A ↔ Pair C |
| `OWNERS.md` | who reviews, who breaks ties | process |

`policy/policy.yaml` and `policy/calibration.json` are **not** in `contracts/` — they are
versioned artifacts with their own hashes, and changing them is a policy-version bump, not a
contract change. But a change to the *shape* of either (a new key, a new state, a new action)
is a contract change and lands here first.

---

## 2. The two-key rule

A `contracts/` PR merges only with:

1. **One Pair A approval** — the owner. Confirms the change is coherent with the wire format
   and the deployment tiers.
2. **One Pair B approval** — confirms the Scorer/ML side can honour it, and that no field
   creates a path for the detector to make a decision.
3. **One Pair C approval** — confirms the PWA/audit side can honour it, and that no field
   creates a path for PII or audio to cross the boundary.

Two of the three keys must come from different pairs. An A+A approval is not two keys.

**Tie-breaker:** named in [OWNERS.md](OWNERS.md). Resolving that name is human decision **H-1**
in [prd.md](prd.md) §9 and is a Phase-0 exit condition — the runbook §7 flags an unresolved
contract tie-breaker as a schedule risk, because it only surfaces during a disagreement, which
is exactly the moment you cannot afford to convene a meeting.

---

## 3. Every contract PR must contain

- [ ] **A version bump.** `contract_id` for the frame contract (`raw-waveform-v1` →
      `raw-waveform-v2`), `info.version` for OpenAPI, a comment block for the proto.
- [ ] **A compatibility note** in the PR body, stating explicitly which of the four categories
      in §4 the change belongs to.
- [ ] **A row appended** to the version-history table in the changed file.
- [ ] **Updated constants** in *both* mirrors if any number moved:
      `gateway/app/constants.py` and `pwa/src/lib/constants.ts`. The CI parity test compares
      them; it fails on divergence, and it must not be skipped to land a change.
- [ ] **Regenerated stubs** if `.proto` changed: `scripts/gen_proto.sh`, committed.
- [ ] **A `memory.md` entry** in the same commit (R-49). A contract change is precisely the
      class of decision the handoff log exists for.

---

## 4. Change categories

| Category | Examples | Requires |
|---|---|---|
| **Additive, backward-compatible** | new optional proto field with a fresh tag number; new optional OpenAPI response property; new `QualityFlag` enum member | two keys |
| **Additive, forward-affecting** | new `ReasonCode`; new `PurposeCode` (needs a `policy.yaml` action mapping or it has no defined behaviour) | two keys + policy update in the same PR |
| **Breaking** | frame size; endian order; window/hop size; renamed or retyped field; reused proto tag number; new hash-chain field | two keys + `contract_id`/version bump + coordinated merge at a sync point, never mid-day |
| **Prohibited** | `approve`/`deny` in any action enum; any audio, transcript, embedding, phone, or caller-name field; `purpose_code` or session history in `ScoreWindowRequest`; an action field in `ScoreWindowResponse` | rejected — see §5 |

**Proto tag numbers are never reused or renumbered.** Deleting a field means reserving its tag:
`reserved 6;`. Reusing a tag silently reinterprets bytes on a mixed-version deploy, which on this
project means a `raw_score` arriving where a `spoof_risk` was expected.

---

## 5. Changes that are rejected, not reviewed

These are not judgement calls; they are structural invariants, and the reason each is worth a
line here is that each looks locally reasonable in a PR diff:

1. **An action field on `ScoreWindowResponse`.** "The model already knows, let it say so." That
   collapses the detection/decision seam this whole architecture is organized around
   ([architecture.md](../architecture.md) §1).
2. **`purpose_code` or session history on `ScoreWindowRequest`.** "The Scorer could weight by
   risk." Then the Scorer is deciding, and the seam is gone.
3. **`approve` or `deny` in any enum.** R-07. The system produces verification pressure, never
   an authorization outcome.
4. **Any field carrying audio, PCM, waveform, transcript, embedding, phone number, MSISDN, or
   caller name** — in a request, response, WSS message, error string, or audit row. R-14, R-15.
5. **Coercion semantics.** "If the frame is 647 bytes, zero-pad it." R-24. Padding makes a wiring
   bug undetectable and destroys cross-tier parity checking.
6. **Widening `context_value_band` to a free string or an amount.** D-5. A closed enum is what
   keeps a currency value out of the audit table.

If one of these turns out to be genuinely necessary, the change is to the *rule* first — its own
PR in [rules.md](../rules.md) with a rationale — and to the contract second. Never both at once,
and never the contract alone.

---

## 6. Merge timing

Breaking changes merge **at a sync point only** — the four named in [phases.md](../phases.md) §7,
or an explicitly convened one. Not mid-workstream. The runbook builds the schedule around
scheduled integration moments precisely so a wire change doesn't arrive unannounced while two
other pairs have a working build.

Day 4 and Day 5: `contracts/` is **frozen** except for a demo-blocking defect. A freeze exception
needs the tie-breaker's approval and a `memory.md` entry naming what broke.

---

## 7. Review checklist for the non-owning reviewers

Pair B, when you review:

- Can the Scorer satisfy this while remaining stateless?
- Does any new field let the Scorer influence an action?
- Does any new field describe the *speaker* rather than the *window*? (R-41)
- If a number changed: does `ml/fixtures/contract_vector_v1.npy` need regenerating, and does the
  ONNX input shape still hold at `[1, 40960]`?

Pair C, when you review:

- Can the PWA produce/consume this without a second capture path?
- Does any new field carry a value a human typed? If yes, where is it pseudonymized?
- Does any new audit field require a `CHAIN_FIELD_SET_VERSION` bump? (R-27)
- Does the deny-list test still pass as an **exact** allow-list match?
