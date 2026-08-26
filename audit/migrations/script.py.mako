"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

WHY (delete this heading, keep the content):
  A one-line "add column X" message is not enough for this table. Every revision after
  0001_audit_event must answer, in this docstring:

  1. Does this change the column set of ``audit_event``? If so, ``technical-design.md`` §5.1 and
     ``audit/migrations/schema_contract.py`` are updated in the SAME commit, and the exact-allow-list
     test in ``audit/tests/`` is expected to fail until they are. That test failing is the control
     working, not a nuisance.
  2. Does it change what is hashed? Adding a column to ``app.audit.chain.CHAIN_FIELDS`` is a BREAKING
     change: bump ``CHAIN_FIELD_SET_VERSION`` and write the re-anchor procedure down (rules.md R-27).
     Every historical hash becomes unverifiable otherwise.
  3. Does the new column name contain any of ``audio``, ``pcm``, ``waveform``, ``transcript``,
     ``embedding``, ``phone``, ``msisdn``, ``caller_name``, ``raw``? Then it does not land
     (rules.md R-15). ``gateway/app/audit/chain.py`` will refuse to import and
     ``audit/tests/test_deny_list.py`` will fail; both are deliberate.
  4. Is it reversible without destroying evidence? ``downgrade`` on this table is a data-loss
     operation. Say what the recovery path is.
"""

from __future__ import annotations

${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: None = ${repr(branch_labels)}
depends_on: None = ${repr(depends_on)}


def upgrade() -> None:
    # Deferred import: audit/tests imports revision modules to assert the DDL statically, with only
    # the standard library available. See 0001_audit_event.py's docstring.
    from alembic import op  # noqa: F401

    ${upgrades if upgrades else "raise NotImplementedError('describe the upgrade')"}


def downgrade() -> None:
    from alembic import op  # noqa: F401

    ${downgrades if downgrades else "raise NotImplementedError('state the recovery path, not just the DDL')"}
