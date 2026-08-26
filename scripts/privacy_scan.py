#!/usr/bin/env python3
"""Static privacy scan — the leaks THIS project can actually have.

Run from the repository root::

    python scripts/privacy_scan.py                 # scan the default roots
    python scripts/privacy_scan.py --json          # machine-readable, for CI annotations
    python scripts/privacy_scan.py --list-rules    # what this tool does and does not cover

This is not a generic linter. Generic PII linters flag every string that looks like a phone number
and get muted within a week. This one encodes the four escape routes the privacy boundary in
``design.md`` section 3 is built to close, and nothing else:

* **P-01 raw-audio persistence** (rules.md R-14) — a file, object-store, or column write that puts
  PCM somewhere durable. The ring buffer is process memory only.
* **P-02 raw ``client_call_ref``** (rules.md R-16) — the un-HMAC'd caller reference reaching a log
  line, a response body, a query, or any module other than the two that are allowed to see it.
* **P-03 non-static error text** (rules.md R-17) — an interpolated HTTP ``detail``, WebSocket close
  ``reason``, or error ``message``. This is the documented path by which a caller reference escapes
  into a log: the value is not logged deliberately, it is logged *because it was in an exception*.
* **P-04 forbidden column / field names** (rules.md R-15) — an audio- or identity-adjacent name
  reaching the audit allow-list, a migration, or the client-facing OpenAPI schema.

**The vocabulary is loaded from the modules that enforce it, never restated here.** The deny-list
comes from ``gateway/app/audit/chain.py`` and the log allow-list from
``gateway/app/telemetry/logging.py``. A second copy of either list in this file would drift from the
real control within days, and a scanner that disagrees with the writer is worse than no scanner: it
reports clean while the writer accepts something new. If those lists cannot be read, this tool exits
non-zero rather than falling back to a hardcoded guess.

Exit codes: ``0`` clean · ``1`` findings · ``2`` the scan itself could not run (missing vocabulary,
unparseable source). ``2`` is deliberately distinct — "the check did not run" must never look like
"the check passed".
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------------------------------
# Scan scope
# --------------------------------------------------------------------------------------------------

#: Python trees scanned by default. Listed rather than "everything" so the report can say what was
#: covered (rules.md R-52) — and so ``tests/`` is excluded on purpose: test files legitimately
#: construct forbidden names in order to assert they are rejected, and flagging those would train
#: everyone to pass ``--ignore``.
DEFAULT_PYTHON_ROOTS: tuple[str, ...] = (
    "gateway/app",
    "scorer/app",
    "audit",
    "ml",
    "scripts",
)

#: SQL and migration files. Alembic is the only sanctioned DDL path (rules.md R-26), so a forbidden
#: column can only arrive through one of these.
SQL_GLOBS: tuple[str, ...] = (
    "**/alembic/versions/*.py",
    "**/migrations/*.py",
    "**/*.sql",
)

#: The only files permitted to mention the RAW caller reference. rules.md R-16 scopes the raw value to
#: Gateway process memory: the endpoint that receives it and the module that pseudonymizes it. Anywhere
#: else — a helper, a logger, a serializer — is a finding regardless of what the code does with it,
#: because "it is only used locally" is a property that survives exactly one refactor.
CLIENT_CALL_REF_ALLOWED_FILES: tuple[str, ...] = (
    "gateway/app/api/v1/sessions.py",
    "gateway/app/security/pseudonym.py",
    "scripts/privacy_scan.py",  # this file names the identifier in order to look for it
)

#: Sinks that mean "this value has left the process boundary".
_LOGGING_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
_DB_METHODS = frozenset({"execute", "executemany", "fetch", "fetchrow", "fetchval", "fetchall", "fetchone"})
_OBJECT_STORE_METHODS = frozenset({"put_object", "upload_file", "upload_fileobj", "write_object"})
_AUDIO_WRITERS = frozenset(
    {
        "wave.open",
        "soundfile.write",
        "sf.write",
        "scipy.io.wavfile.write",
        "wavfile.write",
        "torchaudio.save",
        "librosa.output.write_wav",
        "audiowrite",
        "sox.Transformer.build",
    }
)

#: Names that carry audio in this codebase. From design.md section 4 (ring buffer, window assembly) and
#: the frame contract's own vocabulary. The boundary class includes ``.``, ``/`` and ``-`` so that a
#: *path* matches as readily as an identifier: the first version of this regex required an underscore
#: boundary and therefore missed ``open("/tmp/session_audio.raw", "wb")`` — the single most likely
#: shape of the leak it exists to catch.
_AUDIO_NAME = re.compile(
    r"(?:^|[_\-./])(?:pcm|audio|waveform|wav|samples?|frame_payload|payload|ring|window_bytes|buf|buffer|recording|capture)(?:$|[_\-./])",
    re.IGNORECASE,
)

#: Audio container/raw extensions. Checked separately from the name regex because an extension carries
#: the intent on its own: nobody writes a ``.flac`` for a non-audio reason.
_AUDIO_EXT = re.compile(r"\.(?:wav|flac|pcm|raw|opus|ogg|mp3|m4a|aac|sph|au)\b", re.IGNORECASE)


def _looks_like_audio(text: str) -> bool:
    return bool(_AUDIO_NAME.search(text) or _AUDIO_EXT.search(text))

#: Write-ish file modes. ``open(path)`` defaults to read and is not a persistence risk.
_WRITE_MODE = re.compile(r"[wax+]")

_SQL_WRITE = re.compile(r"\b(?:create\s+table|alter\s+table|add\s+column|insert\s+into|select)\b", re.IGNORECASE)
_SQL_COLUMN_DECL = re.compile(r"^\s*[\"`]?(?P<name>[a-z_][a-z0-9_]*)[\"`]?\s+(?P<type>[a-z0-9_\[\]() ]+)", re.IGNORECASE)

#: SCREAMING_SNAKE_CASE — a module constant by convention, therefore not caller input.
_SCREAMING = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Calls whose result discards the value: a length, a class name, a boolean. Interpolating one of
#: these cannot leak the underlying data, and the Gateway's frame-length errors rely on exactly that.
_VALUE_ERASING_CALLS = frozenset({"len", "type", "id", "hex", "bool", "int", "float"})

#: Structural names that MAY be interpolated into an error string: sizes, sequence numbers, enum values,
#: contract identifiers. Everything here is a number the server computed or a value from a closed
#: vocabulary — none of it is free text a caller supplied. Extended at runtime with the real log
#: allow-list, which is exactly the same judgement applied to log fields.
_SAFE_INTERPOLATION_EXTRA = frozenset(
    {
        "expected",
        "actual",
        "size",
        "length",
        "count",
        "n",
        "limit",
        "index",
        "i",
        "context",
        "name",
        "names",
        "missing",
        "extra",
        "field",
        "fields",
        "value",  # only reached via len()/repr of a structural value; see _interpolated_names
        "seq",
        "sequence",
        "state",
        "provider",
        "profile",
        "version",
        "path",
        "key",
        "mode",
        "timeout",
        "deadline",
        "self",
        "cls",
        "type",
        "exc",
    }
)


# --------------------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    path: str
    line: int
    message: str
    snippet: str = ""

    def render(self) -> str:
        head = f"{self.path}:{self.line}: [{self.rule}] {self.message}"
        return f"{head}\n      {self.snippet}" if self.snippet else head


@dataclass
class Vocabulary:
    """The deny-list and allow-lists, read from the modules that enforce them."""

    forbidden_substrings: tuple[str, ...]
    chain_fields: tuple[str, ...]
    log_allowed_keys: frozenset[str]
    source: str  # "import" or "ast" — reported, because how we got the list matters

    @property
    def safe_interpolation_names(self) -> frozenset[str]:
        return frozenset(self.log_allowed_keys) | _SAFE_INTERPOLATION_EXTRA | set(self.chain_fields)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    scanned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    vocabulary_source: str = "unknown"

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)


@dataclass(frozen=True, slots=True)
class RaiseSite:
    """Where an exception class is raised, and whether its message is static."""

    path: str
    line: int
    static: bool
    unsafe_names: tuple[str, ...] = ()


@dataclass(slots=True)
class ParsedFile:
    rel: str
    src: str
    tree: ast.Module
    #: Module-level names bound to a literal-only value. A constant cannot carry caller input, so a
    #: value read out of one is static no matter how it is indexed.
    constants: frozenset[str]


# --------------------------------------------------------------------------------------------------
# Vocabulary loading — import first, AST second, never a hardcoded copy
# --------------------------------------------------------------------------------------------------


def load_vocabulary(repo: Path) -> Vocabulary:
    """Read the real deny-list and log allow-list.

    ``chain.py`` and ``logging.py`` both import stdlib only (plus ``app.constants``), so importing them
    needs no third-party dependency — which is why the import path is tried first: it gets the value the
    running Gateway actually uses. The AST fallback exists for the case where ``app.constants`` is
    mid-refactor and the import raises; it still reads the same file, so it cannot invent a different
    vocabulary. If both fail we exit 2. A hardcoded fallback would let this scanner pass while the
    writer's list had grown.
    """
    chain_path = repo / "gateway" / "app" / "audit" / "chain.py"
    logging_path = repo / "gateway" / "app" / "telemetry" / "logging.py"
    for required in (chain_path, logging_path):
        if not required.is_file():
            _die(f"cannot load the privacy vocabulary: {required} is missing")

    sys.path.insert(0, str(repo / "gateway"))
    try:
        from app.audit import chain as chain_mod  # noqa: PLC0415  (deliberately late)
        from app.telemetry import logging as logging_mod  # noqa: PLC0415

        return Vocabulary(
            forbidden_substrings=tuple(chain_mod._FORBIDDEN_SUBSTRINGS),  # noqa: SLF001
            chain_fields=tuple(chain_mod.CHAIN_FIELDS),
            log_allowed_keys=frozenset(logging_mod.ALLOWED_EXTRA_KEYS),
            source="import",
        )
    except Exception:  # noqa: BLE001 — any import failure falls through to the AST reader
        pass
    finally:
        sys.path.pop(0)

    forbidden = _literal_from_source(chain_path, "_FORBIDDEN_SUBSTRINGS")
    chain_fields = _literal_from_source(chain_path, "CHAIN_FIELDS")
    allowed = _literal_from_source(logging_path, "ALLOWED_EXTRA_KEYS")
    if forbidden is None or chain_fields is None or allowed is None:
        _die(
            "cannot load the privacy vocabulary by import or by AST. Refusing to scan with a guessed "
            "deny-list: a scanner that disagrees with app/audit/chain.py reports clean while the "
            "writer accepts a forbidden field (rules.md R-15)."
        )
    return Vocabulary(
        forbidden_substrings=tuple(str(x) for x in forbidden),
        chain_fields=tuple(str(x) for x in chain_fields),
        log_allowed_keys=frozenset(str(x) for x in allowed),
        source="ast",
    )


def _literal_from_source(path: Path, name: str) -> Sequence[Any] | None:
    """Extract one module-level literal collection by name, without importing the module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = node.value
        # Unwrap `Final[...]`-style annotations' values and frozenset(...)/tuple(...) wrappers.
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id in {"frozenset", "set", "tuple", "list"} and value.args:
                value = value.args[0]
        try:
            return list(ast.literal_eval(value))  # type: ignore[arg-type]
        except (ValueError, TypeError, SyntaxError):
            return None
    return None


def _die(message: str) -> None:
    print(f"privacy_scan: FATAL: {message}", file=sys.stderr)
    raise SystemExit(2)


# --------------------------------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------------------------------


def _dotted(node: ast.expr) -> str:
    """Render ``a.b.c`` from an attribute chain; ``""`` for anything else."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif not parts:
        return ""
    return ".".join(reversed(parts))


def _names_in(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            out.add(child.id)
        elif isinstance(child, ast.Attribute):
            out.add(child.attr)
    return out


def _is_constant_tree(node: ast.expr, consts: frozenset[str] = frozenset()) -> bool:
    """True when the expression's value cannot carry caller input.

    Literals count, obviously. Three non-literal forms count too, and each one is a pattern the
    Gateway already uses correctly — a checker that rejected them would be reporting the correct
    implementation as the violation, which is how a control gets switched off:

    * **A read from a module-level constant.** ``CLOSE_REASONS[exc.code]`` and
      ``CLOSE_REASONS.get(code, "")`` resolve to one of a closed set of literal strings declared at
      module scope. That is *more* static than an inline string, because the set is reviewable in one
      place. The index may be caller-influenced; the value returned cannot be.
    * **A SCREAMING_SNAKE_CASE name.** By convention a compile-time constant. The trade-off is
      explicit: a name in caps holding caller input would defeat this, and the defence against that is
      code review of the naming, not this scanner.
    * **``len(x)``, ``type(x).__name__``, and friends.** A count or a class name, never the value.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id in consts or bool(_SCREAMING.match(node.id))
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_constant_tree(e, consts) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            k is not None and _is_constant_tree(k, consts) and _is_constant_tree(v, consts)
            for k, v in zip(node.keys, node.values)
        )
    if isinstance(node, ast.Subscript):
        return _is_constant_tree(node.value, consts)
    if isinstance(node, ast.Call):
        target = _dotted(node.func)
        if target in _VALUE_ERASING_CALLS:
            return True
        # `SOME_MAP.get(key, default)` — the mapping is constant, so every possible result is.
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "setdefault"}:
            return _is_constant_tree(node.func.value, consts)
    if isinstance(node, ast.BoolOp):
        return all(_is_constant_tree(v, consts) for v in node.values)
    if isinstance(node, ast.IfExp):
        return _is_constant_tree(node.body, consts) and _is_constant_tree(node.orelse, consts)
    return False


def _module_constants(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound to a literal-only value."""
    out: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        inner = value
        if isinstance(inner, ast.Call) and _dotted(inner.func) in {"frozenset", "set", "tuple", "list", "dict"}:
            inner = inner.args[0] if inner.args else ast.Constant(value=None)
        if _is_constant_tree(inner, frozenset(out)):
            out.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(out)


def _interpolated_names(node: ast.expr) -> set[str]:
    """Names whose *values* are spliced into a string.

    ``len(x)`` and ``type(x).__name__`` contribute nothing: the result is a number or a class name, not
    caller data. SCREAMING_SNAKE names contribute nothing either — a constant is not caller input.
    Excluding both is what keeps the R-17 check quiet enough to stay enabled: a check that fires on
    ``f"expected {n} bytes, got {len(body)}"`` gets suppressed, and a suppressed check is not a check.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.FormattedValue):
            inner = child.value
            if isinstance(inner, ast.Call) and _dotted(inner.func) in _VALUE_ERASING_CALLS:
                continue
            names |= {n for n in _names_in(inner) if not _SCREAMING.match(n)}
    return names


def _string_payload_nodes(call: ast.Call) -> Iterator[tuple[str, ast.expr]]:
    """Yield ``(keyword, value)`` for the arguments that become client-visible error text."""
    for kw in call.keywords:
        if kw.arg in {"detail", "reason", "message", "content", "description"}:
            yield kw.arg, kw.value


# --------------------------------------------------------------------------------------------------
# P-01  raw-audio persistence
# --------------------------------------------------------------------------------------------------


def _check_audio_persistence(tree: ast.AST, rel: str, src: str, report: Report) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _dotted(node.func)

        if target in _AUDIO_WRITERS or target.split(".")[-1] in {"write_wav", "save_audio"}:
            report.add(
                Finding(
                    "P-01",
                    rel,
                    node.lineno,
                    f"audio writer {target!r} called: no raw audio persists anywhere by default "
                    "(rules.md R-14)",
                    _segment(src, node),
                )
            )
            continue

        if target.split(".")[-1] in _OBJECT_STORE_METHODS:
            arg_names = " ".join(sorted(_names_in(node)))
            if _looks_like_audio(arg_names) or any(
                _looks_like_audio(str(c.value))
                for c in ast.walk(node)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ):
                report.add(
                    Finding(
                        "P-01",
                        rel,
                        node.lineno,
                        f"object-store upload {target!r} with an audio-shaped argument: there is no "
                        "audio object store on either tier (rules.md R-14, architecture.md 5.1)",
                        _segment(src, node),
                    )
                )
            continue

        if target in {"open", "io.open", "pathlib.Path.open", "aiofiles.open"} or target.endswith(".open"):
            mode = _open_mode(node)
            if mode is None or not _WRITE_MODE.search(mode):
                continue
            referenced = " ".join(sorted(_names_in(node))) + " " + " ".join(
                str(c.value) for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)
            )
            if _looks_like_audio(referenced):
                report.add(
                    Finding(
                        "P-01",
                        rel,
                        node.lineno,
                        f"file opened for writing (mode {mode!r}) against an audio-shaped path: the "
                        "ring buffer is process memory only and is cleared in a finally (rules.md R-14)",
                        _segment(src, node),
                    )
                )

    # Audio bytes reaching a log call. logging.py replaces a `bytes` value with its length at the sink,
    # so this is defence in depth — but the sink only sees what the call site passes, and an
    # `f"{pcm!r}"` is already a str by the time the formatter runs. That is the one shape the three
    # controls in telemetry/logging.py cannot catch, which is precisely why it is checked here.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts = _dotted(node.func).split(".")
        if len(parts) < 2 or parts[-1] not in _LOGGING_METHODS:
            continue
        if not any(p in {"log", "logger", "_log", "logging"} or p.startswith("_log") for p in parts[:-1]):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.JoinedStr):
                for name in _interpolated_names(arg):
                    if _AUDIO_NAME.search(name):
                        report.add(
                            Finding(
                                "P-01",
                                rel,
                                node.lineno,
                                f"audio-shaped value {name!r} interpolated into a log message. Wrap it "
                                "in len() — an f-string is already a str at the sink, so the bytes "
                                "control in telemetry/logging.py cannot redact it (rules.md R-14)",
                                _segment(src, node),
                            )
                        )


def _open_mode(call: ast.Call) -> str | None:
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
        return call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return "r" if call.args else None


# --------------------------------------------------------------------------------------------------
# P-02  raw client_call_ref
# --------------------------------------------------------------------------------------------------

_RAW_REF_NAMES = frozenset({"client_call_ref", "raw_call_ref", "caller_reference", "client_ref"})


def _check_client_call_ref(tree: ast.AST, rel: str, src: str, report: Report) -> None:
    allowed_here = rel in CLIENT_CALL_REF_ALLOWED_FILES

    if not allowed_here:
        for node in ast.walk(tree):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name in _RAW_REF_NAMES:
                report.add(
                    Finding(
                        "P-02",
                        rel,
                        node.lineno,
                        f"{name!r} appears outside the two files allowed to see the raw caller "
                        "reference. HMAC is applied before the value can reach a response, WSS "
                        "message, log, gRPC request, row, metric label, or webhook (rules.md R-16). "
                        f"Allowed: {', '.join(CLIENT_CALL_REF_ALLOWED_FILES[:2])}",
                        _segment(src, node),
                    )
                )
        return

    # Inside the allowed files the name is legitimate, so check where it FLOWS instead.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _dotted(node.func)
        leaf = target.split(".")[-1]
        sink = None
        if leaf in _LOGGING_METHODS and any(p.startswith("_log") or p in {"log", "logger", "logging"} for p in target.split(".")[:-1]):
            sink = "a log line"
        elif leaf in _DB_METHODS:
            sink = "a database query"
        elif leaf in _OBJECT_STORE_METHODS:
            sink = "an object store"
        elif leaf in {"send_json", "send_text", "send_bytes", "post", "put", "patch"}:
            sink = "an outbound message"
        if sink is None:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if _RAW_REF_NAMES & _names_in(arg):
                report.add(
                    Finding(
                        "P-02",
                        rel,
                        node.lineno,
                        f"the raw caller reference reaches {sink}. Pass the HMAC pseudonym "
                        "(security/pseudonym.py) instead — the raw value must not leave Gateway "
                        "process memory (rules.md R-16)",
                        _segment(src, node),
                    )
                )


# --------------------------------------------------------------------------------------------------
# P-03  non-static error text (rules.md R-17)
# --------------------------------------------------------------------------------------------------

_ERROR_CONSTRUCTORS = frozenset({"HTTPException", "JSONResponse", "WebSocketException", "PlainTextResponse"})


def collect_raise_sites(files: Sequence[ParsedFile], vocab: Vocabulary) -> dict[str, list[RaiseSite]]:
    """Record, per exception class, whether every ``raise`` of it uses a static message.

    This exists to answer one question precisely, rather than to flag a shape. ``sessions.py`` returns
    ``str(exc)`` from a ``PseudonymError`` to the client, with a comment asserting that the message
    "never contains the offending value". That assertion is load-bearing and it lives in a *different
    module* from the code that depends on it: the day someone adds
    ``raise PseudonymError(f"bad ref {value!r}")`` for easier debugging, the raw caller reference
    starts arriving in a 422 body and in whatever log records that response (rules.md R-16, R-17).

    So instead of flagging every forwarded exception message — which would fire on correct code and be
    muted — the scan verifies the assumption and reports only when it stops holding.
    """
    sites: dict[str, list[RaiseSite]] = {}
    safe = vocab.safe_interpolation_names
    for parsed in files:
        for node in ast.walk(parsed.tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            call = node.exc
            if not isinstance(call, ast.Call):
                continue
            cls = _dotted(call.func).split(".")[-1]
            if not cls:
                continue
            payloads = [a for a in call.args if not isinstance(a, ast.Starred)]
            payloads += [kw.value for kw in call.keywords if kw.arg in {"detail", "message", "reason"}]
            unsafe: set[str] = set()
            static = True
            for payload in payloads:
                if _is_constant_tree(payload, parsed.constants):
                    continue
                names = _interpolated_names(payload) if isinstance(payload, ast.JoinedStr) else _names_in(payload)
                bad = {n for n in names if n not in safe and n not in parsed.constants}
                if bad or not isinstance(payload, (ast.JoinedStr, ast.Constant)):
                    static = False
                    unsafe |= bad
            sites.setdefault(cls, []).append(
                RaiseSite(parsed.rel, node.lineno, static, tuple(sorted(unsafe)))
            )
    return sites


def _handler_exception_names(parsed: ParsedFile, node: ast.AST) -> set[str]:
    """Exception classes bound by the ``except`` clause enclosing ``node``, if any."""
    for handler in (n for n in ast.walk(parsed.tree) if isinstance(n, ast.ExceptHandler)):
        span = {d for d in ast.walk(handler) if d is node}
        if not span:
            continue
        etype = handler.type
        if etype is None:
            return {"BaseException"}
        candidates = etype.elts if isinstance(etype, ast.Tuple) else [etype]
        return {_dotted(c).split(".")[-1] for c in candidates if _dotted(c)}
    return set()


def _check_static_errors(
    parsed: ParsedFile,
    vocab: Vocabulary,
    raise_sites: dict[str, list[RaiseSite]],
    report: Report,
) -> None:
    safe = vocab.safe_interpolation_names
    rel, src = parsed.rel, parsed.src
    for node in ast.walk(parsed.tree):
        if not isinstance(node, ast.Call):
            continue
        target = _dotted(node.func)
        leaf = target.split(".")[-1]
        is_error_ctor = leaf in _ERROR_CONSTRUCTORS
        is_close = leaf == "close"
        if not (is_error_ctor or is_close):
            continue

        for kw_name, value in _string_payload_nodes(node):
            if _is_constant_tree(value, parsed.constants):
                continue

            # Case 1: an exception's own message is forwarded to the client. Safe only if every raise
            # site of that class uses a static message; see collect_raise_sites.
            forwarded = _forwarded_exception(value)
            if forwarded is not None:
                classes = _handler_exception_names(parsed, node) or {forwarded}
                offenders = [
                    site
                    for cls in classes
                    for site in raise_sites.get(cls, ())
                    if not site.static
                ]
                if not offenders:
                    continue
                where = "; ".join(f"{s.path}:{s.line}" for s in offenders[:3])
                report.add(
                    Finding(
                        "P-03",
                        rel,
                        node.lineno,
                        f"{leaf} {kw_name} forwards an exception message to the client, but "
                        f"{'/'.join(sorted(classes))} is raised with a NON-static message at {where}. "
                        "That makes this a leak path for whatever was interpolated there "
                        "(rules.md R-17, R-16)",
                        _segment(src, node),
                    )
                )
                continue

            # Case 2: the payload interpolates something directly.
            interpolated = _interpolated_names(value) if isinstance(value, ast.JoinedStr) else _names_in(value)
            unsafe = {n for n in interpolated if n not in safe and n not in parsed.constants}
            if interpolated and not unsafe:
                continue  # every spliced value is a size, an enum, or an allow-listed log field
            what = "close reason" if is_close else f"{leaf} {kw_name}"
            detail = f" unrecognised value(s): {', '.join(sorted(unsafe))}" if unsafe else ""
            report.add(
                Finding(
                    "P-03",
                    rel,
                    node.lineno,
                    f"non-static {what}. Error messages and close reasons are static — interpolating "
                    "client input into one is the documented path for a caller reference to escape "
                    f"into a log (rules.md R-17).{detail}",
                    _segment(src, node),
                )
            )


def _forwarded_exception(node: ast.expr) -> str | None:
    """If the payload is (or contains) ``str(exc)``/``exc.args``/``repr(exc)``, return the bound name."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _dotted(child.func) in {"str", "repr", "format"}:
            for arg in child.args:
                name = _dotted(arg)
                if name and ("exc" in name or "err" in name or name in {"e", "ex"}):
                    return name.split(".")[0]
        if isinstance(child, ast.Attribute) and child.attr in {"args", "detail", "msg", "message"}:
            base = _dotted(child.value)
            if base and ("exc" in base or "err" in base):
                return base.split(".")[0]
    return None


# --------------------------------------------------------------------------------------------------
# P-04  forbidden column / field names (rules.md R-15)
# --------------------------------------------------------------------------------------------------


def _forbidden_hit(name: str, vocab: Vocabulary) -> str | None:
    lowered = name.lower()
    return next((bad for bad in vocab.forbidden_substrings if bad in lowered), None)


#: Exact identifier names that contain a deny-listed substring and are nevertheless correct.
#:
#: Every entry is an exact, whole-identifier match — never a substring — so `sha256_audio` is allowed
#: while `sha256_audio_path`, `audio_sha256`, and `raw_audio` still fire. That distinction is the
#: whole safety of this mechanism: the deny-list exists to stop a column that can *hold* audio, a
#: transcript, or an identifier, and a field holding a 64-character hex digest cannot hold any of
#: them. Loosening the substring match instead of naming exceptions here would silently permit the
#: entire `*audio*` family.
#:
#: Adding to this list is a privacy decision, not a lint fix. Each entry states who reviewed it.
DOCUMENTED_NAME_EXCEPTIONS: dict[str, str] = {
    # Mandatory dataset-manifest field (playbook section 2.1). Holds the SHA-256 of a recording, which
    # is precisely how the manifest carries a reference to audio *without* carrying audio or its path
    # (rules.md R-21). scripts/validate_manifest.py carries the identical exception, and D-02 there
    # still rejects `audio_path`.
    "sha256_audio": "audio",
    # gRPC/diagnostic field in contracts/voice_scorer.proto. `raw` is deny-listed because a column
    # named `raw_*` tends to hold an unprocessed client value; `raw_score` holds an uncalibrated
    # float used for parity and diagnostics only, never as a policy input (rules.md R-11, R-12), and
    # openapi.yaml documents that boundary in prose.
    "raw_score": "raw",
}


def _is_documented_exception(name: str, hit: str) -> bool:
    return DOCUMENTED_NAME_EXCEPTIONS.get(name.lower()) == hit


def _check_forbidden_names_python(tree: ast.AST, rel: str, src: str, vocab: Vocabulary, report: Report) -> None:
    """Flag forbidden names in the two places they would become structural: a field/column collection,
    and a SQL string."""
    for node in ast.walk(tree):
        # A collection literal assigned to a *_FIELDS / *_COLUMNS / *_KEYS name is a schema in
        # disguise. chain.py guards its own list at import; this catches the same mistake in a
        # migration helper or a serializer that chain.py never sees.
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            label = next((t.id for t in targets if isinstance(t, ast.Name)), "")
            if re.search(r"(FIELDS|COLUMNS|KEYS|ALLOW|SCHEMA)", label, re.IGNORECASE) and node.value is not None:
                for child in ast.walk(node.value):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        hit = _forbidden_hit(child.value, vocab)
                        if hit and not _is_documented_exception(child.value, hit):
                            report.add(
                                Finding(
                                    "P-04",
                                    rel,
                                    child.lineno,
                                    f"forbidden substring {hit!r} in {label} entry {child.value!r}. "
                                    "The forbidden-column list is structural, not aspirational — such "
                                    "a column must not exist (rules.md R-15)",
                                    _segment(src, child),
                                )
                            )

        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _SQL_WRITE.search(node.value):
            for finding in _scan_sql_text(node.value, rel, node.lineno, vocab):
                report.add(finding)


def _scan_sql_text(text: str, rel: str, base_line: int, vocab: Vocabulary) -> Iterator[Finding]:
    """Scan a DDL/DML blob for forbidden column names and stray ``bytea`` columns."""
    for offset, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        match = _SQL_COLUMN_DECL.match(line)
        if match is None:
            continue
        name, coltype = match.group("name"), match.group("type").lower()
        if name.lower() in {"create", "alter", "add", "insert", "select", "primary", "unique", "constraint", "check", "foreign"}:
            continue
        hit = _forbidden_hit(name, vocab)
        if hit:
            yield Finding(
                "P-04",
                rel,
                base_line + offset,
                f"forbidden substring {hit!r} in column {name!r}. The deny-list is asserted against "
                "information_schema, so this column must not exist at all (rules.md R-15, "
                "technical-design.md 5.2)",
                line,
            )
        if "bytea" in coltype and name not in {"prev_event_hash", "event_hash"}:
            yield Finding(
                "P-04",
                rel,
                base_line + offset,
                f"bytea column {name!r}. bytea is permitted only for the two 32-byte hash columns — "
                "any other bytea column is a place audio could be stored (rules.md R-15)",
                line,
            )


def _check_openapi(repo: Path, vocab: Vocabulary, report: Report) -> None:
    """The OpenAPI document is the client-facing seam. A forbidden property name here means the *API*
    offers a place for audio or identity data, whatever the database happens to contain.

    Scanned as text rather than parsed: a YAML parser is an extra dependency for a check whose whole
    job is to match property names, and this must run in a job with no pip install.
    """
    path = repo / "contracts" / "openapi.yaml"
    if not path.is_file():
        report.skipped.append("contracts/openapi.yaml (absent)")
        return
    report.scanned.append("contracts/openapi.yaml")
    prop_line = re.compile(r"^\s{2,}(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:$|\{|\|)")
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = prop_line.match(line)
        if match is None:
            continue
        name = match.group("name")
        hit = _forbidden_hit(name, vocab)
        if hit and not _is_documented_exception(name, hit):
            report.add(
                Finding(
                    "P-04",
                    "contracts/openapi.yaml",
                    lineno,
                    f"forbidden substring {hit!r} in schema property {name!r}: the public API must "
                    "offer no field that can carry audio, a transcript, a phone number, a caller "
                    "name, or an embedding (rules.md R-15)",
                    line.strip(),
                )
            )


def _check_log_allowlist(vocab: Vocabulary, report: Report) -> None:
    """The log allow-list is itself a schema. A forbidden key added there would be emitted verbatim."""
    for key in sorted(vocab.log_allowed_keys):
        hit = _forbidden_hit(key, vocab)
        if hit:
            report.add(
                Finding(
                    "P-04",
                    "gateway/app/telemetry/logging.py",
                    0,
                    f"ALLOWED_EXTRA_KEYS contains {key!r} (forbidden substring {hit!r}): an "
                    "allow-listed key is emitted verbatim into every log line (rules.md R-14, R-15)",
                )
            )


# --------------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------------


def _segment(src: str, node: ast.AST) -> str:
    try:
        text = ast.get_source_segment(src, node) or ""
    except Exception:  # noqa: BLE001
        return ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:160]


def _python_files(repo: Path, roots: Iterable[str]) -> Iterator[Path]:
    for root in roots:
        base = repo / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts or any(p in {"tests", "test"} for p in path.parts):
                continue
            yield path


def scan(repo: Path, roots: Sequence[str]) -> Report:
    vocab = load_vocabulary(repo)
    report = Report(vocabulary_source=vocab.source)

    for root in roots:
        if not (repo / root).exists():
            report.skipped.append(f"{root}/ (absent — owned by another pair, not yet landed)")

    # Pass 1: parse everything. Two checks need whole-repo knowledge before either can run — the
    # exception-forwarding check in P-03 has to know how a class is raised in *another* file before it
    # can judge a call site here. A single-pass scanner would have to assume, and assuming safe is how
    # a scanner reports clean on a leak.
    parsed_files: list[ParsedFile] = []
    for path in _python_files(repo, roots):
        rel = path.relative_to(repo).as_posix()
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as exc:
            # Exit 2, not a finding: an unparsed file is an unscanned file, and an unscanned file
            # reported as clean is the failure this whole tool exists to avoid.
            _die(f"{rel}:{exc.lineno}: cannot parse ({exc.msg}); the scan is incomplete")
        parsed_files.append(ParsedFile(rel=rel, src=src, tree=tree, constants=_module_constants(tree)))

    raise_sites = collect_raise_sites(parsed_files, vocab)

    # Pass 2: the checks.
    for parsed in parsed_files:
        report.scanned.append(parsed.rel)
        _check_audio_persistence(parsed.tree, parsed.rel, parsed.src, report)
        _check_client_call_ref(parsed.tree, parsed.rel, parsed.src, report)
        _check_static_errors(parsed, vocab, raise_sites, report)
        _check_forbidden_names_python(parsed.tree, parsed.rel, parsed.src, vocab, report)

    sql_seen = 0
    for pattern in SQL_GLOBS:
        for path in sorted(repo.glob(pattern)):
            if "__pycache__" in path.parts or ".venv" in str(path):
                continue
            rel = path.relative_to(repo).as_posix()
            if rel in report.scanned:
                continue
            sql_seen += 1
            report.scanned.append(rel)
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".sql":
                for finding in _scan_sql_text(text, rel, 1, vocab):
                    report.add(finding)
    if sql_seen == 0:
        # rules.md R-52: say what was not covered. The audit DDL is Pair C's Phase-1 deliverable; until
        # it lands, the structural deny-list is asserted by audit/tests against information_schema and
        # NOT by this scan.
        report.skipped.append("SQL / Alembic migrations (none found — deny-list unverified by this scan)")

    _check_openapi(repo, vocab, report)
    _check_log_allowlist(vocab, report)
    return report


RULES_DOC = """\
P-01  raw-audio persistence            rules.md R-14  audio writer, audio-shaped file/object write,
                                                      audio value interpolated into a log message
P-02  raw client_call_ref              rules.md R-16  the un-HMAC'd reference outside the two files
                                                      allowed to see it, or reaching a log/DB/network
P-03  non-static error text            rules.md R-17  interpolated HTTPException detail, close reason,
                                                      or response message
P-04  forbidden column / field name    rules.md R-15  audio- or identity-adjacent name in a field list,
                                                      DDL, log allow-list, or the OpenAPI schema

NOT covered by this tool (rules.md R-52 — say what you dropped):
  * runtime behaviour. Whether a log line ACTUALLY redacts is asserted by
    gateway/tests/test_log_redaction.py under the `privacy` marker; this is a static scan.
  * the database as built. The structural deny-list against information_schema lives in audit/tests.
  * the PWA. TypeScript is not parsed here; consent-before-capture (R-18) is a PWA test.
  * non-Python, non-SQL config: task definitions, Compose files, Caddyfile.
"""


def _safe_streams() -> None:
    """Make stdout/stderr unable to turn a finding into a traceback.

    A non-ASCII character in a finding message raises ``UnicodeEncodeError`` on a cp1252 console, so
    a real privacy violation would present as a crash in the scanner rather than as a blocked
    release. This check is a release blocker; it must fail *legibly* or it will get bypassed.
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
        description="Static privacy scan for the SIH26104 privacy boundary (rules.md R-14..R-19).",
        epilog="Exit 0 clean, 1 findings, 2 the scan could not run.",
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository root (default: inferred)")
    parser.add_argument("--root", action="append", dest="roots", default=None, help="extra tree to scan")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--list-rules", action="store_true", help="print the rule table and exit")
    args = parser.parse_args(argv)

    if args.list_rules:
        print(RULES_DOC)
        return 0

    repo = args.repo.resolve()
    roots = tuple(args.roots) if args.roots else DEFAULT_PYTHON_ROOTS
    report = scan(repo, roots)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not report.findings,
                    "vocabulary_source": report.vocabulary_source,
                    "files_scanned": len(report.scanned),
                    "skipped": report.skipped,
                    "findings": [f.__dict__ for f in report.findings],
                },
                indent=2,
            )
        )
        return 1 if report.findings else 0

    print(f"privacy_scan: {len(report.scanned)} file(s) scanned; vocabulary read by {report.vocabulary_source}")
    for note in report.skipped:
        print(f"  NOT SCANNED: {note}")

    if not report.findings:
        print("privacy_scan: PASS — no static privacy-boundary violations found.")
        print("  This is a static scan. Runtime redaction is asserted by the `privacy` marker suite.")
        return 0

    print("")
    print("=" * 98)
    print("privacy_scan: FAIL — RELEASE BLOCKER. A privacy-boundary finding is not a style comment.")
    print("=" * 98)
    for finding in report.findings:
        print(f"  {finding.render()}")
    print("")
    print(f"{len(report.findings)} finding(s). See rules.md section C and design.md section 3.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
