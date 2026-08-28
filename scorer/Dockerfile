# Scorer image — ONE Dockerfile, TWO ECR repositories.
#
#     docker build -f scorer/Dockerfile -t sih26104/scorer-cpu:$GIT_SHA .
#     docker build -f scorer/Dockerfile -t sih26104/scorer-gpu:$GIT_SHA \
#         --build-arg ORT_PACKAGE=onnxruntime-gpu \
#         --build-arg CUDA_PIP_PACKAGES="$SCORER_CUDA_PIP_PACKAGES" .
#
# BUILD CONTEXT IS THE REPO ROOT, not scorer/. Consistent with gateway/Dockerfile, and for the same
# reason: the Scorer hashes contracts/voice_scorer.proto into the parity set it prints at startup, and a
# scorer/-scoped context cannot reach contracts/. Copying the proto into scorer/ at build time would
# create a second copy of a two-key-reviewed contract file, which is exactly the drift
# contracts/CONTRACT_CHANGE_POLICY.md forbids. It also keeps ONE build-context convention across both
# services, so a CI job that gets it wrong for one gets it wrong for both and is caught immediately
# instead of intermittently.
#
# WHY ONE FILE WITH A BUILD ARG, NOT TWO FILES
# Two Dockerfiles would drift. Not hypothetically: the CPU tier is the tier that gets exercised every
# day, so a change lands there — a new environment variable, a healthcheck fix, a base-image bump — and
# the GPU file, which is built maybe three times in the whole project, silently keeps the old shape.
# The one thing the demo depends on is that the two tiers behave identically (architecture.md §5.1,
# rules.md R-06), so the build definition is the last place to accept two sources of truth. With one
# file and one argument, the diff between the images is exactly the argument, and it is legible in the
# build command rather than distributed across two files nobody diffs.
#
# THE TWO IMAGES CANNOT BE BYTE-IDENTICAL — DOCUMENTED PARITY EXCEPTION (rules.md R-06)
# One links onnxruntime-gpu against the CUDA runtime; the other links onnxruntime against nothing. No
# build-arg trick makes those the same layer set, and pretending otherwise would be a false claim. So
# the parity set (architecture.md §5.1) is defined over things that CAN be identical and are the things
# that actually determine behaviour:
#
#     application source     identical  (the same scorer/app tree is COPYd into both)
#     protobuf contract      identical  (sha256 of contracts/voice_scorer.proto, printed at startup)
#     model artifact         identical  (sha256 of aasist.onnx, mounted, printed at startup)
#     calibration artifact   identical  (sha256 of calibration.json, mounted, printed at startup)
#     python version         identical  (python:3.12-slim for BOTH; see below)
#     shared runtime pins    identical  (scorer/requirements.txt, byte-for-byte)
#     ---
#     image digest           NOT in the parity set — this exception, and only this one
#     ORT wheel              onnxruntime-gpu vs onnxruntime — the ONE permitted dependency difference
#
# Both images use python:3.12-slim rather than a CUDA base image for the GPU tier. That keeps the
# interpreter build, the libc, and every shared wheel identical between tiers, which shrinks the
# permitted difference to the ORT wheel plus the NVIDIA runtime libraries it needs. A CUDA base image
# would have made the two images differ in the base layer, the system Python, and the apt set as well —
# a much larger diff to defend, for the same functionality.

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS base

# PYTHONDONTWRITEBYTECODE: a read-only-friendly filesystem and no stale .pyc after a layer swap.
# PYTHONUNBUFFERED: log lines reach CloudWatch in order, which matters when the only record of a failed
# demo is the log — and the startup banner is emitted through the logger (app/banner.py).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---------------------------------------------------------------------------------------------------
FROM base AS builder

# THE tier switch. Exactly one wheel, one version, named in one place.
ARG ORT_VERSION=1.20.1
ARG ORT_PACKAGE=onnxruntime

# NVIDIA CUDA/cuDNN runtime libraries, as pip wheels, for the GPU build only. Empty on the CPU build,
# so the CPU image carries no CUDA at all.
#
# UNVERIFIED ON THIS WORKSTATION. There is no GPU and no CUDA toolkit here, so the exact wheel set that
# satisfies onnxruntime-gpu 1.20.1's CUDA 12.x / cuDNN 9.x requirement has not been executed — it is
# validated at the Phase 4 GPU deploy. That is acceptable precisely because getting it wrong is LOUD:
# app/model.py passes a single-entry provider list and then asserts get_providers()[0], so a missing
# library produces ProviderUnavailable before the port is bound. There is no path from a wrong value
# here to a task that comes up healthy on the CPU — which is the failure this whole design exists to
# make impossible (rules.md R-45, and the docstring in app/model.py on why silent fallback is worse
# than a crash).
ARG CUDA_PIP_PACKAGES=""

# build-essential is retained in the BUILDER so a source-only transitive dependency at pin-bump time
# fails visibly instead of silently. It never reaches the runtime stage: a compiler in a serving image
# is attack surface with no purpose.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY scorer/requirements.txt ./requirements.txt

# --require-hashes is deliberately NOT used. requirements.txt carries exact version pins, which is what
# the parity set needs; a hash file for the transitive closure of two ORT wheels on two platforms is a
# maintenance surface this project cannot keep honest in five days, and a stale hash file that has been
# `--no-deps`-worked-around is worse than exact pins.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/venv/bin/pip install --no-cache-dir "${ORT_PACKAGE}==${ORT_VERSION}" ${CUDA_PIP_PACKAGES}

# ---------------------------------------------------------------------------------------------------
FROM base AS runtime

# Non-root. No SSH, no shell access to the host, no ECS Exec in the demo (rules.md R-36).
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 scorer

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# NVIDIA pip wheels install their shared objects under site-packages/nvidia/*/lib, which is not on the
# default loader path. Listed unconditionally: on the CPU image these directories simply do not exist,
# and a nonexistent LD_LIBRARY_PATH entry is ignored by the loader — so the ENV line stays identical
# between the two images instead of becoming a second place the tiers diverge.
ENV LD_LIBRARY_PATH="/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cufft/lib:/opt/venv/lib/python3.12/site-packages/nvidia/curand/lib"

COPY --chown=scorer:scorer scorer/app ./app

# Read at startup only, to hash for the parity set (server.py::_hash_proto). The copy in contracts/
# remains the single source of truth; this one is read-only to the process.
COPY --chown=scorer:scorer contracts/voice_scorer.proto ./contracts/voice_scorer.proto

# The contract test vector IS baked in, unlike the model and the calibration. It is a 160 KiB
# deterministic fixture generated from source (ml/fixtures/make_contract_vector.py) and version
# controlled — part of the byte contract, not a tunable artifact. Baking it means the every-startup
# parity check in server.py cannot be skipped by forgetting a mount (frame_contract.md §6).
COPY --chown=scorer:scorer ml/fixtures/contract_vector_v1.npy /fixtures/contract_vector_v1.npy

# The policy bundle, calibration artifact, and exported ONNX model are baked in for ECS operation
COPY --chown=scorer:scorer policy/ /policy/
COPY --chown=scorer:scorer ml/models/ /models/

USER scorer

EXPOSE 50051

# A REAL gRPC HEALTH CALL, not a port probe.
#
# `nc -z 50051` would pass on a process that had bound the port and then failed to load the model, or
# that was raising on every ScoreWindow, or — the one that matters — that had come up on the CPU when
# CUDA was requested. "The port is open" is not "the service works", and on the GPU tier the gap
# between those two statements is a day of latency measurements that describe the wrong machine.
# `python -m app.server --healthcheck` opens a channel to the loopback, issues the actual Health RPC
# through the generated stub, and exits non-zero unless the response says ready. It deliberately does
# not re-read the full configuration, so it can never report "unhealthy" for a reason unrelated to
# health (see server.py::healthcheck).
#
# start-period allows for the four-step fail-fast startup sequence: hash the ONNX file, create the ORT
# session, assert the execution provider, then score the contract test vector. CUDA session creation
# plus kernel warmup dominates and is the reason this is 30s rather than the Gateway's 20s.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "app.server", "--healthcheck"]

# One process, threads inside it (app/server.py: a bounded ThreadPoolExecutor plus
# maximum_concurrent_rpcs). Not multiple workers: each would hold its own ORT session and its own CUDA
# context, so N workers would multiply GPU memory by N and the intra-op thread count set from the
# measured CPU sweep would then describe none of them.
CMD ["python", "-m", "app.server"]
