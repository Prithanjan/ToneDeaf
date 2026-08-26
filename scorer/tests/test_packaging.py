"""The build definition and the dependency pins, asserted as text.

These are the properties nothing else in the suite can check, because they are not code: which wheel
each image gets, whether a compiler survives into the serving layer, whether the healthcheck exercises
the RPC or just the socket. Every one of them fails silently. A CPU wheel in the GPU image produces a
task that comes up healthy and reports ``CPUExecutionProvider``; a port-probe healthcheck produces a
task that comes up healthy and cannot score. The whole reason these are tests and not review comments is
that a review happens once and a test happens on every push.

Text assertions have a known weakness: they check that the Dockerfile SAYS the right thing, not that a
built image DOES it. That gap is closed by the CI build (Phase 1 exit criteria, technical-design.md §9)
and by the runtime assertions in ``app/model.py``. What these tests catch is the edit that changes the
intent — a pin bump applied to one variant, a ``USER`` line dropped during a debugging session and never
restored — which is the failure mode that actually happens.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, SCORER_ROOT

DOCKERFILE = SCORER_ROOT / "Dockerfile"
REQUIREMENTS = SCORER_ROOT / "requirements.txt"
REQUIREMENTS_DEV = SCORER_ROOT / "requirements-dev.txt"
PYPROJECT = SCORER_ROOT / "pyproject.toml"
GEN_PROTO = REPO_ROOT / "scripts" / "gen_proto.sh"

pytestmark = pytest.mark.parity


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _requirement_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _text(path).splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped or stripped.startswith("-"):
            continue
        name, _, version = stripped.partition("==")
        pins[name.strip().lower()] = version.strip()
    return pins


def _runtime_stage(dockerfile_text: str) -> str:
    """Everything from ``FROM base AS runtime`` onward — the layers that actually ship."""
    marker = "FROM base AS runtime"
    assert marker in dockerfile_text, "the runtime stage is no longer named 'runtime'"
    return dockerfile_text.split(marker, 1)[1]


def _instructions(dockerfile_text: str) -> list[str]:
    """Dockerfile lines with comments and blanks removed.

    Every scan for a forbidden token has to run over this rather than the raw text. The file documents
    the anti-patterns it avoids — a ``nc -z`` port probe, ``build-essential`` in the runtime stage — and
    a substring scan over the comments would fire on the explanation instead of on the mistake, which
    trains whoever hits it to delete the explanation.
    """
    kept: list[str] = []
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kept.append(stripped)
    return kept


class TestExactPins:
    """rules.md R-06. A version range makes the parity set unverifiable."""

    @pytest.mark.parametrize("path", [REQUIREMENTS, REQUIREMENTS_DEV])
    def test_every_dependency_is_pinned_with_double_equals(self, path: Path) -> None:
        """Prevents two tiers built a day apart claiming the same release.

        ``>=`` resolves differently over time, so "the same commit" would stop meaning "the same
        dependency closure" — and the parity claim is a claim about behaviour, which the closure decides.
        """
        for line in _text(path).splitlines():
            stripped = line.split("#", 1)[0].strip()
            if not stripped or stripped.startswith("-r "):
                continue
            assert "==" in stripped, f"{path.name}: unpinned requirement {stripped!r}"
            for loose in (">=", "<=", "~=", ">", "<", "*"):
                assert loose not in stripped, f"{path.name}: loose pin {stripped!r}"

    def test_runtime_dependencies_are_exactly_the_three_documented_ones(self) -> None:
        """Prevents the serving image growing a dependency that is not in the parity story.

        Every addition here widens the diff between the two Scorer images and the attack surface of the
        one container that touches audio. Three packages is the whole runtime: gRPC transport, protobuf
        codec, array math.
        """
        assert set(_requirement_pins(REQUIREMENTS)) == {"grpcio", "protobuf", "numpy"}

    def test_shared_pins_match_the_gateway_exactly(self) -> None:
        """Prevents a protobuf-runtime skew that only shows up as a deserialization error at demo time.

        The generated stubs are version-coupled to the runtime, and the two services exchange messages
        built by the same generator. A Gateway on protobuf 5.29.2 and a Scorer on something else is a
        pair that imports cleanly on both sides and fails on the wire.
        """
        scorer = _requirement_pins(REQUIREMENTS)
        gateway = _requirement_pins(REPO_ROOT / "gateway" / "requirements.txt")
        shared = set(scorer) & set(gateway)
        assert {"grpcio", "protobuf", "numpy"} <= shared
        for name in sorted(shared):
            assert scorer[name] == gateway[name], (
                f"{name} is pinned to {scorer[name]} in the Scorer and {gateway[name]} in the Gateway"
            )

    def test_onnxruntime_is_absent_from_the_runtime_requirements(self) -> None:
        """The tier switch lives in ONE place: the Dockerfile's ``ORT_PACKAGE`` argument.

        Naming either wheel here would make this file describe one of the two images while appearing to
        describe both — and a reader diffing the two builds would find the difference in the wrong place.
        """
        pins = _requirement_pins(REQUIREMENTS)
        assert "onnxruntime" not in pins
        assert "onnxruntime-gpu" not in pins

    def test_the_cpu_wheel_is_a_dev_dependency_and_the_gpu_wheel_is_not(self) -> None:
        """rules.md R-32. The single GPU in the budget is not spent on a test runner.

        The CPU wheel must be installable for tests, because the provider assertion in ``app/model.py``
        is only meaningfully exercised against a real ``InferenceSession``.
        """
        dev = _requirement_pins(REQUIREMENTS_DEV)
        assert dev["onnxruntime"] == "1.20.1"
        assert "onnxruntime-gpu" not in dev

    def test_grpcio_tools_matches_grpcio(self) -> None:
        """Prevents stubs that import on the generating machine and abort in the container.

        grpcio's generated service code carries a ``_version_not_supported`` guard that raises at import
        when the runtime is older than the generator, so a newer grpcio-tools is not a harmless upgrade.
        """
        assert (
            _requirement_pins(REQUIREMENTS_DEV)["grpcio-tools"]
            == _requirement_pins(REQUIREMENTS)["grpcio"]
        )

    def test_gen_proto_pins_the_same_grpcio_tools_version(self) -> None:
        """The generator script's own guard must agree with the file it is guarding."""
        expected = _requirement_pins(REQUIREMENTS_DEV)["grpcio-tools"]
        assert f'EXPECTED_GRPCIO_TOOLS="{expected}"' in _text(GEN_PROTO)


class TestDockerfileTierSwitch:
    """One file, one argument. The permitted difference between the images is that argument."""

    def test_ort_version_is_named_in_exactly_one_place(self) -> None:
        """Prevents a bump landing on one variant and not the other.

        The GPU image is built perhaps three times in the whole project; the CPU image is built daily. A
        version written twice is a version that ends up different, and the pair would then be
        version-skewed while both claiming the same release. So the install command interpolates the arg
        rather than naming a version, and the arg is declared once.
        """
        text = _text(DOCKERFILE)
        assert len(re.findall(r"^ARG ORT_VERSION=", text, re.MULTILINE)) == 1
        assert '"${ORT_PACKAGE}==${ORT_VERSION}"' in text
        installs = [
            line for line in text.splitlines() if "pip install" in line and "onnxruntime" in line
        ]
        for line in installs:
            assert "${ORT_VERSION}" in line, (
                f"hard-coded ORT version in an install step: {line.strip()}"
            )

    def test_both_variants_resolve_to_the_same_version(self) -> None:
        """The one dependency difference is the PACKAGE NAME, never the version.

        ``onnxruntime-gpu`` and ``onnxruntime`` at the same version share the same graph optimizations
        and the same operator implementations for the CPU fallback path, which is what makes the CPU tier
        a parity tier rather than a different model.
        """
        text = _text(DOCKERFILE)
        version = re.search(r"^ARG ORT_VERSION=(\S+)", text, re.MULTILINE)
        assert version is not None
        assert version.group(1) == _requirement_pins(REQUIREMENTS_DEV)["onnxruntime"], (
            "the Dockerfile's ORT_VERSION and the CPU wheel used by CI have drifted; the provider "
            "assertion would then be tested against a different runtime than the one that ships"
        )

    def test_the_package_argument_defaults_to_cpu(self) -> None:
        """Prevents an unparameterised build accidentally producing a GPU image.

        A plain ``docker build`` is a local build, and a local build must never pull a CUDA wheel it
        cannot load — that failure surfaces at container start, far from the command that caused it.
        """
        assert re.search(r"^ARG ORT_PACKAGE=onnxruntime$", _text(DOCKERFILE), re.MULTILINE)

    def test_cuda_packages_default_to_empty(self) -> None:
        """The CPU image must carry no CUDA at all — otherwise "no GPU here" is not verifiable."""
        assert re.search(r'^ARG CUDA_PIP_PACKAGES=""$', _text(DOCKERFILE), re.MULTILINE)

    def test_both_tiers_use_the_same_base_image(self) -> None:
        """Shrinks the permitted diff to the ORT wheel plus its CUDA libraries.

        A CUDA base image for the GPU tier would have made the two images differ in the base layer, the
        system Python, and the apt set as well — a much larger difference to defend for no extra
        functionality, since the CUDA runtime is available as pip wheels.
        """
        text = _text(DOCKERFILE)
        assert re.search(r"^ARG BASE_IMAGE=python:3\.12-slim$", text, re.MULTILINE)
        assert len(re.findall(r"^FROM \S+ AS ", text, re.MULTILINE)) >= 2
        for from_line in re.findall(r"^FROM (\S+)", text, re.MULTILINE):
            assert from_line in ("${BASE_IMAGE}", "base"), (
                f"unexpected base {from_line!r}: every stage must derive from the single BASE_IMAGE so "
                "the two tiers share an interpreter and a libc"
            )

    def test_python_version_matches_the_pinned_interpreter(self) -> None:
        """Prevents the image and the tooling config disagreeing about the language version.

        ``pyproject.toml`` targets py312 and mypy checks against 3.12; PEP 695 generic syntax in
        ``app/config.py`` requires it. A 3.11 base image would fail at import, and a 3.13 one would
        silently change NumPy and ORT wheel selection.
        """
        assert 'target-version = "py312"' in _text(PYPROJECT)
        assert 'python_version = "3.12"' in _text(PYPROJECT)
        assert "python:3.12-slim" in _text(DOCKERFILE)
        assert "python3.12/site-packages" in _text(DOCKERFILE)

    def test_the_parity_exception_is_documented_in_the_file(self) -> None:
        """rules.md R-06. The exception must be stated where the divergence is created.

        The two images cannot be byte-identical, and claiming otherwise would be a false claim in the one
        document a deployer reads. So the Dockerfile names the exception and enumerates what IS in the
        parity set instead.
        """
        text = _text(DOCKERFILE)
        assert "CANNOT BE BYTE-IDENTICAL" in text
        assert "R-06" in text
        assert "image digest" in text
        for member in ("application source", "protobuf contract", "model artifact", "calibration"):
            assert member in text, f"the parity set table no longer lists {member!r}"


class TestRuntimeLayerIsMinimal:
    """What ships. Everything in this class is attack surface the service cannot use."""

    def test_no_compiler_in_the_runtime_stage(self) -> None:
        """Prevents a toolchain in a container that terminates audio.

        ``build-essential`` is installed in the builder so a source-only transitive dependency fails
        visibly at pin-bump time. It must not survive: a compiler in a serving image turns a code-exec
        foothold into arbitrary native tooling.
        """
        runtime = "\n".join(_instructions(_runtime_stage(_text(DOCKERFILE))))
        for tool in ("build-essential", "gcc", "g++", "cmake", "apt-get install"):
            assert tool not in runtime, f"{tool} reaches the runtime stage"

    def test_no_dev_dependencies_in_the_image(self) -> None:
        """A test runner and a linter in a serving container are packages an attacker can reach."""
        instructions = "\n".join(_instructions(_text(DOCKERFILE)))
        assert "requirements-dev" not in instructions
        for package in ("pytest", "ruff", "mypy", "grpcio-tools"):
            assert f"{package}==" not in instructions

    def test_runs_as_a_non_root_uid(self) -> None:
        """rules.md R-36. Prevents a container escape starting from uid 0.

        A fixed high uid, not a name lookup, so a bind-mounted artifact directory has predictable
        ownership across both tiers.
        """
        text = _text(DOCKERFILE)
        assert "--uid 10001" in text
        assert re.search(r"^USER scorer$", text, re.MULTILINE)
        user_index = text.index("\nUSER scorer")
        assert user_index < text.index("\nCMD "), "USER must precede CMD"
        assert "nologin" in text, "the service account must not have a login shell"

    def test_the_model_and_calibration_are_not_baked_in(self) -> None:
        """Prevents ``model_sha256`` in the release manifest describing a layer nobody can point at.

        Both artifacts are mounted, so a recalibration is a file change rather than an image rebuild, and
        the hash in the manifest names a file a reviewer can independently hash. Asserted over COPY
        sources rather than over the whole text, because both artifacts are legitimately NAMED in the
        comment that explains why they are absent.
        """
        sources = re.findall(r"^COPY (?:--\S+ )*(\S+)", _text(DOCKERFILE), re.MULTILINE)
        for source in sources:
            assert not source.endswith(".onnx"), f"the model is baked into the image: {source}"
            assert "calibration" not in source, f"calibration is baked into the image: {source}"

    def test_the_contract_vector_is_baked_in(self) -> None:
        """frame_contract.md §6. The every-startup parity check must not be skippable by a missing mount.

        Unlike the model, this is a 160 KiB deterministic fixture generated from committed source — part
        of the byte contract, not a tunable artifact.
        """
        assert "ml/fixtures/contract_vector_v1.npy" in _text(DOCKERFILE)

    def test_the_proto_is_copied_from_the_reviewed_location(self) -> None:
        """Prevents a second copy of a two-key-reviewed contract file existing in the repo.

        The Scorer hashes the proto into the parity set it prints at startup, so it needs the file — but
        it reads the one in ``contracts/``, which is why the build context is the repo root.
        """
        assert "COPY --chown=scorer:scorer contracts/voice_scorer.proto" in _text(DOCKERFILE)

    def test_build_context_is_the_repo_root_and_says_so(self) -> None:
        """Prevents a CI job using a ``scorer/``-scoped context, which cannot reach ``contracts/``.

        Every COPY source is repo-root-relative, and the header states the expectation with both build
        commands, because ``docker build`` gives a useless error for this mistake.
        """
        text = _text(DOCKERFILE)
        assert "BUILD CONTEXT IS THE REPO ROOT" in text
        assert "docker build -f scorer/Dockerfile" in text
        for source in re.findall(r"^COPY (?:--\S+ )*(\S+)", text, re.MULTILINE):
            if source.startswith(("/", "$")):
                continue
            assert source.startswith(("scorer/", "contracts/", "ml/")), (
                f"COPY source {source!r} is not repo-root-relative"
            )


class TestHealthcheckExercisesTheRpc:
    """An open port is not a working service — and on the GPU tier the gap is a day of wrong numbers."""

    def test_healthcheck_issues_the_health_rpc(self) -> None:
        """Prevents a task reporting healthy while it cannot score, or is on the wrong provider.

        A ``nc -z`` probe passes on a process that bound the port and then failed to load the model, and
        — the case that matters — on one that came up on the CPU when CUDA was requested. The real RPC
        returns the provider, so the same code path that a human reads in the banner is the one the
        orchestrator polls.
        """
        text = _text(DOCKERFILE)
        assert "HEALTHCHECK" in text
        assert '"--healthcheck"' in text
        instructions = "\n".join(_instructions(text))
        for probe in ("nc -z", "curl", "/dev/tcp", "netstat", "socket.connect"):
            assert probe not in instructions, (
                f"the healthcheck degenerated into a port probe ({probe})"
            )

    def test_healthcheck_uses_exec_form(self) -> None:
        """Prevents a shell wrapper swallowing the exit status of the actual check."""
        healthcheck = _text(DOCKERFILE).split("HEALTHCHECK", 1)[1]
        assert 'CMD ["python", "-m", "app.server", "--healthcheck"]' in healthcheck

    def test_the_healthcheck_entry_point_exists(self) -> None:
        """Prevents the Dockerfile referring to a flag ``server.py`` no longer implements.

        This one is checkable in-process, unlike the rest of this file: the flag is parsed by ``main``.
        """
        from app.server import main

        assert main(["--nonsense"]) == 64  # argument error, so the parser is reached and is strict

    def test_start_period_allows_the_full_startup_sequence(self) -> None:
        """Prevents ECS killing a GPU task during CUDA session creation and calling it a crash loop.

        Session creation plus kernel warmup dominates the four-step startup; a start period tuned to the
        CPU tier would make the GPU tier look unstable and send someone debugging the wrong thing.
        """
        text = _text(DOCKERFILE)
        match = re.search(r"--start-period=(\d+)s", text)
        assert match is not None
        assert int(match.group(1)) >= 30

    def test_one_process_not_multiple_workers(self) -> None:
        """Prevents N ORT sessions multiplying GPU memory by N and invalidating the thread sweep.

        Each worker would hold its own CUDA context, and the intra-op thread count measured on the CPU
        tier would then describe none of them.
        """
        text = _text(DOCKERFILE)
        assert 'CMD ["python", "-m", "app.server"]' in text
        instructions = "\n".join(_instructions(text))
        for multiproc in ("--workers", "gunicorn", "uvicorn", "supervisord"):
            assert multiproc not in instructions


@pytest.mark.privacy
class TestNoSecretsAnywhere:
    """rules.md R-34. Asserted as a property of the files, not trusted as a habit."""

    #: Fragments assembled at runtime so this list does not itself put the forbidden strings in the
    #: files it scans. A scanner does not know the difference between a test fixture and a credential,
    #: and neither does the person who copies one into a real config.
    _PATTERNS = (
        "AKI" + "A",
        "ASI" + "A",
        "-----BEGIN " + "PRIVATE KEY",
        "-----BEGIN " + "RSA PRIVATE KEY",
        "Bearer " + "ey" + "J",
        "aws_secret_access" + "_key",
        "xox" + "b-",
        "gh" + "p_",
        "sk-" + "ant-",
    )

    def _scannable_files(self) -> list[Path]:
        """Hand-written files only. Generated stubs are excluded: they carry a serialized descriptor
        that is by nature a long opaque literal, and they are not ours to change if it trips a scanner.
        """
        files = [DOCKERFILE, REQUIREMENTS, REQUIREMENTS_DEV, PYPROJECT]
        files += sorted(
            path
            for path in (SCORER_ROOT / "app").glob("*.py")
            if not path.name.startswith("voice_scorer_pb2")
        )
        files += sorted((SCORER_ROOT / "tests").glob("*.py"))
        return files

    def test_no_file_in_the_scorer_tree_contains_a_credential_shaped_string(self) -> None:
        """Prevents a plausible fixture becoming a real leak, or a scanner failure nobody triages.

        Includes the test directory deliberately. Tests are where an "obviously fake" key gets written,
        and an obviously fake key is indistinguishable from a real one to git history.
        """
        for path in self._scannable_files():
            text = _text(path)
            for pattern in self._PATTERNS:
                assert pattern not in text, f"{path.name} contains a credential-shaped string"

    def test_no_env_line_in_the_dockerfile_assigns_a_secret(self) -> None:
        """Prevents key material baked into a layer, where it survives every later ``docker rm``.

        The Scorer needs no secret at all: ``session_ref`` arrives already pseudonymized (rules.md
        R-16), and there is nothing here to sign.
        """
        for line in _text(DOCKERFILE).splitlines():
            if not line.startswith(("ENV ", "ARG ")):
                continue
            lowered = line.lower()
            for forbidden in ("secret", "password", "token", "credential", "_key="):
                assert forbidden not in lowered, f"secret-shaped build variable: {line.strip()}"

    def test_no_long_hex_or_base64_literal_masquerades_as_a_key(self) -> None:
        """Prevents a 40-character random-looking literal, which is what a scanner flags regardless.

        The suite's SHA-256 values are computed from descriptive byte strings or built from a repeated
        character (``"0" * 64``) precisely so a reader can tell at a glance that they are not secrets.

        The digit floor is what makes this usable. A length-only rule fires on long CamelCase test names
        (``TestScoreWindowRequestCarriesNoPolicyInput`` is 42 characters), and a rule that fires on
        readable identifiers gets deleted rather than fixed. Real keys, hex digests, and base64 blobs all
        carry digits densely; prose and identifiers do not.
        """
        candidate = re.compile(r"(?<![\w/.-])[A-Za-z0-9+/]{40,}={0,2}(?![\w/.-])")
        for path in self._scannable_files():
            for number, line in enumerate(_text(path).splitlines(), start=1):
                if "sha256=" in line or "http" in line:
                    continue
                for match in candidate.finditer(line):
                    run = match.group(0)
                    digits = sum(character.isdigit() for character in run)
                    assert digits < 5, (
                        f"{path.name}:{number} contains a {len(run)}-character opaque literal with "
                        f"{digits} digits; derive test hashes from a descriptive byte string instead"
                    )


class TestToolingConfigMirrorsTheGateway:
    """A reviewer moving between the two services should read one convention, not two."""

    def test_marker_set_is_identical(self) -> None:
        """Prevents ``-m privacy`` in CI silently covering one service and not the other.

        The privacy gate is a release blocker and it is selected by marker. A marker that exists in one
        pyproject and not the other means the gate runs on half the code and reports green.
        """
        scorer_markers = set(re.findall(r'^\s+"(\w+):', _text(PYPROJECT), re.MULTILINE))
        gateway_markers = set(
            re.findall(
                r'^\s+"(\w+):', _text(REPO_ROOT / "gateway" / "pyproject.toml"), re.MULTILINE
            )
        )
        assert {"contract", "privacy", "parity", "integration"} <= scorer_markers
        assert scorer_markers <= gateway_markers, (
            f"markers declared here but not in the Gateway: {sorted(scorer_markers - gateway_markers)}"
        )

    def test_strict_markers_and_strict_config_are_on(self) -> None:
        """Prevents a typo'd marker silently selecting nothing.

        ``-m privcy`` with lax settings runs zero tests and exits 0, which reads as a passing gate.
        """
        text = _text(PYPROJECT)
        assert "--strict-markers" in text
        assert "--strict-config" in text

    def test_warnings_are_errors(self) -> None:
        """The two places a silent numeric change invalidates a calibration without failing a shape test.

        A NumPy overflow warning in the Platt sigmoid, or a dtype-demotion warning in the PCM
        conversion, is exactly the signal that would otherwise scroll past between Day 1 and Day 5.
        """
        assert 'filterwarnings = ["error"]' in _text(PYPROJECT)

    def test_generated_stubs_are_excluded_from_lint_and_typing(self) -> None:
        """A lint fix applied to a generated file is reverted by the next generation, silently."""
        text = _text(PYPROJECT)
        assert "app/voice_scorer_pb2.py" in text
        assert "ignore_errors = true" in text

    def test_line_length_matches_the_gateway(self) -> None:
        pattern = re.compile(r"^line-length = (\d+)$", re.MULTILINE)
        scorer = pattern.search(_text(PYPROJECT))
        gateway = pattern.search(_text(REPO_ROOT / "gateway" / "pyproject.toml"))
        assert scorer is not None and gateway is not None
        assert scorer.group(1) == gateway.group(1)


class TestGeneratedStubsAreCommitted:
    """The Gateway cannot import anything that touches the Scorer until these exist."""

    def test_both_sides_have_the_same_generated_bytes(self) -> None:
        """Prevents the two services running different descriptor bootstraps from the same proto.

        ``scripts/gen_proto.sh`` emits both from one invocation for this reason. A separately generated
        pair passes every field-name test in ``test_detection_decision_seam.py`` on each side while
        disagreeing on the wire.
        """
        for name in ("voice_scorer_pb2.py", "voice_scorer_pb2_grpc.py"):
            scorer_stub = SCORER_ROOT / "app" / name
            gateway_stub = REPO_ROOT / "gateway" / "app" / "scorer" / name
            if not scorer_stub.is_file() or not gateway_stub.is_file():
                pytest.skip("stubs not generated; run scripts/gen_proto.sh")
            assert scorer_stub.read_bytes() == gateway_stub.read_bytes(), (
                f"{name} differs between the two services"
            )

    def test_the_grpc_stub_imports_the_pb2_module_relatively(self) -> None:
        """protoc emits a top-level absolute import that is wrong for both destination packages.

        Left unfixed, ``import voice_scorer_pb2`` resolves only when the containing directory happens to
        be on ``sys.path`` — which it is under pytest and is not under ``python -m app.server``. That
        failure appears at container start, not in CI. The fixup is a relative import so neither package
        needs to know its own dotted path (``app`` here, ``app.scorer`` in the Gateway), which is also
        what lets one generator invocation serve both sides.
        """
        stub = SCORER_ROOT / "app" / "voice_scorer_pb2_grpc.py"
        if not stub.is_file():
            pytest.skip("stubs not generated; run scripts/gen_proto.sh")
        text = _text(stub)
        assert "from . import voice_scorer_pb2 as voice__scorer__pb2" in text
        assert not re.search(r"^import voice_scorer_pb2", text, re.MULTILINE)

    def test_gen_proto_warns_that_the_gateway_cannot_import_without_it(self) -> None:
        """The header is load-bearing: a fresh clone fails at collection with ModuleNotFoundError."""
        text = _text(GEN_PROTO)
        assert "gateway/app/scorer/client.py ALREADY imports these stubs" in text
        assert "do not exist on disk" in text

    def test_gen_proto_only_reads_the_contract(self) -> None:
        """``contracts/voice_scorer.proto`` is under a two-key review rule (contracts/CONTRACT_CHANGE_POLICY.md).

        The generator must be safe to run on any checkout. The import fixup rewrites a GENERATED file;
        nothing in the script may write to the proto directory, or "regenerate the stubs" would become a
        way to change a reviewed contract.
        """
        text = _text(GEN_PROTO)
        assert '--proto_path="${PROTO_DIR}"' in text
        for line in text.splitlines():
            if "PROTO_DIR" not in line and "PROTO_FILE" not in line:
                continue
            for write in (">", ">>", "sed -i", "tee ", "rm "):
                assert write not in line, (
                    f"gen_proto writes to the contract directory: {line.strip()}"
                )

    def test_gen_proto_is_idempotent_by_construction(self) -> None:
        """CI asserts ``git diff --exit-code`` after a run, so a second run must be a no-op.

        The fixup regex is anchored to the un-fixed form, and the file is only rewritten when the
        substitution changed something — otherwise re-running would produce ``from . from . import``.
        """
        text = _text(GEN_PROTO)
        assert "^import voice_scorer_pb2 as voice__scorer__pb2$" in text
        assert "if fixed != source:" in text
        assert 'newline=""' in text, (
            "protoc emits LF; rewriting to CRLF on a Windows checkout would make the committed stubs "
            "differ by platform and turn every parity diff into noise"
        )
