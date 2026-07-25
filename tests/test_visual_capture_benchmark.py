from __future__ import annotations

import copy
from pathlib import Path

import pytest
from PIL import Image

from scripts.benchmark_captura_visual import (
    BenchmarkConfig,
    BenchmarkError,
    ROOT,
    _safe_output,
    compare_results,
    main,
    run_benchmark,
    validate_result,
)


def _synthetic_result(*, pipeline: str = "adaptive", scenario: str = "mixed",
                      frames: int = 8) -> dict:
    return run_benchmark(BenchmarkConfig(
        mode="synthetic",
        frames=frames,
        width=160,
        height=90,
        frame_interval_ms=0.0,
        queue_capacity=0,
        save_jpeg=False,
        pipeline=pipeline,
        scenario=scenario,
    ))


def test_synthetic_benchmark_reports_real_pipeline_metrics() -> None:
    result = _synthetic_result()

    validate_result(result)
    metrics = result["metrics"]
    assert metrics["coverage"]["expected"] == 8
    assert metrics["coverage"]["processed"] == 8
    assert metrics["coverage"]["ratio"] == 1.0
    assert metrics["coverage"]["relevant_recall"] == 1.0
    assert metrics["queue"] == {
        "mode": "none",
        "capacity": 0,
        "max_depth": 0,
        "final_depth": 0,
        "dropped": 0,
        "drained": True,
        "max_in_flight": 1,
    }
    assert metrics["closed"] is True
    assert "wait_ms" in metrics["scheduler"]
    assert metrics["cpu_seconds"] >= 0
    assert metrics["ram"]["peak_mb"] >= metrics["ram"]["baseline_mb"]
    assert {"acquisition", "probe", "hash_compare"} <= set(metrics["stages"])
    for group in ("processing_latency_ms", "end_to_end_latency_ms"):
        assert set(metrics[group]) == {"p50", "p95", "max"}


def test_stable_ab_uses_same_workload_and_adaptive_avoids_duplicate_evidence() -> None:
    before = _synthetic_result(pipeline="legacy", scenario="stable", frames=12)
    after = _synthetic_result(pipeline="adaptive", scenario="stable", frames=12)

    comparison = compare_results(before, after, min_relevant_recall=1.0)

    assert before["workload"] == after["workload"]
    assert before["metrics"]["coverage"]["saved"] == 12
    assert after["metrics"]["coverage"]["saved"] == 1
    assert comparison["passed"] is True
    assert comparison["deltas"]["saved_frames_pct"] < 0


def test_frequent_changes_keep_recall_without_queue() -> None:
    result = _synthetic_result(
        pipeline="adaptive", scenario="frequent", frames=12)

    assert result["metrics"]["coverage"]["relevant_recall"] == 1.0
    assert result["metrics"]["coverage"]["saved"] == 12
    assert result["metrics"]["queue"]["max_in_flight"] == 1


def test_incremental_builds_keep_each_relevant_state_without_cursor_duplicates() -> None:
    result = _synthetic_result(
        pipeline="adaptive", scenario="builds", frames=18)

    coverage = result["metrics"]["coverage"]
    assert coverage["relevant_expected"] == 6
    assert coverage["relevant_detected"] == 6
    assert coverage["relevant_recall"] == 1.0
    assert coverage["saved"] == 6
    assert coverage["duplicates"] == 0


def test_compare_has_no_implicit_universal_threshold() -> None:
    before = _synthetic_result()
    after = copy.deepcopy(before)
    before["metrics"]["cpu_seconds"] = 1.0
    after["metrics"]["cpu_seconds"] = 1.5

    comparison = compare_results(before, after)

    assert comparison["thresholds"] == {}
    assert comparison["violations"] == []
    assert comparison["passed"] is True
    assert comparison["deltas"]["cpu_seconds_pct"] > 0


def test_compare_applies_only_explicit_task_thresholds() -> None:
    before = _synthetic_result()
    after = copy.deepcopy(before)
    coverage = after["metrics"]["coverage"]
    coverage["relevant_detected"] = coverage["relevant_expected"] - 1
    coverage["relevant_missed"] = ["slide-missed"]
    coverage["relevant_recall"] = (
        coverage["relevant_detected"] / coverage["relevant_expected"])

    comparison = compare_results(before, after, max_coverage_drop=0.05)

    assert comparison["passed"] is False
    assert comparison["thresholds"] == {"max_coverage_drop": 0.05}
    assert comparison["violations"]


def test_invalid_or_empty_local_workload_returns_nonzero(tmp_path) -> None:
    output = tmp_path / "result.json"

    exit_code = main([
        "run",
        "--mode", "local",
        "--input-dir", str(tmp_path),
        "--out", str(output),
    ])

    assert exit_code != 0
    assert not output.exists()


def test_queue_capacity_nonzero_is_rejected_because_runtime_has_no_queue(
        tmp_path) -> None:
    output = tmp_path / "result.json"

    exit_code = main([
        "run", "--mode", "synthetic", "--frames", "2",
        "--queue-capacity", "1", "--out", str(output),
    ])

    assert exit_code != 0
    assert not output.exists()


def test_expected_count_mismatch_is_invalid_instead_of_hiding_loss(tmp_path) -> None:
    output = tmp_path / "result.json"

    exit_code = main([
        "run", "--mode", "synthetic", "--frames", "2",
        "--expected-count", "3", "--out", str(output),
    ])

    assert exit_code != 0
    assert not output.exists()


def test_protected_outputs_are_anchored_to_repo_even_from_another_cwd(
        monkeypatch) -> None:
    monkeypatch.chdir(ROOT / "scripts")

    with pytest.raises(BenchmarkError, match="protegida"):
        _safe_output(Path("..") / "contenido" / "benchmark-prohibido.json")
    with pytest.raises(BenchmarkError, match=r"\.env"):
        _safe_output(Path("..") / ".env")


def test_local_fingerprint_uses_content_not_only_name_and_size(tmp_path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = corpus / "slide.png"
    Image.new("RGB", (32, 32), "red").save(path)
    config = BenchmarkConfig(
        mode="local", frames=0, width=32, height=32,
        frame_interval_ms=0.0, input_dir=corpus, save_jpeg=False)
    first = run_benchmark(config)["workload"]["source_fingerprint"]
    Image.new("RGB", (32, 32), "blue").save(path)
    second = run_benchmark(config)["workload"]["source_fingerprint"]

    assert first != second


def test_incomplete_result_is_rejected() -> None:
    result = _synthetic_result()
    del result["metrics"]["queue"]

    with pytest.raises(BenchmarkError, match="queue|cola"):
        validate_result(result)


def test_compare_rejects_dependency_or_harness_drift_but_allows_implementation() -> None:
    before = _synthetic_result()
    after = copy.deepcopy(before)
    after["environment"]["rapidocr"] = "different"

    with pytest.raises(BenchmarkError, match="entornos"):
        compare_results(before, after)

    after = copy.deepcopy(before)
    after["environment"]["harness_hash"] = "different"
    with pytest.raises(BenchmarkError, match="harness"):
        compare_results(before, after)

    after = copy.deepcopy(before)
    after["implementation"]["code_hashes"]["capture"] = "different"
    after["implementation"]["effective_config"]["probe_width"] = 480
    comparison = compare_results(before, after)
    assert comparison["passed"] is True
    assert comparison["before_implementation"] != comparison[
        "after_implementation"]
