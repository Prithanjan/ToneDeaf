"""The audit table's column set, types, constraints, and indexes — asserted structurally.

``technical-design.md`` §5.1 is titled "the complete list", and §5.2 rule 5 makes it an exact SET: an
unexpected *extra* column fails too. That is the property these tests exist for. The version of this
test that everyone writes first —

    for column in EXPECTED:
        assert column in actual        # WRONG

— passes happily the day after someone adds ``raw_audio_path``, because everything expected is still
there. Every assertion below compares sets and reports the symmetric difference.

The spec list is transcribed independently at the top of this file rather than imported from
:mod:`schema_contract`. Importing it would make the test tautological: the module under test would be
supplying its own expected answer. The transcription is the second copy that makes a diff possible, and
it is short enough to check by eye against §5.1.

Static tests assert the DDL the migration generates. ``integration``-marked tests assert the *deployed*
``information_schema``, which is the Phase-1 exit criterion (``technical-design.md`` §9) — reflection is
what catches a migration that was written but never applied, or applied to the wrong schema.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
import schema_contract as sc
from app.audit.chain import CHAIN_FIELDS, EXCLUDED_FIELDS
from tests.conftest import CONTRACTS_DIR, GATEWAY_DIR, DATABASE_URL_ENV, revision_files

# --------------------------------------------------------------------------------------------------
# Independent transcription of technical-design.md §5.1 — (column, declared type, nullable)
# --------------------------------------------------------------------------------------------------

SPEC_5_1: Final[tuple[tuple[str, str, bool], ...]] = (
    ("event_id", "uuid", False),
    ("tenant_id", "text", False),
    ("session_id", "uuid", False),
    ("call_ref", "text", False),
    ("event_seq", "bigint", False),
    ("occurred_at", "timestamptz", False),
    ("purpose_code", "text", False),
    ("context_value_band", "text", False),
    ("window_seq", "bigint", True),
    ("spoof_risk", "numeric(5,4)", True),
    ("risk_state", "text", False),
    ("action", "text", False),
    ("reason_code", "text", False),
    ("policy_version", "text", False),
    ("policy_bundle_sha256", "text", False),
    ("model_version", "text", False),
    ("model_sha256", "text", False),
    ("calibration_version", "text", False),
    ("calibration_sha256", "text", False),
    ("quality_flags", "text[]", False),
    ("detector_mode", "text", False),
    ("execution_provider", "text", False),
    ("deployment_profile", "text", False),
    ("prev_event_hash", "bytea", False),
    ("event_hash", "bytea", False),
    ("retention_expires_at", "timestamptz", False),
)

SPEC_COLUMN_NAMES: Final[frozenset[str]] = frozenset(name for name, _, _ in SPEC_5_1)


def parse_ddl_columns(sql: str) -> list[str]:
    """Column names, in order, from a ``CREATE TABLE`` body.

    Text parsing rather than SQLAlchemy reflection: the statement is what Alembic will hand to
    PostgreSQL, and this suite must run where SQLAlchemy is not installed.
    """
    names: list[str] = []
    for raw in sql.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith(("CREATE TABLE", ")", "CONSTRAINT")):
            continue
        names.append(line.split()[0])
    return names


def openapi_enum(name: str) -> tuple[str, ...]:
    """Read one closed enum out of ``contracts/openapi.yaml`` without a YAML parser.

    PyYAML is not available in every lane this runs in, and the alternative — trusting that the DDL
    vocabulary matches the contract because both were written on the same afternoon — is how
    ``approve`` gets into one of them.
    """
    text = (CONTRACTS_DIR / "openapi.yaml").read_text(encoding="utf-8")
    block = re.search(rf"\n    {name}:\n(.*?)(?=\n    \w+:\n)", text, re.DOTALL)
    assert block, f"{name} not found in contracts/openapi.yaml"
    values = re.search(r"enum:\s*\[(.*?)\]", block.group(1), re.DOTALL)
    assert values, f"{name} has no enum in contracts/openapi.yaml"
    return tuple(
        v.strip() for v in values.group(1).replace("\n", " ").split(",") if v.strip()
    )


class TestAllowList:
    def test_schema_contract_matches_the_spec_table_exactly(self) -> None:
        """The declared allow-list equals technical-design.md §5.1. Set comparison in both directions —
        a missing column and an extra column are the same class of failure."""
        assert set(sc.COLUMN_NAMES) == SPEC_COLUMN_NAMES, {
            "extra_in_module": sorted(set(sc.COLUMN_NAMES) - SPEC_COLUMN_NAMES),
            "missing_from_module": sorted(SPEC_COLUMN_NAMES - set(sc.COLUMN_NAMES)),
        }

    def test_spec_order_is_preserved(self) -> None:
        """Order carries no semantics, but a reviewer diffs this list against the spec table by eye."""
        assert sc.COLUMN_NAMES == tuple(name for name, _, _ in SPEC_5_1)

    def test_ddl_creates_exactly_the_allow_list(self, create_table_sql: str) -> None:
        assert set(parse_ddl_columns(create_table_sql)) == SPEC_COLUMN_NAMES

    def test_declared_types_match_the_spec(self) -> None:
        for name, sql_type, nullable in SPEC_5_1:
            column = sc.COLUMNS_BY_NAME[name]
            assert (
                column.sql_type == sql_type
            ), f"{name}: {column.sql_type!r} != {sql_type!r}"
            assert (
                column.nullable is nullable
            ), f"{name}: nullability disagrees with §5.1"

    def test_only_window_seq_and_spoof_risk_are_nullable(self) -> None:
        """Lifecycle events have no window and no score; everything else is always known at write
        time. A nullable column elsewhere would let a row omit part of the evidence."""
        nullable = {c.name for c in sc.COLUMNS if c.nullable}
        assert nullable == {"window_seq", "spoof_risk"}

    def test_tenant_id_default_is_the_demo_tenant(self, create_table_sql: str) -> None:
        """D-7. NOT NULL DEFAULT 'demo-tenant' so Phase-4 RLS is a policy change rather than a
        migration of live evidence."""
        assert sc.COLUMNS_BY_NAME["tenant_id"].default == "'demo-tenant'::text"
        assert "tenant_id text NOT NULL DEFAULT 'demo-tenant'::text" in create_table_sql

    def test_quality_flags_defaults_to_empty_array(self) -> None:
        assert sc.COLUMNS_BY_NAME["quality_flags"].default == "'{}'::text[]"

    @pytest.mark.integration
    def test_deployed_columns_are_exactly_the_allow_list(
        self, database_url: str
    ) -> None:
        """UNVERIFIED without PostgreSQL 16. This is the assertion §5.2 actually specifies — against
        information_schema, so a migration that was written but never applied fails here."""
        pytest.skip(
            f"needs a live database via {DATABASE_URL_ENV}; see audit/README.md"
        )


class TestWriterAgreement:
    """The schema must agree with ``gateway/app/audit/`` exactly.

    ``writer.py`` derives its INSERT column list from ``chain.py``'s ``CHAIN_FIELDS``. A migration that
    disagrees does not produce a review comment — it produces ``UndefinedColumnError`` on the first
    audit write of a live demo, at which point the stream closes and the evidence for that session does
    not exist.
    """

    @pytest.mark.parity
    def test_chained_columns_equal_chain_fields(self) -> None:
        assert sc.CHAINED_COLUMNS == CHAIN_FIELDS

    @pytest.mark.parity
    def test_unchained_columns_equal_excluded_fields(self) -> None:
        assert set(sc.UNCHAINED_COLUMNS) == set(EXCLUDED_FIELDS)

    @pytest.mark.parity
    def test_writers_insert_columns_are_all_real_columns(self) -> None:
        """Parsed out of ``writer.py``'s source, not reconstructed from ``CHAIN_FIELDS``.

        Reconstructing it would test this test's arithmetic. Reading the file catches the case that
        actually happens: someone appends a column name to ``_INSERT_COLUMNS`` for a field they added
        to the writer but not to a migration.
        """
        source = (GATEWAY_DIR / "app" / "audit" / "writer.py").read_text(
            encoding="utf-8"
        )
        literal = re.search(
            r"_INSERT_COLUMNS: tuple\[str, \.\.\.\] = \((.*?)\n\)", source, re.DOTALL
        )
        assert literal, "could not find _INSERT_COLUMNS in gateway/app/audit/writer.py"
        body = literal.group(1)
        names = set(re.findall(r'"(\w+)"', body))
        if "*CHAIN_FIELDS" in body:
            names |= set(CHAIN_FIELDS)
        assert names == set(sc.COLUMN_NAMES), {
            "writer_inserts_columns_that_do_not_exist": sorted(
                names - set(sc.COLUMN_NAMES)
            ),
            "columns_the_writer_never_populates": sorted(set(sc.COLUMN_NAMES) - names),
        }

    def test_every_column_is_either_hashed_or_deliberately_excluded(self) -> None:
        """No third category. A column that is neither hashed nor on the documented exclusion list is
        stored-but-unverified — data inside the evidence table and outside the tamper evidence."""
        assert set(sc.CHAINED_COLUMNS) | set(sc.UNCHAINED_COLUMNS) == set(
            sc.COLUMN_NAMES
        )


class TestActionVocabulary:
    """R-07, enforced at the database boundary as well as in the enum and the OpenAPI schema."""

    def test_action_check_accepts_exactly_four_values(
        self, create_table_sql: str
    ) -> None:
        assert sc.ACTION_VOCABULARY == ("continue", "verify", "hold", "escalate")
        assert (
            "CONSTRAINT ck_audit_event_action_vocabulary CHECK "
            "(action IN ('continue', 'verify', 'hold', 'escalate'))" in create_table_sql
        )

    @pytest.mark.privacy
    @pytest.mark.parametrize("banned", sc.FORBIDDEN_ACTION_VALUES)
    def test_authorization_verbs_are_absent_from_the_whole_ddl(
        self, banned: str, create_table_sql: str
    ) -> None:
        """Not just absent from the action CHECK — absent from the entire statement.

        The failure this catches is a CHECK on some other column, or a default, or a comment that
        introduces ``deny`` as an accepted value elsewhere. R-07 says the vocabulary does not exist in
        any enum, config value, database CHECK constraint, or API schema; a test scoped to one
        constraint would not notice the second place.
        """
        assert not re.search(rf"'{banned}'", create_table_sql), (
            f"{banned!r} appears as a literal value in the audit DDL. This system produces "
            f"proportionate verification pressure; it never issues an authorization outcome (R-07)."
        )

    def test_risk_state_check_matches_the_engine(self, create_table_sql: str) -> None:
        assert sc.RISK_STATE_VOCABULARY == ("collecting", "uncertain", "high")
        assert "risk_state IN ('collecting', 'uncertain', 'high')" in create_table_sql

    @pytest.mark.contract
    @pytest.mark.parametrize(
        ("schema_name", "vocabulary"),
        [
            ("Action", sc.ACTION_VOCABULARY),
            ("RiskState", sc.RISK_STATE_VOCABULARY),
            ("ContextValueBand", sc.CONTEXT_VALUE_BAND_VOCABULARY),
            ("DeploymentProfile", sc.DEPLOYMENT_PROFILE_VOCABULARY),
            ("DetectorMode", sc.DETECTOR_MODE_VOCABULARY),
        ],
    )
    def test_ddl_vocabularies_equal_the_contract_enums(
        self, schema_name: str, vocabulary: tuple[str, ...]
    ) -> None:
        """The DDL is a third definition site for these enums. That is defence in depth only if the
        three agree; otherwise it is a demo that fails on INSERT after passing contract review."""
        assert set(openapi_enum(schema_name)) == set(vocabulary)

    @pytest.mark.contract
    def test_quality_flag_vocabulary_is_the_contract_enum_plus_the_proto_zero_value(
        self,
    ) -> None:
        """``app.scorer.client`` maps proto enum numbers through ``QualityFlag.Name``, so
        ``QUALITY_FLAG_UNSPECIFIED`` is reachable and must be accepted. Everything else must match the
        contract exactly — a flag the database rejects closes the stream mid-demo."""
        contract = set(openapi_enum("QualityFlag"))
        assert set(sc.QUALITY_FLAG_VOCABULARY) - contract == {
            "QUALITY_FLAG_UNSPECIFIED"
        }
        assert contract - set(sc.QUALITY_FLAG_VOCABULARY) == set()

    def test_purpose_code_is_shape_checked_not_membership_checked(
        self, create_table_sql: str
    ) -> None:
        """Deliberate asymmetry with ``action``.

        ``action`` is closed because R-07 says an extra value must be impossible. ``purpose_code`` is an
        evolving contract enum, and a DDL copy of it means adding a purpose can pass contract review,
        pass policy review, and then fail on the first audit INSERT of a judged demo. Shape-checked
        instead: no free text, no PII, no migration to add a purpose.
        """
        assert "purpose_code ~ '^[a-z][a-z0-9_]{2,63}$'" in create_table_sql
        for purpose in openapi_enum("PurposeCode"):
            assert re.match(
                sc.LOWER_SNAKE_REGEX, purpose
            ), f"{purpose} would be rejected by the CHECK"


class TestCallRefConstraint:
    @pytest.mark.privacy
    def test_call_ref_must_be_64_lowercase_hex(self, create_table_sql: str) -> None:
        """R-16. A client-side bug that sends a raw caller reference where a pseudonym belongs must
        fail at the database boundary — the last place it can be caught before it is durable."""
        assert "call_ref ~ '^[0-9a-f]{64}$'" in create_table_sql

    @pytest.mark.privacy
    @pytest.mark.parametrize(
        "raw",
        [
            "+919876543210",
            "ACME-CALL-00042",
            "priya.sharma@example.com",
            "A" * 64,  # uppercase hex: still not what the pseudonym function emits
            "0" * 63,
            "0" * 65,
            "",
        ],
    )
    def test_the_pattern_rejects_things_that_are_not_pseudonyms(self, raw: str) -> None:
        """The regex is the constraint's whole content, so it is worth testing as a regex. Every value
        here is something a well-meaning integration would plausibly put in ``client_call_ref``."""
        assert not re.match(
            sc.HEX64_REGEX, raw
        ), f"{raw!r} would be accepted as a call_ref"

    def test_the_pattern_accepts_a_real_pseudonym(self) -> None:
        assert re.match(sc.HEX64_REGEX, "a3f" + "0" * 61)

    @pytest.mark.privacy
    def test_scorer_supplied_digests_may_be_empty_but_never_free_text(self) -> None:
        """A deliberate asymmetry, and the one place this schema is looser than it looks.

        ``model_sha256`` and ``calibration_sha256`` are copied from the Scorer's health response.
        ``gateway/app/api/v1/health.py`` falls back to ``""`` when the Scorer is unreachable, and mock
        mode has no ONNX file to hash. Requiring 64 hex would turn "the Scorer did not report a digest"
        into a failed audit write — lost evidence, the worse of the two failures. Free text stays
        impossible, and the real-detector constraint closes the gap that matters.
        """
        pattern = sc.HEX64_OR_EMPTY_REGEX.strip("^$")
        assert re.fullmatch(pattern, "")
        assert re.fullmatch(pattern, "b" * 64)
        assert not re.fullmatch(pattern, "unknown")
        assert not re.fullmatch(pattern, "sha256:abc")

    def test_a_real_detector_must_carry_a_model_digest(
        self, create_table_sql: str
    ) -> None:
        """R-03/R-51. An unattributable score may appear in the evidence only alongside a non-real
        detector mode, so it can never be cited as a measured result."""
        assert (
            "detector_mode <> 'REAL_DETECTOR' OR model_sha256 <> ''" in create_table_sql
        )


class TestChainColumns:
    def test_hash_columns_are_width_checked(self, create_table_sql: str) -> None:
        """``chain.py`` refuses a prev hash that is not 32 bytes. Without the matching CHECK, a
        truncated stored hash surfaces days later as a verification failure with no clue why."""
        assert "octet_length(prev_event_hash) = 32" in create_table_sql
        assert "octet_length(event_hash) = 32" in create_table_sql

    def test_spoof_risk_is_numeric_5_4_to_match_the_canonical_form(self) -> None:
        """``chain.py::_format_risk`` serializes at fixed 4 decimals because the column is exact. A
        float column would round-trip to a different repr and every stored hash would stop verifying."""
        column = sc.COLUMNS_BY_NAME["spoof_risk"]
        assert (column.numeric_precision, column.numeric_scale) == (5, 4)

    def test_spoof_risk_stays_nullable(self) -> None:
        """A lifecycle event carries no score, and NULL is a distinct hash input from 0.0000 —
        collapsing them would make a session.open row indistinguishable from a genuine zero-risk
        window in the evidence."""
        assert sc.COLUMNS_BY_NAME["spoof_risk"].nullable is True
        assert "spoof_risk IS NULL OR (spoof_risk >= 0 AND spoof_risk <= 1)" in "".join(
            c.definition for c in sc.CHECK_CONSTRAINTS
        )

    def test_session_seq_is_unique(self, create_table_sql: str) -> None:
        """Two writers computing ``event_seq`` from the same predecessor fork the chain, which a
        verifier reports as tampering. The unique constraint makes the second insert fail instead."""
        assert (
            "CONSTRAINT uq_audit_event_session_seq UNIQUE (session_id, event_seq)"
            in create_table_sql
        )


class TestIndexes:
    def test_chain_verification_is_an_index_scan_not_a_sort(self) -> None:
        """The verifier reads ``WHERE session_id = $1 ORDER BY event_seq ASC`` for every event of a
        session. ``UNIQUE (session_id, event_seq)`` makes that a forward index scan with no sort node;
        without a leading ``session_id``, verifying a session would sort the table."""
        unique = sc.UNIQUE_CONSTRAINTS[0]
        assert unique.definition == "UNIQUE (session_id, event_seq)"

    def test_retention_sweep_has_both_indexes_it_needs(self) -> None:
        names = {index.name for index in sc.INDEXES}
        assert names == {
            "ix_audit_event_session_retention",
            "ix_audit_event_retention_expires_at",
        }

    def test_every_index_records_why_it_exists(self) -> None:
        """An index with no stated reason is one nobody can safely drop later."""
        for index in sc.INDEXES:
            assert len(index.why) > 40, f"{index.name} has no substantive rationale"


class TestMigrationDiscipline:
    def test_only_one_revision_exists(self) -> None:
        """This is the precondition that makes generating 0001's DDL from ``schema_contract`` safe.

        A landed Alembic revision is normally immutable text; 0001 renders itself from the current
        contract instead, which is only sound while it is the single revision and the contract therefore
        describes exactly what it created. When a second revision lands, this test fails — and the fix
        is to freeze 0001's DDL into literal text and express the change as its own revision (R-26), not
        to relax the assertion.
        """
        files = revision_files()
        assert len(files) == 1, (
            "more than one revision exists. Freeze 0001_audit_event.py's DDL into literal SQL and "
            f"express the change as its own revision. Found: {[f.name for f in files]}"
        )

    def test_head_revision_identifies_itself_readably(
        self, head_revision: ModuleType
    ) -> None:
        """``migration_head`` is reported by ``GET /api/v1/version`` as part of the parity set, and
        somebody has to compare two tiers by eye."""
        assert head_revision.revision == "0001_audit_event"
        assert head_revision.down_revision is None

    def test_alembic_is_not_imported_at_module_scope(self) -> None:
        """The deny-list must be assertable with the standard library alone. A privacy control that
        only runs in the lane with Alembic, SQLAlchemy, and a PostgreSQL service container is one that
        gets skipped the first time that lane is slow."""
        source = revision_files()[0].read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import alembic", "from alembic")):
                pytest.fail(f"module-scope alembic import: {line!r}")
        assert "    from alembic import op" in source

    def test_autogenerate_is_structurally_unavailable(self) -> None:
        """``alembic revision --autogenerate`` against empty metadata emits ``DROP TABLE audit_event``.
        There is no SQLAlchemy model of this table on purpose — a declarative model would be a second
        place the column list lives — so autogenerate is refused rather than merely discouraged."""
        env = (Path(sc.__file__).parent / "env.py").read_text(encoding="utf-8")
        assert "target_metadata = None" in env
        assert (
            "autogenerate" in env.lower()
        ), "env.py must document why autogenerate is unavailable"

    def test_downgrade_states_the_recovery_path(
        self, head_revision: ModuleType
    ) -> None:
        """Dropping this table destroys evidence, and the chain key must not be rotated to match. A
        downgrade docstring that only describes DDL leaves the operator to work that out live."""
        doc = head_revision.downgrade.__doc__ or ""
        assert "snapshot" in doc.lower()
        assert "rotat" in doc.lower()
