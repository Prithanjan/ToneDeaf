"""Alembic environment for the audit schema.

Three decisions, each preventing a specific 2 a.m. failure:

* **No ``target_metadata``, no autogenerate.** There is no SQLAlchemy model of ``audit_event``
  anywhere in this repo, and there deliberately is not one. A declarative model would be a second
  place the column list lives, and the whole point of ``technical-design.md`` §5.1 being an exact
  allow-list is that there is one place to review. ``alembic revision --autogenerate`` against an
  empty metadata would happily emit ``DROP TABLE audit_event``, so autogenerate is refused outright
  rather than left available and documented as "don't use it".

* **The URL comes from ``DATABASE_URL``, and the driver is normalized.** The Gateway pins ``asyncpg``
  and no synchronous PostgreSQL driver (``gateway/requirements.txt``), so a plain
  ``postgresql://`` URL — which is exactly what RDS hands you and what
  ``infra/compose`` sets — would fail with ``ModuleNotFoundError: psycopg2``. That error names the
  wrong problem. It is rewritten to ``postgresql+asyncpg://`` here, once, with a log line.

* **Offline mode emits SQL but is not the deployment path.** ``--sql`` is for review: a reviewer can
  read the DDL that will run before it runs against a database holding evidence.
"""

from __future__ import annotations

import asyncio
import os
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No metadata: see the module docstring. Autogenerate is not available and must not become available.
target_metadata = None

_SYNC_SCHEMES = re.compile(r"^postgres(?:ql)?(?:\+[a-zA-Z0-9_]+)?://")


def _database_url() -> str:
    """Resolve the URL, preferring the environment over the ini file.

    ``alembic.ini`` deliberately ships without a URL. A committed connection string is either wrong
    on every tier or a secret in Git (rules.md R-34); both are worse than failing to start.
    """
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Alembic ships no default connection string on purpose: "
            "a committed URL is either wrong on every tier or a secret in Git (rules.md R-34)."
        )
    normalized = _SYNC_SCHEMES.sub("postgresql+asyncpg://", url)
    if normalized != url:
        # Printed, not silent. A driver rewrite that nobody can see is a rewrite nobody can debug.
        print(
            f"[alembic] rewrote the URL scheme to asyncpg (the only pinned driver): {url.split('://')[0]}:// -> postgresql+asyncpg://"
        )
    return normalized


def run_migrations_offline() -> None:
    """Render the migration as SQL on stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # The version table lives in the same schema as audit_event so that the deny-list scan, which
        # walks current_schema(), sees it. It is exempt from the exact-allow-list assertion and
        # explicitly NOT exempt from the forbidden-name and width rules.
        compare_type=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        # A leaked pool keeps an RDS connection slot open after the task exits, and the Gateway's
        # pool is sized on the assumption that migrations released theirs.
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
