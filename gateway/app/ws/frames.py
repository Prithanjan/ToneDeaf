"""Binary frame parsing — PURE, no I/O (rules.md R-53).

One of the four modules where a correctness bug is most expensive, so it holds no state, opens
nothing, and reads no clock. Every branch is unit-testable with a bytes literal.

Wrong-shaped input is REJECTED, never coerced (rules.md R-24). Padding a short frame or trimming
a long one makes a wiring bug undetectable and destroys the cross-tier parity property.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Final

from app.constants import (
    SAMPLES_PER_FRAME,
    SEQ_PREFIX_BYTES,
    SEQ_STRUCT,
    WS_FRAME_BYTES,
)

_UNPACK_SEQ: Final = struct.Struct(SEQ_STRUCT).unpack_from


class FrameError(Enum):
    """Protocol violations. Values are the app-level codes in technical-design.md section 2.5."""

    FRAME_SIZE = "PROTO_FRAME_SIZE"
    SEQUENCE = "PROTO_SEQUENCE"


class FrameRejected(Exception):
    """Raised on a protocol violation.

    The message is STATIC and never interpolates client input (rules.md R-17). Sizes and sequence
    numbers are carried in typed attributes so a caller that wants them for a metric can read
    them without them ever landing in a formatted log string.
    """

    __slots__ = ("code", "expected", "actual")

    def __init__(self, code: FrameError, *, expected: int | None = None, actual: int | None = None):
        super().__init__(code.value)
        self.code = code
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class Frame:
    """One validated 20 ms frame. ``pcm`` is the raw int16-LE payload, exactly 640 bytes."""

    seq: int
    pcm: bytes

    def __post_init__(self) -> None:
        if len(self.pcm) != SAMPLES_PER_FRAME * 2:
            raise FrameRejected(FrameError.FRAME_SIZE, expected=SAMPLES_PER_FRAME * 2, actual=len(self.pcm))


def parse_frame(data: bytes | bytearray | memoryview) -> Frame:
    """Parse one binary WebSocket message into a :class:`Frame`.

    Layout (contracts/frame_contract.md section 2)::

         byte  0 ─────────────── 7 │ 8 ────────────────────────── 647
        ┌──────────────────────────┬────────────────────────────────┐
        │ sequence  uint64  BE     │ pcm  320 x int16  LE  (640 B)  │
        └──────────────────────────┴────────────────────────────────┘

    Raises:
        FrameRejected: with ``FrameError.FRAME_SIZE`` if the length is not exactly 648.
    """
    if len(data) != WS_FRAME_BYTES:
        raise FrameRejected(FrameError.FRAME_SIZE, expected=WS_FRAME_BYTES, actual=len(data))
    buf = bytes(data)
    (seq,) = _UNPACK_SEQ(buf, 0)
    return Frame(seq=seq, pcm=buf[SEQ_PREFIX_BYTES:])


def check_sequence(seq: int, expected: int) -> None:
    """Enforce strict ``+1`` monotonicity.

    A duplicate, a gap, or a rewind is all the same failure: the stream we are about to score is
    not the stream the client sent. Gap-filling with silence would put un-transmitted audio into
    the evidence window, so there is no lenient mode here.

    The counter starts at ``0`` and RESETS to ``0`` on reconnect — a resumed session is a new
    stream, not a spliced one (contracts/frame_contract.md section 2.1).
    """
    if seq != expected:
        raise FrameRejected(FrameError.SEQUENCE, expected=expected, actual=seq)


def frame_samples(frame: Frame) -> memoryview:
    """Zero-copy int16 view of the payload, for callers that want samples rather than bytes.

    The cast format is ``h`` (native short), which is correct because every deployment target is
    little-endian — that is precisely why D-1 chose LE for the payload. Where numpy is already in
    scope the explicit equivalent is ``np.frombuffer(frame.pcm, dtype=PCM_DTYPE)``.
    """
    return memoryview(frame.pcm).cast("h")
