# Gate 1 — Data (TEMPLATE — NOT YET RUN)

| | |
|---|---|
| **Status** | `not-run` |
| **Blocking** | Training. Not a deploy or release blocker, but nothing downstream means anything without it. |
| **Owner (playbook §6.1)** | Data lead |
| **Pass condition (playbook §6.1, verbatim)** | Manifest validated; no split leakage; licence/consent present |
| **Failure response (playbook §6.1, verbatim)** | Stop training and repair provenance/splits |

Every other gate is conditional on this one. A leaked split does not produce a wrong number, it
produces a *plausible* number that cannot be distinguished from a good one by looking at it — and
it stays in the slide deck long after the leak is found.

## 1. What was run — fill BEFORE running

| Field | Value |
|---|---|
| Report id | `___` |
| Date (UTC) | `___` |
| Source commit | `___` |
| Manifest path | `___` |
| Manifest id | `___` |
| Manifest SHA-256 | `___` |
| Consent ledger SHA-256 | `___` |
| Validator command | `___` |
| Validator version / commit | `___` |

## 2. Predeclared criteria — fill BEFORE running

| Criterion | Threshold / rule | Value |
|---|---|---|
| Permitted straddling grouping keys | must be zero | `___` |
| Permitted duplicate `sha256_audio` across splits | must be zero | `___` |
| Permitted records with no consent basis | must be zero | `___` |
| Permitted records with a missing `retention_expiry` (non-licence basis) | must be zero | `___` |
| Minimum records per split | declare per split | `___` |
| Minimum distinct speakers per split | declare per split | `___` |
| Minimum distinct generator family+version in each eval split | declare | `___` |

## 3. Results — fill AFTER running

### 3.1 Schema validation

| Check | Value |
|---|---|
| Manifest validates against `datasets/manifest/manifest.schema.json` | `___` |
| Consent ledger validates against `datasets/consent_ledger/consent_ledger.schema.json` | `___` |
| Records validated | `___` |
| Schema errors | `___` |

### 3.2 Split leakage

The single check that matters: no `grouping.grouping_key_sha256` appears under more than one
`split`. See `datasets/manifest/manifest.schema.json` → `$defs.grouping.grouping_key_sha256`.

| Check | Value |
|---|---|
| Distinct grouping keys | `___` |
| Grouping keys appearing in more than one split | `___` |
| Grouping keys whose recomputed digest disagrees with the declared one | `___` |
| `sha256_audio` values appearing in more than one split | `___` |
| Records whose `root_sample_id` does not resolve to a depth-0 record | `___` |
| Records with `derived_from_sample_id` set and `augmentation_depth` of zero | `___` |
| Speakers appearing in both `train` and any `eval_*` split | `___` |
| Generator family+version appearing in both `train` and `eval_generator_heldout` | `___` |
| Codec chains appearing in both `train` and `eval_codec_language_heldout` | `___` |
| Speakers or sessions shared between `demo` and any other split | `___` |

### 3.3 Split composition

| Split | Records | Bona fide | Spoof | Distinct speakers | Distinct generators | Languages |
|---|---|---|---|---|---|---|
| `train` | `___` | `___` | `___` | `___` | `___` | `___` |
| `dev_calibration` | `___` | `___` | `___` | `___` | `___` | `___` |
| `eval_locked` | `___` | `___` | `___` | `___` | `___` | `___` |
| `eval_generator_heldout` | `___` | `___` | `___` | `___` | `___` | `___` |
| `eval_codec_language_heldout` | `___` | `___` | `___` | `___` | `___` | `___` |
| `demo` | `___` | `___` | `___` | `___` | `___` | `___` |

### 3.4 Licence and consent

| Check | Value |
|---|---|
| Upstream corpora pinned to an exact revision | `___` |
| Corpora with a named person accepting access terms | `___` |
| Records with `consent_basis` of `public-corpus-license-only` | `___` |
| Records requiring a ledger record | `___` |
| ...of those, records with a matching ledger record | `___` |
| Ledger records whose declared basis disagrees with the manifest | `___` |
| Ledger records whose `retention_expiry` disagrees with the manifest | `___` |
| Withdrawn ledger records | `___` |
| Withdrawn records with surviving manifest samples (must be zero) | `___` |
| Records past their `retention_expiry` (must be zero) | `___` |
| Records with `synthetic_cloning_permitted` false used as a cloning source (must be zero) | `___` |
| Records with `accent_region_source` other than self-reported or dataset metadata | `___` |

### 3.5 Field hygiene

| Check | Value |
|---|---|
| Manifest fields outside the schema's closed property set | `___` |
| Free-text `notes` values tripping the §5.2 forbidden-substring list | `___` |
| Identifier-shaped values found in any free-text field | `___` |

## 4. Verdict

- [ ] **PASS** — every must-be-zero row is zero and every predeclared minimum is met.
- [ ] **FAIL** — stop training and repair provenance/splits. Record what was repaired below and
      re-run this gate; do not amend this report in place.
- [ ] **NOT RUN**

| Field | Value |
|---|---|
| Artifact state supported | `___` |
| Signed off by (data lead) | `___` |
| Date | `___` |

**Findings and repairs:**

`___`

## 5. What this gate does not establish

- It does not show the data is *sufficient*, only that it is described correctly and disjointly.
- It cannot see leakage that the grouping keys do not encode. Two different recordings of the same
  person under two upstream speaker ids hash to two different `speaker_id_hash` values and will
  pass every check here. Cross-corpus speaker overlap is a manual review item, not a script.
- It cannot see whether consent was *informed*. It checks that a basis and a form digest are
  recorded, not that the wording was honest or understood.
- It says nothing about the model. A perfect data gate on a broken split *definition* is a
  correctly-validated leak.
