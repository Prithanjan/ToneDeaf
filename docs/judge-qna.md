# Judge Q&A — Voice Integrity Control Plane (SIH26104 / PS104)

**Status:** preparation aid, not normative. Nothing here overrides [rules.md](../rules.md),
[prd.md](../prd.md), [technical-design.md](../technical-design.md), or [memory.md](../memory.md).
If this file and one of those disagree, they win and this file is stale.

**How to use it.** Each question has two answers. **Say this** is the 15-to-30-second spoken answer.
**The full answer** is what you need to have in your head so the follow-up doesn't knock you over.
Read the short ones out loud until they are yours. Read the long ones until you could draw them.

**The one rule for the room:** every number in this project is either measured, or labelled a
placeholder. There is no third category. If you do not know whether a number is measured, say
"that one is a placeholder, and here is what it would take to make it real." That sentence has never
lost a judge. Guessing has.

---

## Contents

| § | Block | Use when |
|---|---|---|
| 1 | The frame — 30s, 2min, 5min | Opening, every time |
| 2 | Novelty — what is actually new here | "Everyone does deepfake detection" |
| 3 | Architecture | "Walk me through the system" |
| 4 | The policy engine, in full | "What does the policy engine actually do?" |
| 5 | The Scorer and the model | "Which model? How accurate?" |
| 6 | Auth | "How do you stop someone else streaming?" |
| 7 | Secrets | "Where do the keys live?" |
| 8 | Database and the audit chain | "How do I know the log wasn't edited?" |
| 9 | Privacy | "You're recording people's voices" |
| 10 | Tests and CI | "How do you know it works?" |
| 11 | Infra, cost, deployment | "What does this cost to run?" |
| 12 | Process, team, plan | "Five days, three pairs — how?" |
| 13 | **Hostile questions** | The ones designed to break you |
| 14 | Us vs. the typical submission | "What's better here?" |
| 15 | Never say these | Read before you walk in |
| 16 | One-page cheat sheet | Print this |

---

# 1. The frame

## 1.1 The 30-second answer

> A fraudster clones a customer's voice and calls the bank to move money. Our system listens to the
> live call, builds up evidence that the voice is synthetic, and — before the payment completes —
> inserts a verification step sized to what is actually at stake. It never approves or denies
> anything; a human still authorises. And it does that while storing no audio at all, in a log you
> can mathematically prove nobody edited.

Then stop talking. Let them pick the thread.

## 1.2 The 2-minute answer

Four sentences, in this order. Do not reorder them — each one sets up the next.

1. **The problem is not detection, it's what you do with a detection.** Voice cloning is now cheap
   and good. A classifier that says "87% fake" is not a control — someone still has to decide what
   happens to the transaction, and if that decision is a threshold in a script, nobody can audit it.

2. **So we split the system in two, and made the split structural.** A *Scorer* looks at 2.56
   seconds of audio and emits one number. A *Gateway* decides what that number means. The Scorer is
   physically incapable of deciding: it is never told what the call is for, never told what happened
   in earlier windows, and has no field in its response in which to request an action.

3. **The Gateway's decision is evidence-based and proportionate.** It requires 3 high-scoring
   windows out of the last 5 *eligible* ones — one bad window is noise, not evidence. And the same
   evidence produces a different response depending on the purpose: for a balance enquiry it does
   nothing, for a payment release it holds the payment for a step-up, for a beneficiary change it
   escalates to a human. The action vocabulary is `continue`, `verify`, `hold`, `escalate` — the
   words `approve` and `deny` do not exist anywhere in this system.

4. **Every decision writes one row to a hash-chained, feature-only evidence table.** No audio, no
   transcript, no embedding — enforced by a deny-list asserted against the live database schema, not
   by a promise. Each row's hash covers the previous row's hash, so deleting or editing history is
   detectable and we can tell you which row it started at.

## 1.3 The 5-minute version — the demo narration

Run it in this order, and narrate the *reason* for each beat, not the mechanic.

| Beat | What they see | What you say |
|---|---|---|
| 1 | Consent notice, purpose selector | "The purpose is bound server-side, before any audio exists. The client cannot change it later on the audio channel — there's a test for that." |
| 2 | Mic starts, risk timeline empty | "Nothing is scored yet. It needs 2.56 seconds of *voiced* audio. Silence doesn't count, deliberately." |
| 3 | First windows appear, state `collecting` | "One number is not evidence. It won't act until it has 5 eligible windows." |
| 4 | State flips to `uncertain` | "This is the state most systems don't have. It's not 'clean' and not 'fake' — it's 'not enough'." |
| 5 | Third high window → `high` → action fires | "3 of 5. And notice what fired: `hold`, not `deny`. The payment is paused for a second factor. A human still authorises." |
| 6 | Switch purpose to `beneficiary_change`, replay | "Same audio. Same score sequence. Different action — `escalate`. Because there is no step-up that makes a fraudulent beneficiary change safe." |
| 7 | Show the audit row | "One row per scored window. 22 fields in the hash. No audio column exists — let me show you the schema." |
| 8 | Tamper demo | "I'll edit one historical row and re-run the verifier. It tells us which `event_seq` diverged." |

Beat 6 is the one that wins. It is the whole product in one A/B.

---

# 2. Novelty — what is actually new here

## Q: "There are a hundred deepfake detection projects. What's different about yours?"

**Say this:** "Most of them stop where we start. They produce a classifier and a confidence number.
We treat the number as an *input to a governed control*, and almost all our engineering is in the
part after the number — evidence accumulation, proportionality, and an evidence trail you can audit
adversarially. Also: we can show you exactly what we haven't proven yet, which is unusual."

**The full answer.** There are five things here that are genuinely uncommon, and one that is rare
enough to be the real differentiator.

**① The detection/decision seam is enforced by data shape, not discipline.**
Everyone says "separation of concerns." Here it is checked by the compiler and the wire format. The
gRPC request the Scorer receives contains a window of audio and nothing else — no `purpose_code`, no
session history, no window index it's allowed to correlate on. The response has a score and quality
flags and *no action field*. A future contributor who wanted the model to decide would have to change
`contracts/voice_scorer.proto`, which requires a version bump and a two-key review from two different
pairs. There is a test whose only job is to assert this: `scorer/tests/test_detection_decision_seam.py`.

Why it matters: it is what lets you say "the model's opinion never becomes an action without passing
through a reviewed policy file." That is an auditability claim, and auditability is the actual
deliverable in a regulated setting.

**② Evidence accumulation with a real "not enough" state, and ineligible ≠ clean.**
The k-of-n rule (3 of 5) is easy to describe and unusual to implement correctly. The subtle half is
what happens to a window the system *couldn't* score properly — codec-degraded, too short,
quality-flagged. Almost every implementation counts it as low risk, because that's what a default
does. We skip it: it enters neither side of the count.

Why it matters: counting a degraded window as clean means **an attacker on a bad line gets a better
risk state than an honest caller on a good line.** Degrading your own audio becomes an attack. That's
rule R-09, and it is the kind of bug you only find by thinking adversarially before you build.

**③ Proportionality is a reviewed artifact, not a threshold in code.**
Identical evidence → `verify` for a support enquiry, `hold` for a payment release, `escalate` for a
beneficiary change. That mapping lives in `policy/policy.yaml`, whose **raw bytes are hashed and
stamped into every audit row**. So for any decision in history, you can prove which policy produced
it. Re-indenting the file changes the hash and shows up as a policy change — intentionally, because
it's a reviewed artifact and a byte change is a change to what was reviewed.

**④ Privacy is structural and asserted against reality.**
"We don't store audio" is a claim every team makes. Ours is a five-rule deny-list run against
PostgreSQL's own `information_schema`, on **every table**, including tables this project didn't
create. Rule 5 asserts the column list as an **exact set** in both directions — so adding
`raw_audio_path` fails, not just removing a column. The rule that catches most teams is that a subset
check (`expected ⊆ actual`) passes happily the day after someone adds a forbidden column. And the
deny-list module is stdlib-only so it runs in the fastest CI lane, because *a privacy control that is
expensive to run is a privacy control that gets skipped.*

**⑤ Accountability and privacy at the same time, which usually trade off.**
The audit table is feature-only *and* hash-chained. You get "prove nothing was tampered with" without
"we kept the recording." Each row's HMAC covers its own 22 canonical fields concatenated with the
previous row's hash. A per-row signature would catch edits but not deletions — remove a row and every
remaining signature still validates. The chain makes *absence* detectable, and the verifier reports
the first divergent `event_seq`, so you learn where tampering began.

## Q: "If you had to name ONE novel factor, what is it?"

**Say this:** "That the system is built to be *audited*, not demoed. Every mechanism in it assumes a
hostile reviewer will show up and ask 'prove it' — and there's a structural answer instead of a
verbal one."

**The full answer.** The real novelty is a category most projects don't have at all: **honesty
infrastructure.** Not documentation about honesty — code that makes overclaiming fail.

Concretely, six mechanisms:

| Mechanism | What it prevents |
|---|---|
| `artifact_state` is *derived*, never set. A model can only reach `policy_eligible` when `thresholds.derivation` stops saying `placeholder` **and** the calibration artifact's `status` changes. | An artifact promoting itself. There is no `policy_eligible: true` to set. |
| `derivation: placeholder` is compared **as a string** in the loader. | The threshold being described as "tuned" while it isn't. The string is what holds the gate shut. |
| `MOCK_SMOKE_MODE_NOT_A_DETECTOR` appears in the startup banner, every gRPC response, **every audit row**, and the UI. | A mock result screenshotted as a real one. A banner scrolls away; an audit column doesn't. |
| DB constraint: `detector_mode <> 'REAL_DETECTOR' OR model_sha256 <> ''` | An unattributable score being cited as a result. An unidentified model may only ever appear next to a non-real detector mode. |
| `reliability.expected_calibration_error: null` — **not `0.0`, not omitted.** | A zero reading as "perfectly calibrated" — the single most misleading value that file could hold. `null` forces a consumer to branch on "not measured." |
| `evaluation/reports/gate-*.md` require the tolerance to be **committed before** the run. | A tolerance chosen after seeing the deviation, which is not a tolerance — it's a description of the deviation. |

That last one is the sharpest. Gate 5 says it in one line: *"§1 and §2 go in one commit and §3 in
another. The commit order is the evidence."*

**The line to land it:** "Any team can tell you what their system does. We can tell you, in writing,
what it does *not yet* do — and the code enforces the difference."

## Q: "Isn't this over-engineered for a hackathon?"

**Say this:** "For a demo, yes. For the thing the demo is a prototype *of*, no — and the problem
statement asks for a privacy-preserving control in a banking context, which is a regulated setting.
The parts we over-built are exactly the parts you cannot retrofit."

**The full answer.** Three things genuinely cannot be added later, and we built those:

- **The privacy boundary.** A column, once created and written to, cannot be un-created without
  destroying evidence. If audio lands in the schema on day 2, you own it forever.
- **The audit chain's field set.** Changing which fields are hashed invalidates every prior row.
  It is a breaking change requiring a version bump and a documented re-anchor.
- **Branch protection.** Retrofitting it after three pairs have pushed directly to `main` means
  either rewriting history or accepting an unreviewed change to the seam all three pairs integrate
  against. That's why R-57 lands in Phase 0, not Phase 4.

Everything else — the PWA polish, the dashboards, the webhook — is explicitly Future Scope and
labelled as such in [architecture.md](../architecture.md) §3.

---

# 3. Architecture

## Q: "Draw me the architecture."

```
   ┌──────────────┐
   │   PWA        │  React 19 + Vite. Mic capture, WSS client, risk timeline.
   │  (browser)   │  Holds NO secret. Gets a 60-second ticket and nothing else.
   └──────┬───────┘
          │  ① HTTPS  — auth, session create, ticket mint
          │  ② WSS    — 648-byte binary frames, 50/sec
          ▼
   ┌──────────────────────────────────────────────┐
   │                 GATEWAY                      │  FastAPI 0.115.6 / Python 3.12
   │  ┌────────┐ ┌──────┐ ┌────────┐ ┌─────────┐  │
   │  │ frames │→│ VAD  │→│  ring  │→│ policy  │  │  ← all four are PURE (R-53):
   │  └────────┘ └──────┘ └────────┘ └─────────┘  │    no clock, no I/O, no randomness
   │       │                    │          │      │
   │   auth/ticket          81,920 B    audit     │
   └───────────┬────────────────┬──────────┬──────┘
               │                │ ③ gRPC   │ ④ asyncpg
               │                ▼          ▼
               │        ┌──────────────┐  ┌────────────┐
               │        │   SCORER     │  │ PostgreSQL │
               │        │ ONNX Runtime │  │     16     │
               │        │  AASIST      │  │ audit_event│
               │        │              │  │ (1 table)  │
               │        │ NO DB creds. │  └────────────┘
               │        │ NO purpose.  │
               │        │ NO history.  │
               │        └──────────────┘
```

**Four deployable units. One seam.** The PWA never talks to the Scorer. The Scorer never talks to the
database — it has no credentials at all, which you can verify in the ECS task definition: it has no
`Secrets` block. A service that cannot reach the audit table cannot corrupt the audit trail.

**Four planes,** if a judge asks for the conceptual view:
① Edge & Identity · ② Private Inference & Policy · ③ Privacy & Data · ④ Security, Observability, Cost.

## Q: "Why two services? Why not one process?"

**Say this:** "Three reasons, and only one of them is scaling. The important one is that the seam is
the auditability claim — a Scorer that can't see the purpose can't be accused of making the policy
decision. Second, the two halves have completely different runtime needs: one wants a GPU, the other
wants a database connection. Third, it lets two teams work in parallel against a frozen contract."

**The full answer.**

- **Claim integrity (the real reason).** In one process, "the model doesn't make policy decisions" is
  a code-review convention. Across a gRPC boundary with a message that has no action field, it's a
  property. When a regulator asks "could the model have decided this?", the answer is a `.proto` file,
  not a promise.
- **Blast radius / least privilege.** The GPU service holds no secrets, no DB credentials, no ticket
  signing key. Compromising it gets you audio in flight and nothing durable.
- **Independent runtime shape.** The Scorer needs CUDA, an ONNX wheel, ~40 MB of model, and a warm
  session. The Gateway needs asyncpg, a JWKS cache, and 1 worker. Packaging those together means the
  Gateway image carries a CUDA runtime it never uses.
- **Parallel delivery.** Pair A builds the Gateway against the frozen proto while Pair B builds the
  Scorer. Neither blocks. That's why `MockDetector` exists — Pair A's WSS contract tests are a Phase 1
  exit criterion and the model is a Phase 2/3 deliverable.

**Follow-up you should expect — "doesn't gRPC add latency?"** Yes, sub-millisecond in-VPC, against a
640 ms hop budget. It is not the bottleneck; the model forward pass is. And we score one window per
640 ms per session, not per frame — 50 frames arrive, 1 gRPC call goes out.

## Q: "Why exactly 648 bytes? Why not just accept whatever the client sends?"

**Say this:** "Because our credibility rests on a parity claim — the same audio produces the same
policy trace on an AWS GPU and on a laptop CPU. You can only check that if both tiers receive
byte-identical input. The moment one tier pads a short frame and the other trims, they're scoring
different audio and the parity test is measuring nothing."

**The full answer.** 648 = 8 + 640.

- 16 kHz mono, 20 ms per frame → 320 samples → `int16` → **640 bytes** payload.
- **8-byte** `uint64` big-endian sequence prefix.

Rejection, not coercion, is **R-24**. Padding doesn't fix an error; it converts a loud error into a
silent one. A 647-byte frame closes the socket with `PROTO_FRAME_SIZE` / WS 1003.

**The detail that makes people sit up — mixed endianness in one frame.** The payload is
little-endian; the header is big-endian. Deliberate, recorded as decisions D-1 and D-2, pinned by
R-25:

- **Payload LE** because a browser's `Int16Array` is natively little-endian on every platform anyone
  will demo on. Forcing a byte swap in JavaScript 50 times a second is gratuitous work on the client.
- **Header BE** because that's network byte order, the convention for protocol framing, and
  `struct.unpack(">Q")` is unambiguous.

R-25 pins both so nobody "fixes" the inconsistency later and breaks every client at once.

## Q: "Walk me through what happens to one 20-millisecond chunk of audio."

Nine steps. Learn the shape, not the words.

1. **Parse** — exactly 648 bytes, sequence increments by exactly 1, else close. `frames.py` returns
   samples as a `memoryview(...).cast("h")` — a zero-copy *view*, not a copy. Fewer copies of raw
   audio is both faster and a smaller privacy surface.
2. **VAD** — voiced or not.
3. **Ring** — unvoiced frames are **discarded**, and critically do **not** advance the hop counter.
   Silence cannot produce a scored window. If it could, staying quiet would generate near-silent
   low-scoring windows that dilute the evidence.
4. **Hop check** — 32 voiced frames complete a 640 ms hop. The counter is **decremented by
   `HOP_SAMPLES`**, not zeroed: if 33 frames' worth arrived in a batch, zeroing would silently lose
   the 33rd frame's progress.
5. **Window emit** — only if a hop completed **and** the buffer is full. First window therefore needs
   4 full hops ≈ 2.56 s of *voiced* audio.
6. **Score** — one gRPC call. 81,920 bytes out, one `spoof_risk` in `[0,1]` plus quality flags back.
7. **Policy** — `engine.observe(...)` → risk state and, if it changed, an action.
8. **Audit** — one row, hashed onto the chain. **Before** the client is told anything.
9. **Emit** — `risk.event` always; `policy.action` on a state change or while `high`.

And in a `finally` block: `ring.clear()` (zero-fill then clear), decrement `live_streams`,
`registry.end_stream`, `audit.forget`. Not in the happy path, not in the exception handler — in
`finally`, so it runs on clean close, protocol error, unhandled exception, and a client that vanished
mid-frame.

## Q: "Four ordering decisions in that path are security controls. Which?"

This is a great question to be asked and a great one to volunteer.

| Order | Why it is a control, not style |
|---|---|
| Ticket + Origin validated **before** `websocket.accept()` | An unauthenticated peer never gets an accepted socket, so it never reaches the frame parser. The common shape — accept, then check, then close — hands an attacker your parser. |
| `session.open` must be the **first** message | A binary frame arriving first is a protocol error, not audio to buffer. There is no bound purpose yet, so there is nothing a decision could attach to. Audio you cannot act on is audio you should not hold. |
| Audit row written **before** the action is emitted | "The product claim is persistent evidence, so an unrecorded decision is not a decision." If the write fails the client gets an error, not an action. |
| `ring.clear()` in `finally` | R-14 as control flow rather than as a promise in a document. |

## Q: "The Scorer times out on window 7. What happens?"

**Say this:** "We drop that window, record that we dropped it, and keep the stream. We do **not**
retry, and we do **not** count it as low risk."

**The full answer.** `ScorerUnavailable` → increment `windows_dropped`, advance `window_seq`,
`continue`.

- **No retry** — a retry would enter the same evidence into the k-of-n count twice and corrupt it.
- **Not counted as low** — R-09. Absence of evidence is not evidence of absence. If a Scorer failure
  counted as "clean," then *failing the Scorer becomes the attack*: flood it, get every window
  classified clean, walk the payment through.
- The window is visible in metrics as dropped, so a session that decided on 2 of 5 available windows
  is distinguishable from one that had 5.

---

# 4. The policy engine, in full

This section is deliberately exhaustive. If a judge picks one component to interrogate, it will be
this one, because it is where the product claim lives.

## Q: "What does the policy engine actually do?"

**Say this:** "It converts a stream of independent per-window scores into one accountable decision.
Three jobs: decide which windows count as evidence, decide when there's enough of it, and translate
'enough' into an action sized to what the call is for. It holds no clock, no connection and no audio,
so any past session can be replayed from the audit table and the action re-derived."

**The full answer.** File: [gateway/app/policy/engine.py](../gateway/app/policy/engine.py).

### 4.1 Its inputs and outputs

**In**, per window: `spoof_risk` (a float in `[0,1]`), `eligible` (bool, derived from quality flags
and window completeness), and — held from session open, not per window — `purpose_code`.

**Out**: a `RiskState` ∈ {`collecting`, `uncertain`, `high`}, and optionally an `Action` ∈
{`continue`, `verify`, `hold`, `escalate`} plus a `ReasonCode`.

**Config**, loaded once at startup from the hashed bundle: `high_window_risk` (0.78, placeholder),
`k` (3), `n` (5), and the `purpose → {state → action}` table.

### 4.2 The algorithm, in the exact order it runs

```
observe(spoof_risk, eligible):

  1. if not eligible:                    return current state, NO action
                                         # the deque is not touched at all (R-09)

  2. if state is already HIGH:           return HIGH, NO state change
                                         # high is sticky (R-13)

  3. append (spoof_risk >= threshold) to a deque(maxlen=n)

  4. if len(deque) < n:                  return COLLECTING

  5. if sum(deque) >= k:                 return HIGH
     else:                               return UNCERTAIN
```

Five branches. Each is a rule with a name.

**Step 1 — ineligible windows are skipped, never counted low (R-09).** The deque is not appended to.
Not appended as `False`, not appended at all. Consequence: a session with three ineligible windows
reaches its decision *later*, rather than reaching a falsely reassuring decision *sooner*. This is
the single most security-relevant line in the file, for the reason in §2 ② — otherwise degrading your
own audio lowers your risk state.

**Step 2 — `high` is sticky (R-13).** Once `high`, always `high`, for the life of the session.

*Why:* an attacker who is detected and then goes quiet must not be able to wait out the state. If the
deque simply rolled forward, 5 clean windows after the detection would silently return the session to
`uncertain`, and the accumulated evidence would evaporate. Evidence is not a moving average.

*What it costs:* a false positive is also sticky, and today there is no way to clear it. That is
honest and stated: clearing `high` requires an explicit human resolution step, which is Phase 4 and
does not exist. So today `high` is terminal for the session. **Say that out loud if asked** — it's a
real limitation with a designed answer, which is a much better story than pretending it's tunable.

**Step 3 — the threshold comparison is `>=`, and the window is a bounded deque.** `maxlen=n` means
"the most recent n eligible windows" is a property of the data structure, not of bookkeeping code
that could drift.

**Step 4 — `collecting` is a first-class state, not a null.** Fewer than n eligible windows means we
have not yet earned the right to an opinion. Most implementations return "low risk" here, which is a
lie: you don't know. `collecting` maps to `continue` for every purpose, so behaviour is identical —
but the *audit row* records `collecting`, so afterwards you can distinguish "we assessed this and it
looked fine" from "we hadn't assessed it yet." That distinction is the difference between an evidence
trail and a log.

**Step 5 — k-of-n, and the load-bearing refusal.** `sum(deque) >= k`.

`PolicyThresholds.__post_init__` **raises on `k < 2`** at load time — before any audio arrives, before
the port is bound. From `policy.yaml`: *"Lower k for a more responsive demo is a refusal, not a
discussion."*

*Why `k=1` is indefensible:* a single 2.56-second window over threshold is a cough, a codec artifact,
a moment of clipping, a door slamming. Acting on it produces a system whose false-positive rate is
whatever your model's per-window error rate is — and per-window error rates on out-of-distribution
audio are bad. k-of-n is a cheap, explainable temporal filter that trades a fraction of a second of
latency for an order-of-magnitude reduction in spurious actions.

*Why not a fancier filter* (EWMA, HMM, CUSUM)? Two reasons, both about the deliverable. First,
explainability: "3 of the last 5 windows crossed 0.78" fits in one sentence to a fraud analyst and in
one line of an audit row. An EWMA state cannot be re-derived by a human reading the table. Second,
`k` and `n` are the two numbers a policy owner will actually want to tune, and they are legible.
A smoothing constant is not.

### 4.3 The purpose → action table, and why it is the product

```
                  collecting   uncertain    high
payment_release    continue     verify       hold      ← money leaving; step-up can resolve it
beneficiary_change continue     verify       escalate  ← where future money goes; irreversible
account_recovery   continue     verify       escalate  ← account takeover; hands over everything
support_enquiry    continue     continue     verify    ← nothing at stake; high is the floor
```

Read the two deliberate asymmetries out loud when you present this:

**`payment_release` at `high` → `hold`, not `escalate`.** A queue for a human review team is the
wrong instrument for a transaction a step-up can resolve in seconds. Escalating everything trains the
review team to rubber-stamp.

**`support_enquiry` at `uncertain` → `continue`, deliberately.** Interrupting a customer's balance
query with identity checks because 5 windows were ambiguous is a **false-positive cost paid by an
innocent person**. R-06 says the response must be proportionate to what is at risk, which here is
nothing. And `high` → `verify` is the **floor**: strong evidence is never ignored entirely, whatever
the purpose.

That pair — same evidence, opposite treatment, each with a written reason — is the proportionality
argument. It is also the thing a judge remembers.

**Every purpose must map all three states.** An unmapped state would be a `KeyError` at decision time
— a crash during the demo at the exact moment risk was detected — so the loader refuses the bundle at
startup instead.

### 4.4 The closed vocabulary, and the three places it's enforced

`continue` · `verify` · `hold` · `escalate`. **`approve` and `deny` do not exist.**

| Layer | Enforcement |
|---|---|
| Application | `Action` enum in `engine.py` — no member exists |
| Loader | `loader.py::_parse_action` raises `PolicyLoadError` |
| API | `PurposeCode`/action enums in `contracts/openapi.yaml` |
| Database | `CHECK` constraint in `schema_contract.py` |
| Client strings | close-reason table is all constants; even the unknown-code fallback is `"stream closed"`, not `"rejected"` |

The DB deny-list is broader than the rule: `FORBIDDEN_ACTION_VALUES` blocks `approve`, `deny`,
`allow`, `block`, `reject` — *"the synonyms a well-meaning contributor reaches for next."*

**Why three-plus independent sites?** So that adding an authorization outcome costs three commits and
three reviews rather than one line. R-07 calls it a **stop condition for the project, not a config
change.**

**The framing to use:** "This system produces verification *pressure*. A human authorises. We are not
in the authorization path, and we made it expensive to get into it."

### 4.5 Purity, and why it's the auditability claim

`PolicyEngine` holds no clock, no socket, no DB handle, no randomness (R-53, shared with `frames.py`,
`ring.py`, `chain.py`). Everything arrives as an argument.

What that buys: `replay()` runs a whole session's observations through a fresh engine and gets
identical results. **You can replay any past session from the `spoof_risk` column of the audit table
alone and verify the action taken was the action the policy required.**

That is not a testing convenience. For a system whose product claim is *auditable decisions*, it is
the claim itself — the difference between "trust our decision" and "here is the decision, the inputs,
and the function; re-run it yourself."

### 4.6 Questions you will get about the engine

**"Why 0.78?"** It is a placeholder and must never be presented otherwise. See §5.

**"Why 3 of 5 and not 4 of 7?"** `n=5` at a 640 ms hop is ~3.2 s of eligible speech, so a decision
lands a few seconds into a call — fast enough to precede a transaction. `k=3` is a simple majority.
Both are policy parameters in a reviewed file, and the *right* values come from a cost-sensitive
matrix with false-accept and false-reject costs written down and owned by a named person. That person
is currently unassigned (`derivation_owner: "TODO before Phase 3"`), and we say so.

> ⚠️ **Known documentation defect — fix before you present.** The comment above `evidence:` in
> `policy.yaml` says *"n: 5 at ~40 ms hop is roughly 200 ms of eligible speech."* That contradicts
> `HOP_MS = 640` in `constants.py`. The correct figure is **~3.2 s**. The code is right; the comment
> is wrong. If a judge reads that comment and asks, say the comment is stale and quote 640 ms.

**"What if the attacker knows it's 3 of 5?"** Kerckhoffs applies: the parameters are not the secret.
Knowing the rule does not let you cross it while producing synthetic audio — you'd have to keep 3 of
every 5 windows below threshold, which means keeping most of your speech non-synthetic, which defeats
the purpose of cloning a voice. What knowing the rule *does* enable is degrading audio to force
ineligibility — which is exactly why R-09 exists.

**"Can the model influence the policy?"** No, and that's structural. It cannot see the purpose or the
history and has no action field. The diagnostics sidecar is the interesting near-miss: its
`observe()` return value is **discarded**, with a comment saying *"Do not assign this to anything."*
Decision D-12 / R-12 keeps diagnostics advisory until an ablation study proves they add value without
a fairness regression. A comment saying "advisory" is a promise someone breaks in a hurry on Day 4; a
discarded return value is a fact you have to edit a line to change, and that edit shows up in a diff.

---

# 5. The Scorer and the model

This is the section where honesty is your only viable strategy, and it is genuinely a strong one.

## Q: "Which model do you use?"

**Say this:** "AASIST — raw-waveform spectro-temporal graph attention, the official PyTorch
reference. It's exported to ONNX and served through ONNX Runtime so the same graph runs on a CUDA GPU
in AWS and on a CPU laptop."

**The full answer.**

- **Primary:** AASIST. Input contract `[1, 40960]` float32 — a normalized mono raw waveform, 2.56 s
  at 16 kHz. Output `[1, 1]` — one scalar raw logit.
- **Comparators we are required to run** (Baseline gate): **LFCC-LCNN** and **RawNet2**. AASIST must
  match or exceed both on the declared dev protocol *before anything else proceeds*. If it doesn't,
  the gate says investigate the input pipeline before adding features — because a graph-attention
  model losing to LFCC-LCNN usually means the audio is wrong, not the model.
- **Explicitly rejected for five days:** ensembles/fusion, an SSL-encoder candidate, CQCC-GMM as a
  live detector. Scoped out in writing, not forgotten.
- **Ablation-gated (advisory only):** CQT, phase, bicoherence, prosody diagnostics.
- **Artifact exists:** `ml/models/aasist.onnx`, sha256 `45d6eefe…a48505`, recorded in
  `ml/export_summary.json` with the PyTorch↔ONNX **max absolute difference = 6.26 × 10⁻⁶**.

**Two export details worth volunteering** — they show you thought about the failure mode nobody
checks:

- **PCM16 → float32 division by 32768.0 happens *outside* the graph**, and it's a named constant.
  If one tier baked it into the export and the other did it in the client, you'd score audio at half
  amplitude with no error anywhere — just quietly worse numbers.
- **A two-class output is *refused*, not reduced.** `_extract_raw_score` accepts a scalar, `(1,)`, or
  `(1,1)`. Given two logits it raises, because choosing between `out[1]`, `out[1] - out[0]`, and
  `softmax(out)[1]` is a **class-orientation decision** that must be explicit at export time.
  Guessing it at serving time is how a detector ends up inverted while every shape check passes.

## Q: "What's your accuracy? Your EER?"

**Say this — do not soften it:** "We don't have one yet, and I won't quote a number from a paper as
if it were ours. The eight evaluation gates are written, templated, and unrun. What we've built and
proven is the control plane the score feeds; the measured detection numbers are Phase 2 work."

**The full answer, and why this is a strong position.**

All eight gate reports exist as **templates with `status: not-run`**: Data · Baseline · OOD ·
Calibration · ONNX parity · Quantization · Privacy · Demo. Every one is a release blocker. Two are
hard blockers: **ONNX parity blocks deployment**, **Privacy blocks demo release**.

Why refusing to quote a number is the right move, and how to say it:

> "ASVspoof leaderboard EERs are real numbers on a specific corpus with a specific protocol. Quoting
> one for a system running my export, my window length, and my calibration would be borrowing
> someone else's evidence. The number I *can* give you is our export fidelity: PyTorch to ONNX
> agrees to 6.26 × 10⁻⁶ on the committed fixture set. That's a measurement we made."

Then pivot to what's designed, because the *design* of the evaluation is itself the answer:

**Splits:** `train`, `dev_calibration`, `eval_locked`, plus `eval_generator_heldout`,
`eval_codec_language_heldout`, `demo`. Grouped **before augmentation**, disjoint by speaker, parent
sample, session, and generator family+version.

**Why grouping-before-augmentation matters (R-38):** augment first and a speaker's variants land on
both sides of the split, so you validate on data you trained on and your numbers are inflated with no
error anywhere. `datasets/manifest/manifest.schema.json` makes grouping a **required field**, not an
assumption.

**Why `eval_locked` is opened once (R-37):** fitting anything on it — a model, a threshold, or a
calibration mapping — turns a held-out estimate into a training estimate. The failure is **silent and
unrecoverable**: once `eval_locked` has informed a choice, no re-run restores its independence.

**OOD is failed by an *unreported cohort*, not a bad number.** Gate 3's own words. Held-out
generator family+version, codec chain, language, capture device, attack type, duration band — each
reported separately, with the worst-group metric and the max gap. *"A held-out generator that halves
accuracy is a finding. An unreported one is a claim that accuracy is unknown while being described as
measured."*

**Datasets:** ASVspoof 2019 LA (train/dev), ASVspoof 2021 LA+DF (reported separately), MLAAD at a
pinned revision and generator-disjoint, IndicVoices (accepted terms, bona-fide only), and a team
consented local set.

**Forbidden (R-39):** using the 8 kHz/16 kHz sampling boundary as spoof evidence. Sampling rate is a
*channel* property. A model that learns "8 kHz means fake" has learned that people on old phone lines
are fraudsters — wrong and discriminatory. Volunteer this one; it lands.

## Q: "So the demo is fake?"

**Say this:** "The transport, the policy, the audit chain and the privacy boundary are real and
tested. The detector runs in one of two modes and the mode is stamped in every audit row, so you can
always tell which you're looking at. Let me show you the row."

**The full answer.** Two detector implementations, one interface, and the mock is engineered so it
cannot be mistaken for a measurement.

**`MockDetector`** — a BLAKE2b digest over `session_ref` **and** the window's PCM bytes, mapped to a
pseudo-logit in (−6, +6). Design choices, each with a reason:

- **Deterministic across processes and hosts.** Same audio + same session ⇒ same score, every time.
  Python's `hash()` would have been wrong: it's salted per process, so two runs would disagree and the
  disagreement would look like a parity failure.
- **Different windows in the same session get different scores.** Derived from `session_ref` *alone*,
  every window would score identically, so k-of-n could only ever produce all-high or all-low and the
  state machine would be wired but unexercised.
- **`window_seq` is deliberately not an input** — the proto says the Scorer is stateless and must not
  correlate windows, so the mock cannot develop a dependence a real detector wouldn't have.
- **Emits a logit, not a probability**, specifically so the Platt transform is genuinely exercised.
  (This is the detail that surfaced H-6 — see below.)

**Why it cannot be mistaken for real (R-46):** it's a *separate class*, not a detector with a stub
model. It reports `model_version = "mock-smoke-not-a-detector"`, `model_sha256 = sha256(b"")`, and
`detector_mode = MOCK_SMOKE_MODE_NOT_A_DETECTOR` — spelled the long way so a reader's eye cannot skip
it. There is **no configuration that makes it report `REAL_DETECTOR`**. The mode appears in the
banner, every gRPC response, every per-window log line, **every audit row**, and the UI. And the DB
constraint `detector_mode <> 'REAL_DETECTOR' OR model_sha256 <> ''` closes the loop.

One more: `Detector.__slots__ = ()` on the abstract base. Not cosmetic — `__slots__` only removes the
instance `__dict__` if *every* class in the MRO declares it. Without that line,
`detector.last_window = window` would succeed and the R-14 claim would be decoration. With it, that
assignment raises `AttributeError`, so a future edit that tried to stash 2.56 seconds of a caller's
voice on the detector fails **at the point of the edit** rather than in a privacy review.

## Q: "Your threshold is 0.78. How did you derive it?"

**Say this:** "We didn't. It's a placeholder so the state machine has something to compare against,
and the system structurally cannot claim policy eligibility while it stays a placeholder. Here's
exactly what it would take to make it real."

**The full answer.** From `policy.yaml`, verbatim in spirit: *0.78 is NOT derived from a ROC curve,
NOT from an EER, NOT from a cost matrix, NOT from any evaluation on this project's data.*

Four steps to make it real, in order:

1. A fitted calibration on `dev_calibration` **only** (R-37).
2. A cost-sensitive decision matrix with false-accept and false-reject costs **written down and owned
   by a named person**. There is no universally valid 0.78 — the right operating point for a payment
   release is not the right one for a support enquiry.
3. Re-derivation of the value from that matrix, committed alongside it.
4. `derivation` changed from `placeholder` to that derivation's identifier.

Until step 4, `PolicyBundle.artifact_state` can only ever return `demo_eligible`. **The string
`placeholder` is what holds the gate shut** — that's a structural control, not a promise.

Artifact states: `research_only` → `demo_eligible` → `policy_eligible`. Only `policy_eligible` may
drive a high-risk action.

## Q: "Is `spoof_risk` a probability?"

**Say this:** "No, and we're careful about that word. It's a score. Calling it a probability requires
a fitted calibration, and ours is an identity placeholder — it changes no score by any amount."

**The full answer.** R-11. While `calibration.json` reads
`status: placeholder-not-policy-eligible`, `spoof_risk` may only be described as a *score* — never a
probability, likelihood, or confidence — in the UI, in logs, in reports, or out loud.

The artifact is honest to the point of being instructive:
`calibration_version: "0.0.0-placeholder-identity"`, `fitted: false`, `slope: 1`, `intercept: 0`, and
the reliability block is **`null`, not `0.0`, not omitted** — because a zero would read as perfectly
calibrated, the single most misleading value that file could contain, and an omitted key would let a
consumer treat a missing metric as a passing one.

## Q: "What's the hardest bug you found?" ← *volunteer this one*

**Say this:** "A calibration convention mismatch that would have made our core feature silently
unreachable, and the reason nobody caught it is genuinely interesting: the placeholder values are the
one point in parameter space where both conventions agree."

**The full answer — H-6.** Two parts of the system disagreed about what `raw_score` means.

- The **declared** transform in `calibration.json` reads `sigmoid(a · logit(raw) + b)` — that treats
  `raw` as a **probability** and round-trips it.
- The **executed** transform in `scorer/app/calibration.py` computes `sigmoid(slope · raw + intercept)`
  — no `logit()`, treating `raw` as a **logit**.

The Scorer's reading is deliberate and correct: the mock emits a logit in ±6.0 precisely so the
transform is exercised.

**Why it would have been fatal.** Under the probability reading, the highest attainable `spoof_risk`
is `sigmoid(1 · 1.0 + 0) = 0.7311`. The window threshold is **0.78**. So the `high` band — and
therefore `hold` and `escalate`, i.e. **the entire product function** — would have been structurally
unreachable. The demo would have run, scored, accumulated, and never once acted.

**Why both test suites passed.** `(slope, intercept) = (1.0, 0.0)` is the one point in parameter
space where both conventions produce the same two numbers while meaning different things. So the
audit suite proved the identity claim arithmetically true under the formula *it* reimplemented, the
Scorer's suite proved *its* transform correct, and neither ever contradicted the other. A **fitted**
pair would not be so forgiving: coefficients fitted under one convention and applied under the other
produce a plausible, monotone, wrong curve with no error anywhere.

**What we did:** documented it in the artifact itself, named the function to fit against
(`sigmoid(slope · raw_logit + intercept)`), and tracked it as open decision **H-6** before fitting.

**Why to volunteer this.** It demonstrates three things no slide can: that you understand your own
numerics, that you write down what you don't know, and that you found a class of bug — *agreeing
tests that agree by coincidence* — that most teams never look for.

---

# 6. Auth

## Q: "How do you stop someone else from streaming audio into a session?"

**Say this:** "Four hops, each one there because the previous one can't do the job. Cognito issues a
JWT. The JWT creates a session, which binds the purpose server-side. The session mints a 60-second
single-use ticket. The ticket, plus an Origin allow-list, gets you a WebSocket — and all of that is
checked before we accept the socket."

```
① Cognito SRP (AWS) / local JWKS test issuer (local)
       │  RS256 JWT, ~1 hour, iss + aud verified, alg pinned
       ▼
② POST /api/v1/sessions        binds purpose_code + context_value_band SERVER-SIDE
       │                        HMACs client_call_ref → call_ref
       ▼
③ POST /api/v1/stream-ticket   60 s · single-use · bound to session_id + sub
       │  carried in  Sec-WebSocket-Protocol: sih-ticket.<base64url>
       ▼
④ WSS /ws/v1/stream            + Origin allow-list — ALL checked before accept()
```

## Q: "Why the extra ticket? Why not just send the JWT?"

**Say this:** "Because the browser WebSocket API cannot set headers. You get the URL and the
subprotocol list, and that's it. Every other option leaks the credential."

**The full answer — the three options and why two are wrong.**

| Option | Why we rejected it |
|---|---|
| Token in the **query string** | URLs land in access logs, proxy logs, browser history, and `Referer` headers. You have published your credential. |
| **Cookie** | Cross-site WebSocket hijacking. Browsers attach cookies to cross-origin WS handshakes and there is no preflight to stop it. |
| **Subprotocol ticket** ✅ | The one header a browser *does* let you populate. Combined with a 60 s TTL, single use, and binding, a leak is nearly worthless. |

And the Origin allow-list closes the hijacking hole the cookie option would have opened.

**Ticket properties, each doing a specific job:** 60-second TTL (a captured ticket expires before
it's useful) · single-use, spent from a replay cache by `jti` · bound to `session_id` **and** `sub`
(a stolen ticket cannot be pointed at another session or used by another subject).

## Q: "Why isn't the ticket a JWT?"

**Say this:** "Because a fixed-format MAC has no algorithm field to confuse."

**The full answer.** JWTs carry a decade of algorithm-confusion history: `alg: none`, RS256 swapped
for HS256 so the public key becomes the HMAC secret, and so on. Every one stems from **the token
telling the verifier how to verify it.**

Our format is `base64url(payload) . base64url(hmac)`. No header. No algorithm field. One hard-coded
verification path.

Plus **domain separation**: the MAC covers `b"sih26104/stream_ticket/v1\x00"` prepended to the
payload, so a ticket signature can never be confused with any other HMAC in the system even if a key
were reused.

**Verification order, which is itself the control:**

1. **Check the MAC — before parsing the JSON.** Never parse attacker-controlled structure before
   authenticating it.
2. Check expiry.
3. `hmac.compare_digest` on `session_id` and `sub` — constant-time, so you can't time your way to a
   valid binding.

**Every failure returns one `TicketError` code.** Expired, forged, wrong session, wrong subject —
indistinguishable to the caller. Distinct messages are an oracle an attacker uses to narrow down what
they got wrong.

**The bit that looks wrong and isn't:** there's a `peek_binding()` function whose docstring shouts
**UNTRUSTED HINT**. It reads `session_id` and `sub` from the *unverified* payload — because `verify()`
needs to know what to compare against. The flow is: peek to get the claimed values, pass them into
`verify()` as `expected_*`, where the MAC check runs first. A forged `sid` fails the MAC rather than
selecting a different session. The unsafe-looking function is safe **because of where it sits in the
sequence**, and the docstring says exactly that.

## Q: "How do you validate the JWT?"

**The full answer.** One validation path for both tiers — R-04. Local uses a test JWKS issuer, AWS
uses Cognito, and the **code does not branch**. Issuer, audience, and JWKS URL are config.

- `_ALGORITHMS = ("RS256",)` — **pinned in config, never read from the token.** And checked *before*
  fetching a key, so a malformed token can't be used to hammer the JWKS endpoint.
- `JwksCache`: 600 s cache, a **30-second minimum-refresh floor**, 3 s timeout, injectable clock and
  HTTP client. The refresh floor is anti-amplification — without it, tokens with random `kid` values
  make your Gateway hammer Cognito on your behalf. The injectable clock makes expiry testable without
  `sleep()`.
- `Principal` carries **only `sub` and `groups`.** No email, no name, no phone. What you don't extract
  cannot leak into a log line.
- One `AuthError("unauthorized")` for every cause.

## Q: "You have a local login with no password. Isn't that a hole?"

**Say this:** "It's a test harness, it's labelled demo-only in the UI, and the application **refuses
to start** if it's configured on the AWS tier."

**The full answer — R-05, and it's enforced, not promised.** `config.py`'s `_validate` refuses to
start under `DEPLOYMENT_PROFILE=aws-gpu` if:

- the execution provider isn't CUDA (**R-45** — a silent CPU fallback on the GPU tier is a *failure*,
  not a degradation, because it invalidates every latency number recorded that day);
- the issuer URL contains `testidp`, `localhost`, or `127.0.0.1` (**R-05**);
- **any** allowed origin uses `http://`.

Plus `_no_wildcard_origins`: no `*`, scheme required. And `extra="forbid"` on `Settings`, so a
typo'd environment variable is a startup crash rather than a silently-ignored setting.

**The pattern worth naming out loud:** this is the config-not-branch rule done properly. The
application never asks "am I on AWS?" to *decide behaviour*. There *is* an `is_aws` property, and it's
restricted to banners, metrics labels, and audit fields. Instead the tier is **validated at startup**,
so a misconfiguration is a crash at deploy time rather than a quiet security downgrade during a judged
demo.

---

# 7. Secrets

## Q: "Where do your secrets live?"

**Say this:** "Five entries in AWS Secrets Manager, injected by ECS as `secrets:` — never
`environment:`. The application reads four of them as `SecretStr`, which prints as asterisks. Nothing
is in Git, in an image, or in the client."

**The full answer.**

**The four the application reads,** all typed `SecretStr`:

| Env var | Protects | Guard |
|---|---|---|
| `DATABASE_URL` | Postgres connection incl. password | — |
| `HMAC_KEY` | Pseudonymizes `client_call_ref` → `call_ref` | ≥ 32 chars |
| `TICKET_SIGNING_KEY` | MACs the stream ticket | ≥ 32 chars |
| `AUDIT_CHAIN_KEY` | Keys the audit hash chain | ≥ 32 chars |

`SecretStr`'s `repr` is `**********`. So a stack trace, a debug print, or a logged settings object
cannot leak a key. You must call `.get_secret_value()` explicitly — which makes every real use of a
secret **visible in a diff**.

## Q: "Why are there five secrets but four variables?" ← *a great question to be asked*

**Say this:** "Because ECS injects exactly one Secrets Manager value per environment variable and
cannot interpolate. It can't build `postgresql://user:${password}@host/db`. Our config wants the
assembled URL, so the URL is its own secret."

**The full answer.** The five:

```
sih26104/db-password          ← random
sih26104/ticket-signing-key   ← random
sih26104/hmac-key             ← random
sih26104/audit-chain-key      ← random
sih26104/database-url         ← NOT random: the full assembled URL
```

`db-password` and `database-url` hold **the same password in two shapes.** The alternatives were:

- an **entrypoint shim** that assembles the URL — a new moving part, and the password lands in a
  process argument list where `ps` can read it;
- **changing the app's config contract** — worse coupling for a deployment detail.

A fifth secret is the smallest honest option, and the reasoning is recorded in
`infra/cdk/lib/compute-stack.ts` rather than left for someone to rediscover.

**The trap it creates, and we document it:** rotate the pair **together**. Rotate one alone and you
get an authentication error that looks exactly like a networking problem — `psql` from a one-shot task
works while the application can't connect.

**Second trap, in the rotation recipe:** `openssl rand -base64 32 | tr -d '/+='`. The `tr` is not
cosmetic — base64 emits `/`, `+`, and `=`, all of which must be percent-encoded inside a URL's
userinfo field. Stripping them avoids a whole class of failure that presents as bad credentials.

> ⚠️ **Documentation conflict to fix.** `phases.md` §1.2 step 0.7 and the Phase 0 DoD both say
> *"Four Secrets Manager placeholder entries."* `aws-setup-instructions.md` §6 says *"There are FIVE,
> not four"* and names `infra/cdk/lib/secrets-stack.ts` as the tie-breaker. **Five is correct.**

## Q: "Which one can you never rotate?"

**Say this:** "`sih26104/audit-chain-key`, once any audit event exists. Rotating it makes every prior
`event_hash` unverifiable, and there is no migration path."

**The full answer — R-27.** The chain is a keyed HMAC. Rotate the key and the entire evidence trail
reads as tampered. Restoring from a snapshot is the only recovery path. So: rotate it **once**, before
the first real session, then never again. It's in `memory.md`'s standing-traps list, and the migration
that drops the table warns that re-running `upgrade` afterwards produces an empty chain and the key
must **not** be rotated to match.

## Q: "Why `secrets:` rather than `environment:` in the task definition?"

**Say this:** "Because a value in `environment:` is visible in plaintext to anyone who can run
`aws ecs describe-task-definition`. A value in `secrets:` stores only an ARN; the agent resolves it at
container start."

And the detail worth adding: **the Scorer container has no `Secrets` block at all.** The detection
service holds no credentials and cannot reach the database. That's the seam expressed in IAM.

## Q: "Local and cloud must differ, surely?"

| | `local-cpu` | `aws-gpu` |
|---|---|---|
| Provider | Docker secret or git-ignored `.env` | Secrets Manager |
| **Logical names** | **identical** | **identical** |
| Injection | env file | ECS `secrets:` |

Same logical key names on both tiers, so no code branches on where a secret came from.
`gateway/.env.example` documents every variable **with the reason for its guard**, and every
placeholder is an obviously-fake `PLACEHOLDER-…` string that still satisfies the ≥32-char rule so the
file works out of the box.

**R-34:** no secrets in Git, images, or the client. `secret-scan` is a required check on `main`. And
**no real account ID, ARN, or secret value appears in any document in this repository** — placeholders
are obviously fake, which is why every AWS doc uses `<ACCOUNT_ID>`.

Generation recipe: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

---

# 8. Database and the audit chain

## Q: "Show me your schema."

**Say this:** "One table. `audit_event`. 26 columns, 22 of which are in the hash. There is no users
table, no sessions table, and no ORM models package — sessions live in process memory and the audit
table is the only durable state."

**The full answer.** The single table is a constraint, not an oversight.
`technical-design.md` §5.1 is titled **"the complete list,"** and the deny-list test asserts that list
as an **exact set**. A second table isn't forbidden — it is a *deliberate act that must extend §5.1
first.* Which is why the retention worker records its receipts as structured log output instead of
into an `audit_retention_run` table. Adding a table is a decision with a paper trail.

## Q: "How do you know the schema is what you think it is?"

**Say this:** "The schema is declared once as data, and three separate consumers read that same
declaration — the migration generates its DDL from it, the tests assert the *live* database against
it, and the chain verifier imports its column list from it. They agree by construction, not by
comment."

**The full answer.** `audit/migrations/schema_contract.py`.

Columns are declared with the **reflected PostgreSQL spelling**, not what we typed in the DDL —
because *"a test that compares against the DDL string it generated proves only that string formatting
works."*

Three consumers:

1. `versions/0001_audit_event.py` **generates** its `CREATE TABLE` from the tuples.
2. `audit/tests/` asserts the generated DDL **and** (under the `integration` marker) the deployed
   `information_schema`.
3. `scripts/verify_audit_chain.py` **imports** `COLUMN_NAMES` instead of re-listing columns — so a
   verifier that reads a column the migration never created fails **at import**, not at 2 a.m. during
   a demo.

**Two guards that make it fail closed:**

- The module ends with `_self_check()`, executed **at import**, which runs the declared allow-list
  through the deny-list. So *a commit that adds a forbidden column cannot even be imported.* Its
  docstring names the attack it closes: without it, the dangerous change — a forbidden column added
  to the allow-list **and** the migration together — would pass every per-column check.
- `test_only_one_revision_exists` asserts the precondition that makes generated DDL safe. Generating
  DDL from a contract is unusual for Alembic, where a landed revision is immutable text; it is only
  safe while this is the sole revision. The moment a second revision lands, that test fails and forces
  the DDL to be frozen into literal text. *"A guard that enforces its own precondition is the only
  kind worth relying on."*

**And it is stdlib-only on purpose.** *"The privacy control is worth nothing if it only runs in a CI
job that has PostgreSQL, SQLAlchemy, and Alembic installed — that is the job people skip when it is
slow or flaky."* Importing it costs nothing, so the deny-list runs in the fastest lane there is.

## Q: "How do I know the audit log wasn't edited?"

**Say this:** "Each row's hash covers its own contents concatenated with the previous row's hash. Edit
a row and its hash stops matching. Delete a row and the next row's stored predecessor hash stops
matching. The verifier tells you the first `event_seq` where it diverged."

**The full answer.**

```
event_hash = HMAC-SHA256( chain_key, canonical_json(22 ordered fields) ‖ prev_event_hash )
genesis prev_event_hash = 32 × 0x00
```

Chained per session, ordered by `event_seq`.

`verify_chain` checks **two** things, and needing both is the lesson:

| Check | Catches |
|---|---|
| stored `prev_event_hash` == actual predecessor's hash | **deletion and reordering** |
| recomputed `event_hash` == stored `event_hash` | **edits** |

A per-row signature would catch edits but **not deletions** — remove a row and every remaining
signature still validates perfectly. The chain is what makes *absence* detectable.

**`CHAIN_FIELDS` is an explicit ordered 22-tuple. Never `SELECT *`.** With `SELECT *`, adding a
column silently changes the canonical serialization and every historical hash becomes unverifiable.
Explicit ordering makes the hash input a **reviewed artifact**; changing it requires a
`CHAIN_FIELD_SET_VERSION` bump and a documented re-anchor (R-27).

**The four excluded fields, each for a reason:**

- `event_id` — a key the database chose carries no auditable meaning.
- `retention_expires_at` — **so a retention-policy change cannot invalidate history.** This is the
  elegant one; mention it.
- `prev_event_hash`, `event_hash` — the chain columns themselves.

**Three serialization details that are load-bearing:**

- **`_format_timestamp` rejects naive datetimes.** A datetime without a timezone serializes
  differently depending on the machine's locale, so two tiers would produce two hashes for one event.
- **`spoof_risk` is `numeric(5,4)`, not `double precision`.** The chain serializes it to a fixed
  4-decimal string; a float column would round-trip to a different repr and **every stored hash would
  stop verifying.** The column type is dictated by the hash function's serialization format.
- **`NULL` is a distinct hash input from `0.0000`.** Lifecycle events have no score. Conflating them
  would let you forge a lifecycle event as a zero-risk window.

**And BUG-1's fix, which is the pattern to steal:** `_assert_not_forbidden()` runs **first** in
`canonicalize()`, *and* is applied at import time to `CHAIN_FIELDS` and `EXCLUDED_FIELDS`. So adding
`audio_blob` to the canonical field list means **the module does not import.** Not a failing test you
might skip — an `ImportError`. The privacy check guards its own configuration.

## Q: "Why is there a `UNIQUE (session_id, event_seq)`?"

**Say this:** "So a concurrency bug becomes a failed insert instead of a forked chain that a verifier
reports as tampering."

**The full answer.** Two writers computing `event_seq` from the same predecessor produce a **fork** —
two rows claiming the same position, which is indistinguishable from tampering to a verifier. The
constraint makes the second insert fail instead. And its btree doubles as the chain-verification
access path, so the integrity constraint pays for the query plan.

## Q: "What stops someone adding an audio column later?"

**Say this:** "Five structural rules asserted against the live `information_schema`, and the
allow-list is checked as an exact set in both directions."

**The full answer — the five rules in `deny_list_violations()`:**

1. **Forbidden name substrings**, case-insensitive, on **every table** — including tables this
   project didn't create, because a future `session_note` table is exactly where an audio column would
   appear. Substring matching, not exact names: *"an exact-name deny-list is one a rename walks
   around."* So `audio_blob_v2` and `AudioBlob` both trip. Nine substrings: `audio`, `pcm`,
   `waveform`, `transcript`, `embedding`, `phone`, `msisdn`, `caller_name`, `raw`.
2. **Only two `bytea` columns may exist** — `prev_event_hash` and `event_hash`. Anything else that is
   `bytea` is somewhere audio could live.
3. **Forbidden UDT names** — `vector`, `halfvec`, `sparsevec`, and the float arrays `_float4`/
   `_float8`. That's how a voice embedding arrives when pgvector isn't installed. Matched on the
   *reflected* `udt_name`, so the DDL spelling is irrelevant.
4. **No unlisted column wider than 512 bytes.**
5. **The allow-list is an EXACT SET, both directions.** *"`assert set(expected) <= set(actual)`
   passes happily the day after someone adds `raw_audio_path`, because everything expected is still
   there."*

Rules 1–4 apply to every table; rule 5 only to `audit_event`.

**One more detail, and it's the sort a judge who has done this will appreciate:**
`REFLECT_COLUMNS_SQL` uses `current_schema()`, not a literal `'public'` — *"or a search_path change
turns the privacy gate into a no-op that still reports green."*

**And a documented, deliberate divergence.** The DB-side substring list has nine entries; the
writer-side list in `chain.py` has eight (it lacks `raw`). Rather than importing `chain.py`'s tuple —
which would silently adopt the gap at the one layer where the omission is **permanent**, since a
column once created and written to cannot be un-created without destroying evidence — the DB module
declares its own **superset** and asserts the relationship in both directions. Neither test breaks
when someone eventually adds `raw` to `chain.py`. The known gap is documented, contained, and tested
instead of papered over.

## Q: "Which columns are interesting?"

- **`call_ref`** — `text` with `CHECK (call_ref ~ '^[0-9a-f]{64}$')`. An HMAC pseudonym. The raw
  reference never leaves Gateway process memory (R-16), and the column comment names the CHECK as
  *"the last boundary before a raw value would become durable."* A client bug that sends a phone
  number where a pseudonym belongs fails at the database.
- **`quality_flags`** — `text[]`, the only variable-shaped column, and therefore the only place a
  transcript fragment could hide. So it's **membership-constrained** against the contract enum
  (`quality_flags <@ ARRAY[...]`), **not length-constrained**: *"a length bound would not"* make it
  impossible — a 400-character limit still lets you park 400 characters of someone's speech there.
- **`tenant_id`** — decision D-7. Present from Phase 1 so Phase-4 row-level security is a *policy
  change* rather than a migration of live evidence. And the column comment says, **in the database
  itself**, that RLS is NOT enabled and describing this as multi-tenancy today would be a
  target-as-complete claim (R-01).

**Comments live in the database, not just in the file.** `\d+ audit_event` during an incident is
where someone will ask "can this column hold audio?", and the answer is right there.

## Q: "Why one row per window, not one per decision?"

**Say this:** "Because the evidence *sequence* is the artifact. A trail that records only the trigger
cannot show why it fired."

**The full answer.** If you log only the moment the state went `high`, you cannot afterwards
demonstrate that the two windows before it were genuinely below threshold — so you cannot defend the
decision, and you cannot replay it. Recording every scored window is what makes `engine.replay()`
meaningful.

---

# 9. Privacy

## Q: "You're capturing people's voices. Where does the audio go?"

**Say this:** "Into a bounded in-memory ring buffer, and nowhere else. No disk, no object store, no
database column, no log. The buffer is a fixed-length deque, so it cannot grow, and it's zeroed in a
`finally` block so it's cleared on every exit path including a crash."

**The full answer — R-14, and the mechanisms are structural.**

```python
self._buf = deque(maxlen=WINDOW_SAMPLES)   # 40,960 samples. 2.56 seconds. Hard bound.
```

**Overflow is not handled — it is impossible.** Push sample 40,961 and sample 1 falls off the back.
There is no code path where this buffer grows, so there is no code path where a memory bug becomes
audio retention.

Then:

- **Unvoiced frames are discarded entirely** — not buffered-and-ignored.
- **`clear()` overwrites with zeros, then clears**, and is idempotent.
- **`ring.clear()` lives in `finally`** — clean close, protocol error, unhandled exception, vanished
  client. All covered.
- **Zero-copy sample access** via `memoryview(...).cast("h")` — fewer copies of raw audio is fewer
  places it can accidentally persist.
- **`Detector.__slots__ = ()`** means you cannot even attach a window to the detector object;
  `detector.last_window = window` raises `AttributeError`.

Six independent mechanisms, none of which is a promise.

## Q: "What about logs? Everyone leaks in logs."

**Say this:** "Error messages are static constants and never echo client input. Exceptions carry data
as attributes, not in their message strings. And there's a dedicated log-redaction test."

**The full answer — R-17.** Look at `FrameRejected`: it holds `code`, `expected`, `actual` as
**fields**. It does not format them into a string. Write `f"bad frame: got {len(data)} bytes"` and
you've built a channel through which client-controlled data reaches your logs — and logs get shipped,
indexed, and retained.

The close-reason table is all constants, and the fallback for an unknown code is `"stream closed"` —
with a comment explaining why it isn't `"rejected"`: **R-07** bans the action vocabulary from every
client-visible string, and that default is the one spot where a newly-added code could smuggle one in.

`gateway/tests/test_log_redaction.py` exists specifically because the source plan's Phase 1 DoD
*omitted* it — it enforces privacy at the schema level only. We kept it because a schema deny-list
does nothing about a caller reference or a PCM buffer reaching a log line, *"which is the more likely
leak path in a build with debug logging on."*

## Q: "Is HMAC pseudonymization the same as anonymization?"

**Say this:** "No, and we say so in the PRD's non-goals. HMAC pseudonymization of a call reference
does not make all associated metadata anonymous. It's a strong pseudonym with a secret key, which is
a real control — but it's not an anonymization guarantee and we don't claim one."

**The full answer.** With the key, the mapping is reversible; without it, `call_ref` is a 64-hex
pseudonym that can't be linked back or correlated across tenants. What it *doesn't* do: prevent
re-identification from the surrounding metadata (timestamps, purpose, session shape). Claiming
otherwise would be an anonymization claim we haven't earned, and it's listed as an explicit non-goal.

## Q: "What about extra fields a client sends?"

**Say this:** "Rejected. There are no tolerated unknown fields on the audio channel, because an
ignored extra key is somewhere a client could put a transcript or a phone number."

```python
allowed = {"type", "call_ref", "purpose_code", "context_value_band", "client_capture"}
if set(payload) - allowed:
    raise ProtocolError("PROTO_FIRST_MESSAGE")
```

Most JSON handlers ignore extra keys. This one treats tolerance as a privacy leak — because an
ignored field still traverses your logs and your error handlers.

## Q: "What's your retention policy?"

**Say this:** "Audit rows carry `retention_expires_at`, and a worker deletes **whole sessions only** —
never individual rows, because deleting a row from the middle of a hash chain is indistinguishable
from tampering. And `retention_expires_at` is excluded from the hash, so changing the retention policy
can't invalidate history."

## Q: "Walk me through your threat model."

Six threats, each with an enforced control and a required test. This is `prd.md` §6 and it's worth
knowing cold.

| Threat | Control | Test |
|---|---|---|
| Raw audio in a bucket, DB, log, crash dump, or alert | No audio object store; no audio DB columns; redacting logger; volatile buffer clear; payload-size guards | Schema + log scan asserting raw-byte count is zero |
| Replayed or malformed stream | JWT; short-lived single-use signed ticket; Origin allow-list; monotonic sequence; exact 20 ms framing | WSS negative-contract tests |
| Cross-tenant disclosure | *Target:* tenant claim, PostgreSQL RLS, tenant-scoped HMAC context | Integration test returns 403 / zero rows for wrong tenant |
| Overconfident model action | 3-of-5 evidence; an `uncertain` state; human verification instead of denial | Adversarial noisy/codec sample must not auto-act |
| Secrets in source or client | Secrets Manager / Docker secret injection; no client secret; CI secret scan | Secret scan + deployment manifest inspection |
| Audit record alteration | HMAC event chain (+ Phase 4 signed root checkpoint) | Alter one historical row in a test copy → verifier fails deterministically |

**Be explicit that row 3 is a target, not current state.** The `tenant_id` column exists; RLS is not
enabled. Saying so is R-01.

---

# 10. Tests and CI

## Q: "How do you know it works?"

**Say this:** "294 tests pass in the Gateway in under three seconds. 55 contract, 55 privacy, 25
parity, and — honestly — **zero integration**, because integration needs a live database and a live
Scorer and we haven't stood those up together yet."

```bash
cd gateway && ../.venv-ws/Scripts/python.exe -m pytest tests/ -q
# 294 passed in 2.93s
```

**The full answer.** Nine test modules, each mapped to a rule:

| Module | Guards |
|---|---|
| `test_frames.py` | R-24, R-25 — 648-byte contract, byte order, sequence monotonicity |
| `test_ring.py` | R-14 — bounded buffer, unvoiced discard, `clear()` |
| `test_policy_engine.py` | R-08, R-09, R-13 — k-of-n, ineligible skipping, stickiness |
| `test_audit_chain.py` | R-27 — tamper detection: edit, delete, reorder |
| `test_ticket.py` | MAC, expiry, binding, replay |
| `test_pseudonym.py` | R-16 — HMAC shape and stability |
| `test_constants_parity.py` | R-23 — 25 tests **parsing the TypeScript** and comparing to Python |
| `test_log_redaction.py` | R-17 — nothing sensitive reaches a log line |
| `test_ws_negative_contract.py` | the six Phase-1 exit criteria |

**`test_constants_parity.py` is the one to describe if asked for a favourite.** The wire constants
exist twice — Python for the Gateway, TypeScript for the browser. Two copies of a contract *will*
drift. So a test parses the `.ts` file and compares every value. Plus `constants.py` calls
`_self_check()` at import, asserting the arithmetic identities
(`WS_FRAME_BYTES == SEQ_PREFIX_BYTES + BYTES_PER_FRAME_PAYLOAD`,
`HOPS_PER_WINDOW * HOP_SAMPLES == WINDOW_SAMPLES`). Change one number carelessly and the module won't
import.

## Q: "The six negative tests?"

Four from the source plan, two we added because they're stricter:

| # | Test | Result | Threat |
|---|---|---|---|
| 1 | No `sih-ticket.` subprotocol | `AUTH_TICKET_MISSING` / 1008 | unauthenticated stream |
| 2 | `Origin` not allow-listed | `AUTH_ORIGIN_DENIED` / 1008 | cross-site WS hijack |
| 3 | Non-monotonic sequence | `PROTO_SEQUENCE` / 1003 | replayed stream |
| 4 | Frame ≠ 648 bytes | `PROTO_FRAME_SIZE` / 1003 | malformed input |
| 5 | `purpose_code` ≠ server record | `PROTO_PURPOSE_MISMATCH` | **consent-binding gap (D-4)** |
| 6 | Oversized text frame | `PROTO_PAYLOAD_TOO_LARGE` | payload smuggling |

**#5 is worth explaining in full.** `purpose_code` and `context_value_band` are bound **server-side**
at `POST /api/v1/sessions`, before any audio exists. `_expect_session_open` re-checks both against the
server record. A mismatch means the client is trying to change the declared purpose on the audio
channel — exactly the move that would let someone declare `support_enquiry` (where `high` → `verify`)
and then actually perform a `beneficiary_change` (where `high` → `escalate`).

## Q: "What are your CI gates?"

Eight workflows: `gateway-ci`, `scorer-ci`, `pwa-ci` (one per service, path-filtered) ·
`contract-check`, `privacy-check`, `secret-scan` (the gates) · `deploy-runtime`, `stop-runtime`
(manual only, never automatic).

`main` is protected **from Phase 0** (R-57): require a PR, ≥1 approving review, and
`contract-test` + `secret-scan` + **`privacy-tests`**. That third is our addition, with a one-line
justification: **a privacy regression must not be mergeable.**

The timing argument matters as much as the rule: *retrofitting protection after three pairs have
pushed directly to `main` means either rewriting history or accepting an unreviewed change to the seam
all three pairs integrate against.*

**R-55 — OIDC only.** GitHub Actions assumes a deploy role via `sts:AssumeRoleWithWebIdentity`. No
long-lived access keys: not in repo secrets, not in an image, not on a laptop. A leaked long-lived key
grants standing account access; an OIDC token is minted per run and expires. The role has **no
`AdministratorAccess`** — scoped to ECR push, ECS update/describe, ASG update/describe, CloudFormation
+ S3 for CDK, `secretsmanager:GetSecretValue` for verification, and **`iam:PassRole` restricted to
named execution roles.** The rule says why, bluntly: *a `PassRole` on `"*"` converts any workflow
compromise into account takeover.*

**R-56 — promotion by image digest, never a rebuild.** `deploy-runtime` takes
`gateway_image_digest` and `scorer_image_digest`, not tags. ECR repos are `IMMUTABLE`. Workflows are
path-filtered so an unrelated change can't re-push an image and invalidate a digest a release manifest
recorded. *A rebuild from the same source produces different bytes; the moment a manifest names a tag
instead of a digest, it stops describing what is running.*

## Q: "Why three ECR repositories?" ← *shows integrity*

**Say this:** "Because the GPU and CPU scorer images are genuinely not byte-identical — one has
`onnxruntime`, the other `onnxruntime-gpu`. Rather than hide that behind a tag convention, we made the
exception visible in the registry."

**R-06.** `sih26104/gateway`, `sih26104/scorer-gpu`, `sih26104/scorer-cpu`. The invariant we claim is
the **parity set** — behaviour, contract, model hash, calibration hash — **not image bytes**. Making
the exception visible beats hiding it.

## Q: "What's wrong with your CI?" ← *volunteer this*

**Say this:** "One real hole. The `contract` gate can pass while most of its tests *skipped*, because
pytest exits 0 when everything skips. A missing fixture or a broken `conftest.py` import would produce
a green check over near-zero coverage. The fix is a passed-count floor via `--junitxml` — assert
`tests − skipped − failures − errors >= FLOOR`. It's identified and not yet implemented."

This connects to **R-52 — log what you dropped.** If coverage is bounded (top-N, sampling, a skipped
cohort), say so explicitly, because *silent truncation reads as "covered everything."*

## Q: "What haven't you tested?" ← *answer this straight*

**Say this:** "Roughly 130 files across the Scorer, PWA, audit and CI directories have never been
reviewed by a human or verified by execution. Our own memory ledger calls it 'the largest
unquantified risk in the repo.' All 294 passing tests are in the Gateway. The other suites exist and
have not been run in a verified environment."

That asymmetry is the single most important thing to state honestly about project maturity. Saying it
first costs you nothing and buys you every subsequent claim.

---

# 11. Infra, cost, deployment

## Q: "What does this cost to run?"

**Say this:** "At rest, near zero by design — the GPU autoscaling group and both ECS services sit at
desired count 0. A fresh deploy costs nothing to run. The only idle costs are one NAT Gateway and one
`db.t4g.micro`. Runtime is started by hand, for a demo, and stopped after."

**The full answer — six stacks and their idle cost:**

```
NetworkStack → DataStack → SecretsStack → ComputeStack → EdgeStack
                     CostSafetyStack  [standalone, deployed right after DataStack]
```

| Stack | Creates | Idle |
|---|---|---|
| `NetworkStack` | VPC, 2 public + 2 app-private + 2 data-private subnets, **1** NAT GW, deny-by-default SGs | NAT hourly |
| `DataStack` | RDS PostgreSQL 16 `db.t4g.micro`, private, encrypted, single-AZ | RDS hourly |
| `CostSafetyStack` | Budget → SNS → `RuntimeStopper` Lambda | ~zero |
| `SecretsStack` | references the five secrets by ARN, grants read to task roles | secret-months |
| `ComputeStack` | ECS cluster, GPU capacity provider + ASG (**desired 0**), two task defs, Cloud Map | ~zero |
| `EdgeStack` | private S3 + OAC, internal ALB, CloudFront with VPC origin | ~zero |

The chain is forced by cross-stack references. `CostSafetyStack` reads nothing from the others, so its
position is a **policy** decision: deploy it immediately after `DataStack`, because any other order
leaves a window where GPU capacity is deployable with no budget backstop armed — which inverts the
control the stack exists to provide. (Three source documents disagreed on this; we took the strictest
reading, wrote down why, and logged it as **H-5** for a human to confirm. That's R-54.)

**One NAT Gateway, on purpose.** It's a single point of failure and we **accept** it for a five-day
demo, with the instruction to note it in the retrospective rather than pretend otherwise.

**`EdgeStack` last** because CloudFront takes ~15 minutes to propagate, and you don't want to discover
that at the end of a day when you needed it at the start.

## Q: "How do you stop a runaway GPU bill?"

**Say this:** "Three layers, and I want to be precise about the third because it's the one people get
wrong. Runtime is zero by default. Starting it requires a manual workflow with an explicit
cost-acknowledgement checkbox. And the Budget→Lambda path is a **delayed backstop measured in hours**
— not a circuit breaker."

**The full answer.**

- **R-28 — runtime zero by default.** ECS desired 0, GPU ASG desired 0.
- **R-29 — no `git push` can start GPU spend.** CI may build and push images automatically; it may
  never scale runtime. Starting runtime is `workflow_dispatch` with a required boolean
  `confirm_cost_aware` that the job itself guards on (`if: ${{ inputs.confirm_cost_aware }}`). The
  verbal rule became a CI control.
- **R-30 — the honest one.** AWS Budgets evaluate against Cost Explorer data that refreshes a few
  times a day. **The alert fires long after the spend that triggered it.** A GPU left running
  overnight has already billed for the night by the time the Lambda zeroes it. So `stop-runtime` is
  the *mechanism*; the Lambda is a **bounded-loss backstop for the case where a human forgot.** The
  rule explicitly forbids calling it a circuit breaker in docs, slides, or comments — because someone
  who believes it is one will stop running `stop-runtime`.
- **R-31 — stopping means zeroing the ASG too:** `min`, `max`, **and** `desired` → 0, plus both ECS
  service counts. A direct EC2 stop is insufficient: **the ASG relaunches the instance.** That's the
  mistake that quietly bills all night.
- **R-32 — exactly one `g4dn.xlarge`,** one scorer GPU allocation, for the whole five-day window.
- **R-36 — no SSH, no public IP on the GPU host, no ECS Exec in the demo.** Which is why database
  migrations run from a one-shot ECS task on the same network: there is no bastion.

**If a judge asks "so it's not really protected?"** — the answer is that describing a delayed control
as instantaneous is the actual danger, and we chose to document the delay rather than let a teammate
rely on it. That's a better answer than a false claim of a circuit breaker.

## Q: "Can this scale?"

**Say this:** "Not as configured, and I can tell you exactly where it breaks. Single GPU, single AZ,
one NAT Gateway, and the Gateway runs one worker — which is a *correctness* requirement, not a tuning
choice, because the ticket replay cache is in-process. We claim no SLA. Scaling means a shared replay
cache, then multiple workers, then horizontal Gateway."

**The full answer, with the actual chain of blockers in order:**

1. **`--workers 1`** (deviation DEV-2). `ReplayCache` is a dict in memory. Two workers and a ticket
   can be spent twice — single-use tickets silently stop being single-use. Fix: move the cache to
   Redis/DynamoDB with a conditional write. This one first, because "optimizing" it without knowing
   is a security regression.
2. **Session registry is in process memory.** Horizontal Gateway needs it shared, or sticky routing.
3. **Single GPU serializes scoring** (NFR-2). One window per 640 ms per session; concurrency is
   bounded by forward-pass time. Fix: batch windows across sessions, or more GPUs.
4. **Single AZ, one NAT.** Availability, not throughput.

**NFR-7 is the graceful-degradation answer:** at capacity, the Gateway **refuses a new high-risk
stream** rather than queueing unbounded audio (`stream_rejected_backpressure_total`). Refusing is the
privacy-preserving failure mode — queued audio is retained audio.

## Q: "Why AWS at all if it runs on a laptop?"

**Say this:** "Both tiers are the deliverable, not a fallback. The AWS tier is the realistic
deployment; the CPU tier is the proof that the control plane doesn't depend on a GPU being available —
which matters for a bank branch or a regional office. And running both is what makes the parity claim
testable."

**The full answer.** Config-not-branch (R-04) is what makes this honest: the deployment tier is a
*value*, and `if profile == "aws"` is forbidden in application code. `is_aws` exists only for banners,
metrics labels, and audit fields.

The parity set is behaviour, contract, model hash, calibration hash — **not image bytes** (R-06). And
R-45's provider assertion is what keeps the claim real: a silent CPU fallback on the GPU tier would
make both tiers CPU, so "same trace on both providers" would never have been exercised, **and the one
test that would have caught it would have passed.**

## Q: "What's your single biggest deployment risk?"

**Say this:** "The `g4dn.xlarge` vCPU quota in `ap-south-1`. A brand-new AWS account frequently has a
**zero-vCPU** G-family quota, approval can take longer than three days, and nothing in Phase 3 works
without it — the autoscaling group just fails to launch, **silently**, on the highest-coordination day
of the build."

**The full answer.** This is **H-2**, and our source document calls it *"the single most avoidable
failure mode in this whole plan."* An earlier version filed it as a Day-5 risk; that was wrong and it
moved to Phase 0. Note the Definition-of-Done row is satisfied by a **request ID, not an approval** —
filing is what Phase 0 controls; AWS controls the timeline.

Naming this as your biggest risk is a strong answer, because it shows you distinguish risks you
control from risks you can only file early.

---

# 12. Process, team, plan

## Q: "Five days, three pairs. How do you avoid stepping on each other?"

**Say this:** "Directory ownership, one frozen contract directory with a two-key review, and four
scheduled sync points. And the ordering is deliberate — contract and privacy first, model second,
realtime third."

| Pair | Owns | Cannot defer past |
|---|---|---|
| **A — Platform/Infra** | `contracts/` (2-key), `gateway/`, `infra/cdk/`, `infra/compose/` (with C), `.github/workflows/` | Day 2 functional WSS path |
| **B — AI/ML** | `datasets/manifest/`, `ml/`, `evaluation/reports/`, produces `policy/`, `scorer/` | Day 4 deployment candidate |
| **C — Integration & Audit** | `pwa/`, `audit/`, privacy tests, `docs/manifests/`, demo evidence | Day 4 end-to-end rehearsal |

```
Phase 0  Bootstrap             ~½ day   ← repo scaffold DONE, AWS actions OPEN
Phase 1  Contract & Privacy    Day 1    ← contract and privacy boundary FIRST
Phase 2  Benchmark & Calib     Day 2
Phase 3  Realtime & Deploy     Day 3
Phase 4  Robustness & Evidence Day 4
Phase 5  Rehearsal & Failover  Day 5    ← not a feature day
```

**The ordering is the argument.** Contract and privacy before any model training, because those are
the parts you cannot retrofit. Governing rule: *no diagnostic feature, cross-session identity
function, or visual dashboard may become a primary decision input before the core score, calibration,
and policy control loop pass their acceptance gates.* And **R-50**: a red Definition-of-Done row
blocks the next phase **for that track**, not the whole team.

**The sharpest sync point — mid-Day-3:** Pair A does not run `deploy-runtime` until Pair B **confirms
the ONNX parity gate in writing.** It's a *blocking* sync — Pair A idles rather than deploys. *"A
message pointing at the CI parity report is enough. The point is that it is confirmed, not assumed."*

## Q: "Why does `contracts/` need two reviewers?"

**Say this:** "Because all three pairs integrate against it, and a single reviewer from the author's
own pair cannot catch 'this breaks the other two teams.' So a change needs a version bump, a
compatibility note, and review from one Pair B member and one Pair C member."

**R-22.** And there's a live blocker: `contracts/OWNERS.md` is supposed to name a tie-breaker for a
B-vs-C deadlock, and it doesn't. That's **H-1**, one of only two *fatal* Phase-0 items, because an
unnamed tie-breaker stalls all three pairs simultaneously **with no way to route around it.**

## Q: "How do you keep documentation from rotting?"

**Say this:** "Three mechanisms. Decisions have permanent IDs cited from source docstrings, so you
can't renumber one without breaking dozens of citations. `memory.md` must be updated in the same
commit as any change that alters a documented decision. And it separates what we *verified by running
it* from what we merely believe."

**The full answer.**

- **Permanent IDs.** `D-1`…`D-15` for decisions, `R-01`…`R-57` for rules. Source cites them
  (`# Decision D-7`, `(decision D-12)`), so code and docs are mechanically joined. Rule IDs are never
  renumbered.
- **R-49.** Update `memory.md` in the *same commit*. A change that alters a documented decision
  without updating it is defined as incomplete.
- **The §5/§6 split.** `memory.md` §5 is *verified* evidence — a command was run and output recorded.
  §6 is *unverified*, no matter how confident the author felt. That discipline is what makes the
  ledger worth reading.
- **R-54.** When two documents disagree, name the difference; never blend them. Take the strictest
  reading, write down why, and log it for a human.

**And be ready to admit the ledger itself drifts.** `memory.md` §1 says of its own state table
*"re-measure before trusting this table"* — and it's right. See [onboarding-2h.md](onboarding-2h.md)
for the corrections we found by taking it at its word: two "empty directory" claims that are false,
a test filename cited that doesn't exist, and a four-vs-five secrets conflict. **A judge who catches
one of those and finds you already documented it is a judge you've won.**

---

# 13. Hostile questions

These are designed to knock you over. Each has a real answer.

## Q: "Your model isn't trained. Isn't this just plumbing?"

**Say this:** "The plumbing *is* the contribution. Detection research is a crowded field with public
benchmarks and published architectures — we use AASIST, the reference implementation, and we don't
claim to have improved it. What doesn't exist off the shelf is the part that turns a score into an
accountable, proportionate, auditable control without retaining audio. That's what we built, and
that's the part a bank couldn't buy."

Then make the trade explicit: "If we'd spent five days training, we'd have a marginally different EER
on a public corpus and no story about what happens next. We chose the harder half."

## Q: "So you can't actually detect a deepfake."

**Say this:** "The exported AASIST graph runs and produces scores — that part works, and PyTorch to
ONNX agrees to 6 × 10⁻⁶. What we have not done is measure how *well* it detects, on which cohorts,
with what calibration. I won't tell you it's accurate until the gates say so. Every one of those gates
is written and unrun, and each is a release blocker."

**Do not get defensive here.** The follow-up you want is "what would it take?" — and you have a
four-step answer for the threshold and an eight-gate answer for the model.

## Q: "Couldn't an attacker just play audio through a speaker into the phone?"

**Say this:** "Yes, and that's replay, which is a different attack from synthesis. Our model is
trained on the spoof/bona-fide distinction, and replay is in ASVspoof's PA condition, which we are
not currently evaluating. It's a real gap and it's not in our claim."

**The full answer, and the honest framing:** a replayed *recording of a real human* is not synthetic
speech; it's a presentation attack. The channel artifacts differ. If a judge pushes, the right answer
is: "the OOD gate reports by attack type precisely so this shows up as a cohort with its own number
rather than being averaged into a headline figure. Right now that number is unmeasured, so it's not in
the claim."

## Q: "What if the model is wrong and you hold a legitimate payment?"

**Say this:** "That cost is real and it's why the action vocabulary has no `deny` in it. The worst
thing we do to a false positive is add a verification step to a payment or route a beneficiary change
to a human — both of which a legitimate customer passes. We never decline anything, and a human
authorises every transaction."

**The full answer — three layers of false-positive mitigation:**

1. **k-of-n** — one bad window can't act. Costs a few seconds, removes most spurious triggers.
2. **Proportionality** — a support enquiry at `uncertain` gets **nothing**, deliberately, because
   interrupting a balance query is a cost paid by an innocent person (R-06).
3. **The vocabulary** — the worst outcome is friction, never refusal.

**Then volunteer the limitation:** "The one I'd flag is that `high` is sticky, so a false positive
persists for the session and there's no clear-it path yet — that's a Phase 4 human-resolution step
that doesn't exist. It's the right trade against an attacker who goes quiet, but it is a real cost and
I'm not going to pretend the resolution flow is built."

## Q: "Your threshold is made up, your calibration is the identity, and your accuracy is unmeasured. What exactly works?"

Answer it as a list, calmly. This is the question you *want*, because you have receipts.

**Say this:** "Four things work and are tested. One, the transport contract — 648-byte framing,
sequence monotonicity, six negative-path tests, 294 tests passing in under three seconds. Two, the
privacy boundary — bounded ring buffer, zeroed in `finally`, a structural deny-list asserted as an
exact set, log redaction. Three, the decision layer — k-of-n, stickiness, ineligible-skipping,
purpose-proportional actions, and it's pure so any session replays. Four, the evidence chain — HMAC
chained per session, catches edits, deletions and reordering, and names the first divergent row.

What does not work: the detection *quality* claim. That's one gate away, and the gate is written."

## Q: "You've written more documentation than code. Isn't that a smell?"

**Say this:** "The docs aren't prose about the code — they're the reasoning the code cites by ID. Grep
for `R-` in the Gateway source and you'll find rule citations in docstrings throughout. When a
constraint is load-bearing, the reason lives next to it, because the person who breaks it will be
reading the code, not the wiki."

Then give the concrete example: "The database schema has `COMMENT ON` statements attached to five
columns, so `\d+ audit_event` in a psql session during an incident answers 'can this column hold
audio?' The documentation is *in the database.*"

## Q: "This is a mock. Why should I believe any of it?"

**Say this:** "Because you don't have to believe me — check it. The mock mode is stamped in every
audit row, so you can tell mock from real by querying the table. And there's a database constraint
that makes it impossible to write a row claiming `REAL_DETECTOR` with no model hash. I built the
system so that lying to you would require editing constraints in three places."

Then offer the check: "Run `E5` in our onboarding doc — it greps the whole codebase for `approve` and
`deny` and comes back clean. Run `E2` — the schema contract self-checks on bare Python, no database
needed. You can verify our privacy claim in one command without trusting us."

## Q: "What would you do differently?"

**Say this:** "Three things. Fix the calibration convention (H-6) before writing any test that
reimplements a transform — the reason it hid is that both suites were self-consistent. Put the
passed-count floor in CI on day one, because a green check over skipped tests is worse than a red one.
And file the GPU quota before writing a line of code; it's the one blocker where the timeline isn't
ours."

A crisp, specific answer here reads as maturity. A vague "more testing" reads as not having thought
about it.

## Q: "What's the weakest part of the system?"

**Say this:** "The detection quality claim, because it's unmeasured. After that, the single-worker
constraint — it's correct today and it's a hard ceiling on concurrency, and the fix is a shared replay
cache, not more workers. Third, about 130 files across three services have never been executed or
reviewed. Our own ledger calls that the largest unquantified risk in the repo."

Naming three real weaknesses in order of severity, unprompted, is the strongest available answer.

## Q: "If I gave you one more week, what would you build?"

**Say this:** "Not features. In order: run the eight evaluation gates so we can say something
measured; fit the calibration against the correct transform; stand up the integration lane so those
zero integration tests become real numbers; then the human-resolution flow that clears a sticky
`high`. Everything on that list is closing a gap we've already written down."

---

# 14. Us vs. the typical submission

Use this if a judge asks for a comparison, or to structure a closing statement.

| Dimension | Typical submission | This project |
|---|---|---|
| **Output** | A confidence score / "87% fake" | A proportionate action from a closed vocabulary, with a reason code |
| **Temporal logic** | Per-clip or per-frame verdict | 3-of-5 over *eligible* windows; a real `collecting` state |
| **Degraded input** | Counted as clean (the default) | **Skipped** — counted as neither (R-09), because otherwise degrading your audio lowers your risk |
| **Context** | One global threshold | Purpose → action table; same evidence, different action, each with a written reason |
| **Authorization** | "Block the transaction" | `approve`/`deny` **do not exist**; a human authorises. Enforced in 4 places |
| **Privacy claim** | "We don't store the audio" | Bounded deque zeroed in `finally`; 5-rule deny-list against live `information_schema` as an exact set; log-redaction test |
| **Audit** | An application log | HMAC chain per session; catches edit, delete, reorder; names the first divergent row |
| **Model/policy coupling** | Threshold in the inference script | Policy is a byte-hashed artifact; digest in every audit row and the version endpoint |
| **Deployability** | One environment, usually a laptop | Two tiers, one code path (R-04); a silent CPU fallback is a **crash**, not a degradation |
| **Reproducibility** | "It worked on my machine" | Pure decision layer; any session replays from the audit table alone |
| **Cost** | Not considered | Runtime-zero default; no push can start GPU spend; documented that the budget backstop is *delayed* |
| **Honesty** | Best numbers, prominently | `derivation: placeholder` gates eligibility; `null` not `0.0`; mock mode in every audit row; tolerance committed before the run |

**The closing line:** "Most submissions optimise the number. We optimised what happens after the
number — and we made it so that neither we nor a future contributor can quietly overstate it."

---

# 15. Never say these

Read this list before you walk in. Each left-hand phrase is a rule violation, and a sharp judge will
catch it.

| ❌ Never | ✅ Instead | Rule |
|---|---|---|
| "reduces fraud by X%" | "simulated prevention-control effectiveness" | R-02 |
| "probability / likelihood / confidence that it's fake" | "**score**" | R-11 |
| "our tuned threshold" / "our calibrated threshold" | "a placeholder; here's what would make it real" | R-01, D-11 |
| "the system blocks/denies/approves the transaction" | "holds for a step-up" / "escalates to a human" | R-07 |
| "it's fully multi-tenant" | "the column exists for forward compatibility; RLS is Phase 4" | R-01 |
| "the budget acts as a circuit breaker" | "a delayed backstop, measured in hours" | R-30 |
| "the images are identical across tiers" | "the *parity set* matches: behaviour, contract, model hash, calibration hash" | R-06 |
| "anonymized" | "pseudonymized with a keyed HMAC — not an anonymization guarantee" | non-goals |
| "we detect all deepfakes" | "AASIST on our declared protocol; OOD cohorts reported separately" | non-goals |
| "it's production-ready" | "no SLA is claimed before load and network testing" | non-goals |
| "we can lower k for a snappier demo" | "k < 2 raises at load time. It's a refusal, not a setting." | R-08 |
| "the diagnostics feed the risk score" | "advisory only; the return value is discarded until ablation" | R-12, D-12 |

**And a positive one to have ready.** If you're caught not knowing something:

> "I don't know that one. It's either in `rules.md` or it's an open item in `memory.md` §6 — let me
> not guess at it."

That answer has never cost anyone marks. Guessing has.

---

# 16. One-page cheat sheet

**Numbers**

```
648 B frame = 8 B seq (uint64 BIG-endian) + 640 B PCM (320 × int16 LITTLE-endian)
20 ms/frame · 16 kHz mono · 50 frames/sec
window 2.56 s = 40,960 samples = 81,920 B
hop 640 ms = 10,240 samples = 32 frames · 4 hops/window · 75% overlap
PCM16 → float: ÷ 32768.0, OUTSIDE the ONNX graph
evidence 3-of-5 eligible · threshold 0.78 (PLACEHOLDER)
ticket 60 s, single-use · JWT RS256 pinned · JWKS cache 600 s, refresh floor 30 s
audit_event: 26 columns · 22 chained · 4 excluded · genesis 32 × 0x00
model: AASIST · [1,40960] → [1,1] · PyTorch↔ONNX max_diff 6.26e-06
294 gateway tests (contract 55 · privacy 55 · parity 25 · integration 0)
5 secrets · 4 env vars · 6 CDK stacks · 8 workflows · 3 ECR repos · 8 eval gates (all not-run)
```

**Vocabulary** — `continue` · `verify` · `hold` · `escalate`. **Never** `approve`/`deny`.
**States** — `collecting` · `uncertain` · `high` (sticky).
**Actions by purpose at `high`** — payment_release → `hold` · beneficiary_change → `escalate` ·
account_recovery → `escalate` · support_enquiry → `verify`.

**The five sentences that carry the whole pitch**

1. The Scorer emits a number; the Gateway decides — and the Scorer *structurally cannot* decide.
2. Three high windows out of five eligible ones, because one window is noise, not evidence.
3. Same evidence, different action, by what's at stake — and `approve`/`deny` don't exist.
4. No audio persists anywhere by default, enforced by a deny-list run against the live schema.
5. Every decision is one row in an HMAC chain that detects edits, deletions, and reordering.

**The three things to volunteer before you're asked**

1. The threshold is a placeholder and the eight evaluation gates are unrun.
2. H-6 — the calibration convention bug that would have made `high` unreachable, and why both test
   suites passed anyway.
3. ~130 files across three services are unreviewed and unexecuted; all 294 passing tests are in the
   Gateway.

**If you only remember one line:** *"We built the part that turns a score into an accountable
decision — and we built it so that neither we nor anyone after us can quietly overstate what it
does."*
