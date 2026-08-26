"""Voice activity detection — a thin, explicit wrapper over WebRTC VAD.

Wrapped rather than called inline for two reasons: the frame-size requirement is strict enough to be
worth asserting in one place, and a VAD verdict must never become spoof evidence.

**A VAD outcome is a gating signal, not evidence** (playbook section 8). It decides whether a frame
enters the analysis window. It never contributes to ``spoof_risk``, never appears in a policy
decision, and never lands in an audit row. Silence is discarded, not scored — the playbook is
explicit that inferring from silence is out of scope.

Aggressiveness is configuration with a documented default rather than a tuned constant: mode 2 is the
middle of WebRTC's 0–3 range. Mode 3 is the most aggressive filter and would discard quiet speech,
which on this system does not mean "less audio" — it means the 2.56 s voiced window takes longer to
fill and the first decision arrives later. That trade-off is a measurement, not a guess, so the value
is a setting and the Day-2 sweep records what was chosen.
"""

from __future__ import annotations

from typing import Final

import webrtcvad

from app.constants import FRAME_MS, SAMPLE_RATE_HZ, SAMPLES_PER_FRAME

#: WebRTC VAD accepts only 10, 20, or 30 ms frames at 8/16/32/48 kHz. 20 ms at 16 kHz is the
#: contract (contracts/frame_contract.md), so the supported set is a single pair here.
_SUPPORTED_FRAME_MS: Final[frozenset[int]] = frozenset({10, 20, 30})
DEFAULT_AGGRESSIVENESS: Final[int] = 2


class VadError(ValueError):
    """The frame cannot be evaluated. Never carries sample data."""


class FrameVad:
    """Per-stream VAD. One instance per session; WebRTC VAD carries internal state.

    Not shared across sessions: the detector adapts to the noise floor it has seen, so sharing one
    instance would let one caller's channel conditions influence another's gating decisions.
    """

    __slots__ = ("_aggressiveness", "_unvoiced", "_vad", "_voiced")

    def __init__(self, aggressiveness: int = DEFAULT_AGGRESSIVENESS):
        if not 0 <= aggressiveness <= 3:
            raise VadError("aggressiveness must be in 0..3")
        if FRAME_MS not in _SUPPORTED_FRAME_MS:
            raise VadError(f"FRAME_MS={FRAME_MS} is not a WebRTC VAD frame duration")
        self._vad = webrtcvad.Vad(aggressiveness)
        self._aggressiveness = aggressiveness
        self._voiced = 0
        self._unvoiced = 0

    @property
    def aggressiveness(self) -> int:
        return self._aggressiveness

    @property
    def counts(self) -> tuple[int, int]:
        """``(voiced_frames, unvoiced_frames)``. Counts only — never sample values."""
        return self._voiced, self._unvoiced

    def is_voiced(self, pcm: bytes) -> bool:
        """Evaluate one 640-byte int16-LE frame.

        Raises:
            VadError: if the payload is not exactly one contract frame. Coercion is forbidden
                (rules.md R-24); a wrong length here means the caller bypassed ``parse_frame``.
        """
        if len(pcm) != SAMPLES_PER_FRAME * 2:
            raise VadError(f"expected {SAMPLES_PER_FRAME * 2} bytes, got {len(pcm)}")
        try:
            voiced = self._vad.is_speech(pcm, SAMPLE_RATE_HZ)
        except Exception as exc:  # webrtcvad raises bare Exception on bad input
            raise VadError("VAD rejected the frame") from exc

        if voiced:
            self._voiced += 1
        else:
            self._unvoiced += 1
        return bool(voiced)
