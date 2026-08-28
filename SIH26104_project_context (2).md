# SIH26104 — Real-time Voice-Clone Detection — Project Context

## Problem Statement
AICTE PS SIH26104: AI-Powered Real-Time Detection and Prevention of Voice Cloning Impersonation Attacks. Goal: privacy-preserving voice-integrity gateway with calibrated risk scoring (not binary claims), spoof detection + transaction-context policy + secondary-verification recommendations. NOT meant to autonomously approve/reject payments — advises verification only.

## Timeline constraint
4 days total. Full production architecture (telephony ingestion, Kafka/Redis streaming, drift monitoring, full multilingual coverage) is NOT feasible. Strategy: demo-fidelity MVP hitting every "judging test" item honestly (not overclaimed).

## Project strategic direction (IMPORTANT — decided after baseline work)
User's own judgment, confirmed correct by research: retraining AASIST from scratch on ASVspoof is pointless (researchers already optimized it there; no headroom for improvement — confirmed empirically, see below). The actual valuable contribution is:
1. Take pretrained AASIST as strong base (not reinventing it).
2. **Empirically expose its real-world generalization gap** against modern/real internet deepfakes (not just the 2019 lab benchmark).
3. Fine-tune specifically to close that gap.
4. Phase 2 (later, lower priority per user): extend to Indian languages/accents.
User is NOT interested in a "train for nothing valuable" approach — wants a defensible, judge-impressing research narrative, not just benchmark reproduction.

## Environment
Kaggle Notebook. **GPU: Tesla T4** — must explicitly select "GPU T4 x2" in Settings → Accelerator (avoid P100 — incompatible with current PyTorch sm support; avoid whatever gives sm_120 errors on old torch too). Switching accelerator type wipes `/kaggle/working`, requiring full re-clone/re-setup — happened once already.

## Golden-path setup (fully working, reproducible — copy-paste this to rebuild in a new notebook)
1. Settings → Accelerator → GPU T4 x2. Settings → Internet → On.
2. Clone: `%cd /kaggle/working` then `!git clone https://github.com/clovaai/aasist.git` then `%cd /kaggle/working/aasist` then `!pip install -r requirements.txt`. (Pretrained weights `AASIST.pth`/`AASIST-L.pth` already bundled in repo at `models/weights/` — no separate download needed.)
3. Attach Kaggle dataset **`awsaf49/asvpoof-2019-dataset`** via Add Input. Real path has double nesting: `/kaggle/input/datasets/awsaf49/asvpoof-2019-dataset/LA/LA/`
   - Protocols: `ASVspoof2019_LA_cm_protocols/ASVspoof2019.LA.cm.{train.trn,dev.trl,eval.trl}.txt` (format: `speaker_id filename - attack_type label`, label = `bonafide`/`spoof`)
   - Audio: `ASVspoof2019_LA_{train,dev,eval}/flac/<filename>.flac`
4. Load model:
```python
import torch, sys, json

sys.path.insert(0, "/kaggle/working/aasist")
from models.AASIST import Model

with open("/kaggle/working/aasist/config/AASIST.conf") as f:
    config = json.load(f)
model_config = config[
    "model_config"
]  # architecture=AASIST, nb_samp=64600 (~4sec @16kHz)
device = torch.device("cuda")
model = Model(model_config).to(device)
model.load_state_dict(
    torch.load("/kaggle/working/aasist/models/weights/AASIST.pth", map_location=device)
)
model.eval()
```
5. Preprocessing + scoring (define once per session, gets lost across kernel restarts — redefine each fresh session):
```python
import librosa, numpy as np


def pad(x, max_len=64600):
    x_len = x.shape[0]
    if x_len >= max_len:
        return x[:max_len]
    num_repeats = int(max_len / x_len) + 1
    return np.tile(x, (1, num_repeats))[:, :max_len][0]


def get_score(filepath, model):
    y, sr = librosa.load(filepath, sr=16000)
    y_padded = pad(y)
    x_inp = torch.Tensor(y_padded).unsqueeze(0).to(device)
    with torch.no_grad():
        _, output = model(x_inp)
        return output[:, 1].item()
```
Inference: `score = output[:, 1]` — index 1 = bonafide-leaning raw logit (no softmax). Higher = more bonafide-like.
6. EER computed via sklearn `roc_curve` standard method (see reusable snippets section).

## Results so far

### 1. Baseline reproduction (ASVspoof 2019 LA eval, pretrained AASIST, untouched)
- Single-file sanity check: spoof file scored -6.77, bonafide scored +4.46 (clear separation).
- **Batch EER on 300 bonafide + 300 spoof eval subset: 0.67%** — consistent with published AASIST paper (~0.83% EER on full eval set). Pipeline validated as correct.

### 2. Mechanical fine-tuning proof-of-concept (on ASVspoof train subset — NOT the real objective, just validating the training loop works)
- Loaded `Dataset_ASVspoof2019_train` + `genSpoof_list` from repo's own `data_utils.py`, subset of 4000/25380 train files, batch_size=24, Adam optimizer (lr=0.0001, matching config), CrossEntropyLoss, initialized from pretrained weights.
- 2 epochs: loss dropped to 0.0047 avg (near-zero — loop works, converges fast as expected for fine-tuning).
- Re-evaluated EER after this fine-tune: **1.00%** (slightly worse than 0.67% baseline) — expected: model already had zero headroom on this distribution; further training on same-distribution data caused mild overfit/drift, not improvement. Confirms training mechanics work; confirms no point fine-tuning further on ASVspoof itself.
- Checkpoint saved at `/kaggle/working/aasist_finetuned_poc.pth` (not particularly useful, POC only).

### 3. Real-world generalization gap test — key early finding
- Dataset: **In-the-Wild** (Müller et al. 2022) — 58 public figures, real internet-scraped speeches/interviews (bonafide) vs real deepfakes (spoof) circulating online. Downloaded via HuggingFace: `mueller91/In-The-Wild` (`huggingface_hub.snapshot_download`), extracted zip to `/kaggle/working/in_the_wild/release_in_the_wild/`. Metadata: `meta.csv` with columns `file, speaker, label` (label values: `bona-fide`, `spoof`). 19,963 bonafide + 11,816 spoof total, native sr already 16000, durations 1-14+ sec (variable, unlike ASVspoof's uniform ~4sec).
- Tested **clean/untouched pretrained AASIST** (not the POC fine-tune) on 300+300 balanced sample from In-the-Wild.
- **Result: 43.00% EER** (vs 0.67% on ASVspoof) — near chance-level (50% = random guessing). Mean bonafide score -0.81, mean spoof score -1.51 (both near zero, heavily overlapping, weak separation).
- Diagnosed and confirmed this was **genuine domain shift, not a pipeline bug**: sample rate uniform 16000 across all files (ruled out resampling artifacts), waveform amplitude/RMS checks showed normal healthy speech (ruled out corruption/clipping), score distributions showed weak/noisy discrimination rather than confidently-flipped predictions (consistent with real domain shift, matches published literature on ASVspoof-trained detectors collapsing on in-the-wild data).

### 4. Fine-tuning phase — CLOSED, final model chosen
Full detail of the fine-tuning journey (speaker-disjoint splits, low-LR fine-tuning, an ITW-only run, a mixed ASV+ITW run, a lost-session incident and recovery via Kaggle Save & Run All commits, and the final head-to-head decision) — summarized result:

**Final chosen model: `best_mixed_finetune.pth`** — trained on a class-weighted mix of ASVspoof train subset (5,000+5,000) + full In-the-Wild train split (22,615 files), 4 epochs, Adam lr=3e-5, best-checkpoint-on-joint-val-EER tracking.

**Full three-domain held-out result set (the pitch's core evidence table):**
| Test set | EER |
|---|---|
| ASVspoof 2019 (original lab benchmark, clean eval split) | 1.67% |
| In-the-Wild (real-world/different speakers, held-out test split) | 3.17% |
| ASVspoof 2021 DF (codec/compression artifacts, official eval split) | 13.00% |

**Narrative for the pitch:** started at 0.67% EER on the lab benchmark; found it collapses to 43% (near-random) on real-world deepfakes; fine-tuned to recover strong performance across both domains (1.67%/3.17%) while honestly disclosing a remaining, specific weakness against heavy codec/compression artifacts (13.00%) — named directly as a limitation rather than hidden, since real phone calls are compressed/transcoded constantly.

**Decision rationale (why mixed over ITW-only):** an ITW-only fine-tune got even better on ITW (1.07%) but regressed badly on ASVspoof (11.00% — catastrophic-forgetting-style trade-off). The mixed model trades a little ITW performance (1.07%→3.17%) for a large ASVspoof recovery (11.00%→1.67%), making it the more robust, well-rounded detector for real deployment against a mix of older/simpler and modern real-world cloning methods.

**Key operational lessons learned (avoid repeating):**
- Any Kaggle run over ~30 min must use **Save Version → Save & Run All (Commit)** — an interactive-only run was lost entirely once when the laptop closed mid-training, with no checkpoint surviving.
- Consolidate to one clean function definition before committing a long run — a stale duplicate `evaluate_eer` call crashed a commit attempt once.
- A suspiciously perfect `0.00%` EER seen mid-training turned out to be a small-sample fluke (dev-split validation, not a bug) — confirmed clean via later proper held-out eval (1.67%). Don't trust suspiciously perfect metrics until re-verified on a genuinely separate test set.
- Not pursuing further cross-dataset checks (e.g. WaveFake) beyond the three domains above unless significant time remains after the demo/policy layer is built — three domains was judged sufficient generalization evidence for the 4-day budget.

**All checkpoints downloaded locally and verified loadable:** `aasist_finetuned_poc.pth` (early POC, not used further), `best_itw_finetune.pth` (1.07% ITW / 11.00% ASV, not chosen), `best_mixed_finetune.pth` (**final model, chosen for all downstream work**).

### 5. Personal live voice-clone test (own voice, XTTS-v2) — RESULTS
Purpose: build first-hand intuition for how well the final model works, beyond benchmark numbers, and create a compelling, personally-generated live demo case for judges.

**Tooling decision:** ElevenLabs instant voice cloning turned out to require a paid Starter plan (~$5/mo) — free tier only offers prebuilt stock voices, not cloning. Switched to **XTTS-v2 (Coqui)** instead — free, open-source, zero-shot cloning from a short reference clip (~10-15 sec), and arguably more relevant to the PS anyway since it's a real tool actual attackers use.

**Install gotcha:** the original `pip install TTS` package is unmaintained and capped at Python <3.12; Kaggle runs 3.12+, causing a hard install failure. Fix: install the maintained community fork instead — `pip install coqui-tts` — same import path (`from TTS.api import TTS`), no code changes needed otherwise. Also set `os.environ["COQUI_TOS_AGREED"] = "1"` before importing to auto-accept XTTS's license prompt (avoids hanging on an interactive `input()` call in a notebook).

**Procedure:** recorded a personal voice clip via phone Voice Memos, exported as `.m4a`, uploaded to Kaggle as a new private dataset ("Rikuvoice"), attached to the notebook. Path: `/kaggle/input/datasets/swarnendusahu/rikuvoice/Recording.m4a`. Generated a cloned clip via `tts.tts_to_file(text=..., speaker_wav=<that path>, language="en", file_path=...)` using the `xtts_v2` multilingual model.

**Result, scored with `best_mixed_finetune.pth`:**
| Sample | Score | Correct? |
|---|---|---|
| Real voice (own recording) | **+2.94** | ✅ correctly bonafide (positive) |
| XTTS-cloned voice (same voice, synthesized) | **-1.07** | ✅ correctly spoof (negative) |

**Model correctly caught a real, personally-generated modern voice clone it had never seen before, with a clear ~4.0-point score margin.** Separation is less dramatic than the clean ASVspoof benchmark cases (+4.96/-4.79) but direction and margin are both unambiguous. This is a strong, reproducible, personal demo moment for the pitch — genuinely more convincing to judges than benchmark numbers alone, since it can be reproduced live (record → clone → score, in front of the judges).

Minor harmless warning seen during scoring: librosa falls back from PySoundFile to the slower `audioread` backend for `.m4a` files — doesn't affect correctness, just a deprecation-style warning, no action needed.

## PLAYBOOK ALIGNMENT — CRITICAL, READ FIRST (as of this update)
A formal, binding backend-facing document was supplied: `PS104_AI_Training_and_Evaluation_Playbook.md` (governs how the main backend/Gateway expects the model to behave). Comparing it against everything above surfaced **structural mismatches that must be fixed before `best_mixed_finetune.pth` can plug into the backend** — this is not just documentation cleanup, the model's actual input contract is wrong.

**What already matches (no action needed):** output contract (raw logit before calibration — already doing this, no softmax); AASIST as primary model choice; ASVspoof 2019 as canonical train/baseline; ASVspoof 2021 DF as robustness benchmark; team-consented local recording (the Rikuvoice/XTTS test) fits the playbook's own "team consented local set" data role.

**Critical mismatches identified (must fix, in this order):**
1. **Input window length — THE BIG ONE.** Our entire pipeline (baseline, fine-tuning, all EER numbers so far) used AASIST's own default of 64,600 samples (~4.04s). The playbook's Gateway contract requires **2.56s windows = exactly 40,960 samples at 16kHz**, with a 640ms score hop for streaming. `best_mixed_finetune.pth` as it stands **will not integrate with the backend** — wrong input contract, and the model was never trained/fine-tuned at this window length so its behavior there is unverified.
2. **No ONNX export.** Playbook requires `aasist.onnx` with a documented input contract (`[1, 40960]` float32 raw waveform) and a PyTorch-vs-ONNX parity check across GPU/CPU execution providers (Section 7) — this is a hard release gate. We've only worked in PyTorch on Kaggle so far.
3. **No calibration artifact.** Playbook requires `calibration.json` (Platt scaling fit on a `dev_calibration` split only, then frozen with the model) turning the raw logit into a bounded `spoof_risk` probability, with Brier score/ECE reported. Our raw logits (e.g. +2.94/-1.07 from the personal voice test) are NOT yet a calibrated probability.
4. **Policy engine must be temporal/session-based, not single-clip.** Playbook Section 5 explicitly: *"The model should never block an action on one high window"* — requires persistent evidence across multiple windows (e.g. a "three-of-five" style rule), not the single-clip `score → action` design we were about to build.

**Governance gap flagged (not necessarily a blocker, but must be disclosed honestly):** In-the-Wild is NOT in the playbook's approved dataset table (approved list: ASVspoof 2019/2021/5, MLAAD pinned revision, IndicVoices, team-consented set only). It has real research value — it's the source of the whole 43%→3% generalization story — but its speakers (public figures) have no research-consent basis under this playbook's manifest/consent system. Decision: keep using it and reporting its results as legitimate robustness/generalization evidence in the pitch narrative, but do NOT label a model trained on it as "policy_eligible" under the playbook's own eligibility framework (Section 9) without disclosing this. If a strictly "policy_eligible" artifact is ever required, the final candidate should be retrained on approved-only sources (ASVspoof + MLAAD) with In-the-Wild results cited separately as a diagnostic finding.

**Re-plan going forward (agreed approach — quick full re-plan before executing, then in order):**

*Must-fix, execution order:*
1. Verify AASIST's architecture accepts a 40,960-sample input cleanly (quick dummy-tensor forward-pass check before committing to a full retrain — pooling/graph-attention layers may or may not handle the shorter length without adjustment).
2. Re-fine-tune at the corrected 40,960-sample/2.56s window length. Warm-start from `best_mixed_finetune.pth` (faster, spoof-artifact features likely transfer) rather than restarting from raw pretrained `AASIST.pth`; fall back to a from-pretrained restart only if warm-start fine-tuning doesn't converge well at the new window length.
3. Re-measure EER on all three domains (ASVspoof 2019, In-the-Wild, ASVspoof 2021 DF) at the new window length — the existing 1.67%/3.17%/13.00% table was measured at 4.04s and will shift; must be re-reported honestly at the correct contract length.
4. Fit Platt-scaling calibration on a proper `dev_calibration` split (carve out from existing val data), produce `calibration.json` (slope/intercept), report Brier score + ECE.
5. Redesign the policy engine around temporal/session evidence (stream of window scores → persistence rule → action), not a single whole-clip score. This changes the shape of the eventual demo too — continuous scoring over a stream, not one-shot.
6. Export to ONNX, verify PyTorch-vs-ONNX score parity on a fixed test-vector set, confirm it runs correctly on both CPU and GPU execution providers.

*Deprioritized / likely skipped given the 4-day budget (do lightweight versions only, or just disclose the gap in the report rather than building the full infrastructure):* full manifest/hash/provenance tracking system (Section 9) — do a lightweight version instead (record dataset versions + final checkpoint SHA-256 here in this file); LFCC-LCNN/RawNet2 baseline comparators — mention as "architecturally straightforward, not built due to time" in the report unless time remains after the must-fix list; MLAAD/IndicVoices (Phase 2 Indian-language work) — stays deprioritized as already planned; full dataset governance re-architecture — one honest disclosure paragraph in the pitch is sufficient, not a rebuild of data sourcing.

**Status: Priority 1 (window length) — FIX APPLIED, but introduced a regression that needs a follow-up fix. See below.**

### Priority 1 fix — execution log and result

Confirmed via dummy-tensor forward pass that AASIST's architecture accepts a 40,960-sample input with no shape errors. Rebuilt `pad()` (max_len=40960), rebuilt `ASVspoofDataset`/`InTheWildDataset` as thin custom Dataset classes (AASIST's own `Dataset_ASVspoof2019_train` hardcodes `cut=64600` internally and can't be reused directly). Rebuilt the same ASVspoof subset (2,580 bonafide + 5,000 spoof — bonafide pool is smaller than 5,000 in the real train set, capped automatically) + In-the-Wild speaker-disjoint split (same seed=42: 22,615 train / 3,365 val / 5,799 test, reproducible). Warm-started from `best_mixed_finetune.pth`, lr=3e-5, 4 epochs, Save & Run All (Commit) used correctly this time.

**Training curve (checkpoint selection tracked ITW val EER only — see root-cause note below):**
| Stage | ITW val EER |
|---|---|
| Pre-finetune (old model, new window, untested at this length) | 13.28% |
| Epoch 1 | 11.80% |
| Epoch 2 | 10.40% |
| Epoch 3 | 10.34% |
| Epoch 4 (best) | **6.33%** |

Final checkpoint: **`best_mixed_finetune_256s.pth`** (saved to `/kaggle/working`, downloaded locally as backup — recommend also uploading as a Kaggle Dataset for permanence, same as the voice recording, since `/kaggle/working` isn't guaranteed to survive indefinitely).

**Full three-domain re-evaluation at the corrected 2.56s window, compared to the old 4.04s numbers:**
| Domain | Old (4.04s) | New (2.56s) | Change |
|---|---|---|---|
| ASVspoof 2019 eval (full ~71K set) | 1.67% | **6.21%** | worse |
| In-the-Wild test (held-out speakers, 5,799 files) | 3.17% | **1.90%** | better |
| ASVspoof 2021 DF (300+300 sample, eval-phase only, via `trial_metadata.txt`) | 13.00% | **15.83%** | worse |
| Personal voice (Rikuvoice real vs XTTS-cloned) | +2.94 / −1.07 (gap ~4.01) | +2.79 / −0.27 (gap ~3.06) | still correctly separated, but less confident margin |

**ROOT CAUSE OF THE REGRESSION (important, avoid repeating):** the re-fine-tune's checkpoint-saving loop (Cell 5 of the window-length fix) tracked **only In-the-Wild validation EER** for deciding which epoch's weights to keep as "best" — it did NOT track a joint/combined metric across both ASVspoof and In-the-Wild, unlike the reasoning that went into choosing the original `best_mixed_finetune.pth` over `best_itw_finetune.pth`. This let the training silently re-specialize toward In-the-Wild at the expense of ASVspoof (both 2019 and 2021 DF), reproducing the same catastrophic-forgetting-style trade-off that was already identified and solved once before in the original fine-tuning phase. This is a **process mistake to avoid repeating**: any future fine-tuning run must select/save the "best" checkpoint using a combined criterion (e.g. average or worst-of ASVspoof-val-EER and ITW-val-EER), not a single domain's validation metric alone.

**NEXT ACTION NEEDED (not yet done):** re-run the fine-tune again — same warm-start from `best_mixed_finetune.pth`, same 2.56s window, same data splits — but change the checkpoint-selection logic to track a joint validation EER across both domains (need a small ASVspoof dev/val subset loader alongside the existing `itw_val_loader` to compute this each epoch). This should recover a better-balanced result closer to the original 1.67%/3.17%/13.00% pattern, but at the correct window length this time. ASVspoof 2021 DF's slight regression (13.00%→15.83%) may persist somewhat regardless — codec/compression artifacts already were the weakest point before, and this needs to be watched but is not necessarily fully fixable in the remaining time; it should be disclosed honestly as a known limitation either way, consistent with the project's "calibrated humility, don't overclaim" strategy.

## Datasets identified for future testing/fine-tuning (real-world/modern generator focus)
- **In-the-Wild** — used, see above.
- **ASVspoof 2021 DF** — used as third-domain cross-check, see above. Labels required a separate direct download (`wget https://www.asvspoof.org/asvspoof2021/DF-keys-full.tar.gz`) since the Kaggle audio mirror (`mohammedabdeldayem/avsspoof-2021`) doesn't include eval labels.
- **WaveFake** (Zenodo) — 6 modern vocoders/TTS systems. Not yet used; optional if time remains.
- **MLAAD** — multilingual anti-spoofing dataset, relevant for planned Phase 2 (Indian languages). Not yet used.
- Modern real-world cloning tools attackers actually use (context/relevance): ElevenLabs, RVC, **XTTS-v2 (now personally tested against, see section 5)**, Tortoise-TTS, OpenVoice, so-vits-svc, VALL-E-style/CosyVoice.

## Not done yet / next steps (SUPERSEDED BY PLAYBOOK RE-PLAN ABOVE — that section is now authoritative for immediate next steps)
The list below is the pre-playbook plan; items 2-6 are still valid and resume after the playbook must-fix list is complete. Item 1 (calibration+policy as originally scoped) is superseded — see "PLAYBOOK ALIGNMENT" section above for the corrected, temporal/session-based version.
1. ~~Build calibration + policy engine layer as single-clip score→action~~ — SUPERSEDED, see playbook re-plan (must be temporal/session-based, calibration must be Platt-scaling on a proper dev_calibration split).
2. Build React dashboard + mock bank approval flow for the demo (resume after playbook must-fix list).
3. Additional codec/noise stress tests + small multilingual slice for judging criteria, time permitting — don't claim broad generalization (note: ASVspoof 2021 DF check already covers codec-artifact stress-testing at a basic level).
4. Write a short privacy-policy doc (feature-summary retention, no raw audio storage) — no real audit needed.
5. Skip/fake for demo: real Kafka/Redis streaming (use WebSocket loop), real production drift monitoring (toy example).
6. Phase 2 (lower priority, likely post-deadline): Indian language/accent generalization using MLAAD or similar.
7. Consider generating 1-2 more personal test clips (different sentences/tools, e.g. RVC) if time allows, to strengthen the live-demo narrative beyond a single XTTS example.
8. **Model handling going forward:** any Kaggle run over ~30 min must use Save & Run All (Commit), never interactive-only. Consider publishing the final calibrated/ONNX model as a Kaggle Dataset for reuse across notebooks (e.g. the demo backend) rather than re-uploading manually each time.

## Reusable code snippets
**EER function:**
```python
def compute_eer(bona_scores, spoof_scores):
    from sklearn.metrics import roc_curve
    import numpy as np

    labels = np.concatenate([np.ones(len(bona_scores)), np.zeros(len(spoof_scores))])
    scores = np.concatenate([bona_scores, spoof_scores])
    fpr, tpr, thresholds = roc_curve(labels, scores)
    fnr = 1 - tpr
    idx = np.nanargmin(np.abs(fnr - fpr))
    return (fpr[idx] + fnr[idx]) / 2, thresholds[idx]
```
**Pad + score functions:** see "Golden-path setup" step 5 above (kept in one place to avoid duplication/drift).

**XTTS-v2 voice cloning (for generating more personal test clips):**
```python
!pip install coqui-tts --quiet
import os
os.environ["COQUI_TOS_AGREED"] = "1"
from TTS.api import TTS

tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
tts.tts_to_file(
    text="Your sentence here.",
    speaker_wav="<path to a 10-15 sec reference clip>",
    language="en",
    file_path="/kaggle/working/cloned_output.wav"
)
```

**ASVspoof 2021 DF loading recipe** (audio dataset: `mohammedabdeldayem/avsspoof-2021`, keys: extracted from `DF-keys-full.tar.gz` to `/kaggle/working/keys/DF/CM/`). Audio is split across multiple `ASVspoof2021_DF_eval_part{00,01,02,...}/ASVspoof2021_DF_eval/flac/` folders — build a filename→path index once rather than searching per-file. Labels come from `trial_metadata.txt`, whitespace-separated, columns are `speaker filename codec source attack_type label trim phase vocoder task team ...` (label at index 5, phase at index 7 — filter to `phase == "eval"`, not `"progress"`, for the official eval set):
```python
import glob, os

df21_flac_index = {}
for part_dir in glob.glob(".../ASVspoof2021_DF_eval_part*/ASVspoof2021_DF_eval/flac"):
    for fname in os.listdir(part_dir):
        df21_flac_index[fname.replace(".flac", "")] = os.path.join(part_dir, fname)

df21_bona, df21_spoof = [], []
with open("/kaggle/working/keys/DF/CM/trial_metadata.txt") as f:
    for line in f:
        parts = line.strip().split()
        filename, label, phase = parts[1], parts[5], parts[7]
        if phase != "eval":
            continue
        (df21_bona if label == "bonafide" else df21_spoof).append(filename)
```
Some sampled files may be missing from whichever DF eval parts you've attached — use a backfill-sampling pattern (try N candidates, top up from the remaining pool until the target count is reached) rather than failing on missing files.

## Stack reference (from original PS pitch, not all necessarily used)
Python, PyTorch, torchaudio, librosa, ONNX Runtime, FastAPI/gRPC, Redis/Kafka, PostgreSQL, React dashboard, Docker, ASVspoof baselines, EER/min t-DCF evaluation.
