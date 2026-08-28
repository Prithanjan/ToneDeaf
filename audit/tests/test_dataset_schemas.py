"""Structural tests for ``datasets/manifest/`` and ``datasets/consent_ledger/``.

WHY THESE TESTS EXIST AT ALL. A JSON Schema is a claim about what is impossible, and a schema
nobody exercises is a claim nobody checked. The two failure modes are both quiet:

  * The schema is *permissive* where it looks strict — a mistyped keyword (``minimum`` inside
    ``items`` instead of on the value, ``additionalProperties`` misspelled) silently accepts
    everything. Nothing errors; validation just stops meaning anything.
  * The *example* drifts from the schema. The example is what everyone copies, so an example that
    no longer validates propagates an invalid shape into every real manifest written from it.

So each schema is exercised in both directions: the committed example must be ACCEPTED, and a
list of specific, realistic corruptions must be REJECTED, each naming the leak or the privacy
failure it represents.

NO VALIDATION LIBRARY IS AVAILABLE HERE. There is no network in this environment and
``jsonschema`` is not installed, so a small validator covering exactly the keyword subset these
two schemas use lives in this file (``validate``). That is a real cost: the subset validator is
not the library, and a keyword the schemas use that it does not implement would be silently
skipped. ``test_the_subset_validator_covers_every_keyword_the_schemas_use`` closes that hole by
walking both schemas and failing on any keyword outside the implemented set — so the validator
cannot quietly under-check as the schemas grow. ``TestWithTheRealLibrary`` re-runs the same
acceptance checks under ``jsonschema`` when it is present; it SKIPS here and those runs are
therefore unverified.

rules.md R-14/R-15/R-16 (no raw audio, no identifiers), R-37 (calibration split discipline),
R-38 (grouping before augmentation); PS104_AI_Training_and_Evaluation_Playbook.md 2.1 (mandatory
manifest fields) and 2.2 (split disjointness).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_SCHEMA_PATH = REPO_ROOT / "datasets" / "manifest" / "manifest.schema.json"
MANIFEST_EXAMPLE_PATH = REPO_ROOT / "datasets" / "manifest" / "example.manifest.json"
LEDGER_SCHEMA_PATH = (
    REPO_ROOT / "datasets" / "consent_ledger" / "consent_ledger.schema.json"
)
LEDGER_EXAMPLE_PATH = (
    REPO_ROOT / "datasets" / "consent_ledger" / "example.consent_ledger.json"
)

sys.path.insert(0, str(REPO_ROOT / "audit" / "migrations"))
import schema_contract as sc  # noqa: E402


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


MANIFEST_SCHEMA = load(MANIFEST_SCHEMA_PATH)
MANIFEST_EXAMPLE = load(MANIFEST_EXAMPLE_PATH)
LEDGER_SCHEMA = load(LEDGER_SCHEMA_PATH)
LEDGER_EXAMPLE = load(LEDGER_EXAMPLE_PATH)


# ==================================================================================================
# A JSON Schema validator covering exactly the subset these two schemas use
# ==================================================================================================
# Annotation-only keywords. Listed explicitly rather than "anything unrecognised is ignored",
# because ignoring the unrecognised is precisely how a real constraint gets dropped.
ANNOTATIONS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "$comment",
        "$defs",
        "format",
        "default",
        "examples",
    }
)
ASSERTIONS = frozenset(
    {
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "contains",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "$ref",
    }
)

TYPE_MAP: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, name: str) -> bool:
    if name == "integer":
        # bool is a subclass of int in Python; JSON Schema does not consider true an integer.
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    expected = TYPE_MAP[name]
    if expected is dict or expected is list:
        return isinstance(value, expected)
    if expected is str:
        return isinstance(value, str)
    return isinstance(value, expected)


def _resolve(root: dict, ref: str) -> dict:
    assert ref.startswith("#/"), f"only local refs are supported, got {ref!r}"
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(
    instance: Any, schema: dict, root: dict | None = None, path: str = "$"
) -> list[str]:
    """Return a list of human-readable errors. Empty list means valid.

    Errors are returned rather than raised so a negative test can assert *which* rule fired,
    not merely that something did. A negative test that passes for the wrong reason is worse
    than no test: it reports coverage of a constraint that is not actually enforced.
    """
    root = schema if root is None else root
    errors: list[str] = []

    if "$ref" in schema:
        errors += validate(instance, _resolve(root, schema["$ref"]), root, path)

    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(instance, n) for n in names):
            errors.append(f"{path}: type {names} violated by {type(instance).__name__}")
            return errors  # further keywords would produce noise against the wrong type

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: const {schema['const']!r} violated by {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: enum violated by {instance!r}")

    if isinstance(instance, str):
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(
                f"{path}: pattern {schema['pattern']!r} violated by {instance!r}"
            )
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} above maximum {schema['maximum']}")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: required property {key!r} is missing")
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors += validate(value, props[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: additional property {key!r} is not permitted")

    if isinstance(instance, list):
        if "items" in schema:
            for i, item in enumerate(instance):
                errors += validate(item, schema["items"], root, f"{path}[{i}]")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        if schema.get("uniqueItems") and len(instance) != len(
            {json.dumps(i, sort_keys=True) for i in instance}
        ):
            errors.append(f"{path}: uniqueItems violated")
        if "contains" in schema and not any(
            not validate(item, schema["contains"], root, path) for item in instance
        ):
            errors.append(f"{path}: contains violated")

    for sub in schema.get("allOf", []):
        errors += validate(instance, sub, root, path)
    if "anyOf" in schema and not any(
        not validate(instance, sub, root, path) for sub in schema["anyOf"]
    ):
        errors.append(f"{path}: anyOf violated")
    if "oneOf" in schema:
        matched = sum(
            1 for sub in schema["oneOf"] if not validate(instance, sub, root, path)
        )
        if matched != 1:
            errors.append(f"{path}: oneOf matched {matched} subschemas")
    if "not" in schema and not validate(instance, schema["not"], root, path):
        errors.append(f"{path}: not violated")

    if "if" in schema:
        branch = "then" if not validate(instance, schema["if"], root, path) else "else"
        if branch in schema:
            errors += validate(instance, schema[branch], root, path)

    return errors


def keywords_in(schema: Any) -> set[str]:
    """Every mapping key that appears in a schema position, for the coverage self-check."""
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            found.add(key)
            if key == "properties" or key == "$defs":
                for sub in value.values():
                    found |= keywords_in(sub)
            elif key in {"required", "enum", "const", "type"}:
                continue
            else:
                found |= keywords_in(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= keywords_in(item)
    return found


def property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names |= set(value.keys())
                for sub in value.values():
                    names |= property_names(sub)
            else:
                names |= property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= property_names(item)
    return names


def manifest_records() -> list[dict]:
    return MANIFEST_EXAMPLE["records"]


def grouping_key(record: dict) -> str:
    """Recompute grouping_key_sha256 the way scripts/validate_manifest.py must.

    Duplicated here deliberately: if this recomputation lived in a helper the writer also used,
    the test would confirm the two agree and prove nothing about either.
    """
    grouping = record["grouping"]
    parts = [
        record["speaker_id_hash"] if key == "speaker_id_hash" else grouping[key]
        for key in grouping["group_by"]
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# ==================================================================================================
class TestTheValidatorItself:
    """If the validator under-checks, every acceptance test below is vacuous."""

    def test_the_subset_validator_covers_every_keyword_the_schemas_use(self) -> None:
        implemented = ANNOTATIONS | ASSERTIONS
        for name, schema in (("manifest", MANIFEST_SCHEMA), ("ledger", LEDGER_SCHEMA)):
            unknown = keywords_in(schema) - implemented - property_names(schema)
            assert not unknown, (
                f"{name}.schema.json uses {sorted(unknown)}, which validate() does not implement. "
                "An unimplemented assertion keyword is silently skipped, so the schema would look "
                "enforced and not be. Implement it here or remove it from the schema."
            )

    def test_the_validator_rejects_what_it_should(self) -> None:
        # Teeth check. Every negative test below relies on validate() actually failing things.
        schema = {
            "type": "object",
            "required": ["a"],
            "additionalProperties": False,
            "properties": {"a": {"type": "integer", "minimum": 2}},
        }
        assert validate({"a": 2}, schema) == []
        assert validate({}, schema)
        assert validate({"a": 1}, schema)
        assert validate({"a": 2, "b": 1}, schema)
        assert validate({"a": True}, schema), "bool must not satisfy type: integer"


# ==================================================================================================
class TestManifestSchemaAcceptsTheExample:
    def test_the_committed_example_validates(self) -> None:
        errors = validate(MANIFEST_EXAMPLE, MANIFEST_SCHEMA)
        assert errors == [], (
            "the example everyone copies does not validate: " + "; ".join(errors)
        )

    def test_every_ref_resolves(self) -> None:
        for ref in re.findall(
            r'"\$ref":\s*"([^"]+)"', MANIFEST_SCHEMA_PATH.read_text("utf-8")
        ):
            _resolve(MANIFEST_SCHEMA, ref)  # raises KeyError if dangling

    def test_the_example_exercises_more_than_one_split(self) -> None:
        # An example with a single split cannot demonstrate the disjointness invariant, which is
        # the only interesting property of this manifest.
        assert len({r["split"] for r in manifest_records()}) >= 3

    def test_the_example_exercises_a_two_step_augmentation_chain(self) -> None:
        depths = {r["grouping"]["augmentation_depth"] for r in manifest_records()}
        assert {0, 1, 2} <= depths, (
            "the example must contain a depth-2 record: grouping on the immediate parent only "
            "breaks at depth 2, so an example that stops at depth 1 cannot show the bug"
        )


# ==================================================================================================
class TestPlaybookMandatoryFields:
    """Playbook 2.1 lists the fields a manifest must carry. Missing one is not recoverable later."""

    MANDATORY = (
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

    def test_every_mandatory_field_is_a_required_property(self) -> None:
        record = MANIFEST_SCHEMA["$defs"]["record"]
        for field in self.MANDATORY:
            assert field in record["properties"], f"{field} is not in the schema"
            assert field in record["required"], (
                f"{field} is declared but optional. An optional provenance field is an absent "
                "provenance field on the one record where it mattered."
            )

    def test_the_six_playbook_splits_are_the_only_permitted_values(self) -> None:
        assert MANIFEST_SCHEMA["$defs"]["record"]["properties"]["split"]["enum"] == [
            "train",
            "dev_calibration",
            "eval_locked",
            "eval_generator_heldout",
            "eval_codec_language_heldout",
            "demo",
        ]

    def test_dev_calibration_and_eval_locked_are_distinct_splits(self) -> None:
        # R-37 is unenforceable if the two share a name. Stated as a test because the whole
        # calibration discipline rests on the manifest being able to tell them apart.
        splits = MANIFEST_SCHEMA["$defs"]["record"]["properties"]["split"]["enum"]
        assert "dev_calibration" in splits and "eval_locked" in splits


# ==================================================================================================
class TestNoRawAudioAnywhereInTheManifest:
    """R-14/R-15: the manifest identifies audio by digest and never locates it."""

    # DOCUMENTED EXCEPTIONS, each keyed to the ONE substring it excuses, so an exception cannot
    # accidentally cover a second, real hit that appears in the same name later.
    #
    #   sha256_audio -> "audio": mandated verbatim by playbook 2.1 and constrained to
    #       ^[0-9a-f]{64}$. A 64-hex digest cannot hold a waveform or a path, and identifying
    #       content by digest is the mechanism that makes a path unnecessary.
    #
    #   withdrawal / withdrawn / withdrawn_at -> "raw": a genuine COLLISION, not a concession.
    #       "withd-raw-al" contains the forbidden substring "raw". The deny-list is substring-based
    #       on purpose (an exact-name list is one a rename walks around, see
    #       schema_contract.forbidden_substring_hits), and the price of that is English words that
    #       happen to contain "raw". Renaming the field to dodge the matcher would be worse: the
    #       right-to-withdraw block is the one part of the consent ledger that must be easy to find.
    #       Worth noting for whoever owns gateway/app/audit/chain.py: the same collision means an
    #       audit column could never be called withdrawal_reason either.
    SUBSTRING_EXCEPTIONS: dict[str, str] = {
        "sha256_audio": "audio",
        "withdrawal": "raw",
        "withdrawn": "raw",
        "withdrawn_at": "raw",
    }

    def test_no_property_name_trips_the_audit_deny_list(self) -> None:
        for schema, label in ((MANIFEST_SCHEMA, "manifest"), (LEDGER_SCHEMA, "ledger")):
            hits = [
                (name, bad)
                for name, bad in sc.forbidden_substring_hits(
                    sorted(property_names(schema))
                )
                if self.SUBSTRING_EXCEPTIONS.get(name) != bad
            ]
            assert hits == [], (
                f"{label}.schema.json property names trip the 5.2 deny-list: {hits}. The audit "
                "table and these files share one vocabulary of forbidden field names so that a "
                "reviewer does not have to remember two lists."
            )

    def test_every_documented_exception_is_still_needed(self) -> None:
        # An exception list nobody prunes eventually excuses a name that no longer exists, and the
        # next reader assumes the collision it describes is still real.
        declared = set(property_names(MANIFEST_SCHEMA)) | set(
            property_names(LEDGER_SCHEMA)
        )
        for name, substring in self.SUBSTRING_EXCEPTIONS.items():
            assert (
                name in declared
            ), f"{name!r} is excused but no longer a property anywhere"
            hits = dict(sc.forbidden_substring_hits([name]))
            assert hits.get(name) == substring, (
                f"{name!r} no longer trips {substring!r}; delete the exception rather than "
                "leaving a note about a collision that has gone away"
            )

    @pytest.mark.privacy
    def test_no_property_name_locates_a_file(self) -> None:
        # Separate from the substring deny-list: `recording_uri` trips no forbidden substring and
        # is exactly the field this design refuses to have.
        locators = (
            "path",
            "uri",
            "url",
            "filename",
            "filepath",
            "blob",
            "bytes",
            "location",
        )
        for schema, label in ((MANIFEST_SCHEMA, "manifest"), (LEDGER_SCHEMA, "ledger")):
            for name in sorted(property_names(schema)):
                lowered = name.lower()
                assert not any(loc in lowered for loc in locators), (
                    f"{label}.schema.json declares {name!r}. Playbook 2.1 keeps the recording "
                    "path in controlled research storage; a manifest that carries one is copied "
                    "everywhere the manifest is copied."
                )

    @pytest.mark.privacy
    def test_the_record_property_set_is_closed(self) -> None:
        # The deny-list catches names somebody thought of. additionalProperties: false catches the
        # ones nobody did.
        for node in (
            MANIFEST_SCHEMA,
            MANIFEST_SCHEMA["$defs"]["record"],
            MANIFEST_SCHEMA["$defs"]["grouping"],
            LEDGER_SCHEMA,
            LEDGER_SCHEMA["$defs"]["record"],
        ):
            assert node.get("additionalProperties") is False

    @pytest.mark.privacy
    def test_an_added_audio_path_is_rejected(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        doc["records"][0]["audio_path"] = "/data/raw/0001.wav"
        errors = validate(doc, MANIFEST_SCHEMA)
        assert any("audio_path" in e for e in errors), (
            "a manifest carrying a waveform path validated. This is the R-14 boundary: once a "
            "path is in the manifest, every consumer of the manifest can reach the audio."
        )

    @pytest.mark.privacy
    def test_a_free_text_field_cannot_hold_a_transcript(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        doc["records"][0]["notes"] = "x" * 4096
        assert any("maxLength" in e for e in validate(doc, MANIFEST_SCHEMA)), (
            "notes is unbounded. An unbounded string on a per-speaker record is where a "
            "transcript or a name ends up, written by somebody being helpful."
        )


# ==================================================================================================
class TestGroupingIsExplicitNotImplied:
    """The reason this schema exists rather than a README paragraph (R-38, playbook 2.2)."""

    def test_the_grouping_block_is_required_on_every_record(self) -> None:
        assert "grouping" in MANIFEST_SCHEMA["$defs"]["record"]["required"]

    def test_the_grouping_keys_are_pinned_as_a_const_not_merely_described(self) -> None:
        group_by = MANIFEST_SCHEMA["$defs"]["grouping"]["properties"]["group_by"]
        assert group_by["const"] == [
            "speaker_id_hash",
            "root_sample_id",
            "generator_group_id",
        ], (
            "group_by must be a const. A free array would let a splitter that grouped on speaker "
            "alone produce a manifest that validates, which is the failure this pins shut."
        )

    def test_grouping_before_augmentation_has_no_false_value(self) -> None:
        field = MANIFEST_SCHEMA["$defs"]["grouping"]["properties"][
            "grouped_before_augmentation"
        ]
        assert field.get("const") is True
        assert "enum" not in field and field.get("type") != "boolean", (
            "declared as a boolean, this becomes a flag somebody can set to false and still "
            "validate. As a const there is no valid manifest that admits grouping afterwards."
        )

    def test_a_manifest_that_grouped_after_augmentation_cannot_validate(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        doc["records"][0]["grouping"]["grouped_before_augmentation"] = False
        assert any(
            "grouped_before_augmentation" in e for e in validate(doc, MANIFEST_SCHEMA)
        )

    def test_dropping_the_generator_key_from_group_by_is_rejected(self) -> None:
        # The concrete leak: hold out a generator family, but group only on speaker, and the same
        # generator's output appears in train and in eval_generator_heldout.
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        doc["records"][0]["grouping"]["group_by"] = [
            "speaker_id_hash",
            "root_sample_id",
        ]
        assert any("group_by" in e for e in validate(doc, MANIFEST_SCHEMA))

    def test_an_augmented_record_cannot_claim_depth_zero(self) -> None:
        # Depth 0 with a parent means "grouped as an original", i.e. grouped into its own group,
        # i.e. splittable away from the audio it was derived from.
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        record = next(r for r in doc["records"] if r["derived_from_sample_id"])
        record["grouping"]["augmentation_depth"] = 0
        assert validate(doc, MANIFEST_SCHEMA) != []

    def test_an_original_cannot_name_a_parent(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        record = next(
            r for r in doc["records"] if r["grouping"]["augmentation_depth"] == 0
        )
        record["derived_from_sample_id"] = "example-bonafide-original-0001"
        assert validate(doc, MANIFEST_SCHEMA) != []

    def test_the_root_is_the_root_and_not_the_immediate_parent(self) -> None:
        # THE ARGUMENT, checked against the example rather than asserted in prose. In the chain
        # A -> B -> C, C's root must be A. Grouping on derived_from_sample_id would give C's group
        # as B and A's group as A, and A and C could then be split apart.
        by_id = {r["sample_id"]: r for r in manifest_records()}
        chained = [
            r for r in manifest_records() if r["grouping"]["augmentation_depth"] >= 2
        ]
        assert chained, "the example must contain a depth>=2 record"
        for record in chained:
            walker = record
            while walker["derived_from_sample_id"] is not None:
                walker = by_id[walker["derived_from_sample_id"]]
            assert record["grouping"]["root_sample_id"] == walker["sample_id"]
            assert (
                record["grouping"]["root_sample_id"] != record["derived_from_sample_id"]
            ), (
                "at depth >= 2 the root and the immediate parent differ; that difference is the "
                "entire reason root_sample_id exists"
            )

    def test_an_original_is_its_own_root(self) -> None:
        for record in manifest_records():
            if record["grouping"]["augmentation_depth"] == 0:
                assert record["grouping"]["root_sample_id"] == record["sample_id"]

    def test_the_grouping_key_is_reproducible_from_its_declared_inputs(self) -> None:
        # If grouping_key_sha256 is not recomputable, it is an opaque token and the disjointness
        # check cannot be re-derived by a reviewer — it can only be taken on trust.
        for record in manifest_records():
            assert record["grouping"]["grouping_key_sha256"] == grouping_key(record), (
                f"{record['sample_id']}: declared grouping key does not match sha256 of its own "
                "group_by values joined by 0x1f. scripts/validate_manifest.py must reject this."
            )

    def test_a_derivation_chain_shares_one_grouping_key(self) -> None:
        keys = {
            r["grouping"]["grouping_key_sha256"]
            for r in manifest_records()
            if r["grouping"]["root_sample_id"] == "example-bonafide-original-0001"
        }
        assert len(keys) == 1, (
            "the three records of one augmentation chain must land in one group; distinct keys "
            "mean the splitter is free to separate them"
        )

    def test_no_grouping_key_appears_in_two_splits(self) -> None:
        # THE invariant. Stated here so the example is a positive fixture for the check that
        # scripts/validate_manifest.py owns.
        splits_by_key: dict[str, set[str]] = {}
        for record in manifest_records():
            splits_by_key.setdefault(
                record["grouping"]["grouping_key_sha256"], set()
            ).add(record["split"])
        straddling = {k: sorted(v) for k, v in splits_by_key.items() if len(v) > 1}
        assert straddling == {}, f"grouping keys straddle splits: {straddling}"

    def test_the_disjointness_check_would_actually_catch_a_leak(self) -> None:
        # The previous test passes trivially on a clean example. This one proves the check has
        # teeth by moving one record of a chain into eval_locked, which is the leak in question.
        records = copy.deepcopy(manifest_records())
        victim = next(r for r in records if r["grouping"]["augmentation_depth"] == 2)
        victim["split"] = "eval_locked"
        splits_by_key: dict[str, set[str]] = {}
        for record in records:
            splits_by_key.setdefault(
                record["grouping"]["grouping_key_sha256"], set()
            ).add(record["split"])
        assert any(len(v) > 1 for v in splits_by_key.values()), (
            "moving an augmented child into eval_locked did not straddle a grouping key, which "
            "means the key is not actually capturing the derivation relationship"
        )
        # ...and note what the schema alone cannot see: the mutated document is still valid.
        assert (
            validate({**MANIFEST_EXAMPLE, "records": records}, MANIFEST_SCHEMA) == []
        ), (
            "if the schema rejected this, the cross-record check would be redundant; it does not, "
            "which is exactly why the check belongs in scripts/validate_manifest.py"
        )

    def test_no_two_sample_ids_collide(self) -> None:
        ids = [r["sample_id"] for r in manifest_records()]
        assert len(ids) == len(set(ids))

    def test_no_two_records_share_an_audio_digest(self) -> None:
        # The most direct possible leak: the same bytes under two sample_ids, one in train and one
        # in eval. Checked on the example; owned by scripts/validate_manifest.py at scale.
        digests = [r["sha256_audio"] for r in manifest_records()]
        assert len(digests) == len(set(digests))


# ==================================================================================================
class TestLabelAndGeneratorCoherence:
    def test_bonafide_speech_cannot_carry_a_generator(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        record = next(r for r in doc["records"] if r["label"] == "bonafide")
        record["generator_family"] = "example-tts-family"
        assert validate(doc, MANIFEST_SCHEMA) != [], (
            "natural speech labelled with a generator is natural speech on the way to being "
            "labelled spoof, which playbook 2 forbids outright"
        )

    def test_a_spoof_sample_must_name_its_generator(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        record = next(r for r in doc["records"] if r["label"] == "spoof")
        record["generator_family"] = "none"
        assert validate(doc, MANIFEST_SCHEMA) != [], (
            "an unattributed spoof sample cannot be held out by generator, so it silently "
            "weakens eval_generator_heldout"
        )

    def test_a_spoof_sample_must_name_its_attack_type(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        record = next(r for r in doc["records"] if r["label"] == "spoof")
        record["attack_type"] = "none"
        assert validate(doc, MANIFEST_SCHEMA) != []

    def test_the_generator_group_carries_a_version_not_just_a_family(self) -> None:
        for record in manifest_records():
            gid = record["grouping"]["generator_group_id"]
            if record["label"] == "spoof":
                assert ":" in gid, (
                    f"{gid!r} has no version. Two versions of one family differ enough that "
                    "holding out only the family lets a version leak."
                )
                assert gid.startswith(record["generator_family"] + ":")
            else:
                assert gid == "bonafide", (
                    "bona fide records need a non-null generator group or they drop out of the "
                    "grouping entirely and cross splits their spoofed counterparts were held from"
                )

    def test_the_label_is_never_an_authorization_word(self) -> None:
        # R-07 reaches here too: a dataset label vocabulary of approve/deny would leak into
        # reporting language even though the schema is not the audit table.
        labels = set(MANIFEST_SCHEMA["$defs"]["record"]["properties"]["label"]["enum"])
        assert labels == {"bonafide", "spoof"}
        assert not labels & set(sc.FORBIDDEN_ACTION_VALUES)


# ==================================================================================================
class TestAccentIsNeverInferredFromAudio:
    """Playbook 2.1 states it in prose; the enum makes it unstateable."""

    def test_the_source_enum_offers_no_inference_option(self) -> None:
        enum = MANIFEST_SCHEMA["$defs"]["record"]["properties"]["accent_region_source"][
            "enum"
        ]
        assert enum == ["self_reported", "dataset_metadata", "unknown"]
        assert not any("infer" in v or "model" in v or "predict" in v for v in enum), (
            "an inference option here would turn bias analysis into a measurement of the "
            "inference, and would build a demographic classifier nobody asked for"
        )

    def test_declaring_an_inferred_accent_is_rejected(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        doc["records"][0]["accent_region_source"] = "inferred_from_audio"
        assert validate(doc, MANIFEST_SCHEMA) != []

    def test_the_source_is_required_alongside_the_value(self) -> None:
        required = MANIFEST_SCHEMA["$defs"]["record"]["required"]
        assert (
            "accent_region" in required and "accent_region_source" in required
        ), "an accent value with no provenance is indistinguishable from a guessed one"


# ==================================================================================================
class TestConsentIsMandatoryOnEveryRecord:
    def test_consent_basis_and_retention_expiry_are_both_required(self) -> None:
        required = MANIFEST_SCHEMA["$defs"]["record"]["required"]
        assert "consent_basis" in required and "retention_expiry" in required

    def test_only_a_licence_governed_corpus_may_omit_a_retention_date(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        record = next(
            r
            for r in doc["records"]
            if r["consent_basis"] != "public-corpus-license-only"
        )
        record["retention_expiry"] = None
        assert (
            validate(doc, MANIFEST_SCHEMA) != []
        ), "consent with no expiry is indefinite consent, which is not what anyone signs"

    def test_a_public_corpus_may_omit_it(self) -> None:
        # The permitted case, asserted so the constraint above is not accidentally absolute.
        public = [
            r
            for r in manifest_records()
            if r["consent_basis"] == "public-corpus-license-only"
        ]
        assert public and any(r["retention_expiry"] is None for r in public)

    def test_an_empty_consent_basis_is_not_a_valid_value(self) -> None:
        doc = copy.deepcopy(MANIFEST_EXAMPLE)
        doc["records"][0]["consent_basis"] = ""
        assert (
            validate(doc, MANIFEST_SCHEMA) != []
        ), "a blank basis is an absence of information wearing the clothes of an answer"


# ==================================================================================================
class TestConsentLedger:
    def test_the_committed_example_validates(self) -> None:
        errors = validate(LEDGER_EXAMPLE, LEDGER_SCHEMA)
        assert errors == [], "; ".join(errors)

    def test_every_ref_resolves(self) -> None:
        for ref in re.findall(
            r'"\$ref":\s*"([^"]+)"', LEDGER_SCHEMA_PATH.read_text("utf-8")
        ):
            _resolve(LEDGER_SCHEMA, ref)

    def test_consent_basis_and_retention_expiry_are_required_per_record(self) -> None:
        # The brief's explicit requirement for this file.
        required = LEDGER_SCHEMA["$defs"]["record"]["required"]
        assert "consent_basis" in required
        assert "retention_expiry" in required

    def test_retention_expiry_is_not_nullable_here(self) -> None:
        # Unlike the manifest, where a licence-only corpus has no consent clock. Every ledger
        # record describes a person, and every person was told a duration or was not informed.
        field = LEDGER_SCHEMA["$defs"]["record"]["properties"]["retention_expiry"]
        assert field["type"] == "string"

    def test_a_null_retention_expiry_is_rejected(self) -> None:
        doc = copy.deepcopy(LEDGER_EXAMPLE)
        doc["records"][0]["retention_expiry"] = None
        assert validate(doc, LEDGER_SCHEMA) != []

    @pytest.mark.privacy
    def test_the_ledger_has_no_field_for_an_identity(self) -> None:
        names = property_names(LEDGER_SCHEMA)
        for banned in (
            "name",
            "email",
            "mobile",
            "address",
            "signature",
            "dob",
            "aadhaar",
        ):
            offenders = [n for n in names if banned in n.lower()]
            assert offenders == [], (
                f"{offenders} would make this the highest-value file in the repository. The "
                "subject is identified by the same salted hash the manifest uses (R-16)."
            )

    @pytest.mark.privacy
    def test_the_example_contains_no_identifier_shaped_value(self) -> None:
        blob = LEDGER_EXAMPLE_PATH.read_text("utf-8")
        assert (
            re.search(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b", blob) is None
        ), "phone-shaped number"
        assert (
            re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", blob) is None
        ), "email-shaped string"

    def test_the_subject_hash_has_the_same_shape_as_the_manifest_speaker_hash(
        self,
    ) -> None:
        # The join is value equality, so a shape mismatch would silently make it always empty.
        assert (
            LEDGER_SCHEMA["$defs"]["record"]["properties"]["subject_id_hash"]["$ref"]
            == "#/$defs/sha256"
        )
        assert LEDGER_SCHEMA["$defs"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"
        assert MANIFEST_SCHEMA["$defs"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"

    def test_withdrawal_is_present_on_every_record_including_unwithdrawn_ones(
        self,
    ) -> None:
        assert "withdrawal" in LEDGER_SCHEMA["$defs"]["record"]["required"]
        for record in LEDGER_EXAMPLE["records"]:
            assert (
                "withdrawn" in record["withdrawal"]
            ), "an absent withdrawal block cannot distinguish 'not withdrawn' from 'not checked'"

    def test_a_withdrawal_without_a_date_is_rejected(self) -> None:
        doc = copy.deepcopy(LEDGER_EXAMPLE)
        doc["records"][0]["withdrawal"] = {"withdrawn": True, "withdrawn_at": None}
        errors = validate(doc, LEDGER_SCHEMA)
        assert errors != [], (
            "a withdrawal with no timestamp cannot be acted on, audited, or ordered against the "
            "recordings it covers"
        )

    def test_a_withdrawal_must_record_that_deletion_completed(self) -> None:
        doc = copy.deepcopy(LEDGER_EXAMPLE)
        doc["records"][0]["withdrawal"] = {
            "withdrawn": True,
            "withdrawn_at": "2026-02-01T00:00:00Z",
        }
        assert any(
            "deletion_completed_at" in e for e in validate(doc, LEDGER_SCHEMA)
        ), "a withdrawal with no completion timestamp is a request, not a deletion"

    def test_an_unwithdrawn_record_cannot_carry_a_withdrawal_date(self) -> None:
        doc = copy.deepcopy(LEDGER_EXAMPLE)
        doc["records"][0]["withdrawal"] = {
            "withdrawn": False,
            "withdrawn_at": "2026-02-01T00:00:00Z",
            "deletion_completed_at": None,
        }
        assert (
            validate(doc, LEDGER_SCHEMA) != []
        ), "the safe reading of that contradiction is that somebody's withdrawal was reverted"

    def test_cloning_permission_cannot_contradict_the_consent_scope(self) -> None:
        doc = copy.deepcopy(LEDGER_EXAMPLE)
        record = next(r for r in doc["records"] if not r["synthetic_cloning_permitted"])
        record["synthetic_cloning_permitted"] = True
        assert (
            validate(doc, LEDGER_SCHEMA) != []
        ), "two fields that can disagree about whether a voice may be cloned are worse than one"

    def test_demo_playback_requires_a_named_capture_session(self) -> None:
        doc = copy.deepcopy(LEDGER_EXAMPLE)
        record = next(
            r
            for r in doc["records"]
            if "demo_playback_to_third_parties" in r["consent_scope"]
        )
        del record["capture_session_id"]
        assert any("capture_session_id" in e for e in validate(doc, LEDGER_SCHEMA))

    def test_the_example_shows_a_completed_withdrawal(self) -> None:
        withdrawn = [
            r for r in LEDGER_EXAMPLE["records"] if r["withdrawal"]["withdrawn"]
        ]
        assert withdrawn, "the example must show what an honoured withdrawal looks like"
        for record in withdrawn:
            assert record["withdrawal"]["deletion_completed_at"] is not None

    def test_a_withdrawn_subject_has_no_surviving_manifest_records(self) -> None:
        # What a completed withdrawal looks like from outside: ledger record present, deletion
        # timestamped, samples gone. The two example files are consistent on this point.
        sample_ids = {r["sample_id"] for r in manifest_records()}
        speaker_hashes = {r["speaker_id_hash"] for r in manifest_records()}
        for record in LEDGER_EXAMPLE["records"]:
            if not record["withdrawal"]["withdrawn"]:
                continue
            assert record["subject_id_hash"] not in speaker_hashes
            assert not (set(record["covers_sample_ids"]) & sample_ids)

    def test_the_ledger_record_survives_the_deletion_it_records(self) -> None:
        # Deleting the ledger row too would erase the evidence that the withdrawal was honoured.
        assert any(r["withdrawal"]["withdrawn"] for r in LEDGER_EXAMPLE["records"])


# ==================================================================================================
class TestTheTwoFilesJoin:
    """The cross-file rule scripts/validate_manifest.py must enforce, demonstrated on examples."""

    def test_every_consented_sample_has_a_ledger_record(self) -> None:
        covered: set[str] = set()
        for record in LEDGER_EXAMPLE["records"]:
            covered |= set(record["covers_sample_ids"])
        for record in manifest_records():
            if record["consent_basis"] == "public-corpus-license-only":
                continue
            assert record["sample_id"] in covered, (
                f"{record['sample_id']} claims consent basis {record['consent_basis']!r} and no "
                "ledger record covers it. Consent asserted in a manifest and recorded nowhere "
                "is an assertion, not a basis."
            )

    def test_the_declared_basis_agrees_across_the_two_files(self) -> None:
        by_sample = {
            sample: record["consent_basis"]
            for record in LEDGER_EXAMPLE["records"]
            for sample in record["covers_sample_ids"]
        }
        for record in manifest_records():
            if record["sample_id"] in by_sample:
                assert by_sample[record["sample_id"]] == record["consent_basis"]

    def test_the_retention_dates_agree_across_the_two_files(self) -> None:
        by_sample = {
            sample: record["retention_expiry"]
            for record in LEDGER_EXAMPLE["records"]
            for sample in record["covers_sample_ids"]
        }
        for record in manifest_records():
            if record["sample_id"] in by_sample:
                assert (
                    by_sample[record["sample_id"]] == record["retention_expiry"]
                ), "two retention dates for one sample means the earlier one will be ignored"

    def test_the_ledger_basis_vocabulary_is_the_manifest_one_minus_the_licence_case(
        self,
    ) -> None:
        manifest = set(
            MANIFEST_SCHEMA["$defs"]["record"]["properties"]["consent_basis"]["enum"]
        )
        ledger = set(LEDGER_SCHEMA["$defs"]["consent_basis"]["enum"])
        assert ledger == manifest - {"public-corpus-license-only"}, (
            "divergent vocabularies make the join silently partial: a basis valid in one file "
            "and unknown in the other joins to nothing and reports no error"
        )

    def test_every_ledger_form_digest_is_declared_in_the_header(self) -> None:
        declared = {v["form_sha256"] for v in LEDGER_EXAMPLE["consent_form_versions"]}
        for record in LEDGER_EXAMPLE["records"]:
            assert (
                record["consent_form_sha256"] in declared
            ), "a form digest with no matching wording means nobody can say what was consented to"

    def test_the_speaker_hash_is_the_join_key_in_both_directions(self) -> None:
        subjects = {r["subject_id_hash"] for r in LEDGER_EXAMPLE["records"]}
        for record in manifest_records():
            if record["consent_basis"] == "public-corpus-license-only":
                continue
            assert record["speaker_id_hash"] in subjects


# ==================================================================================================
class TestNoPlaceholderPassesAsReal:
    """The examples must be unmistakably examples (R-01..R-04 habits applied to fixtures)."""

    def test_the_manifest_example_announces_itself(self) -> None:
        assert "example" in MANIFEST_EXAMPLE["manifest_id"]
        assert "not-for-training" in MANIFEST_EXAMPLE["manifest_id"], (
            "an example manifest with a plausible id is one `cp` away from being cited as the "
            "manifest a reported number was measured on"
        )

    def test_the_ledger_example_announces_itself(self) -> None:
        assert (
            "example" in LEDGER_EXAMPLE["ledger_id"]
            and "not-real" in LEDGER_EXAMPLE["ledger_id"]
        )

    def test_the_example_licence_cannot_be_mistaken_for_a_real_one(self) -> None:
        for record in manifest_records():
            assert (
                "EXAMPLE" in record["source_license"]
                or "INTERNAL" in record["source_license"]
            )

    def test_the_example_consent_wording_is_marked_as_not_a_form(self) -> None:
        for version in LEDGER_EXAMPLE["consent_form_versions"]:
            assert "NOT A REAL FORM" in version["plain_language_summary"].upper()

    def test_the_named_corpus_access_terms_have_an_unassigned_owner_marked_todo(
        self,
    ) -> None:
        # Better an explicit TODO than a plausible name nobody agreed to be.
        for pin in MANIFEST_EXAMPLE["source_snapshot"]["pinned_revisions"]:
            assert pin["access_terms_accepted_by"].startswith("TODO")


# ==================================================================================================
@pytest.mark.integration
class TestWithTheRealLibrary:
    """Re-runs acceptance under ``jsonschema``. UNVERIFIED here: the library is not installed.

    Marked ``integration`` because it needs a dependency this environment cannot install (no
    network), which is the same reporting category as the tests needing a live PostgreSQL: not
    run, therefore not passing.
    """

    def test_the_manifest_schema_is_itself_a_valid_2020_12_schema(self) -> None:
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.Draft202012Validator.check_schema(MANIFEST_SCHEMA)

    def test_the_ledger_schema_is_itself_a_valid_2020_12_schema(self) -> None:
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.Draft202012Validator.check_schema(LEDGER_SCHEMA)

    def test_the_manifest_example_validates_under_the_real_library(self) -> None:
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.Draft202012Validator(MANIFEST_SCHEMA).validate(MANIFEST_EXAMPLE)

    def test_the_ledger_example_validates_under_the_real_library(self) -> None:
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.Draft202012Validator(LEDGER_SCHEMA).validate(LEDGER_EXAMPLE)

    def test_the_real_library_agrees_with_the_subset_validator_on_rejections(
        self,
    ) -> None:
        # The point of this one: if the subset validator and the library ever disagree about a
        # rejection, the negative tests above are measuring the wrong thing.
        jsonschema = pytest.importorskip("jsonschema")
        validator = jsonschema.Draft202012Validator(MANIFEST_SCHEMA)
        for mutate in (
            lambda d: d["records"][0].__setitem__("audio_path", "/data/x.wav"),
            lambda d: d["records"][0]["grouping"].__setitem__(
                "grouped_before_augmentation", False
            ),
            lambda d: d["records"][0]["grouping"].__setitem__(
                "group_by", ["speaker_id_hash"]
            ),
            lambda d: d["records"][0].__setitem__("split", "eval"),
            lambda d: d["records"][0].__setitem__(
                "accent_region_source", "inferred_from_audio"
            ),
        ):
            doc = copy.deepcopy(MANIFEST_EXAMPLE)
            mutate(doc)
            assert validate(doc, MANIFEST_SCHEMA) != []
            assert not validator.is_valid(doc)
