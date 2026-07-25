from __future__ import annotations

import threading

import pytest
from PIL import Image

from src.visual_runtime import (
    VisualGate,
    VisualRuntimeMetrics,
    build_visual_probe,
    compare_visual_probes,
)


MIB = 1024 * 1024


def _probe(value: int = 96, *, width: int = 64, height: int = 32):
    return build_visual_probe(
        Image.new("L", (width, height), value), width=width)


def _gate_with_initial_probe(*, now: float = 0.0, **kwargs):
    gate = VisualGate(**kwargs)
    probe = _probe()
    decision = gate.decide(probe, now=now)
    assert decision.kind == "initial"
    assert decision.candidate is True
    assert decision.run_ocr is True
    gate.commit(
        decision, probe, now=now, saved=True, ocr_ran=True)
    return gate, probe


def test_probe_reuses_one_grayscale_thumbnail_for_hashes_and_sensitive_delta():
    base = Image.new("L", (64, 32), 100)
    changed = base.copy()
    changed.putpixel((31, 15), 180)

    previous = build_visual_probe(base, width=64)
    current = build_visual_probe(changed, width=64)
    unchanged = build_visual_probe(base, width=64)

    assert previous.thumbnail.mode == "L"
    assert previous.thumbnail.size == (64, 32)
    assert previous.source_size == base.size
    assert previous.coarse_hash.bit_length() <= 64
    assert previous.detail_hash.bit_length() <= 256
    assert previous.digest != current.digest

    stable_delta = compare_visual_probes(previous, unchanged)
    assert stable_delta.coarse_distance == 0
    assert stable_delta.detail_distance == 0
    assert stable_delta.changed_ratio == 0.0
    assert stable_delta.mean_delta == 0.0
    assert stable_delta.max_delta == 0

    delta = compare_visual_probes(previous, current)
    assert delta.changed_ratio == pytest.approx(1 / (64 * 32))
    assert delta.mean_delta == pytest.approx(80 / (64 * 32))
    assert delta.max_delta == 80


def test_stable_fast_path_and_spaced_heartbeat_do_not_create_candidate():
    gate, accepted = _gate_with_initial_probe(
        heartbeat_interval_s=5.0,
        ocr_min_interval_s=20.0,
        ocr_refresh_s=30.0,
    )

    stable = gate.decide(_probe(), now=1.0)
    assert stable.kind == "stable"
    assert stable.candidate is False
    assert stable.run_ocr is False
    assert stable.emit_heartbeat is False

    heartbeat = gate.decide(_probe(), now=5.0)
    assert heartbeat.kind == "stable"
    assert heartbeat.candidate is False
    assert heartbeat.emit_heartbeat is True
    gate.commit(heartbeat, accepted, now=5.0, heartbeat=True)

    spaced = gate.decide(_probe(), now=6.0)
    assert spaced.kind == "stable"
    assert spaced.emit_heartbeat is False


def test_major_change_remains_immediately_persistable_when_ocr_is_rate_limited():
    gate, _accepted = _gate_with_initial_probe(
        ocr_min_interval_s=60.0,
        ocr_refresh_s=120.0,
    )
    major_probe = _probe(230)

    decision = gate.decide(major_probe, now=1.0)

    assert decision.kind == "major_change"
    assert decision.candidate is True
    assert decision.run_ocr is False
    assert decision.emit_heartbeat is False
    assert decision.delta is not None
    assert decision.delta.changed_ratio == 1.0

    gate.commit(decision, major_probe, now=1.0, saved=True, ocr_ran=False)
    assert gate.accepted_probe is major_probe
    assert gate.last_saved_at == 1.0
    assert gate.last_ocr_at == 0.0


def test_isoluminant_color_change_is_relevant_when_gray_hashes_match():
    red = build_visual_probe(Image.new("RGB", (320, 180), (255, 0, 0)))
    green = build_visual_probe(Image.new("RGB", (320, 180), (0, 130, 0)))
    assert red.thumbnail.tobytes() == green.thumbnail.tobytes()
    delta = compare_visual_probes(red, green)
    assert delta.coarse_distance == 0
    assert delta.detail_distance == 0
    assert delta.changed_ratio == 0
    assert delta.color_changed_ratio == 1.0

    gate = VisualGate(ocr_min_interval_s=60.0, ocr_refresh_s=120.0)
    initial = gate.decide(red, now=0.0)
    gate.commit(initial, red, now=0.0, saved=True, ocr_ran=True)
    decision = gate.decide(green, now=2.0)

    assert decision.kind == "major_change"
    assert decision.candidate is True
    assert decision.run_ocr is False


def test_subtle_pixel_change_is_candidate_without_relying_on_perceptual_hash_identity():
    gate, _accepted = _gate_with_initial_probe(
        coarse_threshold=64,
        detail_threshold=256,
        pixel_delta=12,
        change_ratio=0.001,
        major_ratio=0.20,
        mean_delta=0.05,
        subtle_interval_s=2.0,
        ocr_min_interval_s=10.0,
        ocr_refresh_s=60.0,
    )
    changed = Image.new("L", (64, 32), 96)
    for x, y in ((30, 15), (31, 15), (30, 16), (31, 16)):
        changed.putpixel((x, y), 196)
    subtle_probe = build_visual_probe(changed, width=64)

    decision = gate.decide(subtle_probe, now=2.0)

    assert decision.kind == "subtle_change"
    assert decision.candidate is True
    assert decision.run_ocr is False
    assert decision.delta is not None
    assert 0.001 <= decision.delta.changed_ratio < 0.20
    assert decision.delta.mean_delta < 4.0
    assert decision.delta.max_delta == 100


def test_localized_build_survives_global_mean_dilution_without_cursor_noise():
    """Una fórmula corta no debe esperar al siguiente build ni al OCR periódico."""
    base = Image.new("L", (320, 180), 100)
    gate = VisualGate(
        coarse_threshold=64,
        detail_threshold=256,
        pixel_delta=12,
        change_ratio=0.001,
        major_ratio=0.20,
        mean_delta=0.35,
        subtle_interval_s=2.0,
        ocr_min_interval_s=60.0,
        ocr_refresh_s=120.0,
    )
    initial_probe = build_visual_probe(base, width=320)
    initial = gate.decide(initial_probe, now=0.0)
    gate.commit(initial, initial_probe, now=0.0, saved=True, ocr_ran=True)

    build = base.copy()
    for x in range(101, 109):
        for y in range(91, 101):
            build.putpixel((x, y), 200)
    build_probe = build_visual_probe(build, width=320)
    decision = gate.decide(build_probe, now=2.0)

    assert decision.delta is not None
    assert decision.delta.changed_ratio == pytest.approx(80 / (320 * 180))
    assert decision.delta.mean_delta < gate.mean_delta
    assert decision.kind == "subtle_change"
    assert decision.candidate is True

    gate.commit(decision, build_probe, now=2.0, saved=True)
    cursor = build.copy()
    for x in range(250, 253):
        for y in range(150, 155):
            cursor.putpixel((x, y), 20)
    cursor_decision = gate.decide(
        build_visual_probe(cursor, width=320), now=4.0)

    assert cursor_decision.delta is not None
    assert cursor_decision.delta.changed_ratio < gate.change_ratio
    assert cursor_decision.kind == "stable"
    assert cursor_decision.candidate is False


def test_semantic_refresh_requests_ocr_without_creating_a_jpeg_candidate():
    gate, _accepted = _gate_with_initial_probe(
        heartbeat_interval_s=100.0,
        ocr_min_interval_s=2.0,
        ocr_refresh_s=5.0,
    )

    before_refresh = gate.decide(_probe(), now=4.9)
    refresh = gate.decide(_probe(), now=5.0)

    assert before_refresh.kind == "stable"
    assert before_refresh.run_ocr is False
    assert refresh.kind == "semantic_refresh"
    assert refresh.candidate is False
    assert refresh.run_ocr is True
    assert refresh.emit_heartbeat is False


def test_rejected_candidate_does_not_advance_accepted_reference():
    gate, accepted = _gate_with_initial_probe(
        ocr_min_interval_s=30.0,
        ocr_refresh_s=60.0,
    )
    rejected_probe = _probe(230)
    rejected = gate.decide(rejected_probe, now=1.0)
    assert rejected.kind == "major_change"

    gate.commit(
        rejected, rejected_probe, now=1.0,
        saved=False, heartbeat=False, ocr_ran=False,
        semantic_confirmed=False,
    )

    assert gate.accepted_probe is accepted
    assert gate.last_saved_at == 0.0
    repeated = gate.decide(rejected_probe, now=2.0)
    assert repeated.kind == "major_change"
    assert repeated.candidate is True


def test_metrics_bound_samples_measure_wall_and_cpu_and_create_no_threads(
        monkeypatch):
    def forbidden_thread(*_args, **_kwargs):
        raise AssertionError("VisualRuntimeMetrics no debe crear threads")

    monkeypatch.setattr(threading, "Thread", forbidden_thread)
    metrics = VisualRuntimeMetrics(max_samples=16)
    metrics._memory_source = "test_rss"
    metrics._baseline_rss = 100 * MIB
    metrics._peak_rss = 100 * MIB
    metrics._memory_reader = lambda: 125 * MIB

    for _ in range(40):
        with metrics.frame():
            assert metrics.in_flight == 1
            with metrics.stage("probe"):
                pass
    metrics.increment("stable_frames", 40)
    metrics.set_gauge("mode", "stable")
    metrics.record_overrun(0.002)
    metrics.close()

    snapshot = metrics.snapshot()
    assert snapshot["stages"]["probe"]["wall_ms"]["count"] == 16
    assert snapshot["stages"]["probe"]["cpu_ms"]["count"] == 16
    assert snapshot["iteration_latency_ms"]["count"] == 16
    assert snapshot["stages"]["probe"]["wall_ms"]["total"] >= 0
    assert snapshot["stages"]["probe"]["cpu_ms"]["total"] >= 0
    assert snapshot["counters"]["frames_started"] == 40
    assert snapshot["counters"]["frames_finished"] == 40
    assert snapshot["counters"]["stable_frames"] == 40
    assert snapshot["counters"]["poll_overruns"] == 1
    assert snapshot["poll_overrun_ms"]["total"] == pytest.approx(2.0)
    assert snapshot["ram"] == {
        "source": "test_rss",
        "baseline_mb": 100.0,
        "peak_mb": 125.0,
        "delta_mb": 25.0,
    }
    assert snapshot["queue"] == {
        "mode": "none",
        "capacity": 0,
        "max_depth": 0,
        "final_depth": 0,
        "dropped": 0,
        "drained": True,
        "max_in_flight": 1,
    }
    assert snapshot["closed"] is True


def test_metrics_reject_nested_frames_to_keep_single_in_flight_contract():
    metrics = VisualRuntimeMetrics()

    with metrics.frame():
        with pytest.raises(RuntimeError, match="frame|in.flight|vuelo"):
            with metrics.frame():
                pass

    snapshot = metrics.snapshot()
    assert snapshot["queue"]["mode"] == "none"
    assert snapshot["queue"]["capacity"] == 0
    assert snapshot["queue"]["max_in_flight"] <= 1
    assert snapshot["queue"]["drained"] is True
