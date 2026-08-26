"""Voiced-audio ring buffer and hop trigger — PURE, no I/O (rules.md R-53).

Holds at most 2.56 seconds of VOICED audio in process memory and nowhere else. This module opens
no file, no socket, and no database handle, and it reads no clock. That is not an aesthetic
preference: this is the single place raw audio exists in the Gateway, so it is the place where a
privacy bug would be most expensive, and the cheapest way to keep it auditable is to make it
trivially testable without I/O (rules.md R-14).

Design notes that must not be "optimized" away:

* Silence never enters the buffer. Absence of voiced audio is not evidence, and the playbook is
  explicit that inferring from silence is out of scope.
* Overflow is structurally impossible, not prevented by a check that a later refactor could drop:
  the backing store is a ``deque(maxlen=WINDOW_SAMPLES)``.
* No window is emitted until the buffer is FULL. The first decision therefore needs 2.56 s of
  *voiced* audio, which is more wall-clock time than 2.56 s. That is expected, not a bug.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass
from typing import Final

from app.constants import HOP_SAMPLES, SAMPLES_PER_FRAME, WINDOW_BYTES, WINDOW_SAMPLES

_PACK_WINDOW: Final = struct.Struct(f"<{WINDOW_SAMPLES}h").pack


@dataclass(frozen=True, slots=True)
class RingStats:
    """Observable counters. Sample *values* are never exposed — only counts."""

    voiced_samples_buffered: int
    voiced_samples_total: int
    discarded_frames: int
    windows_emitted: int
    is_full: bool


class VoicedRingBuffer:
    """Accumulates voiced samples; emits an 81,920-byte window every 640 ms of voiced audio.

    Usage::

        ring = VoicedRingBuffer()
        window = ring.push(samples, voiced=True)   # bytes | None
        if window is not None:
            ...  # send to ScoreWindow
        ring.clear()                               # in a finally block, always

    ``push`` returns the window bytes at most once per call, because one 20 ms frame can never
    complete two 640 ms hops.
    """

    __slots__ = ("_buf", "_since_hop", "_voiced_total", "_discarded", "_emitted")

    def __init__(self) -> None:
        self._buf: deque[int] = deque(maxlen=WINDOW_SAMPLES)
        self._since_hop = 0
        self._voiced_total = 0
        self._discarded = 0
        self._emitted = 0

    # -- ingest ----------------------------------------------------------------------------------

    def push(self, samples: "list[int] | memoryview | tuple[int, ...]", *, voiced: bool) -> bytes | None:
        """Offer one frame's samples.

        Args:
            samples: exactly ``SAMPLES_PER_FRAME`` int16 values.
            voiced: VAD verdict for this frame. ``False`` discards it entirely — the samples are
                not buffered and do not advance the hop counter.

        Returns:
            An 81,920-byte int16-LE window when this frame completed a hop AND the buffer is
            full; otherwise ``None``.

        Raises:
            ValueError: if the frame is not exactly ``SAMPLES_PER_FRAME`` samples. Coercion is
                forbidden (rules.md R-24), and a short frame here would mean the caller
                bypassed ``frames.parse_frame``.
        """
        if len(samples) != SAMPLES_PER_FRAME:
            raise ValueError(f"expected {SAMPLES_PER_FRAME} samples, got {len(samples)}")

        if not voiced:
            self._discarded += 1
            return None

        self._buf.extend(samples)
        self._since_hop += SAMPLES_PER_FRAME
        self._voiced_total += SAMPLES_PER_FRAME

        if self._since_hop < HOP_SAMPLES:
            return None

        # Consume exactly one hop's worth of credit. Subtracting rather than zeroing keeps the
        # cadence honest if a caller ever pushes a non-multiple frame size in the future.
        self._since_hop -= HOP_SAMPLES

        if len(self._buf) < WINDOW_SAMPLES:
            return None  # hop reached, but there is not yet 2.56 s of voiced audio to score

        self._emitted += 1
        return _PACK_WINDOW(*self._buf)

    # -- lifecycle -------------------------------------------------------------------------------

    def clear(self) -> None:
        """Drop all buffered audio. Idempotent; safe to call from a ``finally`` block.

        Overwrites the backing samples with zeros before releasing the deque. CPython gives no
        guarantee about when the old ints are collected, so this is defence in depth rather than a
        cryptographic wipe — but it means a post-mortem heap dump of a *live* process cannot show
        the audio of a session that already closed.
        """
        n = len(self._buf)
        if n:
            self._buf.clear()
            self._buf.extend([0] * n)
            self._buf.clear()
        self._since_hop = 0

    # -- observation -----------------------------------------------------------------------------

    @property
    def is_full(self) -> bool:
        return len(self._buf) == WINDOW_SAMPLES

    def stats(self) -> RingStats:
        return RingStats(
            voiced_samples_buffered=len(self._buf),
            voiced_samples_total=self._voiced_total,
            discarded_frames=self._discarded,
            windows_emitted=self._emitted,
            is_full=self.is_full,
        )


def assert_window_size(window: bytes) -> None:
    """Guard for the gRPC boundary: exactly 81,920 bytes or it is an error."""
    if len(window) != WINDOW_BYTES:
        raise ValueError(f"expected {WINDOW_BYTES} bytes, got {len(window)}")
