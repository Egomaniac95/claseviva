"""Contrato versionado para continuidad de audio y commits de transcripcion.

``audio_manifest.jsonl`` pertenece a captura y es append-only. El STT remoto escribe un
commit atomico por chunk en ``transcript_chunks/``; ``transcript.jsonl`` es una
vista legacy reconstruible, no la fuente del estado transaccional.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import wave
from pathlib import Path


SCHEMA_VERSION = 1
MANIFEST_NAME = "audio_manifest.jsonl"
JOURNAL_DIR = "transcript_chunks"
ATTEMPTS_NAME = "_attempts.jsonl"
CHUNK_RE = re.compile(
    r"^chunk_(?P<epoch>\d+)_p(?P<part>\d+)(?:_v(?P<speed>\d+))?\.wav$",
    re.IGNORECASE,
)
FINAL_CAPTURE_STATES = frozenset({"sealed", "recovered"})
CONTINUITY_COVERAGE_STATES = frozenset({
    "sealed", "recovered", "discarded_silence",
})
CAPTURE_SESSION_STARTED = "capture_session_started"
CAPTURE_SESSION_CLOSED = "capture_session_closed"
CAPTURE_SESSION_STATES = frozenset({
    CAPTURE_SESSION_STARTED,
    CAPTURE_SESSION_CLOSED,
})
_APPEND_LOCK = threading.Lock()


def _class_dir(target: str | Path) -> Path:
    path = Path(target)
    if path.name in {MANIFEST_NAME, "transcript.jsonl"}:
        return path.parent
    if path.name in {"audio", JOURNAL_DIR}:
        return path.parent
    return path


def manifest_path(target: str | Path) -> Path:
    return _class_dir(target) / MANIFEST_NAME


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8")
    # Si el proceso anterior murio a mitad de fila, separar la nueva fila valida.
    # El lector ignorara solo la fila truncada, no ambas concatenadas.
    with _APPEND_LOCK:
        with open(path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())


def append_event(target: str | Path, event: dict) -> dict:
    """Anexa un evento durable; nunca reescribe eventos anteriores."""
    row = dict(event)
    row.setdefault("schema_version", SCHEMA_VERSION)
    row.setdefault("observed_at", time.time())
    if row.get("sequence") is not None:
        row.setdefault("seq", row["sequence"])
    if row.get("path"):
        row.setdefault("chunk", f"audio/{Path(str(row['path'])).name}")
    if row.get("wall_start") is not None:
        row.setdefault("t_inicio", row["wall_start"])
    if row.get("wall_end") is not None:
        row.setdefault("t_fin", row["wall_end"])
    if row.get("duration") is not None:
        row.setdefault("duration_sec", row["duration"])
    if row.get("rate") is not None:
        row.setdefault("sample_rate", row["rate"])
    if row.get("frame_count") is not None:
        row.setdefault("frames", row["frame_count"])
    if row.get("mode") is not None:
        row.setdefault("audio_modo", row["mode"])
    if row.get("stream_id"):
        row.setdefault("capture_session_id", row["stream_id"])
    if row.get("pid") is not None:
        row.setdefault("root_pid", row["pid"])
    gap_frames = _float(row.get("gap_frames"), 0.0)
    rate = _float(row.get("rate"), 0.0)
    if rate > 0:
        row.setdefault("discontinuity_before_ms", round(gap_frames / rate * 1000, 3))
    if int(row["schema_version"]) != SCHEMA_VERSION:
        raise ValueError(f"schema_version no soportado: {row['schema_version']}")
    status = str(row.get("status") or "").strip()
    if not str(row.get("chunk_id") or "").strip() and status not in CAPTURE_SESSION_STATES:
        raise ValueError("evento de audio sin chunk_id")
    if not status:
        raise ValueError("evento de audio sin status")
    if status in CAPTURE_SESSION_STATES and not str(
            row.get("capture_session_id") or "").strip():
        raise ValueError("evento de sesion de captura sin capture_session_id")
    _append_jsonl(manifest_path(target), row)
    return row


def append_capture_session_event(
        target: str | Path, status: str, capture_session_id: str, **metadata) -> dict:
    status = str(status or "")
    if status not in CAPTURE_SESSION_STATES:
        raise ValueError(f"estado de sesion de captura invalido: {status}")
    return append_event(target, {
        "event_type": "capture_session",
        "status": status,
        "capture_session_id": str(capture_session_id),
        **metadata,
    })


def read_events(target: str | Path) -> list[dict]:
    """Lee filas validas y tolera una ultima linea truncada por un crash."""
    path = manifest_path(target)
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def latest_by_chunk(target: str | Path) -> dict[str, dict]:
    return latest_by_chunk_rows(read_events(target))


def latest_by_chunk_rows(events: list[dict]) -> dict[str, dict]:
    """Reduce una vista ya leida sin volver a abrir el manifest."""
    latest: dict[str, dict] = {}
    for row in events:
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id:
            latest[chunk_id] = row
    return latest


def capture_failure_report(target: str | Path) -> dict:
    """Separa fallos de captura aun pendientes de los recuperados posteriormente.

    El conteo es por chunk. Los eventos repetidos se conservan en ``failure_events``,
    pero un ``sealed`` o ``recovered`` posterior resuelve el fallo sin duplicarlo.
    """
    histories: dict[str, dict] = {}
    failure_events = 0
    for row in read_events(target):
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id:
            continue
        status = str(row.get("status") or "")
        history = histories.setdefault(chunk_id, {
            "failure_events": 0,
            "last_failure": None,
            "recovery": None,
            "unresolved": False,
        })
        if status == "capture_failed":
            failure_events += 1
            history["failure_events"] += 1
            history["last_failure"] = row
            history["recovery"] = None
            history["unresolved"] = True
        elif status in FINAL_CAPTURE_STATES and history["last_failure"] is not None:
            history["recovery"] = row
            history["unresolved"] = False

    terminal: list[dict] = []
    recovered: list[dict] = []
    for chunk_id, history in histories.items():
        failure = history["last_failure"]
        if failure is None:
            continue
        detail = {
            "chunk_id": chunk_id,
            "path": failure.get("path") or f"{chunk_id}.wav",
            "sequence": failure.get("sequence", failure.get("seq")),
            "reason": str(failure.get("reason") or "capture_failed"),
            "error_type": str(failure.get("error_type") or ""),
            "observed_at": failure.get("observed_at"),
            "wall_start": failure.get("wall_start", failure.get("t_inicio")),
            "wall_end": failure.get("wall_end", failure.get("t_fin")),
            "failure_events": int(history["failure_events"]),
        }
        if history["unresolved"]:
            terminal.append(detail)
            continue
        recovery = history["recovery"] or {}
        recovered.append({
            **detail,
            "recovery_status": str(recovery.get("status") or ""),
            "recovery_sequence": recovery.get("sequence", recovery.get("seq")),
            "recovered_at": recovery.get("observed_at"),
        })

    def order(row: dict) -> tuple:
        return (
            _float(row.get("sequence"), float("inf")),
            _float(row.get("observed_at"), float("inf")),
            str(row.get("chunk_id") or ""),
        )
    terminal.sort(key=order)
    recovered.sort(key=order)
    return {
        "failure_events": failure_events,
        "terminal_chunks": terminal,
        "recovered_chunks": recovered,
        "terminal_count": len(terminal),
        "recovered_count": len(recovered),
    }


def unfinished_capture_report(
        target: str | Path, capture_session_active: bool = False) -> dict:
    """Lista chunks cuyo ultimo estado sigue en ``capturing``.

    Un llamador en vivo puede declarar la sesion activa. Solo el ultimo evento global
    queda entonces marcado como transitorio; capturas inconclusas anteriores conservan
    severidad terminal. Auditoria y postproceso usan el valor conservador por defecto.
    """
    events = read_events(target)
    latest: dict[str, tuple[int, dict]] = {}
    for index, row in enumerate(events):
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id:
            latest[chunk_id] = (index, row)
    capturing_latest = [
        (chunk_id, index, row)
        for chunk_id, (index, row) in latest.items()
        if str(row.get("status") or "") == "capturing"
    ]
    active_chunk_id = ""
    if capture_session_active and capturing_latest:
        active_chunk_id = max(
            capturing_latest,
            key=lambda item: (
                _float(item[2].get("sequence", item[2].get("seq")), item[1]),
                item[1],
            ),
        )[0]
    details = []
    for chunk_id, (_index, row) in latest.items():
        if str(row.get("status") or "") != "capturing":
            continue
        active = chunk_id == active_chunk_id
        details.append({
            "chunk_id": chunk_id,
            "path": row.get("path") or f"{chunk_id}.wav",
            "sequence": row.get("sequence", row.get("seq")),
            "observed_at": row.get("observed_at"),
            "wall_start": row.get("wall_start", row.get("t_inicio")),
            "capture_session_id": str(
                row.get("capture_session_id") or row.get("stream_id") or ""),
            "active_session": active,
            "terminal": not active,
        })

    def order(row: dict) -> tuple:
        return (
            _float(row.get("sequence"), float("inf")),
            _float(row.get("observed_at"), float("inf")),
            str(row.get("chunk_id") or ""),
        )

    details.sort(key=order)
    terminal_count = sum(1 for detail in details if detail["terminal"])
    return {
        "count": len(details),
        "terminal_count": terminal_count,
        "active_count": len(details) - terminal_count,
        "details": details,
    }


def _float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _legacy_row(path: Path, sequence: int) -> dict | None:
    match = CHUNK_RE.match(path.name)
    if not match:
        return None
    try:
        with wave.open(str(path), "rb") as wav:
            rate = int(wav.getframerate())
            channels = int(wav.getnchannels())
            sample_width = int(wav.getsampwidth())
            frame_count = int(wav.getnframes())
    except (OSError, EOFError, wave.Error):
        return None
    wall_start = float(match.group("epoch"))
    duration = frame_count / rate if rate > 0 else 0.0
    speed_code = int(match.group("speed") or 10)
    return {
        "schema_version": SCHEMA_VERSION,
        "chunk_id": path.stem,
        "sequence": sequence,
        "status": "sealed",
        "path": path.name,
        "part": int(match.group("part")),
        "speed": speed_code / 10.0,
        "wall_start": wall_start,
        "wall_end": wall_start + duration,
        "rate": rate,
        "channels": channels,
        "sample_width": sample_width,
        "frame_count": frame_count,
        "duration": duration,
        "inferred_legacy": True,
        "timestamp_precision": "epoch_second",
    }


def infer_legacy(target: str | Path) -> list[dict]:
    """Infiere metadata desde WAV legacy sin escribir ni migrar evidencia."""
    class_dir = _class_dir(target)
    audio_dir = Path(target) if Path(target).name == "audio" else class_dir / "audio"
    if not audio_dir.is_dir():
        return []
    rows = []
    for sequence, wav_path in enumerate(sorted(audio_dir.glob("chunk_*.wav")), 1):
        row = _legacy_row(wav_path, sequence)
        if row:
            rows.append(row)
    return rows


def capture_records(target: str | Path) -> list[dict]:
    events = read_events(target)
    if not events:
        return infer_legacy(target)
    latest = latest_by_chunk_rows(events)
    rows = capture_records_from_events(events)
    represented = {str(r.get("chunk_id") or "") for r in rows}
    for legacy in infer_legacy(target):
        previous = latest.get(str(legacy.get("chunk_id") or ""))
        if legacy["chunk_id"] in represented:
            continue
        # Un WAV cuyo productor sigue en `capturing` puede existir entre rename
        # y publicación final. Solo el capturador lo recupera; STT debe esperar.
        if previous:
            continue
        rows.append(dict(legacy, inferred_with_manifest=True))
    return _sort_capture_records(rows)


def _sort_capture_records(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (
        _float(r.get("pcm_wall_start", r.get("wall_start")), float("inf")),
        _float(r.get("sequence"), float("inf")),
        str(r.get("chunk_id") or ""),
    ))


def capture_records_from_events(events: list[dict]) -> list[dict]:
    """Deriva solo publicaciones finales desde filas ya disponibles.

    No infiere WAV huerfanos: esa recuperacion pertenece al productor y la funcion
    publica ``capture_records`` conserva su compatibilidad legacy por separado.
    """
    latest = latest_by_chunk_rows(events)
    rows = [
        row for row in latest.values()
        if row.get("status") in FINAL_CAPTURE_STATES
    ]
    return _sort_capture_records(rows)


def next_sequence(target: str | Path) -> int:
    return max((int(_float(r.get("sequence"), 0)) for r in read_events(target)),
               default=0) + 1


def continuity_report(target: str | Path,
                      expected_chunk_seconds: float = 30.0,
                      capture_session_active: bool = False,
                      declared_inactive_intervals: list[tuple[float, float]]
                      | None = None) -> dict:
    """Cuantifica deficits, huecos y estados terminales de captura.

    ``capture_session_active`` solo debe usarse desde un productor vivo confirmado.
    Auditoria y postproceso deben conservar el valor ``False`` por defecto.
    """
    canonical_records = capture_records(target)
    events = read_events(target)
    if events:
        latest = latest_by_chunk(target)
        records = sorted(
            (row for row in latest.values()
             if row.get("status") in CONTINUITY_COVERAGE_STATES),
            key=lambda row: (
                _float(row.get("pcm_wall_start", row.get("wall_start")), float("inf")),
                _float(row.get("sequence"), float("inf")),
                str(row.get("chunk_id") or ""),
            ),
        )
    else:
        records = canonical_records
    capture_failures = capture_failure_report(target)
    unfinished = unfinished_capture_report(target, capture_session_active)
    boundaries = []
    deficit_frames = 0
    explicit_gap_frames = 0
    data_discontinuity_packets = 0
    terminal_discontinuities = 0
    qpc_inferred_gap_frames = 0
    wall_gap_total = 0.0
    wall_gap_max = 0.0
    positive_wall_gaps: list[float] = []
    declared_inactive_gap_seconds = 0.0
    declared_intervals = [
        (min(float(start), float(end)), max(float(start), float(end)))
        for start, end in (declared_inactive_intervals or [])
        if float(end) > float(start)
    ]
    declared_silence_frames = 0
    for index, row in enumerate(records):
        rate = max(0, int(_float(row.get("rate"), 0)))
        frames = max(0, int(_float(row.get("frame_count"), 0)))
        expected = int(_float(row.get("expected_frames"), 0))
        if not expected and index < len(records) - 1 and rate:
            expected = round(rate * expected_chunk_seconds)
        if expected:
            deficit_frames += max(0, expected - frames)
        if row.get("status") == "discarded_silence":
            declared_silence_frames += frames
        inferred_qpc = max(0, int(_float(row.get("qpc_inferred_gap_frames"), 0)))
        qpc_inferred_gap_frames += inferred_qpc
        explicit_gap_frames += max(0, int(_float(
            row.get("gap_before_frames", row.get("gap_frames")), 0)) - inferred_qpc)
        explicit_gap_frames += max(0, int(_float(
            row.get("timeout_padding_frames"), 0)))
        data_discontinuity_packets += max(0, int(_float(
            row.get("data_discontinuity_packets"), 0)))
        terminal_discontinuities += int(bool(row.get("terminal_discontinuity")))
        if index == 0:
            continue
        previous = records[index - 1]
        same_stream = bool(
            previous.get("stream_id")
            and previous.get("stream_id") == row.get("stream_id")
            and previous.get("end_frame") is not None
            and row.get("start_frame") is not None
        )
        previous_end_wall = _float(
            previous.get("pcm_wall_end", previous.get("wall_end")), 0.0)
        if not previous_end_wall:
            previous_end_wall = (
                _float(previous.get(
                    "pcm_wall_start", previous.get("wall_start")), 0.0)
                + _float(previous.get("duration"), 0.0)
            )
        current_start_wall = _float(
            row.get("pcm_wall_start", row.get("wall_start")), previous_end_wall)
        if same_stream:
            boundary_rate = max(1, int(_float(row.get("rate"), rate or 1)))
            gap = (
                int(_float(row.get("start_frame"), 0))
                - int(_float(previous.get("end_frame"), 0))
            ) / boundary_rate
            boundary_source = "stream_frames"
        else:
            gap = current_start_wall - previous_end_wall
            boundary_source = "pcm_wall" if (
                previous.get("pcm_wall_end") is not None
                or row.get("pcm_wall_start") is not None
            ) else "wall"
        tolerance = 0.75
        declared_inactivity = bool(
            gap > 0 and not same_stream and any(
                start <= previous_end_wall + tolerance
                and end >= current_start_wall - tolerance
                for start, end in declared_intervals
            )
        )
        if gap > 0 and declared_inactivity:
            declared_inactive_gap_seconds += gap
        elif gap > 0:
            wall_gap_total += gap
            wall_gap_max = max(wall_gap_max, gap)
            positive_wall_gaps.append(gap)
        boundaries.append({
            "previous": previous.get("path") or previous.get("chunk_id"),
            "current": row.get("path") or row.get("chunk_id"),
            "wall_gap_seconds": round(gap, 6),
            "source": boundary_source,
            "declared_inactivity": declared_inactivity,
            "approximate": bool(
                previous.get("inferred_legacy") or row.get("inferred_legacy")),
        })
    rates = [int(_float(r.get("rate"), 0)) for r in records if _float(r.get("rate"), 0)]
    reference_rate = max(set(rates), key=rates.count) if rates else 0
    deficit_seconds = deficit_frames / reference_rate if reference_rate else 0.0
    explicit_gap_seconds = explicit_gap_frames / reference_rate if reference_rate else 0.0
    sorted_gaps = sorted(positive_wall_gaps)
    p95_index = max(0, math.ceil(len(sorted_gaps) * 0.95) - 1)
    wall_gap_p95 = sorted_gaps[p95_index] if sorted_gaps else 0.0
    warning = (
        capture_failures["terminal_count"] > 0
        or unfinished["terminal_count"] > 0
        or explicit_gap_frames > 0
        or data_discontinuity_packets > 0
        or terminal_discontinuities > 0
        or wall_gap_p95 > 0.05
        or wall_gap_max > 0.2
        or deficit_seconds > max(0.25, len(records) * 0.05)
    )
    return {
        "status": "warning" if warning else "ok",
        "source": "manifest" if read_events(target) else "legacy_inference",
        "chunks": len(canonical_records),
        "coverage_intervals": len(records),
        "reference_rate": reference_rate,
        "deficit_frames": deficit_frames,
        "deficit_seconds": round(deficit_seconds, 6),
        "explicit_gap_frames": explicit_gap_frames,
        "explicit_gap_seconds": round(explicit_gap_seconds, 6),
        "declared_silence_frames": declared_silence_frames,
        "declared_silence_seconds": round(
            declared_silence_frames / reference_rate if reference_rate else 0.0, 6),
        "qpc_inferred_gap_frames": qpc_inferred_gap_frames,
        "qpc_inferred_silence_seconds": round(
            qpc_inferred_gap_frames / reference_rate if reference_rate else 0.0, 6),
        "data_discontinuity_packets": data_discontinuity_packets,
        "terminal_discontinuities": terminal_discontinuities,
        "capture_failure_events": capture_failures["failure_events"],
        "terminal_capture_failures": capture_failures["terminal_count"],
        "recovered_capture_failures": capture_failures["recovered_count"],
        "terminal_capture_failure_details": capture_failures["terminal_chunks"],
        "recovered_capture_failure_details": capture_failures["recovered_chunks"],
        "unfinished_capture_count": unfinished["count"],
        "terminal_unfinished_capture_count": unfinished["terminal_count"],
        "active_unfinished_capture_count": unfinished["active_count"],
        "unfinished_capture_details": unfinished["details"],
        "wall_gap_total_seconds": round(wall_gap_total, 6),
        "wall_gap_p95_seconds": round(wall_gap_p95, 6),
        "wall_gap_max_seconds": round(wall_gap_max, 6),
        "declared_inactive_gap_seconds": round(
            declared_inactive_gap_seconds, 6),
        "declared_inactive_intervals": [
            [round(start, 6), round(end, 6)]
            for start, end in declared_intervals
        ],
        "boundaries": boundaries,
    }


def orphan_report(target: str | Path) -> dict:
    class_dir = _class_dir(target)
    audio_dir = class_dir / "audio"
    latest = latest_by_chunk(class_dir)
    wavs = {p.stem: p.name for p in audio_dir.glob("chunk_*.wav")} if audio_dir.is_dir() else {}
    sealed = {
        chunk_id: row for chunk_id, row in latest.items()
        if row.get("status") in FINAL_CAPTURE_STATES
    }
    return {
        "wav_without_manifest": sorted(wavs[k] for k in wavs.keys() - sealed.keys()),
        "manifest_without_wav": sorted(
            str(sealed[k].get("path") or f"{k}.wav")
            for k in sealed.keys() - wavs.keys()),
        "part_files": sorted(p.name for p in audio_dir.glob("*.part"))
        if audio_dir.is_dir() else [],
    }


def _safe_chunk_id(chunk_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(chunk_id)).strip("._")
    if not safe:
        raise ValueError("chunk_id invalido")
    return safe


def _commit_path(target: str | Path, chunk_id: str) -> Path:
    return _class_dir(target) / JOURNAL_DIR / f"{_safe_chunk_id(chunk_id)}.json"


def read_transcript_commit(target: str | Path, chunk_id: str) -> dict | None:
    path = _commit_path(target, chunk_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("status") != "complete":
        return None
    return data if _commit_matches_audio(target, data) else None


def sha256_file(path: str | Path) -> str | None:
    """Hash actual, sin cache basada en metadata que pueda aceptar reemplazos."""
    path = Path(path)
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _commit_payload_error(data, expected_chunk_id: str | None = None) -> str:
    """Valida el sobre durable antes de interpretar un commit como completo."""
    if not isinstance(data, dict):
        return "commit_not_object"
    if data.get("schema_version") != SCHEMA_VERSION:
        return "commit_schema_unsupported"
    if data.get("status") != "complete":
        return "commit_not_complete"
    chunk_id = str(data.get("chunk_id") or "")
    if not chunk_id:
        return "commit_chunk_missing"
    if expected_chunk_id is not None and chunk_id != expected_chunk_id:
        return "commit_chunk_mismatch"
    if not isinstance(data.get("metadata"), dict):
        return "commit_metadata_not_object"
    if "lines" in data:
        lines = data.get("lines")
    elif "segments" in data:
        # Compatibilidad de lectura con el nombre usado por el journal experimental.
        lines = data.get("segments")
    else:
        return "commit_lines_missing"
    if not isinstance(lines, list):
        return "commit_lines_not_list"
    if any(not isinstance(line, dict) for line in lines):
        return "commit_line_not_object"
    return ""


def _commit_lines(data: dict) -> list[dict]:
    lines = data.get("lines") if "lines" in data else data.get("segments")
    return lines if isinstance(lines, list) else []


def _commit_matches_audio(target: str | Path, data: dict,
                          latest: dict[str, dict] | None = None,
                          verify_file: bool = True) -> bool:
    if _commit_payload_error(data):
        return False
    chunk_id = str(data.get("chunk_id") or "")
    row = (latest if latest is not None else latest_by_chunk(target)).get(chunk_id)
    metadata = data["metadata"]
    committed_hash = str(metadata.get("input_sha256") or "")
    manifest_hash = str((row or {}).get("sha256") or "")
    if manifest_hash and (not committed_hash or committed_hash != manifest_hash):
        return False
    if not verify_file:
        return True
    audio_path = _class_dir(target) / "audio" / str(
        (row or {}).get("path")
        or metadata.get("path")
        or f"{chunk_id}.wav")
    if not audio_path.is_file():
        # La limpieza explicita puede retirar WAV despues de generar la leccion.
        return True
    if not committed_hash:
        # Compatibilidad con journals experimentales previos sin hash; un
        # manifest con hash ya quedó rechazado arriba.
        return not manifest_hash
    return sha256_file(audio_path) == committed_hash


def commit_transcript_chunk(target: str | Path, chunk_id: str,
                            lines: list[dict], metadata: dict | None = None,
                            materialize: bool = True) -> dict:
    """Publica un chunk completo de una vez, incluidos chunks sin voz."""
    class_dir = _class_dir(target)
    path = _commit_path(class_dir, chunk_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    latest = latest_by_chunk(class_dir)
    # Primer commit válido gana. Es importante comprobarlo antes de construir un
    # segundo payload: un callback tardío o un reinicio nunca debe sustituirlo.
    existing = read_transcript_commit(class_dir, chunk_id)
    if existing is not None:
        if materialize:
            materialize_transcript(class_dir, verify_files=False)
        return existing
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chunk_id": str(chunk_id),
        "status": "complete",
        "committed_at": time.time(),
        "lines": [dict(line) for line in lines],
        "metadata": dict(metadata or {}),
    }
    if not _commit_matches_audio(
            class_dir, payload, latest=latest, verify_file=True):
        raise ValueError(f"audio cambio antes del commit STT: {chunk_id}")
    tmp = path.with_name(
        f"{path.name}.part.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        # Hard-link publica el archivo completo con semántica create-if-absent en
        # NTFS y POSIX. A diferencia de os.replace, no puede pisar un ganador.
        os.link(tmp, path)
    except FileExistsError:
        winner = read_transcript_commit(class_dir, chunk_id)
        if winner is not None:
            payload = winner
        else:
            # No pisar siquiera un archivo inválido: podría cambiar entre la
            # inspección y un replace. Se conserva y el WAV sigue pendiente.
            raise RuntimeError(
                f"commit STT existente inválido; no se sobrescribe: {chunk_id}")
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    if materialize:
        # El payload nuevo ya se validó contra bytes reales. Para evitar O(N²),
        # la proyección compara contratos/hash de commits previos sin rehashearlos.
        materialize_transcript(class_dir, verify_files=False)
    return payload


def _commit_files(target: str | Path) -> list[tuple[Path, dict]]:
    journal = _class_dir(target) / JOURNAL_DIR
    out = []
    if not journal.is_dir():
        return out
    for path in journal.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("status") == "complete":
            out.append((path, data))
    return out


def _legacy_transcript_lines(target: str | Path) -> list[dict]:
    transcript = _class_dir(target) / "transcript.jsonl"
    if not transcript.is_file():
        return []
    rows = []
    for raw in transcript.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _manifest_managed_chunks(target: str | Path) -> set[str]:
    """Chunks nuevos cuyo estado canonico exige un commit transaccional.

    Los eventos `inferred_legacy` creados por versiones previas son una migracion
    incompleta: no deben convertir retroactivamente su transcript en parcial.
    """
    return {
        chunk_id
        for chunk_id, row in latest_by_chunk(target).items()
        if row.get("status") in FINAL_CAPTURE_STATES
        and not row.get("inferred_legacy")
        and bool(row.get("transactional_stt_required") or row.get("sha256"))
    }


def _journal_chunk_ids(target: str | Path) -> set[str]:
    journal = _class_dir(target) / JOURNAL_DIR
    if not journal.is_dir():
        return set()
    chunk_ids: set[str] = set()
    for path in journal.iterdir():
        # Los commits usan temporales unicos ``<chunk>.json.part.<owner>``
        # para que dos procesos nunca compartan ni sobrescriban el mismo
        # staging file. Un temporal huérfano sigue reservando el chunk: no se
        # debe rescatar una línea legacy y confundirla con un commit remoto.
        if ".json.part" in path.name:
            chunk_ids.add(path.name.split(".json.part", 1)[0])
        elif path.name.endswith(".json"):
            chunk_ids.add(path.name[:-len(".json")])
    return chunk_ids


def _preserved_legacy_lines(target: str | Path,
                            committed_ids: set[str]) -> list[dict]:
    managed = _manifest_managed_chunks(target)
    journal_ids = _journal_chunk_ids(target)
    return [
        line for line in _legacy_transcript_lines(target)
        if (chunk_id := Path(str(line.get("chunk") or "")).stem)
        and chunk_id not in committed_ids
        and chunk_id not in managed
        and chunk_id not in journal_ids
    ]


def completed_transcript_chunks(target: str | Path,
                                verify_files: bool = True) -> set[str]:
    commits = _commit_files(target)
    latest = latest_by_chunk(target)
    complete = {
        str(data.get("chunk_id")) for _path, data in commits
        if _commit_matches_audio(target, data, latest, verify_file=verify_files)
    }
    legacy = {
        Path(str(row["chunk"])).stem
        for row in _preserved_legacy_lines(target, complete)
        if row.get("chunk")
    }
    return complete | legacy


def transcript_status_report(target: str | Path,
                             verify_files: bool = True) -> dict:
    """Clasifica STT por commit canónico, con fallback legacy conservador."""
    class_dir = _class_dir(target)
    records = capture_records(class_dir)
    paths: dict[str, str] = {}
    captured: set[str] = set()
    available: set[str] = set()
    transactional_required = False
    for row in records:
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id:
            continue
        captured.add(chunk_id)
        nombre = Path(str(row.get("path") or f"{chunk_id}.wav")).name
        paths[chunk_id] = nombre
        ruta_audio = class_dir / "audio" / nombre
        try:
            if ruta_audio.is_file() and ruta_audio.stat().st_size > 44:
                available.add(chunk_id)
        except OSError:
            pass
        transactional_required = bool(
            transactional_required
            or row.get("transactional_stt_required")
            or row.get("sha256")
        )
    # Sin manifest no existe un productor transaccional que pueda declarar un
    # WAV fallido. Se conserva la lectura legacy de nombres antiguos que no
    # cumplen el patrón moderno. Con manifest presente, el glob indiscriminado
    # reviviría residuos de capture_failed como trabajo STT.
    if not manifest_path(class_dir).is_file():
        audio_dir = class_dir / "audio"
        if audio_dir.is_dir():
            for wav_path in audio_dir.glob("*.wav"):
                captured.add(wav_path.stem)
                paths.setdefault(wav_path.stem, wav_path.name)
                try:
                    if wav_path.stat().st_size > 44:
                        available.add(wav_path.stem)
                except OSError:
                    pass

    legacy_completed = completed_transcript_chunks(
        class_dir, verify_files=verify_files)
    latest = latest_by_chunk(class_dir)
    completed: list[str] = []
    empty_completed: list[str] = []
    pending: list[str] = []
    invalid: list[str] = []
    missing_audio: list[str] = []
    invalid_reasons: dict[str, str] = {}
    states: dict[str, str] = {}
    has_journal_files = False
    committed_segment_count = 0

    for chunk_id in sorted(captured):
        commit_path = _commit_path(class_dir, chunk_id)
        partial_paths = list(commit_path.parent.glob(f"{commit_path.name}.part*"))
        data = None
        commit_error = ""
        if commit_path.is_file():
            has_journal_files = True
            try:
                data = json.loads(commit_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                commit_error = "commit_corrupt"
            if not commit_error:
                commit_error = _commit_payload_error(data, expected_chunk_id=chunk_id)
            if (not commit_error and not _commit_matches_audio(
                    class_dir, data, latest=latest, verify_file=verify_files)):
                commit_error = "commit_audio_mismatch"
        elif partial_paths:
            has_journal_files = True
            commit_error = "commit_partial_file"

        if commit_error:
            invalid.append(chunk_id)
            invalid_reasons[chunk_id] = commit_error
            states[chunk_id] = "invalid"
            if chunk_id not in available:
                missing_audio.append(chunk_id)
        elif isinstance(data, dict):
            completed.append(chunk_id)
            states[chunk_id] = "complete"
            lineas_commit = _commit_lines(data)
            committed_segment_count += len(lineas_commit)
            if not lineas_commit:
                empty_completed.append(chunk_id)
        elif chunk_id in legacy_completed:
            completed.append(chunk_id)
            states[chunk_id] = "complete_legacy"
        elif chunk_id not in available:
            missing_audio.append(chunk_id)
            states[chunk_id] = "missing_audio"
        else:
            pending.append(chunk_id)
            states[chunk_id] = "pending"

    actionable = sorted(set(pending) | (set(invalid) & available))
    unresolved = sorted(set(pending) | set(invalid) | set(missing_audio))
    if has_journal_files or transactional_required:
        source = "transcript_chunks"
    else:
        source = "legacy_transcript"
        committed_segment_count = max(
            committed_segment_count, len(_legacy_transcript_lines(class_dir)))
    return {
        "source": source,
        "captured_chunks": sorted(captured),
        "completed_chunks": sorted(completed),
        "empty_completed_chunks": sorted(empty_completed),
        "available_audio_chunks": sorted(available),
        "missing_audio_chunks": sorted(set(missing_audio)),
        "pending_chunks": sorted(pending),
        "invalid_chunks": sorted(invalid),
        "actionable_chunks": actionable,
        "unresolved_chunks": unresolved,
        "invalid_reasons": invalid_reasons,
        "chunk_states": states,
        "chunk_paths": paths,
        "captured_count": len(captured),
        "available_audio_count": len(available),
        "missing_audio_count": len(set(missing_audio)),
        "completed_count": len(completed),
        "empty_completed_count": len(empty_completed),
        "segment_count": committed_segment_count,
        "pending_count": len(pending),
        "invalid_count": len(invalid),
        "actionable_count": len(actionable),
        "unresolved_count": len(unresolved),
    }


def completion_barrier_report(
        target: str | Path, transcript_report: dict | None = None) -> dict:
    """Contrato final audio -> commits STT usado por Escriba, panel y postproceso.

    ``ready`` significa que no queda trabajo automático capaz de mejorar la
    evidencia. Un ``capture_failed`` ya terminal se conserva como degradación
    auditable, pero después del cierre explícito no puede resolverse esperando.
    """
    events = read_events(target)
    has_manifest = manifest_path(target).is_file()
    session_events = [
        (index, row) for index, row in enumerate(events)
        if str(row.get("status") or "") in CAPTURE_SESSION_STATES
    ]
    chunk_events = [
        (index, row) for index, row in enumerate(events)
        if str(row.get("chunk_id") or "")
    ]
    unfinished = unfinished_capture_report(target)
    failures = capture_failure_report(target)

    if session_events:
        session_index, session_row = session_events[-1]
        last_chunk_index = max((index for index, _row in chunk_events), default=-1)
        capture_session_closed = bool(
            session_row.get("status") == CAPTURE_SESSION_CLOSED
            and session_index >= last_chunk_index
        )
        session_contract = "explicit"
        capture_session_id = str(session_row.get("capture_session_id") or "")
    else:
        # Compatibilidad con clases anteriores al evento de cierre. Un manifest sin
        # `capturing` pendiente se considera productor ya detenido.
        capture_session_closed = unfinished["count"] == 0
        session_contract = "legacy_manifest_inferred" if has_manifest else "legacy"
        capture_session_id = ""

    stt = (
        transcript_report
        if isinstance(transcript_report, dict)
        else transcript_status_report(target, verify_files=True)
    )
    pending_stt = stt["unresolved_chunks"]
    capture_ready = bool(
        capture_session_closed
        and unfinished["terminal_count"] == 0
    )
    capture_integrity_ok = failures["terminal_count"] == 0
    stt_ready = not pending_stt
    ready = capture_ready and stt_ready
    blocking_reasons = []
    if not capture_session_closed:
        blocking_reasons.append("capture_session_not_closed")
    if unfinished["terminal_count"]:
        blocking_reasons.append(
            f"unfinished_captures={unfinished['terminal_count']}")
    if stt["pending_count"]:
        blocking_reasons.append(f"stt_pending={stt['pending_count']}")
    if stt["invalid_count"]:
        blocking_reasons.append(f"stt_invalid={stt['invalid_count']}")
    if stt["missing_audio_count"]:
        blocking_reasons.append(f"audio_missing={stt['missing_audio_count']}")
    warnings = []
    if failures["terminal_count"]:
        warnings.append(
            f"terminal_capture_failures={failures['terminal_count']}")
    reasons = [*blocking_reasons, *warnings]
    if ready and warnings:
        status = "ready_with_warnings"
    elif ready:
        status = "ready"
    elif not capture_ready:
        status = "waiting_capture"
    else:
        status = "waiting_stt"
    return {
        "ready": ready,
        "status": status,
        "reasons": reasons,
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "degraded": bool(warnings),
        "capture_ready": capture_ready,
        "capture_integrity_ok": capture_integrity_ok,
        "stt_ready": stt_ready,
        "capture_session_closed": capture_session_closed,
        "capture_session_id": capture_session_id,
        "capture_session_contract": session_contract,
        "unfinished_capture_count": unfinished["count"],
        "terminal_capture_failure_count": failures["terminal_count"],
        "recovered_capture_failure_count": failures["recovered_count"],
        "stt_state_source": stt["source"],
        "captured_chunk_count": stt["captured_count"],
        "available_audio_chunk_count": stt["available_audio_count"],
        "missing_audio_chunk_count": stt["missing_audio_count"],
        "missing_audio_chunks": stt["missing_audio_chunks"],
        "completed_stt_chunk_count": stt["completed_count"],
        "empty_completed_stt_chunk_count": stt["empty_completed_count"],
        "pending_stt_count": len(pending_stt),
        "pending_stt_chunks": pending_stt,
        "actionable_stt_count": stt["actionable_count"],
        "actionable_stt_chunks": stt["actionable_chunks"],
        "missing_stt_count": stt["pending_count"],
        "missing_stt_chunks": stt["pending_chunks"],
        "invalid_stt_count": stt["invalid_count"],
        "invalid_stt_chunks": stt["invalid_chunks"],
    }


def materialize_transcript(target: str | Path,
                           verify_files: bool = True) -> int:
    """Reconstruye ``transcript.jsonl`` exclusivamente desde commits completos."""
    class_dir = _class_dir(target)
    latest = latest_by_chunk(class_dir)
    commits = [
        data for _path, data in _commit_files(class_dir)
        if _commit_matches_audio(
            class_dir, data, latest, verify_file=verify_files)
    ]
    commits.sort(key=lambda data: (
        _float(data.get("metadata", {}).get("wall_start"), float("inf")),
        _float(data.get("metadata", {}).get("sequence"), float("inf")),
        str(data.get("chunk_id") or ""),
    ))
    committed_ids = {str(commit.get("chunk_id") or "") for commit in commits}
    lines = []
    lines.extend(_preserved_legacy_lines(class_dir, committed_ids))
    for commit in commits:
        lines.extend(_commit_lines(commit))
    lines.sort(key=lambda line: _float(line.get("t"), float("inf")))
    transcript = class_dir / "transcript.jsonl"
    tmp = transcript.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, transcript)
    return len(lines)


def record_transcription_attempt(target: str | Path, chunk_id: str, status: str,
                                 **metadata) -> dict:
    observed = time.time()
    state = str(metadata.pop("state", status) or status)
    attempt_id = str(metadata.pop("attempt_id", "") or (
        f"{chunk_id}:{state}:{time.time_ns()}"))
    started_at = metadata.pop("started_at", None)
    ended_at = metadata.pop("ended_at", None)
    terminal = state in {
        "complete", "completed", "failed", "provider_failed",
        "provider_unavailable", "chain_exhausted", "pending_remote_stt",
        "submission_unknown",
    }
    row = {
        "schema_version": SCHEMA_VERSION,
        "chunk_id": str(chunk_id),
        "status": str(status),
        "state": state,
        "attempt_id": attempt_id,
        "observed_at": observed,
        "started_at": float(started_at) if started_at is not None else observed,
        "ended_at": (
            float(ended_at) if ended_at is not None
            else observed if terminal else None),
        "error_class": str(metadata.get("error_class") or ""),
        **metadata,
    }
    _append_jsonl(_class_dir(target) / JOURNAL_DIR / ATTEMPTS_NAME, row)
    return row


def transcription_attempts(target: str | Path) -> list[dict]:
    path = transcription_attempts_path(target)
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def transcription_attempts_path(target: str | Path) -> Path:
    return _class_dir(target) / JOURNAL_DIR / ATTEMPTS_NAME


def failed_attempt_counts(target: str | Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    current_hashes = {
        chunk_id: str(row.get("sha256") or "")
        for chunk_id, row in latest_by_chunk(target).items()
    }
    for row in transcription_attempts(target):
        chunk_id = str(row.get("chunk_id") or "")
        current_hash = current_hashes.get(chunk_id, "")
        attempt_hash = str(row.get("input_sha256") or "")
        if current_hash and attempt_hash != current_hash:
            continue
        if row.get("status") == "complete":
            counts.pop(chunk_id, None)
        elif row.get("status") == "failed":
            counts[chunk_id] = counts.get(chunk_id, 0) + 1
    return counts


def transcription_retry_state(target: str | Path, now: float | None = None,
                              base_delay: float = 30.0,
                              max_delay: float = 300.0) -> dict[str, dict]:
    """Estado retryable persistente; ningun fallo convierte un WAV en terminal."""
    return transcription_retry_state_from_rows(
        read_events(target), transcription_attempts(target), now=now,
        base_delay=base_delay, max_delay=max_delay)


def transcription_retry_state_from_rows(
        manifest_rows: list[dict], attempt_rows: list[dict],
        now: float | None = None, base_delay: float = 30.0,
        max_delay: float = 300.0) -> dict[str, dict]:
    """Calcula reintentos desde snapshots ya leidos por un consumidor incremental."""
    now = time.time() if now is None else float(now)
    current_hashes = {
        chunk_id: str(row.get("sha256") or "")
        for chunk_id, row in latest_by_chunk_rows(manifest_rows).items()
    }
    state: dict[str, dict] = {}
    for row in attempt_rows:
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id:
            continue
        current_hash = current_hashes.get(chunk_id, "")
        attempt_hash = str(row.get("input_sha256") or "")
        if current_hash and attempt_hash != current_hash:
            continue
        status = str(row.get("status") or "")
        state_name = str(row.get("state") or status)
        if status == "complete" or state_name in {"complete", "completed"}:
            state.pop(chunk_id, None)
            continue
        if status not in {"failed", "pending_remote_stt", "pending_remote_job"} \
                and state_name not in {
                    "failed", "pending_remote_stt", "pending_remote_job",
                    "submission_unknown", "chain_exhausted",
                }:
            continue
        previous = state.get(chunk_id, {})
        failures = int(previous.get("failures", 0)) + 1
        exponent = min(failures - 1, 20)
        delay = min(max(0.0, max_delay),
                    max(0.0, base_delay) * (2 ** exponent))
        failed_at = _float(row.get("observed_at"), now)
        explicit_retry = row.get("retry_after")
        retry_at = (
            max(failed_at, _float(explicit_retry, failed_at + delay))
            if explicit_retry is not None else failed_at + delay)
        retryable = bool(row.get("retryable", True))
        state[chunk_id] = {
            "failures": failures,
            "retryable": retryable,
            "last_failed_at": failed_at,
            "retry_after": retry_at,
            "ready": retryable and now >= retry_at,
            "state": state_name,
            "provider": str(row.get("provider") or ""),
            "request_id": str(row.get("request_id") or ""),
        }
    return state
