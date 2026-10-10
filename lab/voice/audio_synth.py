"""Caller audio: a line of text plus a persona (golden/personas.yaml) -> int16 PCM.

Speech comes from Kokoro, the same engine as the agent's voice, using the model files
Pipecat's Kokoro service downloads. Noise is synthetic and seeded: "engine" is piston
harmonics over pink noise, standing in for a cockpit; "pink" is plain pink noise.
Rendered audio is cached under results/audio/ keyed by text and persona.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import numpy as np
import soundfile as sf
import soxr
import yaml
from pydantic import BaseModel, ConfigDict

from lab.config import ROOT

PERSONAS = ROOT / "golden" / "personas.yaml"
CACHE = ROOT / "results" / "audio"


class Noise(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["engine", "pink"]
    snr_db: float


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    voice: str
    lang: str = "en-us"
    speed: float = 1.0
    noise: Noise | None = None
    pause_at: str | None = None
    pause_s: float = 0.0
    lead_in_s: float = 0.0


def load_personas(path: Path = PERSONAS) -> dict[str, Persona]:
    return {p["id"]: Persona.model_validate(p)
            for p in yaml.safe_load(path.read_text(encoding="utf-8"))}


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """1/f noise by shaping white noise in the frequency domain."""
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(len(spectrum))
    spectrum[1:] /= np.sqrt(f[1:])
    x = np.fft.irfft(spectrum, n)
    return x / (np.abs(x).max() or 1.0)


def engine_noise(n: int, rate: int, rng: np.random.Generator, rpm: float = 2400) -> np.ndarray:
    """Four-stroke piston engine: firing-frequency harmonics, a slow wobble, pink hiss."""
    t = np.arange(n) / rate
    firing = rpm / 60 * 2                                   # 4 cylinders, 2 revs per cycle
    wobble = 1 + 0.02 * np.sin(2 * np.pi * 0.7 * t)
    tone = sum(np.sin(2 * np.pi * firing * k * wobble * t + rng.uniform(0, 2 * np.pi)) / k
               for k in range(1, 7))
    x = 0.7 * tone / np.abs(tone).max() + 0.3 * pink_noise(n, rng)
    return x / np.abs(x).max()


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    p_s = np.mean(speech[np.abs(speech) > 1e-4] ** 2) if np.any(speech) else 1e-6
    p_n = np.mean(noise ** 2) or 1e-12
    out = speech + noise * np.sqrt(p_s / (p_n * 10 ** (snr_db / 10)))
    return out / max(1.0, np.abs(out).max())


class Synth:
    def __init__(self, rate: int = 16000):
        from kokoro_onnx import Kokoro
        from pipecat.services.kokoro.tts import KOKORO_CACHE_DIR, _ensure_model_files

        model, voices = KOKORO_CACHE_DIR / "kokoro-v1.0.onnx", KOKORO_CACHE_DIR / "voices-v1.0.bin"
        _ensure_model_files(model, voices)
        self.kokoro = Kokoro(str(model), str(voices))
        self.rate = rate

    def _speech(self, text: str, p: Persona) -> np.ndarray:
        samples, sr = self.kokoro.create(text, voice=p.voice, speed=p.speed, lang=p.lang)
        return soxr.resample(samples.astype(np.float32), sr, self.rate)

    def render(self, text: str, persona: Persona) -> np.ndarray:
        """int16 PCM at `rate`, cached on disk."""
        key = hashlib.sha256(json.dumps([text, persona.model_dump(), self.rate],
                                        sort_keys=True).encode()).hexdigest()[:16]
        path = CACHE / f"{persona.id}-{key}.wav"
        if path.is_file():
            pcm, _ = sf.read(path, dtype="int16")
            return pcm
        if persona.pause_at and persona.pause_at in text.strip():
            # Pause at the middle-most split point: "how do I | set up the terrain data".
            words = text.strip().split(persona.pause_at)
            mid = max(1, len(words) // 2)
            parts = [persona.pause_at.join(words[:mid]), persona.pause_at.join(words[mid:])]
            gap = np.zeros(int(persona.pause_s * self.rate), np.float32)
            speech = np.concatenate([self._speech(parts[0], persona), gap,
                                     self._speech(parts[1], persona)])
        else:
            speech = self._speech(text, persona)
        if persona.lead_in_s:
            speech = np.concatenate([np.zeros(int(persona.lead_in_s * self.rate),
                                              np.float32), speech])
        if persona.noise:
            rng = np.random.default_rng(int(key, 16) % 2**32)
            noise = (engine_noise(len(speech), self.rate, rng)
                     if persona.noise.kind == "engine" else pink_noise(len(speech), rng))
            speech = mix_at_snr(speech, noise, persona.noise.snr_db)
        pcm = (np.clip(speech, -1, 1) * 32767).astype(np.int16)
        CACHE.mkdir(parents=True, exist_ok=True)
        sf.write(path, pcm, self.rate)
        return pcm
