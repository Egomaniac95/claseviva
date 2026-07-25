"""Benchmark A/B del mismo pipeline visual que usa ClaseViva en vivo.

Los modos sintético y local escriben solo en un temporal. No capturan el monitor ni
certifican hardware. ``legacy`` reproduce el trabajo incondicional por frame observado;
``adaptive`` usa :class:`src.captura_pantalla.VisualEvidencePipeline`.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config as runtime_config  # noqa: E402
from src import imagen, ocr, visual_idle  # noqa: E402
from src.captura_pantalla import (  # noqa: E402
    VisualEvidencePipeline,
    codificar_jpeg,
    persistir_jpeg_atomico,
    preparar_evidencia,
)
from src.visual_runtime import VisualRuntimeMetrics  # noqa: E402


SCHEMA_VERSION = 3
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SYNTHETIC_ALGORITHM = "slides-v3-builds"


class BenchmarkError(RuntimeError):
    """Entrada o resultado inválido; la CLI debe terminar con código no cero."""


@dataclass(frozen=True)
class BenchmarkConfig:
    mode: str
    frames: int
    width: int
    height: int
    frame_interval_ms: float
    logical_interval_ms: float = 2000.0
    queue_capacity: int = 0
    expected_count: int | None = None
    input_dir: Path | None = None
    use_ocr: bool = False
    save_jpeg: bool = True
    pipeline: str = "adaptive"
    scenario: str = "mixed"


@dataclass(frozen=True)
class SourceFrame:
    source: int | Path
    label: str
    expected_relevant: bool = True


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _summary_ms(values: Sequence[float]) -> dict[str, float]:
    return {
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values, default=0.0), 3),
    }


def _slide_layout(draw: ImageDraw.ImageDraw, slide: int, width: int,
                  height: int) -> None:
    palette = (
        (26, 71, 128), (108, 42, 92), (25, 118, 82), (142, 65, 28),
        (54, 64, 152), (126, 102, 22),
    )
    color = palette[slide % len(palette)]
    margin = max(8, width // 40)
    draw.rectangle(
        (margin, margin, width - margin, max(margin + 8, height // 5)),
        fill=color,
    )
    direction = slide % 2
    if direction:
        draw.rectangle(
            (margin, height // 3, width // 2, height - margin),
            outline=color, width=max(2, width // 160))
        draw.line(
            (width // 2, height - margin, width - margin, height // 3),
            fill=(20, 20, 20), width=max(2, width // 200))
    else:
        draw.rectangle(
            (width // 2, height // 3, width - margin, height - margin),
            outline=color, width=max(2, width // 160))
        draw.line(
            (margin, height // 3, width // 2, height - margin),
            fill=(20, 20, 20), width=max(2, width // 200))
    draw.text(
        (margin * 2, max(margin + 2, height // 12)),
        f"ClaseViva slide {slide:03d} | f(x)={slide + 1}", fill="white")


def _build_layout(draw: ImageDraw.ImageDraw, build: int, width: int,
                  height: int) -> None:
    """Fixture geométrico: cada build agrega una línea técnica localizada."""
    margin = max(8, width // 40)
    draw.rectangle(
        (margin, margin, width - margin, max(margin + 8, height // 5)),
        fill=(28, 75, 145),
    )
    draw.rectangle(
        (margin * 2, height // 4, width * 3 // 5, height // 4 + max(8, height // 50)),
        fill=(35, 35, 42),
    )
    line_height = max(2, height // 70)
    line_gap = max(8, height // 12)
    for level in range(1, build + 1):
        y = height // 3 + (level - 1) * line_gap
        color = (25 + level * 18, 65 + level * 10, 125 + level * 12)
        draw.rectangle(
            (margin * 3, y, min(width - margin * 3, width // 2), y + line_height),
            fill=color,
        )
        draw.rectangle(
            (width // 2 + margin, y, width - margin * 4, y + line_height),
            fill=(40, 40, 48),
        )


def _synthetic_label(index: int, frames: int, scenario: str) -> str:
    if scenario == "stable":
        return "slide-000"
    if scenario in {"mixed", "silent"}:
        block = max(1, frames // 4)
        return f"slide-{min(3, index // block):03d}"
    if scenario == "builds":
        block = max(1, frames // 6)
        return f"build-{min(5, index // block):03d}"
    if scenario == "frequent":
        return f"slide-{index:03d}"
    raise BenchmarkError(f"scenario sintético inválido: {scenario}")


def _synthetic_image(index: int, frames: int, width: int, height: int,
                     scenario: str) -> Image.Image:
    label = _synthetic_label(index, frames, scenario)
    state = int(label.rsplit("-", 1)[-1])
    image = Image.new("RGB", (width, height), (245, 245, 242))
    draw = ImageDraw.Draw(image)
    if scenario == "builds":
        _build_layout(draw, state, width, height)
    else:
        _slide_layout(draw, state, width, height)
    if scenario in {"mixed", "silent", "builds"}:
        # Movimiento pequeño de cursor/UI que no debe convertirse en una slide.
        x = 2 + (index * 7) % max(3, width - 4)
        y = max(1, height - 2)
        draw.point((x, y), fill=(90, 90, 90))
    return image


def _local_images(input_dir: Path, limit: int) -> list[Path]:
    if not input_dir.is_dir():
        raise BenchmarkError(f"input-dir no existe o no es directorio: {input_dir}")
    paths = sorted(
        path for path in input_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES
    )
    if limit > 0:
        paths = paths[:limit]
    if not paths:
        raise BenchmarkError("el benchmark local no encontró imágenes")
    return paths


def _content_fingerprint(config: BenchmarkConfig,
                         frames: Sequence[SourceFrame]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps({
        "mode": config.mode,
        "scenario": config.scenario,
        "width": config.width,
        "height": config.height,
        "frames": len(frames),
        "logical_interval_ms": config.logical_interval_ms,
        "synthetic_algorithm": SYNTHETIC_ALGORITHM,
    }, sort_keys=True).encode("utf-8"))
    for frame in frames:
        digest.update(frame.label.encode("utf-8") + b"\0")
        if isinstance(frame.source, Path):
            with frame.source.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(f"synthetic:{frame.source}".encode("ascii"))
    return digest.hexdigest()


def _sources(config: BenchmarkConfig) -> list[SourceFrame]:
    if config.mode == "synthetic":
        if config.frames < 1:
            raise BenchmarkError("el benchmark sintético requiere al menos un frame")
        return [
            SourceFrame(
                index,
                _synthetic_label(index, config.frames, config.scenario),
            )
            for index in range(config.frames)
        ]
    if config.input_dir is None:
        raise BenchmarkError("el benchmark local requiere --input-dir")
    return [SourceFrame(path, path.stem) for path in _local_images(
        config.input_dir, config.frames)]


def _open_source(frame: SourceFrame, config: BenchmarkConfig) -> Image.Image:
    if isinstance(frame.source, Path):
        with Image.open(frame.source) as current:
            current.load()
            return current.convert("RGB")
    return _synthetic_image(
        frame.source, config.frames, config.width, config.height,
        config.scenario)


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            capture_output=True, check=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        return "unavailable"


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


def _environment() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "cpu_model": platform.processor() or "unknown",
        "pillow": _version("Pillow"),
        "psutil": _version("psutil"),
        "rapidocr": _version("rapidocr-onnxruntime"),
        "onnxruntime": _version("onnxruntime"),
        "winrt_runtime": _version("winrt-runtime"),
        "winrt_windows_ocr": _version("winrt-Windows.Media.Ocr"),
        "commit": _git_commit(),
        "harness_hash": _file_hash(Path(__file__)),
    }


def _effective_config() -> dict:
    return {
        "capture_width": runtime_config.CAPTURA_ANCHO_MAX,
        "jpeg_quality": runtime_config.CAPTURA_CALIDAD,
        "probe_width": runtime_config.CAPTURA_PROBE_ANCHO,
        "coarse_threshold": runtime_config.UMBRAL_DHASH,
        "detail_threshold": runtime_config.UMBRAL_DHASH_DETALLE,
        "pixel_delta": runtime_config.CAPTURA_PIXEL_DELTA,
        "change_ratio": runtime_config.CAPTURA_CAMBIO_RATIO,
        "major_ratio": runtime_config.CAPTURA_CAMBIO_MAYOR_RATIO,
        "ocr_min_interval_s": runtime_config.CAPTURA_OCR_INTERVALO_MIN,
        "ocr_refresh_s": runtime_config.CAPTURA_OCR_REFRESH_SEG,
        "heartbeat_interval_s": runtime_config.CAPTURA_HEARTBEAT_SEG,
        "ocr_engine_requested": runtime_config.OCR_ENGINE,
        "ocr_rapid_intra_threads": runtime_config.OCR_RAPID_INTRA_THREADS,
        "ocr_rapid_inter_threads": runtime_config.OCR_RAPID_INTER_THREADS,
        "ocr_windows_languages": list(runtime_config.OCR_WINDOWS_LANGUAGES),
    }


def _implementation_hashes() -> dict:
    return {
        "capture": _file_hash(ROOT / "src" / "captura_pantalla.py"),
        "visual_runtime": _file_hash(ROOT / "src" / "visual_runtime.py"),
        "ocr": _file_hash(ROOT / "src" / "ocr.py"),
        "config": _file_hash(ROOT / "src" / "config.py"),
    }


def _state(config: BenchmarkConfig, epoch: float) -> dict:
    audio = "silencio" if config.scenario == "silent" else "activo"
    return {
        "estado": "grabando",
        "parte": 1,
        "auto_pausa": False,
        "audio_requires_attention": False,
        "audio_ultimo_estado": audio,
        "audio_silencio_chunks": 999 if audio == "silencio" else 0,
        "audio_ultimo_t": epoch,
    }


def _run_adaptive(
        config: BenchmarkConfig, frames: Sequence[SourceFrame], output: Path,
        ) -> tuple[dict, list[float], list[float], set[str], dict[str, float]]:
    metrics = VisualRuntimeMetrics(
        max_samples=runtime_config.CAPTURA_METRICAS_MUESTRAS)
    live_ocr = ocr.texto_de_pil if config.use_ocr else (lambda _img: "")
    pipeline = VisualEvidencePipeline(
        output / "img", output / "img_log.jsonl",
        output / "visual_timeline.jsonl", ocr_fn=live_ocr,
        save_jpeg=config.save_jpeg, metrics=metrics)
    processing: list[float] = []
    end_to_end: list[float] = []
    detected: set[str] = set()
    detection_latency: dict[str, float] = {}
    origin = 1_000.0
    interval_s = config.frame_interval_ms / 1000.0
    logical_interval_s = config.logical_interval_ms / 1000.0
    try:
        for index, frame in enumerate(frames):
            iteration_started = time.perf_counter()
            started = iteration_started
            with metrics.stage("acquisition"):
                image = _open_source(frame, config)
            acquired = time.perf_counter()
            epoch = origin + index * logical_interval_s
            outcome = pipeline.process(
                image, epoch=epoch, parte=1, state=_state(config, epoch))
            finished = time.perf_counter()
            processing.append((finished - acquired) * 1000.0)
            end_to_end.append((finished - started) * 1000.0)
            if outcome.saved:
                detected.add(frame.label)
                detection_latency.setdefault(
                    frame.label, (finished - started) * 1000.0)
            remaining = interval_s - (finished - iteration_started)
            if remaining > 0:
                with metrics.stage("poll_wait"):
                    time.sleep(remaining)
            elif interval_s:
                metrics.record_overrun(-remaining)
        pipeline.close(
            epoch=origin + len(frames) * max(logical_interval_s, 0.001),
            parte=1, motivo="benchmark_end")
    finally:
        pipeline.close(epoch=time.time(), parte=1, motivo="benchmark_finally")
    return pipeline.snapshot(), processing, end_to_end, detected, detection_latency


def _run_legacy(
        config: BenchmarkConfig, frames: Sequence[SourceFrame], output: Path,
        ) -> tuple[dict, list[float], list[float], set[str], dict[str, float]]:
    metrics = VisualRuntimeMetrics(
        max_samples=runtime_config.CAPTURA_METRICAS_MUESTRAS)
    processing: list[float] = []
    end_to_end: list[float] = []
    detected: set[str] = set()
    detection_latency: dict[str, float] = {}
    interval_s = config.frame_interval_ms / 1000.0
    (output / "img").mkdir(parents=True, exist_ok=True)
    for index, frame in enumerate(frames):
        iteration_started = time.perf_counter()
        started = iteration_started
        with metrics.stage("acquisition"):
            image = _open_source(frame, config)
        acquired = time.perf_counter()
        with metrics.frame():
            metrics.increment("frames_observed")
            with metrics.stage("hash_compare"):
                imagen.dhash(image)
                imagen.dhash(image, tam=16)
            with metrics.stage("visual_idle"):
                visual_idle.analizar_imagen(image)
            with metrics.stage("resize"):
                canonical = preparar_evidencia(image)
            if config.use_ocr:
                metrics.increment("ocr_runs")
                with metrics.stage("ocr"):
                    ocr.texto_de_pil(canonical)
            if config.save_jpeg:
                with metrics.stage("compression"):
                    encoded = codificar_jpeg(canonical)
                with metrics.stage("persistence"):
                    persistir_jpeg_atomico(
                        encoded, output / "img" / f"legacy_{index:06d}.jpg")
            metrics.increment("frames_saved")
        finished = time.perf_counter()
        processing.append((finished - acquired) * 1000.0)
        end_to_end.append((finished - started) * 1000.0)
        detected.add(frame.label)
        detection_latency.setdefault(
            frame.label, (finished - started) * 1000.0)
        remaining = interval_s - (finished - iteration_started)
        if remaining > 0:
            with metrics.stage("poll_wait"):
                time.sleep(remaining)
        elif interval_s:
            metrics.record_overrun(-remaining)
    metrics.increment("closed")
    metrics.close()
    return metrics.snapshot(), processing, end_to_end, detected, detection_latency


def run_benchmark(config: BenchmarkConfig) -> dict:
    if config.mode not in {"synthetic", "local"}:
        raise BenchmarkError("mode debe ser synthetic o local")
    if config.pipeline not in {"legacy", "adaptive"}:
        raise BenchmarkError("pipeline debe ser legacy o adaptive")
    if config.mode == "synthetic" and config.scenario not in {
            "stable", "mixed", "frequent", "silent", "builds"}:
        raise BenchmarkError("scenario sintético inválido")
    if config.width < 32 or config.height < 32:
        raise BenchmarkError("width y height deben ser >= 32")
    if config.frame_interval_ms < 0:
        raise BenchmarkError("frame-interval-ms no puede ser negativo")
    if config.logical_interval_ms <= 0:
        raise BenchmarkError("logical-interval-ms debe ser mayor que cero")
    if config.queue_capacity != 0:
        raise BenchmarkError(
            "el pipeline real no tiene cola; queue-capacity debe ser 0")
    frames = _sources(config)
    expected_frames = (
        config.expected_count if config.expected_count is not None else len(frames))
    if expected_frames != len(frames) or expected_frames < 1:
        raise BenchmarkError(
            "expected-count debe coincidir con los frames ofrecidos y ser mayor que cero")
    fingerprint = _content_fingerprint(config, frames)
    expected_labels = {frame.label for frame in frames if frame.expected_relevant}
    errors: list[str] = []
    wall_started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="clase-viva-visual-bench-") as temp:
        output = Path(temp)
        runtime_paths = {
            name: getattr(runtime_config, name)
            for name in ("CONTENIDO_DIR", "CLASES_DIR", "RESUMENES_DIR", "LOGS_DIR")
        }
        runtime_config.CONTENIDO_DIR = output / "contenido"
        runtime_config.CLASES_DIR = output / "contenido" / "benchmark" / "clases"
        runtime_config.RESUMENES_DIR = output / "contenido" / "benchmark" / "resumenes"
        runtime_config.LOGS_DIR = output / "logs"
        try:
            if config.pipeline == "adaptive":
                snapshot, processing, end_to_end, detected, detection_latency = (
                    _run_adaptive(config, frames, output))
            else:
                snapshot, processing, end_to_end, detected, detection_latency = (
                    _run_legacy(config, frames, output))
            if config.use_ocr:
                if not ocr.estado_motor().get("operational"):
                    raise BenchmarkError(
                        "OCR solicitado pero ningún backend está operativo")
                ocr_errors = runtime_config.LOGS_DIR / "errores.log"
                if ocr_errors.is_file() and ocr_errors.stat().st_size:
                    raise BenchmarkError("OCR reportó un error de carga o inferencia")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            raise BenchmarkError(errors[-1]) from exc
        finally:
            for name, value in runtime_paths.items():
                setattr(runtime_config, name, value)
    wall_finished = time.perf_counter()
    missed = sorted(expected_labels - detected)
    relevant_detected = len(expected_labels & detected)
    saved = int(snapshot.get("counters", {}).get("frames_saved", 0))
    duplicates = max(0, saved - relevant_detected)
    detection_ms = [
        detection_latency[label]
        for label in expected_labels if label in detection_latency]
    coverage = {
        "expected": expected_frames,
        "offered": len(frames),
        "processed": int(snapshot.get("counters", {}).get("frames_observed", 0)),
        "dropped": 0,
        "errors": len(errors),
        "ratio": round(len(frames) / expected_frames, 6),
        "relevant_expected": len(expected_labels),
        "relevant_detected": relevant_detected,
        "relevant_missed": missed,
        "relevant_recall": round(
            relevant_detected / max(1, len(expected_labels)), 6),
        "saved": saved,
        "duplicates": duplicates,
        "detection_latency_ms": _summary_ms(detection_ms),
    }
    workload = {
        "mode": config.mode,
        "scenario": config.scenario if config.mode == "synthetic" else "local",
        "frames_offered": len(frames),
        "expected_count": expected_frames,
        "width": config.width,
        "height": config.height,
        "frame_interval_ms": config.frame_interval_ms,
        "logical_interval_ms": config.logical_interval_ms,
        "queue_capacity": 0,
        "ocr": config.use_ocr,
        "save_jpeg": config.save_jpeg,
        "source_fingerprint": fingerprint,
    }
    ocr_runtime = ocr.estado_motor()
    metrics = {
        "wall_seconds": round(wall_finished - wall_started, 6),
        "cpu_seconds": snapshot["cpu_seconds"],
        "cpu_percent_one_core": snapshot["cpu_percent_one_core"],
        "ram": snapshot["ram"],
        "processing_latency_ms": _summary_ms(processing),
        "end_to_end_latency_ms": _summary_ms(end_to_end),
        "stages": snapshot["stages"],
        "scheduler": {
            "poll_overrun_ms": snapshot["poll_overrun_ms"],
            "wait_ms": snapshot.get("stages", {}).get(
                "poll_wait", {}).get("wall_ms", _summary_ms([])),
            "overrun_count": int(
                snapshot.get("counters", {}).get("poll_overruns", 0)),
        },
        "queue": snapshot["queue"],
        "coverage": coverage,
        "counters": snapshot.get("counters", {}),
        "closed": bool(snapshot.get("closed")),
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "captura_visual",
        "mode": config.mode,
        "implementation": {
            "pipeline": config.pipeline,
            "queue_contract": "none_one_frame_in_flight",
            "effective_config": _effective_config(),
            "code_hashes": _implementation_hashes(),
            "ocr_runtime": ocr_runtime,
        },
        "environment": _environment(),
        "workload": workload,
        "metrics": metrics,
        "errors": errors,
        "exit_code": 0,
    }
    validate_result(result)
    return result


def validate_result(result: dict) -> None:
    if result.get("schema_version") != SCHEMA_VERSION:
        raise BenchmarkError("schema_version ausente o incompatible")
    if result.get("benchmark") != "captura_visual":
        raise BenchmarkError("benchmark no es captura_visual")
    if result.get("mode") not in {"synthetic", "local"}:
        raise BenchmarkError("mode inválido en resultado")
    if result.get("exit_code") != 0 or result.get("errors"):
        raise BenchmarkError("resultado contiene errores o exit_code no cero")
    for key in ("implementation", "environment", "workload", "metrics"):
        if key not in result:
            raise BenchmarkError(f"resultado incompleto: falta {key}")
    implementation = result["implementation"]
    for key in ("pipeline", "queue_contract", "effective_config", "code_hashes",
                "ocr_runtime"):
        if key not in implementation:
            raise BenchmarkError(f"implementación incompleta: falta {key}")
    if result["workload"].get("ocr") and not implementation[
            "ocr_runtime"].get("operational"):
        raise BenchmarkError("resultado OCR sin backend operativo")
    metrics = result["metrics"]
    for key in (
            "wall_seconds", "cpu_seconds", "cpu_percent_one_core", "ram",
            "processing_latency_ms", "end_to_end_latency_ms", "stages",
            "scheduler", "queue", "coverage", "counters", "closed"):
        if key not in metrics:
            raise BenchmarkError(f"métricas incompletas: falta {key}")
    if not metrics["closed"]:
        raise BenchmarkError("pipeline no terminó cerrado")
    queue = metrics["queue"]
    expected_queue = {
        "mode": "none", "capacity": 0, "max_depth": 0,
        "final_depth": 0, "dropped": 0,
    }
    for key, expected in expected_queue.items():
        if queue.get(key) != expected:
            raise BenchmarkError(f"contrato de cola inválido: {key}")
    if not queue.get("drained") or int(queue.get("max_in_flight", 99)) > 1:
        raise BenchmarkError("pipeline dejó trabajo pendiente o más de un frame")
    scheduler = metrics["scheduler"]
    for key in ("poll_overrun_ms", "wait_ms", "overrun_count"):
        if key not in scheduler:
            raise BenchmarkError(f"scheduler incompleto: falta {key}")
    coverage = metrics["coverage"]
    for key in (
            "expected", "offered", "processed", "dropped", "errors", "ratio",
            "relevant_expected", "relevant_detected", "relevant_missed",
            "relevant_recall", "saved", "duplicates", "detection_latency_ms"):
        if key not in coverage:
            raise BenchmarkError(f"cobertura incompleta: falta {key}")
    if coverage["expected"] < 1 or coverage["processed"] < 1:
        raise BenchmarkError("resultado sin cobertura medible")
    if coverage["expected"] != coverage["offered"]:
        raise BenchmarkError("workload incompleto: expected != offered")
    if coverage["processed"] + coverage["dropped"] != coverage["offered"]:
        raise BenchmarkError("cobertura incoherente: processed+dropped != offered")
    expected_ratio = coverage["processed"] / coverage["expected"]
    if abs(float(coverage["ratio"]) - expected_ratio) > 1e-6:
        raise BenchmarkError("ratio de cobertura incoherente")
    if not 0.0 <= float(coverage["relevant_recall"]) <= 1.0:
        raise BenchmarkError("relevant_recall fuera de rango")
    relevant_expected = int(coverage["relevant_expected"])
    relevant_detected = int(coverage["relevant_detected"])
    if relevant_expected < 1 or not 0 <= relevant_detected <= relevant_expected:
        raise BenchmarkError("cobertura relevante incoherente")
    if len(coverage["relevant_missed"]) != relevant_expected - relevant_detected:
        raise BenchmarkError("pérdidas relevantes incoherentes")
    expected_recall = relevant_detected / relevant_expected
    if abs(float(coverage["relevant_recall"]) - expected_recall) > 1e-6:
        raise BenchmarkError("recall visual incoherente")


def _percent_delta(before: float, after: float) -> float | None:
    if before == 0:
        return None
    return round(100.0 * (after - before) / before, 3)


def compare_results(
        before: dict, after: dict, *,
        max_cpu_regression_pct: float | None = None,
        max_ram_regression_pct: float | None = None,
        max_latency_regression_pct: float | None = None,
        max_coverage_drop: float | None = None,
        max_queue_depth: int | None = None,
        min_relevant_recall: float | None = None) -> dict:
    validate_result(before)
    validate_result(after)
    if before["workload"] != after["workload"]:
        raise BenchmarkError("los workloads before/after no coinciden")
    environment_keys = (
        "system", "release", "python", "cpu_count", "cpu_model",
        "pillow", "psutil", "rapidocr", "onnxruntime", "winrt_runtime",
        "winrt_windows_ocr", "commit",
    )
    if any(
            before["environment"].get(key) != after["environment"].get(key)
            for key in environment_keys):
        raise BenchmarkError("los entornos before/after no son comparables")
    if before["environment"].get("harness_hash") != after["environment"].get(
            "harness_hash"):
        raise BenchmarkError("el hash del harness before/after no coincide")
    bm, am = before["metrics"], after["metrics"]
    deltas = {
        "cpu_seconds_pct": _percent_delta(bm["cpu_seconds"], am["cpu_seconds"]),
        "peak_ram_mb_pct": _percent_delta(
            bm["ram"]["peak_mb"], am["ram"]["peak_mb"]),
        "ram_delta_mb_pct": _percent_delta(
            bm["ram"]["delta_mb"], am["ram"]["delta_mb"]),
        "processing_p95_pct": _percent_delta(
            bm["processing_latency_ms"]["p95"],
            am["processing_latency_ms"]["p95"]),
        "end_to_end_p95_pct": _percent_delta(
            bm["end_to_end_latency_ms"]["p95"],
            am["end_to_end_latency_ms"]["p95"]),
        "queue_max_depth_delta": (
            am["queue"]["max_depth"] - bm["queue"]["max_depth"]),
        "coverage_ratio_delta": round(
            am["coverage"]["ratio"] - bm["coverage"]["ratio"], 6),
        "relevant_recall_delta": round(
            am["coverage"]["relevant_recall"]
            - bm["coverage"]["relevant_recall"], 6),
        "saved_frames_pct": _percent_delta(
            bm["coverage"]["saved"], am["coverage"]["saved"]),
        "ocr_runs_pct": _percent_delta(
            bm["counters"].get("ocr_runs", 0),
            am["counters"].get("ocr_runs", 0)),
    }
    thresholds = {
        key: value for key, value in {
            "max_cpu_regression_pct": max_cpu_regression_pct,
            "max_ram_regression_pct": max_ram_regression_pct,
            "max_latency_regression_pct": max_latency_regression_pct,
            "max_coverage_drop": max_coverage_drop,
            "max_queue_depth": max_queue_depth,
            "min_relevant_recall": min_relevant_recall,
        }.items() if value is not None
    }
    violations: list[str] = []
    if max_cpu_regression_pct is not None:
        value = deltas["cpu_seconds_pct"]
        if value is None or value > max_cpu_regression_pct:
            violations.append("CPU excede la tolerancia específica")
    if max_ram_regression_pct is not None:
        value = deltas["peak_ram_mb_pct"]
        if value is None or value > max_ram_regression_pct:
            violations.append("RAM excede la tolerancia específica")
    if max_latency_regression_pct is not None:
        values = (deltas["processing_p95_pct"], deltas["end_to_end_p95_pct"])
        if any(value is None or value > max_latency_regression_pct for value in values):
            violations.append("latencia p95 excede la tolerancia específica")
    if max_coverage_drop is not None:
        if -deltas["relevant_recall_delta"] > max_coverage_drop:
            violations.append("cobertura relevante cae más que la tolerancia")
    if max_queue_depth is not None and am["queue"]["max_depth"] > max_queue_depth:
        violations.append("profundidad de cola excede la tolerancia")
    if min_relevant_recall is not None:
        if am["coverage"]["relevant_recall"] < min_relevant_recall:
            violations.append("recall visual relevante menor al mínimo específico")
    return {
        "schema_version": SCHEMA_VERSION,
        "comparison": "captura_visual_before_after",
        "workload": before["workload"],
        "environment": after["environment"],
        "before_implementation": before["implementation"],
        "after_implementation": after["implementation"],
        "before_metrics": bm,
        "after_metrics": am,
        "deltas": deltas,
        "thresholds": thresholds,
        "violations": violations,
        "passed": not violations,
    }


def _safe_output(path: Path) -> Path:
    resolved = path.resolve()
    root = ROOT.resolve()
    for name in ("contenido", "logs", "backups"):
        protected = (root / name).resolve()
        try:
            resolved.relative_to(protected)
        except ValueError:
            continue
        raise BenchmarkError(f"salida rechazada en ruta protegida: {name}/**")
    if resolved == (root / ".env").resolve():
        raise BenchmarkError("salida rechazada en .env")
    return resolved


def _write_json(path: Path, payload: dict) -> None:
    destination = _safe_output(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"no se pudo leer {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkError(f"{path} no contiene un objeto JSON")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="ejecuta benchmark sintético o local")
    run.add_argument("--mode", choices=("synthetic", "local"), required=True)
    run.add_argument("--pipeline", choices=("legacy", "adaptive"),
                     default="adaptive")
    run.add_argument("--scenario", choices=(
        "stable", "mixed", "frequent", "silent", "builds"),
                     default="mixed")
    run.add_argument("--frames", type=int, default=0)
    run.add_argument("--width", type=int, default=1280)
    run.add_argument("--height", type=int, default=720)
    run.add_argument("--frame-interval-ms", type=float, default=50.0)
    run.add_argument("--logical-interval-ms", type=float, default=2000.0)
    run.add_argument("--queue-capacity", type=int, default=0)
    run.add_argument("--expected-count", type=int)
    run.add_argument("--input-dir", type=Path)
    run.add_argument("--ocr", action="store_true")
    run.add_argument("--no-save-jpeg", action="store_true")
    run.add_argument("--out", type=Path, required=True)
    compare = subparsers.add_parser("compare", help="compara resultados compatibles")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    compare.add_argument("--out", type=Path, required=True)
    compare.add_argument("--max-cpu-regression-pct", type=float)
    compare.add_argument("--max-ram-regression-pct", type=float)
    compare.add_argument("--max-latency-regression-pct", type=float)
    compare.add_argument("--max-coverage-drop", type=float)
    compare.add_argument("--max-queue-depth", type=int)
    compare.add_argument("--min-relevant-recall", type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            frames = args.frames
            if args.mode == "synthetic" and frames == 0:
                frames = 120
            payload = run_benchmark(BenchmarkConfig(
                mode=args.mode,
                frames=frames,
                width=args.width,
                height=args.height,
                frame_interval_ms=args.frame_interval_ms,
                logical_interval_ms=args.logical_interval_ms,
                queue_capacity=args.queue_capacity,
                expected_count=args.expected_count,
                input_dir=args.input_dir,
                use_ocr=args.ocr,
                save_jpeg=not args.no_save_jpeg,
                pipeline=args.pipeline,
                scenario=args.scenario,
            ))
            _write_json(args.out, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        before = _load_json(args.before)
        after = _load_json(args.after)
        payload = compare_results(
            before, after,
            max_cpu_regression_pct=args.max_cpu_regression_pct,
            max_ram_regression_pct=args.max_ram_regression_pct,
            max_latency_regression_pct=args.max_latency_regression_pct,
            max_coverage_drop=args.max_coverage_drop,
            max_queue_depth=args.max_queue_depth,
            min_relevant_recall=args.min_relevant_recall,
        )
        _write_json(args.out, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["passed"] else 1
    except BenchmarkError as exc:
        print(f"BENCHMARK_INVALID: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
