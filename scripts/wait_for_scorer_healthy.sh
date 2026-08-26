#!/usr/bin/env bash
# wait_for_scorer_healthy.sh — block until the Scorer's Health RPC is genuinely usable.
#
#   scripts/wait_for_scorer_healthy.sh                                  # localhost:50051, 120s
#   scripts/wait_for_scorer_healthy.sh --target scorer:50051 --timeout 180
#   scripts/wait_for_scorer_healthy.sh --expect-real                    # GPU tier / any real run
#   scripts/wait_for_scorer_healthy.sh --expect-provider CUDAExecutionProvider
#
# WHY THIS IS NOT `sleep 30`
# -------------------------
# The Gateway waits SCORER_WAIT_SECONDS=120 for the Scorer at startup, and the Scorer runs its
# artifact checks BEFORE binding the port (scorer/app/server.py) — so "port open" already means
# "artifacts loaded". A fixed sleep is wrong in both directions: too short and CI fails on a cold
# ONNX load, too long and every job pays for the worst case. Bounded polling with a real RPC is the
# only form that is both fast and honest.
#
# THE DISTINCTION THAT MATTERS
# ----------------------------
# Three states look identical to `sleep`, and this script separates them because they need different
# human responses:
#
#   NOT UP YET        connection refused / UNAVAILABLE / ready=false. Keep waiting. Normal.
#   UP BUT MOCKED     ready=true, detector_mode=MOCK_SMOKE_MODE_NOT_A_DETECTOR. Waiting longer will
#                     never fix this — the process is healthy and is not a detector (rules.md R-46).
#                     Fails IMMEDIATELY with exit 3 under --expect-real, because a latency or
#                     accuracy number captured against mock mode is not a measurement of anything,
#                     and the most expensive version of this mistake is the one nobody noticed.
#   UP BUT ON CPU     ready=true, execution_provider is not the one requested. A silent CPU fallback
#                     on the GPU tier is a failure, not a degradation (rules.md R-45): it invalidates
#                     every latency number recorded that day. Exit 4 under --expect-provider.
#
# Exit codes
#   0  ready, and every assertion requested was satisfied
#   1  timed out (with the last observed state printed, not just "timeout")
#   2  could not probe at all — no usable gRPC client (this is not a pass)
#   3  ready, but reporting mock mode when a real detector was expected
#   4  ready, but running a different execution provider than the one asserted
#
# 3 and 4 are separate from 1 on purpose: a timeout means "wait longer or look at the logs", while 3
# and 4 mean "the thing came up, and it is not the thing you asked for". Collapsing them into one
# code is how a mock-mode demo reaches a judge.

set -euo pipefail

TARGET="${SCORER_TARGET:-localhost:50051}"
TIMEOUT_SECONDS="${SCORER_WAIT_SECONDS:-120}"
INTERVAL_SECONDS=2
EXPECT_REAL=0
EXPECT_PROVIDER=""
EXPECT_PARITY=1
QUIET=0

SERVICE="sih26104.scorer.v1.VoiceScorer"   # contracts/voice_scorer.proto: package + service
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)          TARGET="$2"; shift 2 ;;
    --timeout)         TIMEOUT_SECONDS="$2"; shift 2 ;;
    --interval)        INTERVAL_SECONDS="$2"; shift 2 ;;
    --expect-real)     EXPECT_REAL=1; shift ;;
    --expect-provider) EXPECT_PROVIDER="$2"; shift 2 ;;
    --no-parity-check) EXPECT_PARITY=0; shift ;;
    --quiet)           QUIET=1; shift ;;
    -h|--help)         usage ;;
    *) echo "wait_for_scorer_healthy: unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { [[ "$QUIET" -eq 1 ]] || printf '%s\n' "$*"; }

# ---------------------------------------------------------------------------------------------------
# Probe backend selection.
#
# Two backends because neither is universally present. The generated Python stubs ship inside the
# Scorer image and are produced by scripts/gen_proto.sh in CI, so they are the primary path and need
# no extra tooling. grpcurl is the fallback for an operator on a laptop who has it installed but no
# Python environment. If neither works the script exits 2 rather than 0 — an unprobed Scorer must
# never read as a healthy one (rules.md R-52).
# ---------------------------------------------------------------------------------------------------

PYTHON_BIN=""
for candidate in "${PYTHON:-}" python3 python; do
  [[ -n "$candidate" ]] || continue
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import grpc' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

BACKEND=""
if [[ -n "$PYTHON_BIN" ]]; then
  BACKEND="python"
elif command -v grpcurl >/dev/null 2>&1; then
  BACKEND="grpcurl"
else
  cat >&2 <<'EOF'
wait_for_scorer_healthy: FATAL: no way to make a gRPC call.
  Needs either a Python with `grpc` importable (pip install -r scorer/requirements.txt, then
  scripts/gen_proto.sh for the stubs) or `grpcurl` on PATH.
  Exiting 2, not 0: "I could not check the Scorer" must never be recorded as "the Scorer is healthy".
EOF
  exit 2
fi

# ---------------------------------------------------------------------------------------------------
# One probe. Prints a single line of `key=value` pairs on success, nothing on failure.
# Never `set -e`-fatal: a refused connection is the expected state during startup, not an error.
# ---------------------------------------------------------------------------------------------------

probe_python() {
  PYTHONPATH="${REPO_ROOT}/scorer:${PYTHONPATH:-}" "$PYTHON_BIN" - "$TARGET" <<'PY' 2>/dev/null
import sys

try:
    import grpc
    from app import voice_scorer_pb2 as pb
    from app import voice_scorer_pb2_grpc as pb_grpc
except ImportError as exc:
    print(f"probe_error=import:{type(exc).__name__}")
    raise SystemExit(9)

target = sys.argv[1]
try:
    with grpc.insecure_channel(target) as channel:
        stub = pb_grpc.VoiceScorerStub(channel)
        # A short deadline on purpose: the retry loop in the shell owns the overall budget, so a
        # single slow probe must not silently consume it.
        reply = stub.Health(pb.HealthRequest(), timeout=3.0)
except grpc.RpcError as exc:
    print(f"probe_error=rpc:{exc.code().name}")
    raise SystemExit(1)
except Exception as exc:  # noqa: BLE001 - any failure here is "not up yet"
    print(f"probe_error=other:{type(exc).__name__}")
    raise SystemExit(1)

mode = pb.DetectorMode.Name(reply.detector_mode)
print(
    f"ready={str(reply.ready).lower()} "
    f"detector_mode={mode} "
    f"execution_provider={reply.execution_provider or 'unset'} "
    f"model_version={reply.model_version or 'unset'} "
    f"model_sha256={(reply.model_sha256 or 'unset')[:12]} "
    f"calibration_version={reply.calibration_version or 'unset'} "
    f"artifact_state={reply.artifact_state or 'unset'} "
    f"parity_ok={str(reply.contract_vector_parity_ok).lower()}"
)
PY
}

probe_grpcurl() {
  local json
  json="$(grpcurl -plaintext -max-time 3 -d '{}' "$TARGET" "${SERVICE}/Health" 2>/dev/null)" || return 1
  [[ -n "$json" ]] || return 1
  # Deliberately not jq: jq is absent on plenty of machines, and this reads flat scalar fields from a
  # response we control the schema of. `grep -o` on a known key is adequate and dependency-free.
  local field
  field() {
    printf '%s' "$json" \
      | tr -d ' \n' \
      | grep -o "\"$1\":\(\"[^\"]*\"\|true\|false\|[0-9]*\)" \
      | head -1 | cut -d: -f2- | tr -d '"'
  }
  local ready mode provider
  ready="$(field ready)";              ready="${ready:-false}"
  mode="$(field detectorMode)";        mode="${mode:-$(field detector_mode)}"
  provider="$(field executionProvider)"; provider="${provider:-$(field execution_provider)}"
  printf 'ready=%s detector_mode=%s execution_provider=%s model_version=%s model_sha256=%s calibration_version=%s artifact_state=%s parity_ok=%s\n' \
    "$ready" "${mode:-unset}" "${provider:-unset}" \
    "$(field modelVersion)" "$(field modelSha256)" "$(field calibrationVersion)" \
    "$(field artifactState)" "$(field contractVectorParityOk)"
}

probe() {
  if [[ "$BACKEND" == "python" ]]; then probe_python; else probe_grpcurl; fi
}

field_of() {
  # Extract key=value from a probe line without eval'ing server-controlled text into the shell.
  printf '%s' "$1" | tr ' ' '\n' | grep "^$2=" | head -1 | cut -d= -f2-
}

# ---------------------------------------------------------------------------------------------------
# Poll.
# ---------------------------------------------------------------------------------------------------

log "wait_for_scorer_healthy: target=${TARGET} timeout=${TIMEOUT_SECONDS}s backend=${BACKEND}"
[[ "$EXPECT_REAL" -eq 1 ]] && log "  asserting: detector_mode=REAL_DETECTOR (rules.md R-46)"
[[ -n "$EXPECT_PROVIDER" ]] && log "  asserting: execution_provider=${EXPECT_PROVIDER} (rules.md R-45)"

DEADLINE=$(( $(date +%s) + TIMEOUT_SECONDS ))
ATTEMPT=0
LAST_LINE=""
LAST_REASON="no response yet — nothing has answered on ${TARGET}"

while :; do
  ATTEMPT=$((ATTEMPT + 1))
  LINE="$(probe || true)"

  if [[ -n "$LINE" && "$LINE" != probe_error=* ]]; then
    LAST_LINE="$LINE"
    READY="$(field_of "$LINE" ready)"
    MODE="$(field_of "$LINE" detector_mode)"
    PROVIDER="$(field_of "$LINE" execution_provider)"
    PARITY="$(field_of "$LINE" parity_ok)"

    if [[ "$READY" == "true" ]]; then
      log ""
      log "wait_for_scorer_healthy: Health answered ready=true after ${ATTEMPT} attempt(s)."
      log "  ${LINE}"

      # --- The three assertions. Each is a separate exit code and each fails FAST. -----------------
      # None of these retry. The Scorer is up and self-describing; polling a stable, wrong answer
      # just burns the timeout and then reports the wrong cause.

      if [[ "$EXPECT_REAL" -eq 1 && "$MODE" != "REAL_DETECTOR" ]]; then
        cat >&2 <<EOF

==================================================================================================
wait_for_scorer_healthy: UP, BUT NOT A DETECTOR
==================================================================================================
  The Scorer is healthy and answering. It reports detector_mode=${MODE}.

  This is NOT a startup race and waiting longer will not change it. The process is running as a
  smoke-test stub: it returns synthetic scores. Any latency, accuracy, or policy-behaviour number
  captured against it measures the stub (rules.md R-46 — mock mode is loud, and this is it being
  loud).

  Fix the cause, do not retry:
    * the model artifact is missing or failed to load -> check the Scorer startup banner
    * DETECTOR_MODE / the release manifest declares mock mode -> that declaration is correct and
      the caller's --expect-real is what is wrong
    * you are on the local CPU profile and expected the GPU tier

  Observed: ${LINE}
EOF
        exit 3
      fi

      if [[ -n "$EXPECT_PROVIDER" && "$PROVIDER" != "$EXPECT_PROVIDER" ]]; then
        cat >&2 <<EOF

==================================================================================================
wait_for_scorer_healthy: UP, BUT ON THE WRONG EXECUTION PROVIDER
==================================================================================================
  Asserted ${EXPECT_PROVIDER}; the Scorer reports ${PROVIDER} actually in use.

  A silent CPU fallback on the GPU tier is a failure, not a degradation (rules.md R-45). It does not
  break correctness, which is exactly why it is dangerous: every latency measurement taken after
  this point is a CPU measurement wearing a GPU label, and a p95 recorded now would be quoted later
  as a GPU number.

  Observed: ${LINE}
EOF
        exit 4
      fi

      PARITY_WARNED=0
      if [[ "$EXPECT_PARITY" -eq 1 && "$PARITY" == "false" ]]; then
        # A warning, not a failure: parity_ok is the Scorer's own startup fixed-vector check, and its
        # authoritative gate is contract-check.yml. Failing here too would double-report one defect.
        PARITY_WARNED=1
        log ""
        log "  WARNING: contract_vector_parity_ok=false. The Scorer's startup fixed-vector check did"
        log "  not pass, so CPU/GPU parity is unproven for this process (architecture.md 5.1)."
        log "  contract-check is the gate for this; it is surfaced here because it is visible now."
      fi

      log ""
      if [[ "$PARITY_WARNED" -eq 1 ]]; then
        # Never print an unqualified all-clear over a warning (rules.md R-52).
        log "wait_for_scorer_healthy: READY, with the parity warning above outstanding."
      else
        log "wait_for_scorer_healthy: READY. All requested assertions hold."
      fi
      exit 0
    fi

    LAST_REASON="answered, but ready=false (${LINE})"
  elif [[ "$LINE" == probe_error=* ]]; then
    LAST_REASON="probe could not reach it: ${LINE#probe_error=}"
    if [[ "$LINE" == probe_error=import:* ]]; then
      cat >&2 <<EOF
wait_for_scorer_healthy: FATAL: the Python gRPC stubs are not importable (${LINE#probe_error=}).
  Generate them with scripts/gen_proto.sh, or install grpcurl.
  Exiting 2, not 1: this is a tooling failure, not a Scorer timeout, and the two need different fixes.
EOF
      exit 2
    fi
  fi

  NOW="$(date +%s)"
  if (( NOW >= DEADLINE )); then
    cat >&2 <<EOF

==================================================================================================
wait_for_scorer_healthy: TIMED OUT after ${TIMEOUT_SECONDS}s (${ATTEMPT} attempts)
==================================================================================================
  target : ${TARGET}
  backend: ${BACKEND}
  last   : ${LAST_REASON}
$( [[ -n "$LAST_LINE" ]] && printf '  last full response: %s\n' "$LAST_LINE" )

  What this narrows it to:
    * nothing ever answered  -> the container is not running, crashed at startup, or the port/host
      is wrong. The Scorer runs its artifact checks BEFORE binding the port, so a never-open port
      often means an artifact check failed. Read the Scorer logs, not this output.
    * answered with ready=false -> it is up and deliberately reporting not-ready. The reason is in
      its startup banner.

  Note the Gateway's own budget is SCORER_WAIT_SECONDS=120 (gateway/app/main.py); a timeout longer
  than that here just moves the failure to the Gateway.
EOF
    exit 1
  fi

  if (( ATTEMPT == 1 )) || (( ATTEMPT % 5 == 0 )); then
    log "  attempt ${ATTEMPT} ($(( DEADLINE - NOW ))s left): ${LAST_REASON}"
  fi
  sleep "$INTERVAL_SECONDS"
done
