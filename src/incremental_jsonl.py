"""Lectura JSONL append-only con recuperacion ante truncado o reemplazo.

Los productores de ClaseViva publican varias colas mediante append + flush/fsync.
Los consumidores de larga vida no necesitan volver a decodificar todo el historial
cuando el archivo no cambio. Esta vista conserva todas las filas validas en memoria,
lee solo el sufijo nuevo y reconstruye desde cero si detecta una mutacion incompatible
con append-only.
"""
from __future__ import annotations

import bisect
import json
import math
import threading
from pathlib import Path


class AppendOnlyJsonlReader:
    """Vista incremental de un JSONL; una linea final parcial espera su terminacion."""

    _GUARD_BYTES = 64

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._rows: list[dict] = []
        self._offset = 0
        self._pending = b""
        self._identity: tuple[int, int] | None = None
        self._mtime_ns = -1
        self._guard = b""
        self._loaded = False
        self.version = 0
        self.bytes_read = 0
        self.full_rebuilds = 0
        self.incremental_reads = 0
        self.cache_hits = 0

    @staticmethod
    def _file_identity(stat_result) -> tuple[int, int]:
        return int(getattr(stat_result, "st_dev", 0)), int(
            getattr(stat_result, "st_ino", 0))

    @staticmethod
    def _decode_line(raw: bytes) -> dict | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _parse(self, data: bytes, *, replace: bool) -> bool:
        if replace:
            rows: list[dict] = []
            combined = data
        else:
            rows = self._rows
            combined = self._pending + data

        pieces = combined.split(b"\n")
        trailing = pieces.pop() if pieces else b""
        for raw in pieces:
            row = self._decode_line(raw)
            if row is not None:
                rows.append(row)

        # Los escritores normales siempre cierran con LF. Si el ultimo fragmento
        # ya es JSON valido, se acepta para mantener compatibilidad con evidencia
        # legacy; si esta truncado se conserva hasta el siguiente append.
        trailing_row = self._decode_line(trailing)
        if trailing_row is not None:
            rows.append(trailing_row)
            trailing = b""

        changed = replace or rows is not self._rows or len(rows) != len(self._rows)
        self._rows = rows
        self._pending = trailing
        return changed

    def _set_guard(self, handle) -> None:
        length = min(self._GUARD_BYTES, self._offset)
        if length <= 0:
            self._guard = b""
            return
        handle.seek(self._offset - length)
        self._guard = handle.read(length)

    def _guard_matches(self, handle) -> bool:
        if not self._guard:
            return True
        handle.seek(self._offset - len(self._guard))
        return handle.read(len(self._guard)) == self._guard

    def _clear_missing(self) -> None:
        changed = bool(self._rows or self._pending or self._loaded)
        self._rows = []
        self._pending = b""
        self._offset = 0
        self._identity = None
        self._mtime_ns = -1
        self._guard = b""
        self._loaded = False
        if changed:
            self.version += 1

    def _rebuild(self, stat_result) -> None:
        with self.path.open("rb") as handle:
            data = handle.read()
            self.bytes_read += len(data)
            self._parse(data, replace=True)
            self._offset = len(data)
            self._set_guard(handle)
        self._identity = self._file_identity(stat_result)
        self._mtime_ns = int(getattr(stat_result, "st_mtime_ns", 0))
        self._loaded = True
        self.full_rebuilds += 1
        self.version += 1

    def refresh(self) -> list[dict]:
        """Actualiza la vista y devuelve la lista interna para lectura solamente."""
        with self._lock:
            try:
                stat_result = self.path.stat()
            except FileNotFoundError:
                self._clear_missing()
                return self._rows

            identity = self._file_identity(stat_result)
            size = int(stat_result.st_size)
            mtime_ns = int(getattr(stat_result, "st_mtime_ns", 0))
            if (not self._loaded or identity != self._identity
                    or size < self._offset):
                self._rebuild(stat_result)
                return self._rows

            if size == self._offset and mtime_ns == self._mtime_ns:
                self.cache_hits += 1
                return self._rows

            with self.path.open("rb") as handle:
                if not self._guard_matches(handle):
                    self._rebuild(stat_result)
                    return self._rows
                if size == self._offset:
                    self._mtime_ns = mtime_ns
                    self.cache_hits += 1
                    return self._rows
                handle.seek(self._offset)
                data = handle.read()
                self.bytes_read += len(data)
                self._parse(data, replace=False)
                self._offset += len(data)
                self._set_guard(handle)

            self._mtime_ns = mtime_ns
            self.incremental_reads += 1
            self.version += 1
            return self._rows

    def metrics(self) -> dict:
        with self._lock:
            return {
                "path": str(self.path),
                "rows": len(self._rows),
                "offset": self._offset,
                "pending_bytes": len(self._pending),
                "version": self.version,
                "bytes_read": self.bytes_read,
                "full_rebuilds": self.full_rebuilds,
                "incremental_reads": self.incremental_reads,
                "cache_hits": self.cache_hits,
            }


class JsonlTimeWindowIndex:
    """Indice temporal incremental sobre una vista JSONL append-only.

    La primera consulta ordena el historial. Los appends posteriores insertan solo
    las filas nuevas y una consulta de ventana usa ``bisect`` en vez de filtrar el
    curso completo. Si el archivo se trunca o reemplaza, el indice se reconstruye
    desde la vista recuperada por :class:`AppendOnlyJsonlReader`.
    """

    def __init__(self, path: str | Path,
                 time_fields: tuple[str, ...] = ("t",)):
        self.path = Path(path)
        self.time_fields = tuple(time_fields) or ("t",)
        self.reader = reader_for(self.path)
        self._lock = threading.RLock()
        self._times: list[float] = []
        self._rows: list[dict] = []
        self._reader_version = -1
        self._reader_rebuilds = -1
        self._source_rows = 0
        self.rebuilds = 0
        self.appended_rows = 0
        self.cache_hits = 0

    def _time(self, row: dict) -> float | None:
        for field in self.time_fields:
            value = row.get(field)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                return number
        return None

    def _rebuild(self, source: list[dict]) -> None:
        entries = []
        for order, row in enumerate(source):
            timestamp = self._time(row)
            if timestamp is not None:
                entries.append((timestamp, order, row))
        entries.sort(key=lambda item: (item[0], item[1]))
        self._times = [item[0] for item in entries]
        self._rows = [item[2] for item in entries]
        self._source_rows = len(source)
        self.rebuilds += 1

    def _append(self, rows: list[dict]) -> None:
        for row in rows:
            timestamp = self._time(row)
            if timestamp is None:
                continue
            position = bisect.bisect_right(self._times, timestamp)
            self._times.insert(position, timestamp)
            self._rows.insert(position, row)
            self.appended_rows += 1
        self._source_rows += len(rows)

    def refresh(self) -> None:
        with self._lock:
            source = self.reader.refresh()
            metrics = self.reader.metrics()
            version = int(metrics["version"])
            rebuilds = int(metrics["full_rebuilds"])
            must_rebuild = (
                self._reader_version < 0
                or rebuilds != self._reader_rebuilds
                or len(source) < self._source_rows
            )
            if must_rebuild:
                self._rebuild(source)
            elif version != self._reader_version:
                self._append(source[self._source_rows:])
            else:
                self.cache_hits += 1
            self._reader_version = version
            self._reader_rebuilds = rebuilds

    def between(self, start: float, end: float, *,
                include_start: bool = False) -> list[dict]:
        """Devuelve filas con ``start < t <= end`` (o inicio inclusivo)."""
        self.refresh()
        with self._lock:
            left = (
                bisect.bisect_left(self._times, float(start))
                if include_start
                else bisect.bisect_right(self._times, float(start))
            )
            right = bisect.bisect_right(self._times, float(end))
            return list(self._rows[left:right])

    def all(self) -> list[dict]:
        self.refresh()
        with self._lock:
            return list(self._rows)

    def before(self, end: float, *, limit: int = 1,
               include_end: bool = True) -> list[dict]:
        """Devuelve hasta ``limit`` filas inmediatamente anteriores a ``end``."""
        self.refresh()
        with self._lock:
            right = (
                bisect.bisect_right(self._times, float(end))
                if include_end
                else bisect.bisect_left(self._times, float(end))
            )
            left = max(0, right - max(0, int(limit)))
            return list(self._rows[left:right])

    def bounds(self) -> tuple[float, float] | None:
        self.refresh()
        with self._lock:
            return (self._times[0], self._times[-1]) if self._times else None

    def metrics(self) -> dict:
        with self._lock:
            return {
                "path": str(self.path),
                "rows": len(self._rows),
                "source_rows": self._source_rows,
                "rebuilds": self.rebuilds,
                "appended_rows": self.appended_rows,
                "cache_hits": self.cache_hits,
                "reader": self.reader.metrics(),
            }


_READERS: dict[str, AppendOnlyJsonlReader] = {}
_READERS_LOCK = threading.RLock()
_TIME_INDEXES: dict[tuple[str, tuple[str, ...]], JsonlTimeWindowIndex] = {}


def reader_for(path: str | Path) -> AppendOnlyJsonlReader:
    """Comparte una vista por ruta dentro del proceso consumidor."""
    key = str(Path(path).resolve())
    with _READERS_LOCK:
        reader = _READERS.get(key)
        if reader is None:
            reader = AppendOnlyJsonlReader(path)
            _READERS[key] = reader
        return reader


def read_jsonl(path: str | Path) -> list[dict]:
    return reader_for(path).refresh()


def time_index_for(path: str | Path,
                   time_fields: tuple[str, ...] = ("t",)) -> JsonlTimeWindowIndex:
    key = (str(Path(path).resolve()), tuple(time_fields) or ("t",))
    with _READERS_LOCK:
        index = _TIME_INDEXES.get(key)
        if index is None:
            index = JsonlTimeWindowIndex(path, key[1])
            _TIME_INDEXES[key] = index
        return index


def clear_readers() -> None:
    """Aislamiento explicito para pruebas; el runtime no necesita vaciar la cache."""
    with _READERS_LOCK:
        _TIME_INDEXES.clear()
        _READERS.clear()
