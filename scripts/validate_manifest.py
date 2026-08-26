#!/usr/bin/env python3
"""Dataset manifest validator — the gate between a bad split and a meaningless evaluation number.

    python scripts/validate_manifest.py                          # default datasets/manifest/
    python scripts/validate_manifest.py path/to/manifest.csv
    python scripts/validate_manifest.py --json
    python scripts/validate_manifest.py --list-checks

Why this script is a CI check and not a code review item
--------------------------------------------------------
A split defect is invisible. A manifest that leaks the same speaker into ``train`` and
``eval_locked`` produces a *better* EER, no error, no warning, and no crash — the number simply stops
measuring generalization and starts measuring memorization. Nobody notices until an outside evaluator
runs the model on data the pipeline never touched, which for this project would be a judge. Every
check here exists to fail before that number is quoted (playbook section 2.2; rules.md R-37, R-38).

Checks are ERRORs (exit 1) or WARNINGs (exit 0, still printed). A WARNING is used only where the
source specification itself is conditional ("where feasible") or where a required grouping key is not
in the mandatory field list at all — see the two named conflicts below.

Two conflicts in the source documents, named rather than blended (rules.md R-54)
-------------------------------------------------------------------------------
**M-1 — ``dev`` versus ``dev_calibration``.** Playbook section 2.1 gives the ``split`` vocabulary as
``train``, ``dev``, ``eval_locked``, ``demo``. Section 2.2, two paragraphs later, defines the
partitions as ``train``, ``dev_calibration``, ``eval_locked`` plus three held-out suites and ``demo``.
A validator that accepted only one reading would reject a manifest built from the other page. This
tool accepts both and warns, because renaming a split after models are trained invalidates every
recorded split hash. **Required decision: Pair B names the canonical value and section 2.1 is
corrected.** Until then the warning stays.

**M-2 — session and text disjointness are mandated but not representable.** Section 2.2 requires
``train``/``dev_calibration``/``eval_locked`` to be disjoint by *session*, and ``train``/``eval_locked``
by *original text where known*. Neither ``session_id`` nor any text field appears in the section 2.1
mandatory field list. So the strongest form of the split protocol **cannot be verified from a
conforming manifest**. This tool checks both keys when optional columns supply them
(``session_id``, and one of ``text_hash``/``text_id``/``original_text_hash``) and otherwise reports
the gap in coverage instead of passing silently (rules.md R-52). **Required decision: add
``session_id`` to the mandatory list, or record that session disjointness is unverifiable.**

Exit codes: ``0`` valid (warnings allowed) · ``1`` invalid · ``2`` the check could not run.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, NoReturn, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DIR = Path("datasets/manifest")
DEFAULT_NAMES = ("manifest.parquet", "manifest.csv")

# ── The mandatory field list, playbook section 2.1 ─────────────────────────────────────────────────
# Comma-grouped rows in that table are expanded here: `language, script` is two columns, not one.
REQUIRED_FIELDS: tuple[str, ...] = (
    "sample_id",
    "split",
    "label",
    "source_dataset",
    "source_license",
    "speaker_id_hash",
    "language",
    "script",
    "accent_region",
    "generator_family",
    "generator_version",
    "attack_type",
    "capture_device",
    "codec",
    "sample_rate_hz",
    "channel_condition",
    "duration_ms",
    "sha256_audio",
    "consent_basis",
    "retention_expiry",
    "derived_from_sample_id",
)

#: Optional columns this tool uses when present. Their absence is conflict M-2, not an error.
SESSION_COLUMNS = ("session_id", "session_hash", "recording_session_id")
TEXT_COLUMNS = ("text_hash", "text_id", "original_text_hash", "prompt_hash")

#: Splits used for any form of fitting or tuning. `dev` is here because of conflict M-1.
TUNING_SPLITS = frozenset({"train", "dev", "dev_calibration"})
LOCKED_SPLIT = "eval_locked"

VALID_SPLITS = frozenset(
    {
        "train",
        "dev_calibration",
        "dev",  # conflict M-1
        "eval_locked",
        "eval_generator_heldout",
        "eval_codec_language_heldout",
        "demo",
    }
)
VALID_LABELS = frozenset({"bonafide", "spoof"})

#: Playbook section 2.1: `tts`, `vc`, `replay`, `codec_transcoded`. Bona-fide rows carry no attack.
VALID_ATTACK_TYPES = frozenset({"tts", "vc", "replay", "codec_transcoded"})

#: R-39: sampling rate is a *channel characteristic*, never spoof evidence. Both values are legitimate
#: deployment conditions, so this is a typo guard (`1600`, `160000`), not a quality filter.
VALID_SAMPLE_RATES = frozenset({8000, 16000})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EMPTY = frozenset({"", "none", "null", "nan", "na", "n/a", "-"})

# ── Forbidden columns, vocabulary borrowed from the audit deny-list ────────────────────────────────
# Imported rather than retyped for the same reason verify_audit_chain.py imports canonicalize: two
# copies of a deny-list drift, and the copy that drifts is the one nobody is testing. The manifest and
# the audit table have the same privacy boundary (rules.md R-14, R-15, R-21) — a manifest is allowed
# to carry hashes and IDs, never audio, a transcript, or a phone number.
sys.path.insert(0, str(REPO_ROOT / "gateway"))
try:
    from app.audit.chain import _FORBIDDEN_SUBSTRINGS as FORBIDDEN_SUBSTRINGS

    _VOCAB_SOURCE = "imported from gateway/app/audit/chain.py"
except ImportError:  # pragma: no cover - keeps the validator usable in a data-only checkout
    FORBIDDEN_SUBSTRINGS = (
        "audio",
        "pcm",
        "waveform",
        "transcript",
        "embedding",
        "phone",
        "msisdn",
        "caller_name",
    )
    _VOCAB_SOURCE = "local fallback (gateway/ not importable — verify it matches chain.py)"

#: `sha256_audio` is in the mandatory field list and contains "audio". It holds a hash, which is the
#: whole point of the manifest carrying hashes instead of paths, so it is an explicit exception.
#: Listing it here rather than loosening the substring match keeps `audio_path` a finding.
FORBIDDEN_EXCEPTIONS = frozenset({"sha256_audio"})

#: A path column is the specific failure R-21 exists to prevent: it puts the location of raw research
#: audio into a file that gets committed, and from there into every clone.
PATH_COLUMN_HINTS = ("path", "filepath", "file_path", "filename", "uri", "url", "s3_key", "location")


# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # "error" | "warning"
    message: str
    rows: tuple[int, ...] = ()

    def render(self) -> str:
        tag = "ERROR  " if self.severity == "error" else "WARNING"
        where = ""
        if self.rows:
            shown = ", ".join(str(r) for r in self.rows[:8])
            more = f" (+{len(self.rows) - 8} more)" if len(self.rows) > 8 else ""
            where = f"\n           rows: {shown}{more}"
        return f"  {tag} [{self.check}] {self.message}{where}"


@dataclass
class Report:
    path: str
    rows: int = 0
    findings: list[Finding] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)

    def error(self, check: str, message: str, rows: Iterable[int] = ()) -> None:
        self.findings.append(Finding(check, "error", message, tuple(sorted(rows))))

    def warn(self, check: str, message: str, rows: Iterable[int] = ()) -> None:
        self.findings.append(Finding(check, "warning", message, tuple(sorted(rows))))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


def _die(message: str) -> "NoReturn":
    """Exit 2, not 1.

    ``raise SystemExit("text")`` prints the text but exits **1**, which would make "there is no
    manifest" indistinguishable from "the manifest is invalid". The distinction is the point: a CI
    job that cannot find its input needs a different fix than one that found a bad split, and a
    reviewer reading a red check should not have to guess which happened.
    """
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _blank(value: Any) -> bool:
    return value is None or str(value).strip().lower() in _EMPTY


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# --------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------


def load_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(rows, column_order)``. CSV uses the stdlib; Parquet needs pyarrow or pandas.

    Both formats are permitted by playbook section 2.1 ("``manifest.parquet`` or ``manifest.csv``"),
    so refusing one would make the validator inapplicable to a conforming manifest. CSV is handled
    with the stdlib deliberately: this script must run in a bare CI job with no data-science stack,
    otherwise it gets skipped on the day it matters.
    """
    suffix = path.suffix.lower()

    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            columns = list(reader.fieldnames or [])
            return [dict(r) for r in reader], columns

    if suffix in (".json", ".jsonl"):
        if suffix == ".jsonl":
            rows = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
        else:
            payload = json.loads(path.read_text("utf-8"))
            rows = payload if isinstance(payload, list) else payload.get("samples", [])
        columns = list(rows[0].keys()) if rows else []
        return rows, columns

    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq  # noqa: PLC0415

            table = pq.read_table(path)
            return table.to_pylist(), list(table.column_names)
        except ImportError:
            pass
        try:
            import pandas as pd  # noqa: PLC0415

            frame = pd.read_parquet(path)
            return frame.to_dict("records"), list(frame.columns)
        except ImportError as exc:
            _die(
                "validate_manifest: FATAL: cannot read Parquet — neither pyarrow nor pandas is "
                "installed. Install pyarrow, or export the manifest to CSV and validate that. "
                "Exiting 2 rather than 0: an unreadable manifest is not a valid manifest."
            )

    _die(
        f"validate_manifest: FATAL: unsupported manifest format {path.suffix!r}. "
        "Expected .parquet or .csv (playbook section 2.1)."
    )


def resolve_path(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.exists():
            _die(f"validate_manifest: FATAL: {explicit} does not exist.")
        return explicit

    directory = REPO_ROOT / DEFAULT_DIR
    for name in DEFAULT_NAMES:
        candidate = directory / name
        if candidate.exists():
            return candidate

    _die(
        f"validate_manifest: FATAL: no manifest found in {DEFAULT_DIR}/ "
        f"(looked for {', '.join(DEFAULT_NAMES)}).\n"
        "Exiting 2, not 0. 'No manifest' must never read as 'manifest valid' — the Data gate "
        "(phases.md section 2) requires the manifest to exist before any evaluation number is "
        "quoted, and rules.md R-51 forbids a release without one."
    )


# --------------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------------


def check_columns(report: Report, columns: Sequence[str]) -> None:
    """D-01/D-02: the mandatory field list is present, and no column crosses the privacy boundary."""
    present = set(columns)
    missing = [f for f in REQUIRED_FIELDS if f not in present]
    if missing:
        report.error(
            "D-01",
            f"missing mandatory field(s): {', '.join(missing)}. Playbook section 2.1 makes each one "
            "mandatory for a reason a later reader cannot reconstruct: without `speaker_id_hash` "
            "there is no way to prove speaker disjointness, without `derived_from_sample_id` an "
            "augmented sample can silently land in a different split than its parent (rules.md "
            "R-38), and without `consent_basis`/`retention_expiry` the local set cannot pass the "
            "Data gate at all.",
        )

    for column in columns:
        lowered = column.lower()
        if lowered in FORBIDDEN_EXCEPTIONS:
            continue
        for token in FORBIDDEN_SUBSTRINGS:
            if token in lowered:
                report.error(
                    "D-02",
                    f"column {column!r} contains the forbidden substring {token!r}. The manifest "
                    "carries hashes and IDs; raw audio, transcripts, and caller identity must not "
                    "appear in a file that enters Git (rules.md R-14, R-15, R-21). Deny-list "
                    f"vocabulary {_VOCAB_SOURCE}.",
                )
                break
        else:
            if any(hint in lowered for hint in PATH_COLUMN_HINTS):
                report.error(
                    "D-02",
                    f"column {column!r} looks like a file path or URI. Playbook section 2.1: 'the "
                    "raw recording path stays in controlled research storage'. Committing the "
                    "location of consented audio is the leak R-21 exists to prevent, and a "
                    "`git rm` later does not remove it from history.",
                )

    for group, label in ((SESSION_COLUMNS, "session"), (TEXT_COLUMNS, "text")):
        if not present.intersection(group):
            report.coverage_notes.append(
                f"NOT CHECKED: {label} disjointness — no {' / '.join(group)} column exists. "
                "Playbook section 2.2 requires it; section 2.1 does not provide a field for it "
                "(conflict M-2). This scan cannot verify it."
            )


def check_identity(report: Report, rows: Sequence[dict[str, Any]]) -> None:
    """D-03: one row per file, with a stable key. Also catches the same audio entered twice."""
    seen_ids: dict[str, int] = {}
    dup_ids: list[int] = []
    blank_ids: list[int] = []
    for index, row in enumerate(rows, start=2):  # 2 = first data row of a CSV with a header
        sample_id = _text(row.get("sample_id"))
        if _blank(sample_id):
            blank_ids.append(index)
            continue
        if sample_id in seen_ids:
            dup_ids.append(index)
        else:
            seen_ids[sample_id] = index

    if blank_ids:
        report.error("D-03", "`sample_id` is empty. It is the stable immutable key.", blank_ids)
    if dup_ids:
        report.error(
            "D-03",
            "duplicate `sample_id`. Playbook section 2.1: every file appears exactly once. A "
            "duplicate row double-weights one sample in training and can place the same audio in "
            "two splits at once.",
            dup_ids,
        )

    by_hash: dict[str, list[tuple[int, str]]] = defaultdict(list)
    bad_hash: list[int] = []
    for index, row in enumerate(rows, start=2):
        digest = _text(row.get("sha256_audio")).lower()
        if _blank(digest):
            bad_hash.append(index)
            continue
        if not _HEX64.match(digest):
            bad_hash.append(index)
            continue
        by_hash[digest].append((index, _text(row.get("split"))))

    if bad_hash:
        report.error(
            "D-04",
            "`sha256_audio` is missing or is not 64 lowercase hex characters. It is the only "
            "deduplication and reproducibility handle the manifest has: without it, the same "
            "recording under two `sample_id`s is undetectable.",
            bad_hash,
        )

    for digest, entries in by_hash.items():
        if len(entries) < 2:
            continue
        splits = {split for _, split in entries}
        rownums = [index for index, _ in entries]
        if len(splits) > 1:
            report.error(
                "D-04",
                f"identical `sha256_audio` {digest[:12]}… appears in more than one split "
                f"({', '.join(sorted(splits))}). This is byte-identical audio on both sides of the "
                "split boundary — the evaluation number it produces measures memorization, not "
                "generalization (rules.md R-37).",
                rownums,
            )
        else:
            report.warn(
                "D-04",
                f"identical `sha256_audio` {digest[:12]}… on {len(entries)} rows in the same split. "
                "Duplicate audio inflates the effective weight of one sample.",
                rownums,
            )


def check_vocabularies(report: Report, rows: Sequence[dict[str, Any]]) -> None:
    """D-05: closed vocabularies. An unknown split value silently excludes rows from every check."""
    bad_split: list[int] = []
    dev_rows: list[int] = []
    bad_label: list[int] = []
    bad_attack: list[int] = []
    bad_rate: list[int] = []
    bad_duration: list[int] = []
    natural_spoof: list[int] = []

    for index, row in enumerate(rows, start=2):
        split = _text(row.get("split"))
        if split not in VALID_SPLITS:
            bad_split.append(index)
        if split == "dev":
            dev_rows.append(index)

        label = _text(row.get("label")).lower()
        if label not in VALID_LABELS:
            bad_label.append(index)

        attack = _text(row.get("attack_type")).lower()
        if label == "spoof":
            if attack and attack not in _EMPTY and attack not in VALID_ATTACK_TYPES:
                bad_attack.append(index)
        elif label == "bonafide" and attack and attack not in _EMPTY:
            # R-41: natural speech is never labelled spoof. A bona-fide row carrying an attack type
            # is the row most likely to be relabelled by a later script that trusts attack_type.
            natural_spoof.append(index)

        rate = _text(row.get("sample_rate_hz"))
        try:
            if int(float(rate)) not in VALID_SAMPLE_RATES:
                bad_rate.append(index)
        except (TypeError, ValueError):
            bad_rate.append(index)

        duration = _text(row.get("duration_ms"))
        try:
            if int(float(duration)) <= 0:
                bad_duration.append(index)
        except (TypeError, ValueError):
            bad_duration.append(index)

    if bad_split:
        report.error(
            "D-05",
            f"`split` is not one of {sorted(VALID_SPLITS)}. An unrecognized split value is worse "
            "than a missing one: every disjointness check below groups by split, so a typo like "
            "`eval-locked` creates a partition no check protects.",
            bad_split,
        )
    if dev_rows:
        report.warn(
            "M-1",
            f"{len(dev_rows)} row(s) use `split=dev`. Playbook section 2.1 lists `dev`; section 2.2 "
            "defines `dev_calibration`. Both are accepted here because renaming a split after "
            "training invalidates recorded split hashes. Pair B must name the canonical value "
            "(rules.md R-54).",
            dev_rows[:8],
        )
    if bad_label:
        report.error("D-05", f"`label` must be one of {sorted(VALID_LABELS)}.", bad_label)
    if bad_attack:
        report.error(
            "D-05",
            f"`attack_type` on a spoof row must be one of {sorted(VALID_ATTACK_TYPES)}. Failure "
            "analysis by attack class is what turns a bad number into a fixable finding.",
            bad_attack,
        )
    if natural_spoof:
        report.error(
            "D-05",
            "a `bonafide` row carries an `attack_type`. Natural speech is never spoof — accent, "
            "illness, emotion, and speaking style are not attacks (rules.md R-41). Either the label "
            "or the attack type is wrong, and guessing which is not this tool's job.",
            natural_spoof,
        )
    if bad_rate:
        report.error(
            "D-05",
            f"`sample_rate_hz` must be one of {sorted(VALID_SAMPLE_RATES)}. Both are legitimate "
            "deployment conditions — this is a typo guard, not a quality filter, because a sampling "
            "rate is a channel characteristic and never spoof evidence (rules.md R-39).",
            bad_rate,
        )
    if bad_duration:
        report.error("D-05", "`duration_ms` must be a positive integer.", bad_duration)


def check_consent(report: Report, rows: Sequence[dict[str, Any]]) -> None:
    """D-06: the Data-gate blocker. Missing consent is not a data-quality issue."""
    missing_basis: list[int] = []
    missing_expiry: list[int] = []
    unparseable: list[int] = []
    expired: list[tuple[int, str]] = []
    today = date.today()

    for index, row in enumerate(rows, start=2):
        if _blank(row.get("consent_basis")):
            missing_basis.append(index)

        raw = row.get("retention_expiry")
        if _blank(raw):
            missing_expiry.append(index)
            continue
        parsed = _parse_date(raw)
        if parsed is None:
            unparseable.append(index)
        elif parsed < today:
            expired.append((index, parsed.isoformat()))

    if missing_basis:
        report.error(
            "D-06",
            "`consent_basis` is empty. This is a Data-gate blocker, not a metadata gap (phases.md "
            "section 2; playbook section 6.1). A sample with no recorded lawful basis cannot be "
            "used for training or for the demo, and the team cannot answer 'why do you hold this "
            "recording' for it. rules.md R-21 ties deletion to the consent ledger — a row with no "
            "basis has nothing to delete against.",
            missing_basis,
        )
    if missing_expiry:
        report.error(
            "D-06",
            "`retention_expiry` is empty. Without a date, 'we delete consented audio on schedule' "
            "is a promise with no mechanism, and no process can ever determine that this sample is "
            "due for deletion (rules.md R-21).",
            missing_expiry,
        )
    if unparseable:
        report.error(
            "D-06",
            "`retention_expiry` is not an ISO-8601 date (YYYY-MM-DD or a full timestamp). A date a "
            "deletion job cannot parse is the same as no date.",
            unparseable,
        )
    if expired:
        report.error(
            "D-06",
            f"{len(expired)} sample(s) are past `retention_expiry` (earliest {min(d for _, d in expired)}) "
            "and are still listed in the manifest. Either the audio was deleted and the row is stale, "
            "or the retention commitment made to the speaker has been broken. Both need a human, and "
            "neither is resolved by extending the date.",
            [index for index, _ in expired],
        )


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _root_ancestor(sample_id: str, parents: dict[str, str], *, limit: int = 64) -> tuple[str, bool]:
    """Walk `derived_from_sample_id` to the original recording. Returns ``(root, cycle_detected)``."""
    seen = {sample_id}
    current = sample_id
    for _ in range(limit):
        parent = parents.get(current)
        if parent is None or _blank(parent):
            return current, False
        if parent in seen:
            return current, True
        seen.add(parent)
        current = parent
    return current, True


def check_grouping(report: Report, rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    """D-07 … D-10: the split protocol. This is the reason the file exists.

    Playbook section 2.2 requires grouping **before** any augmentation runs. A validator cannot see
    when grouping happened, but it can detect every observable consequence of grouping too late: a
    derived sample in a different split than its parent, a speaker on both sides, a generator family
    that leaked into the locked set. Each of those is what "grouped after augmenting" looks like from
    the outside (rules.md R-38).
    """
    by_id = {_text(r.get("sample_id")): r for r in rows if not _blank(r.get("sample_id"))}
    parents = {
        _text(r.get("sample_id")): _text(r.get("derived_from_sample_id"))
        for r in rows
        if not _blank(r.get("sample_id"))
    }
    split_of = {sid: _text(row.get("split")) for sid, row in by_id.items()}

    # D-07 — lineage. Every sample sharing a root must share a split.
    dangling: list[int] = []
    cycles: list[int] = []
    lineage_groups: dict[str, set[str]] = defaultdict(set)
    for index, row in enumerate(rows, start=2):
        sample_id = _text(row.get("sample_id"))
        parent = _text(row.get("derived_from_sample_id"))
        if not _blank(parent) and parent not in by_id:
            dangling.append(index)
        if not sample_id:
            continue
        root, cycle = _root_ancestor(sample_id, parents)
        if cycle:
            cycles.append(index)
        lineage_groups[root].add(sample_id)

    if dangling:
        report.error(
            "D-07",
            "`derived_from_sample_id` names a `sample_id` that is not in this manifest. The parent's "
            "split is then unknowable, so the derived sample's split cannot be checked at all "
            "(rules.md R-38). An augmentation whose parent is absent is an augmentation nobody can "
            "place.",
            dangling,
        )
    if cycles:
        report.error("D-07", "`derived_from_sample_id` forms a cycle in the lineage graph.", cycles)

    for root, members in lineage_groups.items():
        splits = {split_of.get(m, "") for m in members}
        if len(splits) > 1:
            rownums = [
                index
                for index, row in enumerate(rows, start=2)
                if _text(row.get("sample_id")) in members
            ]
            report.error(
                "D-07",
                f"lineage rooted at {root!r} spans splits {sorted(s for s in splits if s)}. A "
                "derived sample inherits its parent's split (rules.md R-38). When it does not, an "
                "augmented copy of a training recording is sitting in the evaluation set: the model "
                "has heard that audio, and the resulting score is not an out-of-sample measurement. "
                "This is the exact defect that 'group before you augment' prevents, and it is "
                "invisible in every metric.",
                rownums,
            )

    # D-08 — speaker disjointness across the three fitting/reporting partitions.
    _check_key_disjoint(
        report,
        rows,
        key="speaker_id_hash",
        check="D-08",
        groups=(TUNING_SPLITS, {LOCKED_SPLIT}),
        rationale=(
            "the same speaker is in a tuning split and in `eval_locked`. Speaker-dependent cues "
            "(voice, room, handset) then carry across the boundary and the locked evaluation "
            "measures speaker recall rather than spoof detection (playbook section 2.2)."
        ),
    )

    # D-09 — generator family/version must not cross into eval_locked.
    _check_key_disjoint(
        report,
        rows,
        key=("generator_family", "generator_version"),
        check="D-09",
        groups=(TUNING_SPLITS, {LOCKED_SPLIT}),
        rationale=(
            "a generator family+version appears in both a tuning split and `eval_locked`. The "
            "locked set must be disjoint by generator family/version, otherwise the reported number "
            "describes performance against a *known* synthesizer and says nothing about the "
            "future-TTS case the product actually faces (playbook section 2.2)."
        ),
    )

    heldout = {
        _generator_key(row)
        for row in rows
        if _text(row.get("split")) == "eval_generator_heldout" and _generator_key(row)
    }
    if heldout:
        offenders = [
            index
            for index, row in enumerate(rows, start=2)
            if _text(row.get("split")) in TUNING_SPLITS and _generator_key(row) in heldout
        ]
        if offenders:
            report.error(
                "D-09",
                "a generator family/version present in `eval_generator_heldout` also appears in a "
                "tuning split. `eval_generator_heldout` must hold out the *entire* family+version "
                "(playbook section 2.2); if the model trained on it, the future-TTS simulation "
                "simulates nothing.",
                offenders,
            )

    # D-10 — session and text, where the optional columns exist (conflict M-2).
    present = set(columns)
    session_col = next((c for c in SESSION_COLUMNS if c in present), None)
    if session_col:
        _check_key_disjoint(
            report,
            rows,
            key=session_col,
            check="D-10",
            groups=(TUNING_SPLITS, {LOCKED_SPLIT}),
            rationale=(
                "a recording session appears in both a tuning split and `eval_locked`. One session "
                "shares handset, room, and network path across all its samples, so session leakage "
                "leaks channel identity even when speakers are disjoint (playbook section 2.2)."
            ),
        )
    text_col = next((c for c in TEXT_COLUMNS if c in present), None)
    if text_col:
        _check_key_disjoint(
            report,
            rows,
            key=text_col,
            check="D-10",
            groups=({"train"}, {LOCKED_SPLIT}),
            rationale=(
                "the same spoken text appears in `train` and `eval_locked`. Section 2.2 qualifies "
                "text disjointness as 'where feasible', so this is reported as a warning — but a "
                "shared prompt set is a real shortcut for a model to exploit."
            ),
            severity="warning",
        )


def _generator_key(row: dict[str, Any]) -> str:
    family = _text(row.get("generator_family"))
    version = _text(row.get("generator_version"))
    if _blank(family):
        return ""  # bona-fide rows have no generator; grouping on "" would merge all of them
    return f"{family}@{version}"


def _check_key_disjoint(
    report: Report,
    rows: Sequence[dict[str, Any]],
    *,
    key: str | tuple[str, ...],
    check: str,
    groups: tuple[Iterable[str], Iterable[str]],
    rationale: str,
    severity: str = "error",
) -> None:
    """Assert no value of ``key`` appears in both split groups."""
    left, right = frozenset(groups[0]), frozenset(groups[1])

    def value_of(row: dict[str, Any]) -> str:
        if isinstance(key, tuple):
            return _generator_key(row)
        return _text(row.get(key))

    left_values: dict[str, list[int]] = defaultdict(list)
    right_values: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows, start=2):
        value = value_of(row)
        if _blank(value):
            continue
        split = _text(row.get("split"))
        if split in left:
            left_values[value].append(index)
        elif split in right:
            right_values[value].append(index)

    shared = sorted(set(left_values) & set(right_values))
    if not shared:
        return

    rownums = [i for v in shared for i in left_values[v] + right_values[v]]
    label = "+".join(key) if isinstance(key, tuple) else key
    sample = ", ".join(repr(v[:24]) for v in shared[:4])
    more = f" (+{len(shared) - 4} more)" if len(shared) > 4 else ""
    # ASCII "<->" rather than an arrow glyph: this string is printed, and see _safe_streams() below.
    message = (
        f"{len(shared)} value(s) of `{label}` cross the "
        f"{'/'.join(sorted(left))} <-> {'/'.join(sorted(right))} boundary: {sample}{more}. {rationale}"
    )
    (report.error if severity == "error" else report.warn)(check, message, rownums)


def check_population(report: Report, rows: Sequence[dict[str, Any]]) -> None:
    """D-11: a partition that is empty passes every disjointness check trivially."""
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[_text(row.get("split"))] += 1

    if not rows:
        report.error(
            "D-11",
            "the manifest has zero rows. Every check below passes on an empty table, which is why "
            "this is an error and not a note (rules.md R-52).",
        )
        return

    if counts.get(LOCKED_SPLIT, 0) == 0:
        report.warn(
            "D-11",
            "`eval_locked` is empty. Every locked-set disjointness check therefore passed "
            "vacuously. No final evaluation number can be produced from this manifest (rules.md "
            "R-37).",
        )
    for split in ("train", "dev_calibration"):
        if counts.get(split, 0) == 0 and not (split == "dev_calibration" and counts.get("dev")):
            report.warn("D-11", f"`{split}` is empty.")

    labels: dict[str, int] = defaultdict(int)
    for row in rows:
        labels[_text(row.get("label")).lower()] += 1
    if labels.get("bonafide", 0) == 0 or labels.get("spoof", 0) == 0:
        report.warn(
            "D-11",
            f"one class is absent (bonafide={labels.get('bonafide', 0)}, "
            f"spoof={labels.get('spoof', 0)}). A single-class manifest trains nothing and every "
            "metric computed from it is undefined or trivially perfect.",
        )

    report.coverage_notes.append(
        "split populations: " + ", ".join(f"{k or '<blank>'}={v}" for k, v in sorted(counts.items()))
    )


# --------------------------------------------------------------------------------------------------


def validate(path: Path) -> Report:
    rows, columns = load_rows(path)
    try:
        rel = str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        rel = str(path)
    report = Report(path=rel, rows=len(rows))

    check_columns(report, columns)
    check_population(report, rows)
    check_identity(report, rows)
    check_vocabularies(report, rows)
    check_consent(report, rows)
    check_grouping(report, rows, columns)
    return report


CHECKS_DOC = """\
validate_manifest.py — checks

  D-01  every mandatory field from playbook section 2.1 is present
  D-02  no column crosses the privacy boundary (audit deny-list vocabulary + path/URI columns)
  D-03  `sample_id` present and unique — one row per file
  D-04  `sha256_audio` is 64 hex chars; identical audio never spans two splits
  D-05  closed vocabularies: split, label, attack_type, sample_rate_hz, duration_ms
  D-06  `consent_basis` and `retention_expiry` present, parseable, and not already expired
  D-07  lineage: a derived sample shares its parent's split (R-38); no dangling parents or cycles
  D-08  speaker disjointness between tuning splits and `eval_locked`
  D-09  generator family+version disjointness into `eval_locked`; `eval_generator_heldout` held out
  D-10  session and text disjointness — only when an optional column supplies the key (conflict M-2)
  D-11  no partition or class is silently empty

Named source conflicts, reported not blended (rules.md R-54)
  M-1   `dev` (section 2.1) vs `dev_calibration` (section 2.2) — both accepted, warned on
  M-2   session/text disjointness is mandated by section 2.2 but has no field in section 2.1

NOT covered by this tool (rules.md R-52 — say what you dropped)
  * Whether grouping actually ran *before* augmentation. Only its observable consequences are
    checked (D-07, D-08, D-09). A pipeline that grouped late but happened to produce a consistent
    manifest passes.
  * Whether `sha256_audio` matches the bytes of any real file. The audio is not in this repository
    by design (R-21), so the hash cannot be recomputed here.
  * Whether `consent_basis` corresponds to a real signed consent record. That lives in the consent
    ledger; this tool only proves the manifest claims one.
  * Whether `accent_region` and `language` are correct. Never inferred from audio (playbook 2.1).
  * `speaker_id_hash` collisions across `source_dataset` namespaces.
  * Parquet reading is exercised only when pyarrow or pandas is installed.
"""


def _safe_streams() -> None:
    """Make stdout/stderr unable to turn a finding into a traceback.

    Found the hard way: a `↔` in one finding message raised ``UnicodeEncodeError`` on a cp1252
    console *while printing an error list*, so a manifest with eleven real defects presented as a
    crash in the validator. In CI that reads as "the tool is broken", which is the one failure mode a
    gate must not have — a broken gate gets bypassed, a failing gate gets fixed. The offending
    character is now ASCII, but this guard is what keeps the property true after the next edit.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # detached or non-reconfigurable stream
                pass


def main(argv: Sequence[str] | None = None) -> int:
    _safe_streams()
    parser = argparse.ArgumentParser(
        description="Validate a dataset manifest against the playbook field list and split protocol.",
        epilog="Exit 0 valid (warnings allowed), 1 invalid, 2 could not run.",
    )
    parser.add_argument("manifest", nargs="?", type=Path, help=f"default: {DEFAULT_DIR}/manifest.*")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as errors (use once M-1/M-2 are closed)"
    )
    parser.add_argument("--list-checks", action="store_true", help="describe checks and known gaps")
    args = parser.parse_args(argv)

    if args.list_checks:
        print(CHECKS_DOC)
        return 0

    report = validate(resolve_path(args.manifest))
    failed = bool(report.errors) or (args.strict and bool(report.warnings))

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "manifest": report.path,
                    "rows": report.rows,
                    "errors": [
                        {"check": f.check, "message": f.message, "rows": list(f.rows)}
                        for f in report.errors
                    ],
                    "warnings": [
                        {"check": f.check, "message": f.message, "rows": list(f.rows)}
                        for f in report.warnings
                    ],
                    "coverage_notes": report.coverage_notes,
                },
                indent=2,
            )
        )
        return 1 if failed else 0

    print(f"validate_manifest: {report.path} — {report.rows} row(s)")
    for note in report.coverage_notes:
        print(f"  note: {note}")
    if report.findings:
        print("")
        for finding in report.errors:
            print(finding.render())
        for finding in report.warnings:
            print(finding.render())

    print("")
    if failed:
        print(
            f"validate_manifest: FAIL — {len(report.errors)} error(s), "
            f"{len(report.warnings)} warning(s)."
        )
        print(
            "  A split or consent defect does not surface as a crash. It surfaces as an evaluation\n"
            "  number that is quietly too good, months later, in front of someone who did not build\n"
            "  the pipeline. Fix the manifest, then re-record the split hash."
        )
        return 1

    print(
        f"validate_manifest: PASS — {len(report.warnings)} warning(s). "
        "Field list, vocabularies, consent fields, and split disjointness all hold."
    )
    print("  Run with --list-checks to see what this tool does NOT verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
