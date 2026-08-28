#!/usr/bin/env python3
"""Audit hash-chain verifier — the operator tool.

    # against a database
    export AUDIT_CHAIN_KEY=...            # never a CLI argument; see below
    python scripts/verify_audit_chain.py --dsn "$DATABASE_URL"
    python scripts/verify_audit_chain.py --dsn "$DATABASE_URL" --session <uuid>

    # against an exported row dump, no database access required
    python scripts/verify_audit_chain.py --events-file rows.json

    # prove the verifier itself works, then trust its verdict
    python scripts/verify_audit_chain.py --self-test

**This tool computes nothing.** Canonicalization and the HMAC step are imported from
``gateway/app/audit/chain.py`` — the same functions ``app.audit.writer`` used to produce the stored
hashes. That is the whole design. A verifier with its own second implementation of canonicalization
is a verifier that disagrees with the writer: it would report a healthy chain as tampered (and get
ignored), or an altered chain as healthy (and be worse than nothing). The one property that must hold
is *bit-identical* canonical bytes, and the only reliable way to get it is to not have a second copy.

**It reports the first divergent ``event_seq``, per session, not a count.** ``verify_chain`` returns
the first divergence because localizing the alteration is the property under test — "the chain is
invalid" is a checksum result. Knowing that session ``…4f2a`` diverges at ``event_seq=17`` tells an
operator which decision is in question and that events 0–16 are still sound.

**Chains are per session.** ``app.audit.writer`` seeds every session from ``GENESIS_PREV_HASH`` and
advances a per-session head under a per-session lock, so verification groups by ``session_id`` and
recomputes each group from genesis. Verifying the table as one global sequence would report every
session boundary as tampering.

**The key comes from the environment, never from ``argv``.** A command-line argument lands in shell
history, in ``ps`` output, and in a CI log if the command is echoed. ``AUDIT_CHAIN_KEY`` must never be
rotated while audit events exist — rotation does not invalidate old rows, it makes every one of them
fail verification, which is indistinguishable from tampering (rules.md R-58,
aws-setup-instructions.md section 6). If this tool reports every session failing at ``event_seq=0``,
suspect the key before suspecting an attacker.

Exit codes: ``0`` every chain verified · ``1`` at least one divergence · ``2`` the check could not run.
``2`` is distinct because "I could not verify" must never be mistaken for "verified".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, NoReturn, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "gateway"))

try:
    from app.audit.chain import (
        CHAIN_FIELDS,
        ChainFieldError,
        VerificationResult,
        verify_chain,
    )
    from app.constants import CHAIN_FIELD_SET_VERSION, GENESIS_PREV_HASH
except (
    ImportError
) as exc:  # pragma: no cover - environment problem, not a chain problem
    print(
        f"verify_audit_chain: FATAL: cannot import the chain implementation ({exc}).\n"
        "Run from the repository root. This tool deliberately has no fallback: a second "
        "canonicalization would disagree with the writer (rules.md R-27).",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


def _die(message: str) -> "NoReturn":
    """Exit 2, not 1.

    ``raise SystemExit("text")`` prints the text but exits **1** — the same code this tool uses for
    "the chain diverged". Conflating "I could not verify the audit log" with "the audit log was
    altered" is the one mistake an integrity tool must not make.
    """
    print(message, file=sys.stderr)
    raise SystemExit(2)


#: Read explicitly, never ``SELECT *`` — for the same reason ``CHAIN_FIELDS`` is explicit (decision
#: D-9). With ``SELECT *`` a later additive migration would silently add a key to every event dict,
#: ``canonicalize`` would reject it as an unknown field, and a schema change would present as
#: tampering across the whole table.
_SELECT_COLUMNS = (*CHAIN_FIELDS, "prev_event_hash", "event_hash")


# --------------------------------------------------------------------------------------------------
# Normalization of stored/exported values
# --------------------------------------------------------------------------------------------------


def _as_bytes(value: Any, *, field: str) -> bytes:
    """Accept the three shapes a 32-byte hash arrives in: bytes, memoryview, or a hex string.

    A JSON export cannot carry ``bytea``, so hex is what ``psql -t -A -c "... encode(h,'hex')"`` and
    most dump tools produce. Length is checked here rather than deep inside the HMAC step so the error
    names the column.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    elif isinstance(value, str):
        text = value[2:] if value.startswith("\\x") else value
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            raise ChainFieldError(f"{field} is not valid hex: {value[:24]!r}…") from exc
    else:
        raise ChainFieldError(f"{field} has unexpected type {type(value).__name__}")
    if len(raw) != 32:
        raise ChainFieldError(f"{field} must be 32 bytes, got {len(raw)}")
    return raw


def _normalize_event(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce one exported row into what ``canonicalize`` expects.

    ``occurred_at`` gets parsed back into a ``datetime`` rather than passed through as a string,
    because ``chain._format_timestamp`` returns a string unchanged. Passing an ISO string with a
    ``+00:00`` offset straight through would hash a different byte sequence than the writer's
    ``…Z``-suffixed microsecond form, and every row would appear tampered. Re-parsing hands the
    formatting decision back to chain.py, which is the only module allowed to make it.
    """
    event = dict(row)

    occurred = event.get("occurred_at")
    if isinstance(occurred, str):
        text = occurred.strip()
        try:
            event["occurred_at"] = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            # Leave it alone: chain.py accepts a pre-formatted string, and a value this tool cannot
            # parse may still be exactly what the writer hashed.
            pass

    for field in ("prev_event_hash", "event_hash"):
        if field in event and event[field] is not None:
            event[field] = _as_bytes(event[field], field=field)

    if event.get("quality_flags") is None:
        event["quality_flags"] = []

    return event


def _group_by_session(
    rows: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group rows by session and order each group by ``event_seq``.

    Sorting here rather than trusting the input order matters for the file path: a hand-edited or
    concatenated export can arrive out of order, and an out-of-order chain fails verification for a
    reason that has nothing to do with the data's integrity.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("session_id")), []).append(row)
    for events in grouped.values():
        events.sort(key=lambda e: int(e.get("event_seq", 0)))
    return grouped


# --------------------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------------------


async def _rows_from_db(dsn: str, session: str | None) -> list[dict[str, Any]]:
    try:
        import asyncpg  # noqa: PLC0415 — only the DB path needs it
    except ImportError:
        _die(
            "verify_audit_chain: FATAL: asyncpg is not installed. Install "
            "gateway/requirements.txt, or export the rows and use --events-file."
        )

    columns = ", ".join(_SELECT_COLUMNS)
    conn = await asyncpg.connect(dsn)
    try:
        if session is not None:
            from uuid import UUID  # noqa: PLC0415

            records = await conn.fetch(
                f"SELECT {columns} FROM audit_event WHERE session_id = $1 ORDER BY event_seq ASC",
                UUID(session),
            )
        else:
            # session_id first so the sort groups sessions contiguously; event_seq orders within.
            records = await conn.fetch(
                f"SELECT {columns} FROM audit_event ORDER BY session_id, event_seq ASC"
            )
    finally:
        await conn.close()
    return [dict(r) for r in records]


def _rows_from_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        # Tolerate `{"events": [...]}` and `{"rows": [...]}` wrappers from ad-hoc export scripts.
        for key in ("events", "rows", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        _die(
            "verify_audit_chain: FATAL: --events-file must contain a JSON array of audit rows "
            "(or an object with an 'events' array)."
        )
    return [dict(r) for r in payload]


# --------------------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------------------


def verify_rows(
    chain_key: bytes, rows: Sequence[dict[str, Any]]
) -> tuple[int, list[dict[str, Any]]]:
    """Verify every session's chain. Returns ``(exit_code, per_session_results)``."""
    grouped = _group_by_session(rows)
    results: list[dict[str, Any]] = []
    failed = 0

    for session_id, raw_events in sorted(grouped.items()):
        try:
            events = [_normalize_event(e) for e in raw_events]
        except ChainFieldError as exc:
            failed += 1
            results.append(
                {
                    "session_id": session_id,
                    "events": len(raw_events),
                    "ok": False,
                    "first_bad_event_seq": None,
                    "detail": f"row could not be read: {exc}",
                }
            )
            continue

        try:
            outcome: VerificationResult = verify_chain(
                chain_key, events, genesis=GENESIS_PREV_HASH
            )
        except ChainFieldError as exc:
            # canonicalize() refused the row: a missing, extra, or forbidden field. That is a schema
            # or migration problem, not necessarily tampering, and saying so is the difference
            # between an operator checking Alembic and an operator declaring an incident.
            failed += 1
            results.append(
                {
                    "session_id": session_id,
                    "events": len(events),
                    "ok": False,
                    "first_bad_event_seq": None,
                    "detail": (
                        f"canonical field set rejected the row: {exc}. Check the migration head "
                        f"against CHAIN_FIELD_SET_VERSION={CHAIN_FIELD_SET_VERSION} (rules.md R-27) "
                        "before treating this as tampering."
                    ),
                }
            )
            continue

        if not outcome.ok:
            failed += 1
        results.append(
            {
                "session_id": session_id,
                "events": len(events),
                "ok": outcome.ok,
                "first_bad_event_seq": outcome.first_bad_event_seq,
                "first_bad_index": outcome.first_bad_index,
                "detail": outcome.detail,
            }
        )

    return (1 if failed else 0), results


# --------------------------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------------------------


def self_test() -> int:
    """Build a chain, verify it, tamper with it, and assert the reported seq is the tampered one.

    An operator feature, not just a unit test: before you rely on a "chain verified" line from a tool
    you have never seen fail, you want to watch it fail on purpose. It also runs without a database,
    which is what makes it usable at 2 a.m. from a laptop.
    """
    from app.audit.chain import chain_events  # noqa: PLC0415

    key = b"self-test-key-not-a-real-secret-0000"
    base = {
        "tenant_id": "demo-tenant",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "call_ref": "a" * 64,
        "occurred_at": datetime.fromisoformat("2026-08-26T12:00:00+00:00"),
        "purpose_code": "payment_release",
        "context_value_band": "medium",
        "window_seq": 0,
        "spoof_risk": "0.5000",
        "risk_state": "collecting",
        "action": "continue",
        "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
        "policy_version": "0.1.0-placeholder",
        "policy_bundle_sha256": "b" * 64,
        "model_version": "mock-0",
        "model_sha256": "c" * 64,
        "calibration_version": "0.0.0-placeholder",
        "calibration_sha256": "d" * 64,
        "quality_flags": [],
        "detector_mode": "MOCK_SMOKE_MODE_NOT_A_DETECTOR",
        "execution_provider": "CPUExecutionProvider",
        "deployment_profile": "local-cpu",
    }
    events = [{**base, "event_seq": seq, "window_seq": seq} for seq in range(5)]
    for event, (prev, digest) in zip(events, chain_events(key, events)):
        event["prev_event_hash"] = prev
        event["event_hash"] = digest

    code, results = verify_rows(key, events)
    if code != 0 or not results[0]["ok"]:
        print(
            f"self-test FAILED: a freshly built chain did not verify: {results}",
            file=sys.stderr,
        )
        return 2
    print("self-test 1/3 ok: a well-formed 5-event chain verifies")

    tampered = [dict(e) for e in events]
    tampered[2] = {
        **tampered[2],
        "action": "escalate",
    }  # the alteration that would matter most
    code, results = verify_rows(key, tampered)
    if code != 1 or results[0]["first_bad_event_seq"] != 2:
        print(
            f"self-test FAILED: an altered action at event_seq=2 was not localized: {results}",
            file=sys.stderr,
        )
        return 2
    print("self-test 2/3 ok: an altered `action` is localized to event_seq=2")

    truncated = [dict(e) for e in events]
    del truncated[
        3
    ]  # a deleted row must surface as a prev_event_hash break at the NEXT row
    code, results = verify_rows(key, truncated)
    if code != 1 or results[0]["first_bad_event_seq"] != 4:
        print(
            f"self-test FAILED: a deleted row was not detected: {results}",
            file=sys.stderr,
        )
        return 2
    print("self-test 3/3 ok: a deleted event_seq=3 surfaces as a break at event_seq=4")
    print(
        "self-test PASSED — the verifier detects alteration and deletion, and localizes both."
    )
    return 0


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------


def _render(results: Sequence[dict[str, Any]], *, key_source: str) -> None:
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]

    print(
        f"audit chain verification — chain_field_set={CHAIN_FIELD_SET_VERSION}, key from {key_source}"
    )
    print(f"  sessions verified : {len(ok)}")
    print(f"  sessions failing  : {len(bad)}")
    print(f"  events examined   : {sum(r['events'] for r in results)}")

    if not results:
        print("")
        print("  no audit_event rows found. Nothing was verified — this is not a pass.")
        return

    if not bad:
        print("")
        print(
            "  RESULT: every session's chain recomputes from genesis. Tamper-evident."
        )
        return

    print("")
    print("=" * 98)
    print("  RESULT: CHAIN DIVERGENCE")
    print("=" * 98)
    for row in bad:
        seq = row["first_bad_event_seq"]
        where = f"event_seq={seq}" if seq is not None else "event_seq unknown"
        print(f"  session {row['session_id']}  FIRST DIVERGENCE AT {where}")
        print(f"      {row['detail']}")
        if seq is not None and seq > 0:
            print(
                f"      events 0..{seq - 1} in this session still recompute correctly."
            )
    print("")
    print("  Before declaring tampering, rule out the two benign causes:")
    print(
        "    1. AUDIT_CHAIN_KEY differs from the key that wrote the rows (rules.md R-58). A wrong"
    )
    print(
        "       key fails EVERY session at its first event — that pattern means the key, not an"
    )
    print("       attacker.")
    print(
        "    2. The hash-chain field set changed without a documented re-anchor (rules.md R-27)."
    )
    print("       Compare the migration head against CHAIN_FIELD_SET_VERSION above.")


def _safe_streams() -> None:
    """Make stdout/stderr unable to turn a verdict into a traceback.

    A non-ASCII character in a report line raises ``UnicodeEncodeError`` on a cp1252 console, which
    would present a detected chain divergence as a crash in the verifier. "The tool is broken" and
    "the audit log was altered" must never look the same.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    _safe_streams()
    parser = argparse.ArgumentParser(
        description="Verify the audit HMAC hash chain and report the first divergent event_seq.",
        epilog="AUDIT_CHAIN_KEY must be in the environment. Exit 0 verified, 1 divergence, 2 cannot run.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dsn", help="PostgreSQL DSN; defaults to $DATABASE_URL")
    source.add_argument(
        "--events-file", type=Path, help="JSON array of exported audit rows"
    )
    source.add_argument(
        "--self-test", action="store_true", help="prove the verifier detects tampering"
    )
    parser.add_argument("--session", help="verify one session_id only")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    raw_key = os.environ.get("AUDIT_CHAIN_KEY")
    if not raw_key:
        print(
            "verify_audit_chain: FATAL: AUDIT_CHAIN_KEY is not set.\n"
            "It is read from the environment on purpose — a key passed as a CLI argument appears in "
            "shell history, in `ps`, and in CI logs (rules.md R-34).",
            file=sys.stderr,
        )
        return 2
    chain_key = raw_key.encode("utf-8")

    if args.events_file:
        rows = _rows_from_file(args.events_file)
        key_source, origin = "$AUDIT_CHAIN_KEY", str(args.events_file)
    else:
        dsn = args.dsn or os.environ.get("DATABASE_URL")
        if not dsn:
            print(
                "verify_audit_chain: FATAL: no --dsn and no $DATABASE_URL. Pass --events-file to "
                "verify an export instead.",
                file=sys.stderr,
            )
            return 2
        rows = asyncio.run(_rows_from_db(dsn, args.session))
        key_source, origin = "$AUDIT_CHAIN_KEY", "database"

    if args.session and args.events_file:
        rows = [r for r in rows if str(r.get("session_id")) == args.session]

    code, results = verify_rows(chain_key, rows)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": code == 0,
                    "source": origin,
                    "chain_field_set_version": CHAIN_FIELD_SET_VERSION,
                    "sessions": results,
                },
                indent=2,
                default=str,
            )
        )
        return code

    print(f"source: {origin}")
    _render(results, key_source=key_source)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
