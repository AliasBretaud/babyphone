from __future__ import annotations

import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from scipy.signal import welch


@dataclass
class CryEvent:
    timestamp: float
    energy: float
    ratio_mid_band: float


class CryDetector:
    """
    Détection simple des pleurs par analyse énergétique + spectrale.

    Cette heuristique cible les signatures audio typiques des pleurs :
    énergie élevée, contenu fréquentiel dans la bande 400-1500 Hz et
    persistance sur quelques fenêtres.
    """

    def __init__(
        self,
        sample_rate: int,
        window_seconds: float = 2.0,
        hop_seconds: float = 0.5,
        energy_threshold: float = 0.01,
        band_ratio_threshold: float = 0.45,
    ) -> None:
        self.sample_rate = sample_rate
        self.window_size = int(sample_rate * window_seconds)
        self.hop_size = int(sample_rate * hop_seconds)
        self.energy_threshold = energy_threshold
        self.band_ratio_threshold = band_ratio_threshold
        self._buffer = np.empty(0, dtype=np.float32)

    def process_samples(self, samples: np.ndarray) -> Optional[CryEvent]:
        """
        Ajoute des échantillons (mono, float32) et retourne un CryEvent
        si un cri est détecté.
        """
        self._buffer = np.concatenate((self._buffer, samples))
        while self._buffer.size >= self.window_size:
            window = self._buffer[: self.window_size]
            self._buffer = self._buffer[self.hop_size :]
            if event := self._detect(window):
                return event
        return None

    def _detect(self, window: np.ndarray) -> Optional[CryEvent]:
        energy = float(np.mean(window**2))
        if energy < self.energy_threshold:
            return None

        freqs, psd = welch(
            window,
            fs=self.sample_rate,
            nperseg=min(1024, self.window_size),
            scaling="spectrum",
        )
        total_energy = float(np.sum(psd))
        if total_energy <= 1e-8:
            return None

        band_mask = (freqs >= 400.0) & (freqs <= 1500.0)
        ratio_mid_band = float(np.sum(psd[band_mask]) / total_energy)

        if ratio_mid_band > self.band_ratio_threshold:
            return CryEvent(
                timestamp=time.time(),
                energy=energy,
                ratio_mid_band=ratio_mid_band,
            )
        return None


class AudioAnalyzer:
    """Gère l'enregistrement audio (optionnel) et la détection de pleurs."""

    def __init__(
        self,
        output_dir: str,
        record_audio: bool = False,
        cry_cooldown: float = 5.0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.record_audio = record_audio
        if self.record_audio:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        self._wave_file: Optional[wave.Wave_write] = None
        self._detector: Optional[CryDetector] = None
        self._last_cry_ts: float = 0.0
        self._cry_cooldown = cry_cooldown

    def ensure_writer(self, sample_rate: int) -> None:
        if not self.record_audio or self._wave_file is not None:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._wave_file = wave.open(
            str(self.output_dir / f"baby_audio_{timestamp}.wav"), "wb"
        )
        self._wave_file.setnchannels(1)
        self._wave_file.setsampwidth(2)  # 16 bits
        self._wave_file.setframerate(sample_rate)
        self._detector = CryDetector(sample_rate)

    def process_frame(self, frame) -> Optional[CryEvent]:

        pcm = frame.to_ndarray(format="s16")
        if pcm.ndim == 2:
            pcm = pcm.mean(axis=0).astype(np.int16)
        else:
            pcm = pcm.flatten()

        if pcm.size == 0:
            return None

        sample_rate = frame.sample_rate
        if self._detector is None:
            # Initialise le détecteur à la première frame
            self._detector = CryDetector(sample_rate)

        self.ensure_writer(sample_rate)

        if self.record_audio and self._wave_file is not None:
            self._wave_file.writeframes(pcm.tobytes())

        float_samples = pcm.astype(np.float32) / 32768.0
        event = self._detector.process_samples(float_samples)
        if event and (event.timestamp - self._last_cry_ts) >= self._cry_cooldown:
            self._last_cry_ts = event.timestamp
            return event
        return None

    def close(self) -> None:
        if self._wave_file:
            try:
                self._wave_file.close()
            except OSError:
                pass
            self._wave_file = None
        self._detector = None
