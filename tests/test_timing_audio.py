import hashlib
from pathlib import Path

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src import (audio_manifest, audio_sesiones, captura_audio, config, escriba,
                 estado, panel, post_procesar, transcriptor_vivo)


def _aislar_entorno(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "CONTENIDO_DIR", tmp_path / "contenido")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setattr(panel, "_dispositivos", lambda: [])
    monkeypatch.setattr(panel, "_monitores", lambda: [])
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _fijar_estado(d: Path, **campos) -> dict:
    st = estado.leer_estado(d)
    st.update(campos)
    estado.escribir_estado(d, st)
    return st


def _marcas(d: Path, *items: dict) -> None:
    (d / "marcas.jsonl").write_text("", encoding="utf-8")
    for item in items:
        estado.anexar_jsonl(d / "marcas.jsonl", item)


def test_cambiar_fuente_restaura_enfoque_local_antes_de_mover_la_propiedad(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Cambio de fuente", curso="curso-enfoque")
    _fijar_estado(d, fuente="chrome", enfoque=True)
    llamadas = []
    monkeypatch.setattr(
        audio_sesiones,
        "restaurar_enfoques",
        lambda: llamadas.append("restaurar") or 2,
        raising=False,
    )

    respuesta = panel.app.test_client().post(
        "/api/comando", json={"cmd": "fuente msedge"}).get_json()

    assert respuesta["ok"] is True
    assert llamadas == ["restaurar"]
    final = estado.leer_estado(d)
    assert final["fuente"] == "msedge"
    assert final["enfoque"] is False


def test_fin_restaura_snapshots_locales_aunque_el_flag_compartido_sea_false(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Fin enfoque", curso="curso-enfoque")
    _fijar_estado(d, fuente="chrome", enfoque=False)
    llamadas = []
    monkeypatch.setattr(
        audio_sesiones,
        "restaurar_enfoques",
        lambda: llamadas.append("restaurar") or 1,
    )
    monkeypatch.setattr(panel, "MODO_APP", False)

    respuesta = panel.app.test_client().post(
        "/api/comando", json={"cmd": "fin"}).get_json()

    assert respuesta["ok"] is True
    assert llamadas == ["restaurar"]
    final = estado.leer_estado(d)
    assert final["estado"] == "fin"
    assert final["enfoque"] is False


def test_api_estado_duracion_descuenta_pausa_manual_y_auto(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Timer activo", curso="curso-timer")
    st = _fijar_estado(d, inicio=1000.0, estado="grabando", auto_pausa=False, parte=2)
    _marcas(
        d,
        {"t": 1000.0, "tipo": "inicio", "parte": 1},
        {"t": 1120.0, "tipo": "auto_pausa", "parte": 1},
        {"t": 1300.0, "tipo": "auto_seguir", "parte": 1},
        {"t": 1300.0, "tipo": "fin_parte", "parte": 1},
        {"t": 1360.0, "tipo": "pausa", "parte": 2},
        {"t": 1500.0, "tipo": "seguir", "parte": 2},
    )
    monkeypatch.setattr(panel.time, "time", lambda: 1600.0)

    data = panel.app.test_client().get("/api/estado").get_json()

    assert panel._segundos_almacenando(d, st, 1600.0) == 280.0
    assert data["duracion"] == "00:04:40"
    assert data["duracion_parte"] == "00:02:40"


def test_tiempo_activo_se_congela_en_auto_pausa_actual(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Auto pausa", curso="curso-timer")
    st = _fijar_estado(d, inicio=1000.0, estado="grabando", auto_pausa=True, parte=1)
    _marcas(
        d,
        {"t": 1000.0, "tipo": "inicio", "parte": 1},
        {"t": 1120.0, "tipo": "auto_pausa", "parte": 1},
    )

    assert panel._segundos_almacenando(d, st, 2000.0) == 120.0


def test_postproceso_colapsa_pausas_y_intervalo_entre_partes(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Continuación", curso="curso-timeline")
    st = _fijar_estado(
        d, inicio=1000.0, estado="fin", auto_pausa=False, parte=2)
    _marcas(
        d,
        {"t": 1000.0, "tipo": "inicio", "parte": 1},
        {"t": 1100.0, "tipo": "auto_pausa", "parte": 1},
        {"t": 1300.0, "tipo": "fin", "parte": 1},
        {"t": 1300.0, "tipo": "fin_parte", "parte": 1},
        {"t": 10000.0, "tipo": "reanudar_sesion", "parte": 2},
        {"t": 10100.0, "tipo": "auto_pausa", "parte": 2},
        {"t": 10200.0, "tipo": "auto_seguir", "parte": 2},
        {"t": 10250.0, "tipo": "fin", "parte": 2},
    )

    origen = post_procesar._origen_postproceso(d, st)

    assert estado.rangos_partes(d) == [
        (1, 1000.0, 1300.0),
        (2, 10000.0, 10250.0),
    ]
    assert post_procesar._normalizar_valor_tiempo(10000.0, origen) == 100.0
    assert post_procesar._normalizar_valor_tiempo(10050.0, origen) == 150.0
    assert post_procesar._normalizar_valor_tiempo(10225.0, origen) == 225.0
    assert post_procesar.fmt_t(10225.0, origen) == "03:45"


def test_pid_silencioso_con_pantalla_activa_activa_fallback():
    assert captura_audio.proceso_loopback_silencioso_con_pantalla_activa(
        "chrome", "chrome:proceso(pid=3100)", 66.0, None, False, 120)
    assert captura_audio.proceso_loopback_silencioso_con_pantalla_activa(
        "chrome", "chrome:proceso(pid=3100)", 66.0, 0.020, False, 120)
    assert not captura_audio.proceso_loopback_silencioso_con_pantalla_activa(
        "chrome", "chrome:proceso(pid=3100)", 66.0, 0.0, False, 120)
    assert not captura_audio.proceso_loopback_silencioso_con_pantalla_activa(
        "chrome", "chrome:proceso(pid=3100)", 66.0, None, True, 120)
    assert not captura_audio.proceso_loopback_silencioso_con_pantalla_activa(
        "chrome", "chrome:dispositivo(default)", 66.0, None, False, 120)


def test_clasificar_audio_navegador_distingue_voz_baja_de_pcm_mudo():
    voz_baja = captura_audio.clasificar_muestra_audio(
        "chrome", rms=55.0, peak=520, pico_sesion=0.04, silencio_rms=120)
    assert voz_baja["guardar"] is True
    assert voz_baja["es_silencio"] is False
    assert voz_baja["captura_rota"] is False

    mixer_bajo = captura_audio.clasificar_muestra_audio(
        "chrome", rms=2.5, peak=30, pico_sesion=0.006, silencio_rms=120)
    assert mixer_bajo["guardar"] is True
    assert mixer_bajo["es_silencio"] is False
    assert mixer_bajo["navegador_activo"] is True
    assert mixer_bajo["pcm_util"] is False

    mixer_bajo_roto = captura_audio.clasificar_muestra_audio(
        "chrome", rms=0.34, peak=1, pico_sesion=0.006, silencio_rms=120)
    assert mixer_bajo_roto["guardar"] is False
    assert mixer_bajo_roto["captura_rota"] is True

    casi_mudo = captura_audio.clasificar_muestra_audio(
        "chrome", rms=0.34, peak=1, pico_sesion=0.04, silencio_rms=120)
    assert casi_mudo["guardar"] is False
    assert casi_mudo["captura_rota"] is True

    sistema_mudo = captura_audio.clasificar_muestra_audio(
        "sistema", rms=0.34, peak=1, pico_sesion=None, silencio_rms=120)
    assert sistema_mudo["guardar"] is False
    assert sistema_mudo["es_silencio"] is True


def test_selector_audio_no_reelige_pid_bloqueado_y_prefiere_root_pid():
    sesiones = [
        {"pid": 3100, "root_pid": 14880, "pico": 0.40},
    ]

    sesion, razon = captura_audio.seleccionar_sesion_audio(
        sesiones, {14880: 200.0}, ahora=100.0)

    assert sesion is None
    assert razon == "pid_bloqueado"

    sesion, razon = captura_audio.seleccionar_sesion_audio(
        sesiones, {14880: 90.0}, ahora=100.0)

    assert razon == "ok"
    assert sesion["root_pid"] == 14880


def test_panel_audio_ruta_ui_distingue_pid_y_fallback():
    ruta, detalle = panel._audio_ruta_ui(
        "chrome:proceso(pid=14880, sesion=3100)", "chrome")
    assert ruta == "EXCLUSIVO POR PID"
    assert "pid=14880" in detalle

    ruta, detalle = panel._audio_ruta_ui(
        "chrome:dispositivo(Altavoces Realtek [Loopback])", "chrome")
    assert ruta == "FALLBACK SALIDA SISTEMA"
    assert "mezcla" in detalle


def test_resumen_vivo_compacta_apuntes_sin_encabezado_interno():
    cuerpo = (
        "### Apuntes del tramo\n"
        "Se explica descenso de gradiente con tasa de aprendizaje y momentum. "
        "El profesor contrasta convergencia estable con oscilaciones.\n\n"
        "### Conceptos clave\n"
        "- **Momentum**: acumulacion de direccion previa.\n\n"
        "#### Para profundizar (web)\n"
        "- recurso externo ruidoso"
    )

    compacto = escriba._compactar_tramo_vista(cuerpo, max_chars=180)

    assert "Apuntes del tramo" not in compacto
    assert "descenso de gradiente" in compacto
    assert "Para profundizar" not in compacto


def test_postproceso_usa_tiempos_relativos_y_transcript_compacto():
    origen = {"origen": "vivo", "inicio": 1000.0}

    assert post_procesar.fmt_t(1030.0, origen) == "00:30"
    assert post_procesar.fmt_t(3661.0, {"origen": "video"}) == "1:01:01"

    segs = [
        {"t": 1000.0, "fin": 1001.0, "texto": "Primera frase"},
        {"t": 1001.2, "fin": 1002.0, "texto": "continuacion inmediata"},
        {"t": 1010.0, "fin": 1011.0, "texto": "nuevo parrafo"},
    ]
    lineas = post_procesar._lineas_segmentos_compactas(segs, origen, gap_seg=3.0)

    assert lineas == [
        "[00:00] Primera frase continuacion inmediata",
        "[00:10] nuevo parrafo",
    ]


def test_postproceso_limpia_titulo_duplicado_resumen():
    resumen = "## Resumen Ejecutivo de Clase\n\nContenido limpio."

    assert post_procesar._limpiar_resumen_ejecutivo(resumen) == "Contenido limpio."


def test_normalizar_audio_whisper_amplifica_voz_baja_sin_clipping():
    t = np.linspace(0, 1, 16000, endpoint=False, dtype=np.float32)
    voz_baja = (0.001 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    rms_antes = float(np.sqrt(np.mean(np.square(voz_baja, dtype=np.float64))))

    salida, diag = transcriptor_vivo.normalizar_audio_whisper(
        voz_baja, target_rms=0.02, max_gain=20.0, floor_rms=0.00002,
        floor_peak=0.00012)
    rms_despues = float(np.sqrt(np.mean(np.square(salida, dtype=np.float64))))

    assert diag["applied"] is True
    assert diag["gain"] > 1.0
    assert rms_despues > rms_antes * 10
    assert float(np.max(np.abs(salida))) <= 0.980001


def test_normalizar_audio_whisper_no_levanta_pcm_casi_plano():
    casi_plano = np.full(16000, 1.0 / 32768.0, dtype=np.float32)

    salida, diag = transcriptor_vivo.normalizar_audio_whisper(casi_plano)

    assert diag["applied"] is False
    assert diag["reason"] == "near_silence"
    assert np.allclose(salida, casi_plano)


def test_api_estado_no_expone_texto_del_profesor_en_monitor(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Monitor vivo", curso="curso-monitor")
    _fijar_estado(d, inicio=1000.0, estado="grabando", auto_pausa=False, parte=1)
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 900.0, "parte": 1, "texto": "FUERA_DE_VENTANA",
    })
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1030.0, "parte": 1, "texto": "inicio del bloque reciente",
    })
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1115.0, "parte": 1, "texto": "cierre del bloque reciente",
    })
    monkeypatch.setattr(panel.time, "time", lambda: 1120.0)

    data = panel.app.test_client().get("/api/estado").get_json()

    serializado = str(data)
    assert "ultimo_segmento_texto" not in data
    assert "transcript_blocks" not in data
    assert "trans_visible" not in data
    assert "inicio del bloque reciente" not in serializado
    assert "cierre del bloque reciente" not in serializado
    assert "FUERA_DE_VENTANA" not in serializado


def test_api_estado_no_marca_pendiente_un_commit_vacio(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Commit vacío", curso="curso-stt")
    wav = d / "audio" / "chunk_000000001000_p1_v10.wav"
    wav.touch()
    audio_manifest.append_event(d, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(
        d, wav.stem, [], {"sequence": 1, "path": wav.name})

    data = panel.app.test_client().get("/api/estado").get_json()

    assert data["stt_pending_count"] == 0
    assert data["stt_invalid_count"] == 0
    assert data["stt_empty_completed_count"] == 1
    assert "wav pendientes 0" in data["trans_diag"]


def test_panel_y_postproceso_invalidan_commit_si_cambian_bytes_wav(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Hash físico", curso="curso-stt")
    wav = d / "audio" / "chunk_hash_fisico.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"wav-original")
    sha = hashlib.sha256(wav.read_bytes()).hexdigest()
    audio_manifest.append_event(d, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "sha256": sha, "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(
        d, wav.stem, [], {"path": wav.name, "input_sha256": sha})
    wav.write_bytes(b"wav-cambiado")

    # El monitor LIVE es deliberadamente O(1): no rehashea todos los WAV cada
    # pocos segundos. La barrera terminal sí debe descubrir la alteración.
    live = panel.app.test_client().get("/api/estado").get_json()
    _fijar_estado(d, estado="fin")
    final = panel.app.test_client().get("/api/estado").get_json()
    cobertura = post_procesar._comparar_wavs_transcript(d, [])

    assert live["stt_pending_count"] == 0
    assert live["stt_invalid_count"] == 0
    assert final["stt_pending_count"] == 1
    assert final["stt_invalid_count"] == 1
    assert final["consolidation_ready"] is False
    assert cobertura["stt"]["invalid_chunks"] == [wav.stem]
    assert cobertura["wav_sin_transcript"] == [wav.name]


def test_api_estado_expone_snapshot_de_persistencia_wav(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Cola WAV", curso="curso-cola")
    snapshot = {
        "queue_depth": 2,
        "queue_depth_max": 3,
        "active_jobs": 1,
        "pending_jobs": 3,
        "enqueue_wait_count": 4,
        "persistence_seconds_last": 0.25,
    }
    estado.actualizar_estado(d, wav_persistence=snapshot)

    data = panel.app.test_client().get("/api/estado").get_json()

    assert data["wav_persistence"] == snapshot
    assert "WAV cola 2/3" in data["trans_diag"]
    assert "pendientes 3" in data["trans_diag"]


def test_postproceso_cobertura_temporal_reporta_auto_pausa(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("MIT corto", curso="curso-mit")
    _fijar_estado(
        d,
        inicio=1000.0,
        estado="fin",
        auto_pausa=True,
        auto_pausa_motivo="silencio",
        parte=1,
    )
    _marcas(
        d,
        {"t": 1000.0, "tipo": "inicio", "parte": 1},
        {"t": 1120.0, "tipo": "auto_pausa", "parte": 1,
         "nota": "120s de silencio (rms=66)"},
        {"t": 1900.0, "tipo": "fin", "parte": 1},
    )

    reporte = post_procesar.analizar_cobertura_temporal(d, [], {"origen": "vivo", "inicio": 1000.0})
    md = post_procesar._reporte_cobertura_temporal_md(reporte)

    assert reporte["estado"] == "advertencia"
    assert reporte["captura_sesion"]["auto_pausa_total"] == 1
    assert reporte["captura_sesion"]["duracion_almacenando_fmt"] == "02:00"
    assert "Auto-pausas registradas: 1 (primera a 02:00)" in md
    assert "la clase terminó estando en auto-pausa" in md


@pytest.mark.timeout(5)
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    eventos=st.lists(
        st.sampled_from(["pausa", "seguir", "auto_pausa", "auto_seguir"]),
        min_size=0,
        max_size=20,
    )
)
def test_tiempo_activo_hypothesis_no_supera_reloj(monkeypatch, tmp_path, eventos):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Timeline property", curso="curso-timer")
    st_actual = _fijar_estado(d, inicio=1000.0, estado="grabando",
                              auto_pausa=False, parte=1)
    marcas = [{"t": 1000.0, "tipo": "inicio", "parte": 1}]
    activo = True
    esperado = 0.0
    cursor = 1000.0
    ultimo_activo = cursor

    for i, tipo in enumerate(eventos, start=1):
        cursor = 1000.0 + i * 17.0
        if tipo in {"pausa", "auto_pausa"}:
            if activo:
                esperado += cursor - ultimo_activo
                activo = False
        elif tipo in {"seguir", "auto_seguir"}:
            if not activo:
                ultimo_activo = cursor
                activo = True
        marcas.append({"t": cursor, "tipo": tipo, "parte": 1})

    ahora = 1000.0 + (len(eventos) + 1) * 17.0
    if activo:
        esperado += ahora - ultimo_activo
    st_actual.update({
        "estado": "grabando",
        "auto_pausa": bool(not activo and marcas[-1]["tipo"] == "auto_pausa"),
    })
    estado.escribir_estado(d, st_actual)
    _marcas(d, *marcas)

    obtenido = panel._segundos_almacenando(d, st_actual, ahora)

    assert obtenido == pytest.approx(esperado)
    assert 0.0 <= obtenido <= ahora - 1000.0
