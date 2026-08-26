"""Frame contract tests.

The negative cases matter more than the positive one. A parser that accepts a 647-byte frame by
zero-padding it produces plausible audio from a broken client, and the resulting spoof_risk is
evidence about a stream nobody transmitted (rules.md R-24).
"""

from __future__ import annotations

import struct

import pytest

from app.constants import (
    BYTES_PER_FRAME_PAYLOAD,
    SAMPLES_PER_FRAME,
    SEQ_PREFIX_BYTES,
    WS_FRAME_BYTES,
)
from app.ws.frames import (
    Frame,
    FrameError,
    FrameRejected,
    check_sequence,
    frame_samples,
    parse_frame,
)
from tests.conftest import make_frame


class TestParse:
    def test_round_trips_sequence_and_payload(self) -> None:
        payload = struct.pack(f"<{SAMPLES_PER_FRAME}h", *range(SAMPLES_PER_FRAME))
        frame = parse_frame(make_frame(7, payload=payload))
        assert frame.seq == 7
        assert frame.pcm == payload

    def test_sequence_header_is_big_endian(self) -> None:
        """Decision D-2. The header is network order even though the payload is little-endian.

        This is the single most likely cross-language integration bug in the build: a JS client using
        DataView's default (big-endian) for the header and setInt16(…, true) for the payload is
        correct, and one written the other way round produces a sequence of 2**56 on frame 1.
        """
        raw = make_frame(1)
        assert raw[:SEQ_PREFIX_BYTES] == b"\x00" * 7 + b"\x01"
        assert parse_frame(raw).seq == 1

    def test_accepts_max_uint64_sequence(self) -> None:
        big = 2**64 - 1
        assert parse_frame(make_frame(big)).seq == big

    def test_accepts_memoryview_and_bytearray(self) -> None:
        raw = make_frame(3)
        assert parse_frame(bytearray(raw)).seq == 3
        assert parse_frame(memoryview(raw)).seq == 3

    @pytest.mark.parametrize(
        "length", [0, 1, WS_FRAME_BYTES - 1, WS_FRAME_BYTES + 1, WS_FRAME_BYTES * 2]
    )
    def test_rejects_any_length_but_648(self, length: int) -> None:
        with pytest.raises(FrameRejected) as caught:
            parse_frame(b"\x00" * length)
        assert caught.value.code is FrameError.FRAME_SIZE
        assert caught.value.expected == WS_FRAME_BYTES
        assert caught.value.actual == length

    def test_off_by_one_short_frame_is_not_padded(self) -> None:
        """The specific failure R-24 exists to prevent: silent coercion of a nearly-right frame."""
        with pytest.raises(FrameRejected):
            parse_frame(make_frame(0)[:-1])

    def test_rejection_message_carries_no_client_data(self) -> None:
        """rules.md R-17: sizes live in typed attributes, not in the message string."""
        with pytest.raises(FrameRejected) as caught:
            parse_frame(b"\xde\xad\xbe\xef")
        assert str(caught.value) == FrameError.FRAME_SIZE.value


class TestFrameInvariant:
    def test_direct_construction_still_validates(self) -> None:
        """__post_init__ closes the bypass: parse_frame is not the only door into the type."""
        with pytest.raises(FrameRejected):
            Frame(seq=0, pcm=b"\x00" * (BYTES_PER_FRAME_PAYLOAD - 2))

    def test_frame_is_immutable(self) -> None:
        frame = parse_frame(make_frame(0))
        with pytest.raises((AttributeError, TypeError)):
            frame.seq = 99  # type: ignore[misc]


class TestSequence:
    def test_strict_increment_passes(self) -> None:
        check_sequence(0, 0)
        check_sequence(1, 1)

    @pytest.mark.parametrize(
        ("seq", "expected"),
        [
            (0, 1),  # duplicate / rewind
            (2, 1),  # gap
            (1, 2),  # replayed earlier frame
            (0, 5),  # reconnect that did not reset the server-side counter
        ],
    )
    def test_any_deviation_is_rejected(self, seq: int, expected: int) -> None:
        with pytest.raises(FrameRejected) as caught:
            check_sequence(seq, expected)
        assert caught.value.code is FrameError.SEQUENCE
        assert (caught.value.expected, caught.value.actual) == (expected, seq)

    def test_duplicate_and_gap_are_the_same_error(self) -> None:
        """There is no lenient mode. Gap-filling with silence would insert un-transmitted audio into
        the evidence window, so a gap is as fatal as a duplicate — one code, one behaviour."""
        codes = set()
        for seq, expected in [(9, 1), (0, 1)]:
            with pytest.raises(FrameRejected) as caught:
                check_sequence(seq, expected)
            codes.add(caught.value.code)
        assert codes == {FrameError.SEQUENCE}


class TestSamples:
    def test_view_length_and_values(self) -> None:
        payload = struct.pack(f"<{SAMPLES_PER_FRAME}h", *([-1234] * SAMPLES_PER_FRAME))
        view = frame_samples(parse_frame(make_frame(0, payload=payload)))
        assert len(view) == SAMPLES_PER_FRAME
        assert view[0] == -1234
        assert view[-1] == -1234

    def test_extreme_values_survive_the_cast(self) -> None:
        payload = struct.pack("<2h", -32768, 32767) + b"\x00" * (BYTES_PER_FRAME_PAYLOAD - 4)
        view = frame_samples(parse_frame(make_frame(0, payload=payload)))
        assert (view[0], view[1]) == (-32768, 32767)
