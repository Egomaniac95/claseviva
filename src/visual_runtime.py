"""Núcleo barato e instrumentado del pipeline visual en vivo.

No captura pantalla, no crea workers y no persiste evidencia. El propietario de la
sesión usa estas primitivas para mantener como máximo un frame en vuelo.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from PIL import Image, ImageChops


def _dhash_gris(img: Image.Image, tam: int) -> int:
    reducida = img.resize((tam + 1, tam), Image.Resampling.BILINEAR)
    pixeles = reducida.tobytes()
    bits = 0
    ancho = tam + 1
    for fila in range(tam):
        inicio = fila * ancho
        for columna in range(tam):
            bits = (bits << 1) | int(
                pixeles[inicio + columna] > pixeles[inicio + columna + 1])
    return bits


@dataclass(frozen=True)
class VisualProbe:
    """Firma visual calculada desde una sola miniatura RGB y su luminancia."""

    thumbnail: Image.Image = field(repr=False, compare=False)
    color_thumbnail: Image.Image = field(repr=False, compare=False)
    coarse_hash: int
    detail_hash: int
    digest: str
    source_size: tuple[int, int]


@dataclass(frozen=True)
class VisualDelta:
    coarse_distance: int
    detail_distance: int
    changed_ratio: float
    mean_delta: float
    max_delta: int
    color_changed_ratio: float
    color_mean_delta: float
    color_max_delta: int


@dataclass(frozen=True)
class VisualDecision:
    kind: str
    reason: str
    candidate: bool
    run_ocr: bool
    emit_heartbeat: bool
    delta: VisualDelta | None = None


def build_visual_probe(img: Image.Image, *, width: int = 320) -> VisualProbe:
    """Reduce una vez y conserva señal de luminancia y crominancia."""
    width = max(32, int(width))
    height = max(16, round(img.height * width / max(1, img.width)))
    if img.size == (width, height) and img.mode == "RGB":
        color_thumbnail = img.copy()
    else:
        color_thumbnail = img.resize(
            (width, height), Image.Resampling.BILINEAR).convert("RGB")
    thumbnail = color_thumbnail.convert("L")
    raw = color_thumbnail.tobytes()
    return VisualProbe(
        thumbnail=thumbnail,
        color_thumbnail=color_thumbnail,
        coarse_hash=_dhash_gris(thumbnail, 8),
        detail_hash=_dhash_gris(thumbnail, 16),
        digest=hashlib.blake2b(raw, digest_size=16).hexdigest(),
        source_size=tuple(img.size),
    )


def compare_visual_probes(
        previous: VisualProbe, current: VisualProbe, *, pixel_delta: int = 12,
        ) -> VisualDelta:
    """Compara señal estructural y píxel a píxel sobre miniaturas homogéneas."""
    anterior = previous.thumbnail
    actual = current.thumbnail
    if anterior.size != actual.size:
        actual = actual.resize(anterior.size, Image.Resampling.BILINEAR)
    diferencia = ImageChops.difference(anterior, actual)
    histograma = diferencia.histogram()
    total = max(1, sum(histograma))
    umbral = max(0, min(254, int(pixel_delta)))
    cambiados = sum(histograma[umbral + 1:])
    suma = sum(valor * cantidad for valor, cantidad in enumerate(histograma))
    maximo = next(
        (valor for valor in range(255, -1, -1) if histograma[valor]), 0)
    color_anterior = previous.color_thumbnail
    color_actual = current.color_thumbnail
    if color_anterior.size != color_actual.size:
        color_actual = color_actual.resize(
            color_anterior.size, Image.Resampling.BILINEAR)
    canales = ImageChops.difference(color_anterior, color_actual).split()
    color_maximo = ImageChops.lighter(
        ImageChops.lighter(canales[0], canales[1]), canales[2])
    histograma_color = color_maximo.histogram()
    total_color = max(1, sum(histograma_color))
    cambiados_color = sum(histograma_color[umbral + 1:])
    suma_color = sum(
        valor * cantidad for valor, cantidad in enumerate(histograma_color))
    maximo_color = next(
        (valor for valor in range(255, -1, -1) if histograma_color[valor]), 0)
    return VisualDelta(
        coarse_distance=(previous.coarse_hash ^ current.coarse_hash).bit_count(),
        detail_distance=(previous.detail_hash ^ current.detail_hash).bit_count(),
        changed_ratio=cambiados / total,
        mean_delta=suma / total,
        max_delta=maximo,
        color_changed_ratio=cambiados_color / total_color,
        color_mean_delta=suma_color / total_color,
        color_max_delta=maximo_color,
    )


class VisualGate:
    """Decide el fast path sin usar un hash perceptual como identidad semántica."""

    def __init__(
            self, *, coarse_threshold: int = 6, detail_threshold: int = 18,
            pixel_delta: int = 12, change_ratio: float = 0.001,
            major_ratio: float = 0.08, mean_delta: float = 0.35,
            ocr_min_interval_s: float = 6.0, ocr_refresh_s: float = 30.0,
            subtle_interval_s: float = 2.0, heartbeat_interval_s: float = 10.0,
            ) -> None:
        self.coarse_threshold = max(0, int(coarse_threshold))
        self.detail_threshold = max(0, int(detail_threshold))
        self.pixel_delta = max(1, int(pixel_delta))
        self.change_ratio = max(0.0, float(change_ratio))
        self.major_ratio = max(self.change_ratio, float(major_ratio))
        self.mean_delta = max(0.0, float(mean_delta))
        self.ocr_min_interval_s = max(0.0, float(ocr_min_interval_s))
        self.ocr_refresh_s = max(
            self.ocr_min_interval_s, float(ocr_refresh_s))
        self.subtle_interval_s = max(0.0, float(subtle_interval_s))
        self.heartbeat_interval_s = max(0.0, float(heartbeat_interval_s))
        self.accepted_probe: VisualProbe | None = None
        self.last_observed_probe: VisualProbe | None = None
        self.last_ocr_at: float | None = None
        self.last_saved_at: float | None = None
        self.last_event_at: float | None = None

    def reset(self) -> None:
        self.accepted_probe = None
        self.last_observed_probe = None
        self.last_ocr_at = None
        self.last_saved_at = None
        self.last_event_at = None

    def resume(self) -> None:
        """Fuerza un heartbeat al reabrir visibilidad sin perder identidad."""
        self.last_event_at = None

    @staticmethod
    def _elapsed(now: float, previous: float | None) -> float:
        return float("inf") if previous is None else max(0.0, now - previous)

    def decide(self, probe: VisualProbe, *, now: float) -> VisualDecision:
        now = float(now)
        referencia = self.accepted_probe
        self.last_observed_probe = probe
        if referencia is None:
            return VisualDecision(
                kind="initial", reason="inicio", candidate=True,
                run_ocr=True, emit_heartbeat=False)

        delta = compare_visual_probes(
            referencia, probe, pixel_delta=self.pixel_delta)
        cambio_mayor = bool(
            (
                delta.coarse_distance > self.coarse_threshold
                and max(delta.changed_ratio, delta.color_changed_ratio)
                >= self.change_ratio
            )
            or delta.changed_ratio >= self.major_ratio
            or delta.color_changed_ratio >= self.major_ratio
            or delta.mean_delta >= max(4.0, self.mean_delta * 8.0)
            or delta.color_mean_delta >= max(4.0, self.mean_delta * 8.0)
        )
        cambio_sutil = bool(
            delta.detail_distance > self.detail_threshold
            or delta.coarse_distance > self.coarse_threshold
            or (
                delta.changed_ratio >= self.change_ratio
                and delta.max_delta > self.pixel_delta
            )
            or (
                delta.color_changed_ratio >= self.change_ratio
                and delta.color_max_delta > self.pixel_delta
            )
        )
        if cambio_mayor:
            tipo, motivo, candidato = "major_change", "cambio_visual_mayor", True
        elif cambio_sutil and self._elapsed(now, self.last_saved_at) >= self.subtle_interval_s:
            tipo, motivo, candidato = "subtle_change", "cambio_visual_sutil", True
        else:
            tipo, motivo, candidato = "stable", "visual_estable", False

        ocr_vencido = self._elapsed(now, self.last_ocr_at) >= self.ocr_refresh_s
        if not candidato and ocr_vencido:
            tipo, motivo = "semantic_refresh", "verificacion_semantica_periodica"
        ejecutar_ocr = bool(
            ocr_vencido
            or (candidato and self._elapsed(now, self.last_ocr_at)
                >= self.ocr_min_interval_s)
        )
        heartbeat = bool(
            not candidato
            and self._elapsed(now, self.last_event_at) >= self.heartbeat_interval_s)
        return VisualDecision(
            kind=tipo,
            reason=motivo,
            candidate=candidato,
            run_ocr=ejecutar_ocr,
            emit_heartbeat=heartbeat,
            delta=delta,
        )

    def commit(
            self, decision: VisualDecision, probe: VisualProbe, *, now: float,
            saved: bool = False, heartbeat: bool = False, ocr_ran: bool = False,
            semantic_confirmed: bool = False) -> None:
        now = float(now)
        if ocr_ran:
            self.last_ocr_at = now
        if saved:
            self.accepted_probe = probe
            self.last_saved_at = now
            self.last_event_at = now
        elif semantic_confirmed:
            self.accepted_probe = probe
        if heartbeat:
            self.last_event_at = now


def _percentil(valores: list[float], fraccion: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    posicion = (len(ordenados) - 1) * fraccion
    inferior = int(posicion)
    superior = min(len(ordenados) - 1, inferior + 1)
    peso = posicion - inferior
    return ordenados[inferior] * (1.0 - peso) + ordenados[superior] * peso


def _resumen(valores: deque[float], total: float, count: int | None = None) -> dict:
    muestras = list(valores)
    resumen = {
        "count": len(muestras),
        "total": round(float(total), 6),
        "p50": round(_percentil(muestras, 0.50), 3),
        "p95": round(_percentil(muestras, 0.95), 3),
        "max": round(max(muestras, default=0.0), 3),
    }
    if count is not None:
        resumen["total_count"] = int(count)
    return resumen


class VisualRuntimeMetrics:
    """Telemetría acotada; muestrea en límites de etapa y no crea threads."""

    def __init__(self, *, max_samples: int = 256) -> None:
        self.max_samples = max(16, int(max_samples))
        self.started_wall = time.perf_counter()
        self.started_cpu = time.process_time()
        self.closed = False
        self.counters: Counter[str] = Counter()
        self.gauges: dict[str, float | int | str | bool] = {}
        self._wall_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.max_samples))
        self._cpu_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.max_samples))
        self._wall_totals: Counter[str] = Counter()
        self._cpu_totals: Counter[str] = Counter()
        self._stage_counts: Counter[str] = Counter()
        self._iteration_samples: deque[float] = deque(maxlen=self.max_samples)
        self._iteration_total = 0.0
        self._iteration_count = 0
        self._overrun_samples: deque[float] = deque(maxlen=self.max_samples)
        self._overrun_total = 0.0
        self.in_flight = 0
        self.max_in_flight = 0
        self._memory_source = "unavailable"
        self._memory_reader = None
        self._baseline_rss = 0
        self._peak_rss = 0
        try:
            import psutil

            process = psutil.Process(os.getpid())
            self._memory_reader = lambda: int(process.memory_info().rss)
            self._memory_source = "psutil_rss"
            self._baseline_rss = self._memory_reader()
            self._peak_rss = self._baseline_rss
        except Exception:
            pass

    def increment(self, name: str, value: int = 1) -> None:
        self.counters[str(name)] += int(value)

    def set_gauge(self, name: str, value) -> None:
        self.gauges[str(name)] = value

    def _sample_memory(self) -> None:
        if self._memory_reader is None:
            return
        try:
            self._peak_rss = max(self._peak_rss, self._memory_reader())
        except Exception:
            self._memory_reader = None

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        wall = time.perf_counter()
        cpu = time.process_time()
        try:
            yield
        finally:
            wall_ms = (time.perf_counter() - wall) * 1000.0
            cpu_ms = (time.process_time() - cpu) * 1000.0
            key = str(name)
            self._wall_samples[key].append(wall_ms)
            self._cpu_samples[key].append(cpu_ms)
            self._wall_totals[key] += wall_ms
            self._cpu_totals[key] += cpu_ms
            self._stage_counts[key] += 1
            self._sample_memory()

    @contextmanager
    def frame(self) -> Iterator[None]:
        if self.in_flight != 0:
            raise RuntimeError("visual frame already in flight")
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.increment("frames_started")
        inicio = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - inicio) * 1000.0
            self._iteration_samples.append(elapsed)
            self._iteration_total += elapsed
            self._iteration_count += 1
            self.in_flight = max(0, self.in_flight - 1)
            self.increment("frames_finished")

    def record_overrun(self, seconds: float) -> None:
        milliseconds = max(0.0, float(seconds) * 1000.0)
        if milliseconds <= 0:
            return
        self.increment("poll_overruns")
        self._overrun_samples.append(milliseconds)
        self._overrun_total += milliseconds

    def close(self) -> None:
        self.closed = True
        self._sample_memory()

    def snapshot(self) -> dict:
        wall_seconds = max(0.0, time.perf_counter() - self.started_wall)
        cpu_seconds = max(0.0, time.process_time() - self.started_cpu)
        stages = {}
        for name in sorted(set(self._wall_samples) | set(self._cpu_samples)):
            stages[name] = {
                "wall_ms": _resumen(
                    self._wall_samples[name], self._wall_totals[name],
                    self._stage_counts[name]),
                "cpu_ms": _resumen(
                    self._cpu_samples[name], self._cpu_totals[name],
                    self._stage_counts[name]),
            }
        baseline_mb = self._baseline_rss / (1024 * 1024)
        peak_mb = self._peak_rss / (1024 * 1024)
        return {
            "wall_seconds": round(wall_seconds, 6),
            "cpu_seconds": round(cpu_seconds, 6),
            "cpu_percent_one_core": round(
                100.0 * cpu_seconds / max(wall_seconds, 1e-9), 3),
            "ram": {
                "source": self._memory_source,
                "baseline_mb": round(baseline_mb, 3),
                "peak_mb": round(peak_mb, 3),
                "delta_mb": round(max(0.0, peak_mb - baseline_mb), 3),
            },
            "stages": stages,
            "iteration_latency_ms": _resumen(
                self._iteration_samples, self._iteration_total,
                self._iteration_count),
            "poll_overrun_ms": _resumen(
                self._overrun_samples, self._overrun_total,
                self.counters.get("poll_overruns", 0)),
            "counters": dict(sorted(self.counters.items())),
            "gauges": dict(sorted(self.gauges.items())),
            "queue": {
                "mode": "none",
                "capacity": 0,
                "max_depth": 0,
                "final_depth": 0,
                "dropped": 0,
                "drained": self.in_flight == 0,
                "max_in_flight": self.max_in_flight,
            },
            "closed": bool(self.closed),
        }
