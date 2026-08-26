# SIH26104 — AI Training, Calibration, and Evaluation Playbook

**Document status:** Binding ML playbook for the PS104 team. **Purpose:** define how the team selects, trains or adapts, validates, calibrates, exports, and governs the voice-integrity model. This document deliberately separates benchmark success from a safe product decision. A model artifact may score a 2.56-second window; only the Gateway’s calibrated, temporal, context-aware policy decides whether to continue, verify, hold, or escalate.

> **Core ML rule:** A model is eligible for the live policy only when its provenance, input/output contract, calibration, benchmark results, codec/language/generator hold-outs, and ONNX parity checks are all recorded in a release manifest.

## 1. What the Model Must and Must Not Do

The primary model is an **audio anti-spoofing classifier**, not speaker verification and not fraud detection. Its task is binary at window level: estimate the probability that the 2.56-second PCM window contains evidence consistent with synthetic, converted, replayed, or otherwise manipulated speech under the model’s trained definition. The product turns this into a `spoof_risk` only after calibration. The policy engine then requires persistent evidence across windows before it emits a safe action.

| ML scope | Required behavior | Explicit exclusion |
|---|---|---|
| Primary task | Classify bona fide versus spoof/manipulated speech for a fixed raw-waveform input | Identify a person, infer intent, infer a caller’s truthfulness, or verify a transaction |
| Realtime unit | 2.56 seconds of 16 kHz mono PCM16; 640 ms score hop after voiced accumulation | Classify a 20 ms frame in isolation or infer from silence |
| Product score | Calibrated probability-like risk with quality flags | A raw logit or “AI percentage” without calibration |
| Policy input | Primary score plus valid quality checks; temporal evidence | Unvalidated CQT/phase/bicoherence/prosody heuristic |
| Cross-session feature | Disabled by default; opt-in research/prototype only | Passive speaker embedding retention or biometric surveillance |
| Evaluation claim | Measured discrimination, calibration, latency, and simulated-control effectiveness | “Significant fraud reduction” without an operational pilot |

## 2. Dataset Strategy and Data Governance

No single corpus proves Indian-language, mobile, codec, or new-generator robustness. The final training package uses a layered design: canonical anti-spoof benchmarking, broad multilingual synthetic-speech variation, diverse Indian bona-fide speech where terms permit, and a small fully consented local challenge set. The final hold-out must be disjoint from all tuning decisions.

| Dataset or source | Role | What it contributes | Guardrail |
|---|---|---|---|
| ASVspoof 2019 Logical Access train/dev | Canonical training and baseline comparison | Established bona-fide/spoof protocols for AASIST and baseline systems | Follow published protocol and licence; do not use final evaluation labels to tune |
| ASVspoof 2021 LA and DF | Benchmark robustness reporting | Channel variation in LA and a dedicated Speech Deepfake track; official EER/min t-DCF resources | Report separate LA/DF metrics; do not collapse them into one “accuracy” [1] |
| ASVspoof 5 plan/protocol | Evaluation discipline reference | Fixed versus flexible conditions and protocol-first reporting | Keep a locked, untouched hold-out manifest [2] |
| MLAAD, pinned revision | Secondary multilingual spoof diversity | The reviewed paper reports 378 h synthetic audio, 38 languages, 82 TTS models, 33 architectures | Pin exact release/manifest; generator-disjoint split; do not treat as Indian telephony proof [3] |
| IndicVoices, approved revision | Indian bona-fide diversity | Natural read, extempore, and conversational speech across 22 Indian languages and broad districts | Accept access terms, record licence/version; never label natural speech itself as spoof [4] |
| Team consented local set | Final product realism and judge demo | Exact phones, speakers, room acoustics, languages, codecs, and consented synthetic samples | Written consent; no unapproved cloning; never upload raw audio to shared cloud storage by default |

### 2.1 Dataset manifest: required fields

Every file is represented once in a versioned `manifest.parquet` or `manifest.csv`. The raw recording path stays in controlled research storage; audit and deployment data do not receive the path. The following fields are mandatory.

| Field | Example | Why it matters |
|---|---|---|
| `sample_id` | `asv19_LA_train_000123` | Stable immutable key |
| `split` | `train`, `dev`, `eval_locked`, `demo` | Prevents accidental split mixing |
| `label` | `bonafide`, `spoof` | Training target only |
| `source_dataset` | `ASVspoof2019_LA` | Provenance |
| `source_license` | `ODC-BY-1.0` | Reuse governance |
| `speaker_id_hash` | SHA-256 namespace-hash | Speaker disjointness without storing direct identity in model ledger |
| `language`, `script` | `hi`, `Devanagari` | Cohort analysis |
| `accent_region` | controlled vocabulary or `unknown` | Bias analysis; never infer this from audio |
| `generator_family`, `generator_version` | `VITS`, `heldout-v2` | Generator-disjoint split control |
| `attack_type` | `tts`, `vc`, `replay`, `codec_transcoded` | Failure analysis |
| `capture_device`, `codec`, `sample_rate_hz` | `android_midrange`, `opus_24k`, `16000` | Deployment realism |
| `channel_condition` | `clean`, `voip`, `speakerphone`, `noise` | Robustness stratification |
| `duration_ms`, `sha256_audio` | `5310`, hash | Deduplication and reproducibility |
| `consent_basis`, `retention_expiry` | `research-consent-v1`, date | Mandatory for local data |
| `derived_from_sample_id` | source ID or null | Tracks augmentations and prevents parent leakage |

### 2.2 Split protocol: non-negotiable

Perform grouping **before** any augmentation. A source parent, speaker, generator family/version, original text when known, and session must never leak across training and locked evaluation. The data scientist creates three nested partitions: `train`, `dev_calibration`, and `eval_locked`. The flexible robustness suite is additional—not a substitute for the locked evaluation set.

| Split | Uses | Must be disjoint by | Allowed decisions |
|---|---|---|---|
| `train` | Fit model weights | Speaker, parent sample, session, text where feasible | Architecture, optimizer, augmentation recipe |
| `dev_calibration` | Early stopping, threshold and calibrator fit | Speaker, parent, session, generator instance | Hyperparameters, Platt/isotonic choice, operating points |
| `eval_locked` | Final report only | Speaker, parent, session, generator family/version, codec-language cohort where possible | No tuning; one-time final evaluation per candidate release |
| `eval_generator_heldout` | Future-TTS simulation | Entire generator family/version | Robustness comparison only |
| `eval_codec_language_heldout` | Mobile/Indian deployment realism | Codec chain and language-device combinations | Safety and cohort report only |
| `demo` | Judge rehearsal and live proof | Separate consented sessions and speakers | Demonstration script; never use for model tuning |

## 3. Model Landscape and Final Selection

AASIST is the primary family because it is a recognised raw-waveform anti-spoofing architecture with official PyTorch training/evaluation code built around ASVspoof 2019. Its integrated spectro-temporal graph-attention design is appropriate for a core classifier, but its official repository is a research framework, not a ready-made multilingual, calibrated, licensed ONNX production package. [5]

| Candidate | Role | Decision | Reason |
|---|---|---|---|
| **AASIST** | Primary live scorer | **Adopt** | Strong anti-spoofing baseline family; raw waveform fits 16 kHz WSS contract; feasible GPU/CPU ONNX deployment after verification |
| LFCC-LCNN | Transparent baseline and diagnostic comparator | **Keep** | Official-style baseline family; useful to detect regression and show that primary model earns its complexity |
| CQCC-GMM | Legacy baseline / feature diagnostic | **Do not use live** | Useful as a reproducible diagnostic only; insufficient as the primary detector |
| RawNet2 | Raw-waveform baseline | **Keep as benchmark** | Helps test whether AASIST gain is real rather than data-pipeline artifact |
| SSL encoder + classifier | Research candidate | **Defer** | Can improve generalization but adds data, licence, latency, and fusion complexity unsuitable before the core is validated |
| Ensemble/fusion | Production research extension | **Reject for five days** | Multiple correlated scores invite leakage and overfitting; every added model requires calibration and ablation proof |
| CQT, phase, bicoherence, prosody | Explainability/diagnostic lane | **Ablation-gated** | The problem asks for them, but they cannot influence `hold`/`verify` until robust incremental value is demonstrated |

> **Decision rule:** the live policy reads one primary calibrated score. A diagnostic may be shown to an analyst as a labelled observation, but it cannot make a high-risk action more likely until an ablation report shows a material gain on generator-, codec-, language-, and device-held-out tests without fairness regression.

## 4. Exact Training Configuration

The following is the controlled **starting configuration**, not a claim that these settings are universally optimal. Every run stores a YAML config, Git commit, dataset manifest hash, random seed, environment lockfile, and artifact checksum. Do not change more than one factor per experiment.

| Parameter | Starting value | Rationale | Sweep only after baseline |
|---|---:|---|---|
| Input sample rate | `16000 Hz` | Matches product WSS and common ASVspoof workflows | `8000`, `16000` only as distinct deployment conditions—not spoof heuristics |
| Window length | `2.56 s` / `40,960` samples | Matches Gateway rolling window | `1.28`, `2.56`, `4.0 s` with latency trade-off report |
| Training crop | 40,960 samples; random crop for longer clips, repeat/pad policy documented for shorter clips | Aligns training/inference input contract | Padding policy only after short-utterance analysis |
| Batch size | `16` on 16 GiB GPU, gradient accumulation to effective `64` | Conservative starting point for `g4dn.xlarge` | Fit to actual VRAM; do not silently OOM-retry with changed batch |
| Optimizer | AdamW | Stable default for neural anti-spoofing fine-tune | Compare only after reproducible baseline |
| Learning rate | `1e-4` for fine-tune; `3e-4` for fresh head | Starting values, not official AASIST constants | Log-scale `3e-5` to `3e-4` |
| Weight decay | `1e-4` | Regularization starting point | `0`, `1e-5`, `1e-4` |
| Scheduler | cosine decay with 5% warmup | Limits abrupt early updates | Plateau scheduler only as controlled alternative |
| Epochs | max `50`, early stop patience `7` on dev EER | Avoids overtraining against demo corpus | Record best checkpoint by dev EER and calibration result |
| Loss | weighted cross entropy, bona-fide/spoof weights from training manifest | Handles observed imbalance transparently | Focal loss only with false-negative analysis |
| Precision | FP32 reference; mixed precision only after parity | Calibration should start on reference precision | FP16/bfloat16 requires score-distribution regression |
| Seeds | `17`, `23`, `41` | Minimum variance estimate | Report mean and range, not a single lucky seed |
| Data-loader workers | `4` baseline | Avoids starving GPU on small host | Tune separately from model hyperparameters |

### 4.1 Augmentation policy

Augmentation simulates distribution shift; it must never create label leakage or alter all spoof samples in a way that makes classification trivial. Apply each transformation to both bona-fide and spoof branches unless the transformation models an attack class that is separately labelled.

| Augmentation | Parameter range | Apply to | Purpose | Gate |
|---|---|---|---|---|
| Resampling | 8 kHz ↔ 16 kHz ↔ 24 kHz then product-normalize to 16 kHz | Both | Telephony/source-rate robustness | Report per-rate performance |
| Codec round-trip | Opus, AAC, G.711 μ-law/A-law profiles | Both | VoIP and PSTN approximation | Hold out one codec family |
| Additive noise | Noise classes at 5–30 dB SNR | Both | Device/environment robustness | Hold out noise environment |
| Room impulse response | Small/medium/large simulated rooms | Both | Speakerphone/reverberation effect | Do not replace real local device test |
| Gain/clipping | Controlled gain, light clipping only | Both | Cheap handset/mic variation | Audit waveform validity |
| Packet-loss/dropout simulation | Short, bounded loss masks | Both or labelled channel condition | WSS/VoIP robustness | Ensure no model learns mask artifact |
| RawBoost-style perturbations | Only after baseline | Both | Potential unseen-spoof generalization | Separate ablation experiment |

Never use an 8 kHz or 16 kHz sampling boundary as a spoof rule. Sampling rate is a channel characteristic, not evidence of synthetic speech.

## 5. Calibration and Policy Translation

The scorer returns a raw logit or class score. The calibration package maps it to a bounded `spoof_risk`. Fit calibration on `dev_calibration` only; then freeze it with the model. Start with Platt scaling/logistic calibration because it is simple and stable under limited data; compare isotonic regression only if it improves calibration error without degrading locked-set behavior.

| Artefact | Required contents | Validation |
|---|---|---|
| `aasist.onnx` | Input name/shape/dtype, output interpretation, model SHA-256, source commit | PyTorch-vs-ONNX score parity on fixed test vector set |
| `calibration.json` | Method, `slope`, `intercept` or isotonic bins, calibration data manifest hash, model SHA, version | Brier score, ECE, reliability plot on dev and locked evaluation |
| `policy.yaml` | High-risk threshold, three-of-five rule, purpose-to-action map, policy version | Policy simulation confusion matrix and time-to-action chart |
| `release_manifest.json` | Model/calibration/policy hashes, datasets, environment, commit, execution provider | Signed review by ML and privacy owners |

The first policy threshold is an **experimental operating point**, not a universally valid `0.78`. Select it by examining a cost-sensitive matrix: false high-risk holds burden legitimate users; false low-risk continues miss simulated attacks. The target operating decision must be approved per use case and show a confidence/uncertain band. The model should never block an action on one high window.

## 6. Evaluation Protocol and Metrics

Report two types of metrics and never mix them. **Benchmark metrics** make results comparable to anti-spoofing literature. **Product metrics** establish whether the system is usable and safe in this deployment profile.

| Category | Metric | Interpretation | Report stratification |
|---|---|---|---|
| Benchmark discrimination | EER | Point where false accept and false reject rates match | ASVspoof 2019/2021 LA/DF separately [1] |
| Tandem risk | min t-DCF | Cost measure when combined with ASV assumptions | Official protocol only; do not fabricate ASV parameters |
| Product discrimination | ROC-AUC, PR-AUC, TPR at selected FPR, FNR at selected operating point | Classifier separation under product-like cohorts | Codec, language, device, generator, duration |
| Calibration | Brier score, Expected Calibration Error, reliability diagram | Whether risk can be treated as probability-like | Global and high/low risk cohort |
| Temporal policy | Session-level sensitivity, false hold rate, detection-to-action time | Evidence accumulation and intervention quality | Purpose code and codec/device cohort |
| Runtime | p50/p95 scorer latency, first-decision latency, backlog | Demo usability | AWS GPU and actual fallback laptop separately |
| Fairness/robustness | Worst-group metric and max gap | Detect hidden degradation for language/accent/device cohorts | Only consented/legitimately labelled metadata |

### 6.1 Evaluation gates

| Gate | Pass condition | Owner | Failure response |
|---|---|---|---|
| Data gate | Manifest validated; no split leakage; licence/consent present | Data lead | Stop training and repair provenance/splits |
| Baseline gate | AASIST exceeds or matches LFCC-LCNN and RawNet2 on declared dev protocol | ML lead | Investigate input pipeline before adding features |
| OOD gate | Report generator-, codec-, language-, and device-held-out results | ML + evaluation lead | Restrict claim or retain `uncertain` policy; do not hide gap |
| Calibration gate | Improved ECE/Brier on dev without harmful locked-set regression | ML lead | Freeze simpler calibrator or retrain |
| ONNX parity gate | Output ranking and calibrated decisions match reference within predeclared tolerance | ML + platform lead | Block deployment artifact |
| Quantization gate | Locked-set metrics, calibration, and temporal policy remain acceptable | ML lead | Retain FP32 model |
| Privacy gate | No raw audio/transcript/embedding in audit/log export | Privacy lead | Block demo release |
| Demo gate | AWS GPU and local CPU run same test trace with recorded latency | Team lead | Fix parity or present one tier only, truthfully |

## 7. ONNX Export, CPU/GPU Parity, and Serving Parameters

Export only after the PyTorch model passes evaluation. Freeze preprocessing inside a clearly defined boundary. The recommended contract is raw normalized mono waveform `[1, 40960]` float32, with the original PCM16-to-float conversion documented outside the graph. Do not leave resampling, clipping policy, channel downmix, or output class orientation implicit.

| Check | AWS GPU scorer | Local CPU scorer | Required parity evidence |
|---|---|---|---|
| Execution provider | `CUDAExecutionProvider`, CPU fallback as last resort | `CPUExecutionProvider` only | Startup log confirms provider list |
| Runtime package | Pinned `onnxruntime-gpu` compatible with CUDA/cuDNN | Pinned plain `onnxruntime` | Image SBOM and package lock |
| Input | Exact raw-waveform 16 kHz float32 tensor | Same | Fixture hash and shape assertion |
| Model/calibration | Same SHA-256 | Same SHA-256 | Release manifest fails on mismatch |
| Threading | GPU worker concurrency measured | `intra_op` sweep, `inter_op=1` start, sequential mode | Laptop p50/p95 sweep report |
| Quantization | Optional, separate model version | Optional, separate model version | No silent artifact replacement |

ONNX Runtime’s CUDA Execution Provider is sensitive to CUDA/cuDNN compatibility and provider configuration; production images must pin those versions rather than rely on a moving “latest” tag. [6] For the CPU tier, tune threads on the real laptop. A reported p95 is a measurement from a named host, not a portability promise.

## 8. Diagnostics and Explainability Lane

The statement explicitly mentions acoustic/spectral artefacts, phase inconsistencies, and prosody. Satisfy this requirement through a **diagnostic sidecar**, not unsupported rules. It can emit a compact `diagnostic_summary` such as spectral-flatness percentile, CQT consistency descriptor, phase-coherence descriptor, prosody stability descriptor, and quality flags. It cannot store raw feature matrices by default or alter high-risk policy until it passes the ablation gate.

| Diagnostic | Valid initial use | Invalid use |
|---|---|---|
| CQT/spectral descriptor | Analyst explanation and error analysis | “High-frequency cutoff means clone” rule |
| Phase/coherence descriptor | Compare distribution shifts across codec cohorts | Stand-alone voice-clone verdict |
| Bicoherence/bispectral descriptor | Research ablation and visual explanation | Uncalibrated score fusion |
| Prosody descriptor | Investigate rhythmic/pitch model failures | Penalize accent, illness, emotion, or speaking style |
| Voice activity and quality flags | Avoid scoring silence and low-quality windows | Treat VAD outcome as spoof evidence |

## 9. Community, Tooling, and Reproducibility

The community resources are inputs to a reproducible engineering process, not plug-and-play authority. Use the official ASVspoof challenge repository for baseline and metric reference; use the official AASIST repository for architecture/training comparison; use dataset cards/papers to pin terms and revision. [1] [5]

Every experiment writes: dataset manifest hash; split hash; training config; source commit; container digest; Python/driver/CUDA/ORT versions; seed; checkpoint SHA-256; ONNX SHA-256; calibration SHA-256; full metric table; and a decision whether the artifact is `research_only`, `demo_eligible`, or `policy_eligible`. Save the exact audio list only in approved research storage, never the deployment repository.

## 10. Five-Day ML Priorities

| Day | Minimum ML deliverable | Stop condition |
|---:|---|---|
| 1 | Dataset manifest, consent ledger, ASVspoof baseline environment, model contract test vector | Do not proceed without split/provenance controls |
| 2 | AASIST baseline score pipeline plus LFCC-LCNN/RawNet2 comparator on a small declared protocol | Do not add diagnostics/ensembles before baseline works |
| 3 | Calibration, threshold simulation, ONNX export, PyTorch-to-ONNX parity | Do not deploy score if class orientation/calibration is uncertain |
| 4 | Codec/language/generator/device holdout table; CPU thread sweep; optional quantization gate | Do not promise fallback speed until actual laptop p95 exists |
| 5 | AWS/local demo score trace, privacy proof, model manifest, claim-boundary slide | Do not claim real fraud reduction or universal clone detection |

## References

[1]: https://www.asvspoof.org/index2021.html "ASVspoof 2021 official challenge page"
[2]: https://www.asvspoof.org/file/ASVspoof5___Evaluation_Plan_Phase1.pdf "ASVspoof 5 evaluation plan"
[3]: https://arxiv.org/html/2401.09512v5 "MLAAD: The Multi-Language Audio Anti-Spoofing Dataset"
[4]: https://huggingface.co/datasets/ai4bharat/IndicVoices "AI4Bharat IndicVoices dataset card"
[5]: https://github.com/clovaai/aasist "Official AASIST PyTorch repository"
[6]: https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html "ONNX Runtime CUDA Execution Provider"
[7]: /home/ubuntu/upload/pasted_content_2.txt "Authoritative SIH26104 problem statement"
[8]: /home/ubuntu/upload/SIH26104—Expected-OutcomeAlignmentandPrivacy-LayerReport.md "Supplied expected outcome and privacy contract"
[9]: /home/ubuntu/upload/pasted_content_3.txt "Binding CPU-only fallback specification"
