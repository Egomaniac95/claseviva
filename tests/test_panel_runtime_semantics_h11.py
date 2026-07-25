import hashlib
from pathlib import Path
import wave

import pytest

from src import audio_manifest, config, estado, panel
ACK_FILENAME = panel.ACK_FILENAME
SPOOL_FILENAME = panel.SPOOL_FILENAME
STATE_FILENAME = panel.STATE_FILENAME


def _reporte_stt(**campos):
    base = {
        "captured_count": 0,
        "completed_count": 0,
        "empty_completed_count": 0,
        "pending_count": 0,
        "invalid_count": 0,
        "unresolved_count": 0,
        "pending_chunks": [],
        "invalid_chunks": [],
    }
    base.update(campos)
    return base


@pytest.mark.parametrize(
    ("st", "wavs", "esperado"),
    [
        ({"estado": "fin"}, 2, "closed"),
        ({"estado": "grabando", "audio_requires_attention": True}, 0,
         "requires_attention"),
        ({"estado": "grabando", "auto_pausa": True}, 1, "auto_paused"),
        ({"estado": "pausado"}, 1, "manual_paused"),
        ({"estado": "grabando", "audio_route_health": {
            "attempt": 2, "last_result": "flat", "route": "proceso"}}, 0,
         "fallback"),
        ({"estado": "grabando", "audio_ultimo_estado": "silencio",
          "audio_warning": "Chrome parece silenciado",
          "audio_route_health": {"attempt": 2, "last_result": "silence"}}, 0,
         "video_paused"),
        ({"estado": "grabando", "audio_ultimo_estado": "silencio",
          "audio_route_health": {"attempt": 3, "last_result": "silence"}}, 2,
         "silent"),
        ({"estado": "grabando", "audio_ultimo_estado": "activo",
          "audio_route_health": {"attempt": 2, "last_result": "valid"}}, 1,
         "active"),
        ({"estado": "grabando", "audio_route_health": {"attempt": 0}}, 0,
         "initializing"),
        ({"estado": "grabando", "audio_ultimo_estado": "silencio",
          "audio_route_health": {
              "attempt": 3, "last_result": "silence",
              "seconds_without_valid_audio": 35.0,
              "no_valid_audio_timeout": 60.0}}, 0, "no_valid_wav"),
    ],
)
def test_clasificador_audio_distingue_salud_y_evidencia(st, wavs, esperado):
    resultado = panel.clasificar_estado_audio(st, valid_wav_count=wavs)

    assert resultado["code"] == esperado
    assert resultado["valid_wav_count"] == wavs
    if wavs == 0:
        assert resultado["has_valid_wav"] is False


@pytest.mark.parametrize(
    ("st", "reporte", "esperado"),
    [
        ({"estado": "grabando", "audio_requires_attention": True},
         _reporte_stt(), "no_valid_audio"),
        ({"estado": "grabando", "audio_route_health": {"attempt": 0}},
         _reporte_stt(), "waiting_first_wav"),
        ({"estado": "grabando", "audio_valid_wav_count": 1,
          "stt_lifecycle": {"status": "waiting_audio"}},
         _reporte_stt(captured_count=1, pending_count=1, unresolved_count=1),
         "waiting_chunk"),
        ({"estado": "grabando", "audio_valid_wav_count": 1,
          "stt_lifecycle": {"status": "processing", "phase": "transcribe"}},
         _reporte_stt(captured_count=1, pending_count=1, unresolved_count=1),
         "processing"),
        ({"estado": "grabando", "audio_valid_wav_count": 1,
          "stt_ultimo_chunk": "chunk_1.wav", "stt_ultimo_segmentos": 0},
         _reporte_stt(captured_count=1, completed_count=1,
                      empty_completed_count=1), "complete_empty"),
        ({"estado": "grabando", "audio_valid_wav_count": 1,
          "stt_ultimo_chunk": "chunk_1.wav", "stt_ultimo_segmentos": 3},
         _reporte_stt(captured_count=1, completed_count=1),
         "complete_segments"),
        ({"estado": "grabando", "audio_valid_wav_count": 1},
         _reporte_stt(captured_count=1, completed_count=1), "no_pending"),
        ({"estado": "grabando", "audio_valid_wav_count": 1,
          "stt_supervisor": {"status": "idle_no_pending"}},
         _reporte_stt(captured_count=1, completed_count=1),
         "idle_no_pending"),
        ({"estado": "fin", "audio_valid_wav_count": 1,
          "stt_lifecycle": {"status": "stopped_no_pending"}},
         _reporte_stt(captured_count=1, completed_count=1), "stopped_normal"),
        ({"estado": "grabando", "audio_valid_wav_count": 1,
          "stt_supervisor": {"status": "lock_conflict"}},
         _reporte_stt(captured_count=1, pending_count=1, unresolved_count=1),
         "lock_conflict"),
        ({"estado": "grabando", "audio_valid_wav_count": 1,
          "stt_supervisor": {"status": "lock_stale", "restarted": True}},
         _reporte_stt(captured_count=1, pending_count=1, unresolved_count=1),
         "restarting"),
        ({"estado": "grabando", "audio_valid_wav_count": 1,
          "stt_lifecycle": {"status": "python_exception"}},
         _reporte_stt(captured_count=1, invalid_count=1,
                      unresolved_count=1), "error"),
    ],
)
def test_clasificador_stt_no_confunde_ausencia_de_trabajo_con_crash(
        st, reporte, esperado):
    resultado = panel.clasificar_estado_stt(st, reporte)

    assert resultado["code"] == esperado
    assert "label" in resultado and resultado["label"]


def _aislar_panel(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(config, "CONTENIDO_DIR", tmp_path / "contenido")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setattr(panel, "_dispositivos", lambda: [])
    monkeypatch.setattr(panel, "_monitores", lambda: [])
    config.LOGS_DIR.mkdir(parents=True)
    return estado.crear_clase("Panel runtime", curso="curso-h11")


def _cerrar_con_wav_y_fallo_terminal(d: Path) -> None:
    wav_path = d / "audio" / "chunk_000000000100_p1_v10.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(100)
        wav.writeframes(b"\x01\x00" * 100)
    sha256 = hashlib.sha256(wav_path.read_bytes()).hexdigest()
    audio_manifest.append_capture_session_event(
        d, "capture_session_started", capture_session_id="sesion-panel")
    audio_manifest.append_event(d, {
        "chunk_id": wav_path.stem,
        "status": "sealed",
        "sequence": 1,
        "path": wav_path.name,
        "sha256": sha256,
        "rate": 100,
        "frame_count": 100,
        "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(
        d, wav_path.stem, [{"t": 100.0, "texto": "contenido", "parte": 1}], {
            "path": wav_path.name,
            "sequence": 1,
            "wall_start": 100.0,
            "input_sha256": sha256,
        })
    audio_manifest.append_event(d, {
        "chunk_id": "chunk_000000000101_p1_v10",
        "status": "capture_failed",
        "sequence": 2,
        "path": "chunk_000000000101_p1_v10.wav",
        "reason": "watchdog_pcm_flat",
        "capture_outcome": "route_failed",
        "transactional_stt_required": True,
    })
    audio_manifest.append_capture_session_event(
        d, "capture_session_closed", capture_session_id="sesion-panel",
        terminal_capture_failures=1)
    estado.transicionar_captura(d, "fin", ahora=200.0)
    estado.actualizar_estado(d, shutdown_state="pendiente_consolidar")


def test_cierre_degradado_habilita_consolidar_sin_mutar_estado(
        monkeypatch, tmp_path):
    d = _aislar_panel(monkeypatch, tmp_path)
    _cerrar_con_wav_y_fallo_terminal(d)
    estado_path = d / "session_state.json"
    estado_previo = estado_path.read_bytes()
    iniciados = []
    monkeypatch.setattr(panel, "_POST_CORRIENDO", False)
    monkeypatch.setattr(panel, "_POST_ERROR", "")
    monkeypatch.setattr(
        panel, "_iniciar_worker_post", lambda target: iniciados.append(target))
    cliente = panel.app.test_client()

    estado_ui = cliente.get("/api/estado").get_json()
    aceptado = cliente.post("/api/consolidar", json={}).get_json()

    assert estado_path.read_bytes() == estado_previo
    assert estado_ui["shutdown_state"] == "pendiente_consolidar"
    assert estado_ui["shutdown_blocking"] is False
    assert estado_ui["consolidation_ready"] is True
    assert estado_ui["consolidation_status"] == "ready_with_warnings"
    assert estado_ui["consolidation_warnings"] == [
        "terminal_capture_failures=1"]
    assert "advertencias de cobertura" in estado_ui["post_mensaje"]
    assert aceptado["ok"] is True
    assert len(iniciados) == 1
    panel._POST_CORRIENDO = False


def test_cierre_incompleto_mantiene_consolidar_bloqueado(
        monkeypatch, tmp_path):
    d = _aislar_panel(monkeypatch, tmp_path)
    audio_manifest.append_capture_session_event(
        d, "capture_session_started", capture_session_id="sesion-abierta")
    estado.transicionar_captura(d, "fin", ahora=200.0)
    estado.actualizar_estado(d, shutdown_state="pendiente_consolidar")
    cliente = panel.app.test_client()

    estado_ui = cliente.get("/api/estado").get_json()
    rechazado = cliente.post("/api/consolidar", json={}).get_json()

    assert estado_ui["shutdown_blocking"] is True
    assert estado_ui["consolidation_ready"] is False
    assert "Esperando el cierre verificable" in estado_ui["post_mensaje"]
    assert rechazado["ok"] is False
    assert rechazado["reason"] == "shutdown_not_complete"
    assert "capture_session_not_closed" in rechazado["barrier_reasons"]


def test_api_expone_causa_cero_audio_sin_declarar_caida_stt(
        monkeypatch, tmp_path):
    d = _aislar_panel(monkeypatch, tmp_path)
    estado.actualizar_estado(
        d,
        audio_requires_attention=True,
        audio_attention_reason="fallback_cycle_limit",
        audio_route_health={"attempt": 4, "last_result": "flat"},
        stt_lifecycle={"status": "stopped_no_pending"},
    )

    data = panel.app.test_client().get("/api/estado").get_json()

    assert data["audio_state"] == "requires_attention"
    assert data["audio_valid_wav_count"] == 0
    assert data["stt_state"] == "no_valid_audio"
    assert data["stt_has_work"] is False
    assert "0 audio válido" in data["stt_state_detail"]
    assert "se cayó" not in data["stt_state_detail"]


def test_consolidar_sin_wav_ni_transcript_exige_confirmacion_explicita(
        monkeypatch, tmp_path):
    d = _aislar_panel(monkeypatch, tmp_path)
    estado.transicionar_captura(d, "fin", ahora=200.0)
    estado.actualizar_estado(d, shutdown_state="finalizada")
    iniciados = []
    monkeypatch.setattr(panel, "_POST_CORRIENDO", False)
    monkeypatch.setattr(panel, "_POST_ERROR", "")
    monkeypatch.setattr(
        panel, "_iniciar_worker_post", lambda target: iniciados.append(target))
    cliente = panel.app.test_client()

    advertencia = cliente.post("/api/consolidar", json={}).get_json()
    confirmado = cliente.post(
        "/api/consolidar",
        json={"confirmar_sin_evidencia": True}).get_json()

    assert advertencia["ok"] is False
    assert advertencia["requires_confirmation"] is True
    assert advertencia["audio_valid_wav_count"] == 0
    assert advertencia["transcript_segment_count"] == 0
    assert confirmado["ok"] is True
    assert confirmado["accepted_without_audio"] is True
    assert len(iniciados) == 1
    panel._POST_CORRIENDO = False


def test_pagina_muestra_estados_audio_stt_y_confirmacion_sin_evidencia():
    assert 'id="audioestado"' in panel.PAGINA
    assert 'id="sttestado"' in panel.PAGINA
    assert 'id="runtimeHealth"' in panel.PAGINA
    assert 'Sesión total <b id="tiempo_pared"' in panel.PAGINA
    assert 'Transcrito hasta <b id="transcrito_hasta"' in panel.PAGINA
    assert 'Retraso STT <b id="retraso_stt"' in panel.PAGINA
    assert 'id="chipvideo"' in panel.PAGINA
    assert "confirmar_sin_evidencia" in panel.PAGINA
    assert "s.shutdown_blocking" in panel.PAGINA
    assert "min-inline-size:8ch" in panel.PAGINA
    assert "font-variant-numeric:tabular-nums" in panel.PAGINA


def test_formato_servidor_y_reloj_local_conservan_ancho_hhmmss():
    assert panel.config.hhmmss_rel(161) == "00:02:41"
    assert panel.config.hhmmss_rel(360_000) == "100:00:00"
    assert "padStart(2,'0')" in panel.PAGINA


def test_salud_runtime_stt_no_contamina_log_de_postproceso():
    panel.POST_LOG.clear()
    panel.SALUD.clear()

    panel._runtime_health_append("STT al día; esperando audio.")

    assert panel.SALUD == ["STT al día; esperando audio."]
    assert panel.POST_LOG == []


def test_api_expone_salud_stt_fuera_del_log_de_postproceso(
        monkeypatch, tmp_path):
    _aislar_panel(monkeypatch, tmp_path)
    panel.POST_LOG.clear()
    panel.SALUD.clear()
    panel._runtime_health_append("STT al día; esperando audio.")

    data = panel.app.test_client().get("/api/estado").get_json()

    assert data["stt_runtime_health_latest"] == "STT al día; esperando audio."
    assert data["stt_runtime_health"] == ["STT al día; esperando audio."]
    assert data["post_log"] == ""


def test_api_prioriza_transcrito_hasta_y_retraso_stt_cacheados(
        monkeypatch, tmp_path):
    d = _aislar_panel(monkeypatch, tmp_path)
    estado.actualizar_estado(
        d,
        inicio=1000.0,
        audio_valid_wav_count=2,
        audio_last_valid_wall_end=1130.0,
        stt_transcribed_until_wall=1100.0,
    )
    monkeypatch.setattr(panel.time, "time", lambda: 1140.0)

    data = panel.app.test_client().get("/api/estado").get_json()

    assert data["stt_transcribed_until_sec"] == pytest.approx(100.0)
    assert data["stt_transcribed_until"] == "00:01:40"
    assert data["stt_delay_sec"] == pytest.approx(30.0)
    assert data["stt_delay"] == "00:00:30"


def test_fuente_sistema_no_se_rotula_como_fallback_de_navegador():
    ruta, detalle = panel._audio_ruta_ui(
        "sistema:dispositivo(Altavoces USB)", "sistema")

    assert ruta == "TODO EL SISTEMA"
    assert "mezcla completa" in detalle


def test_api_no_expone_texto_vivo_y_muestra_cadena_remota(
        monkeypatch, tmp_path):
    d = _aislar_panel(monkeypatch, tmp_path)
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(panel, "RUNTIME_DIR", runtime)
    estado.actualizar_estado(
        d,
        inicio=100.0,
        stt_device="remote",
        stt_backend="remote-chain",
        stt_provider="deepgram",
        stt_readiness="ready",
        stt_hardware={
            "device": "remote",
            "backend": "remote-chain",
            "readiness": "ready",
        },
    )
    estado.anexar_jsonl(
        d / "transcript.jsonl",
        {"t": 101.0, "parte": 1, "texto": "Texto final canónico."})
    data = panel.app.test_client().get("/api/estado").get_json()

    assert "transcript_blocks" not in data
    assert "stt_stable_text" not in data
    assert "stt_provisional_text" not in data
    assert "Texto final canónico" not in str(data)
    assert "renderTranscript" not in panel.PAGINA
    assert "STT remoto" in data["stt_hardware_label"]
    assert "deepgram" in data["stt_hardware_label"]
    assert "Whisper local" not in panel.PAGINA


def test_cleanup_runtime_stt_elimina_solo_ipc_de_la_instancia(
        monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(panel, "RUNTIME_DIR", runtime)
    for name in (SPOOL_FILENAME, ACK_FILENAME, STATE_FILENAME):
        (runtime / name).write_bytes(b"synthetic")
    unrelated = runtime / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    removed = panel._limpiar_runtime_stt()

    assert removed == 3
    assert unrelated.read_text(encoding="utf-8") == "keep"
