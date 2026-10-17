"""Speech engines. Kokoro is optional (the `voice` extra); FakeSynth is for tests."""

from __future__ import annotations

import threading
from typing import Any, Protocol

import numpy as np

SAMPLE_RATE = 24000
INSTALL_HINT = (
    "Voiceovers need the optional voice extra. In the nomic env run: "
    "pip install torch --index-url https://download.pytorch.org/whl/cpu && "
    'pip install -e ".[voice]"'
)


class Synth(Protocol):
    sample_rate: int

    def speak(self, text: str, voice: str, speed: float) -> np.ndarray:
        """Mono float32 audio in [-1, 1] at ``sample_rate``."""
        ...


# Measured on this project's hardware (Kokoro-82M, real game lines): Ryzen CPU ~5x realtime,
# RTX 5060 ~70x. Used only to estimate render time in the UI.
REALTIME_FACTOR = {"cpu": 5.0, "cuda": 60.0}


def render_device() -> str:
    """'cuda' if Kokoro will run on the GPU, else 'cpu'."""
    try:
        import torch
    except ImportError:
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def kokoro_available() -> bool:
    try:
        import kokoro  # noqa: F401
    except ImportError:
        return False
    return True


class KokoroSynth:
    """Kokoro-82M via one shared model and a pipeline per accent (voice ids a*/b*)."""

    sample_rate = SAMPLE_RATE
    REPO = "hexgrad/Kokoro-82M"

    def __init__(self) -> None:
        self._model: Any = None
        self._pipes: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _pipe(self, lang: str) -> Any:
        from kokoro import KModel, KPipeline

        with self._lock:
            if self._model is None:
                import torch

                self._model = KModel(repo_id=self.REPO).eval()
                if torch.cuda.is_available():
                    self._model = self._model.to("cuda")
            if lang not in self._pipes:
                self._pipes[lang] = KPipeline(lang_code=lang, repo_id=self.REPO, model=self._model)
            return self._pipes[lang]

    def speak(self, text: str, voice: str, speed: float) -> np.ndarray:
        pipe = self._pipe(voice[0])
        chunks = [
            r.audio.detach().cpu().numpy()
            for r in pipe(text, voice=voice, speed=speed)
            if r.audio is not None
        ]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32)


class FakeSynth:
    """Deterministic stand-in: a quiet tone, 0.05s per word."""

    sample_rate = SAMPLE_RATE

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def speak(self, text: str, voice: str, speed: float) -> np.ndarray:
        self.calls.append((text, voice, speed))
        n = int(self.sample_rate * 0.05 * max(1, len(text.split())) / speed)
        freq = 200 + (sum(map(ord, voice)) % 200)
        t = np.arange(n, dtype=np.float32) / self.sample_rate
        return (0.1 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
