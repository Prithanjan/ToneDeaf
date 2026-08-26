# Gateway image — CPU and GPU tiers build from THIS SAME FILE.
#
# The Gateway has no GPU dependency; only the Scorer does. So there is exactly one Gateway image and
# it is genuinely identical across tiers — unlike the Scorer, whose images cannot be byte-identical
# because one links onnxruntime and the other onnxruntime-gpu (rules.md R-06).
#
# BUILD CONTEXT IS THE REPO ROOT, not gateway/:
#     docker build -f gateway/Dockerfile -t sih26104/gateway:$GIT_SHA .
# The Gateway hashes contracts/openapi.yaml and contracts/voice_scorer.proto into the parity set it
# prints at startup and serves from /api/v1/version, so those files must be inside the context. A
# gateway/-scoped context cannot reach them, and a build-time copy into gateway/ would create a second
# copy of a two-key-reviewed contract file — exactly the drift CONTRACT_CHANGE_POLICY.md forbids.
#
# Python 3.12, not 3.14 (decision D-10): webrtcvad-wheels and onnxruntime wheels for 3.14 are not
# assured, and pinning here is what stops a local 3.14 interpreter from producing a build that works
# on a laptop and fails in ECS.

FROM python:3.12-slim AS base

# PYTHONDONTWRITEBYTECODE: a read-only-friendly filesystem and no stale .pyc after a layer swap.
# PYTHONUNBUFFERED: log lines reach CloudWatch in order, which matters when the only record of a
# failed demo is the log.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# webrtcvad-wheels ships wheels, but build-essential is retained in the builder stage so a
# source-only transitive dependency cannot break the build silently at pin-bump time. It stays out of
# the runtime stage: a compiler in a serving image is attack surface with no purpose.
FROM base AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY gateway/requirements.txt ./requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM base AS runtime

# Non-root. No SSH, no shell access to the host, no ECS Exec in the demo (rules.md R-36).
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 gateway

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY --chown=gateway:gateway gateway/app ./app

# Read at startup only, to hash for the parity set (main.py::_hash_contract). Read-only to the
# process; the copy in contracts/ remains the single source of truth.
COPY --chown=gateway:gateway contracts/openapi.yaml ./contracts/openapi.yaml
COPY --chown=gateway:gateway contracts/voice_scorer.proto ./contracts/voice_scorer.proto

# The policy bundle and calibration artifact ARE baked in, at an absolute `/policy` rather than under
# WORKDIR, so that POLICY_BUNDLE_PATH and CALIBRATION_PATH hold the *same value on both tiers* and the
# Compose bind mount shadows this copy at exactly the same path (rules.md R-04 — the tier is a value,
# never a branch).
#
# This reverses an earlier decision that said "mounted (Compose) or pulled from the task definition
# path (ECS)". The ECS half of that sentence described a mechanism that does not exist: a task
# definition does not deliver files, and Fargate has no host filesystem to bind-mount from, so the
# Gateway would have started on AWS with POLICY_BUNDLE_PATH pointing at nothing. The alternatives were
# an EFS filesystem with mount targets and a security group, or an S3 fetch in an entrypoint shim —
# both disproportionate for two small text files, and both new failure modes on the startup path.
#
# What baking costs, stated plainly: changing a threshold on AWS now needs an image rebuild and a
# redeploy, not a file swap and a restart. For a demo whose policy is meant to be locked that is
# acceptable, and it is arguably the better default — a policy change becomes a versioned artifact
# rather than a live edit. Local iteration is unaffected, because the mount still wins.
#
# The release manifest's `policy_sha256` is unharmed and slightly strengthened: the Gateway hashes the
# bundle at startup into the parity set, and baking makes that hash immutable for a given image digest
# instead of dependent on whatever happened to be mounted.
COPY --chown=gateway:gateway policy/ /policy/

# The audit package, including `migrations/`. The `gateway-migrate` task definition is this same image
# with a different command (`alembic -c /app/audit/migrations/alembic.ini upgrade head`), so the
# migrations must be present here or that task cannot run — and on AWS there is no bind mount to cover
# for their absence, unlike under Compose (aws-setup-instructions.md §8).
COPY --chown=gateway:gateway audit/ ./audit/

USER gateway

EXPOSE 8080

# Liveness only. /readyz additionally checks the database and the Scorer's execution provider, and is
# what a deploy gate should wait on — using it here would make a Scorer outage kill a healthy Gateway.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"]

# One worker. Session state, the ticket replay cache, and the per-session audit chain head are all
# in-process (session_registry.py, security/ticket.py, audit/writer.py). A second worker would fork
# the hash chain, and a verifier reports a forked chain as tampering. Concurrency is asyncio, not
# processes. Phase 4 moves that state to a shared store before desired_count goes above 1.
#
# --ws-max-size is a memory-exhaustion backstop, deliberately set well ABOVE the 648-byte frame size
# rather than just above it. A cap at ~1 KiB would make uvicorn close oversized frames itself with
# code 1009 before app code ran, and the Phase 1 negative-contract suite asserts OUR close code and
# reason for an oversized frame (design.md section 9). The application rejects anything that is not
# exactly 648 bytes; this value only stops a client from making the server allocate megabytes first.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", \
     "--no-server-header", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--ws-max-size", "65536"]
