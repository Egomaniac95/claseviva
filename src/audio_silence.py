"""Recorta únicamente pausas digitales largas y conserva el reloj de origen.

No clasifica habla: música, ruido y voz tenue se conservan. No modifica WAV.
El mapa es determinista para poder recuperar trabajos STT ya enviados.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


VERSION = "digital-silence-v2"
LEGACY_VERSION = "digital-silence-v1"
RATE = 16000


@dataclass(frozen=True)
class SilenceMap:
    # (inicio en audio enviado, inicio original, cantidad de muestras)
    spans: tuple[tuple[int, int, int], ...]
    original_samples: int
    submitted_samples: int

    def original_time(self, seconds: float, *, end: bool = False) -> float:
        sample = min(self.submitted_samples, max(0.0, float(seconds) * RATE))
        for index, (packed, original, length) in enumerate(self.spans):
            boundary = packed + length
            if sample < boundary or (end and sample == boundary) or index == len(self.spans) - 1:
                return (original + min(length, max(0, sample - packed))) / RATE
        return float(seconds)


def trim_digital_silence(audio: np.ndarray, *, legacy: bool = False) -> tuple[np.ndarray, SilenceMap]:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    identity = SilenceMap(((0, 0, len(samples)),), len(samples), len(samples))
    if not len(samples) or not np.isfinite(samples).all():
        return samples, identity
    # Los trabajos antiguos deben recuperar exactamente los bytes enviados.
    # En trabajos nuevos una señal no nula se conserva aunque sea muy tenue.
    quiet = np.abs(samples) <= 2.0 / 32767.0 if legacy else samples == 0
    edges = np.diff(np.r_[False, quiet, False].astype(np.int8))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    margin = round(0.3 * RATE)
    cuts = [(int(start + margin), int(end - margin))
            for start, end in zip(starts, ends)
            if end - start >= 2 * RATE]
    if not cuts:
        return samples, identity
    spans = []
    cursor = packed = 0
    for start, end in cuts:
        if start > cursor:
            spans.append((packed, cursor, start - cursor))
            packed += start - cursor
        cursor = end
    if cursor < len(samples):
        spans.append((packed, cursor, len(samples) - cursor))
        packed += len(samples) - cursor
    # Do not turn an all-silent chunk into an empty request; the existing gate
    # owns that decision and its terminal commit semantics.
    if not spans or packed < RATE:
        return samples, identity
    result = np.concatenate([samples[start:start + length]
                             for _, start, length in spans])
    return result, SilenceMap(tuple(spans), len(samples), len(result))
