"""Borradores progresivos para intervenciones locales del estudiante.

El navegador abre el micrófono solo por una acción explícita. Los fragmentos se
guardan de forma ordenada e idempotente fuera del audio canónico de la clase.
Un borrador no aparece en la lección hasta que el estudiante lo guarda; al
descartarlo no queda una nota publicada.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import student_notes


SCHEMA_VERSION = 1
ROOT_NAME = "local_intervention_drafts"
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_DRAFT_BYTES = student_notes.MAX_ORIGINAL_BYTES
MAX_CHUNKS = 7200
_LOCK = threading.RLock()


def root(class_dir: Path | str) -> Path:
    return Path(class_dir) / ROOT_NAME


def _draft_dir(class_dir: Path | str, draft_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(str(draft_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("identificador de borrador inválido") from exc
    return root(class_dir) / normalized


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        .encode("utf-8"),
    )


def _metadata_path(directory: Path) -> Path:
    return directory / "draft.json"


def _load(class_dir: Path | str, draft_id: str) -> tuple[Path, dict]:
    directory = _draft_dir(class_dir, draft_id)
    path = _metadata_path(directory)
    if not path.is_file():
        raise KeyError("borrador de intervención no encontrado")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("metadata del borrador ilegible") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("metadata del borrador incompatible")
    return directory, payload


def _public(payload: dict) -> dict:
    return {
        key: value for key, value in payload.items()
        if key not in {"device_id"}
    }


def _extension(mime: str) -> str:
    value = str(mime or "").casefold()
    if "wav" in value:
        return ".wav"
    if "ogg" in value:
        return ".ogg"
    if "mp4" in value or "m4a" in value:
        return ".m4a"
    return ".webm"


def create_draft(class_dir: Path | str, *, mime: str, device_id: str,
                 device_label: str, class_seconds: float) -> dict:
    value_mime = str(mime or "audio/webm").strip()
    if not value_mime.casefold().startswith("audio/"):
        raise ValueError("el borrador debe contener audio")
    draft_id = str(uuid.uuid4())
    directory = _draft_dir(class_dir, draft_id)
    directory.mkdir(parents=True, exist_ok=False)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "draft_id": draft_id,
        "status": "recording",
        "mime": value_mime,
        "extension": _extension(value_mime),
        "device_id": str(device_id or ""),
        "device_label": str(device_label or "Micrófono seleccionado"),
        "class_seconds": round(max(0.0, float(class_seconds or 0.0)), 3),
        "created_at": time.time(),
        "next_sequence": 0,
        "bytes": 0,
        "sha256": "",
        "preview_path": "",
    }
    _atomic_json(_metadata_path(directory), payload)
    return _public(payload)


def append_chunk(class_dir: Path | str, draft_id: str, sequence: int,
                 data: bytes) -> dict:
    try:
        seq = int(sequence)
    except (TypeError, ValueError) as exc:
        raise ValueError("secuencia de fragmento inválida") from exc
    raw = bytes(data)
    if seq < 0 or seq >= MAX_CHUNKS:
        raise ValueError("secuencia de fragmento fuera de rango")
    if not raw:
        raise ValueError("fragmento de audio vacío")
    if len(raw) > MAX_CHUNK_BYTES:
        raise ValueError("fragmento de audio demasiado grande")
    digest = hashlib.sha256(raw).hexdigest()
    with _LOCK:
        directory, payload = _load(class_dir, draft_id)
        if payload.get("status") != "recording":
            raise RuntimeError("el borrador ya no acepta fragmentos")
        expected = int(payload.get("next_sequence", 0) or 0)
        chunk = directory / "chunks" / f"{seq:06d}.part"
        if seq < expected:
            if chunk.is_file() and hashlib.sha256(chunk.read_bytes()).hexdigest() == digest:
                return {"ok": True, "stored": False, "sequence": seq,
                        "next_sequence": expected}
            raise ValueError("colisión de fragmento ya recibido")
        if seq != expected:
            raise ValueError(
                f"secuencia no contigua: se esperaba {expected} y llegó {seq}")
        total = int(payload.get("bytes", 0) or 0) + len(raw)
        if total > MAX_DRAFT_BYTES:
            raise ValueError("la intervención excede el límite de 64 MiB; detén y guarda")
        _atomic_bytes(chunk, raw)
        payload["next_sequence"] = expected + 1
        payload["bytes"] = total
        _atomic_json(_metadata_path(directory), payload)
        return {"ok": True, "stored": True, "sequence": seq,
                "next_sequence": expected + 1}


def seal_draft(class_dir: Path | str, draft_id: str) -> dict:
    with _LOCK:
        directory, payload = _load(class_dir, draft_id)
        if payload.get("status") == "sealed":
            return _public(payload)
        if payload.get("status") != "recording":
            raise RuntimeError("estado de borrador inválido")
        count = int(payload.get("next_sequence", 0) or 0)
        chunks = directory / "chunks"
        expected = [chunks / f"{index:06d}.part" for index in range(count)]
        if count <= 0 or any(not path.is_file() for path in expected):
            raise ValueError("no hay una secuencia completa de audio para sellar")
        final = directory / f"recording{payload.get('extension') or '.webm'}"
        temporary = final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as output:
                for path in expected:
                    with path.open("rb") as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            output.write(block)
                            digest.update(block)
                            size += len(block)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, final)
        finally:
            temporary.unlink(missing_ok=True)
        if size != int(payload.get("bytes", -1)):
            final.unlink(missing_ok=True)
            raise RuntimeError("el tamaño sellado no coincide con los fragmentos")
        payload.update({
            "status": "sealed", "sealed_at": time.time(),
            "sha256": digest.hexdigest(),
            "preview_path": final.relative_to(Path(class_dir)).as_posix(),
        })
        _atomic_json(_metadata_path(directory), payload)
        shutil.rmtree(chunks, ignore_errors=True)
        return _public(payload)


def commit_draft(class_dir: Path | str, draft_id: str) -> dict:
    with _LOCK:
        directory, payload = _load(class_dir, draft_id)
        if payload.get("status") != "sealed":
            raise RuntimeError("detén la intervención antes de guardarla")
        recording = Path(class_dir) / str(payload.get("preview_path") or "")
        if not recording.is_file():
            raise RuntimeError("falta el audio sellado del borrador")
        recording_bytes = recording.read_bytes()
        if hashlib.sha256(recording_bytes).hexdigest() != payload.get("sha256"):
            raise RuntimeError("el hash del audio sellado no coincide")
        note = student_notes.capture_file(
            class_dir,
            recording_bytes,
            original_name=f"intervencion-propia{payload.get('extension') or '.webm'}",
            mime=str(payload.get("mime") or "audio/webm"),
            source_kind="local_intervention",
            user_action="manual_record",
            class_seconds=float(payload.get("class_seconds", 0) or 0),
            source_details={
                "device_id": str(payload.get("device_id") or ""),
                "device_label": str(payload.get("device_label") or ""),
                "draft_sha256": str(payload.get("sha256") or ""),
            },
        )
        note = student_notes.set_inclusion(
            class_dir, note["note_id"], "included", target="lesson")
        shutil.rmtree(directory)
        _prune_root(class_dir)
        return note


def discard_draft(class_dir: Path | str, draft_id: str) -> dict:
    with _LOCK:
        directory = _draft_dir(class_dir, draft_id)
        existed = directory.exists()
        if existed:
            shutil.rmtree(directory)
        _prune_root(class_dir)
        return {"ok": True, "discarded": existed, "draft_id": str(draft_id)}


def _prune_root(class_dir: Path | str) -> None:
    directory = root(class_dir)
    try:
        directory.rmdir()
    except OSError:
        pass


def list_drafts(class_dir: Path | str) -> list[dict]:
    base = root(class_dir)
    if not base.is_dir():
        return []
    rows = []
    for directory in sorted(base.iterdir()):
        if not directory.is_dir():
            continue
        try:
            _directory, payload = _load(class_dir, directory.name)
        except (KeyError, RuntimeError, ValueError):
            continue
        rows.append(_public(payload))
    return rows


def preview_path(class_dir: Path | str, draft_id: str) -> Path:
    _directory, payload = _load(class_dir, draft_id)
    if payload.get("status") != "sealed":
        raise RuntimeError("el borrador todavía no tiene vista previa")
    relative = Path(str(payload.get("preview_path") or ""))
    base = Path(class_dir).resolve()
    target = (Path(class_dir) / relative).resolve()
    if base not in target.parents or not target.is_file():
        raise RuntimeError("vista previa de audio ausente")
    return target


def pending_report(class_dir: Path | str) -> dict:
    drafts = list_drafts(class_dir)
    return {
        "blocking": bool(drafts),
        "count": len(drafts),
        "draft_ids": [str(item.get("draft_id")) for item in drafts],
        "statuses": {str(item.get("draft_id")): str(item.get("status"))
                     for item in drafts},
    }
