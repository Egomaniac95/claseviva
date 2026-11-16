"""Consolidación reintentable: WAV durables -> commits -> DeepSeek -> publicación."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time

from . import audio_manifest, config, estado as est, stt_backend, study_package


class RecoveryError(RuntimeError):
    """Error visible: conserva evidencia y permite otro intento explícito."""


def status_path(d: Path) -> Path:
    key = hashlib.sha256(str(d.resolve()).casefold().encode("utf-8")).hexdigest()[:20]
    root = config.ROOT if d.resolve().is_relative_to(config.ROOT.resolve()) else d.parent
    return root / "logs" / "consolidacion" / f"{key}.json"


def read_status(d: Path) -> dict:
    try:
        data = json.loads(status_path(d).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(d: Path, **changes) -> dict:
    data = {**read_status(d), **changes, "updated_at": time.time()}
    path = status_path(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    study_package.write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))
    return data


@contextmanager
def _exclusive(d: Path):
    path = status_path(d).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RecoveryError("Ya hay una consolidación en curso. Espera a que termine.") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def capture_stopped(d: Path) -> dict:
    state = est.leer_estado(d)
    if state.get("estado") != "fin":
        raise RecoveryError("La grabación sigue activa. Usa Fin cuando termine la clase; el audio continúa guardándose.")
    if state.get("shutdown_state") in {"finalizando", "pendiente_cierre"}:
        raise RecoveryError("La captura todavía está cerrando sus archivos. Espera y vuelve a pulsar Consolidar.")
    barrier = audio_manifest.completion_barrier_report(
        d, transcript_report=audio_manifest.transcript_status_report(d, verify_files=False))
    if not barrier.get("capture_ready"):
        raise RecoveryError("Todavía hay audio sin sellar o captura sin cerrar. Se conserva toda la evidencia.")
    for name, module in (("captura_audio", "src.captura_audio"),
                         ("transcriptor", "src.transcriptor_vivo")):
        if config.diagnosticar_instancia(name, modulo=module, class_dir=d).get("owner_valid"):
            raise RecoveryError("La captura o el transcriptor aún están cerrando. Vuelve a intentar cuando terminen.")
    return barrier


def recover_audio(d: Path, log=print, backend_factory=None) -> dict:
    """Un intento manual por cola; nunca borra WAV ni confirma fallos como silencio."""
    from . import transcriptor_vivo as transcriber

    capture_stopped(d)
    report = audio_manifest.transcript_status_report(d, verify_files=True)
    if report["missing_audio_count"] or report["invalid_count"]:
        raise RecoveryError(
            "Hay audio ausente o commits inválidos. No se publicará una lección incompleta ni se eliminarán archivos.")
    pending = list(report["actionable_chunks"])
    if not pending:
        return report
    state = est.leer_estado(d)
    factory = backend_factory or stt_backend.create_backend
    backend, _info = factory(
        backend=state.get("stt_backend_requested", "remote"),
        remote_consent=bool(state.get("stt_remote_consent")))
    rows = {str(row["chunk_id"]): row for row in transcriber.registros_sellados_para_stt(d)}
    for index, chunk in enumerate(pending):
        capture_stopped(d)
        if audio_manifest.read_transcript_commit(d, chunk) is not None:
            continue
        row = rows.get(chunk)
        if row is None:
            raise RecoveryError(f"No se encontró audio sellado para {chunk}. Se conserva la evidencia.")
        name = str(row.get("path") or "")
        if Path(name).name != name or not name.endswith(".wav"):
            raise RecoveryError("Ruta de audio pendiente inválida; no se envió ningún archivo.")
        path = d / "audio" / name
        sha = transcriber.snapshot_audio_input(d, path, row)
        write_status(d, stage="transcription", completed_chunks=index, total_chunks=len(pending))
        log(f"[recuperación] Transcribiendo pendiente {index + 1}/{len(pending)}: {name}")

        def record(**event):
            status = str(event.pop("status", event.get("state", "started")))
            audio_manifest.record_transcription_attempt(d, chunk, status, path=name, **event)

        backend.set_attempt_context(
            chunk_id=chunk, audio_sha256=sha, sequence=row.get("sequence"),
            history=audio_manifest.transcription_attempts(d), recorder=record, manual_retry=True)
        started = time.time()
        try:
            lines = transcriber.transcribir_chunk(
                backend, path, contexto="", inicio_abs=float(
                    row.get("pcm_wall_start", row.get("wall_start")) or 0),
                anterior=None, lineas_anteriores=[], permitir_solape_anterior=False)
            transcriber.validar_audio_input(d, path, sha, row)
            provider = str(getattr(backend, "last_provider", "") or "remote-chain")
            audio_manifest.commit_transcript_chunk(d, chunk, lines, {
                "sequence": row.get("sequence"), "path": name,
                "wall_start": row.get("pcm_wall_start", row.get("wall_start")),
                "input_sha256": sha, "segments": len(lines),
                "stt_backend": "remote-chain", "stt_provider": provider,
                "stt_model": str(getattr(backend, "last_model", "") or ""),
                "request_id": str(getattr(backend, "last_request_id", "") or ""),
                "silence_gate": provider == "silence_gate",
                "stt_submitted_audio_seconds": float(
                    getattr(backend, "last_submitted_audio_seconds", 0) or 0),
                **transcriber.metricas_commit_stt(
                    row, committed_at=time.time(), transcribe_seconds=time.time()-started,
                    audio_seconds=transcriber.duracion_wav_seg(path), backlog=len(pending)-index-1),
            })
            record(status="complete", input_sha256=sha, segments=len(lines), provider=provider)
        except Exception as error:
            record(status="pending_remote_job" if getattr(error, "pending_job", False) else "pending_remote_stt",
                   input_sha256=sha, retryable=True,
                   provider=str(getattr(error, "provider", "") or "remote-chain"),
                   error_type=type(error).__name__, error_class=str(getattr(error, "error_class", "") or ""))
            raise RecoveryError(
                f"No se pudo transcribir {name} ({type(error).__name__}). "
                "Los audios y los bloques ya transcritos están guardados. "
                "Revisa la red o las APIs y vuelve a pulsar Consolidar.") from error
    audio_manifest.materialize_transcript(d, verify_files=True)
    report = audio_manifest.transcript_status_report(d, verify_files=True)
    if report["unresolved_count"]:
        raise RecoveryError("Quedan audios pendientes. Se conservan para volver a intentar Consolidar.")
    return report


def consolidate(d: Path, *, light: bool = True, log=print, post_fn=None,
                backend_factory=None) -> None:
    """El error es durable y un nuevo clic retoma solo el trabajo que falta."""
    d = Path(d)
    with _exclusive(d):
        capture_stopped(d)
        from . import local_interventions, student_notes
        if local_interventions.pending_report(d).get("blocking"):
            raise RecoveryError("Guarda o descarta la intervención de micrófono pendiente antes de consolidar.")
        if not student_notes.package_readiness_report(d).get("ok"):
            raise RecoveryError("Hay notas sin decidir o sin texto. Corrígelas, exclúyelas o descártalas antes de consolidar.")
        write_status(d, running=True, status="running", stage="transcription", error="", started_at=time.time())
        try:
            recover_audio(d, log=log, backend_factory=backend_factory)
            capture_stopped(d)
            write_status(d, stage="deepseek")
            from . import post_procesar
            target = post_fn or post_procesar.consolidar_todo
            with post_procesar.api_errors_fatal():
                target(d, forzar=True, limpiar_al_final=light, paquete_ligero=light)
            validation = study_package.validate_candidate(d)
            final_html = d / "leccion_final.html"
            if not validation["ok"] or not final_html.is_file():
                raise RecoveryError(
                    "La consolidación terminó sin una publicación válida de Markdown y HTML. "
                    "Vuelve a pulsar Consolidar.")
            compacted = not (d / "session_state.json").exists() and not (d / "audio").exists()
            cleanup_status = "complete" if compacted else "retained" if light else "not_requested"
            write_status(
                d, running=False, status="complete", stage="complete", error="",
                lesson_sha256=study_package.sha256_file(d / "leccion_final.md"),
                html_sha256=study_package.sha256_file(final_html),
                cleanup_status=cleanup_status,
                cleanup_message=(
                    "Lección publicada. La limpieza quedó pendiente; revisa la cobertura "
                    "y la integridad de la transcripción antes de reintentar."
                    if cleanup_status == "retained" else ""))
        except Exception as error:
            message = (str(error) if isinstance(error, (RecoveryError, post_api_error())) else
                       f"{type(error).__name__} durante la consolidación. Los audios están conservados. "
                       "Revisa la red y vuelve a pulsar Consolidar.")
            write_status(d, running=False, status="error", stage="error", error=message)
            log("[recuperación] ERROR: " + message)
            raise RecoveryError(message) from error


def post_api_error():
    from .post_procesar import ConsolidationApiError
    return ConsolidationApiError
