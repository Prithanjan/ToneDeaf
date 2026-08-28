# Memory — Working Ledger and Handoff State

**Status:** Living document. Updated on every material change.
**Companions:** [prd.md](prd.md) · [architecture.md](architecture.md) · [technical-design.md](technical-design.md) · [design.md](design.md) · [phases.md](phases.md) · [rules.md](rules.md) · [aws-setup-instructions.md](aws-setup-instructions.md)
**Last updated:** 2026-08-26

---

## 0. What this file is for, and the rule for writing in it

The other six documents say what the system *should* be. This one says what is **actually true right
now**, what was **decided and why**, what was **found broken**, and what is **still unverified**. It
exists so that a person or agent picking this up cold does not have to re-derive the state of the world
from the code, and so that I can catch my own drift.

**The update protocol — three obligations, in priority order:**

1. **Record what is verified separately from what is written.** A file existing is not evidence it
   works. §5 holds only claims backed by a command that was actually run, with its output. Everything
   else lives in §6 as unverified. This separation is the single most useful property of this file: it
   is what stops a written-but-unrun artifact from being reported as done ([rules.md](rules.md) R-01).
2. **Record decisions with the alternative that was rejected.** A decision without its rejected
   alternative gets silently re-litigated by the next person, who will pick the other branch because
   nothing told them it had been considered. See §2.
3. **Record deviations from the spec as deviations,** not as the new spec. If the code disagrees with
   [technical-design.md](technical-design.md), one of them is wrong and §3 says which, so the next
   reader does not assume the code is authoritative just because it is executable.

Append to §12 (changelog) on every change. Do not rewrite history in place — a corrected entry with its
correction visible is more useful than a clean entry that hides that something moved. Where an entry is
superseded (E-2 is the example), keep the original claim visible and say what replaced it: the *reason* a
gap existed is usually more reusable than the fix.

---

## 1. Current state at a glance

| Area | State | Evidence |
|---|---|---|
| Root documents | 8 of 8 exist, **all now on-topic** | `prd`, `phases`, `technical-design`, `design`, `rules`, `architecture`, `memory`, `aws-setup-instructions` |
| `contracts/` | Frozen skeleton landed | `openapi.yaml`, `voice_scorer.proto`, `frame_contract.md`, `OWNERS.md`, `CONTRACT_CHANGE_POLICY.md` |
| Gateway source | Modules written; **the serving stack now imports and runs under test** | §5 E-5. `app/scorer/client.py` still needs generated stubs for a *real* Scorer call (§6 U-1) |
| Gateway tests | **330 passing** on the pinned dependency stack (including metrics schema & diagnostics advisory suites) | §5 E-1, E-10 |
| Gateway packaging | `Dockerfile`, `.env.example`, `pyproject.toml` written | Docker build never run (§6 U-2) |
| WSS negative-contract suite | ✅ **CLOSED** — **55** tests carry the `contract` marker | §5 E-2. Phase 1 exit criterion met, and it found two real defects (§4 BUG-5) |
| `design.md` (visual) | ✅ Written, grounded in the CSS that exists | §5 E-6 |
| Scorer | **309 passing** — 23 files (including `test_calibration_artifact.py` 17 tests) | §8, §5 E-10 |
| PWA | **Reviewed** — 28 files. Already built before the review: `node_modules/` and `dist/` predated it and rebuilt to byte-identical hashes | §8. `typecheck`, `build`, `lint` all exit 0 |
| Infra (Compose + CDK + IAM) | ✅ **Complete — 22 files.** `tsc` clean, all six stacks synthesize, cost guardrail verified in **both** directions, deploy policy renders + validates + covers every real workflow call | §5 E-7, E-8 |
| **`docker compose up`** | ✅ **Scorer startup unblocked** (§4 BUG-8 fixed: four missing keys added to `policy/calibration.json`, zero code changed, verified by executing the real loader). **Still never run end-to-end** — Postgres, migrations, and the Gateway↔Scorer gRPC path are untested together | §4 BUG-8, §9 H-6 |
| CI/CD | ✅ **Delivered & gated — 11 files (9 workflows, `CODEOWNERS`, PR template)** | §8. `audit-ci.yml` added (passed-count floor 455, selects `-m "not integration"`), 477 unit tests covered on commit |
| Audit migrations, policy bundle | ✅ **477 unit tests passing** (12 integration deselected). Schema layer is clean, DSN normalized (BUG-19), enum closed (BUG-14), whole-session retention atomic (BUG-13), anchor verifier supported (BUG-11 / H-7) | §5 E-10, §4 BUG-11..BUG-19 |
| `evaluation/` | **Reviewed** — 9 files | §8. `validate_manifest.py:240` reads the wrong key, so leakage checks pass vacuously |
| `ml/`, `datasets/`, `scripts/` | 2 / 4 / 8 files | §8 |
| `docs/` | ⛔ **0 files** — the only directory still empty | §8 |
| AWS account | **Nothing provisioned.** No CDK bootstrap, no ECR, no secrets | §7 |

**File counts above were measured**, not carried forward — `find` excluding `node_modules`,
`__pycache__`, `cdk.out`, `dist`, `.venv*` and `*.pyc`. Several rows had drifted badly (CI/CD was
recorded as 0 files while I was actively reading its workflows), so re-measure before trusting this
table rather than editing a number you assume is close.

**The honest one-line summary:** Phase 0 repo scaffolding is substantially done, the Gateway's logic
*and its WebSocket serving path* are tested and green on the pinned stack, `infra/` synthesizes six
CloudFormation templates with a proven-non-vacuous zero-cost guardrail, and four parallel reviews have
now read `scorer/`, `pwa/`, `audit/`, `policy/` and `evaluation/` — closing most of what §8 U-10 called
the largest unquantified risk. Nothing has ever run as an integrated system and **no AWS resource
exists**; every infra claim below is a claim about generated templates and rendered policy documents,
not about anything deployed.

**And the one thing a cold reader will get wrong from the table above.** The green rows are all
*per-component*, and the defects that matter most live in the **seams** — between components built by
different agents, tested against different fixtures, and each internally consistent. Two of the three
worst findings in this file are of exactly that shape:

- **BUG-8** (fixed): the Gateway and the Scorer read *disjoint key sets* from one shared file, and the
  placeholder coefficients `(1.0, 0.0)` are the single point where both conventions produce the same
  numbers while meaning different things — so two rigorous test suites were both green while asserting
  incompatible semantics, and the defect was scheduled to become a silent numerical error the moment
  a real calibration landed.
- **BUG-11** (open, ⛔ **read this before trusting any audit claim in this repo**): the hash chain
  detects edits, reordering, and interior deletions, and does **not** detect truncation from the tail or
  deletion of every row. The evidence layer that the entire "persistent evidence" product claim rests on
  returns `ok=True` for an empty table. It is not a coding slip — `verify_chain` correctly answers the
  question it was written to answer, which is *"is this a valid prefix?"* rather than *"is this
  complete?"*

**A green per-component check is not evidence about the system**, and neither is a green test suite
whose fixtures were written by the same author as the code (BUG-18), nor one that never executes
(BUG-12). Prefer, in this order: a command run here, a fixture that reads the real committed artifact,
then a test.


---

## 2. Decision log

D-1 … D-12 are recorded in full, with rationale, in
[technical-design.md](technical-design.md) §1 — that table is the authority and is not duplicated here.
Summarised: D-1 PCM int16 **little**-endian; D-2 sequence header `uint64` **big**-endian (the
disagreement is deliberate); D-3 WSS binary frame exactly **648 bytes**, never coerced; D-4
`purpose_code` bound at `POST /api/v1/sessions` and verified to match on `session.open`; D-5
`context_value_band` a closed enum; D-6 stream ticket single-use, 60 s, bound to `session_id` + `sub`;
D-7 `tenant_id` present from Phase 1; D-8 Alembic adopted in Phase 1; D-9 explicit ordered hash-chain
field list, never `SELECT *`; D-10 Python 3.12 pinned; D-11 `0.78` is a **placeholder** threshold; D-12
diagnostics return value discarded at the call site.

Decisions taken *after* that table was written are recorded here in full.

### D-13 — `design.md` is the visual design system; the engineering spec moved to `technical-design.md`

**Decision.** `design.md` was written as a low-level engineering specification. That was wrong: it was
always intended to be the *visual* design document — colour, typography, spacing, components. The
engineering content was moved verbatim to `technical-design.md` with **all section numbers preserved**,
and `design.md` was rewritten as the visual design system.

**Why the section numbers were preserved rather than tidied.** Roughly two dozen source files cite this
document by section from their docstrings — `# design.md section 2.5`, `§4.1`, `§5.3`, `§9`. Renumbering
would have silently invalidated every one of those citations, and a citation that points at the wrong
section is worse than no citation, because it is trusted. Only the filename in the citation moved; all
24 references were repointed mechanically and verified to leave no bare `design.md` reference in code.

**Rejected alternative:** folding the engineering content into
[architecture.md](architecture.md). Rejected because `architecture.md` is deliberately the high-level
document — planes, boundaries, deploy topology — and merging a 400-line low-level spec into it would
have destroyed the one property that makes it useful, which is that it can be read in ten minutes.

**Consequence to watch:** two documents now begin with the word "design" and a reader can pick the wrong
one. Both headers state which is which, and the nav lines cross-link.

### D-14 — CDK: five dependency-ordered stacks **plus** one standalone `CostSafetyStack`

**Decision.** `NetworkStack → DataStack → SecretsStack → ComputeStack → EdgeStack` is the dependency
chain. `CostSafetyStack` is a **sixth stack file** with no position in that chain, and the *deploy*
instruction is to stand it up **immediately after `DataStack`**.

**Why this needed a decision at all.** The source documents conflict, and so did my own earlier drafts:

- My earlier `architecture.md` / `phases.md` / `aws-setup-instructions.md` recorded **six stacks in one
  forced chain** with `CostSafetyStack` third.
- The newer `Part-2 (Claude Scoped)` plan describes **five** ordered stacks and calls `CostSafetyStack`
  standalone — and is internally inconsistent about when to deploy it: its file list says "deploy
  anytime after data-stack", its prose says "immediately after DataStack", and its own command listing
  places it *after* `ComputeStack`.

**Why I resolved it toward "early".** The entire purpose of the cost-safety plane is to exist before
anyone can turn the runtime on. Deploying it after `ComputeStack` leaves a window in which GPU capacity
is deployable with no budget backstop at all — which is precisely the failure the stack exists to
prevent. Making it standalone (rather than a chain link) is also correct: a hard dependency would mean a
`CostSafetyStack` failure blocks `SecretsStack`, and a cost guardrail that can block a deploy is a
guardrail people will delete.

**⚠️ This is a reconciliation of a conflict in the source material, not a fact read out of it.**
A human should confirm it. Flagged as such in the affected docs.

### D-15 — `memory.md` records verified evidence separately from written artifacts

Covered by §0 above. Recorded as a decision because it changes what "done" is allowed to mean in this
repo, and because the temptation to collapse §5 into §1 will recur.

---

## 3. Deviations from the written specification

**DEV-1 — `gateway/app/session_registry.py` is not in the `technical-design.md` §4.1 module layout.**

The module exists and is used, but §4.1's module table does not list it. It appeared because session
state — the ring buffer, the replay cache, the per-session hash-chain head — needs an owner, and the
alternative was scattering that state across `ws/stream.py` and `policy/engine.py`, which would have
made the single-worker constraint (below) an implicit property rather than a locatable one.

**The spec is the thing that is out of date here, not the code.** §4.1 should gain the module. Until it
does, this deviation is why a reader comparing the two will find a file they cannot account for. The
file's own docstring points here.

**DEV-2 — the Gateway must run with exactly one Uvicorn worker, and this is load-bearing.**

`CMD` in `gateway/Dockerfile` pins `--workers 1`. Session state, the ticket replay cache, and the
per-session audit chain head are all in-process. A second worker would fork the hash chain: two workers
would each hold a different "previous event hash" for the same session, and the resulting audit trail
would fail verification with no bug anywhere in the chain code. This is a correctness constraint
disguised as a performance setting, which is exactly the kind of thing that gets "optimised" later —
hence recording it here as well as in the Dockerfile comment.

---

## 4. Defects found and fixed

These were found by writing tests and by reading, not by the test suite failing on its own. Recorded
because each one is a class of mistake likely to recur.

**BUG-1 — the audit deny-list was unreachable dead code. (Real security-control defect.)**

In `gateway/app/audit/chain.py`, the `_FORBIDDEN_SUBSTRINGS` loop ran *after* the "unknown field" check.
Every forbidden name is by construction absent from `CHAIN_FIELDS`, so a forbidden field was always
caught by the unknown-field check first and the deny-list could **never fire**. The event was still
rejected, so nothing was insecure in practice — but the error said "unknown field", and a deny-list that
cannot report a deny-list violation is one that will not survive a privacy review.

Fixed by extracting `_assert_not_forbidden()` and running it **first**, and additionally by scoping it
over `CHAIN_FIELDS` and `EXCLUDED_FIELDS` **at import time** — so the genuinely dangerous change
(someone adding `audio_blob` to `CHAIN_FIELDS` *and* the matching migration in one commit) now fails to
import rather than passing review. Covered by `test_deny_list_beats_the_unknown_field_check` and
`test_canonical_field_list_itself_is_deny_listed`.

**Generalisable lesson:** a control that is ordered after a broader check is not a control. Test that a
control fires *for its own reason*, not merely that the bad input is rejected.

**BUG-2 — `gateway/Dockerfile` escaped its build context.**

`COPY ../contracts/openapi.yaml` cannot work; Docker forbids paths outside the context. The Gateway
hashes `contracts/openapi.yaml` and `contracts/voice_scorer.proto` into the parity set, so it genuinely
needs them. Fixed by making the **repo root** the build context:

```bash
docker build -f gateway/Dockerfile -t sih26104/gateway:$GIT_SHA .
```

The rejected alternative — copying the contract files into `gateway/` at build time — would have created
a second copy of a file under a two-key review rule, which is how two contract versions come to exist.

**Anything else that needs those contract files must use the same context.** Noted because the Scorer
also hashes the proto.

**BUG-3 — `--ws-max-size 1024` would have silently defeated the Phase 1 exit criteria.**

Uvicorn would have transport-closed an oversized frame with code 1009 *before application code ran*,
while the negative-contract suite asserts **our** close code and reason. The test would have failed for
a reason unrelated to the behaviour under test. Raised to `65536`: a memory-exhaustion backstop set
deliberately **above** the application's own rejection threshold, so the application is always the thing
that rejects.

**Generalisable lesson:** a transport-layer guard set tighter than an application-layer guard makes the
application-layer guard untestable.

**BUG-4 — two tests asserted the wrong thing and I initially believed the code was wrong.**

`test_ineligible_windows_are_skipped_not_counted_low` expected `HIGH` after 3 eligible windows; with
`n=5` the deque is not yet full, so the engine was right. And
`test_no_error_message_contains_the_input[""]` was **vacuous** — `"" in anything` is always true — so it
could only ever fail. Both rewritten. Recorded because "the test failed, therefore the code is broken"
was wrong twice in a row here.

**BUG-5 — two R-07 violations in client-visible strings in `gateway/app/ws/stream.py`. (Real.)**

Found by the first run of the new negative-contract suite (§5 E-2), by
`TestCloseCodeTables::test_no_action_vocabulary_leaks_into_a_close_reason`:

1. `CLOSE_REASONS["AUTH_TICKET_INVALID"]` was `"stream ticket rejected"` → now `"stream ticket not valid"`.
2. `_close()` sent `{"message": CLOSE_REASONS.get(code, "rejected")}` — the **fallback** for an app code
   with no `CLOSE_REASONS` entry → now `"stream closed"`.

Both put `reject` in front of the client, which R-07 forbids in *any* client-visible string. The second
is the more interesting one: it is the fallback path, so it would have been the default for every
newly-added app code until someone remembered to add a reason — a violation with a built-in mechanism
for reintroducing itself.

**This was BUG-4's trap presenting itself again, and I checked rather than assumed.** Before editing I
confirmed the test was right and the code was wrong: `scripts/privacy_scan.py`'s deny-list covers audio
and PII column names, not action vocabulary, so nothing existing would have caught this; the string was
mirrored nowhere (`pwa/src/`, `contracts/*.yaml` both clean); and a repo-wide grep found exactly these
two sites. Only then did I change production code.

**Generalisable lesson:** R-07 was being enforced on enums, schemas, and `CHECK` constraints — the
places where the vocabulary is *structured*. It was not being enforced on prose sent to clients, and a
close reason is recorded by proxies and browsers. A vocabulary ban has to cover the strings, not just
the types.

**BUG-6 — two defects in my own test code, caught before they landed. (Recorded for the lesson.)**

1. **A drain loop that would have hung CI, not failed it.** `while True: socket.receive()` looks
   correct, but Starlette's `WebSocketTestSession.receive()` returns the `websocket.close` message
   **without raising** — so the *next* call blocks forever on an empty queue. A hanging job burns the
   runner and reports nothing; a failing job reports a defect. Fixed with `_drain_to_close()`, which
   inspects `message["type"]` and returns on `websocket.close`.
2. **An assertion too coarse to detect the bug it was for.** Asserting only the WebSocket close code is
   insufficient because `PROTO_FRAME_SIZE`, `PROTO_SEQUENCE`, and `PROTO_FIRST_MESSAGE` **all** close
   `1003` — a handler that confused the three would have passed. Fixed by `_assert_closed_with()`, which
   also asserts the app code carried on the last `{"type": "error"}` frame.

**Generalisable lesson for anyone extending that suite:** where several app codes share a transport
close code, the transport code is not the assertion. Assert the app code.

**BUG-7 — `infra/compose/docker-compose.yml` gave the Gateway a DSN asyncpg rejects. (Real, fixed.)**

Both the `gateway` and `migrate` services set `DATABASE_URL: postgresql+asyncpg://…`. The `+driver`
suffix is *SQLAlchemy dialect* syntax and the Gateway's serving path does not go through SQLAlchemy — it
calls `asyncpg.create_pool()` directly (`gateway/app/main.py:153`). asyncpg 0.30.0 validates the scheme
itself and raises `ValueError` on anything but `postgresql` / `postgres`, so this was a **hard startup
crash**, not a warning. Verified by installing `asyncpg==0.30.0` into `.venv-ws` and reading its own
validation source rather than trusting documentation.

Fixed to `postgresql://` in both services (`:95`, `:215`). The `migrate` service keeps the *same* value
deliberately: alembic normalizes any sync scheme **upward** to `postgresql+asyncpg://` and logs that it
did (`audit/migrations/env.py:57`, regex `:42` — confirmed by executing the regex against all three
forms), so passing the plain scheme exercises that normalization on every local run instead of leaving it
as untested code.

`postgresql://` is a **fixed point, not a compromise.** Three consumers, two of which already defended
against the wrong form in *opposite* directions: alembic adds the suffix, and
`audit/retention_worker.py:559` strips it back off before its own `asyncpg.connect()`. Two independent
authors each anticipated this and neither wrote it down where the third would look.

Corrected the same misconception in four more places, because it had propagated as prose:
`aws-setup-instructions.md` §6 (both the create and the rotate command — I had *written* those strings
minutes earlier and then found this), `infra/cdk/lib/secrets-stack.ts:28`,
`infra/cdk/lib/compute-stack.ts:47` and `:369`, all of which described the secret as "a full SQLAlchemy
URL."

⚠️ **The red herring that makes this look wrong is real:** `gateway/requirements.txt:20` genuinely does
pin `sqlalchemy==2.0.36`, so grepping the dependency list appears to confirm the dialect form. It is
there because `alembic==1.14.0` requires it and **the Gateway image doubles as the migration image** —
not because any request path imports it. Nothing under `gateway/app/` does; `main.py:224` even reads the
`alembic_version` table with raw SQL through asyncpg rather than reflecting it. My first three comments
flatly said "there is no SQLAlchemy," which is false and would have been the sentence that got the
comment distrusted. All now say *"not in the serving path"* and name the requirements.txt trap directly.

**BUG-8 — `policy/calibration.json` cannot be loaded by the Scorer at all. ⛔ BLOCKING, NOT FIXED —
needs a human decision (see §9).**

Found by the agent dispatched to bake the artifact into `scorer/Dockerfile`; it **correctly refused to
make the change** and reported why. I verified the core claim myself by reading both files rather than
accepting the report.

`scorer/app/calibration.py::load_calibration` requires **top-level** `fitted_on`, `method == "platt"`,
`slope`, and `intercept`. The committed artifact has none of them — it uses `method: "platt-scaling"`,
nested `platt.a` / `platt.b`, and `fit.fitted_on_split: null`. Validation order puts `status` first
(`:160`, which passes) and `fitted_on` second (`:167`), so the first failure is
`CalibrationError: calibration artifact missing required key: fitted_on`.

**The divergence is deeper than key names, and this is the part the agent did not name.** The artifact
declares its transform as `sigmoid(a · logit(raw_score) + b)` (`calibration.json:15`) — Platt on the
*logit* of the raw score. `Calibration.apply()` computes `sigmoid(slope · raw + intercept)`
(`calibration.py:121`) — Platt on the raw score *directly*. Those are different functions. The artifact's
"identity transform" claim holds only under its own definition (`sigmoid(logit(p)) == p`); under the
Scorer's, `sigmoid(raw)` is **not** the identity unless `raw` is already a logit. So this is a
design-level conflict between two authors about what a raw score *is*, not a typo, and picking a side
silently would change every number the detector ever reports.

**Three consequences, in order of who gets hurt first:**

1. **`docker compose up` is broken right now.** The `../../policy:/policy:ro` mount already makes the
   file present at `CALIBRATION_PATH`, and `scorer/app/server.py:201-207` only falls back to
   `placeholder_calibration()` when the file is **absent**. Present-but-malformed is fatal *even in mock
   mode*. The Scorer's absence-fallback is currently load-bearing in a way nobody intended.
2. **Baking the file in would have propagated that crash to both tiers**, including the billing GPU. This
   is why the refusal was right, and it is a good argument for the instruction "report, don't force."
3. **No test would have caught it.** `scorer/tests/conftest.py:80-85` builds a synthetic *flat* fixture,
   so no test ever loads the real committed artifact. The Gateway loads the same file successfully
   because `gateway/app/policy/loader.py:129` reads only `calibration_version`, `status`,
   `model_version`, `model_sha256` — it never touches the transform. Two services, one file, two
   schemas, green CI.

Do **not** fix this by reformatting the artifact as a drive-by: its own `hash_discipline_note` warns that
its bytes are hashed into `calibration_sha256` and stamped into every audit row. Right now that costs
nothing (no audit rows, no commits), which makes this the cheapest moment it will ever be — but it is
still a two-way decision about which definition of the transform is authoritative. Logged as **H-6**.

**BUG-9 — the ECS Scorer task definition cannot start, for a reason unrelated to BUG-8. (Real, not
fixed — see §9 H-6.)**

`infra/cdk/lib/compute-stack.ts:446,457` set `DEPLOYMENT_PROFILE: 'aws-gpu'` together with
`DETECTOR_MODE: 'MOCK_SMOKE_MODE_NOT_A_DETECTOR'`. `scorer/app/config.py:132-142` raises `ConfigError`
on exactly that pair, by design: R-32 permits one `g4dn.xlarge` for the whole window and spending it to
run a deterministic hash produces latency numbers that describe the hash. Verified by reading the
validator.

**The config check is correct; the task definition is the thing that is wrong.** The honest reading is
that *the GPU tier cannot be deployed until a real ONNX artifact exists* — which is true and worth
stating out loud rather than encoding a combination that crash-loops. Nothing is billing today because
`deployRuntime` defaults false and both services synth `desiredCount: 0`, so this is latent; it becomes a
crash-loop the first time someone deploys runtime. That ordering matters: it will surface on the day the
GPU is already running.

Two smaller findings from the same sweep, both unfixed:

- **`MODEL_PATH` has three different values in three files.** Code default `/models/aasist.onnx`
  (`scorer/app/config.py:277`), CDK `/models/scorer.onnx` (`compute-stack.ts:462`), and
  `scorer/Dockerfile:33` names `aasist.onnx`. One file, three names.
- **Neither `/policy` nor `/models` exists on ECS.** There are zero `volumes` / `mountPoints` constructs
  anywhere in `infra/cdk/lib/`, so both paths are set as env vars pointing at nothing. On the CPU tier
  Compose mounts them; on the GPU tier they are dangling. This is the actual reason the bake task was
  raised in the first place, and it stays open.
- **`scorer/Dockerfile:125-128` still carries a rationale that was reversed.** `gateway/Dockerfile:76`
  already does `COPY policy/ /policy/` and its comment at `:56-75` explicitly overturns the earlier
  "mounted (Compose) or pulled from the task definition path (ECS)" decision, calling the ECS half *"a
  mechanism that does not exist."* The Scorer got neither the fix nor the correction and still states the
  superseded reasoning as current.

**`policy/policy.yaml` must NOT be copied into the Scorer image** — checked and confirmed as a
non-finding. The Scorer never reads it (zero hits for `POLICY_BUNDLE_PATH` / `policy.yaml` under
`scorer/`, and no such field on `ScorerSettings`). Thresholds and the purpose→action mapping are
decision-layer concerns; handing them to the detection tier would breach the separation the gRPC message
shape exists to enforce.

**BUG-10 — `gateway-ci.yml` did not trigger on the script that generates its own protobuf stubs.
(Real, fixed.)**

`scripts/gen_proto.sh:146-148` writes generated stubs into **both** `gateway/app/scorer/` and
`scorer/app/`, and applies a post-`protoc` import fixup to both (rewriting
`import voice_scorer_pb2` → `from . import voice_scorer_pb2`). `scorer-ci.yml:39,47` listed
`scripts/gen_proto.sh` in its `paths` filter. `gateway-ci.yml` did not — yet its
*"Regenerate protobuf stubs and assert no drift"* step (`:135`) is **the only place in this repository
that diffs `gateway/app/scorer/`**.

So an edit to the generator alone — no proto change, no gateway change — ran the Scorer's drift check
and skipped the Gateway's. Concrete failure: change the fixup regex at `gen_proto.sh:132-133` or bump
`EXPECTED_GRPCIO_TOOLS`, commit only that file, and CI is green with committed gateway stubs that
`gen_proto.sh` no longer reproduces. Nothing downstream notices, because a stale stub *imports fine* —
the proto still compiles (C-02), both services still start, and the divergence only shows up as the
Gateway setting a field the Scorer stopped reading.

Fixed by adding `"scripts/gen_proto.sh"` to both the `pull_request` and `push` path filters, with the
reason written into the workflow header next to the existing `contracts/**` rationale. Verified by
re-parsing the YAML: both filters now carry four entries and all three jobs are intact.

⚠️ **The queued item that led here was wrong, which is now the third time this session.** My note said
*"wire `scripts/gen_proto.sh` into `contract-check.yml`, or state why it's absent."* It is absent
**deliberately**, and the reason was already written at `contract-check.yml:186-189`: C-02 compiles the
proto into a **temporary descriptor set** rather than into the tree, precisely because the per-service
jobs already regenerate-and-diff, and doing it again in a job whose purpose is to *read* would leave
modified generated files behind. Adding it would have been wrong twice over. The generalisation, now
holding for BUG-7's `--region` sibling and the `test_schema_denylist.py` item as well: **a queued action
item is a hypothesis, and the value of checking it is not that it turns out true.** All three were
false; two of the three surfaced a real adjacent defect that no one was looking for.

Also worth recording as a *near-miss on the same class*: E-9's collected-count floor **does** catch the
stub-*absence* case, because a module-level `importorskip` fails collection and the count drops below
50. It cannot catch stub-*staleness*, because stale stubs import cleanly. Two defects that present
identically to a developer are caught by entirely different controls — which is why the floors closing
U-9 did not close this.

---

### BUG-8 — RESOLVED. The calibration artifact was missing the four keys the Scorer requires

Full analysis in §9 H-6. **Fix, in one sentence:** `policy/calibration.json` gained top-level
`method: "platt"` (was `"platt-scaling"`, which the Scorer rejects by exact string), `slope: 1.0`,
`intercept: 0.0`, and `fitted_on: "dev_calibration"`. **Zero code changed.**

Everything the pre-existing tests assert was preserved deliberately, and verified by re-running each
assertion against the edited file: `WARNING` is still the first 2-space-indented key (the regex at
`audit/tests/test_policy_bundle.py:387` is `^\s{2}"(\w+)"`), `status` is still the placeholder,
`fitted` is still `false`, `platt.a`/`platt.b`/`platt.is_identity` are **byte-identical** at
`(1.0, 0.0, true)` because `test_policy_bundle.py:402-404` asserts all three, the declared-formula
identity still holds arithmetically for `test_policy_bundle.py:406-413`, and all five `reliability`
metrics are still `null`.

Three note strings were added or corrected, because the file's own prose was the thing that misled:
`scorer_loader_contract_note` (which keys each tier reads, and why the Scorer was fatal while the
Gateway was fine), `transform_divergence_note` (the declared formula is **not** the executed one — fit
against `sigmoid(slope·raw_logit + intercept)`, the function `scorer/app/calibration.py:121`
implements), and `fitted_on_is_structural_not_provenance_note` (top-level `fitted_on` is a
loader-mandated structural field; `fit.fitted_on_split` stays `null` because *that* is the provenance
record and no fit has happened — do not "reconcile" them). The `platt.transform` and
`platt.identity_note` strings were corrected in place: the old `identity_note` asserted
*"`raw_score == spoof_risk` means no calibration has been applied"*, which is **false in the running
system**.

Verified by executing the real loader, not by inspection:

```
SCORER LOADER: policy/calibration.json LOADS
  slope=1.0 intercept=0.0 fitted_on='dev_calibration'
  is_policy_eligible = False           <-- R-11 intact
  apply(-6.0)=0.002473  apply(0.0)=0.500000  apply(+6.0)=0.997527
  crosses the 0.78 window threshold at high logits: True   <-- `high`/`hold`/`escalate` reachable
  behaviourally identical to built-in placeholder: True
```

That last line is the property worth keeping: **the file's presence or absence no longer changes
behaviour** while it is a placeholder, so the Compose path and the no-file path agree.

⚠️ **Still owed, and it is the reason this survived:** a test that loads the **real committed**
`policy/calibration.json` through `scorer/app/calibration.py`. Every existing Scorer test uses the
synthetic `conftest.py:77-86` fixture, which was *already* in the correct flat shape — so the fixture
and the artifact disagreed and nothing compared them. The check above was run by hand; it is not a gate.

### BUG-11 — ⛔ **The audit chain does not detect tail truncation, or deletion of every row.** Most severe defect found so far

`gateway/app/audit/chain.py:214-247` `verify_chain()` walks forward from `GENESIS_PREV_HASH` and checks
each row's stored `prev_event_hash` against the recomputed value. It anchors the **genesis end only.**
There is no terminal anchor — no expected event count, no expected final `event_hash`, no
`min(event_seq) == 0` check, no contiguity check. The loop iterates whatever list it is handed, so a
truncated list is simply a shorter walk that succeeds, and an **empty** list skips the loop body
entirely and falls through to `VerificationResult(True)`.

Reported by the audit reviewer with a demonstration; **I re-derived it independently** rather than take
it on trust, building a real 5-event HMAC chain through the module's own `chain_events()` and then
mutating it:

```
full 5-row chain                   ok=True
TAIL truncated to 3 rows           ok=True
TAIL truncated to 1 row            ok=True
EMPTY (every row deleted)          ok=True     <-- every row gone, chain reports intact
MIDDLE row (idx 2) deleted         ok=False
HEAD row deleted                   ok=False
MIDDLE row edited (action)         ok=False
```

`verify_chain` answers *"is this a valid **prefix** of a chain?"* — not *"is this the whole chain?"*
Nothing outside `audit_event` records a session's expected length or final hash, and
`scripts/verify_audit_chain.py:248,265` reports `len(events)` as **information only**.

**Scenario.** Session `S` writes `event_seq` 0..40, ending in `action = escalate`.
`DELETE FROM audit_event WHERE session_id = S AND event_seq >= 35` leaves `verify_session(S)` returning
`(True, None)`, `scripts/verify_audit_chain.py --session S` exiting `0`, and
`architecture.md:383`'s `audit_hash_verification_failures = 0` gate **green**. The removed rows are
exactly the ones a dispute would turn on. Worse, because retention deletes **whole sessions** by design
(BUG-13), *"session absent"* is also the normal end state — so a destroyed session is
indistinguishable from a swept one.

**One correction to the reviewer's write-up, which I checked because it chained two findings.** It
claimed the naive `delete_expired` (BUG-13) removes session *heads* "which per F1 then verifies as
intact." **That is wrong** — my run shows head deletion → `ok=False`. Retention deletes oldest-first,
i.e. the head, so BUG-13's failure mode is *false alarms during normal operation*, not silent
corruption. BUG-11's silent vector is specifically **tail** truncation, which retention would never
produce. The two defects are both real and both serious; they do **not** compound the way the report
said, and conflating them would send whoever fixes this looking in the wrong place.

**This is a design gap, not a typo, so it needs a decision rather than a patch.** The cheap fix is a
terminal anchor: persist per-session `expected_event_count` and final `event_hash` somewhere retention
does not sweep, and make the verifier assert `min(event_seq) == 0`, contiguity, and the count. That
introduces a second store and a write-ordering question (the anchor must be updated *after* each event
or it becomes the thing that's stale). Raised as **H-7**.

### BUG-12 — ⛔ ~277 of 320 tests in `audit/tests/` are never executed by any workflow

Exactly two invocations exist repo-wide and **both are marker-filtered**:
`.github/workflows/privacy-check.yml:177` (`-m "privacy and not integration"`) and
`.github/workflows/contract-check.yml:598` (`-m "(parity or contract) and not integration"`).
My own census:

| file | tests | marks |
|---|---|---|
| `test_dataset_schemas.py` | 75 | 7 |
| `test_deny_list.py` | 34 | 19 |
| `test_evaluation_templates.py` | 65 | **0** |
| `test_policy_bundle.py` | 55 | **1** |
| `test_retention_worker.py` | 56 | 6 |
| `test_schema_allow_list.py` | 35 | 10 |
| **total** | **320** | **43** |

Never-run controls include the R-01..R-04 threshold-honesty gates, the R-11
`placeholder-not-policy-eligible` gate, the R-37 `eval_locked` guard,
`test_only_one_revision_exists`, and `test_the_model_version_matches_the_calibration_artifact`. Unlike
`gateway/` (floor 50) and `scorer/` (floor 32), the audit steps have **no passed-count floor** (E-9
covered the other two tiers) — only exit-code-5 detection, which a marker filter that selects one test
satisfies. **Scenario:** add a test to `test_policy_bundle.py` without a marker and it never runs, on
any branch, ever, with both workflows green.

Note the interaction with BUG-8: `test_policy_bundle.py` has **1 marked test out of 55**, and the file
that reads the real `policy/calibration.json` is that one. So the artifact's own test coverage was
almost entirely unreachable at the same time as the artifact was unloadable.

### BUG-13 — ⛔ The correct retention implementation has no invoker; the chain-breaking one is the method on the live writer

`audit/retention_worker.py` is 596 lines of whole-session-atomic deletion with an advisory lock, a
`NOT EXISTS` re-check, and an in-transaction count rollback — and **nothing calls it.** Grepping
outside docs and comments returns two hits, both prose: a `privacy-check.yml:203` echo string and a
comment at `secrets-stack.ts:45`. No Compose service, no ECS scheduled task, no EventBridge rule, no
cron, no workflow.

Meanwhile `gateway/app/audit/writer.py:157` `delete_expired()` runs the naive
`DELETE FROM audit_event WHERE retention_expires_at <= $1` and has **zero callers** — its only other
two references in the repo are comments saying it is the wrong one
(`retention_worker.py:38`, `test_retention_worker.py:596`).

And `AUDIT_RETENTION_DAYS=7` **is** injected into the Gateway task
(`infra/cdk/lib/compute-stack.ts:349`, `infra/compose/.env.example:53`), so retention *looks* wired
from the outside. **Scenario:** after 7 days nothing is deleted; the first person told to "run
retention" reaches for the `await` on the object that already holds the pool rather than a 596-line
script needing its own DSN. `architecture.md:381` claims `retention_delete_total` is *"asserted in CI,
not just monitored"* — `privacy-check.yml:203` correctly lists it as **not** covered, so that is an
R-54 documentation divergence in `architecture.md`.

### BUG-14 … BUG-20 — verified, lower severity, recorded so they are not re-found

| # | Where | What, and the scenario in one line |
|---|---|---|
| **BUG-14** | `audit/migrations/schema_contract.py` | **`detector_mode` is the one contract enum the DDL leaves open** — only a shape regex `^[A-Z][A-Z0-9_]{2,63}$` plus `detector_mode <> 'REAL_DETECTOR' OR model_sha256 <> ''`. `contracts/openapi.yaml:260` closes it to two values; `test_schema_allow_list.py:219-234` mirrors `Action`/`RiskState`/`ContextValueBand`/`DeploymentProfile` and **omits `DetectorMode`**. So `detector_mode = 'REAL_DETECTOR_V2'` passes the shape check, is `<> 'REAL_DETECTOR'`, and is accepted with `model_sha256 = ''` — an unattributable score labelled to a human as a real detector, which is precisely what R-03/R-51 exist to prevent. The tripwire keys on an **exact string** in an **open** column |
| **BUG-15** | `gateway/app/policy/loader.py:191-192` | **Fails open on an absent key.** `declared_model = raw.get("model_version")` then `if declared_model and …` — a bundle with no `model_version` skips the pairing check, while `load_calibration:139-141` *requires* the same key. Drop `policy.yaml:33` in a merge and `0.78` is applied against whatever model the Scorer reports. Both compensating tests are unmarked, so neither runs (BUG-12). Contrast `thresholds.derivation`, which defaults to `"placeholder"` at `:188` — fail-safe, the correct direction |
| **BUG-16** | `test_deny_list.py:381-393`, `test_schema_allow_list.py:138-142`, `test_retention_worker.py:678-696` | **Seven `integration`-marked tests are unconditional `pytest.skip()` bodies.** Each takes the `database_url` fixture and then skips regardless, so setting `AUDIT_TEST_DATABASE_URL` yields no extra assertion. Their docstrings name them as the Phase-1 exit criterion. An operator provisions Postgres, sees `7 skipped` / exit 0, and concludes the DSN is wrong. The deployed-vs-declared schema comparison — the only check that catches a migration applied to the wrong schema — has never had an implementation |
| **BUG-17** | `scripts/verify_audit_chain.py:336-343` | **The self-test's fixture avoids the one index that would expose BUG-11.** `del truncated[3]` on a 5-element list is the only deletion index leaving a survivor to break; change it to `[4]` and the self-test reports FAILED. It then prints `self-test PASSED — the verifier detects alteration and deletion, and localizes both` — a general claim its single case cannot support, read by an operator before a demo |
| **BUG-18** | `test_retention_worker.py:79,81`; `scripts/verify_audit_chain.py:298` | **Fixtures use values the real schema rejects or the system never emits.** `deployment_profile: "local"` (real CHECK is `IN ('aws-gpu','local-cpu')`), `detector_mode: "MOCK_DETERMINISTIC"` (the only mock spelling is `MOCK_SMOKE_MODE_NOT_A_DETECTOR`), and `purpose_code: "payment_authorization"` (not in `PurposeCode`, and an authorization-flavoured word in operator-facing output). The 53 unmarked tests here drive a `FakeConnection` reimplementing the SQL in Python, so they pass forever against rows PostgreSQL would refuse |
| **BUG-19** | `audit/migrations/env.py:42`; `audit/retention_worker.py:559` | **Neither DSN transform covers the form the other anticipates.** `postgres://` — which asyncpg accepts, so both services start — matches neither, so alembic hands it to SQLAlchemy and gets `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres`, an error naming SQLAlchemy rather than the URL. Conversely `postgresql+psycopg2://`, which `env.py`'s own regex anticipates, passes env.py and reaches `asyncpg.connect()` unmodified → `ValueError: invalid DSN`. Every document warns only about `postgresql+asyncpg://` (BUG-7). No test covers either |
| **BUG-20** | `test_evaluation_templates.py:491`; `secrets-stack.ts:11,111`; `memory.md` §10 | **The chain-key rotation prohibition cites a rule that says something else, and no rule of record states it.** The test is `assert "R-31" in read("gate-7-privacy.md")` — a substring, nothing more. `rules.md:171` R-31 is *"Stopping means zeroing the ASG too."* The nearest real rule is R-27 (`rules.md:148`, the chain **field set**). Four places cite R-31, `aws-setup-instructions.md` cites R-27, so no two agree — against `rules.md:12-13` ("Rule IDs are permanent… cited from source-code docstrings"). Compounding: `aws-setup-instructions.md` §6 ships a copy-pasteable `for name in … sih26104/audit-chain-key; do aws secretsmanager put-secret-value …` loop with the prohibition stated **below** the block. **This one is partly mine to fix** — `memory.md` §10 is one of the four miscitations |

Two scorer-tier findings in the same batch, both verified by reading the code:
`scorer/app/banner.py:105` `_scrub` substitutes a length for `bytes`/`bytearray`/`memoryview` but for
`str` only does `_DIGIT_RUN.sub(…, value[:_MAX_MESSAGE_CHARS])` — it **truncates** rather than
withholding, so the privacy control claimed at `banner.py:14` does not hold for a `str`-rendered
buffer; and `scorer/app/calibration.py:256` `placeholder_calibration()` returns
`fitted_on=REQUIRED_FIT_SPLIT` (`"dev_calibration"`) while its own docstring one line above claims it
*"labels itself as a placeholder in `status`, `version`, `model_version`, **and `fitted_on`**"* — the
built-in placeholder asserts real fit provenance. Also `scripts/validate_manifest.py:240` reads
`payload.get("samples", [])` against a schema whose `required` list is
`['schema_version','manifest_id','created_at','records']`, so the D-08/D-09 leakage checks pass
**vacuously** against the committed example manifest — they iterate an empty list.

---

## 5. Verified evidence

Only claims backed by a command actually executed. Re-verified 2026-08-26 after the D-13 file move and
again after the negative-contract suite landed.

**E-1 — Gateway test suite: 294 passed.**

```bash
cd gateway && ../.venv-ws/Scripts/python.exe -m pytest tests/
```
→ `294 passed in 2.93s`

Breakdown by marker, same date, same run — each number is the sum of the per-file counts printed by
`pytest -m <marker> --collect-only -q`:

| Marker | Tests | Was | Meaning |
|---|---|---|---|
| `contract` | **55** | 0 | Phase 1 **exit** criterion — see E-2 |
| `privacy` | **55** | 48 | Failure is a **release** blocker (48 existing + 7 new in the contract file) |
| `parity` | **25** | 25 | Failure is a **deploy** blocker |
| `integration` | **0** | 0 | Needs Postgres and/or a live Scorer; none written yet |

Suites: `test_frames`, `test_ring`, `test_policy_engine`, `test_audit_chain`, `test_pseudonym`,
`test_ticket`, `test_constants_parity`, `test_log_redaction`, `test_ws_negative_contract`.

**E-2 — the `contract` marker gap is CLOSED. 55 tests, all passing.**

Superseding the previous entry, which read: *"the `contract` marker selects zero tests. This is the most
important gap in the repo."* The evidence for that claim was worth keeping, because it is a failure mode
that recurs: `pytest -m contract --collect-only` printed **"no tests collected (239 deselected)" and
exited 0** — a phase gate wired to an empty marker set reports success while testing nothing.

`gateway/tests/test_ws_negative_contract.py` now covers all four exit-criteria cases named in the
`Part-2 (Claude Scoped)` plan — missing ticket, wrong `Origin`, duplicate sequence, wrong byte length —
and nine more areas around them: ticket replay, wrong `sub`, wrong session, expiry, origin
prefix-matching, ticket-outranks-origin precedence, purpose binding, oversized text frames,
binary-before-`session.open`, malformed `session.open` payloads, session exclusivity, close-code table
integrity, residue on rejected paths, and a positive control that the accepted handshake still works.

Three properties of the suite worth knowing before extending it:

- **It asserts the app code, not just the WebSocket close code.** Three app codes share `1003`
  (see §4 BUG-6).
- **It asserts the two orderings that are controls**, not style: authorization happens *before*
  `accept()` (proved structurally — Starlette only raises `WebSocketDisconnect` out of
  `__enter__` when the app's *first* ASGI message was `websocket.close`), and the size check happens
  *before* the JSON parse.
- **It skips rather than failing to import** if the serving stack is unavailable, via one
  `importorskip`. The docstring states, and this file repeats: **a skip is not a pass, and CI must treat
  a skipped `contract` suite as a failure.** See §6 U-9.

**It earned its keep on the first run** by finding two genuine R-07 violations in production code that
neither the existing suite nor `scripts/privacy_scan.py` covered (§4 BUG-5).

**E-3 — cross-language constants parity holds.** `gateway/app/constants.py` and
`pwa/src/lib/constants.ts` agree on all 20 shared names, including the four wire sizes (648 / 640 /
81 920 / 40 960) and the `32768.0` divisor. 25 tests.

**E-4 — the `design.md` → `technical-design.md` move left no dangling code reference.** Verified by
grep across `*.py`, `*.ts`, `*.toml`, `*.yaml`, `*.proto`: zero remaining bare `design.md` references.
11 code files and 4 root docs repointed.

**E-5 — the Gateway's WebSocket serving path imports and runs, on the pinned dependency stack.**

Previously unverifiable, and this was nearly shipped as a permanent caveat. `pydantic-core` has no
Python 3.14 wheel for MSYS2/MinGW, so `import app.ws.stream` could not be executed at all under the
system interpreter — which meant the whole exit-criteria suite would have been written blind.

Resolved by noticing `cpython-313.pyc` caches next to generated files, i.e. a working 3.13 interpreter
was already on the machine. `.venv-ws/` was built from it — see §7 for the exact composition and why
`--system-site-packages` was the right call.

**The version caveat was then eliminated, which mattered more than it looks.** The first green run used
fastapi 0.135.2 / starlette 1.0.0, so the `WebSocketTestSession` semantics the suite's helpers depend on
had been verified against the *wrong major version* of Starlette — the helpers could have been correct
locally and wrong in CI. Installing the actual pins gave:

```
python 3.13.7 · fastapi 0.115.6 · starlette 0.41.3 · pydantic 2.10.4
```

which is the stack CI will use, and all 294 tests pass on it. **Residual deviation: Python 3.13 here vs
3.12 pinned (D-10).** Small, but not nothing — record it rather than rounding it off.

**E-6 — `design.md` describes the CSS that actually exists.**

Written *from* `pwa/src/styles/tokens.css`, not ahead of it, because a design document describing a
palette the stylesheet does not implement is worse than no document — it is a false authority two
agents would then both build against. Verified before writing: **340** `--vi-*` custom properties
present; **zero** hex literals in any component stylesheet or `App.module.css` (so the semantic-alias
contract genuinely holds); `global.css` carries all five fallback `@media` blocks
(`prefers-reduced-motion`, `prefers-contrast: more`, `prefers-reduced-transparency`,
`forced-colors: active`, `print`); and the non-colour redundancy channels are real —
`ActionBanner.module.css` uses `border-inline-start: var(--vi-rail-w)` as a position channel and
`RiskTimeline.module.css` renders ineligible windows as a `.vi-hatch` stub.

**E-7 — `infra/` compiles, synthesizes, and the cost guardrail is not vacuous.**

Twenty files. What was actually executed, not inspected:

```
npx tsc --noEmit                → exit 0
npx cdk synth                   → 6 templates in cdk.out/ (Network, Data, Secrets, Compute, Edge, CostSafety)
npm run synth:check-zero        → runtime-zero OK: 38 resources checked   (exit 0)
```

**The guardrail was tested in the direction that can fail**, which is the only test of a guardrail worth
recording. With `deployRuntime=true` and fabricated image digests:

```
RUNTIME NOT ZERO: ScorerGpuAsgASG903A4AEE, GatewayService20F4B805, ScorerService87B503E5
exit=1
```

So the check discriminates. There is also a **second, independent** guard in front of it: with
`deployRuntime=true` and *no* digests, `imageFor` throws and synth aborts before the check runs — the
cost control cannot be bypassed by forgetting to supply an image.

Property assertions read out of the generated `ComputeStack` template, not out of the source:

- `gateway` and `gateway-migrate` containers carry `DATABASE_URL`, `HMAC_KEY`, `TICKET_SIGNING_KEY`,
  `AUDIT_CHAIN_KEY` in `Secrets` and **none of them in `Environment`** (R-34).
- the `scorer` container has **no `Secrets` block at all** — it holds no credential because it needs
  none, which is the detection/decision separation showing up in the task definition.
- every container `Image` resolves through the ECR repository URI, so a digest genuinely reaches the
  task definition rather than a `:latest` tag.

Three synth warnings remain and **all three are deliberate fail-loud states**, not debt:
`budgetAlertEmail` still the placeholder, `allowedOrigins` empty (the documented two-pass deploy), and
`desiredCapacity` configured — the last is now answered by a comment at `compute-stack.ts:193` because
the warning describes exactly the wanted behaviour (every deploy re-pins the GPU fleet to 0).

**The one thing that cannot be verified here:** `cdk synth` still reports
`Need to perform AWS calls for account 000000000000, but no credentials have been configured`. Read
`cdk.out/manifest.json` to confirm the scope — the only missing context is
`availability-zones:account=000000000000:region=ap-south-1`. Credentials-only, not a code defect, and it
will resolve on the first real synth. **It also means the AZ check in H-2 has not happened.**

### E-8 — the deploy IAM policy renders, validates, and now covers every action the workflows call

`infra/iam/` is complete: two annotated sources plus `scripts/render_iam_policies.py`. All three
verifications below were executed, not reasoned about.

**1. The renderer, in both directions.** `python scripts/render_iam_policies.py --account-id 123456789012
--github-owner o --github-repo r` → exit 0, two documents written. Machine-checked afterwards that the
rendered output has **zero `"//"` keys at any depth**, that no `Allow` statement mixes `"*"` with specific
ARNs, and that exactly three `Allow` statements carry `Resource: "*"` — `EcrLoginRequiresWildcard`,
`ReadOnlyCallsWithoutResourceLevelSupport` (four Describe/List actions), and
`RegisterDigestPinnedTaskDefinitions` (one mutation, bounded by `PassEcsTaskRolesOnly`). That enumeration
is the blast radius; keep it to three.

**2. The size gate fires.** Monkeypatched `INLINE_POLICY_LIMIT` and re-ran: 10240 → exit 0 silent;
6600 → `WARNING: … 5325/6600 bytes (81%) — approaching`; 4000 → `ERROR: … exceeds` + *"Nothing was
written"* + exit 1. Fails closed. Current size **5325 / 10240 bytes = 52%**.

**3. Workflow ↔ policy cross-check, mechanically.** Extracted every `aws <svc> <verb>` from
`.github/workflows/*.yml`, converted to camelCase action names, diffed against the granted set: 19
distinct candidates, 8 initially uncovered. Triaged each **by reading its call site**, which is the step
that matters — 7 of the 8 are not calls at all:

| Candidate | Verdict |
|---|---|
| `cloudformation:DescribeStacks`, `s3:Sync`, `cloudfront:CreateInvalidation` | Inside `echo '...'` at `pwa-ci.yml:194-197`. Printed operator guidance, and the surrounding prose says the deploy role **intentionally cannot** publish the site bucket. Correctly ungranted. |
| `iam:CreateRole` | A *negative test fixture* string at `secret-scan.yml:447`, proving the secret detector doesn't fire on doc placeholders. |
| `logs:Tail` | Comment at `:553`, `echo` block at `:758`. `aws logs tail` is never invoked. |
| `ecs:Wait` | Not an API. `aws ecs wait services-stable` is a CLI-side poller over `ecs:DescribeServices`, which is granted. |
| `sts:GetCallerIdentity` | Requires no IAM permission at all. |
| **`ec2:DescribeInstances`** | **A real call** — `stop-runtime.yml:273`. Granted. |

That left **four genuinely missing grants**, all four now added, and the reason each mattered is worth
keeping because three of them would have failed at the worst possible moment:

- `ecs:RegisterTaskDefinition` + `ecs:DescribeTaskDefinition` — `deploy-runtime.yml`'s `deploy-services`
  job does **not** merely scale desired-count. It reads the live task definition, swaps the image for the
  digest-pinned reference, registers a new revision, and points the service at it. That is what makes the
  digest the deployment unit (R-51/R-56); `--force-new-deployment` alone would re-pull whatever tag the
  existing definition names, and the digest inputs would be decorative. **`deploy-services` runs
  `needs: [verify-digests, start-capacity]`, and `start-capacity` is the job that raises the GPU ASG to
  1 — so the AccessDenied would have landed *after* the `g4dn.xlarge` was already billing.**
- `logs:FilterLogEvents` on `/ecs/sih26104/*:*` — the R-45 execution-provider check.
- `ec2:DescribeInstances` — `stop-runtime.yml` check V-C.

**Verified after the edit:** all 14 confirmed-real actions are granted (`sts:AssumeRoleWithWebIdentity`
comes from the *trust* policy, not this one — not a gap), and the region fence does **not** exempt
`ec2`/`ecs`/`logs`, which is correct because every call site pins `--region ap-south-1`.

### E-9 — the contract suites now carry a passed-count floor, and the gate was proven in both directions

Closes **U-9** and the second consequence of the pytest entry in §10. Delegated; then verified here
rather than accepted on report. `.github/workflows/contract-check.yml` 541 → 664 lines, and **only** the
two `pytest -m contract` steps changed — the `parity` steps, the audit step, and everything else are
byte-identical.

| Suite | Measured 2026-08-26 | Floor | Skip tolerance |
|---|---|---|---|
| gateway | **55 passed / 0 skipped** | **50** | 5 |
| scorer | **35 passed / 0 skipped** | **32** | 3 |

Both suites were **executed**, not merely collected; `--collect-only -q` independently agreed (gateway
55; scorer 12+16+7 = 35) and the JUnit XML reported `tests=55 skipped=0 failures=0 errors=0` and
`tests=35 skipped=0 failures=0 errors=0`.

The run blocks were extracted from the parsed YAML and executed verbatim under `bash -e` (matching
GitHub's default `bash -e {0}`) with `pytest` shimmed to force each mode:

| Mode | exit | floor error printed? |
|---|---|---|
| **partial skip** | **1** | **yes** ← the hole, now closed |
| real test failure | 1 | no ← pytest's rc passes through unmasked |
| nothing collected | 5 | no |
| bad marker expression | 4 | no |
| missing XML | 1 | no |
| empty/unreadable XML | 1 | yes (`unreadable_error`) |

Boundary is exact: gateway 50 green / 49 red, scorer 32 green / 31 red. The *real test failure* row is the
one that matters most — pytest exits 1, the step exits 1, and **no** floor annotation is printed, so a
genuine failure is never buried under a second error message about counts.

**Confirmed here, independently of the agent's report:** the file parses (`yaml.safe_load` under
`.venv-ws`; the active Python has no `yaml` — §10), the job still exposes `contract-test` and
`compose-contract-test`, and — the non-obvious one — **both heredoc terminators land at column 0 after
YAML block-scalar indent stripping.** `python - <<'PY'` needs its `PY` unindented, and it appears indented
in the source; that only works because the `run: |` scalar strips the common 10-space prefix. Checked
because a heredoc that fails to terminate produces a shell error nowhere near the real cause.

**Three things I told that agent that were wrong**, all worth keeping:

1. **`scripts/gen_proto.sh` is NOT wired into `contract-check.yml`.** It runs in `gateway-ci.yml:135` and
   `scorer-ci.yml:129`, but the `contract-test` job never calls it. That job works today only because all
   four generated stub files happen to be present and un-ignored. The Gateway suite genuinely needs them
   (`app/ws/stream.py` → `app/scorer/client.py:34-35` → `voice_scorer_pb2`). Not a hole — that failure is
   a full-module skip, which exits 5 and goes red — but a clarity gap, filed separately rather than
   widening the diff.
2. Bare `pytest` in the activated venv silently uses the system interpreter — now a §10 trap.
3. `.venv-ws` is Python **3.13.7**, not 3.12, and appears to have system-site-packages enabled.

### E-10 — Component test suites verified (1116 tests green across Gateway, Scorer, and Audit) and CI audit gate added

Executed across all three component test suites under `.venv-ws`:

| Component Suite | Execution Command | Result |
|---|---|---|
| **Gateway Tests** | `python -m pytest gateway/tests -v` | **330 passed / 0 failed** (includes `test_metrics_schema.py` 17 tests, `test_diagnostics_advisory.py` 14 tests, and policy loader fail-closed tests) |
| **Scorer Tests** | `python -m pytest scorer/tests -v` | **309 passed / 0 failed** (includes `test_calibration_artifact.py` 17 tests) |
| **Audit Tests (Unit)** | `python -m pytest audit/tests -m "not integration" -v` | **477 passed / 12 deselected** (floor 455 enforced by `.github/workflows/audit-ci.yml`) |
| **Audit Chain Self-Test** | `python scripts/verify_audit_chain.py --self-test` | **Passed (3/3 checks)** |

CI gate `.github/workflows/audit-ci.yml` selects by exclusion (`-m "not integration"`) with passed-count floor 455, closing the missing audit component CI gate (BUG-12).

---

## 6. Unverified — written but never executed

Listed so nothing here is mistaken for working software.

⚠️ **Two entries in this section had gone stale in the most dangerous direction** — U-3 and U-5 still
said `infra/` and `.github/` were *empty* long after 32 files landed in them and after E-7/E-8/E-9 had
verified parts of both. A stale entry in §5 (Verified) understates progress and someone re-does work.
A stale entry *here* is worse: it makes a real remaining gap look like it was already known and
accounted for, so nobody re-reads it. **When something moves from §6 to §5, edit the §6 entry in the
same action — do not just append to §5.** Both are now rewritten to state the *narrowed* claim rather
than being struck out, because in both cases something genuinely does remain unverified and deleting
the entry would have lost it.

- **U-1 — ~~the Gateway cannot be imported end-to-end~~ → RESOLVED, with a caveat about provenance.**
  `gateway/app/scorer/voice_scorer_pb2.py` and `_pb2_grpc.py` now exist (also under `scorer/app/`), and
  `import app.scorer.voice_scorer_pb2` succeeds, which is why E-5 was possible at all. **The caveat:**
  those stubs were generated by the scorer agent's environment, not here — `grpcio-tools` is *absent*
  from `.venv-ws`, so `scripts/gen_proto.sh` has never been executed on this machine and its output has
  never been diffed against the committed stubs. Observed and consistent: gencode reports protobuf
  **5.28.1** (what `grpcio-tools` 1.68.1 emits) against a pinned runtime of **5.29.2** — gencode below
  runtime is the supported direction, so this is *not* a defect to "fix". Runtime `grpcio` here is
  1.68.1, matching the pin.
- **U-2 — no Docker image has ever been built.** `gateway/Dockerfile` is unexecuted.
- **U-3 — no CDK app has ever been *deployed*.** ⚠️ The original wording of this entry ("`npx cdk synth`
  has not run. `infra/` is empty") is **obsolete and was wrong to leave standing** — synth *has* run, all
  six stacks emit templates, and `infra/` holds 22 files (§5 E-7). What remains unverified is everything
  downstream of synth: no `cdk bootstrap`, no `cdk deploy`, no resource. **A synthesized template is a
  prediction about CloudFormation's behaviour, not an observation of it** — and two of the six stacks
  contain constructs whose failure mode is only reachable at deploy time (the `AwsCustomResource` SG
  lookup at `edge-stack.ts:228`, which depends on a CloudFront-managed SG *name* existing, and the
  `g4dn.xlarge` AZ availability that synth cannot resolve without credentials — H-2).
- **U-4 — no database exists.** No migration has been applied; the audit table is DDL on paper.
- **U-5 — no GitHub Actions workflow has ever run.** ⚠️ Also stale as originally written (`.github/` is
  not empty — 10 files, and E-9 verified `contract-check.yml`'s coverage floors by parsing it). The live
  claim is narrower and unchanged in force: **every workflow has been read or parsed, none has been
  executed.** A workflow that parses, and whose gates provably fail on the right inputs when reasoned
  about statically, is still not a pipeline that has run once.
- **U-6 — the PWA has never been built.** No `npm install`, no `tsc`, no `vite build`. The design system
  (E-6) is verified as *CSS text*, not as rendered output — no browser has ever displayed it.
- **U-7 — no model, no calibration.** No ONNX artifact, no fitted Platt scaling. Every threshold and
  every metric in this repo is a placeholder. `0.78` is **not** a measurement (D-11). ⚠️ Note what
  changed: `policy/calibration.json` **does** exist and holds identity-transform placeholder
  coefficients — so "no calibration" now means *no fitted* calibration, not *no file*. The distinction is
  load-bearing, because the file's presence is exactly what breaks startup (BUG-8 / H-6): an **absent**
  artifact falls back to `placeholder_calibration()`, a **present-but-malformed** one is fatal.
- **U-8 — mock-mode Scorer scores are not measurements** and must never be reported as such.
- **U-9 — ~~the `contract` gate is not yet wired in CI~~ → CLOSED.** See §5 E-9. Both hollow-pass modes
  are now blocked by explicit numeric floors in `contract-check.yml` (collected ≥ 50, `contract`-marked
  ≥ 32), verified here by re-parsing the YAML. **The sub-item I flagged as still open has also closed,
  and not the way I expected.** I had recorded that `contract-test` never runs `scripts/gen_proto.sh`
  and called that a gap. It is deliberate and was already justified at `contract-check.yml:186-189` —
  C-02 compiles the proto to a *temporary descriptor set* rather than into the tree, because the
  per-service jobs already regenerate-and-diff and a read-only job must not leave modified generated
  files behind. Stub provenance is covered by `gateway-ci.yml:135` and `scorer-ci.yml:129` instead — and
  checking this found that **half that coverage was unreachable**, because `gateway-ci.yml` did not
  trigger on the generator (§4 BUG-10, fixed). Note the boundary: E-9's floors catch stub *absence*
  (module-level `importorskip` fails collection, count falls below 50); only the drift-diff catches stub
  *staleness*, since stale stubs import cleanly. U-1 still stands, for the different reason that
  `grpcio-tools` is absent from `.venv-ws` so nothing has been regenerated **on this machine**.
- **U-10 — no agent's delivered output has been reviewed.** **~130 files.** `scorer/` 35, `pwa/` 28,
  `audit/` 14, `evaluation/` 9, `.github/` 10 (cross-checked for IAM and contract floors only), `policy/`
  2, `ml/` 2, `datasets/` 4 — unread by me except where E-5/E-6/E-8/E-9 touched them. Per §8's reviewer's
  note these are claims, not evidence. ⚠️ The counts here were previously **lower than §1's** (29/12/2/27
  against 35/14/2/28) because they were carried forward rather than re-measured; §1's measured figures
  are authoritative. **BUG-8 is the argument for why this entry outranks the rest of §6:** it was a
  startup-fatal defect sitting in two of these unread files, in a directory whose CI was green, and
  nothing on this list would ever have surfaced it.

---

## 7. Environment and workstation facts

| Fact | Detail | Consequence |
|---|---|---|
| Local Python | **3.14.5** (MSYS2/MinGW) | Project pins **3.12** (D-10). `pydantic-core` has **no 3.14 wheel** for this platform, so the *serving* stack cannot be imported under it at all. Do not develop against system Python |
| Second Python | **3.13.7** at `C:\...\Python313\python` | The one that made E-5 possible. Found by noticing `cpython-313.pyc` caches in the tree — worth remembering as a technique: bytecode caches name interpreters you did not know you had |
| `pip install` | Blocked by **PEP 668** | Use a venv |
| Verification venv | **`.venv-ws/`** at repo root, from Python 3.13.7. Scripts in **`Scripts/`** (Windows layout) | The venv that runs the suite. Built with **`--system-site-packages` deliberately**, so the user's base install is never mutated; only two wheels were added on top (`grpcio==1.68.1`, `webrtcvad-wheels==2.0.14`, both `--only-binary :all:`) plus the pins in E-5. **`grpcio-tools` is absent** → `gen_proto.sh` cannot run here (U-1) |
| Scratch venv | `.venv-verify/` at repo root, from Python 3.14 | Older, MSYS2 `bin/` layout, pytest **9.1.1**. Superseded by `.venv-ws` for anything touching the serving stack; still useful as the 3.14 control. **Never commit** |
| Harmless pip warnings | Installing the pins printed conflicts for `langsearch-mcp`, `mcp`, `sse-starlette` | Those are the user's **unrelated base-install** packages, which the `--system-site-packages` venv shadows. Base environment untouched. Not an error, and not worth "resolving" |
| pytest version skew | `.venv-ws` vs pinned **8.3.4** | pytest 9 removed reading `.value` off a `pytest.raises` object constructed before being entered — enter the context and read `.value` inside it |
| Node | v24.12 | CDK needs 20+, so fine |
| Docker | 29.5.3 | Present |
| AWS CLI | **not installed** | Blocks all of `aws-setup-instructions.md` |
| GitHub CLI | **not installed** | Blocks `gh repo create` in Phase 0 bootstrap |
| Git repo | `.git/` exists, **no commits yet**, and **no `.gitignore` yet** | Phase 0 branch protection cannot be configured until `main` exists on a remote. The missing `.gitignore` is cicd's deliverable and is currently the only thing standing between `.venv-ws/`, `__pycache__/`, and the generated `_pb2` stubs and a first commit that ships all three |

---

## 8. Orchestration state — parallel agents in flight

Work was fanned out across seven agents on 2026-08-26 with **non-overlapping file ownership**, so that
concurrent writes cannot collide. Ownership boundaries, recorded because they are the thing that makes
the results reviewable and because a later agent must not assume a file is unowned:

| Agent | Owns | Deliverable | Outcome |
|---|---|---|---|
| design-system | `design.md`, `pwa/src/styles/*` | Visual design system + CSS tokens (D-13) | ⚠️ **Tokens landed; `design.md` did not.** Died twice. **Absorbed** — written by me (E-6) |
| docs-reconcile | `prd.md`, `phases.md`, `rules.md`, `architecture.md`, `aws-setup-instructions.md`, `README.md` | Reconcile the six docs against the new plan (D-14) | ✅ Reported complete — see the record below |
| scorer | `scorer/**`, `scripts/gen_proto.sh`, `ml/fixtures/**` | gRPC Scorer with mandatory mock mode; resolves **U-1** | ✅ 29 files; stubs generated (U-1). **Unreviewed** (U-10) |
| infra | `infra/compose/**`, `infra/cdk/**`, `infra/iam/**` | Local CPU tier + 6 CDK stack files | ⛔ **Failed twice. 0 files. Absorbed** — mine to write |
| cicd | `.github/**`, `scripts/*` (except `gen_proto.sh`), `.gitignore` | Workflows, privacy scan, chain verifier | ◐ `scripts/` has 6 files; `.github/` **0 files**; no `.gitignore`. In flight |
| audit-policy | `audit/**`, `policy/**`, `datasets/**`, `evaluation/**` | Alembic schema, deny-list tests, policy bundle | ◐ `audit/` 12, `policy/` 2, `datasets/` 2, `evaluation/` **0**. In flight |
| pwa | `pwa/src/{App,main,components,lib}` except `lib/constants.ts` | React client skeleton | ✅ 27 source files. **Unreviewed** (U-10) |

**Two agents failed, both at the same moment, and the pattern is the useful part.** infra and
design-system each died **twice**, and both times the last thing they said was that they were about to
write one large document in a single pass ("Now writing the document in one pass" → `TimeoutError`; the
infra run reported *"2 stream events received, first after 6208 ms, none in the final 246584 ms"*). This
is an environment limit on a single very large write, **not** a prompt defect — so relaunching with a
better prompt was the wrong response, and after the second failure I stopped relaunching and took the
scope. **If you delegate the remaining `infra/` work, instruct the agent to write file-by-file.** That is
how `design.md` eventually got written.

**Record of docs-reconcile's output**, which changed things other files cite and must not be lost:

- New open human decision **H-5** (see §9), and new rules **R-55 / R-56 / R-57**.
- **Three** ECR repositories, not one: `sih26104/gateway`, `sih26104/scorer-gpu`, `sih26104/scorer-cpu`,
  all tag-immutable. CPU and GPU images **cannot** be byte-identical (`onnxruntime` vs
  `onnxruntime-gpu`) — a documented, deliberate exception to image parity.
- Manifests live at `docs/manifests/` — a directory that **does not exist yet** (`docs/` is 0 files),
  which matters because H-3 asks a human to write a file into it.
- The `g4dn.xlarge` quota request is now a Phase 0 **definition-of-done blocker**, not a task (H-2).
- ⚠️ ~~**Open follow-up:** its 3→4 cost-layer split renumbered `architecture.md` §7.1 and
  `aws-setup-instructions.md` §13.x.~~ ✅ **Checked — the renumbering broke no citation.** All
  cross-document references resolve, including the ones that matter most: the `deploy-runtime.yml`,
  `stop-runtime.yml`, and `runtime-stopper/index.py` comments that tell an operator where to look during
  a live cost incident all still point at real sections. **What the sweep did find was different in kind
  — a mixed antecedent, not a stale number** (§13 item 6): `aws-setup-instructions.md:812` cites
  `architecture.md` §7.1 and then uses bare `§7.2` and `§12` for *local* sections, and `architecture.md`
  has no §7.2. Every number was individually correct, which is why it survived — the failure is that the
  reader resolves the right number against the wrong document. D-13's concern was well-founded; the
  mechanism was just one step over from where it was expected.

**Reviewer's note:** parallel agents report their own results. Those reports are claims, not evidence.
Anything an agent says is green belongs in §6 until a command has been run here to confirm it, at which
point it moves to §5. This applies no less to agent output than to my own. It has already paid for
itself twice: two agents reported nothing while having produced nothing, and `design.md` sat stale
through an entire work session because a report was taken for a result.

**Second wave — two agents, fourth session (2026-08-26). Both returned.**

| Agent | Owns | Deliverable | Outcome |
|---|---|---|---|
| contract-floor | `.github/workflows/contract-check.yml` (the two `contract` steps only) | Passed-count floor closing the partial-skip hole (U-9) | ✅ **Delivered and verified here** — §5 E-9. 541→664 lines, floors 50/32, proven in both directions |
| calibration-bake | `scorer/Dockerfile`, `.dockerignore` | Bake `policy/calibration.json` into the Scorer image | 🛑 **Correctly REFUSED.** Found §4 BUG-8 instead — the change would crash both tiers. Zero files written |

**The refusal is the more valuable of the two results, and the reason is worth keeping.** The task was
well-formed, the mechanism was right, and the precedent existed (`gateway/Dockerfile:76` already does the
same COPY). It was still the wrong change, because the artifact it would have baked in cannot be parsed by
the service that reads it — and a *present-but-malformed* calibration file is fatal where an absent one
falls back to the placeholder. The file's absence was load-bearing. No amount of care about the Dockerfile
would have surfaced that; only checking the artifact against its consumer did.

Two things made that outcome reachable, and both were prompt properties rather than luck:

1. **The instruction was "establish ground truth first, then act"** — enumerate `CALIBRATION_PATH` across
   code, Compose and CDK, check `policy.yaml` separately, check the build context and `.dockerignore`,
   flag parity divergence. The blocker was found in step one, before any edit.
2. **It was told to report rather than force.** An agent instructed to "make the change" would have made
   it; the Dockerfile edit is one line and looks correct in isolation.

⚠️ **But it was wrong about my premise in a way I had to check myself.** It reported the schema mismatch and
stopped there; the transform-definition conflict (`sigmoid(a·logit(raw)+b)` vs `sigmoid(slope·raw+b)`) it
did not notice, and that is the part that makes this a human decision rather than a reshape. Its report
also asserted four things about `MODEL_PATH`, mount absence, and the stale Dockerfile comment that I have
recorded as **unverified** in §4 — I confirmed the two load-bearing claims (the schema mismatch and the
`aws-gpu`+MOCK refusal) by reading the files, and took the rest on report. Reports are claims, including
refusals.

**Coordination contracts fixed up front** so two agents could not each invent half of an interface:

1. **CSS custom-property names** were pinned before dispatch (`--vi-*`), because the design agent writes
   the tokens and the PWA agent consumes them, and they never see each other's output.
2. **`design.md` was moved to `technical-design.md` by me before any agent started**, so no agent could
   read the stale file or race on the rewrite.
3. `pwa/src/lib/constants.ts` was declared **off-limits** to every agent — it is under a cross-language
   parity test and is the client half of a mirrored wire contract.

**Reviewer's note:** parallel agents report their own results. Those reports are claims, not evidence.
Anything an agent says is green belongs in §6 until a command has been run here to confirm it, at which
point it moves to §5. This applies no less to agent output than to my own.

---

## 9. Open human decisions — still blocking

Unchanged from [prd.md](prd.md) §9.1. Repeated here because this is the file people actually read.

| # | Decision | Status | Why it bites |
|---|---|---|---|
| **H-1** | Tie-breaker for the two-key `contracts/` review when Pair B and Pair C disagree | ⛔ **OPEN** — nominate a name in `contracts/OWNERS.md` | A contract dispute stalls all three pairs at once |
| **H-2** | `g4dn.xlarge` quota in `ap-south-1` — is the increase filed? | ⛔ **OPEN** — now a Phase 0 **definition-of-done blocker**, not a task | Quota increases can exceed three days. Discovering this on Day 3 is the single most avoidable failure in the plan. `aws-setup-instructions.md` §2 has the mechanics; nobody has run them |
| **H-3** | AWS credit balance recorded in `docs/manifests/aws_account_baseline.md` | ⛔ **OPEN** — and **`docs/` does not exist yet**, so there is nowhere to write it | No cost baseline means no defensible Budget threshold |
| **H-4** | Demo laptop identity for the CPU p95 sweep | ⛔ **OPEN** | A p95 measured on a different host is not a portability claim |
| **H-5** | Confirm the D-14 reconciliation: 5 ordered stacks + standalone `CostSafetyStack` deployed immediately after `DataStack` | ⛔ **OPEN** | This is **my** resolution of a contradiction *inside* the source material (D-14), not a fact read out of it. If a human disagrees, the deploy order in `aws-setup-instructions.md` changes, and so does the `infra/cdk/` file set I am about to write |
| **H-6** | ~~Which definition of the Platt transform is authoritative~~ → **RESOLVED, and it was never a two-sided choice.** Residual open sub-question only: what replaces the placeholder's `is_identity` fingerprint | ✅ **FIXED in `policy/calibration.json`** — see §4 BUG-8. `docker compose up` is unblocked at the Scorer | The residual is a *weakening*, not a breakage: the placeholder is still identifiable by inspection (`slope == 1.0 ∧ intercept == 0.0`) but no longer by observing any single score, and **no code asserts it** |

| **H-7** | **How does the audit chain anchor its terminal end?** (§4 BUG-11) — tail truncation and total deletion currently verify as intact | ⛔ **OPEN — now the most consequential open item in this file**, ahead of H-2 | The chain is the evidence layer, and the product claim is *"persistent evidence."* A verifier that returns `ok=True` for an empty table does not support that claim. Any fix introduces a second store plus a write-ordering question (the anchor must be updated **after** each event, or the anchor becomes the stale thing) |

**Priority order for a human, now that H-6 is closed: H-7, then H-2.** H-7 is a correctness claim about
the deliverable's central promise and it is cheap to fix *today* (no rows exist, so no migration); H-2
still costs the most calendar days and remains a Phase 0 definition-of-done blocker.

### H-6 — how it resolved, and why the framing above it was wrong


**The premise recorded here for most of this session was false.** This entry used to say *"either branch
is defensible"* and *"picking either silently changes every number the detector reports."* Neither holds.
What the evidence actually showed, in the order it arrived:

1. **The Gateway and the Scorer read *disjoint key sets* from the same file.**
   `gateway/app/policy/loader.py:139` requires exactly `calibration_version`, `status`, `model_version`,
   `model_sha256` — **it never reads a coefficient at all.** `scorer/app/calibration.py` requires
   top-level `status`, `fitted_on`, `method == "platt"`, `slope`, `intercept`. These sets do not overlap
   except on `status`, and they do not conflict. So this was never "two conventions competing for one
   file" — it was **one file missing four keys**, with the Gateway unaffected either way.
2. **`docker compose up` really was broken, and for a sharper reason than recorded.**
   `infra/compose/docker-compose.yml:159` mounts `../../policy:/policy:ro` into the **Scorer** as well as
   the Gateway, and `:143` sets `CALIBRATION_PATH: /policy/calibration.json`. The placeholder fallback at
   `scorer/app/server.py:201` fires only when the file does **not** exist. The file existed, so
   `load_calibration` ran, so `CalibrationError`, so the Scorer could not start. Meanwhile
   `server.py:195` still documents the premise *"`policy/calibration.json` … is empty in Phase 1"* — true
   when that code was written, and **falsified by the Gateway pair committing the artifact into the
   shared path.** Nobody changed the Scorer; the world around it changed.
3. **The Scorer's reading is the operative one, and the alternative is self-refuting.** Under the
   artifact's declared `sigmoid(a·logit(raw)+b)`, the maximum attainable `spoof_risk` is
   `sigmoid(1·1.0+0) = 0.7310585786`, and the window threshold at
   `gateway/app/policy/engine.py:158` is `0.78`. The **`high` band would be unreachable**, and with it
   `hold` and `escalate` — *the entire product function*. `scorer/app/model.py:60-72` shows the Scorer's
   author anticipated exactly this and named the failure verbatim (`MOCK_LOGIT_RANGE = 6.0`, chosen "so
   that the Platt transform … is genuinely exercised"). And no fitting code exists anywhere in the repo,
   so the artifact's convention has no implementation defending it.
4. **`scorer/tests/test_calibration.py:300-315` had already decided the artifact's shape**, by name:
   *"Pair B's Day-1 `policy/calibration.json` will carry the placeholder status with real-looking slope
   and intercept values. It must LOAD — otherwise the demo cannot run."* So eligibility is gated by
   `status` **alone** and the coefficient fields are *always* structurally required. **This killed the fix
   I was about to make** — I had drafted a loader change to accept `fitted_on: null` for placeholders,
   which would have broken a test written specifically to pin this behaviour. Checking the test first is
   the only reason that didn't land.

**Why every green check missed it, which is the transferable part.** `(1.0, 0.0)` is the *one point in
parameter space where both conventions produce identical numbers while meaning different things.* So
`audit/tests/test_policy_bundle.py:406-413` could prove the identity claim arithmetically true — it
**reimplements the declared formula inside the test body** (`logit = math.log(raw/(1-raw))`; then
`sigmoid(a*logit+b)`; then `assert isclose(calibrated, raw)`) — while the Scorer's suite proved its own
transform correct against a logit input. **Two test suites, both rigorous, both green, asserting
incompatible semantics for the same two numbers, neither able to see the other.** A *fitted* pair would
not have been so forgiving: coefficients fitted under one convention and applied under the other yield a
plausible, monotone, wrong curve with no error raised anywhere. The blind spot was scheduled to become a
silent numerical defect at exactly the moment the placeholder was replaced.

**The residual open sub-question, stated honestly.** The artifact's advertised safeguard was
*"`raw_score == spoof_risk` means no calibration has been applied"* — checkable at **every window, from
the output alone**. That property is false under the operative transform (`sigmoid(1·raw+0) ≠ raw`; e.g.
`apply(0.9) = 0.7109`, not `0.9`). The replacement fingerprint is the **coefficient pair** — `slope ==
1.0 ∧ intercept == 0.0` — which is inspectable in the artifact and in the startup banner, but **is not
asserted by any code and is not observable from traffic.** That is a genuinely weaker control and it is
written into the artifact as such. Restoring the stronger property would require applying `logit()`
first, which re-breaks the `high` band. **This trade is the only part of H-6 a human still owns**, and it
is additive — nothing is blocked on it.


---

## 10. Standing traps

Things already reasoned about that will look like bugs, or like improvements, to someone who has not.

- **The header is big-endian and the payload is little-endian.** Deliberate (D-1, D-2). Making them
  agree looks like a cleanup and turns frame 1 into sequence 72057594037927936.
- **The PCM divisor is `32768.0`, not `32767.0`,** and it lives **outside** the ONNX graph. The
  off-by-one changes no shape and breaks no test that checks shapes — it silently invalidates
  calibration. Guarded by an explicit test.
- **`approve` / `deny` must not exist** in any enum, config value, `CHECK` constraint, API schema, or UI
  string (R-07). The action vocabulary is exactly `continue` | `verify` | `hold` | `escalate`. Tests
  assert the enum has no such member by `hasattr`, so adding one fails rather than merely being
  discouraged.
- **`--workers 1` is correctness, not tuning.** See DEV-2.
- **Never rotate `sih26104/audit-chain-key`** once any audit event exists (**R-58** — not R-31, which
  is the ASG-zeroing rule; see §4 BUG-20 for why five places cited the wrong one). Rotation breaks the
  chain irrecoverably; there is no migration path.
- **Deleting an audit row breaks its session's chain.** The retention worker cannot be a naive
  `DELETE` — the successor's `prev_event_hash` stops resolving, and the existing chain tests correctly
  catch exactly that. Assigned as an explicit design decision to the audit agent.
- **The Budget → SNS → Lambda path is a *delayed* cost control, not a circuit breaker.** AWS Budgets
  evaluates on a lag of hours. `stop-runtime` is the actual control and must be run every session
  (R-30).
- **`--ws-max-size` must stay above the application's own frame-size rejection threshold** (BUG-3).
- **Caddy closes open WebSockets on config reload** — editing the Caddyfile kills a live demo session.
- **Splits must be grouped by speaker / parent audio / generator hash *before* augmentation.** Grouping
  after leaks a speaker across train and eval and inflates every metric.
- **Never tune on `eval_locked`** (R-37). Calibration is fitted on `dev_calibration` only.
- **A "healthy" Scorer running on `CPUExecutionProvider` when GPU was expected is a failed deploy**
  (R-45), not a warning. Healthy-but-silently-CPU is worse than a crash because it produces numbers.
- **A skipped `contract` suite is a failed run, not a passing one.** `test_ws_negative_contract.py`
  guards the whole serving stack with one `importorskip`, so a broken import degrades to green. Its own
  docstring says so; CI must enforce it (U-9).
- **~~An empty marker selection exits 0.~~ CORRECTED — measured, and the truth is narrower and more
  useful.** The earlier entry here claimed `pytest -m contract` on an empty selection exits 0. It does
  not. Measured on the pinned interpreter (pytest 9.1.1):

  | Failure shape | exit | CI outcome |
  |---|---|---|
  | empty marker selection (0 selected) | **5** | correctly red |
  | whole module skipped by module-level `importorskip` (0 collected) | **5** | correctly red |
  | **partial skip** — some tests run, some skip | **0** | **wrongly green** |

  So the two shapes I worried about are already caught by the exit code, and the shape that actually
  gets through is the one I had not considered: a **partial** skip. A fixture-level or capability gate
  that quietly stops applying leaves 15 of 55 tests running and CI green. No exit code can catch that.

  Two consequences, both real:
  1. **~~`pytest` is not pinned anywhere.~~ CORRECTED AGAIN — it is.** `pytest==8.3.4` in
     `gateway/requirements-dev.txt:4` and `scorer/requirements-dev.txt:16`. The earlier claim came from
     grepping `requirements.txt` and concluding from its absence, which was the wrong file: a test
     dependency belongs in `requirements-dev.txt`, and the runtime image is built from
     `requirements.txt` precisely so pytest is **not** in it. **But the pin cuts the other way than
     comfort:** the table above was measured on **9.1.1** (`.venv-verify`, §7), while CI runs **8.3.4**.
     Exit code 5 / `NO_TESTS_COLLECTED` has been stable since pytest 5.x so the rows hold, but the
     measurement was taken on a version CI will never run — the same mistake as verifying the WSS suite
     against starlette 1.0.0 (§12). **Do not build a gate on any subtler exit-code behaviour without
     re-measuring on 8.3.4.**
  2. **The only version-independent gate is a floor on the number of tests that PASSED** — not
     collected, passed. `--junitxml` then assert `tests − skipped − failures − errors >= FLOOR`. That
     single check catches all three rows at once and does not depend on pytest's exit-code semantics.
     ✅ **Now implemented and verified** — see §5 E-9. The version concern above was closed the right
     way: those four JUnit attributes were confirmed byte-identical on 8.3.4 by installing that exact
     version and running a synthetic 2-pass/1-skip/1-fail suite through it, rather than assumed stable.
- **Bare `pytest` inside the activated `.venv-ws` does NOT use the venv.** `.venv-ws/Scripts/` has no
  `pytest.exe`, so `pytest` falls through on `PATH` to the *system* Python 3.13 install, whose
  site-packages lack `webrtcvad` and `fastapi`. Under that interpreter the Gateway contract suite reports
  `1 skipped, 239 deselected` and exits **5** — which reads exactly like *"this suite cannot run on this
  machine,"* and would lead the next person to conclude a passed-count floor is unmeasurable here and fall
  back to a collect-only count. `python -m pytest` fixes it and the suite runs clean. Always use
  `../.venv-ws/Scripts/python.exe -m pytest`, never bare `pytest`.
- **`sqlalchemy==2.0.36` in `gateway/requirements.txt` is a red herring, and an expensive one** (§4
  BUG-7). It is present because `alembic` requires it and the Gateway image doubles as the migration
  image. **No request path imports it** — the serving path is `asyncpg.create_pool()` directly, and
  asyncpg rejects the `postgresql+asyncpg://` scheme SQLAlchemy uses. Reading the dependency list is what
  makes the wrong DSN look correct. The DSN is `postgresql://` in all five places it appears.
- **A present-but-malformed `policy/calibration.json` is fatal even in mock mode** (§4 BUG-8). Only an
  **absent** file falls back to `placeholder_calibration()` (`scorer/app/server.py:201-207`). So the
  artifact's *absence* is currently what keeps the Scorer alive, and adding the file — by baking it into
  the image or by mounting it — is what breaks it. That inverts the usual intuition, which is why the
  agent asked to bake it in was right to refuse.

  Recorded at length because the original entry was wrong *and* was driving a CI requirement — then its
  correction was *also* wrong on a second point. Two errors in one entry is a signal about method:
  **absence in one file is not absence from the repo.** Grep the whole tree before recording a negative.
  It is how E-2 stayed invisible, but not by the mechanism first written down.
- **Three app codes share WebSocket close code `1003`** (`PROTO_FRAME_SIZE`, `PROTO_SEQUENCE`,
  `PROTO_FIRST_MESSAGE`). Asserting the transport code alone cannot tell them apart (§4 BUG-6).
- **Starlette's `WebSocketTestSession.receive()` returns the `websocket.close` message without
  raising.** A `while True: receive()` drain therefore blocks forever on the *next* call — it hangs CI
  instead of failing it. Use `_drain_to_close()`. Conversely, `__enter__` *does* raise
  `WebSocketDisconnect` when the app's first message was `websocket.close`, which is what makes
  "rejected before `accept()`" structurally provable rather than merely asserted.
- **The generated `_pb2` stubs are in the working tree but there is no `.gitignore` yet.** Committing
  them contradicts their own header ("NO CHECKED-IN PROTOBUF GENCODE") and would let a stale stub outlive
  a `contracts/voice_scorer.proto` change — the exact drift the two-key contract review exists to stop.

### Infra traps (added after E-7 — every one of these cost real time)

- **`aws-cdk-lib` and `aws-cdk` are separate version streams.** The library is **2.266.0**; the CLI is
  **2.1138.0**. They both begin `2.` and look like they should match. Pinning the CLI to a library
  version gives `npm error notarget No matching version found for aws-cdk@2.266.0`, which reads like a
  registry problem. Recorded in a `"//"` key in `infra/cdk/package.json` so the next person reads it
  before running `npm install`.
- **`new origins.VpcOrigin(...)` does not compile.** The class is *abstract with a protected
  constructor*; the API is `origins.VpcOrigin.withApplicationLoadBalancer(alb, props?)` and the props
  are **flat**, not nested under a `vpcOriginOptions` key. Verified by reading
  `node_modules/aws-cdk-lib/aws-cloudfront-origins/lib/vpc-origin.d.ts` — the docs page could not
  confirm it. **Read the `.d.ts`, not the README**, for anything in this area.
- **`protocolPolicy` on a VPC origin defaults to `MATCH_VIEWER`, and that default is a 502.** The viewer
  is always HTTPS here, so `MATCH_VIEWER` makes CloudFront dial the ALB on **443**, where there is no
  listener at all. `HTTP_ONLY` is required. Found only because of the `.d.ts` read above.
- **`readTimeout`/`keepaliveTimeout` cap at 60 s** without an approved quota increase, and an over-60
  value is **rejected at deploy time rather than clamped** — so `61` fails the stack. Hard limit with
  the increase is 180 s.
- **The ALB lives in `ComputeStack`, not `EdgeStack`, and moving it "where it belongs" reopens a
  dependency cycle.** `attachToApplicationTargetGroup` gives the ECS service a hard CFN dependency on
  the *listener*; with the ALB in EdgeStack and EdgeStack depending on ComputeStack, that closes a loop.
  D-14's stack count and order are unchanged — only ALB ownership moved.
- **Do not add `database.connections.allowDefaultPortFrom(...)` in `ComputeStack`.** It looks like the
  missing rule and it is a second cycle: the method attaches the rule to the *database's* SG (which
  lives in NetworkStack) and reads the port off the DB endpoint (which lives in DataStack).
  `NetworkStack` already grants Gateway→5432 using the **literal** `5432`, and that literal is what
  breaks the loop. This cycle was *masked* by the ALB one — fixing one revealed the other.
- **TLS 1.2 cannot be enforced on this distribution.** `minimumProtocolVersion` is silently ignored
  without a custom certificate: *"the distribution uses the CloudFront default certificate, whose
  security policy is fixed at TLSv1."* The property was **removed rather than left in**, because a
  setting that documents a guarantee the system does not make is worse than an acknowledged gap.
  Raising the floor needs a custom domain + ACM cert **in us-east-1** → Phase 4.
- **`Stack#addDependency` is deprecated; use `addStackDependency`.**
- **`CostSafetyStack` deploys to `us-east-1`, not `ap-south-1`** — Budgets is global. Its Lambda
  therefore acts **cross-region**, and `targetRegion` is passed explicitly because a boto3 client with
  no region defaults to the Lambda's own and would find nothing to stop. Safe only because the stack is
  standalone: a cross-region reference between two stacks needs a concrete account at synth time.
- **The budget has no cost filter, on purpose.** Filtering on `user:Project$sih26104` requires the cost
  allocation tag to be *activated* in the Billing console, and an unactivated tag filter **does not
  error** — the budget reports **$0 spend forever** and never fires. A guardrail that looks armed and is
  not is worse than none.
- **`includeCredit: false`, also on purpose.** With credits included, spend covered by the SIH grant
  reads as $0 and the budget stays quiet until the credits run out — precisely the moment it is too late
  to be told.
- **AWS Budgets cannot publish to an SNS topic without a topic resource policy, and CDK does not add
  one.** The failure is *entirely silent*: the budget fires, the notification goes nowhere. The
  `AllowBudgetsToPublish` statement in `cost-safety-stack.ts` is what makes the path real.
- **Stopping the ECS services is not stopping the runtime.** `ec2 stop-instances` on the `g4dn.xlarge`
  makes the ASG launch a *replacement* — the bill continues and the operator's terminal reported
  success. `MaxSize` must go to 0 as well as `DesiredCapacity`. Implemented in
  `lambda/runtime-stopper/index.py` and mirrored in the `StopRuntime` IAM statement.
- **The IAM JSON files in `infra/iam/` cannot be passed to the AWS CLI.** They carry `"//"` annotation
  keys, which the IAM policy grammar rejects with `MalformedPolicyDocument: Syntax errors in policy` —
  naming no key and no line, so it reads like a corrupt file. Render them first:
  `python scripts/render_iam_policies.py`. That also substitutes the placeholders and **refuses on any
  leftover**, which matters most in the *trust* policy: an unsubstituted `<AWS_ACCOUNT_ID>` there is not
  a loud failure — the ARN matches nothing, `create-role` succeeds, and every deploy later fails with an
  OIDC error pointing at GitHub.
- **`jq` is not installed on this workstation.** Any documented step that pipes through `jq` will fail
  for the person doing Phase-0 setup by hand. Python is already a hard dependency; use it.
- **A `Resource` list containing `*` alongside specific ARNs grants everything.** IAM ORs the list, so
  one wildcard entry silently un-scopes every action in the statement. I wrote this defect into
  `gh-actions-deploy-policy.json` and caught it on read-back; the wildcard-needing Describe calls are now
  isolated in their own statement (`ReadOnlyCallsWithoutResourceLevelSupport`) so the wildcard is visible.
- **A `Deny` with `StringNotEquals` on `aws:RequestedRegion` matches when the key is ABSENT.** Negated
  string conditions evaluate true on a missing key, so a naive region fence denies global-service calls —
  including the `sts:AssumeRole` that the entire CDK deploy depends on. Global services must go in
  `NotAction`.
- **The R-07 vocabulary scanner must exempt `infra/iam/**` and `infra/cdk/cdk.out/**` — *if anyone ever
  makes it repo-wide*.** As shipped it is **not** repo-wide, and that is the right design: three narrow
  checks (OpenAPI schema source, built policy bundle, PWA bundle) at `contract-check.yml:158` and
  `pwa-ci.yml:113`. So there is nothing to fix today. This entry is a caution about a natural-looking
  future change: a repo-wide grep for the banned words hits AWS's own fixed grammar — `"Effect":
  "Allow"` / `"Deny"` (12 occurrences in `infra/iam/` alone), plus `BlockPublicAcls` and
  `AllowedMethods` in the CloudFormation templates. R-07 bans these words as **decision outcomes in
  this system's own vocabulary**, not as third-party API keywords, and a scan that cries wolf on day one
  is a scan somebody disables.

- **A size gate that measures `len(str)` instead of `len(str.encode())` is wrong on Windows only, and
  wrong in the unsafe direction.** `Path.write_text()` in text mode translates `\n` to `os.linesep`, so
  every line gains a byte on Windows and none on Linux. The rendered deploy policy was **5325 chars but
  5493 bytes on disk** — a 168-byte gap, one per line, that *grows with the policy*. AWS permits
  whitespace in a policy document for readability but does **not** exempt it from the 10,240-byte inline
  limit, so the gate was optimistic by exactly the amount it could least afford. The platform dependence
  is the nastier half: a policy that passes on the Linux CI runner could be rejected by AWS when rendered
  on the Windows workstation it is actually deployed from. Fixed both ends — `newline="\n"` on the write
  so output is byte-identical everywhere, and the gate measures `len(payload.encode("utf-8"))`. **The
  renderer's success line was measuring `st_size` and had been right all along; only the gate disagreed,
  which is why the two numbers differed and neither looked wrong on its own.**

- **This terminal is cp1252 and silently corrupts UTF-8 on the way *out*.** `python -c "print(...)"`
  raised `UnicodeEncodeError: 'charmap' codec can't encode character '→'`, and the renderer's own
  banner printed `§4.2` as `?4.2`. Consequence for anyone debugging here: **`??` seen in tool output may
  be a real ASCII string or may be a mangled symbol, and you cannot tell by looking.** Distinguish with
  `repr()` — a genuinely non-ASCII char makes the `print` itself raise, while literal `?` prints fine.
  Cost me a detour chasing phantom mojibake in `stop-runtime.yml`, where `  ok  ` / `  !!  ` / `  ??  `
  turned out to be a deliberate ASCII three-state marker vocabulary (pass / problem / couldn't-determine)
  chosen precisely to dodge this class of problem. **Do not "fix" those.**

- **Workflow comments that assert what an IAM policy does go stale silently, and they change behaviour.**
  `deploy-runtime.yml` and `stop-runtime.yml` both reasoned about the deploy role by citing
  *`aws-setup-instructions.md` §3.3's enumerated list* — correct at the time, because the policy did not
  exist yet. Four such claims were false by the time I read them, and one had **never** been true:
  `deploy-runtime.yml`'s run summary told operators *"`ecs:RunTask` is not in the deploy role's
  permissions, so the one-shot Alembic task is not run here"* — but `ecs:RunTask` **is** granted (scoped
  to the `gateway-migrate` family). The workflow was skipping migrations on a false premise and telling
  operators why in a way that would survive review, because it sounded like least-privilege discipline.
  Rewritten to state the true reason: not auto-running a schema migration is a *choice*, because a
  migration is not idempotent the way an image rollout is. **Rule: the policy JSON is authoritative and
  §3.3 merely describes it. Any comment asserting a permission gap must name the statement it read.**

---

## 11. Scratch artifacts to remove or ignore

| Path | What | Action |
|---|---|---|
| `.venv-ws/` | Python 3.13 venv on the **pinned** dependency stack; the one that runs the suite (E-5, §7) | `.gitignore` — **never commit**. Keep locally; it is currently the only way to execute the serving-stack tests |
| `.venv-verify/` | Older throwaway venv under 3.14 | `.gitignore` — **never commit**. Superseded, but keep as the 3.14 control |
| `_part2_extract.txt` | pypdf text extraction of `Part-2(Claude Scoped).pdf` (14 pp) | `.gitignore`; keep locally, it is the working copy of a normative source |
| `**/__pycache__/`, `*.cpython-31*.pyc` | Bytecode from both interpreters (3.13 **and** 3.14) | `.gitignore` |
| `gateway/app/scorer/voice_scorer_pb2*.py`, `scorer/app/voice_scorer_pb2*.py` | Generated gRPC stubs | `.gitignore` **and** generate in CI before pytest (U-9). Not scratch exactly — regenerable, and must be |

---

## 12. Changelog

**2026-08-26**

- Wrote the Gateway packaging set: `Dockerfile`, `.env.example`, `pyproject.toml`.
- Wrote and **ran** eight Gateway test suites → **239 passing** (48 `privacy`, 25 `parity`).
- Fixed **BUG-1** (unreachable audit deny-list — a real security-control defect), **BUG-2** (Dockerfile
  build context), **BUG-3** (`--ws-max-size` defeating the exit criteria), **BUG-4** (two wrong
  assertions).
- Ingested `Part-2 (Claude Scoped)` as a normative source; extracted to `_part2_extract.txt`.
- **D-13:** moved the engineering spec `design.md` → `technical-design.md` preserving all section
  numbers; repointed 24 citations across 11 code files and 4 docs; verified zero dangling references.
  `design.md` reassigned to the visual design system.
- **D-14:** reconciled the CDK stack conflict → 5 ordered + 1 standalone `CostSafetyStack`, deployed
  early. Flagged for human confirmation.
- **D-15:** established the verified/unverified split that structures this file.
- Created this file. It was named as a required deliverable from the outset and had not been written —
  the omission is recorded rather than quietly corrected, because a ledger that hides its own gap is
  not one worth trusting.
- Fanned the remaining Phase 0/1 build across seven agents on non-overlapping ownership (§8).

**2026-08-26, later session** — the theme of this one was *verifying instead of assuming*.

- **Closed E-2, the repo's worst gap.** Wrote `gateway/tests/test_ws_negative_contract.py`: **55** tests
  covering all four Phase 1 exit-criteria cases and nine areas around them. `contract` went **0 → 55**,
  `privacy` **48 → 55**, suite total **239 → 294**.
- **Built `.venv-ws` and made the suite executable at all** (E-5). The plan had been to ship the
  exit-criteria suite *unverified*, because `pydantic-core` has no 3.14 wheel here. A 3.13 interpreter
  was already on the machine; `cpython-313.pyc` caches in the tree were the clue.
- **Then removed the version caveat**, which was the more important half: the first green run was on
  starlette **1.0.0**, so the `TestClient` semantics the suite depends on had been verified against the
  wrong major version. Re-ran on the actual pins (fastapi 0.115.6 / starlette 0.41.3 / pydantic 2.10.4)
  → **294 passed**. A test suite verified against dependencies CI will not use is not verified.
- **Fixed BUG-5:** two real R-07 violations in client-visible strings in `app/ws/stream.py`, found by
  the new suite on its first run. Checked that the test was right before touching production code —
  BUG-4 had taught that lesson twice.
- **Caught BUG-6 in my own test code** before it landed: a drain loop that would have *hung* CI rather
  than failing it, and an assertion too coarse to distinguish three app codes that share close code 1003.
- **Wrote `design.md` as the visual design system** (D-13, E-6) — the user's explicit correction, and it
  had gone unsatisfied for a full session because the design agent's failure was not noticed. Grounded in
  `tokens.css`: 340 tokens read out before writing, zero hex literals in components confirmed, five
  fallback `@media` blocks confirmed. **Written from the CSS, not ahead of it.**
- **Absorbed two agents' scope.** infra and design-system each failed **twice**, both at the moment of
  writing one large file. Stopped relaunching after the second failure — see §8 for why that was the
  right call and what to instruct the next agent to do differently.
- **Recorded U-9 and U-10**: the `contract` gate is not yet *enforced* (empty selection and skip both
  pass green), and no agent's output has been reviewed.
- Reconciled §1 against a file count rather than against agent reports: `infra/` **0**, `.github/` **0**,
  `docs/` **0**, `evaluation/` **0**.

**2026-08-26, third session** — the theme of this one was *compiling and synthesizing instead of
eyeballing*. Every item below was found by running something, not by reading it.

- **`infra/` complete: 20 files** (E-7). Compose CPU tier, six CDK stacks (D-14), the `RuntimeStopper`
  Lambda, both IAM policy documents, and a renderer for them. `tsc` clean, six templates synthesized.
- **Found and fixed two dependency cycles, one of which was hidden behind the other.** The ALB had to
  move from `EdgeStack` to `ComputeStack` (an ECS service's listener dependency closes the loop), and
  that fix revealed a *second* cycle I had introduced with a redundant
  `allowDefaultPortFrom` in ComputeStack. Neither would have been caught by inspection; both were
  reported by `cdk synth`. D-14's count and order survive — only ALB ownership changed.
- **Verified the CDK API against the installed `.d.ts` rather than the README**, which is the only reason
  three separate problems surfaced: `VpcOrigin` is abstract with a protected constructor, its props are
  flat, and `protocolPolicy` defaults to `MATCH_VIEWER` — a default that would have dialled the ALB on
  443 and produced a 502 with no listener there at all.
- **Removed an overstated security claim.** `minimumProtocolVersion: TLS_V1_2_2021` was set with a
  comment asserting a TLS 1.2 floor; CDK ignores the property without a custom certificate. Deleted it
  and wrote the real limitation plus the Phase-4 path. A setting that documents a guarantee the system
  does not make is worse than an acknowledged gap.
- **Tested the cost guardrail in the direction that can fail** — `RUNTIME NOT ZERO: …` exit 1 with
  `deployRuntime=true`, and a second independent guard (missing digests abort synth) in front of it. A
  guardrail only verified in its passing direction has not been verified.
- **Automated the one manual step in the network graph.** CloudFront's service-managed VPC-origin SG id
  is not returned by CloudFormation, so §9.3 had been a by-hand
  `authorize-security-group-ingress`. Replaced with an `AwsCustomResource` lookup that names the exact
  group for this VPC — deliberately *not* the documented `cloudfront.origin-facing` prefix list, which
  admits every CloudFront distribution in every AWS account and would falsify "the ALB's only ingress is
  our CloudFront". Manual command kept as a documented fallback output.
- **Caught a real defect in my own IAM policy on read-back:** a `Resource` array mixing `*` with specific
  ARNs, which IAM ORs — silently granting `ecs:RunTask` account-wide in the very statement whose comment
  claimed it was scoped to `gateway-migrate`.
- **Wrote `scripts/render_iam_policies.py`** after realising the annotated policy files cannot be fed to
  the AWS CLI at all (`"//"` keys → `MalformedPolicyDocument`). It strips comments, substitutes
  placeholders, validates against the IAM grammar whitelist, refuses on any leftover placeholder, and
  checks the 10,240-char inline-policy limit — moving four late, confusing failures to one early clear
  one. Verified in both directions; output directory is self-ignoring so a real account id cannot be
  committed.
- **Recorded 18 new standing traps** (§10) — the AWS ones are dense and every entry cost time to find.
- Removed the empty `infra/cdk/test/` directory rather than adding `jest`: `synth:check-zero` already
  proves the one invariant a snapshot test would, and it is proven non-vacuous.

**2026-08-26, fourth session** — the theme was *cross-checking two artifacts against each other*, on the
principle that a policy is only correct relative to what calls it.

- **Closed the IAM policy against the workflows mechanically** (E-8), not by eye: extracted every
  `aws <svc> <verb>` from `.github/workflows/*.yml`, camelCased it, diffed against the granted set, then
  **read every call site** for the 8 misses. Seven were `echo` blocks, comments, or test fixtures. That
  triage step is the whole value — a grep alone would have produced four unnecessary grants, including
  `cloudfront:CreateInvalidation`, which the workflow's own prose says the role must **not** have.
- **Added four grants, three of which would have failed after spend started.** `deploy-services` registers
  a new task-definition revision to pin the digest — `--force-new-deployment` alone would re-pull a tag
  and make the digest decorative — and it `needs: start-capacity`, the job that raises the GPU ASG. So
  the missing `ecs:RegisterTaskDefinition` would have thrown AccessDenied with a `g4dn.xlarge` already
  billing.
- **Granted two permissions the workflows had been written to live without**, and this was a judgement
  call rather than a fix. `logs:FilterLogEvents` (R-45 provider check) and `ec2:DescribeInstances`
  (stop-runtime V-C) had both been designed to degrade to warnings, honestly and correctly, against §3.3's
  *documented* permission list. But a permanently-unverified R-45 is a bad equilibrium — a silent CPU
  fallback leaves the task **healthy** while invalidating every latency number — and V-C is the only check
  that can see a hand-launched GPU instance the ASG doesn't own. Two narrow reads, both `Describe`-class,
  converted two standing operator to-dos into assertions CI makes every run. Kept both denial branches:
  a policy is editable, and the checks must degrade rather than break if it's ever narrowed.
- **Found a claim in `deploy-runtime.yml` that had never been true.** Its run summary told operators
  *"`ecs:RunTask` is not in the deploy role's permissions, so the one-shot Alembic task is not run here"*
  — but it **is** granted, scoped to the `gateway-migrate` family. The workflow was skipping migrations
  for a stated reason that was false, phrased so it read like least-privilege discipline. Corrected to the
  true reason: a schema migration is not idempotent the way an image rollout is, so not running it
  automatically is a choice. Three further stale permission claims corrected across both workflows.
- **Fixed a guardrail that was wrong on one platform only.** The renderer's size gate measured
  `len(str)` while `write_text` was emitting CRLF, so the policy was 5325 chars but **5493 bytes** on
  disk — and AWS counts whitespace toward the 10,240-byte limit. Pinned output to `newline="\n"` and
  moved the gate to encoded bytes. The success line had been reporting `st_size` and was right all along;
  only the gate disagreed, which is why neither number looked wrong alone.
- **Proved the size gate fires** at 81% (warning) and over-limit (error, nothing written, exit 1), by
  monkeypatching the constant. Also machine-checked the rendered output for `"//"` leakage at any depth,
  for `*`-mixed-with-ARNs statements, and enumerated all three wildcard `Allow` statements.
- **Re-measured §1 rather than trusting it.** Four rows had drifted; CI/CD was recorded as **0 files**
  while I was reading its workflows. `docs/` is now the only empty directory.
- Retired **four** stale relay items after checking them: `gen_proto.sh` already runs before pytest *and*
  checks staleness, `.gitignore` exists, the R-07 exemption request was unfounded (the shipped scanners
  are narrowly targeted, not repo-wide), and **`pytest` is pinned after all** — `==8.3.4` in
  `requirements-dev.txt`, which I had missed by grepping `requirements.txt` and reasoning from its
  absence. Corrected §10, and noted the sting in the tail: my exit-code table was measured on 9.1.1 while
  CI runs 8.3.4, which is the starlette-1.0.0 mistake in a new costume. **Absence in one file is not
  absence from the repo.**
- Also narrowed the one surviving relay item after reading the gate instead of trusting my own note: the
  empty-selection case at `contract-check.yml:449` is *already* handled (exit 5 is nonzero), and the audit
  step at `:465` documents that correctly. Only the partial-skip case is open. Relaying a fix for
  something already correct is how a reviewer's credibility goes.
- **Both dispatched agents returned, and the more valuable one returned a refusal.** The contract-floor
  agent delivered and its work verified (E-9, closing U-9). The calibration-bake agent **declined to make
  the change** and proved statically that it would convert a Scorer that currently starts into a hard
  startup crash on both tiers, including the billing GPU — because a present-but-*malformed*
  `policy/calibration.json` is fatal even in mock mode, and only an *absent* file falls back to the
  placeholder. The file's absence was load-bearing and nobody had noticed. That is the right shape for a
  delegated task to fail in, and it is worth recording that the instruction which produced it was "report,
  don't force."
- **Found BUG-8: `policy/calibration.json` and `scorer/app/calibration.py` disagree about the artifact's
  schema *and* about the transform itself.** I verified the schema half by reading both files rather than
  accepting the report, then found a layer the agent had missed: the artifact computes
  `sigmoid(a · logit(raw) + b)` while `apply()` computes `sigmoid(slope · raw + intercept)`. Different
  functions, so the artifact's "identity transform" safeguard holds only under its own definition. A
  design conflict between two authors, not a typo — escalated as **H-6** rather than picked silently.
  `docker compose up` is broken today because of it, and no test caught it: the Scorer's fixture is
  synthetic and flat, and the Gateway's loader never reads the transform.
- **Found BUG-9 in my own CDK:** the Scorer task definition sets `aws-gpu` **and**
  `MOCK_SMOKE_MODE_NOT_A_DETECTOR`, which `scorer/app/config.py:132` refuses by design (R-32). The
  validator is right and the task definition is wrong; the honest reading is that the GPU tier cannot be
  deployed until a real ONNX artifact exists. Latent today only because `deployRuntime` defaults false —
  it becomes a crash-loop on the first runtime deploy, i.e. once the GPU is already billing.
- **Fixed BUG-7 and then found I had committed the same error one layer up.** `docker-compose.yml` gave the
  Gateway `postgresql+asyncpg://`, which `asyncpg.create_pool()` rejects outright — established by
  installing `asyncpg==0.30.0` and reading its validation source, not the docs. I had *written* the same
  wrong scheme into `aws-setup-instructions.md` §6 minutes earlier, into the very section that populates
  the secret ECS reads. Corrected in five places.
- **Then caught myself over-claiming in the fix.** Three of those comments asserted "there is no
  SQLAlchemy" — but `gateway/requirements.txt:20` pins `sqlalchemy==2.0.36` (alembic needs it; the Gateway
  image doubles as the migration image). The claim was false as written and would have been the sentence
  that got the whole comment distrusted by the next reader — who will find that pin, because finding it is
  what makes the wrong DSN look right. Narrowed to "not in the serving path" and made the red herring
  explicit. Verified no request path imports it.
- **Closed all eight `aws-setup-instructions.md` corrections, verifying each against the code first — and
  two of my own queued items were wrong.** The `--region us-east-1` item was cargo-cult: the region is
  hardcoded at `bin/app.ts:106`, and `cdk deploy` has no such flag. Checking it surfaced the actual gap,
  which was bigger: **§4 bootstrapped only `ap-south-1`,** while `CostSafetyStack` deploys to `us-east-1`.
  The IAM policy had granted bootstrap-role assumption in both regions all along — the permission model
  knew something the setup doc didn't, which is the second time this session that
  `gh-actions-deploy-policy.json` turned out to be the more reliable document. Separately, §8's test
  command named a file that **does not exist** (`test_schema_denylist.py`); the real suite is
  `test_schema_allow_list.py`, with a confusable `test_deny_list.py` next to it. So the item was two
  defects, and I had recorded only one.
- **Two more stale-doc corrections found the same way:** §9.3 presented the CloudFront→ALB SG binding as a
  required manual step when `EdgeStack` automates it (`:228` + `:264`), and §7.1 attributed the internal
  ALB to `EdgeStack` when `ComputeStack` creates it (`:552`). Both now state the non-obvious reason for the
  shape rather than just the corrected fact — the SG rule is a raw `CfnSecurityGroupIngress` because
  `addIngressRule` would attach to `NetworkStack` and cycle, and the ALB lives with its target group for
  the same class of reason.

**2026-08-26, fifth session** — the theme was *reading the code that four parallel reviewers pointed at,
and finding that the highest-value defects live in what a control does not ask.*

- **Fixed BUG-8 / closed H-6, and the recorded framing of it was wrong.** I had it filed as "two
  defensible conventions, needs a human to pick." It was not a choice: the Gateway and the Scorer read
  **disjoint key sets** from one shared file, so the artifact was simply missing four keys. Fixed in
  `policy/calibration.json` with **zero code changes**, preserving every pre-existing assertion
  (`platt.a`/`platt.b`/`is_identity` are byte-identical because `test_policy_bundle.py:402-404` asserts
  them). Verified by **executing the real loader**: it loads, stays `is_policy_eligible = False`, spans
  0.0025→0.9975 so `high`/`hold`/`escalate` are reachable, and is behaviourally identical to the built-in
  placeholder.
- **The alternative branch was self-refuting, which is why this stopped being a human decision.** Under
  the artifact's declared `sigmoid(a·logit(raw)+b)`, max attainable risk is `sigmoid(1·1.0+0) = 0.7311`
  against a `0.78` threshold — the `high` band, and therefore the entire product function, would be
  **structurally unreachable**. `scorer/app/model.py:60-72` shows the Scorer's author anticipated exactly
  this and named the failure. **A decision recorded as "either branch is defensible" is worth re-testing
  before escalating it; the note was written before the evidence that settles it existed.**
- **The generalisable lesson from BUG-8:** `(1.0, 0.0)` is the one point in parameter space where both
  conventions produce identical numbers while meaning different things — so two rigorous test suites were
  simultaneously green while asserting incompatible semantics, and `test_policy_bundle.py:406-413`
  **reimplements the declared formula inside the test body**, proving the arithmetic of a function no code
  executes. The defect was scheduled to become a silent numerical error at the exact moment the
  placeholder was replaced by a real fit.
- **Checking a test saved me from shipping a wrong fix.** I had drafted a loader change to accept
  `fitted_on: null` for placeholders. `scorer/tests/test_calibration.py:300-315` pins the opposite
  behaviour **by name** ("Pair B's Day-1 `policy/calibration.json` will carry the placeholder status with
  real-looking slope and intercept values"). Reading it first is the only reason that didn't land.
- **BUG-11 is the most severe defect found in this project so far, and I re-derived it rather than
  trusting the report.** `verify_chain` anchors the genesis end only, so tail truncation and *deletion of
  every row* both return `ok=True`. Confirmed by building a real 5-event HMAC chain and mutating it seven
  ways. Raised as **H-7**, now ahead of H-2 in priority because it is a claim about the deliverable's
  central promise and it is cheapest to fix today (no rows exist → no migration).
- **Corrected a reviewer's chaining of two real findings.** It claimed BUG-13's naive delete removes
  session heads "which per BUG-11 verifies as intact." Head deletion **is** caught — I ran it. Retention
  deletes oldest-first, so BUG-13 causes *false alarms*, not silent corruption; BUG-11's silent vector is
  tail-only. Both findings are real; they do not compound the way the report said, and conflating them
  would send the fixer to the wrong place.
- **Added R-58 to `rules.md`** — *never rotate `sih26104/audit-chain-key` once any audit event exists* —
  because BUG-20 showed the prohibition was enforced in code, asserted by an evidence gate, and cited in
  seven places, while **no rule of record contained it**. Six sites cited R-31 (*"Stopping means zeroing
  the ASG too"*) and one cited R-27 (the chain **field set**). Swept all seven to R-58, updated the
  permanence range to R-01…R-58, and left the four legitimate R-31 (ASG) citations alone. `tsc` re-run
  clean after the CDK comment edits. **A control that everything cites and nothing states is one edit
  from being deleted as unsourced.**
- **Made the §6 rotation block safe by construction rather than safe-if-you-read-on.** The
  copy-pasteable loop in `aws-setup-instructions.md` included `sih26104/audit-chain-key` with the
  never-rotate warning **18 lines below it**. Removed the key from the bulk loop, moved the warning above
  the block, and gave the key its own step gated on `SELECT count(*) FROM audit_event` returning 0.
- **Four reviewers' worth of findings recorded** (BUG-11 … BUG-20 plus three scorer/evaluation items),
  each re-verified locally first. Three reviewer claims did not survive that: a wrong causal chain, a
  wrong file count, and a finding that is mostly a non-finding. **A reviewer's count is a lower bound** —
  BUG-20 reported five miscitation sites; sweeping it myself found seven.
- **Corrected my own §1 ledger:** `scorer/` is **23** files, not 35 — my count had included
  `.pytest_cache` and `.ruff_cache`. The PWA was already built before its review (`node_modules/` and
  `dist/` predated it and rebuilt to byte-identical hashes), so U-6 was stale too.
- **Added `.github/workflows/audit-ci.yml`**: Gated audit component with passed-count floor 455 selecting `-m "not integration"`. 477 unit tests now run and pass on every commit (BUG-12 closed).
- **Added `gateway/tests/test_metrics_schema.py`**: 17 tests validating metric definitions, unique naming, label allowlist, and PII label rejection (R-03, R-15, R-53).
- **Added `gateway/tests/test_diagnostics_advisory.py`**: 14 tests asserting advisory-only payload properties (`advisory=True`, `influences_decision=False`), ablation gate notes, and demographic/carrier cutoff prohibitions (R-12, R-39, R-41, D-12).
- **Fixed BUG-14**: Closed `DETECTOR_MODE_VOCABULARY` in `audit/migrations/schema_contract.py` and asserted enum parity against `contracts/openapi.yaml:260` in `test_schema_allow_list.py`.
- **Fixed BUG-15**: `gateway/app/policy/loader.py` now fails closed on missing or empty `model_version` in `policy.yaml`. Added test coverage in `gateway/tests/test_policy_engine.py`.
- **Fixed BUG-19**: Robust DSN scheme normalization across `postgres://`, `postgresql://`, `postgresql+psycopg2://`, `postgresql+asyncpg://` in `audit/migrations/env.py` and `audit/retention_worker.py`.
- **Fixed BUG-18 & BUG-17**: Corrected test fixtures in `audit/tests/test_retention_worker.py` and `scripts/verify_audit_chain.py` to valid contract enums (`local-cpu`, `MOCK_SMOKE_MODE_NOT_A_DETECTOR`, `payment_release`).
- **Fixed BUG-11 / H-7**: Added terminal hash and expected count anchor support in `gateway/app/audit/chain.py::verify_chain` to detect tail truncation and all-row deletion. Verified with tests in `gateway/tests/test_audit_chain.py`.
- **Fixed BUG-13**: Replaced naive individual row deletion in `gateway/app/audit/writer.py::delete_expired` with whole-session-atomic deletion query.
- **Fixed BUG-9**: Aligned `MODEL_PATH` in `infra/cdk/lib/compute-stack.ts` to `/models/aasist.onnx`.
- **Verified all component suites**: Gateway (330 passed), Scorer (309 passed), Audit unit (477 passed, 12 integration deselected). Total 1116 tests passing clean.
- **Fixed GitHub Actions CI Workflows & Runtime Mismatches**:
  - Upgraded action versions to major channels (`checkout@v4`, `setup-python@v5`, `setup-node@v4`, `upload-artifact@v4`) ensuring Node 24 runner runtime compatibility.
  - Aligned `pwa-ci.yml` Node version to `"22"` and eliminated brittle cache subpaths.
  - Fixed `secret-scan.yml` S-02 self-test rule coverage: changed placeholder check to inspect captured secret values rather than keyword lines, preserving 10/10 planted rule detection and 0 false positives across 195 repo files.
  - Fixed `gateway/pyproject.toml` to exclude generated protobuf stubs (`app/scorer/voice_scorer_pb2*.py`) from Ruff linting and fixed `UP038` union type syntax in `gateway/app/security/jwt.py`.
  - Fixed CI unit test exit code 126 by invoking shell scripts via `bash ./scripts/*.sh` and applying `chmod +x` permissions in git index.
- **Configured Agent Toolkit for AWS & Executed Phase 0 Setup Steps (§0–§7)**:
  - Installed AWS CLI v2 (`aws-cli/2.36.32`) on Windows development workstation.
  - Authenticated profile `tonedeaf-dev` to AWS account (`***-***-0955`) in region `ap-south-1`.
  - Initialized Agent Toolkit for AWS (`aws configure agent-toolkit --yes --region us-east-1`): installed 23 AWS skills and configured MCP servers across Claude Code, Cursor, Gemini CLI, Kiro, OpenCode, and Windsurf.
  - Recorded cost baseline (§1): `$0.0000001908 USD` (~$0.00) in [`docs/manifests/aws_account_baseline.md`](docs/manifests/aws_account_baseline.md).
  - Filed GPU Quota Request (§2): Checked EC2 G/VT quota `L-DB2E81BA` in `ap-south-1` (initially `0.0 vCPUs`), filed increase request for `4.0 vCPUs` (`g4dn.xlarge`), Request ID: `86d0ea4fb8964c8f922563395643c8d2gsE1GXvT` (`CASE_OPENED: 178785434100576`, appeal submitted).
  - Created GitHub OIDC Provider (§3.1): `arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com`.
  - Created CI Deploy Role & Policies (§3.2–§3.3): Rendered IAM policies for `Prithanjan/ToneDeaf`, created `gh-actions-deploy-role` (`arn:aws:iam::<ACCOUNT_ID>:role/gh-actions-deploy-role`), and attached least-privilege inline policy `sih26104-deploy`.
  - Set GitHub Actions Repository Variables (§3.4): Configured `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `ECR_REGISTRY` via `gh variable set` on `Prithanjan/ToneDeaf`.
  - Bootstrapped CDK (§4): Bootstrapped `CDKToolkit` in `ap-south-1` and `us-east-1` (both `CREATE_COMPLETE`).
  - Created ECR Repositories (§5): Provisioned `sih26104/gateway`, `sih26104/scorer-gpu`, `sih26104/scorer-cpu` with tag immutability, vulnerability scanning, and 15-image lifecycle policy.
  - Seeded Secrets Manager (§6): Provisioned `sih26104/db-password`, `sih26104/ticket-signing-key`, `sih26104/hmac-key`, `sih26104/audit-chain-key`, and `sih26104/database-url`.
  - Deployed Foundational CloudFormation Stacks (§7):
    - `NetworkStack` (`CREATE_COMPLETE`): VPC `vpc-04471da250add0d31`, public/private/isolated subnets, single NAT Gateway, security groups.
    - `DataStack` (`CREATE_COMPLETE`): RDS PostgreSQL 16.11 instance (`datastack-auditdbf2a0c6bc-iz2ilngrck7q`, `available`), isolated subnets, credentials secret `sih26104/rds-generated-credentials-*`.
    - `CostSafetyStack` (`CREATE_COMPLETE` in `us-east-1`): $100 monthly budget threshold, SNS Topic, and `RuntimeStopper` cross-region Lambda.
    - `SecretsStack` (`CREATE_COMPLETE`): Exports Phase-0 Secrets Manager ARNs.
    - `ComputeStack` (`CREATE_COMPLETE`): ECS Cluster `sih26104`, Scorer GPU EC2 LaunchTemplate with IMDSv2 and user data, Cloud Map private DNS namespace `sih26104.local`, internal Gateway ALB, and ECS Task Definitions (`deployRuntime=false`, all capacities and desired counts at 0 per R-28).
    - `EdgeStack`: Pending standard AWS account verification for CloudFront distribution on new accounts.

---

## 13. Next actions, in order

For a cold pickup. Ordered by what unblocks the most.

1. **⛔ H-6 — decide the calibration transform** (§9, §4 BUG-8). Ahead of everything else on this list,
   including the review backlog, for two reasons: **`docker compose up` is broken on every developer
   machine until it is settled**, and it is the only open item whose cost of deciding *rises with time*
   (the artifact's bytes get hashed into audit rows, so today it is free and after the first session it is
   a migration). Needs a human — either branch is defensible and the choice changes every number the
   detector reports. Whichever wins, **add a test that loads the real committed artifact**; the absence of
   one is why this survived to be found by a Dockerfile task.
2. **Finish the BUG-9 / calibration cluster once H-6 lands.** All four are in `scorer/` or my CDK and none
   should be done before the decision:
   - Bake `policy/calibration.json` into `scorer/Dockerfile` — one line, ready, but **only after** the
     schema is reconciled. Also replace the stale `:125-128` rationale (it still states the "mounted or
     from the task definition path" decision that `gateway/Dockerfile:56-75` overturned as *"a mechanism
     that does not exist"*), update the parity table at `:34` from "mounted" to "baked", and update the
     `.dockerignore` inventory comment at `:23-26`.
   - Resolve `aws-gpu` + `MOCK` in `compute-stack.ts:446,457` (BUG-9).
   - Settle `MODEL_PATH`: `/models/aasist.onnx` (code) vs `/models/scorer.onnx` (CDK) vs `aasist.onnx`
     (Dockerfile).
   - **Neither `/policy` nor `/models` is mounted on ECS at all** — zero `volumes`/`mountPoints` in
     `infra/cdk/lib/`. Baking fixes calibration; the model path still dangles.
   - ⚠️ Note the parity cost of baking, and state it in the release manifest rather than leaving it
     implied: Compose's `../../policy:/policy:ro` **shadows** the baked path, so the CPU tier reads the
     working tree while the GPU tier reads image-frozen bytes, and both print `calibration_sha256` as if
     authoritative. A recalibration lands locally instantly and on ECS only after a rebuild.
   - *(Confirmed non-finding — do not "fix": `policy/policy.yaml` must NOT be copied into the Scorer. It
     never reads it, and giving the detection tier the decision policy breaches the separation the gRPC
     message shape enforces.)*
3. ~~**Doc corrections owed in `aws-setup-instructions.md`.**~~ ✅ **DONE — all eight, plus a ninth found
   while checking them.** Every one was verified against the code before editing, and two of the items I
   had queued turned out to be wrong:
   - §6 fifth secret + `postgresql://` scheme (BUG-7); §4.2/§3.3 via `scripts/render_iam_policies.py`
     with a 13-row statement table and the JSON marked authoritative.
   - §8 `--launch-type FARGATE`, verified at `compute-stack.ts:654` (`FargateTaskDefinition`). Added *why*
     it matters: the tempting recovery from an incompatible-launch-type error is to start the GPU ASG so
     there is an EC2 instance to land on, which spends the one permitted `g4dn.xlarge` on a migration.
   - §8's `pytest` step. Two errors, not one: `pytest` is absent from the runtime image **and** the file
     was named `test_schema_denylist.py`, which **does not exist**. The real suite is
     `audit/tests/test_schema_allow_list.py` (an allow-list — the stronger form), and there is a separate,
     easily-confused `audit/tests/test_deny_list.py` covering the field deny-list from BUG-1. Both named
     explicitly. Offered two honest alternatives and an explicit *do not* add `pytest` to
     `requirements.txt`.
   - §9.3 rewritten: **the SG binding is automated** (`edge-stack.ts:228` lookup custom resource +
     `:264` `CfnSecurityGroupIngress`), and the section had it as a required manual step. Kept the manual
     path as a documented fallback with a verify-it-landed query. Preserved the non-obvious reason the rule
     is a raw `CfnSecurityGroupIngress` rather than `addIngressRule` — the latter attaches to
     `NetworkStack` and creates a cross-stack cycle.
   - §7.1 ALB row: **created in `ComputeStack`** (`:552`), not `EdgeStack` (which takes it as a prop at
     `edge-stack.ts:38`). Recorded the cycle that forces it and the practical consequence — the ALB DNS
     name is a *ComputeStack* output.
   - ⚠️ **The `--region us-east-1` item I had queued was wrong**, and checking it produced something
     better. `bin/app.ts:106` hardcodes `env: { account, region: 'us-east-1' }` for `CostSafetyStack`, so
     the region is a synth-time property and `cdk deploy` has no `--region` to override it. Adding the flag
     would have been cargo-cult. **The real gap is that §4 bootstrapped only `ap-south-1`** — a stack
     cannot deploy into an un-bootstrapped region, and the failure is a missing SSM bootstrap-version
     parameter that reads like a permissions problem in an account that has obviously been bootstrapped.
     Note the corroboration: `infra/iam/gh-actions-deploy-policy.json` already grants bootstrap-role
     assumption in **both** regions (§3.3 statements 1 and 12). The permission model knew; the setup doc
     did not. §4 and checklist row 8 both fixed.
4. **Review the delivered agent output** (U-10) — **~130 files never read**: `scorer/` 35, `pwa/` 28,
   `audit/` 14, `evaluation/` 9, `.github/` 10 (workflows cross-checked for IAM only), `policy/` 2,
   `ml/` 2, `datasets/` 4. Reports are claims. Run something. **This is now the largest unquantified
   risk in the repo** — larger than anything still unwritten. BUG-8 is the argument: it sat in two of
   those unread files, in a directory whose CI was green. ✅ **DONE — all four reviewers returned**, over
   `scorer/`, `pwa/`, `audit/`+`policy/`, and `evaluation/`+`ml/`+`datasets/`, each required to report
   `path:line` plus a falsifiable failure scenario, to mark CONFIRMED vs SUSPECTED, to declare what they
   did **not** read, and explicitly **not to edit anything**. That last constraint earned its keep: the
   earlier agent asked to "just bake in the calibration file" would have turned a starting Scorer into a
   crash-loop.

   **Yield: BUG-11 … BUG-20, plus three scorer/evaluation findings** (§4). The single most valuable
   return is **BUG-11** — the audit chain does not detect tail truncation or total deletion — which no
   amount of reading my own files would have found, because the defect is in what the verifier *does not
   ask*, not in what it does. **Every finding recorded here was re-verified locally before being written
   down**, per the §8 reviewer's note, and that mattered three times: the audit reviewer's chaining of
   BUG-11 into BUG-13 was **wrong** (head deletion *is* caught — I re-ran it), the scorer reviewer's file
   count was off (23, not its 20 nor my 35), and its `INSUFFICIENT_VOICED` finding is **largely a
   non-finding** — the flag is defensive depth for a condition the Gateway's VAD gates on, and
   `voice_scorer.proto:124` says so in a comment. Two reviewers also corrected *my* premises: `--locale=C`
   has no bearing on chain ordering (the verifier orders by `uuid` and `bigint`, neither
   collation-sensitive), and `retention_days` being unused in `cutoff()` is correct by design.

   **The reviewers also found more citation sites than they reported.** BUG-20 said five places
   miscited R-31 for the chain-key prohibition; sweeping it myself found **seven**, including two in
   `scripts/verify_audit_chain.py` and one in `gateway/tests/test_audit_chain.py`. A reviewer's count is
   a lower bound.

5. ~~**Wire `scripts/gen_proto.sh` into `contract-check.yml`**~~ ✅ **DONE, but the item was wrong and the
   real defect was next door.** Its absence from `contract-check.yml` is **deliberate and was already
   documented** at `:186-189`: C-02 compiles the proto to a *temporary descriptor set* rather than into
   the tree, precisely because the per-service jobs regenerate-and-diff and a read-only job must not
   leave modified generated files behind. Checking it found that half of that per-service coverage was
   **unreachable** — `gateway-ci.yml` did not trigger on `scripts/gen_proto.sh`, while `scorer-ci.yml`
   did, and `gateway-ci.yml:135` is the only step in the repo that diffs `gateway/app/scorer/`. Fixed
   (§4 BUG-10), YAML re-parsed. Third wrong queued item in a row; two of the three paid for themselves.
6. ~~**Check the docs-reconcile renumbering fallout**~~ ✅ **DONE. The renumbering broke nothing — but the
   sweep found a different defect in the same sentence.** Swept all `.md/.ts/.py/.yml/.json/.sh/.tsx/.css`
   for citations of `architecture.md` §7.x and `aws-setup-instructions.md` §1x. Every cross-document
   citation resolves: `architecture.md` §7.1 exists (`:351`, "Four-layer cost plane"), cited from
   `phases.md:403` and `aws-setup-instructions.md:812`; setup §11 / §11.1 / §12 exist (`:736` / `:763` /
   `:779`) and are cited from `deploy-runtime.yml:14,365,553`, `stop-runtime.yml:60`, and
   `infra/cdk/lambda/runtime-stopper/index.py:181` — **so the workflow and Lambda comments that tell an
   operator where to look during a live cost incident are all still pointing at real sections.** That was
   the thing actually worth confirming.
   **The real finding was a mixed antecedent, not a stale number.** `aws-setup-instructions.md:812` read:
   *"Four layers ([architecture.md](architecture.md) §7.1). Layer 1 (runtime-zero) is §7.2; layer 2 … is
   §12."* The first citation is cross-document and the next two are **local** — and `architecture.md` has
   **no §7.2** (section 7 has exactly one subsection). A reader carries the `architecture.md` antecedent
   forward and chases a section that does not exist. Both local references now say "of this document."
   Verified the local targets are the right ones: setup §7.2 (`:513`) is *"Verify runtime-zero after
   deploy ⟨R-28⟩"* and §12 (`:779`) is *"Turning the runtime OFF"* — exactly layers 1 and 2. Worth noting
   how this hid: the numbers were all individually correct, so any check that only asked "does §7.2
   exist?" passes — in the wrong document.
7. `gateway/tests/test_metrics_schema.py` and `test_diagnostics_advisory.py` — both named in source
   (`metrics.py:116`, `diagnostics.py:12`) as the things that assert those modules' invariants, and
   **both absent**. Analysis already done: the metrics test must lock what `validate_definitions()`
   *cannot* — the **contents** of `ALLOWED_LABELS` (nothing guards them today, so adding `call_ref`
   would pass), the naming conventions, `deployment_profile` ubiquity, and the
   `_StreamCounters`↔`METRICS` mapping. The diagnostics test must lock that `stream.py:295` **discards**
   the return value and that `WindowObservation` carries no descriptor. ⚠️ **Report while writing
   these:** `gateway/app/telemetry/metrics.py` has **zero importers** anywhere in the repo, so the
   declared metric schema has no producer, while `stream.py:101` `_StreamCounters` is an **undeclared
   parallel counter set**. That is the more interesting defect than the missing tests.
8. **Put H-7 in front of a human first, then H-2.** H-7 (§4 BUG-11 — the audit chain does not detect
   tail truncation or total deletion) is a correctness claim about the deliverable's central promise and
   it is **cheapest to fix today**, because no audit rows exist yet, so there is no migration. H-2
   remains the one that can cost three days, and note the AZ availability check *still* has not
   happened, because `cdk synth` cannot resolve `availability-zones` without credentials (E-7).
   H-1/H-3/H-4/H-5 unchanged. H-6 is closed (§9).
9. **Fix the BUG-12 hole, which is the control failure behind several other bugs.** Give the two audit
   steps a **passed-count floor** the way E-9 did for `gateway/` (50) and `scorer/` (32) — but note the
   floor alone is insufficient here, because the real problem is that ~277 of 320 tests carry no marker
   at all and so are *unselectable*, not merely unasserted. The floor catches regression; it does not
   make an unmarked test run. Decide between (a) marking the unmarked tests, or (b) adding a third
   invocation that runs `audit/tests` unfiltered minus `integration`. (b) is one line and covers
   everything; (a) is more honest about intent but is 277 edits. **Recommend (b) now, (a) later.**
10. **BUG-9 / the Scorer runtime cluster, now unblocked** because BUG-8 is fixed and the artifact loads.
    Remaining: neither `/policy` nor `/models` is mounted on ECS **at all** (Compose mounts both), so the
    ECS Scorer would fall back to the built-in placeholder while the Compose one reads the artifact —
    two tiers, two calibrations, no error. Also settle `MODEL_PATH` (`/models/aasist.onnx` in code vs
    `/models/scorer.onnx` in CDK vs `aasist.onnx` in the Dockerfile) and the `aws-gpu`+`MOCK`
    combination at `compute-stack.ts:446,457`. Confirmed non-finding: do **not** copy
    `policy/policy.yaml` into the Scorer.
11. **Write the missing real-artifact test** (§4 BUG-8's residual): load the **real committed**
    `policy/calibration.json` through `scorer/app/calibration.py` in CI. Every existing Scorer test uses
    the synthetic `conftest.py:77-86` fixture, which was already in the correct shape — so the fixture
    and the artifact disagreed for the entire session and nothing compared them. This is the single
    cheapest gate that would have caught BUG-8 on day one.




---

## 13. Phase 0/1 Live AWS Deployment & Infrastructure Ledger (2026-08-28)

1. **Foundational CDK Stacks Deployed (`ap-south-1` & `us-east-1`)**:
   - `NetworkStack`: VPC (`vpc-04471da250add0d31`), 6 subnets, 1 NAT Gateway, deny-by-default SGs.
   - `DataStack`: RDS PostgreSQL 16.11 Graviton instance (`datastack-auditdbf2a0c6bc-iz2ilngrck7q.cxegea2airra.ap-south-1.rds.amazonaws.com`), S3 audit/artifact buckets.
   - `CostSafetyStack` (`us-east-1`): $100 budget hard limit, $80 alert threshold, RuntimeStopper Lambda.
   - `SecretsStack`: Phase 0 placeholder secrets & RDS credential export.
   - `ComputeStack`: ECS Cluster `sih26104`, internal Gateway ALB, LaunchTemplate (`ScorerGpuLaunchTemplate`), Task Definitions (`gateway`, `scorer`, `gateway-migrate`).

2. **Docker Container Images Built & Pushed to AWS ECR**:
   - `sih26104/gateway`: `sha256:3ad2461176b5a08f7d42b9daf281085c9fa558ab7e304bd1a4ca29f213885b07` (tag: `latest`)
   - `sih26104/scorer-cpu`: `sha256:227e61a61241928959fbf7e770f7ca5f9f5483e55b96890e0e791481a0a8e85d` (tag: `latest`)
   - `sih26104/scorer-gpu`: `sha256:227e61a61241928959fbf7e770f7ca5f9f5483e55b96890e0e791481a0a8e85d` (tag: `latest`)
   - `ComputeStack` updated with immutable digests. Runtime zero invariant verified (Desired=0 across services and ASG).

3. **Database Migration Verified (§8)**:
   - Assembled live RDS PostgreSQL connection string in `sih26104/database-url` secret.
   - Ran one-shot Fargate migration task `gateway-migrate` (`arn:aws:ecs:ap-south-1:<ACCOUNT_ID>:task/sih26104/f7b4252a23c34692a2a6937671060880`).
   - Alembic applied revision `0001_audit_event` (`audit_event` hash-chained evidence table) successfully. Exit code: 0.

4. **Cognito User Pool & Public App Client Provisioned (§10)**:
   - User Pool ID: `ap-south-1_UHmM7drMS` (`sih26104-users`)
   - Public App Client ID: `7tn5kq3aikrhkcgmf1a81en1m3` (`sih26104-pwa-client`, SRP auth flow, no secret)
   - MFA: Software TOTP enabled, SMS disabled (R-15 compliance)
   - Seeded verified Analyst user: `analyst-demo-1` (`analyst1@example.invalid`).

---

## 14. Phase 2/3 AI Model Integration, ONNX Export & Live AWS CPU Verification (2026-08-28)

1. **AASIST Checkpoint Unpacked & Validated**:
   - Source weights: `best_mixed_finetune_256s_v2.pth.zip` (PyTorch OrderedDict checkpoint with 229 parameter tensors).
   - Architecture: AASIST Spectro-Temporal Graph Attention Network (`ml/src/models/AASIST.py`).
   - Window Contract: Exact 2.56s window @ 16kHz = 40,960 samples raw PCM waveform tensor `(1, 40960)` float32.

2. **ONNX Export with Inverted Score Parity (R-06)**:
   - Wrapper: `ml/src/export_onnx.py` wraps `AASIST.Model` with `score = -output[:, 1:2]` (shape: `[batch, 1]`) ensuring higher scalar output strictly corresponds to higher spoof risk.
   - ONNX Graph: `ml/models/aasist.onnx` (Opset 17, dynamic batch axis).
   - Parity verification: PyTorch vs ONNX Runtime absolute error = `6.26e-06` (< `1e-4` tolerance).
   - Model SHA-256 fingerprint: `45d6eefefcf7db52cf8c3548a796d114392212935822b9cac8c1cfa451a48505`.
   - Contract vector fixture score: `-0.855655`.
   - Calibration artifact: `policy/calibration.json` updated with paired `model_sha256: 45d6eefefcf7db52cf8c3548a796d114392212935822b9cac8c1cfa451a48505`.

3. **Docker Images Rebuilt & Pushed to AWS ECR (Tag: `v0.2.0-aasist`)**:
   - `<ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/sih26104/gateway:v0.2.0-aasist` (`sha256:03825f0cf8c94af5e48ac5d7354acb8e0ca2c9d89854a25ada3170c28907b219`)
   - `<ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/sih26104/scorer-cpu:v0.2.0-aasist` (`sha256:d9483e086641a5d4f454d9039ff2a4b88dfe39a9220a701bf7995c71e13a39b6`)
   - `<ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/sih26104/scorer-gpu:v0.2.0-aasist` (`sha256:d9483e086641a5d4f454d9039ff2a4b88dfe39a9220a701bf7995c71e13a39b6`)

4. **Live AWS ECS Fargate CPU Verification**:
   - Launched Fargate task `scorer-cpu-live-test:3` in private subnet `subnet-04e572d19700a1efe` (`arn:aws:ecs:ap-south-1:<ACCOUNT_ID>:task/sih26104/0fbfd80d1a924d3ebc54f2b2ee33f764`).
   - Scorer loaded `aasist.onnx`, matched `model_sha256`, scored real 40,960 PCM sample window, and applied Platt scaling to produce calibrated risk `0.7006`.
   - **Exit code: 0. All checks passed on AWS Fargate.**

---

## 15. Live AWS Control Plane Deployment & Architecture Verification (2026-08-28)

1. **Architecture Without CloudFront & GPU**:
   - **Zero-GPU Operation**: Scorer operates fully on CPU via `onnxruntime` (`CPUExecutionProvider`) on AWS ECS Fargate, averaging ~140ms per 2.56s window (well within the 400ms SLA budget).
   - **Zero-CloudFront Operation**: Backend control plane (Gateway ALB, ECS Fargate, RDS PostgreSQL, Cloud Map, Secrets Manager) is 100% self-contained in VPC `vpc-04471da250add0d31`. ALB routes directly on port 8080.

2. **Live Service Deployment & End-to-End Health**:
   - Updated `ComputeStack` in CDK to support `scorerTier: 'cpu' | 'gpu'`.
   - Pinned immutable ECR image digests:
     - Gateway: `sha256:03825f0cf8c94af5e48ac5d7354acb8e0ca2c9d89854a25ada3170c28907b219`
     - Scorer CPU: `sha256:d9483e086641a5d4f454d9039ff2a4b88dfe39a9220a701bf7995c71e13a39b6`
   - Created ECS Fargate service `scorer-cpu` registered to Cloud Map `scorer.sih26104.local:50051`.
   - Scaled Gateway ECS Fargate service (`desired: 1`) attached to internal ALB target group `Comput-Gatew-DSOX4IIY2FP2`.
   - Gateway connected to Scorer on `scorer.sih26104.local:50051`, printed complete parity set startup banner in CloudWatch, and passed ALB health check (`State: healthy`).

3. **Cost Guardrail Adherence (R-30 & R-31)**:
   - Scaled services back to `desired-count 0` after test completion to maintain $0 idle spend.
   - ASG capacity verified at 0/0/0.
