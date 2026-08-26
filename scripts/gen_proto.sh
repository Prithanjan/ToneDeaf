#!/usr/bin/env bash
#
# Generate the VoiceScorer gRPC stubs for BOTH sides of the seam from contracts/voice_scorer.proto.
#
# ─── READ THIS FIRST ────────────────────────────────────────────────────────────────────────────
# gateway/app/scorer/client.py ALREADY imports these stubs:
#
#     from app.scorer import voice_scorer_pb2 as pb
#     from app.scorer import voice_scorer_pb2_grpc as pb_grpc
#
# Until this script has run, those modules do not exist on disk. That means NOTHING in the Gateway
# that transitively touches the Scorer is importable — client.py, main.py, and every test that
# imports the app factory will fail at collection with ModuleNotFoundError, not with a useful
# message. If you have just cloned the repo, run this before running the Gateway test suite.
#
# The generated files are COMMITTED, not built at runtime. A stub/contract mismatch then fails in CI
# on a diff, rather than at demo time on a serialization error nobody can read.
# ────────────────────────────────────────────────────────────────────────────────────────────────
#
# WHY THE grpcio-tools VERSION IS PINNED AND CHECKED HERE
# Generated stubs are version-coupled to the runtime: the descriptor-pool bootstrap emitted by
# protoc-gen-python calls into google.protobuf internals whose shape changes between releases, and
# grpcio's generated service code carries a `_version_not_supported` guard that raises at import if
# the runtime is older than the generator. Generating with a different grpcio-tools than the pinned
# grpcio in gateway/requirements.txt and scorer/requirements.txt produces stubs that import fine on
# the generating machine and abort on the container. So this script refuses to run on a mismatch
# instead of producing a plausible-looking artifact.
#
# IDEMPOTENCE
# Running this twice with the same proto and the same grpcio-tools yields byte-identical files:
# protoc output is deterministic, and the import fixup below only rewrites lines that still match
# the un-fixed form. `git diff --exit-code` after a run is the CI check.
#
# WHY THERE IS NO --pyi_out
# grpcio-tools 1.68 can emit .pyi stubs, but gateway/pyproject.toml already declares
# `ignore_errors = true` for the two generated modules and wraps every stub call in a typed
# boundary (client.py returns a typed WindowScore). Emitting type stubs would add two more
# generated files to a two-key-reviewed contract's output set for no checking that is not already
# done at the wrapper. If that trade changes, it changes in this script and in both pyproject files
# together.

set -euo pipefail

EXPECTED_GRPCIO_TOOLS="1.68.1"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="${REPO_ROOT}/contracts"
PROTO_FILE="voice_scorer.proto"

# Both consumers of the contract get stubs from the SAME generator invocation. Generating them
# separately is how the two sides end up on different descriptor bootstraps while the proto file
# hash in the parity set still matches.
GATEWAY_OUT="${REPO_ROOT}/gateway/app/scorer"
SCORER_OUT="${REPO_ROOT}/scorer/app"

PYTHON="${PYTHON:-python3}"

die() { printf 'gen_proto: %s\n' "$*" >&2; exit 1; }

# --- preconditions ------------------------------------------------------------------------------

command -v "${PYTHON}" >/dev/null 2>&1 || die "no interpreter at '${PYTHON}'. Set PYTHON=/path/to/python3.12"

[[ -f "${PROTO_DIR}/${PROTO_FILE}" ]] || die "contract not found: ${PROTO_DIR}/${PROTO_FILE}"

ACTUAL_GRPCIO_TOOLS="$(
  "${PYTHON}" - <<'PY' 2>/dev/null || true
try:
    from importlib.metadata import version
    print(version("grpcio-tools"))
except Exception:
    pass
PY
)"

[[ -n "${ACTUAL_GRPCIO_TOOLS}" ]] || die \
  "grpcio-tools is not installed for '${PYTHON}'. Install the pinned version:
    ${PYTHON} -m pip install 'grpcio-tools==${EXPECTED_GRPCIO_TOOLS}'
  (Python 3.12 — decision D-10. The 3.14 interpreter on this workstation has no assured wheels.)"

if [[ "${ACTUAL_GRPCIO_TOOLS}" != "${EXPECTED_GRPCIO_TOOLS}" ]]; then
  die "grpcio-tools version mismatch: found ${ACTUAL_GRPCIO_TOOLS}, this repo pins ${EXPECTED_GRPCIO_TOOLS}.
  Stubs are version-coupled to the grpcio runtime pinned in gateway/requirements.txt and
  scorer/requirements.txt. Generating with ${ACTUAL_GRPCIO_TOOLS} would produce files that import on
  this machine and fail inside the container. Refusing to generate."
fi

for out_dir in "${GATEWAY_OUT}" "${SCORER_OUT}"; do
  [[ -d "${out_dir}" ]] || die "output package does not exist: ${out_dir}"
  # Generated modules must land inside a real package or the relative import fixup below is a lie.
  [[ -f "${out_dir}/__init__.py" ]] || die "output package has no __init__.py: ${out_dir}"
done

# --- generate ------------------------------------------------------------------------------------

# --proto_path is contracts/ so the emitted descriptor name is "voice_scorer.proto" with no
# directory prefix. A prefix would become part of the descriptor pool key and both sides would have
# to agree on the repo layout, not just on the contract.
for out_dir in "${GATEWAY_OUT}" "${SCORER_OUT}"; do
  "${PYTHON}" -m grpc_tools.protoc \
    --proto_path="${PROTO_DIR}" \
    --python_out="${out_dir}" \
    --grpc_python_out="${out_dir}" \
    "${PROTO_FILE}"
done

# --- fix the generated import -------------------------------------------------------------------
#
# protoc emits, in voice_scorer_pb2_grpc.py:
#
#     import voice_scorer_pb2 as voice__scorer__pb2
#
# That is a top-level absolute import. It resolves only if the generation directory happens to be on
# sys.path, which it is not: the modules live inside app.scorer (Gateway) and app (Scorer). Left
# alone it raises ModuleNotFoundError at import time — and because the Gateway imports the stub
# during app construction, the symptom is "the whole Gateway is broken", not "one import is wrong".
#
# Rewritten to an explicit relative import, which is correct for both destination packages without
# either needing to know its own dotted path. The regex is anchored to the un-fixed form, so
# re-running is a no-op rather than producing `from . from . import`.
fix_imports() {
  local target="$1"
  [[ -f "${target}" ]] || die "expected protoc to write ${target}"
  "${PYTHON}" - "${target}" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
fixed = re.sub(
    r"^import voice_scorer_pb2 as voice__scorer__pb2$",
    "from . import voice_scorer_pb2 as voice__scorer__pb2",
    source,
    flags=re.MULTILINE,
)
if fixed != source:
    # newline="" keeps protoc's LF endings on Windows checkouts. Rewriting them to CRLF would make
    # the committed stubs differ by platform and every parity diff would be noise.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(fixed)
    print(f"  fixed relative import: {path.name}")
PY
}

for out_dir in "${GATEWAY_OUT}" "${SCORER_OUT}"; do
  fix_imports "${out_dir}/voice_scorer_pb2_grpc.py"
done

# --- verify -------------------------------------------------------------------------------------
#
# Import the generated modules as package members and round-trip a message. A stub that parses but
# cannot serialize the contract is worse than a missing one: it fails at the first window of the
# first demo session.
"${PYTHON}" - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
failures = []

for package_root, dotted in ((repo_root / "gateway", "app.scorer"), (repo_root / "scorer", "app")):
    sys.path.insert(0, str(package_root))
    try:
        import importlib

        pb = importlib.import_module(f"{dotted}.voice_scorer_pb2")
        importlib.import_module(f"{dotted}.voice_scorer_pb2_grpc")

        request = pb.ScoreWindowRequest(contract_id="raw-waveform-v1", sample_rate_hz=16000)
        assert pb.ScoreWindowRequest.FromString(request.SerializeToString()) == request
        assert pb.DetectorMode.Name(2) == "MOCK_SMOKE_MODE_NOT_A_DETECTOR"
    except Exception as exc:  # noqa: BLE001 - reported, then the next tree is still checked
        failures.append(f"{dotted}: {type(exc).__name__}: {exc}")
    finally:
        sys.path.remove(str(package_root))
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]

if failures:
    print("gen_proto: generated stubs do not import cleanly:", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    raise SystemExit(1)

print("  verified: both stub sets import and round-trip a ScoreWindowRequest")
PY

printf 'gen_proto: OK (grpcio-tools %s)\n' "${EXPECTED_GRPCIO_TOOLS}"
printf '  %s/voice_scorer_pb2{,_grpc}.py\n' "${GATEWAY_OUT#"${REPO_ROOT}/"}"
printf '  %s/voice_scorer_pb2{,_grpc}.py\n' "${SCORER_OUT#"${REPO_ROOT}/"}"
printf 'Commit the generated files. CI asserts `git diff --exit-code` after re-running this script.\n'
