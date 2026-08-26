"""audit_event — the feature-only, hash-chained evidence table

Revision ID: 0001_audit_event
Revises: None
Create Date: 2026-08-26

The initial and, in Phase 1, the only revision. It creates exactly one table, and the reason it
creates only one is worth stating: ``technical-design.md`` §5.1 is titled "the complete list", and the
deny-list test asserts the allow-list as an exact SET. A second table is not forbidden, but it is a
deliberate act that must extend §5.1 first — which is why the retention worker records its receipts as
structured log output rather than into an ``audit_retention_run`` table (see ``audit/README.md``).

The DDL is generated from :mod:`schema_contract` rather than written out here. That is unusual for
Alembic, where a landed revision is normally immutable text, and it is only safe while this is the
sole revision — so ``audit/tests/test_schema_allow_list.py::test_only_one_revision_exists`` asserts
that precondition. The moment a second revision lands, that test fails and forces this file's DDL to
be frozen into literal text, with the change expressed as its own revision (R-26). A guard that
enforces its own precondition is the only kind worth relying on.

``from alembic import op`` is deliberately deferred into :func:`upgrade` / :func:`downgrade` instead
of sitting at module scope. It lets ``audit/tests`` import this module — and therefore assert the
generated DDL — with nothing installed but the standard library. The §5.2 deny-list is a privacy
control; a privacy control that only runs in the CI lane which has Alembic, SQLAlchemy, and a
PostgreSQL service container is a privacy control that gets skipped the first time that lane is slow.
"""

from __future__ import annotations

from typing import Final

from schema_contract import (
    CHECK_CONSTRAINTS,
    COLUMNS,
    INDEXES,
    TABLE_NAME,
    UNIQUE_CONSTRAINTS,
)

# Alembic identifiers. ``migration_head`` is part of the parity set reported by ``GET /api/v1/version``
# (contracts/openapi.yaml#/components/schemas/VersionInfo), so the revision id is a readable slug
# rather than a random hex string: a human comparing two tiers has to be able to read it aloud.
revision: str = "0001_audit_event"
down_revision: str | None = None
branch_labels: None = None
depends_on: None = None


def _create_table_sql() -> str:
    """Render the CREATE TABLE, including every constraint, as one statement.

    One statement rather than a CREATE followed by ALTER ... ADD CONSTRAINT calls: DDL in PostgreSQL
    is transactional, so both are atomic, but a single statement cannot leave a reviewer wondering
    whether a constraint further down the file was actually reached.
    """
    body: list[str] = [f"    {column.ddl}" for column in COLUMNS]
    body.append(f"    CONSTRAINT pk_{TABLE_NAME} PRIMARY KEY (event_id)")
    body.extend(
        f"    CONSTRAINT {c.name} {c.definition}" for c in UNIQUE_CONSTRAINTS
    )
    body.extend(
        f"    CONSTRAINT {c.name} CHECK ({c.definition})" for c in CHECK_CONSTRAINTS
    )
    return f"CREATE TABLE {TABLE_NAME} (\n" + ",\n".join(body) + "\n)"


#: Comments live in the database, not only in this file. ``\\d+ audit_event`` in a psql session during
#: an incident is where someone will ask "can this column hold audio?", and the answer should be there.
COMMENTS: Final[tuple[tuple[str, str], ...]] = (
    (
        f"TABLE {TABLE_NAME}",
        "Feature-only, hash-chained audit evidence. NO raw audio, transcript, or embedding may ever "
        "be stored here or in any other table (rules.md R-14, R-15; technical-design.md 5.2). The "
        "column list is an exact allow-list asserted by audit/tests.",
    ),
    (
        f"COLUMN {TABLE_NAME}.call_ref",
        "HMAC-SHA256 pseudonym of client_call_ref, 64 lowercase hex. The raw reference never leaves "
        "Gateway process memory (rules.md R-16); the CHECK constraint is the last boundary before a "
        "raw value would become durable.",
    ),
    (
        f"COLUMN {TABLE_NAME}.tenant_id",
        "Decision D-7: present from Phase 1 for forward-compatible row-level security. RLS is NOT "
        "enabled — describing this as multi-tenancy today would be a target-as-complete claim (R-01).",
    ),
    (
        f"COLUMN {TABLE_NAME}.prev_event_hash",
        "32 bytes. Genesis is 32 x 0x00. HMAC chain per session, ordered by event_seq "
        "(technical-design.md 5.3).",
    ),
    (
        f"COLUMN {TABLE_NAME}.retention_expires_at",
        "Excluded from the hash input so a retention-policy change cannot invalidate history. "
        "Retention deletes WHOLE SESSIONS only; see audit/retention_worker.py.",
    ),
)


def upgrade() -> None:
    from alembic import op

    op.execute(_create_table_sql())
    for index in INDEXES:
        op.execute(f"CREATE INDEX {index.name} ON {TABLE_NAME} {index.columns}")
    for target, text in COMMENTS:
        # Doubled single quotes: these strings contain apostrophes only via the words above, but a
        # future edit that adds one must not become an injection point in a migration.
        op.execute(f"COMMENT ON {target} IS '{text.replace(chr(39), chr(39) * 2)}'")


def downgrade() -> None:
    """Drop the table.

    A downgrade here destroys audit evidence, which is why it exists but is not a routine operation:
    re-running ``upgrade`` afterwards produces an empty chain, and the chain key must NOT be rotated
    to match (technical-design.md 5.3 — rotation makes every historical row read as tampered).
    Restoring from a snapshot is the supported recovery path; this is for a local teardown.
    """
    from alembic import op

    op.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
