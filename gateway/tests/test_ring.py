"""Ring buffer and hop-cadence tests.

The cadence arithmetic is the part that is easy to get subtly wrong and hard to notice: a buffer that
emits every 640 ms but only holds 2.0 s of audio still produces a score, still produces a decision, and
still looks correct on screen. These tests pin the exact frame counts at which a window may and may not
appear.
"""

from __future__ import annotations

import struct

import pytest

from app.audio.ring import RingStats, VoicedRingBuffer, assert_window_size
from app.constants import (
    FRAMES_PER_HOP,
    HOPS_PER_WINDOW,
    SAMPLES_PER_FRAME,
    WINDOW_BYTES,
    WINDOW_SAMPLES,
)
from tests.conftest import make_samples

#: Frames of voiced audio needed to fill the window: 40960 / 320 = 128.
FRAMES_PER_WINDOW = WINDOW_SAMPLES // SAMPLES_PER_FRAME


def push_voiced(ring: VoicedRingBuffer, count: int, *, fill: int = 100) -> list[bytes]:
    return [w for _ in range(count) if (w := ring.push(make_samples(fill), voiced=True)) is not None]


class TestFillBehaviour:
    def test_no_window_before_the_buffer_is_full(self) -> None:
        """The first decision needs 2.56 s of VOICED audio, not 2.56 s of wall clock.

        Emitting a short window early would be the tempting "make the demo feel responsive" change,
        and it would mean the first score describes a different amount of audio than every later one.
        """
        ring = VoicedRingBuffer()
        assert push_voiced(ring, FRAMES_PER_WINDOW - 1) == []
        assert not ring.is_full

    def test_first_window_arrives_exactly_when_full(self) -> None:
        ring = VoicedRingBuffer()
        windows = push_voiced(ring, FRAMES_PER_WINDOW)
        assert len(windows) == 1
        assert ring.is_full

    def test_window_is_exactly_81920_bytes(self) -> None:
        ring = VoicedRingBuffer()
        window = push_voiced(ring, FRAMES_PER_WINDOW)[0]
        assert len(window) == WINDOW_BYTES
        assert_window_size(window)

    def test_frames_per_window_matches_the_hop_arithmetic(self) -> None:
        assert FRAMES_PER_WINDOW == FRAMES_PER_HOP * HOPS_PER_WINDOW == 128


class TestHopCadence:
    def test_one_window_per_hop_after_the_first(self) -> None:
        ring = VoicedRingBuffer()
        push_voiced(ring, FRAMES_PER_WINDOW)
        for _ in range(5):
            assert push_voiced(ring, FRAMES_PER_HOP - 1) == []
            assert len(push_voiced(ring, 1)) == 1

    def test_a_single_frame_never_emits_two_windows(self) -> None:
        """push returns at most one window per call, because a 20 ms frame cannot complete two 640 ms
        hops. So the emission count over a run of frames is exactly frames // FRAMES_PER_HOP — a
        buffer that returned a list, or that looped internally, would exceed that."""
        ring = VoicedRingBuffer()
        push_voiced(ring, FRAMES_PER_WINDOW)
        frames = FRAMES_PER_HOP * 3
        emitted = sum(
            1 for i in range(frames) if ring.push(make_samples(i % 7), voiced=True) is not None
        )
        assert emitted == frames // FRAMES_PER_HOP == 3

    def test_overlap_is_75_percent(self) -> None:
        """Consecutive windows share 3 of 4 hops, which is what makes 4 scores per 2.56 s possible."""
        ring = VoicedRingBuffer()
        push_voiced(ring, FRAMES_PER_WINDOW, fill=1)
        second = push_voiced(ring, FRAMES_PER_HOP, fill=2)[0]
        samples = struct.unpack(f"<{WINDOW_SAMPLES}h", second)
        carried = sum(1 for s in samples if s == 1)
        fresh = sum(1 for s in samples if s == 2)
        assert fresh == FRAMES_PER_HOP * SAMPLES_PER_FRAME
        assert carried == WINDOW_SAMPLES - fresh
        assert carried / WINDOW_SAMPLES == 0.75


class TestVoicingGate:
    def test_unvoiced_frames_are_discarded_entirely(self) -> None:
        """Silence is not evidence. Unvoiced frames neither enter the buffer nor advance the hop."""
        ring = VoicedRingBuffer()
        for _ in range(1000):
            assert ring.push(make_samples(0), voiced=False) is None
        stats = ring.stats()
        assert stats.voiced_samples_buffered == 0
        assert stats.voiced_samples_total == 0
        assert stats.discarded_frames == 1000
        assert not stats.is_full

    def test_interleaved_silence_does_not_shift_the_cadence(self) -> None:
        ring = VoicedRingBuffer()
        emitted = 0
        for i in range(FRAMES_PER_WINDOW * 2):
            if ring.push(make_samples(i % 50), voiced=True) is not None:
                emitted += 1
            for _ in range(3):
                assert ring.push(make_samples(0), voiced=False) is None
        # 128 frames to fill, then one window every 32 frames.
        assert emitted == 1 + (FRAMES_PER_WINDOW * 2 - FRAMES_PER_WINDOW) // FRAMES_PER_HOP


class TestStructuralLimits:
    def test_buffer_cannot_overflow(self) -> None:
        """maxlen makes overflow impossible rather than checked. A later refactor cannot drop a
        guard that does not exist as a guard."""
        ring = VoicedRingBuffer()
        push_voiced(ring, FRAMES_PER_WINDOW * 10)
        assert ring.stats().voiced_samples_buffered == WINDOW_SAMPLES

    @pytest.mark.parametrize("count", [0, 1, SAMPLES_PER_FRAME - 1, SAMPLES_PER_FRAME + 1])
    def test_wrong_frame_length_raises_rather_than_coercing(self, count: int) -> None:
        ring = VoicedRingBuffer()
        with pytest.raises(ValueError):
            ring.push([0] * count, voiced=True)

    def test_wrong_frame_length_does_not_corrupt_state(self) -> None:
        ring = VoicedRingBuffer()
        push_voiced(ring, 10)
        with pytest.raises(ValueError):
            ring.push([0] * 5, voiced=True)
        assert ring.stats().voiced_samples_buffered == 10 * SAMPLES_PER_FRAME


class TestLifecycle:
    @pytest.mark.privacy
    def test_clear_empties_the_buffer(self) -> None:
        """rules.md R-14. Called from the stream's finally block on every exit path, including
        exceptions — which is why it must be idempotent and must never raise."""
        ring = VoicedRingBuffer()
        push_voiced(ring, FRAMES_PER_WINDOW)
        ring.clear()
        assert ring.stats().voiced_samples_buffered == 0
        assert not ring.is_full

    @pytest.mark.privacy
    def test_clear_is_idempotent_on_an_empty_buffer(self) -> None:
        ring = VoicedRingBuffer()
        ring.clear()
        ring.clear()
        assert ring.stats().voiced_samples_buffered == 0

    def test_clear_resets_the_hop_counter(self) -> None:
        """A resumed session is a new stream, not a spliced one: partial hop credit from before the
        clear must not carry over."""
        ring = VoicedRingBuffer()
        push_voiced(ring, FRAMES_PER_WINDOW)
        ring.clear()
        assert push_voiced(ring, FRAMES_PER_WINDOW - 1) == []
        assert len(push_voiced(ring, 1)) == 1

    def test_totals_survive_clear_for_metrics(self) -> None:
        """Cumulative counters are session telemetry; the audio is what gets dropped."""
        ring = VoicedRingBuffer()
        push_voiced(ring, 10)
        ring.clear()
        assert ring.stats().voiced_samples_total == 10 * SAMPLES_PER_FRAME


class TestStats:
    @pytest.mark.privacy
    def test_stats_expose_counts_only(self) -> None:
        """RingStats is the observation surface. If a sample value could be read from it, the
        buffer would no longer be the only place raw audio exists."""
        fields = set(RingStats.__dataclass_fields__)
        assert fields == {
            "voiced_samples_buffered",
            "voiced_samples_total",
            "discarded_frames",
            "windows_emitted",
            "is_full",
        }
        ring = VoicedRingBuffer()
        push_voiced(ring, 4, fill=31337)
        assert "31337" not in repr(ring.stats())


class TestWindowGuard:
    @pytest.mark.parametrize("length", [0, WINDOW_BYTES - 2, WINDOW_BYTES + 2])
    def test_assert_window_size_rejects_wrong_lengths(self, length: int) -> None:
        with pytest.raises(ValueError):
            assert_window_size(b"\x00" * length)
