#!/usr/bin/env bash
# contract_test.sh — assert the running Gateway matches contracts/, against ANY environment.
#
#   scripts/contract_test.sh                                  # local Compose (https://localhost:8443)
#   scripts/contract_test.sh http://localhost:8000            # bare uvicorn, no Caddy
#   scripts/contract_test.sh https://demo.example.invalid      # a deployed environment
#   scripts/contract_test.sh --token "$JWT" https://…          # include the authenticated checks
#   scripts/contract_test.sh --json                            # machine-readable summary
#
# WHY A BASE URL IS AN ARGUMENT AND NOT A CONSTANT
# -----------------------------------------------
# The same suite has to run in three places or it is not a contract test: on a laptop before a push,
# in CI against Compose, and against the deployed stack after a `deploy-runtime` promotion. Deployment
# tier is configuration, never a code branch (rules.md R-04) — a test suite that only knows how to
# reach localhost quietly becomes a local-only test, and the deployed environment ends up verified by
# clicking around in a browser. The one thing that legitimately differs by target is whether the
# local `contracts/` tree should match the deployed hashes; see T-05.
#
# WHAT THIS IS NOT
# ----------------
# This is a black-box HTTP contract check. It does NOT open a WebSocket, send a 648-byte frame, or
# exercise the policy engine — those need a real audio stream and live in the `contract` and
# `integration` pytest suites. Coverage is stated explicitly at the end rather than implied
# (rules.md R-52), because "contract tests passed" is exactly the phrase that gets quoted as
# "the contract is verified".
#
# Exit codes: 0 all checks passed (warnings allowed) · 1 a check failed · 2 the suite could not run.

set -uo pipefail   # NOT -e: every check must run so the report is complete, not stop at the first red

BASE_URL="${GATEWAY_BASE_URL:-https://localhost:8443}"
TOKEN="${GATEWAY_TOKEN:-}"
INSECURE=0
JSON_OUT=0
REQUIRE_LOCAL_CONTRACTS=""   # empty = decide from the base URL; see T-05
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0; FAIL=0; WARN=0; SKIP=0
declare -a RESULTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)            TOKEN="$2"; shift 2 ;;
    --insecure|-k)      INSECURE=1; shift ;;
    --json)             JSON_OUT=1; shift ;;
    --local-contracts)  REQUIRE_LOCAL_CONTRACTS=1; shift ;;
    --no-local-contracts) REQUIRE_LOCAL_CONTRACTS=0; shift ;;
    -h|--help)          sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)                 echo "contract_test: unknown flag: $1" >&2; exit 2 ;;
    *)                  BASE_URL="${1%/}"; shift ;;
  esac
done

command -v curl >/dev/null 2>&1 || {
  echo "contract_test: FATAL: curl is not on PATH. Exiting 2 — an unrun suite is not a passing suite." >&2
  exit 2
}

# mkcert's local CA is trusted on a developer machine but not inside a CI container, so a self-signed
# localhost certificate is expected there. Auto-relaxing only for localhost keeps a real deployed
# target's TLS failure a genuine failure instead of something the script waves through.
CURL_TLS=()
if [[ "$INSECURE" -eq 1 ]] || [[ "$BASE_URL" == https://localhost* ]] || [[ "$BASE_URL" == https://127.0.0.1* ]]; then
  CURL_TLS=(--insecure)
fi

if [[ -z "$REQUIRE_LOCAL_CONTRACTS" ]]; then
  # Default: demand hash agreement for a local target (same tree, so a mismatch is a real defect) and
  # only warn for a remote one (a deployed build legitimately predates the working tree).
  if [[ "$BASE_URL" == *localhost* ]] || [[ "$BASE_URL" == *127.0.0.1* ]]; then
    REQUIRE_LOCAL_CONTRACTS=1
  else
    REQUIRE_LOCAL_CONTRACTS=0
  fi
fi

# ---------------------------------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------------------------------

ok()   { PASS=$((PASS+1)); RESULTS+=("PASS|$1|$2"); printf '  \033[32mPASS\033[0m  [%s] %s\n' "$1" "$2"; }
bad()  { FAIL=$((FAIL+1)); RESULTS+=("FAIL|$1|$2"); printf '  \033[31mFAIL\033[0m  [%s] %s\n' "$1" "$2"; }
warn() { WARN=$((WARN+1)); RESULTS+=("WARN|$1|$2"); printf '  \033[33mWARN\033[0m  [%s] %s\n' "$1" "$2"; }
skip() { SKIP=$((SKIP+1)); RESULTS+=("SKIP|$1|$2"); printf '  SKIP  [%s] %s\n' "$1" "$2"; }

# Dependency-free JSON scalar read. Not jq: jq is absent on many machines and in slim CI images, and
# adding a hard dependency is how a contract test becomes a test nobody can run. These are flat scalar
# fields in a schema we own, so this is sufficient — it is deliberately NOT a general JSON parser.
jget() {
  printf '%s' "$1" | tr -d '\n' \
    | grep -o "\"$2\"[[:space:]]*:[[:space:]]*\(\"[^\"]*\"\|true\|false\|null\|-\?[0-9.]*\)" \
    | head -1 | sed 's/^[^:]*:[[:space:]]*//; s/^"//; s/"$//'
}

# Writes the body to $BODY and the status to $STATUS. Returns non-zero only if curl itself failed.
BODY=""; STATUS=""
request() {
  local method="$1" path="$2" data="${3:-}"
  local tmp; tmp="$(mktemp)"
  local args=(-s -S -o "$tmp" -w '%{http_code}' -X "$method" "${CURL_TLS[@]}" --max-time 20)
  [[ -n "$TOKEN" ]] && args+=(-H "Authorization: Bearer ${TOKEN}")
  if [[ -n "$data" ]]; then
    args+=(-H 'Content-Type: application/json' -d "$data")
  fi
  STATUS="$(curl "${args[@]}" "${BASE_URL}${path}" 2>/dev/null)" || { STATUS="000"; }
  BODY="$(cat "$tmp")"; rm -f "$tmp"
  [[ "$STATUS" != "000" ]]
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum   >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"
  fi
}

echo "contract_test: target ${BASE_URL}"
echo "  local-contract hash agreement: $([[ "$REQUIRE_LOCAL_CONTRACTS" -eq 1 ]] && echo required || echo advisory)"
echo "  authenticated checks: $([[ -n "$TOKEN" ]] && echo enabled || echo 'SKIPPED (no --token / $GATEWAY_TOKEN)')"
echo ""

# ---------------------------------------------------------------------------------------------------
# T-01  /healthz — liveness, and the cheapest proof the target is the Gateway at all
# ---------------------------------------------------------------------------------------------------
if ! request GET /healthz; then
  cat >&2 <<EOF
contract_test: FATAL: cannot reach ${BASE_URL}/healthz at all.
  Nothing else can be checked, so exiting 2 rather than reporting a wall of failures that all share
  one cause. Check the container is up, the port is right, and (for https://localhost) that the
  mkcert CA is installed or that you passed --insecure.
EOF
  exit 2
fi
if [[ "$STATUS" == "200" ]]; then
  # openapi.yaml types this as text/plain "ok". A JSON body here means someone changed the contract.
  if [[ "$(printf '%s' "$BODY" | tr -d '[:space:]')" == "ok" ]]; then
    ok T-01 "/healthz returns 200 with body 'ok'"
  else
    bad T-01 "/healthz returned 200 but body is '$(printf '%s' "$BODY" | head -c 40)', not 'ok'"
  fi
else
  bad T-01 "/healthz returned HTTP ${STATUS}, expected 200"
fi

# ---------------------------------------------------------------------------------------------------
# T-02  /readyz — must name WHICH dependency is down, not just fail
#
# 503 is a legitimate, contract-defined answer (the endpoint returns ReadinessReport with 503 when not
# ready). Treating 503 as a suite failure would make this script unusable as a startup probe, so the
# check is on the SHAPE and on the per-subsystem detail: a readiness endpoint that cannot tell you
# which dependency is missing is a readiness endpoint that generates a debugging session.
# ---------------------------------------------------------------------------------------------------
request GET /readyz
READY_BODY="$BODY"; READY_STATUS="$STATUS"
MISSING=""
for subsystem in database scorer policy_bundle secrets; do
  value="$(jget "$READY_BODY" "$subsystem")"
  [[ "$value" == "false" ]] && MISSING="${MISSING}${subsystem} "
done
if [[ "$READY_STATUS" == "200" && "$(jget "$READY_BODY" ready)" == "true" ]]; then
  ok T-02 "/readyz 200 and ready=true (database, scorer, policy_bundle, secrets all up)"
elif [[ "$READY_STATUS" == "503" ]]; then
  if [[ -n "$MISSING" ]]; then
    warn T-02 "/readyz 503 — not ready. Down: ${MISSING%% }. Contract-correct response; the service is not usable yet."
  else
    bad T-02 "/readyz 503 but no subsystem is reported false. The report cannot say what is wrong, which is the one thing it exists to do."
  fi
else
  bad T-02 "/readyz returned HTTP ${READY_STATUS}; the contract allows only 200 or 503"
fi

# ---------------------------------------------------------------------------------------------------
# T-03  /api/v1/version — every parity-set field is present and populated
#
# architecture.md 5.1 defines the parity set; rules.md R-51 forbids a release without a manifest
# carrying it. This endpoint is where a human or a judge reads it back off a running system, so an
# empty field is a hole in the only artifact that connects "what is running" to "what was tested".
# ---------------------------------------------------------------------------------------------------
request GET /api/v1/version
VERSION_BODY="$BODY"
if [[ "$STATUS" != "200" ]]; then
  bad T-03 "/api/v1/version returned HTTP ${STATUS}, expected 200"
else
  # The six openapi.yaml marks `required`, plus the artifact/model fields the parity set needs.
  REQUIRED_VERSION_FIELDS=(git_commit deployment_profile api_schema_sha256 proto_sha256
                           policy_bundle_sha256 artifact_state)
  ADVISORY_VERSION_FIELDS=(execution_provider policy_version model_version model_sha256
                           calibration_version calibration_sha256 migration_head detector_mode)
  BLANK=""; ADVISORY_BLANK=""
  for f in "${REQUIRED_VERSION_FIELDS[@]}"; do
    v="$(jget "$VERSION_BODY" "$f")"
    [[ -z "$v" || "$v" == "null" ]] && BLANK="${BLANK}${f} "
  done
  for f in "${ADVISORY_VERSION_FIELDS[@]}"; do
    v="$(jget "$VERSION_BODY" "$f")"
    [[ -z "$v" || "$v" == "null" ]] && ADVISORY_BLANK="${ADVISORY_BLANK}${f} "
  done
  if [[ -z "$BLANK" ]]; then
    ok T-03 "/api/v1/version reports all required parity fields (git_commit=$(jget "$VERSION_BODY" git_commit | head -c 12), profile=$(jget "$VERSION_BODY" deployment_profile))"
  else
    bad T-03 "/api/v1/version is missing or empty for required field(s): ${BLANK%% }"
  fi
  [[ -n "$ADVISORY_BLANK" ]] && warn T-03 "optional parity field(s) empty: ${ADVISORY_BLANK%% } (expected while artifacts are placeholders)"
fi

# ---------------------------------------------------------------------------------------------------
# T-04  the contract hashes are not the string "unavailable"
#
# gateway/app/main.py::_hash_contract returns "unavailable" when the contract file cannot be read.
# That is a deliberate non-crashing fallback, and it is precisely the failure this check exists for:
# the service starts, serves traffic, answers /readyz, and silently reports no contract hash. Parity
# then cannot be established for a build that looks completely healthy. A missing hash must be loud
# somewhere, and this is the somewhere.
# ---------------------------------------------------------------------------------------------------
API_SHA="$(jget "$VERSION_BODY" api_schema_sha256)"
PROTO_SHA="$(jget "$VERSION_BODY" proto_sha256)"
UNAVAILABLE=""
[[ "$API_SHA"   == "unavailable" ]] && UNAVAILABLE="${UNAVAILABLE}api_schema_sha256 "
[[ "$PROTO_SHA" == "unavailable" ]] && UNAVAILABLE="${UNAVAILABLE}proto_sha256 "
if [[ -n "$UNAVAILABLE" ]]; then
  bad T-04 "${UNAVAILABLE%% } reported as 'unavailable' — the image cannot read its own contracts/ files. The service is healthy and its parity set is unverifiable (architecture.md 5.1). Check the Dockerfile COPY of contracts/."
elif [[ -n "$API_SHA" && -n "$PROTO_SHA" ]]; then
  ok T-04 "both contract hashes are real values, not 'unavailable'"
fi

# ---------------------------------------------------------------------------------------------------
# T-05  served hashes match the local contracts/ tree
#
# Promotion is by digest, and the parity set names the OpenAPI and proto hashes (R-56, R-51). Against
# a local target the tree and the image come from the same commit, so a mismatch means the image is
# stale — the single most common way to spend an hour debugging a contract that was already fixed.
# Against a deployed target a mismatch is expected whenever the working tree has moved on, so it is
# reported as a warning WITH both hashes rather than either failing or staying silent.
# ---------------------------------------------------------------------------------------------------
LOCAL_API_SHA=""; LOCAL_PROTO_SHA=""
[[ -f "${REPO_ROOT}/contracts/openapi.yaml"     ]] && LOCAL_API_SHA="$(sha256_of "${REPO_ROOT}/contracts/openapi.yaml")"
[[ -f "${REPO_ROOT}/contracts/voice_scorer.proto" ]] && LOCAL_PROTO_SHA="$(sha256_of "${REPO_ROOT}/contracts/voice_scorer.proto")"

if [[ -z "$LOCAL_API_SHA" || -z "$LOCAL_PROTO_SHA" ]]; then
  skip T-05 "no local contracts/ tree to compare against (running outside a checkout)"
elif [[ -z "$API_SHA" || "$API_SHA" == "unavailable" ]]; then
  skip T-05 "served hashes unusable — see T-04"
else
  for pair in "openapi:${LOCAL_API_SHA}:${API_SHA}" "proto:${LOCAL_PROTO_SHA}:${PROTO_SHA}"; do
    name="${pair%%:*}"; rest="${pair#*:}"; want="${rest%%:*}"; got="${rest#*:}"
    if [[ "$want" == "$got" ]]; then
      ok T-05 "${name} hash matches the local contracts/ tree (${got:0:12})"
    elif [[ "$REQUIRE_LOCAL_CONTRACTS" -eq 1 ]]; then
      bad T-05 "${name} hash mismatch: local ${want:0:12}, served ${got:0:12}. The running image was built from different contracts/ — rebuild before trusting any other result here."
    else
      warn T-05 "${name} hash differs from this working tree: local ${want:0:12}, served ${got:0:12}. Expected if the deployment predates your checkout; confirm against the release manifest, not against your tree."
    fi
  done
fi

# ---------------------------------------------------------------------------------------------------
# T-06  mock mode is visible, and never coexists with policy_eligible
#
# rules.md R-46: mock mode is loud — it appears in every gRPC response, every audit row, and the UI.
# R-11/R-46: it must refuse to start when the release manifest asserts policy_eligible. The dangerous
# combination is a stub detector behind an artifact state that says its scores may drive a high-risk
# action; this asserts that pairing cannot be observed from outside.
# ---------------------------------------------------------------------------------------------------
DETECTOR_MODE="$(jget "$VERSION_BODY" detector_mode)"
ARTIFACT_STATE="$(jget "$VERSION_BODY" artifact_state)"
if [[ "$DETECTOR_MODE" == "MOCK_SMOKE_MODE_NOT_A_DETECTOR" ]]; then
  if [[ "$ARTIFACT_STATE" == "policy_eligible" ]]; then
    bad T-06 "detector_mode=MOCK_SMOKE_MODE_NOT_A_DETECTOR with artifact_state=policy_eligible. A stub is declared fit to drive high-risk actions. It must refuse to start in this pairing (rules.md R-46)."
  else
    warn T-06 "detector_mode=MOCK_SMOKE_MODE_NOT_A_DETECTOR (artifact_state=${ARTIFACT_STATE}). Correct and loud, but NO number measured against this target describes a detector."
  fi
elif [[ "$DETECTOR_MODE" == "REAL_DETECTOR" ]]; then
  ok T-06 "detector_mode=REAL_DETECTOR, artifact_state=${ARTIFACT_STATE}"
elif [[ -n "$DETECTOR_MODE" ]]; then
  bad T-06 "detector_mode=${DETECTOR_MODE} is outside the proto enum (REAL_DETECTOR | MOCK_SMOKE_MODE_NOT_A_DETECTOR)"
fi

# ---------------------------------------------------------------------------------------------------
# T-07  the served schema contains no `approve` / `deny`
#
# rules.md R-07: the action vocabulary is closed to continue | verify | hold | escalate, enforced
# structurally so adding one is not a one-line change. Checking the SERVED schema rather than the file
# is the point — contract-check.yml already reads the file, and this catches a schema that diverged at
# runtime (a hand-edited model, a middleware rewrite, a stale image).
# ---------------------------------------------------------------------------------------------------
request GET /openapi.json
if [[ "$STATUS" == "200" ]]; then
  BANNED=""
  # Word-boundary match on quoted enum values: avoids firing on prose like "approved by".
  printf '%s' "$BODY" | grep -qE '"approve"|"approved"' && BANNED="approve "
  printf '%s' "$BODY" | grep -qE '"deny"|"denied"'      && BANNED="${BANNED}deny"
  if [[ -z "$BANNED" ]]; then
    ok T-07 "served schema contains no 'approve'/'deny' action value (rules.md R-07)"
  else
    bad T-07 "served schema contains a forbidden action value: ${BANNED}. The vocabulary is closed: continue | verify | hold | escalate. A direct approve/deny is an irreversible side effect the system must not be able to express."
  fi
else
  skip T-07 "/openapi.json not served (HTTP ${STATUS}) — action vocabulary unverified at runtime"
fi

# ---------------------------------------------------------------------------------------------------
# Authenticated checks
# ---------------------------------------------------------------------------------------------------
if [[ -z "$TOKEN" ]]; then
  skip T-08 "POST /api/v1/sessions pseudonymization — needs a bearer token"
  skip T-09 "static error messages (R-17) — needs a bearer token"
  skip T-10 "POST /api/v1/stream-ticket — needs a bearer token"
  skip T-11 "GET /api/v1/sessions/{id}/audit chain_verified — needs a bearer token"
else
  # T-08 — R-16: the raw client_call_ref must not survive the round trip.
  #
  # A deliberately distinctive marker string, so finding it anywhere in the response is unambiguous
  # rather than a coincidental substring match. This is the strongest privacy assertion available
  # over plain HTTP: /api/v1/sessions is the ONLY endpoint that accepts a human-readable reference,
  # and the raw value must never be logged, stored, forwarded, or returned.
  MARKER="CTRTEST-RAW-DO-NOT-ECHO-$$"
  request POST /api/v1/sessions \
    "{\"client_call_ref\":\"${MARKER}\",\"purpose_code\":\"payment_authorization\",\"context_value_band\":\"medium\",\"consent_acknowledged\":true}"
  SESSION_BODY="$BODY"
  if [[ "$STATUS" == "201" ]]; then
    CALL_REF="$(jget "$SESSION_BODY" call_ref)"
    SESSION_ID="$(jget "$SESSION_BODY" session_id)"
    if printf '%s' "$SESSION_BODY" | grep -qF "$MARKER"; then
      bad T-08 "the raw client_call_ref was echoed back in the response body. The raw value must never leave Gateway process memory (rules.md R-16)."
    elif [[ ! "$CALL_REF" =~ ^[0-9a-f]{64}$ ]]; then
      bad T-08 "call_ref '${CALL_REF:0:20}' is not 64 lowercase hex characters — it does not look like an HMAC-SHA256 pseudonym"
    else
      ok T-08 "raw client_call_ref absent from the response; call_ref is a 64-hex pseudonym (${CALL_REF:0:12})"
    fi
  elif [[ "$STATUS" == "401" || "$STATUS" == "403" ]]; then
    bad T-08 "POST /api/v1/sessions returned ${STATUS} — the supplied token was rejected"
  else
    bad T-08 "POST /api/v1/sessions returned HTTP ${STATUS}, expected 201"
  fi

  # T-09 — R-17: an error must not interpolate the client's input.
  #
  # Over-length client_call_ref (maxLength 128) is used because it is guaranteed invalid AND the
  # offending value is exactly the kind of caller reference that must not escape into a log. If it
  # appears in the error body it is already in the log, and from there in whatever ships logs.
  LONG_MARKER="CTRTEST-OVERLONG-$(printf 'X%.0s' $(seq 1 200))"
  request POST /api/v1/sessions \
    "{\"client_call_ref\":\"${LONG_MARKER}\",\"purpose_code\":\"payment_authorization\",\"context_value_band\":\"medium\",\"consent_acknowledged\":true}"
  if [[ "$STATUS" == "400" || "$STATUS" == "422" ]]; then
    if printf '%s' "$BODY" | grep -qF "CTRTEST-OVERLONG"; then
      bad T-09 "the ${STATUS} error body echoes the rejected client_call_ref. Error messages are static and never interpolate client input (rules.md R-17) — this is the documented path for a caller reference to escape into a log."
    elif [[ -n "$(jget "$BODY" code)" ]]; then
      ok T-09 "invalid input rejected with ${STATUS}, static body, code=$(jget "$BODY" code), no echo of the input"
    else
      warn T-09 "invalid input rejected with ${STATUS} and no echo, but the body has no 'code' field (Error schema requires code + message)"
    fi
  else
    bad T-09 "an over-length client_call_ref returned HTTP ${STATUS}; expected 400 or 422. Wrong-shaped input is rejected, never coerced (rules.md R-24)."
  fi

  # T-10 — the ticket exists so the bearer token never appears in a WebSocket URL.
  if [[ -n "${SESSION_ID:-}" ]]; then
    request POST /api/v1/stream-ticket "{\"session_id\":\"${SESSION_ID}\"}"
    if [[ "$STATUS" == "201" ]]; then
      TICKET="$(jget "$BODY" ticket)"
      if [[ -n "$TICKET" ]]; then
        ok T-10 "stream ticket minted (201), $(printf '%s' "$TICKET" | wc -c | tr -d ' ') chars"
      else
        warn T-10 "stream-ticket returned 201 but no 'ticket' field was found in the body"
      fi
    else
      bad T-10 "POST /api/v1/stream-ticket returned HTTP ${STATUS}, expected 201"
    fi

    # T-11 — the audit endpoint self-verifies its hash chain.
    request GET "/api/v1/sessions/${SESSION_ID}/audit"
    if [[ "$STATUS" == "200" ]]; then
      VERIFIED="$(jget "$BODY" chain_verified)"
      if [[ "$VERIFIED" == "true" ]]; then
        ok T-11 "audit chain_verified=true for the new session"
      elif [[ "$VERIFIED" == "false" ]]; then
        bad T-11 "audit chain_verified=false. Run scripts/verify_audit_chain.py to find the first divergent event_seq before treating this as tampering (a rotated AUDIT_CHAIN_KEY looks identical)."
      else
        bad T-11 "audit response has no 'chain_verified' field; the schema requires it"
      fi
      if printf '%s' "$BODY" | grep -qiE '"(audio|pcm|waveform|transcript|embedding|phone|msisdn|caller_name)[a-z_]*"'; then
        bad T-11 "the audit response contains a field whose name is on the privacy deny-list. No endpoint in this API may return audio or caller identity (rules.md R-14, R-15)."
      else
        ok T-11 "audit response exposes no deny-listed field name"
      fi
    elif [[ "$STATUS" == "404" || "$STATUS" == "501" ]]; then
      skip T-11 "audit endpoint returns ${STATUS} — contract defined in Phase 1, implemented in Phase 4 (openapi.yaml)"
    else
      bad T-11 "GET audit returned HTTP ${STATUS}"
    fi
  else
    skip T-10 "no session_id from T-08"
    skip T-11 "no session_id from T-08"
  fi
fi

# ---------------------------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------------------------
echo ""
if [[ "$JSON_OUT" -eq 1 ]]; then
  printf '{\n  "ok": %s,\n  "target": "%s",\n  "passed": %d,\n  "failed": %d,\n  "warnings": %d,\n  "skipped": %d,\n  "results": [\n' \
    "$([[ "$FAIL" -eq 0 ]] && echo true || echo false)" "$BASE_URL" "$PASS" "$FAIL" "$WARN" "$SKIP"
  first=1
  for row in "${RESULTS[@]}"; do
    [[ "$first" -eq 1 ]] || printf ',\n'; first=0
    printf '    {"status": "%s", "check": "%s", "detail": "%s"}' \
      "${row%%|*}" "$(printf '%s' "$row" | cut -d'|' -f2)" \
      "$(printf '%s' "$row" | cut -d'|' -f3- | sed 's/"/\\"/g')"
  done
  printf '\n  ]\n}\n'
fi

echo "contract_test: ${PASS} passed, ${FAIL} failed, ${WARN} warning(s), ${SKIP} skipped — target ${BASE_URL}"
cat <<'EOF'

  NOT COVERED by this suite (rules.md R-52 — bounded coverage is stated, not implied):
    * the WebSocket contract: handshake subprotocol, one-and-only-one session.open, the 648-byte
      frame, uint64 big-endian sequence, and every WsErrorCode close path. Needs a real stream —
      see the `contract` and `integration` pytest markers.
    * the policy engine: k-of-n (3-of-5) evidence, sticky `high`, ineligible-window skipping.
    * frame/window constant parity between Python and TypeScript — that is contract-check.yml.
    * whether audio is retained at runtime — that is the `privacy` marker suite plus privacy-check.
    * ticket single-use enforcement: proving it needs two WebSocket upgrades.
EOF

[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
