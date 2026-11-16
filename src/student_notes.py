"""Persistencia auditable para aportes del estudiante.

El namespace es deliberadamente independiente de audio/transcript docente. Los
originales son content-addressed, el manifest es append-only y ``index.json`` es una
vista reconstruible. La captura solo ocurre por una acción explícita del estudiante.
"""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
import wave
import zipfile
from pathlib import Path
from xml.etree import ElementTree

SCHEMA_VERSION = 1
ROOT_NAME = "student_notes"
PACKAGED_ROOT = Path("img") / ".student_notes"
MANIFEST_NAME = "manifest.jsonl"
INDEX_NAME = "index.json"
MAX_ORIGINAL_BYTES = 64 * 1024 * 1024
ALLOWED_SUFFIXES = {
    ".txt", ".md", ".csv", ".json", ".pdf", ".doc", ".docx", ".pptx",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
    ".wav", ".webm", ".ogg", ".m4a", ".mp3", ".flac",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_SUFFIXES = {".wav", ".webm", ".ogg", ".m4a", ".mp3", ".flac"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json"}
_APPEND_LOCK = threading.RLock()
_TRANSCRIBE_LOCK = threading.Lock()


def transcription_in_progress() -> bool:
    """Permite bloquear administración de contenido durante una nota remota."""
    return _TRANSCRIBE_LOCK.locked()


def root(class_dir: Path | str) -> Path:
    class_path = Path(class_dir)
    legacy = class_path / ROOT_NAME
    packaged = class_path / PACKAGED_ROOT
    if legacy.exists() or not packaged.exists():
        return legacy
    return packaged


def _relative_root(class_dir: Path | str) -> Path:
    return root(class_dir).relative_to(Path(class_dir))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _atomic_json(path: Path, payload: dict) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        .encode("utf-8"),
    )


def _append_event(class_dir: Path | str, event: dict) -> dict:
    row = dict(event)
    row.setdefault("schema_version", SCHEMA_VERSION)
    row.setdefault("event_id", str(uuid.uuid4()))
    row.setdefault("recorded_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    path = root(class_dir) / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8")
    with _APPEND_LOCK:
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        rebuild_index(class_dir)
    return row


def _events(class_dir: Path | str) -> list[dict]:
    path = root(class_dir) / MANIFEST_NAME
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(row, dict) and row.get("note_id"):
            rows.append(row)
    return rows


def rebuild_index(class_dir: Path | str) -> dict:
    notes: dict[str, dict] = {}
    for event in _events(class_dir):
        note_id = str(event["note_id"])
        current = notes.get(note_id)
        if current is None or int(event.get("revision", 0)) >= int(
                current.get("revision", 0)):
            notes[note_id] = event
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rebuilt_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notes": notes,
    }
    _atomic_json(root(class_dir) / INDEX_NAME, payload)
    return payload


def load_index(class_dir: Path | str) -> dict:
    manifest = root(class_dir) / MANIFEST_NAME
    index = root(class_dir) / INDEX_NAME
    if not manifest.is_file():
        return {"schema_version": SCHEMA_VERSION, "notes": {}}
    if index.is_file() and index.stat().st_mtime_ns >= manifest.stat().st_mtime_ns:
        try:
            payload = json.loads(index.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("notes"), dict):
                return payload
        except (OSError, ValueError, TypeError):
            pass
    # El manifest manda. Reconstruir repara una vista ausente/truncada.
    return rebuild_index(class_dir)


def _safe_suffix(original_name: str, mime: str) -> str:
    suffix = Path(str(original_name or "")).suffix.casefold()
    if not suffix:
        suffix = (mimetypes.guess_extension(str(mime or "").split(";", 1)[0]) or "")
    if suffix == ".jpe":
        suffix = ".jpg"
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            "tipo de archivo no admitido; usa texto, imagen, PDF, DOC/DOCX/PPTX o audio")
    return suffix


def _validate_original(data: bytes, suffix: str) -> None:
    if not data:
        raise ValueError("archivo vacío")
    if len(data) > MAX_ORIGINAL_BYTES:
        raise ValueError(f"archivo excede {MAX_ORIGINAL_BYTES // (1024 * 1024)} MiB")
    if suffix == ".pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("el contenido no corresponde a un PDF")
    if suffix in {".docx", ".pptx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise ValueError("documento Office inválido") from exc
        required = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
        # Algunos fixtures mínimos no llevan presentation.xml, pero sí slides válidas.
        if required not in names and not (
                suffix == ".pptx" and any(name.startswith("ppt/slides/slide") for name in names)):
            raise ValueError("documento Office incompleto")
    if suffix in IMAGE_SUFFIXES:
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except Exception as exc:
            raise ValueError("imagen inválida o corrupta") from exc


def _extract_office_xml(path: Path, prefix: str, text_tag: str) -> str:
    pieces: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            (name for name in archive.namelist()
             if name.startswith(prefix) and name.endswith(".xml")),
            key=lambda name: [
                int(part) if part.isdigit() else part.casefold()
                for part in re.split(r"(\d+)", name)
            ],
        )
        for name in names:
            try:
                xml = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                continue
            values = [str(node.text or "").strip() for node in xml.iter()
                      if node.tag.endswith(text_tag) and str(node.text or "").strip()]
            if values:
                pieces.append(" ".join(values))
    return "\n".join(pieces).strip()


def _ocr_image(path: Path) -> str:
    from PIL import Image

    from . import ocr
    with Image.open(path) as image:
        return str(ocr.texto_de_pil(image.convert("RGB")) or "").strip()


def _extract_pdf(path: Path) -> str:
    from .visual_materials import preflight_pdf_renderer
    pdfium = preflight_pdf_renderer()
    document = pdfium.PdfDocument(str(path))
    pages: list[str] = []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    text = str(text_page.get_text_range() or "").strip()
                finally:
                    text_page.close()
                if text:
                    pages.append(f"[Página {index + 1}]\n{text}")
            finally:
                page.close()
    finally:
        document.close()
    return "\n\n".join(pages)


def _extract(path: Path, suffix: str) -> dict:
    try:
        if suffix in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
            kind = "text"
            engine = "python-utf8"
        elif suffix in IMAGE_SUFFIXES:
            text = _ocr_image(path)
            kind = "ocr"
            engine = "clase-viva-ocr"
        elif suffix == ".pdf":
            text = _extract_pdf(path)
            kind = "document_text"
            engine = "pypdfium2"
        elif suffix == ".docx":
            text = _extract_office_xml(path, "word/", "}t")
            kind = "document_text"
            engine = "ooxml"
        elif suffix == ".pptx":
            text = _extract_office_xml(path, "ppt/slides/slide", "}t")
            kind = "document_text"
            engine = "ooxml"
        elif suffix in AUDIO_SUFFIXES:
            return {
                "status": "pending", "kind": "stt", "engine": "large-v3-turbo",
                "text": "", "derived_paths": [],
            }
        else:
            return {
                "status": "pending", "kind": "document_text", "engine": "manual",
                "text": "", "derived_paths": [],
                "error": "formato conservado; extracción automática no disponible",
            }
        return {
            "status": "ready", "kind": kind, "engine": engine,
            "text": text, "derived_paths": [],
        }
    except Exception as exc:
        return {
            "status": "failed", "kind": "ocr" if suffix in IMAGE_SUFFIXES
            else "document_text", "engine": "clase-viva",
            "text": "", "derived_paths": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _base_note(*, note_id: str, source: dict, original: dict,
               extraction: dict, class_seconds: float) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "note_id": note_id,
        "event_type": "captured",
        "revision": 1,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "class_time": {
            "mode": "relative", "seconds": round(max(0.0, float(class_seconds)), 3),
        },
        "source": source,
        "original": original,
        "extraction": extraction,
        "correction": {"text": "", "based_on_revision": 0, "edited_at": "",
                       "editor": "student"},
        # Guardar/seleccionar es una acción explícita. La nota se incluye por
        # defecto y puede excluirse o descartarse antes de consolidar.
        "inclusion": {
            "state": "included", "target": "lesson",
            "reason": "explicit_capture",
        },
    }


def capture_file(class_dir: Path | str, data: bytes, *, original_name: str,
                 mime: str, source_kind: str, user_action: str,
                 class_seconds: float = 0.0,
                 source_details: dict | None = None) -> dict:
    payload = bytes(data)
    suffix = _safe_suffix(original_name, mime)
    _validate_original(payload, suffix)
    digest = _sha256(payload)
    relative = _relative_root(class_dir) / "originals" / f"{digest}{suffix}"
    original_path = Path(class_dir) / relative
    if not original_path.exists():
        _atomic_bytes(original_path, payload)
    note_id = str(uuid.uuid4())
    extraction = _extract(original_path, suffix)
    note = _base_note(
        note_id=note_id,
        source={
            "kind": str(source_kind), "user_action": str(user_action),
            "original_name": str(original_name or ""),
            "mime": str(mime or "application/octet-stream"),
            **{
                str(key): str(value)
                for key, value in dict(source_details or {}).items()
                if str(key) not in {"kind", "user_action", "original_name", "mime"}
            },
        },
        original={
            "sha256": digest, "path": relative.as_posix(), "size_bytes": len(payload),
        },
        extraction=extraction,
        class_seconds=class_seconds,
    )
    return _append_event(class_dir, note)


def capture_text(class_dir: Path | str, text: str, *,
                 class_seconds: float = 0.0) -> dict:
    value = str(text or "").strip()
    if not value:
        raise ValueError("nota vacía")
    note = capture_file(
        class_dir, value.encode("utf-8"), original_name="nota.txt",
        mime="text/plain; charset=utf-8", source_kind="free_text",
        user_action="type", class_seconds=class_seconds,
    )
    return note


def _current(class_dir: Path | str, note_id: str) -> dict:
    note = load_index(class_dir).get("notes", {}).get(str(note_id))
    if not isinstance(note, dict):
        raise KeyError("nota no encontrada")
    return dict(note)


def _revise(class_dir: Path | str, note: dict, event_type: str) -> dict:
    note = dict(note)
    note["event_id"] = str(uuid.uuid4())
    note["event_type"] = event_type
    note["revision"] = int(note.get("revision", 0)) + 1
    note["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return _append_event(class_dir, note)


def correct_note(class_dir: Path | str, note_id: str, text: str) -> dict:
    note = _current(class_dir, note_id)
    note["correction"] = {
        "text": str(text or "").strip(),
        "based_on_revision": int(note.get("revision", 0)),
        "edited_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "editor": "student",
    }
    return _revise(class_dir, note, "corrected")


def set_inclusion(class_dir: Path | str, note_id: str, state: str, *,
                  target: str = "lesson", reason: str = "") -> dict:
    state = str(state or "").strip()
    if state not in {"pending", "included", "excluded"}:
        raise ValueError("estado de inclusión inválido")
    if state == "excluded" and not str(reason or "").strip():
        previous = _current(class_dir, note_id)
        if previous.get("inclusion", {}).get("state") == "included":
            raise ValueError("excluir una nota publicada requiere motivo")
    note = _current(class_dir, note_id)
    note["inclusion"] = {
        "state": state,
        "target": str(target if state == "included" else "none"),
        "reason": str(reason or "").strip(),
    }
    return _revise(class_dir, note, "inclusion_changed")


def discard_note(class_dir: Path | str, note_id: str, *,
                 reason: str = "student_discarded") -> dict:
    """Oculta una nota sin borrar su original ni romper el manifest append-only."""
    note = _current(class_dir, note_id)
    note["inclusion"] = {
        "state": "discarded",
        "target": "none",
        "reason": str(reason or "student_discarded").strip(),
    }
    return _revise(class_dir, note, "discarded")


def _write_wav(path: Path, pcm: bytes, rate: int = 16000) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    _atomic_bytes(path, buffer.getvalue())


def _to_mono16k(source: Path, target: Path) -> None:
    import numpy as np
    if source.suffix.casefold() == ".wav":
        with wave.open(str(source), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        if width != 2:
            raise ValueError("WAV de nota debe usar PCM de 16 bits")
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
    else:
        import av
        chunks = []
        with av.open(str(source)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                converted = resampler.resample(frame)
                converted = converted if isinstance(converted, list) else [converted]
                for item in converted:
                    chunks.append(item.to_ndarray().reshape(-1).astype(np.float32))
        if not chunks:
            raise ValueError("audio sin frames decodificables")
        samples = np.concatenate(chunks)
        rate = 16000
    if rate != 16000 and samples.size:
        source_axis = np.arange(samples.size, dtype=np.float64)
        target_size = max(1, round(samples.size * 16000 / rate))
        target_axis = np.linspace(0, max(0, samples.size - 1), target_size)
        samples = np.interp(target_axis, source_axis, samples)
    pcm = np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()
    _write_wav(target, pcm, 16000)


def _transcribe_wav(path: Path, *, model=None, model_name: str = "",
                    backend_name: str | None = None,
                    remote_consent: bool = False) -> dict:
    if model is None:
        from . import stt_backend
        model, backend_info = stt_backend.create_backend(
            backend=backend_name, remote_consent=remote_consent)
        model_name = str(backend_info.get("model") or "remote-failover")
    if hasattr(model, "set_attempt_context"):
        model.set_attempt_context(
            chunk_id=f"student-note:{path.stem}", audio_sha256="",
            sequence=None, history=[], recorder=None)
    segments, info = model.transcribe(
        str(path), language=None, vad_filter=True, beam_size=5,
        condition_on_previous_text=False,
    )
    text = " ".join(str(segment.text or "").strip() for segment in segments).strip()
    provider = str(
        getattr(model, "last_provider", "")
        or
        getattr(model, "provider", "")
        or getattr(info, "provider", "") or "remote-chain")
    effective_model = str(
        getattr(model, "last_model", "") or getattr(info, "model", "")
        or model_name or "remote-failover")
    engine = f"{provider}:{effective_model}"
    return {
        "text": text,
        "engine": engine,
        "language": str(getattr(info, "language", "") or ""),
    }


def transcribe_audio_note(class_dir: Path | str, note_id: str, *,
                          teacher_capture_active: bool,
                          model=None, model_name: str = "",
                          backend_name: str | None = None,
                          remote_consent: bool = False) -> dict:
    if teacher_capture_active and model is None:
        # Con STT remoto la nota se transcribe durante la clase: el
        # transcriptor la toma en cuanto su cola docente queda libre y le pasa
        # su propio backend. Lo que sigue prohibido es abrir un segundo backend
        # en paralelo al del transcriptor mientras la captura está activa.
        raise RuntimeError(
            "durante la captura la nota de voz se transcribe con el backend "
            "del transcriptor; no se abre un segundo backend STT")
    if not _TRANSCRIBE_LOCK.acquire(blocking=False):
        raise RuntimeError("ya hay una nota de voz en transcripción")
    try:
        note = _current(class_dir, note_id)
        source = note.get("source", {})
        if source.get("kind") not in {"microphone", "local_intervention"} and Path(
                str(note.get("original", {}).get("path", ""))).suffix.casefold() \
                not in AUDIO_SUFFIXES:
            raise ValueError("la nota no contiene audio")
        original = Path(class_dir) / str(note["original"]["path"])
        # La nota original permanece intacta. El derivado WAV evita diferencias
        # de codecs entre proveedores y permite aplicar el mismo gate de silencio.
        relative = _relative_root(class_dir) / "derived" / note_id / "audio.wav"
        derived = Path(class_dir) / relative
        derived.parent.mkdir(parents=True, exist_ok=True)
        _to_mono16k(original, derived)
        result = (
            _transcribe_wav(
                derived, backend_name=backend_name,
                remote_consent=remote_consent)
            if model is None else
            _transcribe_wav(derived, model=model, model_name=model_name)
        )
        derived_paths = [relative.as_posix()]
        note["extraction"] = {
            "status": "ready", "kind": "stt", "engine": result["engine"],
            "text": result["text"], "language": result.get("language", ""),
            "derived_paths": derived_paths,
        }
        return _revise(class_dir, note, "extracted")
    except Exception as exc:
        try:
            note = _current(class_dir, note_id)
            note["extraction"] = {
                **dict(note.get("extraction", {})),
                "status": "failed", "error": f"{type(exc).__name__}: {exc}",
            }
            _revise(class_dir, note, "failed")
        except Exception:
            pass
        raise
    finally:
        _TRANSCRIBE_LOCK.release()


def relocate_to_study_package(class_dir: Path | str) -> Path | None:
    """Mueve notas originales bajo ``img/`` sin perder su manifest auditable."""
    class_path = Path(class_dir)
    legacy = class_path / ROOT_NAME
    packaged = class_path / PACKAGED_ROOT
    if not legacy.exists():
        return packaged if packaged.exists() else None
    if packaged.exists():
        raise RuntimeError(
            "existen simultáneamente notas legacy y empaquetadas; no se elimina ninguna")
    packaged.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy), str(packaged))

    index = load_index(class_path)
    for current in list(index.get("notes", {}).values()):
        note = dict(current)
        original = dict(note.get("original", {}))
        original_path = str(original.get("path", "") or "")
        if original_path.startswith(f"{ROOT_NAME}/"):
            original["path"] = (
                f"{PACKAGED_ROOT.as_posix()}/"
                f"{original_path[len(ROOT_NAME) + 1:]}"
            )
            note["original"] = original
        extraction = dict(note.get("extraction", {}))
        derived = []
        for raw in extraction.get("derived_paths", []) or []:
            value = str(raw)
            if value.startswith(f"{ROOT_NAME}/"):
                value = (
                    f"{PACKAGED_ROOT.as_posix()}/"
                    f"{value[len(ROOT_NAME) + 1:]}"
                )
            derived.append(value)
        extraction["derived_paths"] = derived
        note["extraction"] = extraction
        _revise(class_path, note, "packaged")
    return packaged


def list_notes(class_dir: Path | str, *, include_discarded: bool = False) -> list[dict]:
    notes = list(load_index(class_dir).get("notes", {}).values())
    if not include_discarded:
        notes = [
            note for note in notes
            if note.get("inclusion", {}).get("state") != "discarded"
        ]
    return sorted(notes, key=lambda note: (
        float(note.get("class_time", {}).get("seconds", 0) or 0),
        str(note.get("recorded_at", "")), str(note.get("note_id", "")),
    ))


def pending_audio_notes(class_dir: Path | str, *,
                        source_kinds: set[str] | None = None,
                        included_only: bool = True) -> list[dict]:
    allowed = set(source_kinds or {"microphone", "local_intervention"})
    rows = []
    for note in list_notes(class_dir):
        source = dict(note.get("source", {}))
        extraction = dict(note.get("extraction", {}))
        inclusion = dict(note.get("inclusion", {}))
        suffix = Path(str(note.get("original", {}).get("path", ""))).suffix.casefold()
        if source.get("kind") not in allowed and suffix not in AUDIO_SUFFIXES:
            continue
        if extraction.get("status") != "pending":
            continue
        if included_only and inclusion.get("state") != "included":
            continue
        rows.append(note)
    return rows


def local_intervention_segments(class_dir: Path | str) -> list[dict]:
    """Vista pedagógica separada; nunca modifica el transcript docente."""
    segments = []
    for note in list_notes(class_dir):
        if note.get("source", {}).get("kind") != "local_intervention":
            continue
        if note.get("inclusion", {}).get("state") != "included":
            continue
        corrected = str(note.get("correction", {}).get("text", "") or "").strip()
        extracted = str(note.get("extraction", {}).get("text", "") or "").strip()
        text = corrected or extracted
        if not text:
            continue
        seconds = max(0.0, float(note.get("class_time", {}).get("seconds", 0) or 0))
        segments.append({
            "t": seconds,
            "t_rel": seconds,
            "ini": seconds,
            "fin": seconds,
            "texto": text,
            "source_role": "student_local_intervention",
            "speaker_label": "Intervención del estudiante",
            "student_note_id": str(note.get("note_id") or ""),
        })
    return sorted(segments, key=lambda row: (
        float(row.get("t_rel", 0) or 0), str(row.get("student_note_id", ""))))


def unresolved_local_interventions(class_dir: Path | str) -> list[dict]:
    unresolved = []
    for note in list_notes(class_dir):
        if note.get("source", {}).get("kind") != "local_intervention":
            continue
        if note.get("inclusion", {}).get("state") != "included":
            continue
        corrected = str(note.get("correction", {}).get("text", "") or "").strip()
        extracted = str(note.get("extraction", {}).get("text", "") or "").strip()
        if not (corrected or extracted):
            unresolved.append(note)
    return unresolved


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(float(seconds or 0))))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def package_readiness_report(class_dir: Path | str) -> dict:
    issues = []
    for note in list_notes(class_dir):
        inclusion_state = note.get("inclusion", {}).get("state")
        if inclusion_state == "pending":
            issues.append({
                "note_id": note.get("note_id"),
                "reason": "student_decision_pending",
            })
            continue
        if inclusion_state != "included":
            continue
        source = note.get("source", {})
        original = note.get("original", {})
        suffix = Path(str(original.get("path", ""))).suffix.casefold()
        corrected = str(note.get("correction", {}).get("text", "") or "").strip()
        extracted = str(note.get("extraction", {}).get("text", "") or "").strip()
        if source.get("kind") == "local_intervention":
            if not (corrected or extracted):
                issues.append({
                    "note_id": note.get("note_id"),
                    "reason": "local_intervention_without_text",
                })
            continue
        if suffix in IMAGE_SUFFIXES:
            path = Path(class_dir) / str(original.get("path", ""))
            if not path.is_file() or _sha256(path.read_bytes()) != original.get("sha256"):
                issues.append({
                    "note_id": note.get("note_id"),
                    "reason": "included_image_missing_or_changed",
                })
        elif not (corrected or extracted):
            issues.append({
                "note_id": note.get("note_id"),
                "reason": "included_note_without_publishable_text",
            })
    return {"ok": not issues, "issues": issues}


def _publish_image_asset(class_dir: Path | str, note: dict) -> str:
    original = dict(note.get("original", {}))
    source = Path(class_dir) / str(original.get("path", ""))
    suffix = source.suffix.casefold()
    if suffix not in IMAGE_SUFFIXES or not source.is_file():
        return ""
    data = source.read_bytes()
    digest = _sha256(data)
    if digest != str(original.get("sha256") or ""):
        raise RuntimeError("la imagen incluida cambió desde su captura")
    from .image_optimization import optimize_bytes
    data, conversion = optimize_bytes(data)
    if conversion["optimized"]:
        suffix = ".webp"
    published_digest = _sha256(data)
    name = f"nota_estudiante_{published_digest[:20]}{suffix}"
    target = Path(class_dir) / "img" / name
    if target.is_file() and _sha256(target.read_bytes()) != published_digest:
        raise RuntimeError("colisión al publicar imagen de nota")
    if not target.exists():
        _atomic_bytes(target, data)
    return name


def lesson_section(class_dir: Path | str, *, publish_assets: bool = False) -> str:
    included = [
        note for note in list_notes(class_dir)
        if note.get("inclusion", {}).get("state") == "included"
        and note.get("inclusion", {}).get("target") in {"lesson", "appendix"}
    ]
    if not included:
        return ""
    lines = [
        "## Notas del estudiante 📝", "",
        "> Estos aportes pertenecen al estudiante, no forman parte de la "
        "transcripción del profesor y se publican claramente separados.", "",
    ]
    for note in included:
        source = note.get("source", {})
        extraction = note.get("extraction", {})
        correction = note.get("correction", {})
        original = note.get("original", {})
        seconds = note.get("class_time", {}).get("seconds", 0)
        if source.get("kind") == "local_intervention":
            label = "Intervención del estudiante (voz)"
        elif source.get("kind") == "microphone":
            label = "Nota de voz del estudiante"
        else:
            label = source.get("original_name") or source.get("kind") or "nota"
        lines.extend([
            f"### {_fmt_time(seconds)} · {label}", "",
            "- **Original:** conservado y verificado antes de consolidar.", "",
            "<details><summary>Procedencia e integridad</summary>", "",
            f"- Captura: `{source.get('kind', 'desconocida')}` · "
            f"`{source.get('user_action', 'acción del estudiante')}`.",
            f"- Huella: `sha256:{original.get('sha256', '')}`.", "",
            "</details>", "",
        ])
        suffix = Path(str(original.get("path", ""))).suffix.casefold()
        if publish_assets and suffix in IMAGE_SUFFIXES:
            asset = _publish_image_asset(class_dir, note)
            if asset:
                lines.extend([
                    f'<img src="img/{asset}" alt="Nota visual del estudiante" '
                    'width="760">', "",
                ])
        extracted = str(extraction.get("text", "") or "").strip()
        corrected = str(correction.get("text", "") or "").strip()
        if extracted:
            lines.extend(["**Texto extraído:**", "", extracted, ""])
        if corrected:
            lines.extend(["**Corrección del estudiante:**", "", corrected, ""])
        if not extracted and not corrected:
            lines.extend([
                f"> Extracción `{extraction.get('status', 'pending')}`; revisar el original.",
                "",
            ])
    return "\n".join(lines).strip() + "\n"
