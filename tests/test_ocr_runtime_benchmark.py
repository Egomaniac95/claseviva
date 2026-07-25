from __future__ import annotations

import pytest

from scripts.benchmark_ocr_runtime import (
    BenchmarkError,
    OcrBenchmarkConfig,
    _safe_output,
    run_benchmark,
    validate_result,
)


def test_ocr_benchmark_reports_cpu_ram_latency_and_quality(monkeypatch) -> None:
    text = (
        "descenso gradiente algoritmo optimizacion learning rate red neuronal "
        "convolucional entrada relu pooling precision codigo python entrenar "
        "modelo datos loss return item tabla resultados accuracy seleccion"
    )
    result = run_benchmark(
        OcrBenchmarkConfig(engine="rapidocr", width=640, height=360),
        recognize=lambda _image: text,
    )

    validate_result(result)
    assert result["workload"]["calls"] == 4
    assert result["metrics"]["token_recall_mean"] == 1.0
    assert result["metrics"]["token_recall_min"] == 1.0
    assert result["metrics"]["cpu_seconds"] >= 0
    assert result["metrics"]["peak_rss_mb"] >= 0
    assert all(call["wall_ms"] >= 0 for call in result["metrics"]["calls"])


def test_ocr_benchmark_rejects_empty_results() -> None:
    with pytest.raises(BenchmarkError, match="vacíos|errores"):
        run_benchmark(
            OcrBenchmarkConfig(engine="rapidocr", width=640, height=360),
            recognize=lambda _image: "",
        )


def test_ocr_benchmark_rejects_protected_output() -> None:
    with pytest.raises(BenchmarkError, match="protegida"):
        _safe_output(
            __import__("pathlib").Path("contenido") / "ocr-benchmark.json")
