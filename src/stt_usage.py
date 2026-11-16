"""Métricas de uso STT que sobreviven al paquete ligero, sin estimar saldos.

Los minutos enviados no equivalen a factura ni a cuota disponible. Se resume por
bloque de grabación para sustituir el mismo bloque al reconsolidar y conservar
bloques anteriores al continuar una clase compactada.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from . import audio_manifest, study_package


def _seconds(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def summarize(class_dir: Path | str) -> dict:
    d = Path(class_dir)
    previous = (study_package.read_metadata(d) or {}).get("stt_usage") or {}
    parts = dict(previous.get("parts") or {})
    attempts = {}
    for row in audio_manifest.transcription_attempts(d):
        provider = str(row.get("provider") or "")
        attempt = str(row.get("attempt_id") or "")
        chunk = str(row.get("chunk_id") or "")
        state = str(row.get("state") or row.get("status") or "")
        # The worker also logs "complete" after committing the transcript.
        # That is not another provider request or more consumed audio.
        if state not in {"started", "uploaded", "accepted", "polling",
                         "submission_unknown", "completed", "provider_failed",
                         "provider_unavailable", "cooldown"}:
            continue
        if provider not in {"assemblyai", "deepgram", "speechmatics", "groq"} or not attempt:
            continue
        key = (chunk, provider, attempt)
        attempts[key] = {**attempts.get(key, {}), **row}
    current = {}
    for (chunk, provider, _), row in attempts.items():
        match = re.search(r"_p(\d+)(?:_|$)", chunk)
        part = match.group(1) if match else "unknown"
        stats = current.setdefault(part, {}).setdefault(provider, {
            "completed_requests": 0, "failed_requests": 0, "unresolved_requests": 0,
            "submitted_seconds": 0.0, "original_seconds_measured": 0.0,
            "silence_seconds_saved": 0.0, "requests_without_original_duration": 0,
            "requests_without_submitted_duration": 0,
        })
        state = str(row.get("state") or row.get("status") or "")
        if state in {"completed", "complete"}:
            stats["completed_requests"] += 1
            sent = _seconds(row.get("submitted_audio_seconds"))
            original = _seconds(row.get("original_audio_seconds"))
            stats["submitted_seconds"] += sent or 0.0
            if sent is None:
                stats["requests_without_submitted_duration"] += 1
            if original is None:
                stats["requests_without_original_duration"] += 1
            else:
                stats["original_seconds_measured"] += original
                if sent is not None:
                    stats["silence_seconds_saved"] += max(0.0, original - sent)
        elif state in {"provider_failed", "failed", "provider_unavailable"}:
            stats["failed_requests"] += 1
        elif state != "cooldown":
            stats["unresolved_requests"] += 1
    parts.update(current)
    for providers in parts.values():
        for stats in providers.values():
            for key, value in stats.items():
                if isinstance(value, float):
                    stats[key] = round(value, 3)
    return {"schema_version": 1, "basis": "local_attempts_not_billing",
            "quota_remaining": None, "parts": parts}
