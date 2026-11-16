"""Auditoría local de espacio, llamadas y ahorro de silencio; no usa APIs."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import time

from src import config, study_package, stt_usage


def audit(*, audio_samples: int = 0) -> dict:
    root = config.ROOT
    tags = defaultdict(lambda: {"calls": 0, "estimated_usd": 0.0,
                               "input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0})
    seen = set()
    path = root / "logs" / "uso_api.jsonl"
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not object")
        except ValueError:
            malformed += 1
            continue
        usage_id = row.get("usage_id")
        if usage_id and usage_id in seen:
            continue
        if usage_id:
            seen.add(usage_id)
        label = str(row.get("etiqueta") or "unknown")
        tag = tags[label]
        tag["calls"] += 1
        tag["estimated_usd"] += float(row.get("costo_usd") or 0)
        for target, source in (("input_tokens", "tokens_entrada"),
                               ("output_tokens", "tokens_salida"),
                               ("cache_hit_tokens", "tokens_cache_hit")):
            tag[target] += int(row.get(source) or 0)
    for tag in tags.values():
        tag["estimated_usd"] = round(tag["estimated_usd"], 9)
    classes, wavs = [], []
    for d in sorted(config.CONTENIDO_DIR.glob("*/clases/*")):
        if not d.is_dir():
            continue
        files = [p for p in d.rglob("*") if p.is_file()]
        audio = sorted((d / "audio").glob("*.wav"))
        wavs.extend(audio)
        final = d / "leccion_final.md"
        metadata = study_package.read_metadata(d)
        classes.append({
            "class": d.relative_to(root).as_posix(),
            "bytes": sum(p.stat().st_size for p in files), "files": len(files),
            "wav_count": len(audio), "wav_bytes": sum(p.stat().st_size for p in audio),
            "final_sha256": study_package.sha256_file(final) if final.is_file() else None,
            "has_package_metadata": bool(metadata), "stt_usage": stt_usage.summarize(d),
        })
    sample = {"files": 0, "seconds_before": 0.0, "seconds_after": 0.0,
              "errors": 0, "selection": "evenly_spaced_existing_wavs", "network_calls": 0}
    if audio_samples and wavs:
        from src import audio_silence, stt_backend, transcriptor_vivo
        from src.compat_av import cargar_wav16k, estirar_wsola
        count = min(audio_samples, len(wavs))
        indices = sorted({round(i * (len(wavs) - 1) / max(1, count - 1)) for i in range(count)})
        for index in indices:
            try:
                wav = wavs[index]
                data = cargar_wav16k(wav)
                if not stt_backend.wav_has_useful_signal(data)[0]:
                    continue  # already skipped by the existing gate, not new savings
                speed = transcriptor_vivo.velocidad_desde_chunk(wav)
                if speed > 1.01:
                    data = estirar_wsola(data, speed, sr=16000)
                data, _ = transcriptor_vivo.normalizar_audio_whisper(data)
                packed, _ = audio_silence.trim_digital_silence(data)
                sample["files"] += 1
                sample["seconds_before"] += len(data) / 16000
                sample["seconds_after"] += len(packed) / 16000
            except (OSError, ValueError, RuntimeError):
                sample["errors"] += 1
    sample["seconds_saved"] = sample["seconds_before"] - sample["seconds_after"]
    sample["percent_saved"] = round(
        100 * sample["seconds_saved"] / max(1, sample["seconds_before"]), 3)
    return {"schema_version": 1, "created_at": time.time(),
            "llm_log_malformed_lines": malformed, "llm_by_tag": dict(tags),
            "classes": classes, "silence_sample": sample}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-samples", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(audio_samples=max(0, args.audio_samples))
    study_package.write_text_atomic(args.output, json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"output": str(args.output), "classes": len(report["classes"]),
                      "silence_sample": report["silence_sample"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
