# SIH26104 — Five-Day Dual-Tier Implementation Plan

**Document status:** Team execution plan. **Operating principle:** build the AWS GPU tier and the CPU-only local fallback **in parallel from Day 1**, while keeping application contracts, model/calibration artifacts, audit schema, policy bundle, and tests identical. The local tier is not a late emergency rewrite. It is a rehearsed deployment profile that proves PS104’s edge/privacy/reusability requirement.

> **Definition of done:** on Day 5, the team can run the same consented 90-second scenario on either AWS or local CPU, produce the same policy state transition and privacy evidence, and state exactly what the model has and has not been validated to prove.

## 1. Team Tracks and Non-Negotiable Working Rules

| Track | Primary responsibility | Daily artifact | Cannot be deferred past |
|---|---|---|---|
| Platform and security | CDK, AWS account, ECS/ECR, local Compose/Caddy, secrets, cost stop, deployment manifests | Synth output, Compose health output, security checklist | Day 2 functional WSS path |
| Gateway and integrations | REST/WSS, JWT/tickets, VAD/windowing, policy, mock transaction, webhook, audit API | OpenAPI, protobuf, contract tests | Day 3 policy path |
| ML and evaluation | Dataset manifest, baselines, AASIST, calibration, ONNX parity, codec/language hold-outs | Model release manifest and metric table | Day 4 deployment candidate |
| PWA and judge UX | Consent notice, Cognito/local auth UX, microphone capture, risk timeline, Privacy Inspector | Phone test recording and screenshot set | Day 4 end-to-end rehearsal |
| QA, privacy, and pitch | Test matrix, consent register, schema/log scans, traceability, claim boundaries, narrative | Signed release checklist and demo script | Day 5 final rehearsal |

Every merge must preserve four invariants. First, the Gateway and Scorer application contracts do not branch on “AWS” versus “local.” Second, no raw audio, transcript, phone number, or default speaker embedding enters durable logs or the audit DB. Third, every model change carries a model/calibration/policy release manifest. Fourth, no one claims a capability that has not been demonstrated with recorded evidence.

## 2. Common Deliverables Built Once

The following files and contracts are shared by both deployment profiles. They are the anti-divergence spine of the project.

| Shared asset | Required content | Validation |
|---|---|---|
| `services/proto/scorer.proto` | `ScoreWindow` request/reply, exact 16 kHz/2.56 s contract, version field | Generated client/server compatibility test |
| Gateway policy module | Session open, HMAC call reference, VAD/windowing, three-of-five evidence, purpose-to-action map | Deterministic unit tests with fixed score sequences |
| PostgreSQL migration set | Feature-only audit schema, retention index, hash-chain fields; production target tenant/RLS migration | Schema deny-list test and migration test on RDS + local Postgres |
| Model release directory | `model.onnx`, `calibration.json`, `release_manifest.json`, test vectors | SHA-256, PyTorch/ONNX parity, calibration and locked-set report |
| PWA protocol client | Consent notice, stream ticket, WSS frames, reconnect/backoff, risk states | Browser protocol test and real phone run |
| Test suite | API/WSS/gRPC/privacy/retention/policy/e2e tests parameterized by `BASE_URL` | Same tests run against AWS and local profiles |
| Traceability matrix | PS-01 to PS-11 and OUT-01 to OUT-04 | Every row has test, evidence, owner, and non-claim |

## 3. Day 1 — Contract, Privacy Boundary, and Two Empty Shells

Day 1 is successful only when both infrastructure shells exist and all teams agree on the exact data contract. Do not begin by training an untracked model or designing dashboard cards.

| Workstream | Concrete work | Acceptance evidence | Stop / escalation condition |
|---|---|---|---|
| Platform | Run CDK install/build/synth with `deployRuntime=false`; verify ECS ASG and services synth at zero. Create AWS account budget, ECR repositories, private S3, Cognito, VPC shell only after cost owner approves. | CDK synth report, runtime-zero template excerpt, cost-owner checklist | No runtime/GPU launch before budget stop test is planned |
| Local fallback | Create Compose topology with Caddy, PWA, Gateway, Scorer, PostgreSQL, and restricted local test issuer. Generate local CA/certificate and prepare the phone/LAN trust procedure. | `docker compose config`, service network diagram, no Gateway/Scorer/DB published ports | If local TLS cannot work on the demo phone, choose the tunnel/QR contingency now and rehearse it |
| Gateway | Freeze `SessionOpen`, binary frame layout, stream-ticket flow, error codes, and audit event schema. Add Origin allowlist requirement. | OpenAPI/proto checked in; JSON fixtures and binary fixture test | Any ambiguity in sample rate, endian order, sequence, or context code blocks the next day |
| ML | Create data manifest schema, consent ledger, split function, and baseline environment. Download only datasets whose terms have been accepted and recorded. | Empty validated manifest plus a small approved sample set; dependency lock | No audio enters training without source/licence/consent metadata |
| Privacy/QA | Write schema deny-list and log-redaction tests. Finalize language for consent/notice and outcome claim boundary. | Test specification and approved PWA copy | If raw audio need is proposed, require written case-hold policy; default remains off |

**Day 1 exit gate.** `pnpm build`, `pnpm synth`, PWA build, Python syntax compile, and local Compose configuration all pass. The team can explain the difference between `client_call_ref` and `call_ref` HMAC, between a raw classifier score and a policy action, and between AWS Budget and real-time cost control.

## 4. Day 2 — End-to-End Transport and Baseline Scoring

Day 2 validates the non-negotiable path before visual polish: consented capture or approved fixture → WSS → Gateway → gRPC → scorer → risk event → feature-only audit. Mock scorer is permitted only to test transport; it cannot be used as proof of detection.

| Workstream | Concrete work | Acceptance evidence | Stop / escalation condition |
|---|---|---|---|
| Gateway | Implement WSS ticket validation, Origin validation, strict sequence enforcement, VAD, ring window, scorer timeout, and connection cleanup. | Contract tests for valid and malformed sequence/frame paths | If any raw PCM appears in logging, stop and correct before proceeding |
| Scorer | Integrate a known baseline model or mock mode with explicit `MOCK_SMOKE_MODE_NOT_A_DETECTOR`; create health/readiness and model-contract check. | gRPC test returns model/calibration version and quality flags | Do not substitute fixed mock risk for real model evaluation |
| PWA | Connect mobile client to both local and AWS endpoint formats; show consent, start/stop, status, and current action. | Real-phone video of session accepted and clean stop | Replace the temporary capture path if device/browser fails repeatedly |
| Platform | Push signed/tagged ECR images; test first AWS task only after initial model image is ready. Build/load the local images from the same source commit; prepare `docker save` offline bundle. | ECR image scan, local image digest, Compose health | Do not enable full runtime if scorer model exits or quota is unavailable |
| ML | Run baseline AASIST and at least one comparator on declared small/dev protocol. Verify label orientation. | Baseline table, confusion matrix, fixed test vectors | If output class semantics are unclear, block ONNX export |

**Day 2 exit gate.** The exact same WSS binary fixture reaches both AWS Gateway and local Gateway; each writes a feature-only audit row with opaque HMAC reference. The mock mode remains labelled as transport-only. The evaluator has a baseline result table with data manifest hash.

## 5. Day 3 — Calibration, Policy Control, and CPU Performance

Day 3 turns a raw model score into a safer control mechanism. This is the day to demonstrate the problem solution—not merely classification.

| Workstream | Concrete work | Acceptance evidence | Stop / escalation condition |
|---|---|---|---|
| ML | Fit calibration on `dev_calibration`; create model/calibration release manifest; export ONNX; compare PyTorch versus ONNX fixed vectors. | Brier/ECE report, parity report, ONNX SHA-256 | Any parity/calibration failure blocks policy-eligible release |
| Gateway | Add `collecting → uncertain → high` logic, three-of-five high rule, versioned purpose policy, mock transaction `hold`, and signed feature-only webhook. | Deterministic score-sequence tests prove hold/verify/escalate actions | Never add a direct “approve” action or irreversible side effect |
| Local fallback | Run ORT CPU thread sweep on the actual demo laptop; record p50/p95 per setting. Test CPU model with same ONNX/calibration hash. | `cpu_benchmark.csv`, selected `ORT_INTRA_OP_THREADS` and host specs | If p95 target is missed, evaluate INT8 only through full parity/calibration gate |
| AWS platform | Verify GPU provider start, model load, first scoring latency, and RuntimeStopper Lambda manual test. | CloudWatch evidence of provider/model version and services/ASG returning to zero | If ASG does not reach zero, do not leave account running; repair manually first |
| QA/privacy | Implement hash-chain verifier and one-day retention test with controlled clock/fixture. | Tamper detection test and deletion test | If verification can be bypassed, label audit integrity as incomplete |

**Day 3 exit gate.** A real evaluated candidate model—not a mock—produces calibrated scores. Three controlled high windows produce a `hold` only for a high-value purpose. Both local and AWS providers report the same model/calibration hash. CPU latency is measured, not assumed.

## 6. Day 4 — Robustness, Privacy Inspector, and Full Evidence Pack

Day 4 hardens the claim. The output is a presentable evidence pack with limitations visible, not hidden.

| Workstream | Concrete work | Acceptance evidence | Stop / escalation condition |
|---|---|---|---|
| ML | Evaluate generator-, codec-, language-, device-, and duration-held-out subsets. Run diagnostic ablation only if primary baseline is stable. | Cohort metrics table, model card, failure examples, declared limitations | If a subgroup degrades materially, restrict claim and retain uncertainty rather than bury result |
| PWA | Add Privacy Inspector showing raw-audio off, opaque reference, retention period, model/policy version, inference profile, and current action explanation. | Mobile screenshots/video plus accessibility check | Never expose raw scores alone without state/action context |
| Platform | Add planned EventBridge Scheduler stop target or document it as not implemented; confirm local Caddy WSS reconnection after simulated reload. | Scheduler config/run log or a clearly labelled backlog entry; reconnect test | Do not represent missing scheduler/mTLS/PKCE as complete |
| Security | Run cross-tenant target test where tenant implementation exists; otherwise mark it production-backlog. Run test issuer restriction, secret scan, direct-service-port denial, and Origin denial. | Security test matrix with pass/fail and scope | A local no-password token issuer cannot be presented as authentication |
| QA/pitch | Complete traceability matrix and test the exact judge narrative: problem → privacy boundary → risk timeline → action → audit → limitation. | Slide-ready matrix and 90-second script | Remove unsupported graphs or metrics from pitch |

**Day 4 exit gate.** Every PS/OUT requirement has one test and one judge-visible proof. The team can open the DB proof showing no raw audio, demonstrate an HMAC pseudonym, show an action transition, and explain why real fraud-reduction claims await a pilot.

## 7. Day 5 — Rehearsal, Failover, and Final Demo

Day 5 is not a feature day. Freeze the release manifest, test both deployment profiles, and rehearse the fallback before judges arrive.

| Timebox | Activity | Required evidence |
|---|---|---|
| First rehearsal | Run AWS end-to-end with a consented legitimate sample and an approved simulated synthetic/replay sample. | Screen recording, model/policy version, score/state timeline, mock hold |
| Failover rehearsal | Stop AWS runtime intentionally; start local Compose; scan QR/visit local domain from judge-like device; repeat same script. | `docker compose ps`, phone WSS run, same model/calibration hash, local audit row |
| Privacy proof | Query audit table and verify no raw-audio fields; show Privacy Inspector and retention configuration. | Read-only query screenshot, log-schema scan |
| Cost proof | Confirm ECS services and GPU ASG return to zero after test. | ECS/ASG console screenshot and RuntimeStopper log |
| Recovery rehearsal | Simulate dropped WSS and one local Caddy reload; PWA reconnects or displays explicit retry state. | UX video and no duplicate policy action |
| Final freeze | Tag Git commit, export image/model bundle, archive metric and traceability PDFs/Markdown. | Release manifest signed by track owners |

### The 90-second judge script

1. State the risk: voice cloning creates a social-engineering channel in high-risk voice workflows.
2. Show the purpose notice and consented microphone activation. Explain that raw audio remains transient.
3. Start a legitimate sample. The UI shows `collecting`, then a non-blocking continuation.
4. Switch to an approved synthetic/manipulated test sample. The risk timeline accumulates evidence across windows, not one frame.
5. After the temporal threshold, show `high` and a simulated `hold` before a mock payment release.
6. Open Privacy Inspector and audit record: opaque reference, feature-only data, model/policy versions, action, hash chain—no raw audio.
7. State the limitation: this demonstrates simulated prevention-control effectiveness, not measured reduction in real fraud.
8. If AWS fails, state that the same signed release is running on the local edge fallback, then execute the already rehearsed local script.

## 8. Release Criteria and Go/No-Go Table

| Criterion | Go threshold | No-go / scope reduction |
|---|---|---|
| Model provenance | Manifest and permitted dataset/consent records complete | Demo only transport/policy flow; do not claim detection |
| Class semantics | Bona-fide/spoof output orientation verified | Block risk threshold and high-action policy |
| Calibration | ECE/Brier reported and calibration artifact matched to model | Show raw research result only, no probability language |
| ONNX parity | Fixed vectors and policy decisions match reference tolerance | Do not deploy ONNX model |
| Local CPU | Measured p95 on actual host meets chosen session cadence, or is honestly slower but stable | Use AWS only if reliable; otherwise show fixed non-live trace labelled as recorded |
| AWS runtime | GPU task healthy, WSS path valid, stop function works | Use local fallback as primary and do not wait for cloud repair |
| Privacy | Zero default raw persistence confirmed | Stop demo release until fixed |
| Claims | Traceability and non-claim language approved | Remove any unsupported outcome statement |

## 9. Post-Demo Teardown and Retrospective

Immediately after the final demonstration, stop both ECS services and set ASG min/desired/max to zero; then run `cdk destroy --force` when the team no longer needs AWS evidence. Confirm deletion in CloudFormation and inspect Cost Explorer later for residual charges. On the local host, use `docker compose down` and delete test volumes only after archiving approved feature-only evidence; delete or securely archive consented raw research audio according to the consent ledger, not the demo database retention policy.

The retrospective records: unsupported model cohorts, observed latency on both tiers, Caddy/device trust friction, AWS quota/cost friction, privacy-test gaps, and the top three production investments—mTLS/service identity, tenant RLS/encryption isolation, and broader controlled codec/language/generator evaluation.

## References

[1]: /home/ubuntu/upload/pasted_content_2.txt "Authoritative SIH26104 problem statement"
[2]: /home/ubuntu/upload/SIH26104—Expected-OutcomeAlignmentandPrivacy-LayerReport.md "Supplied expected outcome and privacy contract"
[3]: /home/ubuntu/upload/pasted_content_3.txt "Binding CPU-only fallback specification"
[4]: https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html "AWS Budgets timing and notifications"
[5]: https://caddyserver.com/docs/caddyfile/directives/reverse_proxy "Caddy reverse proxy"
