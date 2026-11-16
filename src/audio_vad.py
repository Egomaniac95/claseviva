"""Silero VAD local, CPU/ONNX: recorte conservador con reloj persistible.

No descarga modelos, no carga PyTorch y nunca modifica el WAV canónico. El estado
recurrente pertenece a cada audio; una sesión ONNX puede reutilizarse entre bloques.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib

import numpy as np

from . import app_paths, audio_silence
from .audio_silence import RATE, SilenceMap

VERSION = "silero-6.2.3-conservative-v2"
LEGACY_VERSION = "silero-6.2.3-conservative-v1"
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
WINDOW = 512
MIN_PAUSE = 2 * RATE
MARGIN = int(.4 * RATE)


@lru_cache(maxsize=1)
def _session():
    import onnxruntime as ort

    model = app_paths.resource_root() / "assets" / "silero_vad" / "silero_vad.onnx"
    if hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError("Silero VAD: el modelo no coincide con la versión publicada")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(model), sess_options=options,
                                providers=["CPUExecutionProvider"])


def probabilities(audio: np.ndarray) -> np.ndarray:
    """Probabilidad por ventana de 32 ms; estado/contexto aislados por bloque."""
    engine = _session()
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, 64), dtype=np.float32)
    result = []
    for start in range(0, len(audio), WINDOW):
        frame = np.zeros((1, WINDOW), dtype=np.float32)
        values = audio[start:start + WINDOW]
        frame[0, :len(values)] = values
        inputs = np.concatenate((context, frame), axis=1)
        score, state = engine.run(None, {
            "input": inputs, "state": state, "sr": np.array(RATE, dtype=np.int64),
        })
        context = inputs[:, -64:]
        result.append(float(score.reshape(-1)[0]))
    return np.asarray(result, dtype=np.float32)


def _pack(samples: np.ndarray, cuts: list[tuple[int, int]]) -> tuple[np.ndarray, SilenceMap]:
    spans = []
    cursor = packed = 0
    for start, end in cuts:
        if start > cursor:
            spans.append((packed, cursor, start - cursor))
            packed += start - cursor
        cursor = end
    if cursor < len(samples):
        spans.append((packed, cursor, len(samples) - cursor))
    if not spans:
        spans = [(0, 0, len(samples))]
    result = np.concatenate([samples[start:start + size] for _, start, size in spans])
    return result, SilenceMap(tuple(spans), len(samples), len(result))


def trim(audio: np.ndarray, *, predictor=None) -> tuple[np.ndarray, SilenceMap, dict]:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    details = {"engine": VERSION, "model_sha256": MODEL_SHA256, "reason": "ok"}
    try:
        if len(samples) < MIN_PAUSE or not np.isfinite(samples).all():
            raise ValueError("short_or_invalid_audio")
        scores = np.asarray((predictor or probabilities)(samples), dtype=np.float32)
        if (scores.shape != ((len(samples) + WINDOW - 1) // WINDOW,)
                or not np.isfinite(scores).all() or np.any((scores < 0) | (scores > 1))):
            raise ValueError("invalid_vad_output")
        # Absence of detected speech is not proof of absent words. Keep the old
        # digital-only filter when the detector cannot positively identify voice.
        if float(scores.max()) < .5:
            raise ValueError("no_confident_speech")
        # Una probabilidad VAD baja también puede corresponder a voz tenue.
        # Cortar exige además que todas las muestras sean cero digital.
        digital_quiet = np.array([
            not np.any(samples[start:start + WINDOW])
            for start in range(0, len(samples), WINDOW)], dtype=bool)
        quiet = (scores < .20) & digital_quiet
        edges = np.diff(np.r_[False, quiet, False].astype(np.int8))
        cuts = []
        for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
            start, end = int(a * WINDOW), min(len(samples), int(b * WINDOW))
            if end - start >= MIN_PAUSE:
                cuts.append((start + MARGIN, end - MARGIN))
        packed, clock = _pack(samples, cuts)
        details["cuts"] = len(cuts)
    except Exception as error:
        # A model/runtime failure must not prevent transcription or discard voice.
        packed, clock = audio_silence.trim_digital_silence(samples)
        details.update(reason=str(error) if isinstance(error, ValueError) else type(error).__name__,
                       fallback=audio_silence.VERSION)
    details["removed_seconds"] = round((len(samples) - len(packed)) / RATE, 3)
    return packed, clock, details


def serialize_map(clock: SilenceMap, packed: np.ndarray) -> dict:
    return {"rate": RATE, "original_samples": clock.original_samples,
            "submitted_samples": clock.submitted_samples,
            "spans": [list(span) for span in clock.spans],
            "packed_sha256": hashlib.sha256(np.asarray(packed, dtype="<f4").tobytes()).hexdigest()}


def restore_map(audio: np.ndarray, payload: dict) -> tuple[np.ndarray, SilenceMap]:
    """Recupera exactamente el audio enviado, sin volver a ejecutar el detector."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not isinstance(payload, dict):
        raise ValueError("mapa de audio pendiente inválido")
    if payload.get("rate") != RATE or payload.get("original_samples") != len(samples):
        raise ValueError("el audio pendiente tiene otra duración o frecuencia")
    spans = []
    packed_end = original_end = 0
    for row in payload.get("spans", []):
        if (not isinstance(row, (list, tuple)) or len(row) != 3
                or any(type(x) is not int for x in row)):
            raise ValueError("mapa de audio pendiente inválido")
        packed, original, size = row
        if packed != packed_end or original < original_end or size <= 0 or original + size > len(samples):
            raise ValueError("mapa de audio pendiente fuera de rango")
        spans.append(tuple(row))
        packed_end, original_end = packed + size, original + size
    if not spans or packed_end != payload.get("submitted_samples"):
        raise ValueError("mapa de audio pendiente incompleto")
    result = np.concatenate([samples[start:start + size] for _, start, size in spans])
    if hashlib.sha256(np.asarray(result, dtype="<f4").tobytes()).hexdigest() != payload.get("packed_sha256"):
        raise ValueError("el preprocesamiento del audio pendiente cambió")
    return result, SilenceMap(tuple(spans), len(samples), len(result))
