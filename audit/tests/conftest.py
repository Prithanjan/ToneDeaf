"""Fixtures for the audit database-layer tests.

These tests cover the layer ``gateway/tests/test_audit_chain.py`` cannot reach. That suite proves the
chain *mathematics* catches an edit, a deletion, a reorder, and a re-key. It says nothing about whether
the table those rows live in can hold a waveform, whether ``action`` will accept ``'deny'``, or whether
the column set still matches what the writer inserts. Those are properties of the DDL, and they are
what this directory asserts. There is deliberately no overlap.

**Everything importable here works with the standard library alone.** There is no PostgreSQL on a
developer laptop and none in the fast CI lane, so the §5.2 deny-list, the exact allow-list, and the
CHECK vocabulary are asserted against the DDL the migration *generates*. Tests that need a live
database carry ``@pytest.mark.integration`` and are skipped unless ``AUDIT_TEST_DATABASE_URL`` is set —
they are the Phase-1 exit criterion (``technical-design.md`` §9, "Schema deny-list: structural
assertion against information_schema") and the static tests are what makes the same control run on
every commit rather than only where a database exists.

Path setup is explicit because this directory is not inside an installed package: ``audit/migrations``
(for ``schema_contract`` and the revision modules), ``audit`` (for ``retention_worker``), and
``gateway`` (for ``app.audit.chain``, so the two deny-lists can be compared rather than trusted).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
AUDIT_DIR: Final[Path] = REPO_ROOT / "audit"
MIGRATIONS_DIR: Final[Path] = AUDIT_DIR / "migrations"
VERSIONS_DIR: Final[Path] = MIGRATIONS_DIR / "versions"
GATEWAY_DIR: Final[Path] = REPO_ROOT / "gateway"
POLICY_DIR: Final[Path] = REPO_ROOT / "policy"
CONTRACTS_DIR: Final[Path] = REPO_ROOT / "contracts"

for _path in (MIGRATIONS_DIR, AUDIT_DIR, GATEWAY_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

#: Set to a PostgreSQL URL to run the ``integration`` tests. Kept separate from ``DATABASE_URL`` on
#: purpose: a test suite that picks up whatever ``DATABASE_URL`` happens to be exported will one day
#: run its DDL assertions — and its retention deletes — against a database holding real evidence.
DATABASE_URL_ENV: Final[str] = "AUDIT_TEST_DATABASE_URL"


def pytest_configure(config: pytest.Config) -> None:
    """Register the markers locally.

    ``gateway/pyproject.toml`` defines these for the Gateway suite, but this directory sits outside it
    and pytest resolves one ini file per run. Registering here means ``-p no:cacheprovider --strict-markers``
    behaves the same whether the suite is invoked from the repo root or from ``audit/``.
    """
    for marker, description in (
        ("integration", "needs PostgreSQL; skipped unless AUDIT_TEST_DATABASE_URL is set"),
        ("privacy", "asserts a privacy control; failure is a RELEASE blocker (rules.md R-14..R-19)"),
        ("contract", "asserts agreement with contracts/; Phase 1 exit criteria"),
        ("parity", "asserts a parity-set property; failure is a DEPLOY blocker"),
    ):
        config.addinivalue_line("markers", f"{marker}: {description}")


def _load_module(path: Path, name: str) -> ModuleType:
    """Import a module by file path.

    Revision modules are not importable by name — Alembic loads them by path, and their filenames start
    with a digit. Loading them the same way here means the tests assert the DDL of the file Alembic will
    actually run, not a copy of it.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing file is the assertion
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def revision_files() -> list[Path]:
    """Every revision module Alembic would pick up, sorted by filename."""
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))


@pytest.fixture(scope="session")
def head_revision() -> ModuleType:
    """The single Phase-1 revision module, imported without Alembic installed.

    Possible only because ``0001_audit_event.py`` defers ``from alembic import op`` into ``upgrade()``.
    See that file's docstring for why that is a deliberate choice and not a workaround.
    """
    files = revision_files()
    assert files, "no revision files found; the audit schema migration is missing"
    return _load_module(files[-1], "audit_head_revision")


@pytest.fixture(scope="session")
def create_table_sql(head_revision: ModuleType) -> str:
    """The exact ``CREATE TABLE`` statement the migration will execute."""
    return str(head_revision._create_table_sql())


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        pytest.skip(
            f"{DATABASE_URL_ENV} is not set. The integration assertions against information_schema "
            "are the Phase-1 exit criterion and are UNVERIFIED without a PostgreSQL 16 instance."
        )
    return url
