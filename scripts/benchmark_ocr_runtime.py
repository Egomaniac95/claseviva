"""Benchmark reproducible de backends OCR sobre un corpus sintético fijo.

Incluye inicialización perezosa, conversión desde PIL e inferencia. No captura el
monitor, no usa evidencia real y debe ejecutarse en un proceso nuevo por opción.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, ocr  # noqa: E402


SCHEMA_VERSION = 1


class BenchmarkError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrBenchmarkConfig:
    engine: str
    rapid_intra_threads: int = 2
    rapid_inter_threads: int = 1
    repeat: int = 1
    width: int = 1280
    height: int = 720


@dataclass(frozen=True)
class CorpusItem:
    label: str
    image: Image.Image
    expected_tokens: frozenset[str]


def _font(size: int, *, bold: bool = False):
    candidates = [
        Path(r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
             else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size), str(path)
    return ImageFont.load_default(size=size), "Pillow-default"


def _corpus(width: int, height: int) -> tuple[list[CorpusItem], str]:
    normal, normal_name = _font(max(18, round(height * 0.047)))
    small, _ = _font(max(16, round(height * 0.039)))
    bold, bold_name = _font(max(22, round(height * 0.061)), bold=True)
    specs = [
        ("formula", "white", "Descenso de gradiente", [
            "Algoritmo de optimizacion", "w = w - eta * gradiente",
            "learning rate = 0.01",
        ], {"descenso", "gradiente", "algoritmo", "optimizacion", "learning", "rate"}),
        ("diagrama", "white", "Red neuronal convolucional", [
            "Entrada -> Conv2D -> ReLU -> Pooling", "precision = 94.7%",
            "epoca 12 de 20",
        ], {"red", "neuronal", "convolucional", "entrada", "relu", "pooling", "precision"}),
        ("codigo_oscuro", (24, 27, 35), "Codigo Python", [
            "def entrenar(modelo, datos):", "    loss = modelo(datos)",
            "    return loss.item()",
        ], {"codigo", "python", "entrenar", "modelo", "datos", "loss", "return", "item"}),
        ("tabla", "white", "Tabla de resultados", [
            "Modelo A    Accuracy 0.91", "Modelo B    Accuracy 0.94",
            "Seleccion: Modelo B",
        ], {"tabla", "resultados", "modelo", "accuracy", "seleccion"}),
    ]
    items = []
    for label, background, title, lines, expected in specs:
        image = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(image)
        foreground = "white" if background != "white" else (18, 23, 35)
        accent = (64, 170, 255) if background != "white" else (25, 90, 170)
        header = max(70, round(height * 0.146))
        draw.rectangle((0, 0, width, header), fill=accent)
        draw.text((round(width * 0.042), round(height * 0.035)), title,
                  font=bold, fill="white")
        y = round(height * 0.243)
        for line in lines:
            draw.text((round(width * 0.059), y), line,
                      font=small if line.startswith("    ") else normal,
                      fill=foreground)
            y += round(height * 0.139)
        items.append(CorpusItem(label, image, frozenset(expected)))
    return items, f"{normal_name}|{bold_name}"


def _tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", text or "")
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char))
    return set(re.findall(r"[a-z0-9_]{3,}", normalized.lower()))


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _fingerprint(items: Sequence[CorpusItem]) -> str:
    digest = hashlib.sha256()
    for item in items:
        digest.update(item.label.encode("utf-8") + b"\0")
        digest.update(item.image.tobytes())
        digest.update("\n".join(sorted(item.expected_tokens)).encode("utf-8"))
    return digest.hexdigest()


def run_benchmark(
        benchmark: OcrBenchmarkConfig,
        recognize: Callable[[Image.Image], str] | None = None) -> dict:
    if benchmark.engine not in {"auto", "windows", "rapidocr"}:
        raise BenchmarkError("engine debe ser auto, windows o rapidocr")
    if benchmark.repeat < 1 or benchmark.width < 320 or benchmark.height < 180:
        raise BenchmarkError("repeat y dimensiones deben ser positivos")
    if benchmark.rapid_intra_threads < 0 or benchmark.rapid_inter_threads < 0:
        raise BenchmarkError("los hilos RapidOCR no pueden ser negativos")

    previous = (
        config.OCR_ENGINE,
        config.OCR_RAPID_INTRA_THREADS,
        config.OCR_RAPID_INTER_THREADS,
        config.LOGS_DIR,
        ocr._motor,
        dict(ocr._motor_info),
    )
    temporary = tempfile.TemporaryDirectory(prefix="clase-viva-ocr-bench-")
    config.OCR_ENGINE = benchmark.engine
    config.OCR_RAPID_INTRA_THREADS = benchmark.rapid_intra_threads
    config.OCR_RAPID_INTER_THREADS = benchmark.rapid_inter_threads
    config.LOGS_DIR = Path(temporary.name) / "logs"
    ocr._motor = None
    ocr._motor_info = {
        "requested": benchmark.engine,
        "engine": "uninitialized",
        "operational": None,
    }
    recognize = recognize or ocr.texto_de_pil
    items, font = _corpus(benchmark.width, benchmark.height)
    try:
        import psutil
        process = psutil.Process()
        baseline_rss = int(process.memory_info().rss)
        peak_rss = baseline_rss
    except Exception:
        process = None
        baseline_rss = peak_rss = 0

    calls = []
    errors = []
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    for iteration in range(benchmark.repeat):
        for item in items:
            call_wall = time.perf_counter()
            call_cpu = time.process_time()
            try:
                text = str(recognize(item.image) or "")
            except Exception as exc:
                text = ""
                errors.append(f"{item.label}:{type(exc).__name__}:{exc}")
            wall_ms = (time.perf_counter() - call_wall) * 1000.0
            cpu_ms = (time.process_time() - call_cpu) * 1000.0
            if process is not None:
                peak_rss = max(peak_rss, int(process.memory_info().rss))
            observed = _tokens(text)
            recall = len(observed & item.expected_tokens) / len(item.expected_tokens)
            calls.append({
                "iteration": iteration,
                "label": item.label,
                "wall_ms": round(wall_ms, 3),
                "cpu_ms": round(cpu_ms, 3),
                "token_recall": round(recall, 6),
                "recognized_text": text,
            })
            if not text.strip():
                errors.append(f"{item.label}:empty_result")
    wall_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started
    runtime = ocr.estado_motor()
    if recognize is not ocr.texto_de_pil and runtime.get("operational") is None:
        runtime = {
            "requested": benchmark.engine,
            "engine": "injected_test_reader",
            "operational": True,
        }
    recalls = [call["token_recall"] for call in calls]
    result = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "ocr_runtime",
        "implementation": {
            "requested_engine": benchmark.engine,
            "rapid_intra_threads": benchmark.rapid_intra_threads or "auto",
            "rapid_inter_threads": benchmark.rapid_inter_threads or "auto",
            "runtime": runtime,
        },
        "environment": {
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "cpu_count": __import__("os").cpu_count(),
            "pillow": _version("Pillow"),
            "rapidocr": _version("rapidocr-onnxruntime"),
            "onnxruntime": _version("onnxruntime"),
            "winrt_runtime": _version("winrt-runtime"),
            "winrt_windows_ocr": _version("winrt-Windows.Media.Ocr"),
            "font": font,
        },
        "workload": {
            "corpus": "technical-slides-v1",
            "fingerprint": _fingerprint(items),
            "width": benchmark.width,
            "height": benchmark.height,
            "items": len(items),
            "repeat": benchmark.repeat,
            "calls": len(calls),
        },
        "metrics": {
            "wall_seconds": round(wall_seconds, 6),
            "cpu_seconds": round(cpu_seconds, 6),
            "cpu_percent_one_core": round(
                100.0 * cpu_seconds / max(wall_seconds, 1e-9), 3),
            "peak_rss_mb": round(peak_rss / (1024 * 1024), 3),
            "rss_delta_mb": round(
                max(0, peak_rss - baseline_rss) / (1024 * 1024), 3),
            "token_recall_mean": round(sum(recalls) / len(recalls), 6),
            "token_recall_min": round(min(recalls), 6),
            "calls": calls,
        },
        "errors": errors,
        "exit_code": 0 if not errors else 2,
    }
    try:
        validate_result(result)
    finally:
        (
            config.OCR_ENGINE,
            config.OCR_RAPID_INTRA_THREADS,
            config.OCR_RAPID_INTER_THREADS,
            config.LOGS_DIR,
            ocr._motor,
            previous_info,
        ) = previous
        ocr._motor_info = previous_info
        temporary.cleanup()
    return result


def validate_result(result: dict) -> None:
    if result.get("schema_version") != SCHEMA_VERSION:
        raise BenchmarkError("schema OCR ausente o incompatible")
    if result.get("benchmark") != "ocr_runtime":
        raise BenchmarkError("resultado no es ocr_runtime")
    if result.get("errors") or result.get("exit_code") != 0:
        raise BenchmarkError("OCR produjo errores o resultados vacíos")
    for key in ("implementation", "environment", "workload", "metrics"):
        if key not in result:
            raise BenchmarkError(f"resultado incompleto: falta {key}")
    if not result["implementation"]["runtime"].get("operational"):
        raise BenchmarkError("backend OCR no operativo")
    metrics = result["metrics"]
    if not metrics.get("calls") or not 0 <= metrics["token_recall_min"] <= 1:
        raise BenchmarkError("métricas OCR incompletas")


def _safe_output(path: Path) -> Path:
    resolved = path.resolve()
    for name in ("contenido", "logs", "backups"):
        protected = (ROOT / name).resolve()
        try:
            resolved.relative_to(protected)
        except ValueError:
            continue
        raise BenchmarkError(f"salida rechazada en ruta protegida: {name}/**")
    if resolved == (ROOT / ".env").resolve():
        raise BenchmarkError("salida rechazada en .env")
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("auto", "windows", "rapidocr"),
                        required=True)
    parser.add_argument("--rapid-intra-threads", type=int, default=2)
    parser.add_argument("--rapid-inter-threads", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_benchmark(OcrBenchmarkConfig(
            engine=args.engine,
            rapid_intra_threads=args.rapid_intra_threads,
            rapid_inter_threads=args.rapid_inter_threads,
            repeat=args.repeat,
            width=args.width,
            height=args.height,
        ))
        output = _safe_output(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(json.dumps({
            "engine": result["implementation"]["runtime"].get("engine"),
            **{key: result["metrics"][key] for key in (
                "wall_seconds", "cpu_seconds", "peak_rss_mb", "rss_delta_mb",
                "token_recall_mean", "token_recall_min")},
        }, ensure_ascii=False))
        return 0
    except BenchmarkError as exc:
        print(f"BENCHMARK_INVALID: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
