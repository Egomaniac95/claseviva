"""Benchmark sintético read-only para la latencia WAV -> sealed -> commit.

Ejecuta el mismo persistidor y los mismos bytes en dos temporales. ``legacy`` retiene
el resultado hasta terminar el siguiente corte lógico; ``immediate`` usa el callback
productivo del worker. Whisper se modela con tiempos explícitos para aislar la causa de
publicación sin red, GPU ni evidencia real.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from src import audio_manifest, captura_audio  # noqa: E402
from src.transcriptor_vivo import metricas_commit_stt  # noqa: E402


SCHEMA_VERSION = 1


class BenchmarkError(RuntimeError):
    """Workload inválido o invariante roto."""


class LogicalClock:
    def __init__(self, start: float = 2_000_000_000.0):
        self._start = float(start)
        self._monotonic = time.monotonic()
        self._offset = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._start + self._offset + (
                time.monotonic() - self._monotonic)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._offset += max(0.0, float(seconds))


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _summary(values: list[float]) -> dict:
    clean = [max(0.0, float(value)) for value in values]
    return {
        "samples": len(clean),
        "p50_seconds": round(_percentile(clean, 0.50), 6),
        "p95_seconds": round(_percentile(clean, 0.95), 6),
        "max_seconds": round(max(clean), 6) if clean else 0.0,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_mode(
        root: Path, mode: str, *, chunks: int, rate: int, channels: int,
        canonical_seconds: float, capture_block_seconds: float,
        stt_poll_seconds: float, transcribe_seconds: float,
        commit_seconds: float) -> dict:
    class_dir = root / mode
    audio_dir = class_dir / "audio"
    audio_dir.mkdir(parents=True)
    clock = LogicalClock()
    published = threading.Event()
    wall_started = time.perf_counter()
    cpu_started = time.process_time()

    def publish(result):
        final = captura_audio.publicar_resultado_persistencia(
            class_dir, result, clock=clock)
        published.set()
        return final

    persistidor = captura_audio.PersistidorWav(
        capacidad=1,
        timeout_envio=1.0,
        timeout_cierre=5.0,
        publicador=publish if mode == "immediate" else None,
    )
    results = []
    frames = round(rate * canonical_seconds)
    pcm_frame = b"\x2a\x00" * channels
    for sequence in range(1, chunks + 1):
        wall_end = clock()
        path = audio_dir / f"chunk_{sequence:012d}_p1_v10.wav"
        event = {
            "chunk_id": path.stem,
            "sequence": sequence,
            "status": "sealed",
            "path": path.name,
            "frame_count": frames,
            "expected_frames": frames,
            "rate": rate,
            "channels": channels,
            "sample_width": 2,
            "duration": canonical_seconds,
            "pcm_wall_start": wall_end - canonical_seconds,
            "pcm_wall_end": wall_end,
            "transactional_stt_required": True,
        }
        audio_manifest.append_event(class_dir, {**event, "status": "capturing"})
        job = captura_audio.TrabajoPersistenciaWav(
            ruta=path,
            data=pcm_frame * frames,
            rate=rate,
            canales=channels,
            manifest_event=event,
            audio_log={},
        )
        if not persistidor.enviar(job):
            raise BenchmarkError(f"{mode}: backpressure inesperado en {sequence}")
        completed = persistidor.vaciar(timeout=5.0)
        if len(completed) != 1:
            raise BenchmarkError(f"{mode}: persistencia incompleta en {sequence}")
        result = completed[0]
        if mode == "legacy":
            # El worker terminó, pero el hilo principal ya está en leer(30 s).
            clock.advance(capture_block_seconds)
            result = captura_audio.publicar_resultado_persistencia(
                class_dir, result, clock=clock)
        else:
            if not published.wait(1.0):
                raise BenchmarkError(f"{mode}: callback no publicó {sequence}")
            published.clear()
            clock.advance(capture_block_seconds)
        results.append(result)
    persistidor.cerrar()

    rows = [
        row for row in audio_manifest.capture_records(class_dir)
        if row.get("status") == "sealed"
    ]
    rows.sort(key=lambda row: int(row.get("sequence") or 0))
    sealed_latencies = [
        float(row.get("audio_to_sealed_latency_seconds") or 0.0) for row in rows
    ]
    commit_latencies = []
    commit_chunk_ids = []
    for row in rows:
        committed_at = (
            float(row["sealed_at"])
            + stt_poll_seconds + transcribe_seconds + commit_seconds)
        metrics = metricas_commit_stt(
            row,
            committed_at=committed_at,
            transcribe_seconds=transcribe_seconds,
            audio_seconds=canonical_seconds,
            backlog=0,
        )
        audio_manifest.commit_transcript_chunk(
            class_dir,
            str(row["chunk_id"]),
            [],
            {
                "sequence": row["sequence"],
                "path": row["path"],
                "input_sha256": row["sha256"],
                "segments": 0,
                **metrics,
            },
        )
        commit = audio_manifest.read_transcript_commit(
            class_dir, str(row["chunk_id"]))
        if not commit:
            raise BenchmarkError(f"{mode}: commit inválido para {row['chunk_id']}")
        commit_chunk_ids.append(str(row["chunk_id"]))
        commit_latencies.append(float(metrics["commit_latency_seconds"]))
    hashes = [_sha256(audio_dir / str(row["path"])) for row in rows]
    sequences = [int(row["sequence"]) for row in rows]
    return {
        "mode": mode,
        "chunks": len(rows),
        "sequences": sequences,
        "unique_chunk_ids": len({str(row["chunk_id"]) for row in rows}),
        "commit_chunk_ids": commit_chunk_ids,
        "completed_commits": len(audio_manifest.completed_transcript_chunks(class_dir)),
        "wav_sha256": hashes,
        "wav_frames": [int(row["frame_count"]) for row in rows],
        "audio_to_sealed": _summary(sealed_latencies),
        "audio_to_commit": _summary(commit_latencies),
        "persistor_metrics": persistidor.metricas(),
        "wall_seconds": round(time.perf_counter() - wall_started, 6),
        "process_cpu_seconds": round(time.process_time() - cpu_started, 6),
    }


def run_benchmark(
        *, chunks: int = 8, rate: int = 800, channels: int = 1,
        canonical_seconds: float = 30.0, capture_block_seconds: float = 30.0,
        stt_poll_seconds: float = 2.0, transcribe_seconds: float = 3.0,
        commit_seconds: float = 0.01) -> dict:
    if chunks < 1 or rate < 1 or channels < 1 or canonical_seconds <= 0:
        raise BenchmarkError("workload inválido")
    with tempfile.TemporaryDirectory(prefix="clase-viva-sealed-benchmark-") as tmp:
        root = Path(tmp)
        common = {
            "chunks": chunks,
            "rate": rate,
            "channels": channels,
            "canonical_seconds": canonical_seconds,
            "capture_block_seconds": capture_block_seconds,
            "stt_poll_seconds": stt_poll_seconds,
            "transcribe_seconds": transcribe_seconds,
            "commit_seconds": commit_seconds,
        }
        legacy = _run_mode(root, "legacy", **common)
        immediate = _run_mode(root, "immediate", **common)

    invariant = {
        "same_sequences": legacy["sequences"] == immediate["sequences"],
        "same_wav_hashes": legacy["wav_sha256"] == immediate["wav_sha256"],
        "same_wav_frames": legacy["wav_frames"] == immediate["wav_frames"],
        "same_commit_chunk_ids": (
            legacy["commit_chunk_ids"] == immediate["commit_chunk_ids"]),
        "no_duplicates": (
            legacy["chunks"] == legacy["unique_chunk_ids"] == chunks
            and immediate["chunks"] == immediate["unique_chunk_ids"] == chunks
            and legacy["completed_commits"] == immediate["completed_commits"]
            == chunks),
    }
    if not all(invariant.values()):
        raise BenchmarkError(f"invariancia rota: {invariant}")
    before = legacy["audio_to_sealed"]["p95_seconds"]
    after = immediate["audio_to_sealed"]["p95_seconds"]
    if not after < before:
        raise BenchmarkError(f"sealed no mejoró: {before} -> {after}")
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "sealed-publication-synthetic",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "workload": common,
        "legacy": legacy,
        "immediate": immediate,
        "delta": {
            "audio_to_sealed_p95_seconds": round(after - before, 6),
            "audio_to_commit_p95_seconds": round(
                immediate["audio_to_commit"]["p95_seconds"]
                - legacy["audio_to_commit"]["p95_seconds"], 6),
        },
        "invariant": invariant,
        "ok": True,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compara publicación sealed legacy vs inmediata")
    parser.add_argument("--chunks", type=int, default=8)
    parser.add_argument("--rate", type=int, default=800)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--canonical-seconds", type=float, default=30.0)
    parser.add_argument("--capture-block-seconds", type=float, default=30.0)
    parser.add_argument("--stt-poll-seconds", type=float, default=2.0)
    parser.add_argument("--transcribe-seconds", type=float, default=3.0)
    parser.add_argument("--commit-seconds", type=float, default=0.01)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_benchmark(
            chunks=args.chunks,
            rate=args.rate,
            channels=args.channels,
            canonical_seconds=args.canonical_seconds,
            capture_block_seconds=args.capture_block_seconds,
            stt_poll_seconds=args.stt_poll_seconds,
            transcribe_seconds=args.transcribe_seconds,
            commit_seconds=args.commit_seconds,
        )
        _write_json(args.out.resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (BenchmarkError, OSError, ValueError) as error:
        print(f"[benchmark-sealed] ERROR: {error}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
