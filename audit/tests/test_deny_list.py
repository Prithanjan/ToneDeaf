"""The §5.2 structural deny-list — the privacy control, asserted rather than promised.

``technical-design.md`` §5.2 is titled "enforced, not promised", and this file is where that claim is
either true or marketing. The five rules are transcribed literally at the top so a diff against the
design document is possible; :mod:`schema_contract` is not consulted for the expected answer.

Two things distinguish these tests from a checklist:

*They test the detector, not just today's schema.* Every rule except rule 5 is currently vacuous — the
allow-list contains no forbidden name, no stray ``bytea``, no vector. A test that only asserts
"``deny_list_violations(declared) == []``" therefore passes even if the detector is a function that
returns ``[]`` unconditionally. :class:`TestTheDetectorCatchesRealisticMigrations` feeds it the columns
a future migration would plausibly add and asserts each one is caught, with the rule it trips.

*They compare against ``gateway/app/audit/chain.py`` instead of duplicating it.* The two deny-lists live
in different packages and must not drift; the relationship is asserted in the direction that matters.

Rules 1-4 are asserted against every table in the schema, not only ``audit_event``. An audio column is
forbidden wherever it appears, and a future ``session_note`` table is exactly where it would appear.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import schema_contract as sc
from app.audit.chain import CHAIN_FIELDS, EXCLUDED_FIELDS, _FORBIDDEN_SUBSTRINGS
from tests.conftest import DATABASE_URL_ENV, MIGRATIONS_DIR

# --------------------------------------------------------------------------------------------------
# Independent transcription of technical-design.md §5.2
# --------------------------------------------------------------------------------------------------

#: Rule 1, verbatim: no column name matching %audio%, %pcm%, %waveform%, %transcript%, %embedding%,
#: %phone%, %msisdn%, %caller_name%, %raw%.
SPEC_RULE_1: Final[frozenset[str]] = frozenset(
    {
        "audio",
        "pcm",
        "waveform",
        "transcript",
        "embedding",
        "phone",
        "msisdn",
        "caller_name",
        "raw",
    }
)

#: Rule 2, verbatim: no ``bytea`` column other than ``prev_event_hash`` and ``event_hash``.
SPEC_RULE_2: Final[frozenset[str]] = frozenset({"prev_event_hash", "event_hash"})

#: Rule 4, verbatim: no column wider than 512 bytes that is not on the allow-list.
SPEC_RULE_4_BYTES: Final[int] = 512


class TestRule1ForbiddenNames:
    def test_the_substring_list_is_exactly_what_the_design_names(self) -> None:
        assert set(sc.FORBIDDEN_COLUMN_SUBSTRINGS) == SPEC_RULE_1, {
            "extra": sorted(set(sc.FORBIDDEN_COLUMN_SUBSTRINGS) - SPEC_RULE_1),
            "missing": sorted(SPEC_RULE_1 - set(sc.FORBIDDEN_COLUMN_SUBSTRINGS)),
        }

    @pytest.mark.privacy
    def test_no_declared_column_trips_rule_1(self) -> None:
        assert sc.forbidden_substring_hits(sc.COLUMN_NAMES) == []

    @pytest.mark.privacy
    def test_matching_is_case_insensitive_and_substring_not_exact(self) -> None:
        """An exact-name deny-list is one a rename walks around: ``audio_blob`` is refused,
        ``audioBlob`` is not, and the reviewer sees a green tick."""
        assert sc.forbidden_substring_hits(["AudioBlob"]) == [("AudioBlob", "audio")]
        assert sc.forbidden_substring_hits(["window_pcm_v2"]) == [("window_pcm_v2", "pcm")]
        assert sc.forbidden_substring_hits(["CALLER_NAME"]) == [("CALLER_NAME", "caller_name")]

    @pytest.mark.privacy
    def test_a_clean_name_is_not_flagged(self) -> None:
        """A deny-list that flags everything gets disabled within a week."""
        assert sc.forbidden_substring_hits(["spoof_risk", "quality_flags", "event_seq"]) == []


class TestChainPyAgreement:
    """The Gateway and the database must refuse the same names.

    ``chain.py`` runs its list over ``CHAIN_FIELDS`` at import time, so a forbidden *field* cannot even
    be loaded. That guard covers what gets hashed. It does not cover what gets *stored*: a column that
    is never hashed is never seen by ``_assert_not_forbidden``, and the database is the layer where the
    mistake is permanent — a column, once created and written to, cannot be un-created without
    destroying evidence.
    """

    def test_chain_pys_list_is_contained_in_the_database_list(self) -> None:
        """The load-bearing direction. If someone adds a substring to ``chain.py``, the database layer
        must already refuse it — otherwise the tighter guard is the one that never sees the column."""
        assert set(_FORBIDDEN_SUBSTRINGS) <= set(sc.FORBIDDEN_COLUMN_SUBSTRINGS), (
            "gateway/app/audit/chain.py refuses names this schema would accept: "
            f"{sorted(set(_FORBIDDEN_SUBSTRINGS) - set(sc.FORBIDDEN_COLUMN_SUBSTRINGS))}"
        )

    def test_the_only_tolerated_gap_is_raw(self) -> None:
        """A known, deliberate divergence, asserted tolerantly so it can be fixed without a failure.

        ``chain.py::_FORBIDDEN_SUBSTRINGS`` omits ``raw``, which §5.2 rule 1 and R-15 both require.
        ``chain.py`` is not this component's file, so the omission is recorded rather than patched, and
        the database list is a deliberate superset. This assertion fails if ``chain.py`` drops any
        *other* substring, and keeps passing on the day ``raw`` is added there.
        """
        gap = set(sc.FORBIDDEN_COLUMN_SUBSTRINGS) - set(_FORBIDDEN_SUBSTRINGS)
        assert gap <= {"raw"}, f"chain.py has lost deny-list coverage for {sorted(gap - {'raw'})}"

    @pytest.mark.privacy
    def test_raw_is_refused_at_the_database_layer_regardless(self) -> None:
        """Which is the point of the superset: ``raw_audio_path`` is caught twice, ``raw_features`` —
        which contains no other forbidden substring — is caught only here."""
        assert sc.forbidden_substring_hits(["raw_features"]) == [("raw_features", "raw")]
        assert sc.forbidden_substring_hits(["raw_call_ref"]) == [("raw_call_ref", "raw")]

    @pytest.mark.parity
    def test_the_hashed_and_stored_field_sets_agree(self) -> None:
        """Restated here as well as in the allow-list suite, because a deny-list is only complete over
        a known column set: an unhashed extra column is both a rule-5 violation and a hole in rule 1's
        coverage as ``chain.py`` applies it."""
        assert set(CHAIN_FIELDS) | set(EXCLUDED_FIELDS) == set(sc.COLUMN_NAMES)


class TestRule2Bytea:
    @pytest.mark.privacy
    def test_only_the_two_hash_columns_are_bytea(self) -> None:
        """``bytea`` is the only declared type in PostgreSQL that can hold a waveform without looking
        like one — no name check catches ``window_features bytea``."""
        actual = {c.name for c in sc.COLUMNS if c.udt_name == "bytea"}
        assert actual == SPEC_RULE_2
        assert sc.PERMITTED_BYTEA_COLUMNS == SPEC_RULE_2

    @pytest.mark.privacy
    def test_both_permitted_bytea_columns_are_width_pinned(self) -> None:
        """Rule 2 permits two ``bytea`` columns, so the deny-list cannot help here; the CHECK does.
        32 bytes each, which is an HMAC-SHA256 digest and nothing else — 40 ms of 16 kHz mono PCM is
        1280 bytes, so a width-pinned column cannot hold even one window."""
        definitions = " ".join(c.definition for c in sc.CHECK_CONSTRAINTS)
        for column in sorted(SPEC_RULE_2):
            assert f"octet_length({column}) = 32" in definitions


class TestRule3VectorShapes:
    @pytest.mark.privacy
    def test_no_declared_column_has_a_vector_or_float_array_shape(self) -> None:
        assert {c.udt_name for c in sc.COLUMNS} & sc.FORBIDDEN_UDT_NAMES == set()

    @pytest.mark.privacy
    def test_the_pgvector_and_plain_array_spellings_are_both_covered(self) -> None:
        """An embedding arrives as ``vector`` where pgvector is installed and as ``real[]`` or
        ``double precision[]`` where it is not. Matching on the reflected ``udt_name`` makes the DDL
        spelling irrelevant, which is why ``float4[]`` and ``real[]`` need no separate entry."""
        assert {"vector", "halfvec", "sparsevec"} <= sc.FORBIDDEN_UDT_NAMES
        assert {"_float4", "_float8", "_numeric"} <= sc.FORBIDDEN_UDT_NAMES

    def test_the_one_permitted_array_is_not_numeric(self) -> None:
        """``quality_flags text[]`` is the only array on the allow-list. ``_text`` is deliberately not
        in the forbidden set, and deliberately is not a float array — a text array cannot carry a
        feature vector without every element being a decimal string, which the flag CHECK rejects."""
        arrays = {c.name: c.udt_name for c in sc.COLUMNS if c.udt_name.startswith("_")}
        assert arrays == {"quality_flags": "_text"}


class TestRule4Width:
    def test_the_threshold_is_512_bytes(self) -> None:
        assert sc.MAX_UNLISTED_COLUMN_BYTES == SPEC_RULE_4_BYTES

    @pytest.mark.privacy
    def test_rule_4_is_what_makes_a_jsonb_blob_impossible(self) -> None:
        """§5.2 has no rule reading "no JSONB". It does not need one, and this test pins down why.

        A JSONB column is the way a column-name deny-list gets defeated: the column is called
        ``debug_context`` and the waveform is a base64 string under a key inside it, where no
        ``information_schema`` query will ever see the word ``audio``. Rule 4 catches it structurally
        instead — ``jsonb`` has no ``character_maximum_length``, so it is unbounded, so it is wider than
        512 bytes, so it cannot exist unless §5.1 is amended first.
        """
        blob = sc.ColumnFact(sc.TABLE_NAME, "debug_context", "jsonb", "jsonb")
        violations = sc.deny_list_violations([*sc.declared_column_facts(), blob])
        assert any("rule 4" in v for v in violations), violations

    def test_no_json_or_xml_column_is_declared_today(self) -> None:
        assert {c.udt_name for c in sc.COLUMNS} & {"json", "jsonb", "xml"} == set()

    def test_a_bounded_narrow_column_is_allowed_by_rule_4(self) -> None:
        """Rule 4 bounds width; it does not ban new columns. Blurring the two would make the deny-list
        an argument against ever extending the schema, and it would lose the argument."""
        narrow = sc.ColumnFact(sc.TABLE_NAME, "channel_hint", "character varying", "varchar", 32)
        assert not any("rule 4" in v for v in sc.deny_list_violations([narrow]))


class TestRule5ExactSet:
    @pytest.mark.privacy
    def test_an_extra_column_fails(self) -> None:
        """The rule as written: "an unexpected *extra* column fails the test too"."""
        problems = sc.allow_list_violations([*sc.COLUMN_NAMES, "notes"])
        assert problems == [f"rule 5 (exact set): unexpected column {sc.TABLE_NAME}.notes"]

    def test_a_missing_column_fails(self) -> None:
        problems = sc.allow_list_violations([n for n in sc.COLUMN_NAMES if n != "event_hash"])
        assert problems == [f"rule 5 (exact set): missing column {sc.TABLE_NAME}.event_hash"]

    def test_both_directions_are_reported_together(self) -> None:
        """One CI run per violation turns a schema review into five CI runs."""
        mutated = [n for n in sc.COLUMN_NAMES if n != "event_hash"] + ["notes"]
        assert len(sc.allow_list_violations(mutated)) == 2

    def test_the_exact_set_holds_for_the_declared_schema(self) -> None:
        assert sc.allow_list_violations(sc.COLUMN_NAMES) == []


class TestTheDetectorCatchesRealisticMigrations:
    """Rules 1-4 are vacuous against today's schema, so this is where they are actually exercised.

    Each case is a column somebody would add for a defensible reason — debugging a false positive,
    caching a feature, making support calls easier to trace. Every one of them is a privacy incident,
    and the point of a structural deny-list is that none of them needs to be argued about in review.
    """

    @pytest.mark.privacy
    @pytest.mark.parametrize(
        ("fact", "expected_rule", "why_someone_would_add_it"),
        [
            (
                sc.ColumnFact("audit_event", "raw_audio_path", "text", "text"),
                "rule 1",
                "to re-listen to a window that scored oddly",
            ),
            (
                sc.ColumnFact("audit_event", "window_pcm", "bytea", "bytea"),
                "rule 1",
                "to replay the exact bytes the model saw",
            ),
            (
                sc.ColumnFact("audit_event", "transcript_snippet", "text", "text"),
                "rule 1",
                "to show an analyst what was said",
            ),
            (
                sc.ColumnFact("audit_event", "caller_name", "text", "text"),
                "rule 1",
                "to make the audit trail readable",
            ),
            (
                sc.ColumnFact("audit_event", "phone_number", "text", "text"),
                "rule 1",
                "to join against the CRM",
            ),
            (
                sc.ColumnFact("audit_event", "speaker_embedding", "ARRAY", "_float4"),
                "rule 1",
                "to cluster repeat callers",
            ),
            (
                sc.ColumnFact("audit_event", "features", "bytea", "bytea"),
                "rule 2",
                "a name the deny-list does not match, holding exactly what it forbids",
            ),
            (
                sc.ColumnFact("audit_event", "voiceprint", "USER-DEFINED", "vector"),
                "rule 3",
                "to add speaker matching later",
            ),
            (
                sc.ColumnFact("audit_event", "debug_context", "jsonb", "jsonb"),
                "rule 4",
                "to attach whatever turns out to be useful",
            ),
            (
                sc.ColumnFact("session_note", "agent_comment", "text", "text"),
                "rule 4",
                "a second table, where §5.1 was never consulted",
            ),
        ],
    )
    def test_a_plausible_future_column_is_refused(
        self, fact: sc.ColumnFact, expected_rule: str, why_someone_would_add_it: str
    ) -> None:
        violations = sc.deny_list_violations([*sc.declared_column_facts(), fact])
        assert any(expected_rule in v for v in violations), (
            f"{fact.column_name} ({why_someone_would_add_it}) was NOT refused by {expected_rule}. "
            f"Violations reported: {violations}"
        )

    @pytest.mark.privacy
    def test_the_dangerous_case_is_caught_at_import_not_at_test_time(self) -> None:
        """The genuinely dangerous commit adds the forbidden column to the allow-list *and* the
        migration together, so every per-column check agrees with it.

        :func:`schema_contract._self_check` runs the allow-list through the deny-list at import, exactly
        as ``chain.py`` does over ``CHAIN_FIELDS``, so such a commit cannot be imported — let alone
        migrated. The test below proves the guard has teeth by running it over a mutated list.
        """
        poisoned = [*sc.declared_column_facts(), sc.ColumnFact("audit_event", "audio_ref", "text", "text")]
        assert sc.deny_list_violations(poisoned), "the import-time self-check would not have fired"

    def test_a_clean_second_table_is_still_allowed(self) -> None:
        """Rules 1-4 apply schema-wide, which must not mean "no other table may exist". Alembic's own
        ``alembic_version`` lives in the same schema and has to survive the scan."""
        version_table = sc.ColumnFact(
            sc.ALEMBIC_VERSION_TABLE, "version_num", "character varying", "varchar", 32
        )
        assert sc.deny_list_violations([*sc.declared_column_facts(), version_table]) == []


class TestForbiddenActionValues:
    """R-07 across the whole migration directory, not just the CREATE TABLE.

    Scoped to SQL and template files rather than a naive grep: this suite's own source, and the
    schema-contract docstrings, name the forbidden words in order to forbid them.
    """

    @pytest.mark.privacy
    @pytest.mark.parametrize("banned", sc.FORBIDDEN_ACTION_VALUES)
    def test_no_migration_file_uses_an_authorization_verb_as_a_value(self, banned: str) -> None:
        offenders: list[str] = []
        for path in sorted(MIGRATIONS_DIR.rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "schema_contract.py":
                continue
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                if re.search(rf"""['"]{banned}['"]""", line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, (
            f"{banned!r} appears as a literal. This system emits proportionate verification pressure "
            f"(continue/verify/hold/escalate); an authorization verb in the evidence table would let a "
            f"reader conclude the system approved or blocked a transaction (rules.md R-07):\n"
            + "\n".join(offenders)
        )

    def test_schema_contract_names_them_only_to_forbid_them(self) -> None:
        """The one exempted file, checked rather than trusted: the words may appear in
        ``FORBIDDEN_ACTION_VALUES`` and nowhere that a CHECK constraint would accept them."""
        for banned in sc.FORBIDDEN_ACTION_VALUES:
            assert banned not in sc.ACTION_VOCABULARY
            assert banned not in sc.RISK_STATE_VOCABULARY
            for constraint in sc.CHECK_CONSTRAINTS:
                assert f"'{banned}'" not in constraint.definition, constraint.name


class TestReflectionSql:
    """The ``integration`` tests below cannot run here, so the SQL they depend on is asserted as text.

    A privacy gate whose query is wrong reports green forever. These are cheap and they are the only
    coverage the reflection queries get in a lane without PostgreSQL.
    """

    def test_the_column_scan_covers_every_table_not_just_audit_event(self) -> None:
        """A scan filtered to ``audit_event`` would miss the ``session_note`` table someone adds in
        Phase 2, which is where a transcript column would actually land."""
        assert "table_name" in sc.REFLECT_COLUMNS_SQL
        assert "audit_event" not in sc.REFLECT_COLUMNS_SQL

    def test_the_scan_follows_the_search_path_rather_than_hardcoding_public(self) -> None:
        """``table_schema = 'public'`` turns the gate into a no-op the moment a migration runs into a
        non-default schema, and it still reports green."""
        assert "current_schema()" in sc.REFLECT_COLUMNS_SQL
        assert "'public'" not in sc.REFLECT_COLUMNS_SQL

    def test_constraint_reflection_reads_postgres_rendering_not_the_migration_file(self) -> None:
        """``pg_get_constraintdef`` is the deployed truth. Asserting against the migration text proves
        only that the file says what the file says."""
        assert "pg_get_constraintdef" in sc.REFLECT_CONSTRAINTS_SQL

    def test_reflection_queries_are_parameterised(self) -> None:
        """asyncpg positional parameters, not f-strings — these are run by a verifier that may be
        pointed at a table name from configuration."""
        assert "$1" in sc.REFLECT_CONSTRAINTS_SQL
        assert "$1" in sc.REFLECT_INDEXES_SQL


class TestDeployedSchema:
    """Phase-1 exit criterion: "Schema deny-list: structural assertion against information_schema"
    (``technical-design.md`` §9). UNVERIFIED in this environment — no PostgreSQL 16 is available."""

    @pytest.mark.integration
    @pytest.mark.privacy
    def test_no_deployed_column_anywhere_trips_rules_1_to_4(self, database_url: str) -> None:
        pytest.skip(f"needs a live database via {DATABASE_URL_ENV}; see audit/README.md")

    @pytest.mark.integration
    @pytest.mark.privacy
    def test_deployed_check_constraints_match_the_migration(self, database_url: str) -> None:
        pytest.skip(f"needs a live database via {DATABASE_URL_ENV}; see audit/README.md")

    @pytest.mark.integration
    def test_deployed_indexes_match_the_migration(self, database_url: str) -> None:
        pytest.skip(f"needs a live database via {DATABASE_URL_ENV}; see audit/README.md")


def test_the_deny_list_covers_the_files_that_exist() -> None:
    """A guard on this suite itself.

    Every assertion here reads ``audit/migrations``. If the directory is renamed or the revision moves,
    the loops above iterate over nothing and pass. This test makes an empty sweep fail.
    """
    assert MIGRATIONS_DIR.is_dir(), MIGRATIONS_DIR
    scanned = [p for p in MIGRATIONS_DIR.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(scanned) >= 3, f"expected env.py, schema_contract.py and a revision; found {scanned}"
    assert (Path(MIGRATIONS_DIR) / "versions").is_dir()
