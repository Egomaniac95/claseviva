"""Red caída/restaurada: nunca perder WAV ni convertir un fallo en publicación."""
from types import SimpleNamespace
import wave

import pytest

from src import audio_manifest, config, consolidation_recovery as recovery
from src import estado, escriba, panel, post_procesar, recovery_service, transcriptor_vivo
from src import lesson_export, study_package


def _publish(d, text="Lección recuperada"):
    metadata = {"recording_parts": [{"part": 1, "useful_start_sec": 0, "useful_end_sec": 60}],
                "images": []}
    (d / "leccion_final.md").write_text(
        study_package.metadata_comment(metadata) + "\n\n# Clase\n\n" + text,
        encoding="utf-8")
    lesson_export.export_html(d)


@pytest.fixture
def pending(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "diagnosticar_instancia", lambda *a, **k: {"owner_valid": False})
    d = tmp_path / "contenido" / "curso" / "clases" / "CLASE_01_prueba"
    d.mkdir(parents=True)
    estado.escribir_estado(d, {"estado": "fin", "parte": 3, "inicio": 1000.,
                              "clase_id": d.name, "nombre": "Fixture", "shutdown_state": "pendiente_consolidar",
                              "stt_remote_consent": True})
    audio_manifest.append_event(d, {"status": "capture_session_started", "capture_session_id": "fixture"})
    paths = []
    for index in range(2):
        chunk = f"chunk_{1000+index*30:012d}_p2_v10"
        path = d / "audio" / f"{chunk}.wav"
        path.parent.mkdir(exist_ok=True)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x80\x01" * 1600)
        audio_manifest.append_event(d, {
            "chunk_id": chunk, "status": "sealed", "path": path.name, "part": 2,
            "sequence": index, "sha256": audio_manifest.sha256_file(path),
            "wall_start": 1000+index*30, "wall_end": 1000.1+index*30,
            "rate": 16000, "channels": 1, "sample_width": 2, "frame_count": 1600,
        })
        paths.append(path)
    audio_manifest.append_event(d, {"status": "capture_session_closed", "capture_session_id": "fixture"})
    calls, contexts = [], []
    backend = SimpleNamespace(
        last_provider="assemblyai", last_model="fixture", last_request_id="fixture-request",
        last_submitted_audio_seconds=.1, set_attempt_context=lambda **kw: contexts.append(kw))

    def transcribe(_backend, path, **kw):
        calls.append(path.name)
        return [{"t": 1000.0, "ini": 0., "fin": .1, "texto": "evidencia parte dos",
                 "chunk": path.name, "parte": 2}]

    monkeypatch.setattr(transcriptor_vivo, "transcribir_chunk", transcribe)
    return SimpleNamespace(d=d, paths=paths, calls=calls, contexts=contexts, transcribe=transcribe,
                           factory=lambda **kw: (backend, {}))


def test_network_failure_preserves_all_audio_and_retry_skips_successful_commits(pending, monkeypatch):
    originals = [p.read_bytes() for p in pending.paths]
    published = []
    failure = {"enabled": True}

    def transcribe(model, path, **kw):
        if path == pending.paths[1] and failure["enabled"]:
            pending.calls.append(path.name)
            raise ConnectionError("synthetic firewall")
        return pending.transcribe(model, path, **kw)

    monkeypatch.setattr(transcriptor_vivo, "transcribir_chunk", transcribe)
    with pytest.raises(recovery.RecoveryError, match="Consolidar"):
        recovery.consolidate(pending.d, light=False, backend_factory=pending.factory,
                             post_fn=lambda d, **k: (_publish(d), published.append(True)))
    assert [p.read_bytes() for p in pending.paths] == originals
    assert recovery.read_status(pending.d)["status"] == "error"
    assert audio_manifest.read_transcript_commit(pending.d, pending.paths[0].stem)
    assert audio_manifest.read_transcript_commit(pending.d, pending.paths[1].stem) is None
    assert not published
    failure["enabled"] = False
    recovery.consolidate(pending.d, light=False, backend_factory=pending.factory,
                         post_fn=lambda d, **k: (_publish(d), published.append(True)))
    assert pending.calls.count(pending.paths[0].name) == 1
    assert pending.calls.count(pending.paths[1].name) == 2
    assert all(ctx["manual_retry"] for ctx in pending.contexts)
    assert recovery.read_status(pending.d)["status"] == "complete" and published == [True]
    assert [p.read_bytes() for p in pending.paths] == originals


def test_deepseek_failure_keeps_previous_lesson_and_avoids_retranscription(pending, monkeypatch):
    previous = pending.d / "leccion_final.md"
    previous.write_text("Lección publicada anterior", encoding="utf-8")
    monkeypatch.setenv("CLASE_POST_LLM", "1")
    monkeypatch.setattr(post_procesar, "preguntar", lambda *a, **k: None)

    def post(d, **kwargs):
        text = post_procesar.preguntar_cacheado(d, [{"role": "user", "content": "resumir"}],
                                              300, "fixture-resumen", "fixture-v1")
        _publish(d, text)

    with pytest.raises(recovery.RecoveryError, match="DeepSeek"):
        recovery.consolidate(pending.d, backend_factory=pending.factory, post_fn=post)
    assert previous.read_text(encoding="utf-8") == "Lección publicada anterior"
    assert all(p.is_file() for p in pending.paths) and len(pending.calls) == 2
    monkeypatch.setattr(post_procesar, "preguntar", lambda *a, **k: "Lección recuperada")
    recovery.consolidate(pending.d, light=False, backend_factory=pending.factory, post_fn=post)
    assert previous.read_text(encoding="utf-8").endswith("Lección recuperada")
    assert len(pending.calls) == 2  # Se retoma DeepSeek, no se paga STT otra vez.


def test_active_recording_is_never_stopped_or_processed(pending):
    estado.actualizar_estado(pending.d, estado="grabando", parte=3)
    before = [p.read_bytes() for p in pending.paths]
    with pytest.raises(recovery.RecoveryError, match="grabación sigue activa"):
        recovery.consolidate(pending.d, backend_factory=pending.factory)
    assert estado.leer_estado(pending.d)["estado"] == "grabando" and not pending.calls
    assert [p.read_bytes() for p in pending.paths] == before


def test_return_without_published_outputs_is_not_reported_complete(pending):
    with pytest.raises(recovery.RecoveryError, match="publicación válida"):
        recovery.consolidate(pending.d, backend_factory=pending.factory,
                             post_fn=lambda *a, **k: None)
    assert recovery.read_status(pending.d)["status"] == "error"
    assert all(path.is_file() for path in pending.paths)


def test_restart_keeps_failed_publication_visible_even_when_markdown_is_new(pending, monkeypatch):
    _publish(pending.d)
    recovery.write_status(pending.d, running=False, status="error", error="Falló el cierre")
    monkeypatch.setattr(panel, "_POST_ERROR", "")
    monkeypatch.setattr(panel, "_POST_CORRIENDO", False)

    final = panel._leccion_final_info(pending.d)
    ui = panel._estado_operativo(estado.leer_estado(pending.d), final)

    assert not final["actualizada"]
    assert ui["clave"] == "error" and ui["detalle"] == "Falló el cierre"
    recording = panel._estado_operativo({"estado": "grabando"}, final)
    assert recording["clave"] == "grabando"


def test_retained_cleanup_is_durable_and_visible_after_restart(pending, monkeypatch):
    recovery.consolidate(pending.d, light=True, backend_factory=pending.factory,
                         post_fn=lambda d, **k: _publish(d))
    monkeypatch.setattr(panel, "_POST_ERROR", "")
    monkeypatch.setattr(panel, "_POST_CORRIENDO", False)

    final = panel._leccion_final_info(pending.d)
    ui = panel._estado_operativo(estado.leer_estado(pending.d), final)

    assert recovery.read_status(pending.d)["cleanup_status"] == "retained"
    assert "limpieza quedó pendiente" in ui["detalle"]


def test_restart_exposes_unfinished_consolidation(pending, monkeypatch):
    _publish(pending.d)
    recovery.write_status(pending.d, running=True, status="running", error="")
    monkeypatch.setattr(panel, "_POST_ERROR", "")
    monkeypatch.setattr(panel, "_POST_CORRIENDO", False)
    final = panel._leccion_final_info(pending.d)
    assert not final["actualizada"]
    assert "cierre confirmado" in panel._estado_operativo(
        estado.leer_estado(pending.d), final)["detalle"]


def test_one_consolidation_owns_class_and_releases_lock_after_failure(pending):
    with recovery._exclusive(pending.d):
        with pytest.raises(recovery.RecoveryError, match="en curso"):
            recovery.consolidate(pending.d, backend_factory=pending.factory)
    with recovery._exclusive(pending.d):
        pass


def test_panel_allows_recoverable_stt_after_capture_closed_but_not_while_recording(pending):
    gate = panel._consolidation_gate(pending.d, estado.leer_estado(pending.d), fresh=True)
    assert gate["ready"] and gate["status"] == "ready_to_recover_stt"
    estado.actualizar_estado(pending.d, estado="grabando")
    assert not panel._consolidation_gate(pending.d, estado.leer_estado(pending.d), fresh=True)["ready"]


@pytest.mark.parametrize("owner_valid", [False, True])
def test_writer_fin_defers_parked_audio_without_waiting_for_api(pending, monkeypatch, owner_valid):
    if owner_valid:
        estado.actualizar_estado(pending.d, stt_lifecycle={"status": "pending_remote_stt"})
    monkeypatch.setattr(config, "diagnosticar_instancia", lambda *a, **k: {"owner_valid": owner_valid})
    monkeypatch.setattr(escriba.time, "sleep", lambda *_: pytest.fail("no esperar STT estacionado"))
    originals = [p.read_bytes() for p in pending.paths]

    report = escriba._esperar_transcriptor_fin(pending.d)

    assert report["deferred_to_consolidation"] and not report["timed_out"]
    assert report["capture_ready"] and not report["ready"]
    assert report["pending_stt_count"] == 2
    assert not pending.calls and [p.read_bytes() for p in pending.paths] == originals


def test_writer_waits_for_active_transcriber_and_never_defers_open_capture(pending, monkeypatch):
    monkeypatch.setattr(config, "diagnosticar_instancia", lambda *a, **k: {"owner_valid": True})
    monkeypatch.setenv("CLASE_ESCRIBA_FIN_WAIT", "0")
    active = escriba._esperar_transcriptor_fin(pending.d)
    assert active["timed_out"] and not active.get("deferred_to_consolidation")

    estado.actualizar_estado(pending.d, stt_lifecycle={"status": "pending_remote_stt"})
    audio_manifest.append_capture_session_event(pending.d, "capture_session_started", capture_session_id="reopened")
    open_capture = escriba._esperar_transcriptor_fin(pending.d)
    assert open_capture["status"] == "waiting_capture" and open_capture["timed_out"]
    assert not open_capture.get("deferred_to_consolidation")


def test_missing_audio_blocks_consolidation_and_never_calls_api(pending):
    pending.paths[0].unlink()
    with pytest.raises(recovery.RecoveryError, match="ausente"):
        recovery.consolidate(pending.d, backend_factory=pending.factory)
    assert not pending.calls and pending.paths[1].is_file()


def test_tasks_failure_is_fatal_in_user_consolidation(monkeypatch):
    monkeypatch.setenv("CLASE_POST_LLM", "1")
    monkeypatch.setattr(post_procesar, "preguntar", lambda *a, **k: None)
    with post_procesar.api_errors_fatal():
        with pytest.raises(post_procesar.ConsolidationApiError, match="post-tareas"):
            post_procesar._consultar_tareas("contenido")


def test_resume_summary_starts_at_current_session_instead_of_old_class_clock(pending):
    estado.anexar_jsonl(pending.d / "marcas.jsonl", {"tipo": "reanudar_sesion", "parte": 2, "t": 2000.})
    estado.anexar_jsonl(pending.d / "marcas.jsonl", {"tipo": "reanudar_sesion", "parte": 3, "t": 7000.})
    assert escriba.inicio_ultima_sesion(pending.d, {"inicio": 1000., "parte": 3}) == 7000.


def test_resumed_summary_boundary_does_not_count_hours_between_sessions(pending):
    marks = [{"tipo": "inicio", "parte": 1, "t": 1000.},
             {"tipo": "fin", "parte": 1, "t": 1600.},
             {"tipo": "reanudar_sesion", "parte": 3, "t": 7000.}]
    for row in marks:
        estado.anexar_jsonl(pending.d / "marcas.jsonl", row)
    state = {"inicio": 1000., "parte": 3, "estado": "grabando", "auto_pausa": False}
    anchor = escriba.inicio_ultima_sesion(pending.d, state)
    plan = escriba.planificar_auto_captura_util(state, marks, ultimo_auto_t=anchor,
                                               ahora=7320., intervalo=300., margen=0.)
    assert plan["boundary_wall_t"] == 7300. and plan["due"]


def test_live_bridge_refuses_untrusted_origins_and_refuses_active_capture(pending, monkeypatch):
    monkeypatch.setattr(recovery_service.est, "clase_activa", lambda: pending.d)
    estado.actualizar_estado(pending.d, estado="grabando")
    client = recovery_service.app.test_client()
    assert client.post("/api/consolidar", json={}, headers={"Origin": "https://untrusted.example"}).status_code == 403
    response = client.post("/api/consolidar", json={}, headers={"Origin": "http://localhost:8756"})
    assert response.status_code == 409 and "grabación" in response.get_json()["msg"]
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:8756"
    assert not pending.calls


@pytest.fixture
def bridge(pending, monkeypatch):
    monkeypatch.setattr(recovery_service.est, "clase_activa", lambda: pending.d)
    monkeypatch.setattr(recovery_service, "_running", False)
    monkeypatch.setattr(recovery_service, "_start_error", "")
    monkeypatch.setattr(recovery_service, "_logs", [])
    monkeypatch.setattr(recovery_service, "_last_class", None)
    monkeypatch.setattr(recovery_service.threading, "Thread",
                        lambda target, **kw: SimpleNamespace(start=target))
    real_consolidate = recovery.consolidate
    monkeypatch.setattr(recovery, "consolidate", lambda d, **kw: real_consolidate(
        d, light=False, backend_factory=pending.factory, post_fn=lambda d, **k: _publish(d), log=kw["log"]))
    return recovery_service.app.test_client()


def test_first_bridge_consolidation_creates_status_directory_and_processes_audio(pending, bridge):
    assert not recovery.status_path(pending.d).parent.exists()

    response = bridge.post("/api/consolidar", json={})

    assert response.status_code == 200 and response.get_json()["ok"]
    progress = bridge.get("/status").get_json()
    assert not progress["running"] and progress["status"] == "complete"
    assert len(pending.calls) == 2
    assert all(audio_manifest.read_transcript_commit(pending.d, path.stem) for path in pending.paths)


def test_bridge_api_failure_stops_running_indicator_and_preserves_audio(pending, bridge, monkeypatch):
    def fail(*a, **kw):
        raise ConnectionError("synthetic firewall")

    monkeypatch.setattr(transcriptor_vivo, "transcribir_chunk", fail)
    assert bridge.post("/api/consolidar", json={}).get_json()["ok"]

    progress = bridge.get("/status").get_json()
    assert not progress["running"] and progress["status"] == "error"
    assert "Consolidar" in progress["error"]
    assert all(path.is_file() for path in pending.paths)


@pytest.mark.parametrize("failed_start", ["status_file", "thread"])
def test_failed_bridge_start_reports_error_and_allows_retry(pending, bridge, monkeypatch, failed_start):
    def fail(*a, **kw):
        raise PermissionError("synthetic startup failure")

    with monkeypatch.context() as failure:
        if failed_start == "status_file":
            failure.setattr(recovery, "write_status", fail)
        else:
            failure.setattr(recovery_service.threading, "Thread",
                            lambda **kw: SimpleNamespace(start=fail))
        response = bridge.post("/api/consolidar", json={})

    assert response.status_code == 500 and not response.get_json()["ok"]
    progress = bridge.get("/status").get_json()
    assert not progress["running"] and progress["status"] == "error"
    assert "Consolidar" in progress["error"] and "PermissionError" in progress["error"]
    assert not pending.calls and all(path.is_file() for path in pending.paths)

    assert bridge.post("/api/consolidar", json={}).get_json()["ok"]
    assert bridge.get("/status").get_json()["status"] == "complete"
    assert len(pending.calls) == 2
