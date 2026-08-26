<!--
  Pull request template — SIH26104 Voice Integrity Control Plane.

  This asks for six short pieces of WRITING and almost nothing tickable, deliberately. A checklist of
  boxes gets ticked in one pass without being read; a question that needs a sentence either gets an
  answer or gets visibly left blank, and a visibly blank field is something a reviewer can point at.

  Delete any section that genuinely does not apply, and say so — "n/a, no contract change" is an answer.
  An untouched template is not.
-->

## What and why

<!-- Two or three sentences. What changes, and what breaks if it does not land. -->

## Paths and pair

<!--
  Which pair owns what you touched (contracts/OWNERS.md): A = platform/infra/contracts/gateway,
  B = ml/scorer/calibration, C = pwa/audit/policy.yaml. If you touched more than one pair's paths, say
  which, because the reviewer you need is decided by that and by nothing else. CODEOWNERS is currently
  INERT (human decision H-1 — see .github/CODEOWNERS), so this line is the actual routing mechanism
  right now, not a formality.
-->

- Pair:
- Paths touched:

## Contract change (rules.md R-22, R-23)

<!--
  Answer this even to say no. A `contracts/` change needs TWO approvals, and the second one is not
  whoever is online — it is the pair on the other side of the wire:

    frame_contract.md    A + B + C   (all three consume it)
    voice_scorer.proto   A + B       (+ C if an audit field changed)
    openapi.yaml         A + C       (+ B if VersionInfo or artifact state changed)

  The conditional halves (the "+ B" / "+ C" above) depend on the CONTENT of the diff, which CODEOWNERS
  cannot see. They are your declaration here and nowhere else.

  R-23: one definition per language. If a constant moved, it moved in ONE place per language and the
  others now read it. If you copied a number, `contract-test` check C-04 will catch the copy — but only
  for constants already in the frame contract table.
-->

- [ ] No files under `contracts/` changed, and no constant in `frame_contract.md` section 1 changed.
- [ ] `contracts/` changed. Second/third key required from: ______ . Both wire sides updated in this PR
      (regenerated stubs committed, `openapi.yaml` and the client's types, or a stated reason why not).

## What I did NOT verify

<!--
  MANDATORY. rules.md R-52 — record what was dropped and why, at the moment of dropping it. This is the
  most useful field in this template and the one most worth resisting the urge to leave empty.

  "I ran the unit tests but never started the stack." "The GPU path is untested; I only have CPU."
  "The migration was written but never applied to a database with rows in it." "I changed the timeout but
  did not test the timeout firing."

  This is not a confession. It is the thing that stops a reviewer assuming coverage that does not exist,
  and it is the thing that stops a number from this PR being quoted next week as if it were measured.
-->

## Cost (rules.md R-28 .. R-36)

<!--
  Nothing in CI starts GPU spend: `deploy-runtime.yml` is `workflow_dispatch`-only and it is the single
  place with `update-auto-scaling-group` and `update-service --desired-count` in it. If this PR adds a
  `push:`, `schedule:`, `release:`, or `repository_dispatch:` trigger to that file — or relaxes the
  `github.ref == 'refs/heads/main'` guard on a build job — say so here in words, because that converts a
  routine merge into a billable event and it is not visible from a diffstat.
-->

- [ ] This PR does not add any trigger to `deploy-runtime.yml` and does not add ECS/ASG scaling calls to
      any other workflow.
- [ ] If I started a runtime while working on this: **I ran `stop-runtime.yml` and it reported VERIFIED
      STOPPED.** (Not "I zeroed desired count" — `min-size` undoes that in seconds, R-31. The workflow
      reads the state back; a `desired-count 0` call on its own is not evidence.)

## Privacy (rules.md R-14 .. R-19) — a failure here is a release blocker

<!--
  `privacy-check` runs `scripts/privacy_scan.py` plus the `privacy` marker suites, so a mechanical
  violation should already be red before a human reads this. What the scanner cannot judge is intent, so
  answer if ANY of these is true — a "yes" is fine, it just needs a sentence:

    * a new log line, metric label, span attribute, or exception message
    * a new database column, or a new field in a client-facing response
    * a new file write, temp file, buffer, or cache anywhere on a request path
    * anything that touches `client_call_ref`

  The three failure shapes worth naming: a raw `client_call_ref` reaching a log or a query (R-16); an
  error message interpolating caller input, which turns the error channel into a data channel (R-17);
  PCM reaching anything durable (R-14). The audit table is append-only and hash-chained, so a raw
  identifier written there cannot be deleted later without breaking every subsequent event hash.
-->

- Any of the above? (yes + one sentence, or "no"):

## Parity set (rules.md R-51, R-56)

<!--
  The parity set (architecture.md 5.1) is: git commit, app source, proto hash, OpenAPI hash, migration
  set, policy bundle hash, model ONNX SHA-256, calibration SHA-256, contract-test suite. If this PR
  changes ANY member, every previously recorded run is now describing a different system, and a
  measurement taken before this merge cannot be compared with one taken after.

  Say which member changed. Not to create work — to stop a chart from silently mixing two systems.
-->

- Parity-set members changed (or "none"):

## Numbers (rules.md R-01, R-45, R-47)

<!--
  Only if this PR's description, a commit message, or a doc it edits states a latency, accuracy, or
  throughput figure. Every such figure needs the named host it was measured on and the execution provider
  it ran under. A CPU number is not a slower GPU number, it is a different number: a Scorer that fell
  back to CPUExecutionProvider comes up healthy and reports plausible latencies that mean nothing for the
  GPU tier (R-45).

  If a number in here is an estimate, a target, or something someone said, label it as one (R-01).
-->

- Host / provider for any figure quoted above (or "no figures quoted"):

---

## Required checks

<!--
  These four are the required contexts on `main` (R-57). They are listed so a reviewer knows a
  "waiting" state is a real blocker and not a flake, and so a red one is not merged around.

    contract-test   frame contract + constants mirror + proto/OpenAPI hashes + the `contract`/`parity`
                    suites. Red here means two components disagree about the wire, which shows up at
                    runtime as a confusing bug rather than as a type error.
    privacy-tests   RELEASE BLOCKER. Not a lint warning, and not something to fix in a follow-up.
    secret-scan     A hit means ROTATE FIRST, then remove. Deleting the line does not un-publish the
                    value — it is in the ref history and in every clone and cache.
    gateway-ci / scorer-ci / pwa-ci as applicable to the paths you touched.

  An admin override on any of these needs a line in memory.md saying who overrode what and why. A
  bypass with no record is indistinguishable from the check never having existed.
-->

- [ ] All required checks green, or a named reason each red one is safe to merge.
