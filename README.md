# SIH26104 / PS104 — Voice Integrity Control Plane

A privacy-preserving, real-time decision layer that turns persistent evidence of synthetic or
manipulated speech into a proportionate verification control before a simulated high-risk voice action
completes. The **Scorer** answers one narrow question — how much evidence of synthetic speech is in
this 2.56-second voiced window — while the **Gateway** answers a different one: given recent evidence,
declared purpose, policy version, and uncertainty, what is the appropriate safe next action. That
separation is the architecture: a score is an observation, an action is a decision, and no model output
ever calls a banking API.

---

## ⚠️ The one safety fact

**Nothing deploys automatically. GPU spend is manual, and it must be turned off after every session.**

CI builds and pushes images on every merge to `main`, then stops. ECS desired count is `0` and the GPU
ASG desired capacity is `0` by default, so a fresh deploy costs nothing to run. The only path that
starts GPU spend is `deploy-runtime.yml`, triggered by hand with an explicit `confirm_cost_aware`
input — and `stop-runtime.yml` runs after **every** session, without exception. The AWS Budget → SNS →
Lambda path is a *delayed* backstop: it fires hours after the spend that triggered it, because budgets
evaluate against cost data that refreshes a few times a day. It is not a circuit breaker and must not
be described as one. See [rules.md](rules.md) R-28 … R-31.

---

## Which document to read

| Document | Read it when |
|---|---|
| **[prd.md](prd.md)** | You need the product claim, the explicit non-goals, the functional and non-functional requirements, the evaluation gates, or the traceability matrix. Start here to learn what is *not* being claimed. |
| **[architecture.md](architecture.md)** | You need system shape: four planes, the `ap-south-1` AWS tier, the CDK stack decomposition, the repo → Actions → AWS delivery plane, the CPU fallback tier, and the parity invariant. |
| **[technical-design.md](technical-design.md)** | You are writing code and need exact bytes: the 648-byte WSS frame, the gRPC `ScoreWindow` contract, the audit schema and hash chain, module layouts, the config surface. |
| **[design.md](design.md)** | You are building UI and need the visual system — colour, type, spacing, components, accessibility. |
| **[phases.md](phases.md)** | You want to know what happens today: Phase 0–5, per-pair tracks, Definition-of-Done tables, sync points, and what is deliberately *not* being built yet. |
| **[rules.md](rules.md)** | Before your first edit. `R-01 … R-57` are binding invariants, each stated with the failure it prevents. They are cited by ID from source docstrings — never renumber one. |
| **[aws-setup-instructions.md](aws-setup-instructions.md)** | You are provisioning AWS. Follow it in order; the order is forced by cross-resource dependencies, and §2 is time-critical. |

[memory.md](memory.md) is the running decision log — decisions, deviations, resolved unknowns, new
blockers — updated in the same commit as the change it records ([rules.md](rules.md) R-49). It is the
handoff mechanism between humans and between agents.

---

## Repository layout

```
contracts/           protobuf + OpenAPI — Pair A, two-key review rule (1×Pair B + 1×Pair C)
gateway/             FastAPI app: ingress, VAD, policy, audit — Pair A
scorer/              AASIST ONNX serving — Pair B
pwa/                 React PWA — Pair C
infra/cdk/           CDK TypeScript: 5 chained stacks + standalone CostSafetyStack — Pair A
infra/compose/       local CPU fallback tier — Pair A + Pair C jointly
datasets/manifest/   manifest.parquet, split hashes — Pair B
evaluation/reports/  gate reports, metric tables — Pair B
policy/              policy.yaml, calibration.json — Pair B produces, Pair A consumes
audit/               schema, hash-chain verifier, retention worker — Pair C
docs/manifests/      release_manifest.json + judge-facing evidence — Pair C
.github/workflows/   CI, one workflow per service — Pair A
```

Pair A is Platform/Infra, Pair B is AI/ML, Pair C is Integration & Audit.

---

## Local quickstart (CPU tier, no AWS credentials needed)

The local Compose tier is the integration harness, not a second product. It runs the same application
source, contract, schema, policy bundle, model, and calibration artifact as AWS.

```bash
git clone git@github.com:<org>/sih26104-voice-integrity.git
cd sih26104-voice-integrity

# Local trusted TLS — the PWA needs a secure context for getUserMedia
mkcert -install
mkcert -cert-file infra/compose/certs/local.pem \
       -key-file  infra/compose/certs/local-key.pem sih26104.local

cp infra/compose/.env.example infra/compose/.env    # placeholder secrets, git-ignored

pnpm -C pwa install && pnpm -C pwa build
docker compose -f infra/compose/docker-compose.yml up --build

# → https://sih26104.local     Caddy is the only published host port.
# Startup banner prints the parity set: commit, contract hashes, model SHA, calibration SHA, provider.
```

Use **Python 3.12**, not the workstation's 3.14 — `webrtcvad-wheels` and `onnxruntime` wheels for 3.14
are not assured (`uv python install 3.12`). Never reload Caddy during a live stream; it closes open
WebSockets on config reload.

> Phase 0 scaffolding is still landing. If a path above does not exist yet, that is a Phase 0
> Definition-of-Done row, not a documentation error — check [phases.md](phases.md) §1.4.

---

## Phase 0 open blockers

Day 1 does not start for the whole team until these close. Full table:
[phases.md](phases.md) §1.4 · rationale: [prd.md](prd.md) §9.1.

| # | Blocker | Owner |
|---|---|---|
| **H-1** | Name the tie-breaker for the two-key `contracts/` review in `contracts/OWNERS.md`. An unnamed tie-breaker stalls all three pairs at once | Team lead |
| **H-2** | File the `g4dn.xlarge` on-demand quota increase in `ap-south-1`. Approval can take longer than three days and **nothing in Phase 3 works without it** — the ASG fails to launch silently | Pair A |
| H-3 | Record the Paid Plan credit balance in `docs/manifests/aws_account_baseline.md` | Pair A |
| H-4 | Name the exact demo laptop for the CPU p95 sweep | Team lead |
| H-5 | Confirm the reconciled `CostSafetyStack` deploy position (immediately after `DataStack`) | Team lead |

---

**What this is not:** a deepfake truth machine, a speaker-verification system, a fraud-reduction
claim, or an automated denial system. The action vocabulary is exactly `continue` · `verify` · `hold` ·
`escalate` — `approve` and `deny` do not exist, by construction. The supportable outcome is *simulated
prevention-control effectiveness*. See [prd.md](prd.md) §1.1 and [rules.md](rules.md) R-01, R-02, R-07.
