"""Atribución local y de solo lectura del gasto API por sesión y curso."""
from __future__ import annotations

import bisect
import math
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import config
from . import estado as est
from . import incremental_jsonl


_CLASS_CACHE_LOCK = threading.RLock()
_CLASS_CACHE: dict[str, tuple[tuple, dict, tuple[float, ...]]] = {}
_CLASS_CACHE_READS = 0
_CLASS_CACHE_HITS = 0


def _path_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (
        int(stat_result.st_mtime_ns), int(stat_result.st_size),
        int(getattr(stat_result, "st_ino", 0)),
    )


def _ruta_relativa(d: Path) -> str:
    try:
        return d.relative_to(config.ROOT).as_posix()
    except ValueError:
        return d.as_posix()


def _rango_clase(d: Path, st: dict, ahora: float,
                 fines: tuple[float, ...] | None = None) -> tuple[float, float]:
    try:
        inicio = float(st.get("inicio", ahora) or ahora)
    except (TypeError, ValueError):
        inicio = ahora
    if fines is None:
        parsed: list[float] = []
        for marca in est.leer_marcas(d):
            if marca.get("tipo") != "fin":
                continue
            try:
                parsed.append(float(marca.get("t")))
            except (TypeError, ValueError):
                continue
        fines = tuple(parsed)
    fin = fines[-1] if st.get("estado") == "fin" and fines else ahora
    return inicio, max(inicio, fin)


def _class_state(d: Path) -> tuple[dict, tuple[float, ...]]:
    """Relee estado/marcas solo cuando cambia alguno de esos archivos."""
    global _CLASS_CACHE_HITS, _CLASS_CACHE_READS
    signature = (
        _path_signature(d / "session_state.json"),
        _path_signature(d / "marcas.jsonl"),
    )
    key = str(d.resolve())
    with _CLASS_CACHE_LOCK:
        cached = _CLASS_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            _CLASS_CACHE_HITS += 1
            return cached[1], cached[2]

    st = est.leer_estado(d)
    fines: list[float] = []
    for marca in est.leer_marcas(d):
        if marca.get("tipo") != "fin":
            continue
        try:
            instante = float(marca.get("t"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(instante):
            fines.append(instante)
    value = (dict(st), tuple(fines))
    with _CLASS_CACHE_LOCK:
        _CLASS_CACHE[key] = (signature, value[0], value[1])
        _CLASS_CACHE_READS += 1
    return value


def _clases(curso: str, ahora: float) -> list[tuple[Path, dict, tuple[float, float]]]:
    clases_dir = config.dir_curso(curso) / "clases"
    if not clases_dir.is_dir():
        return []
    resultado = []
    for d in sorted(clases_dir.iterdir()):
        if not (d / "session_state.json").is_file():
            continue
        try:
            st, fines = _class_state(d)
            resultado.append((d, st, _rango_clase(d, st, ahora, fines)))
        except Exception:
            continue
    return resultado


def _costo(fila: dict) -> float:
    try:
        return float(fila.get("costo_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _en_rango(fila: dict, rango: tuple[float, float]) -> bool:
    try:
        instante = float(fila.get("t", 0.0))
    except (TypeError, ValueError):
        return False
    return rango[0] <= instante <= rango[1]


def _es_de_clase(fila: dict, d: Path, st: dict,
                  rango: tuple[float, float]) -> bool:
    curso = str(st.get("curso") or est.curso_de(d))
    clase_id = str(st.get("clase_id") or d.name)
    log_curso = str(fila.get("curso") or "")
    log_clase = str(fila.get("clase_id") or "")
    log_path = str(fila.get("clase_path") or "")
    if any((log_curso, log_clase, log_path)):
        if log_curso and log_curso != curso:
            return False
        if log_path:
            return log_path == _ruta_relativa(d)
        return bool(log_clase and log_clase == clase_id)
    return _en_rango(fila, rango)


def _es_de_curso(
        fila: dict, curso: str,
        clases: list[tuple[Path, dict, tuple[float, float]]]) -> bool:
    log_curso = str(fila.get("curso") or "")
    if log_curso:
        return log_curso == curso
    log_path = str(fila.get("clase_path") or "")
    if log_path:
        return any(log_path == _ruta_relativa(d) for d, _st, _rango in clases)
    log_clase = str(fila.get("clase_id") or "")
    if log_clase:
        return any(
            log_clase == str(st.get("clase_id") or d.name)
            for d, st, _rango in clases
            if str(st.get("curso") or est.curso_de(d)) == curso
        )
    return any(_en_rango(fila, rango) for _d, _st, rango in clases)


class _UsageCostIndex:
    """Agrega una vez cada fila de uso y consulta por curso/clase en O(1).

    Los registros legacy sin metadata se mantienen en un indice temporal con
    sumas prefijas; no obligan a recorrer el historial en cada refresco.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.reader = incremental_jsonl.reader_for(self.path)
        self._lock = threading.RLock()
        self._reader_version = -1
        self._reader_rebuilds = -1
        self._source_rows = 0
        self._by_course: defaultdict[str, float] = defaultdict(float)
        self._course_path: defaultdict[tuple[str, str], float] = defaultdict(float)
        self._path_no_course: defaultdict[str, float] = defaultdict(float)
        self._course_class: defaultdict[tuple[str, str], float] = defaultdict(float)
        self._class_no_course: defaultdict[str, float] = defaultdict(float)
        self._legacy: list[tuple[float, int, float]] = []
        self._legacy_times: list[float] = []
        self._legacy_prefix: list[float] = [0.0]
        self._legacy_dirty = False
        self._serial = 0
        self.aggregate_rebuilds = 0
        self.rows_indexed = 0
        self.cache_hits = 0

    def _reset(self) -> None:
        self._by_course.clear()
        self._course_path.clear()
        self._path_no_course.clear()
        self._course_class.clear()
        self._class_no_course.clear()
        self._legacy.clear()
        self._legacy_times.clear()
        self._legacy_prefix = [0.0]
        self._legacy_dirty = False
        self._serial = 0
        self._source_rows = 0
        self.aggregate_rebuilds += 1

    def _index_row(self, row: dict) -> None:
        cost = _costo(row)
        course = str(row.get("curso") or "")
        class_path = str(row.get("clase_path") or "")
        class_id = str(row.get("clase_id") or "")
        if course:
            self._by_course[course] += cost
            if class_path:
                self._course_path[(course, class_path)] += cost
            elif class_id:
                self._course_class[(course, class_id)] += cost
        elif class_path:
            self._path_no_course[class_path] += cost
        elif class_id:
            self._class_no_course[class_id] += cost
        else:
            try:
                timestamp = float(row.get("t", 0.0))
            except (TypeError, ValueError):
                timestamp = float("nan")
            if math.isfinite(timestamp):
                entry = (timestamp, self._serial, cost)
                if not self._legacy or entry[:2] >= self._legacy[-1][:2]:
                    self._legacy.append(entry)
                else:
                    bisect.insort_right(self._legacy, entry)
                self._serial += 1
                self._legacy_dirty = True
        self.rows_indexed += 1

    def refresh(self) -> None:
        with self._lock:
            rows = self.reader.refresh()
            metrics = self.reader.metrics()
            version = int(metrics["version"])
            rebuilds = int(metrics["full_rebuilds"])
            if (
                self._reader_version < 0
                or rebuilds != self._reader_rebuilds
                or len(rows) < self._source_rows
            ):
                self._reset()
                pending = rows
            elif version != self._reader_version:
                pending = rows[self._source_rows:]
            else:
                self.cache_hits += 1
                return
            for row in pending:
                self._index_row(row)
            self._source_rows = len(rows)
            self._reader_version = version
            self._reader_rebuilds = rebuilds

    def _prepare_legacy(self) -> None:
        if not self._legacy_dirty:
            return
        self._legacy_times = [item[0] for item in self._legacy]
        prefix = [0.0]
        for _timestamp, _serial, cost in self._legacy:
            prefix.append(prefix[-1] + cost)
        self._legacy_prefix = prefix
        self._legacy_dirty = False

    def _legacy_range(self, start: float, end: float) -> float:
        self._prepare_legacy()
        left = bisect.bisect_left(self._legacy_times, float(start))
        right = bisect.bisect_right(self._legacy_times, float(end))
        return self._legacy_prefix[right] - self._legacy_prefix[left]

    def _legacy_union(self, ranges: list[tuple[float, float]]) -> float:
        if not ranges:
            return 0.0
        merged: list[list[float]] = []
        for start, end in sorted(ranges):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return sum(self._legacy_range(start, end) for start, end in merged)

    def course_total(
            self, course: str,
            classes: list[tuple[Path, dict, tuple[float, float]]]) -> float:
        with self._lock:
            paths = {_ruta_relativa(d) for d, _st, _range in classes}
            class_ids = {
                str(st.get("clase_id") or d.name) for d, st, _range in classes
                if str(st.get("curso") or est.curso_de(d)) == course
            }
            return (
                self._by_course[course]
                + sum(self._path_no_course[path] for path in paths)
                + sum(self._class_no_course[class_id] for class_id in class_ids)
                + self._legacy_union([span for _d, _st, span in classes])
            )

    def class_total(self, d: Path, st: dict,
                    span: tuple[float, float]) -> float:
        with self._lock:
            course = str(st.get("curso") or est.curso_de(d))
            class_id = str(st.get("clase_id") or d.name)
            class_path = _ruta_relativa(d)
            return (
                self._course_path[(course, class_path)]
                + self._path_no_course[class_path]
                + self._course_class[(course, class_id)]
                + self._class_no_course[class_id]
                + self._legacy_range(*span)
            )

    def metrics(self) -> dict:
        with self._lock:
            return {
                "source_rows": self._source_rows,
                "rows_indexed": self.rows_indexed,
                "aggregate_rebuilds": self.aggregate_rebuilds,
                "cache_hits": self.cache_hits,
                "legacy_rows": len(self._legacy),
                "reader": self.reader.metrics(),
            }


_USAGE_INDEXES_LOCK = threading.RLock()
_USAGE_INDEXES: dict[str, _UsageCostIndex] = {}


def _usage_index(path: Path) -> _UsageCostIndex:
    key = str(path.resolve())
    with _USAGE_INDEXES_LOCK:
        index = _USAGE_INDEXES.get(key)
        if index is None:
            index = _UsageCostIndex(path)
            _USAGE_INDEXES[key] = index
        return index


def clear_caches() -> None:
    """Aislamiento para pruebas; el runtime conserva sus indices toda la sesion."""
    global _CLASS_CACHE_HITS, _CLASS_CACHE_READS
    with _CLASS_CACHE_LOCK:
        _CLASS_CACHE.clear()
        _CLASS_CACHE_HITS = 0
        _CLASS_CACHE_READS = 0
    with _USAGE_INDEXES_LOCK:
        _USAGE_INDEXES.clear()


def cache_metrics() -> dict:
    path = config.LOGS_DIR / "uso_api.jsonl"
    with _USAGE_INDEXES_LOCK:
        index = _USAGE_INDEXES.get(str(path.resolve()))
    with _CLASS_CACHE_LOCK:
        classes = {
            "entries": len(_CLASS_CACHE),
            "reads": _CLASS_CACHE_READS,
            "hits": _CLASS_CACHE_HITS,
        }
    return {"classes": classes, "usage": index.metrics() if index else {}}


def resumen_curso(
        curso: str, ahora: float | None = None,
        clase_actual: Path | None = None) -> dict:
    """Devuelve gasto de la sesión activa y acumulado de su curso, sin total global."""
    ahora = time.time() if ahora is None else float(ahora)
    clases = _clases(curso, ahora)
    index = _usage_index(config.LOGS_DIR / "uso_api.jsonl")
    index.refresh()
    total_curso = index.course_total(curso, clases)
    lecciones = []
    actual_resuelta = clase_actual.resolve() if clase_actual is not None else None
    for d, st, rango in clases:
        costo = index.class_total(d, st, rango)
        activa = bool(actual_resuelta is not None and d.resolve() == actual_resuelta)
        lecciones.append({
            "clase_id": st.get("clase_id", d.name),
            "nombre": st.get("nombre", d.name),
            "curso": st.get("curso") or est.curso_de(d),
            "estado": st.get("estado", ""),
            "inicio": rango[0], "fin": rango[1], "activa": activa,
            "costo_usd": round(costo, 6),
        })
    costo_actual = next(
        (item["costo_usd"] for item in lecciones if item["activa"]), 0.0)
    return {
        "curso": curso,
        "total_usd": round(total_curso, 6),
        "clase_actual_usd": round(costo_actual, 6),
        "lecciones": lecciones,
    }
