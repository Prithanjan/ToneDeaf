"""The declared shape of the audit schema — one dependency-free source of truth.

``technical-design.md`` §5.1 gives the complete allowed-column list and §5.2 gives the structural
deny-list. This module states both as data so that three different consumers agree by construction
instead of by comment:

* ``versions/0001_audit_event.py`` emits its DDL from these tuples.
* ``audit/tests/`` asserts the DDL text and (under the ``integration`` marker) the *deployed*
  ``information_schema`` against them.
* ``scripts/verify_audit_chain.py`` (Pair A) can import :data:`COLUMN_NAMES` rather than re-listing
  columns, so a verifier that reads a column the migration never created fails at import.

**Stdlib only, on purpose.** The privacy control in §5.2 is worth nothing if it only runs in a CI job
that has PostgreSQL, SQLAlchemy, and Alembic installed — that is the job people skip when it is slow
or flaky. Importing this module costs nothing, so the deny-list assertion can run in the fastest
lane there is. Nothing here reads a file, opens a socket, or needs a driver.

Cross-checked against ``gateway/app/audit/chain.py``: :data:`CHAINED_COLUMNS` must equal that
module's ``CHAIN_FIELDS`` and :data:`UNCHAINED_COLUMNS` its ``EXCLUDED_FIELDS``. The writer derives
its INSERT column list from ``CHAIN_FIELDS``, so a migration that disagrees does not produce a review
comment — it produces a runtime ``UndefinedColumnError`` on the first audit write of a live demo.
``audit/tests/test_schema_allow_list.py`` asserts the equality in both directions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, Sequence

TABLE_NAME: Final[str] = "audit_event"

#: Alembic's own bookkeeping table. Exempt from the exact-allow-list assertion (it is not ours) but
#: NOT exempt from the deny-list scan — a forbidden column is forbidden wherever it appears.
ALEMBIC_VERSION_TABLE: Final[str] = "alembic_version"


# --------------------------------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Column:
    """One allowed column, declared with everything needed to emit it and to reflect it back.

    ``data_type`` / ``udt_name`` are what PostgreSQL 16 reports in ``information_schema.columns``,
    not what we typed in the DDL. Storing the *reflected* spelling is the point: a test that compares
    against the DDL string it generated proves only that string formatting works.
    """

    name: str
    sql_type: str
    nullable: bool
    data_type: str
    udt_name: str
    default: str | None = None
    numeric_precision: int | None = None
    numeric_scale: int | None = None

    @property
    def ddl(self) -> str:
        parts = [self.name, self.sql_type, "NULL" if self.nullable else "NOT NULL"]
        if self.default is not None:
            parts.append(f"DEFAULT {self.default}")
        return " ".join(parts)


#: The complete allow-list, in the order of technical-design.md §5.1. The order carries no semantics
#: — the assertions compare sets and report the diff — but a reviewer diffing this list against the
#: spec table should be able to read straight down.
COLUMNS: Final[tuple[Column, ...]] = (
    # Surrogate key. Excluded from the hash: a key the database chose carries no auditable meaning.
    Column("event_id", "uuid", False, "uuid", "uuid"),
    # Decision D-7: present from Phase 1 so Phase-4 row-level security is a policy change rather
    # than a migration of live evidence. RLS is deliberately NOT enabled here (see README, R-01).
    Column("tenant_id", "text", False, "text", "text", default="'demo-tenant'::text"),
    Column("session_id", "uuid", False, "uuid", "uuid"),
    # The HMAC pseudonym, never the raw client_call_ref (R-16). CHECK-constrained to 64 lowercase
    # hex below — the database is the last boundary before a raw reference becomes durable.
    Column("call_ref", "text", False, "text", "text"),
    Column("event_seq", "bigint", False, "bigint", "int8"),
    Column("occurred_at", "timestamptz", False, "timestamp with time zone", "timestamptz"),
    Column("purpose_code", "text", False, "text", "text"),
    Column("context_value_band", "text", False, "text", "text"),
    # NULL for lifecycle events, which have no window. Distinct from 0 in the hash input.
    Column("window_seq", "bigint", True, "bigint", "int8"),
    # numeric(5,4), matching chain.py's fixed 4-decimal serialization. A float column would round-trip
    # to a different repr and every stored hash would stop verifying.
    Column(
        "spoof_risk", "numeric(5,4)", True, "numeric", "numeric",
        numeric_precision=5, numeric_scale=4,
    ),
    Column("risk_state", "text", False, "text", "text"),
    Column("action", "text", False, "text", "text"),
    Column("reason_code", "text", False, "text", "text"),
    Column("policy_version", "text", False, "text", "text"),
    Column("policy_bundle_sha256", "text", False, "text", "text"),
    Column("model_version", "text", False, "text", "text"),
    Column("model_sha256", "text", False, "text", "text"),
    Column("calibration_version", "text", False, "text", "text"),
    Column("calibration_sha256", "text", False, "text", "text"),
    # The only array column in the schema, and therefore the only place a bounded enum could be
    # widened into free text. Membership-constrained below rather than length-constrained.
    Column("quality_flags", "text[]", False, "ARRAY", "_text", default="'{}'::text[]"),
    # R-46: mock mode is loud. It is visible in every audit row, not only in a startup banner.
    Column("detector_mode", "text", False, "text", "text"),
    Column("execution_provider", "text", False, "text", "text"),
    Column("deployment_profile", "text", False, "text", "text"),
    # The two permitted bytea columns, and the only two (§5.2 rule 2). 32 bytes each, CHECKed.
    Column("prev_event_hash", "bytea", False, "bytea", "bytea"),
    Column("event_hash", "bytea", False, "bytea", "bytea"),
    # Excluded from the hash so a retention-policy change does not invalidate history.
    Column("retention_expires_at", "timestamptz", False, "timestamp with time zone", "timestamptz"),
)

COLUMN_NAMES: Final[tuple[str, ...]] = tuple(c.name for c in COLUMNS)
COLUMNS_BY_NAME: Final[dict[str, Column]] = {c.name: c for c in COLUMNS}

#: Must equal ``app.audit.chain.EXCLUDED_FIELDS`` (as a set).
UNCHAINED_COLUMNS: Final[tuple[str, ...]] = (
    "event_id",
    "retention_expires_at",
    "prev_event_hash",
    "event_hash",
)

#: Must equal ``app.audit.chain.CHAIN_FIELDS`` (as a set), and in the same order — the order is
#: documented in technical-design.md §5.3 for human review.
CHAINED_COLUMNS: Final[tuple[str, ...]] = tuple(
    c.name for c in COLUMNS if c.name not in UNCHAINED_COLUMNS
)


# --------------------------------------------------------------------------------------------------
# Closed vocabularies — mirrored from contracts/openapi.yaml
# --------------------------------------------------------------------------------------------------

#: R-07. The complete action vocabulary. A CHECK constraint on it is the third independent place the
#: closure is enforced (Python enum, OpenAPI enum, database), which is the point: adding an
#: authorization outcome has to be done three times, in three reviews.
ACTION_VOCABULARY: Final[tuple[str, ...]] = ("continue", "verify", "hold", "escalate")

#: Values that must not appear as an accepted value anywhere in the schema. ``approve``/``deny`` are
#: named by R-07; the other three are the synonyms a well-meaning contributor reaches for next.
FORBIDDEN_ACTION_VALUES: Final[tuple[str, ...]] = ("approve", "deny", "allow", "block", "reject")

RISK_STATE_VOCABULARY: Final[tuple[str, ...]] = ("collecting", "uncertain", "high")
CONTEXT_VALUE_BAND_VOCABULARY: Final[tuple[str, ...]] = ("low", "medium", "high", "unspecified")
EXECUTION_PROVIDER_VOCABULARY: Final[tuple[str, ...]] = (
    "CUDAExecutionProvider",
    "CPUExecutionProvider",
)
DEPLOYMENT_PROFILE_VOCABULARY: Final[tuple[str, ...]] = ("aws-gpu", "local-cpu")
DETECTOR_MODE_VOCABULARY: Final[tuple[str, ...]] = (
    "REAL_DETECTOR",
    "MOCK_SMOKE_MODE_NOT_A_DETECTOR",
)

#: ``contracts/openapi.yaml#/components/schemas/QualityFlag`` plus the proto's zero value, which
#: ``app.scorer.client`` can legitimately produce by ``QualityFlag.Name(0)``.
QUALITY_FLAG_VOCABULARY: Final[tuple[str, ...]] = (
    "QUALITY_FLAG_UNSPECIFIED",
    "LOW_ENERGY",
    "CLIPPING_DETECTED",
    "NARROWBAND_SUSPECTED",
    "HIGH_NOISE",
    "PACKET_LOSS_SUSPECTED",
    "DC_OFFSET",
    "INSUFFICIENT_VOICED",
)

#: ``purpose_code`` is deliberately NOT membership-checked. The enum lives in ``contracts/`` under a
#: two-key rule (R-22) and in ``policy.yaml``'s action map; a third copy in DDL means adding a
#: purpose can pass contract review, pass policy review, and then fail on the first audit INSERT of a
#: judged demo. Shape-checked instead: closed enough to keep free text and PII out, open enough that
#: a new purpose is not a migration. Same reasoning for ``reason_code`` and ``detector_mode``.
LOWER_SNAKE_REGEX: Final[str] = "^[a-z][a-z0-9_]{2,63}$"
UPPER_SNAKE_REGEX: Final[str] = "^[A-Z][A-Z0-9_]{2,63}$"
TENANT_ID_REGEX: Final[str] = "^[a-z0-9][a-z0-9_-]{1,63}$"
VERSION_REGEX: Final[str] = "^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"

#: 64 lowercase hex — the shape ``app.security.pseudonym`` produces and the pattern
#: ``contracts/openapi.yaml`` declares for ``call_ref``.
HEX64_REGEX: Final[str] = "^[0-9a-f]{64}$"

#: Hex-or-empty, for the two artifact digests the Gateway copies out of the Scorer's health response.
#: Empty is reachable today: ``gateway/app/api/v1/health.py`` falls back to ``""`` when the Scorer is
#: unreachable, and mock mode has no ONNX file to hash. Requiring 64 hex here would turn "the Scorer
#: did not report a digest" into a failed audit write, i.e. lost evidence — the worse of the two
#: failures. The ``ck_audit_event_real_detector_is_identified`` constraint below closes the gap that
#: matters: an unidentified model may only ever appear alongside a non-real detector mode.
HEX64_OR_EMPTY_REGEX: Final[str] = "^([0-9a-f]{64})?$"


# --------------------------------------------------------------------------------------------------
# Constraints
# --------------------------------------------------------------------------------------------------


def _in_list(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


@dataclass(frozen=True, slots=True)
class Constraint:
    """A named table constraint. The name is part of the contract: tests assert on names, and an
    anonymous constraint produces an error message nobody can act on at 2 a.m."""

    name: str
    definition: str
    why: str


CHECK_CONSTRAINTS: Final[tuple[Constraint, ...]] = (
    Constraint(
        "ck_audit_event_call_ref_is_pseudonym",
        f"call_ref ~ '{HEX64_REGEX}'",
        "R-16. A client-side bug that sends a raw caller reference where a pseudonym belongs must "
        "fail at the database boundary — the last place it can be caught before it is durable.",
    ),
    Constraint(
        "ck_audit_event_action_vocabulary",
        _in_list("action", ACTION_VOCABULARY),
        "R-07. approve/deny/allow/block/reject are not rejected by convention here; there is no "
        "accepted value for them to be.",
    ),
    Constraint(
        "ck_audit_event_risk_state_vocabulary",
        _in_list("risk_state", RISK_STATE_VOCABULARY),
        "technical-design.md §5.1. Matches app.policy.engine.RiskState exactly.",
    ),
    Constraint(
        "ck_audit_event_context_value_band_vocabulary",
        _in_list("context_value_band", CONTEXT_VALUE_BAND_VOCABULARY),
        "Decision D-5: a coarse closed band, never a free string or an amount. An amount column "
        "would be unauditable and would invite PII into the audit table.",
    ),
    Constraint(
        "ck_audit_event_execution_provider_vocabulary",
        _in_list("execution_provider", EXECUTION_PROVIDER_VOCABULARY),
        "R-45. A silent CPU fallback on the GPU tier is a failure, not a degradation; the provider "
        "recorded per row is what makes a latency claim attributable.",
    ),
    Constraint(
        "ck_audit_event_deployment_profile_vocabulary",
        _in_list("deployment_profile", DEPLOYMENT_PROFILE_VOCABULARY),
        "R-06. The tiers are not byte-identical, so every row says which one produced it.",
    ),
    Constraint(
        "ck_audit_event_quality_flags_vocabulary",
        f"quality_flags <@ ARRAY[{', '.join(repr(f) for f in QUALITY_FLAG_VOCABULARY)}]::text[]",
        "R-14/R-15. text[] is the only variable-shaped column in the schema and therefore the only "
        "one that could carry a transcript fragment. Membership against the contract enum makes "
        "that structurally impossible; a length bound would not.",
    ),
    Constraint(
        "ck_audit_event_purpose_code_shape",
        f"purpose_code ~ '{LOWER_SNAKE_REGEX}'",
        "Shape, not membership — see the LOWER_SNAKE_REGEX note. Blocks free text and PII without "
        "making a new purpose_code a migration.",
    ),
    Constraint(
        "ck_audit_event_reason_code_shape",
        f"reason_code ~ '{UPPER_SNAKE_REGEX}'",
        "Same reasoning as purpose_code. ReasonCode is a closed enum in app.policy.engine.",
    ),
    Constraint(
        "ck_audit_event_detector_mode_shape",
        f"detector_mode ~ '{UPPER_SNAKE_REGEX}'",
        "R-46. Shape-checked so mock mode's loud name survives verbatim into the evidence.",
    ),
    Constraint(
        "ck_audit_event_tenant_id_shape",
        f"tenant_id ~ '{TENANT_ID_REGEX}'",
        "D-7. A tenant identifier is an opaque slug; free text here would become a PII channel the "
        "moment someone typed a customer name into a config file.",
    ),
    Constraint(
        "ck_audit_event_versions_shape",
        " AND ".join(
            f"{c} ~ '{VERSION_REGEX}'"
            for c in ("policy_version", "model_version", "calibration_version")
        ),
        "Version strings are identifiers. Bounded so a stack trace or a note cannot be parked in one.",
    ),
    Constraint(
        "ck_audit_event_policy_bundle_sha256_is_hex",
        f"policy_bundle_sha256 ~ '{HEX64_REGEX}'",
        "The Gateway computes this itself from the bundle file, so it is always a real digest. "
        "Unlike the Scorer-supplied digests it has no legitimate empty case.",
    ),
    Constraint(
        "ck_audit_event_artifact_sha256_is_hex_or_empty",
        f"model_sha256 ~ '{HEX64_OR_EMPTY_REGEX}' AND "
        f"calibration_sha256 ~ '{HEX64_OR_EMPTY_REGEX}'",
        "Hex or empty, never free text. See HEX64_OR_EMPTY_REGEX for why empty is permitted.",
    ),
    Constraint(
        "ck_audit_event_real_detector_is_identified",
        "detector_mode <> 'REAL_DETECTOR' OR model_sha256 <> ''",
        "R-03/R-51. An unidentified model may appear in the evidence only alongside a non-real "
        "detector mode. This is what stops an unattributable score from being cited as a result.",
    ),
    Constraint(
        "ck_audit_event_hash_widths",
        "octet_length(prev_event_hash) = 32 AND octet_length(event_hash) = 32",
        "technical-design.md §5.3. chain.py refuses a prev hash that is not 32 bytes; a truncated "
        "stored hash would otherwise fail verification days later with no clue where it came from.",
    ),
    Constraint(
        "ck_audit_event_sequences_non_negative",
        "event_seq >= 0 AND (window_seq IS NULL OR window_seq >= 0)",
        "The writer's per-session sequence starts at 0. A negative value means a counter was reset "
        "or wrapped, which forks the chain.",
    ),
    Constraint(
        "ck_audit_event_spoof_risk_bounded",
        "spoof_risk IS NULL OR (spoof_risk >= 0 AND spoof_risk <= 1)",
        "A bounded risk is the only kind the policy engine's threshold comparison means anything "
        "against. NULL stays legal: a lifecycle event carries no score, and NULL is a distinct hash "
        "input from 0.0000.",
    ),
)

#: UNIQUE (session_id, event_seq) per §5.1. Its btree is also the chain-verification access path:
#: ``WHERE session_id = $1 ORDER BY event_seq ASC`` becomes a forward index scan with no sort node,
#: which matters because the verifier walks every event of a session rather than sampling.
UNIQUE_CONSTRAINTS: Final[tuple[Constraint, ...]] = (
    Constraint(
        "uq_audit_event_session_seq",
        "UNIQUE (session_id, event_seq)",
        "Two writers computing event_seq from the same predecessor produce a fork, which a verifier "
        "reports as tampering. This makes the second insert fail instead.",
    ),
)


@dataclass(frozen=True, slots=True)
class Index:
    name: str
    columns: str
    why: str


INDEXES: Final[tuple[Index, ...]] = (
    Index(
        "ix_audit_event_session_retention",
        "(session_id, retention_expires_at)",
        "The retention sweep asks 'which sessions have every row expired?' — a GROUP BY session_id "
        "HAVING max(retention_expires_at) <= cutoff. Leading on session_id lets that run as an "
        "index-only GroupAggregate instead of a heap scan plus sort.",
    ),
    Index(
        "ix_audit_event_retention_expires_at",
        "(retention_expires_at)",
        "Cheap 'is there anything to do at all?' probe so an hourly worker on an idle demo database "
        "costs one index lookup. Not redundant with the composite above: that index cannot be "
        "scanned by its second column.",
    ),
)


# --------------------------------------------------------------------------------------------------
# The structural deny-list (technical-design.md §5.2)
# --------------------------------------------------------------------------------------------------

#: Substring vocabulary for §5.2 rule 1.
#:
#: ``gateway/app/audit/chain.py`` owns the writer-side copy in ``_FORBIDDEN_SUBSTRINGS`` and this
#: tuple must remain a SUPERSET of it, asserted by
#: ``audit/tests/test_deny_list.py::test_db_deny_list_covers_the_writer_side_list``. It is a superset
#: and not an import because of a real, current divergence: §5.2 and R-15 both list ``%raw%``, and
#: chain.py's tuple omits it. Importing chain.py's tuple as *the* list would silently adopt that gap
#: at the database layer, which is the one layer where the omission is permanent — a column, once
#: created and written to, cannot be un-created without destroying evidence.
#:
#: The relationship is asserted rather than assumed, in the direction that matters: chain.py's list
#: must be contained in this one (so a substring added there cannot be missed here), and every
#: substring §5.2 names must be present (so a deletion here fails). Neither test breaks when someone
#: fixes chain.py by adding ``raw``.
FORBIDDEN_COLUMN_SUBSTRINGS: Final[tuple[str, ...]] = (
    "audio",
    "pcm",
    "waveform",
    "transcript",
    "embedding",
    "phone",
    "msisdn",
    "caller_name",
    "raw",
)

#: §5.2 rule 2. The only two bytea columns that may exist.
PERMITTED_BYTEA_COLUMNS: Final[frozenset[str]] = frozenset({"prev_event_hash", "event_hash"})

#: §5.2 rule 3. ``vector`` is the pgvector type; the float arrays are how an embedding arrives when
#: pgvector is not installed. Matched on the reflected ``udt_name``, so the DDL spelling is irrelevant.
FORBIDDEN_UDT_NAMES: Final[frozenset[str]] = frozenset(
    {"vector", "halfvec", "sparsevec", "_float4", "_float8", "_numeric", "_bytea", "_int2"}
)

#: §5.2 rule 4.
MAX_UNLISTED_COLUMN_BYTES: Final[int] = 512


@dataclass(frozen=True, slots=True)
class ColumnFact:
    """One row of ``information_schema.columns``, reduced to what the deny-list needs.

    Deliberately a plain record rather than a driver row so the same assertions run against a
    reflected database and against the columns this module declares. The Phase-1 exit criterion is
    the reflected form; the static form is what keeps the check runnable in every CI lane.
    """

    table_name: str
    column_name: str
    data_type: str
    udt_name: str
    character_maximum_length: int | None = None


def declared_column_facts() -> tuple[ColumnFact, ...]:
    """The allow-list expressed as ``ColumnFact``s, for running the deny-list without a database."""
    return tuple(
        ColumnFact(TABLE_NAME, c.name, c.data_type, c.udt_name) for c in COLUMNS
    )


def forbidden_substring_hits(names: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``(name, matched_substring)`` for every name that trips §5.2 rule 1.

    Case-insensitive substring matching, mirroring ``chain.py::_assert_not_forbidden``, so
    ``audio_blob_v2`` and ``AudioBlob`` are both caught. An exact-name deny-list is one a rename
    walks around.
    """
    hits: list[tuple[str, str]] = []
    for name in names:
        lowered = name.lower()
        for bad in FORBIDDEN_COLUMN_SUBSTRINGS:
            if bad in lowered:
                hits.append((name, bad))
                break
    return hits


def _is_wide(fact: ColumnFact) -> bool:
    """Whether a column could hold more than :data:`MAX_UNLISTED_COLUMN_BYTES` (§5.2 rule 4)."""
    if fact.character_maximum_length is not None:
        return fact.character_maximum_length > MAX_UNLISTED_COLUMN_BYTES
    # No declared bound: text, bytea, json, xml, and every array are unbounded in practice.
    return fact.data_type in {"text", "bytea", "json", "jsonb", "xml", "ARRAY"}


def deny_list_violations(facts: Iterable[ColumnFact]) -> list[str]:
    """Apply all five §5.2 rules and return human-readable violations, empty when clean.

    Returns a list rather than raising so a failing test can print every violation at once. A
    deny-list that reports one problem per CI run turns a schema review into five CI runs.

    Rules 1-4 apply to **every** table in the schema, including tables this project did not create:
    an audio column is forbidden wherever it appears, and a future ``session_note`` table is exactly
    the kind of place it would appear. Rule 5 (exact set) applies only to :data:`TABLE_NAME`, since
    that is the only table whose complete column list §5.1 specifies.
    """
    facts = tuple(facts)
    violations: list[str] = []

    for fact, hit in (
        (f, h)
        for f in facts
        for _, h in forbidden_substring_hits([f.column_name])
    ):
        violations.append(
            f"rule 1 (forbidden name): {fact.table_name}.{fact.column_name} contains {hit!r}"
        )

    for fact in facts:
        if fact.udt_name == "bytea" and fact.column_name not in PERMITTED_BYTEA_COLUMNS:
            violations.append(
                f"rule 2 (bytea): {fact.table_name}.{fact.column_name} is bytea and is not one of "
                f"{sorted(PERMITTED_BYTEA_COLUMNS)}"
            )
        if fact.udt_name in FORBIDDEN_UDT_NAMES:
            violations.append(
                f"rule 3 (vector/float-array shape): {fact.table_name}.{fact.column_name} "
                f"has udt_name {fact.udt_name!r}"
            )
        listed = fact.table_name == TABLE_NAME and fact.column_name in COLUMNS_BY_NAME
        if not listed and _is_wide(fact):
            violations.append(
                f"rule 4 (width): {fact.table_name}.{fact.column_name} is unbounded or wider than "
                f"{MAX_UNLISTED_COLUMN_BYTES} bytes and is not on the §5.1 allow-list"
            )

    violations.extend(allow_list_violations(
        f.column_name for f in facts if f.table_name == TABLE_NAME
    ))
    return violations


def allow_list_violations(actual: Iterable[str]) -> list[str]:
    """§5.2 rule 5 — the allow-list is an **exact** set, not a lower bound.

    Both directions are reported. The subset-only version of this check is the failure mode worth
    naming: ``assert set(expected) <= set(actual)`` passes happily the day after someone adds
    ``raw_audio_path``, because everything expected is still there.
    """
    actual_set = set(actual)
    expected = set(COLUMN_NAMES)
    problems: list[str] = []
    for extra in sorted(actual_set - expected):
        problems.append(f"rule 5 (exact set): unexpected column {TABLE_NAME}.{extra}")
    for missing in sorted(expected - actual_set):
        problems.append(f"rule 5 (exact set): missing column {TABLE_NAME}.{missing}")
    return problems


# --------------------------------------------------------------------------------------------------
# Reflection SQL — shared so the test and Pair A's verifier reflect the same thing
# --------------------------------------------------------------------------------------------------

#: Every column in the application schema. ``current_schema()`` rather than a literal ``'public'``:
#: the deny-list must scan whatever schema the migration actually ran into, or a search_path change
#: turns the privacy gate into a no-op that still reports green.
REFLECT_COLUMNS_SQL: Final[str] = """
SELECT table_name, column_name, data_type, udt_name, character_maximum_length,
       is_nullable, column_default, numeric_precision, numeric_scale
FROM information_schema.columns
WHERE table_schema = current_schema()
ORDER BY table_name, ordinal_position
"""

#: Constraint definitions as PostgreSQL renders them, for asserting the CHECK vocabulary is deployed
#: and not merely written down in a migration file that may never have been applied.
REFLECT_CONSTRAINTS_SQL: Final[str] = """
SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = current_schema() AND t.relname = $1
ORDER BY c.conname
"""

REFLECT_INDEXES_SQL: Final[str] = """
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = current_schema() AND tablename = $1
ORDER BY indexname
"""


def _self_check() -> None:
    """Internal consistency. Wrong here means every assertion built on this module is wrong.

    Mirrors the import-time guard in ``chain.py``: the allow-list is run through the deny-list at
    import, so a commit that adds a forbidden column to :data:`COLUMNS` cannot be imported — let
    alone migrated. Without this, the dangerous change (a forbidden column added to the allow-list
    *and* the migration together) would pass every per-column check.
    """
    assert len(COLUMN_NAMES) == len(set(COLUMN_NAMES)), "duplicate column name"
    assert set(UNCHAINED_COLUMNS) <= set(COLUMN_NAMES), "unchained column not on the allow-list"
    assert not (set(CHAINED_COLUMNS) & set(UNCHAINED_COLUMNS)), "column both chained and excluded"
    assert len(CHAINED_COLUMNS) + len(UNCHAINED_COLUMNS) == len(COLUMN_NAMES)
    assert not forbidden_substring_hits(COLUMN_NAMES), "the allow-list itself trips the deny-list"
    assert not deny_list_violations(declared_column_facts()), "the declared schema is not clean"
    assert not (set(ACTION_VOCABULARY) & set(FORBIDDEN_ACTION_VALUES)), "R-07 violated in-module"
    names = [c.name for c in (*CHECK_CONSTRAINTS, *UNIQUE_CONSTRAINTS, *INDEXES)]
    assert len(names) == len(set(names)), "duplicate constraint or index name"


_self_check()
