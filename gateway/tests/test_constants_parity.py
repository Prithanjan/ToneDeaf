"""Cross-language constants parity — a PARITY-set test, so a failure is a deploy blocker.

``gateway/app/constants.py`` and ``pwa/src/lib/constants.ts`` define the same wire contract twice
because the browser cannot import Python. This test parses both as TEXT and asserts every shared name
has an equal value.

Parsing rather than importing is the point: importing the TypeScript would need a Node toolchain in
the Python CI job, and a test that is expensive to run is a test that gets skipped. Text parsing keeps
this in the same fast unit job as everything else.

The failure it prevents: someone tunes the frame size on one side, the server rejects every frame, the
client reports a close code, and the obvious diagnosis — "the WebSocket is broken" — is wrong. This
turns a confusing hour of debugging into a red CI line naming the constant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import constants as py_constants

TS_PATH = Path(__file__).resolve().parents[2] / "pwa" / "src" / "lib" / "constants.ts"

#: Names that must exist on BOTH sides with equal values. Listed explicitly rather than discovered by
#: intersection: an intersection shrinks silently when someone renames or deletes one side, and the
#: test would then pass by covering less.
REQUIRED_SHARED = (
    "CONTRACT_ID",
    "SAMPLE_RATE_HZ",
    "CHANNELS",
    "FRAME_MS",
    "SAMPLES_PER_FRAME",
    "BYTES_PER_FRAME_PAYLOAD",
    "SEQ_PREFIX_BYTES",
    "WS_FRAME_BYTES",
    "WINDOW_MS",
    "WINDOW_SAMPLES",
    "WINDOW_BYTES",
    "HOP_MS",
    "HOP_SAMPLES",
    "FRAMES_PER_HOP",
    "HOPS_PER_WINDOW",
    "PCM16_FLOAT_DIVISOR",
    "MAX_TEXT_FRAME_BYTES",
    "TICKET_TTL_SECONDS",
    "WS_SUBPROTOCOL",
    "WS_TICKET_SUBPROTOCOL_PREFIX",
)

_TS_EXPORT = re.compile(
    r"^export const (?P<name>[A-Z][A-Z0-9_]*)\s*(?::\s*[^=]+)?=\s*(?P<value>[^;]+);",
    re.MULTILINE,
)


def _coerce(raw: str) -> object:
    """Turn a TS literal into a comparable Python value.

    Handles the three forms actually used: numeric separators (``16_000``), single-quoted strings, and
    booleans. Anything else raises rather than being guessed at — a silently unparsed value would
    compare unequal and the failure would look like a parity break instead of a parser gap.
    """
    value = raw.strip().rstrip(",")
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return value[1:-1]
    cleaned = value.replace("_", "")
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if re.fullmatch(r"-?\d*\.\d+", cleaned):
        return float(cleaned)
    raise ValueError(f"unparsable TS literal: {raw!r}")


@pytest.fixture(scope="module")
def ts_values() -> dict[str, object]:
    assert TS_PATH.is_file(), f"the client half of the mirror is missing: {TS_PATH}"
    source = TS_PATH.read_text(encoding="utf-8")
    return {m.group("name"): _coerce(m.group("value")) for m in _TS_EXPORT.finditer(source)}


@pytest.mark.parity
class TestParity:
    def test_parser_found_the_exports(self, ts_values: dict[str, object]) -> None:
        """Guards against the worst outcome for this test: a regex that matches nothing, so parity
        holds trivially and the whole file is decoration."""
        assert len(ts_values) >= len(REQUIRED_SHARED)

    @pytest.mark.parametrize("name", REQUIRED_SHARED)
    def test_shared_constant_matches(self, name: str, ts_values: dict[str, object]) -> None:
        assert hasattr(py_constants, name), f"{name} is missing from gateway/app/constants.py"
        assert name in ts_values, f"{name} is missing from pwa/src/lib/constants.ts"
        assert (
            ts_values[name] == expected
        ), f"{name} diverged: python={expected!r} typescript={ts_values[name]!r}"

    def test_the_four_wire_sizes_are_exact(self, ts_values: dict[str, object]) -> None:
        """Spelled out as literals in exactly one place — here — so the test would catch a coordinated
        change on both sides that silently broke the documented contract."""
        assert ts_values["WS_FRAME_BYTES"] == py_constants.WS_FRAME_BYTES == 648
        assert ts_values["BYTES_PER_FRAME_PAYLOAD"] == py_constants.BYTES_PER_FRAME_PAYLOAD == 640
        assert ts_values["WINDOW_BYTES"] == py_constants.WINDOW_BYTES == 81_920
        assert ts_values["WINDOW_SAMPLES"] == py_constants.WINDOW_SAMPLES == 40_960

    def test_pcm_divisor_is_32768_not_32767(self, ts_values: dict[str, object]) -> None:
        """The specific off-by-one that would invalidate calibration without changing any test that
        checks shapes rather than values."""
        assert py_constants.PCM16_FLOAT_DIVISOR == 32_768.0
        assert ts_values["PCM16_FLOAT_DIVISOR"] == 32_768.0

    def test_byte_order_disagreement_is_recorded_on_both_sides(
        self, ts_values: dict[str, object]
    ) -> None:
        """Decision D-2. The header is big-endian and the payload little-endian, and the client must
        say so explicitly rather than relying on a DataView default nobody remembers."""
        assert py_constants.SEQ_STRUCT == ">Q"
        assert ts_values.get("SEQ_IS_BIG_ENDIAN") is True
        assert ts_values.get("PCM_IS_LITTLE_ENDIAN") is True

    def test_contract_id_matches_the_frame_contract_document(self) -> None:
        contract = TS_PATH.resolve().parents[3] / "contracts" / "frame_contract.md"
        assert contract.is_file()
        assert py_constants.CONTRACT_ID in contract.read_text(encoding="utf-8")
