import ctypes
from pathlib import Path

import pytest

from src import audio_manifest, captura_audio, captura_pantalla, loopback_proceso


def _capturador_posicion(*, rate=100, ultimo=None):
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador.frame_bytes = 2
    capturador.rate = rate
    capturador._pendiente = bytearray()
    capturador._ultimo_device_end = ultimo
    capturador._continuidad = {
        "gap_frames": 0,
        "overlap_frames": 0,
        "timeout_padding_frames": 0,
        "data_discontinuity_packets": 0,
        "device_position_start": None,
        "device_position_end": ultimo,
        "device_position_event": "",
    }
    return capturador


def test_device_position_creciente_conserva_continuidad_normal():
    capturador = _capturador_posicion()

    capturador._anexar_paquete(b"\x01\x00" * 10, 10, 100)
    capturador._anexar_paquete(b"\x02\x00" * 5, 5, 110)

    assert capturador._ultimo_device_end == 115
    assert capturador._continuidad["gap_frames"] == 0
    assert capturador._continuidad["overlap_frames"] == 0
    assert capturador._continuidad["device_position_event"] == ""


def test_device_position_cero_repetida_con_qpc_creciente_usa_orden_de_llegada():
    capturador = _capturador_posicion()

    capturador._anexar_paquete(
        b"\x01\x00" * 10, 10, 0, qpc_position=1_000_000)
    capturador._anexar_paquete(
        b"\x02\x00" * 10, 10, 0, qpc_position=1_100_000)

    assert capturador._pendiente == b"\x01\x00" * 10 + b"\x02\x00" * 10
    assert capturador._ultimo_device_end == 20
    assert capturador._continuidad["device_position_event"] == (
        "device_position_unavailable")
    assert capturador._continuidad["device_position_unavailable_packets"] == 1


def test_device_position_no_disponible_infiere_silencio_largo_desde_qpc():
    capturador = _capturador_posicion(rate=100)

    capturador._anexar_paquete(
        b"\x01\x00" * 10, 10, 0, qpc_position=1_000_000)
    capturador._anexar_paquete(
        b"\x02\x00" * 10, 10, 0, qpc_position=2_500_000)

    assert capturador._pendiente == (
        b"\x01\x00" * 10 + b"\x00\x00" * 5 + b"\x02\x00" * 10)
    assert capturador._continuidad["qpc_inferred_gap_frames"] == 5
    assert capturador._continuidad["gap_frames"] == 5
    assert capturador._ultimo_device_end == 25


def test_qpc_recovery_tiene_rollback_sin_inventar_frames(monkeypatch):
    monkeypatch.setenv("CLASE_LOOPBACK_QPC_RECOVERY", "0")
    capturador = _capturador_posicion(rate=100)

    capturador._anexar_paquete(
        b"\x01\x00" * 10, 10, 0, qpc_position=1_000_000)
    capturador._anexar_paquete(
        b"\x02\x00" * 10, 10, 0, qpc_position=2_500_000)

    assert capturador._pendiente == b"\x01\x00" * 10 + b"\x02\x00" * 10
    assert capturador._continuidad["qpc_inferred_gap_frames"] == 0


def test_jitter_qpc_subframe_no_inventa_silencio():
    capturador = _capturador_posicion(rate=100)

    capturador._anexar_paquete(
        b"\x01\x00" * 10, 10, 0, qpc_position=1_000_000)
    capturador._anexar_paquete(
        b"\x02\x00" * 10, 10, 0, qpc_position=2_049_000)

    assert capturador._pendiente == b"\x01\x00" * 10 + b"\x02\x00" * 10
    assert capturador._continuidad["qpc_inferred_gap_frames"] == 0


def test_qpc_decreciente_declara_paquete_fuera_de_orden_sin_filtrarlo():
    capturador = _capturador_posicion()
    capturador._anexar_paquete(
        b"\x01\x00" * 10, 10, 0, qpc_position=1_000_000)
    previo = bytes(capturador._pendiente)

    with pytest.raises(loopback_proceso.DiscontinuidadPosicion) as error:
        capturador._anexar_paquete(
            b"\x02\x00" * 10, 10, 10, qpc_position=900_000)

    assert error.value.kind == "qpc_out_of_order"
    assert bytes(capturador._pendiente) == previo


def test_padding_por_timeout_no_avanza_el_reloj_hardware():
    capturador = _capturador_posicion(ultimo=110)

    capturador._rellenar_timeout(1000)

    assert capturador._ultimo_device_end == 110
    assert capturador._continuidad["device_position_end"] == 110
    assert capturador._continuidad["timeout_padding_frames"] == 1000
    capturador._pendiente.clear()
    capturador._anexar_paquete(b"\x03\x00" * 5, 5, 110)
    assert capturador._pendiente == b"\x03\x00" * 5


def test_device_position_negativa_grande_declara_reset_sin_filtrar_frames():
    capturador = _capturador_posicion(rate=100, ultimo=2000)

    with pytest.raises(loopback_proceso.DiscontinuidadPosicion) as error:
        capturador._anexar_paquete(b"\x04\x00" * 10, 10, 0)

    assert error.value.kind == "device_position_reset"
    assert capturador._pendiente == b""
    assert capturador._continuidad["device_position_event"] == (
        "device_position_reset")


def test_reset_a_cero_se_detecta_aunque_el_delta_sea_pequeno():
    capturador = _capturador_posicion(rate=100, ultimo=150)
    capturador._ultimo_device_position_raw = 140
    capturador._ultimo_qpc_position = 1_000_000
    capturador._device_position_progress_seen = True

    with pytest.raises(loopback_proceso.DiscontinuidadPosicion) as error:
        capturador._anexar_paquete(
            b"\x04\x00" * 10, 10, 0, qpc_position=1_100_000)

    assert error.value.kind == "device_position_reset"
    assert capturador._pendiente == b""


def test_device_position_positivo_imposible_es_terminal_y_no_filtra_frames():
    capturador = _capturador_posicion(rate=100, ultimo=100)

    with pytest.raises(loopback_proceso.DiscontinuidadPosicion) as error:
        capturador._anexar_paquete(b"\x05\x00" * 10, 10, 1000)

    assert error.value.kind == "device_position_invalid_jump"
    assert capturador._pendiente == b""


def test_nueva_instancia_process_inicia_epoca_y_stream_id_distintos(monkeypatch):
    ticks = iter((100, 200))
    com = {"init": 0, "uninit": 0}
    monkeypatch.setattr(
        loopback_proceso.time, "monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(
        loopback_proceso.comtypes, "CoInitialize",
        lambda: com.__setitem__("init", com["init"] + 1),
    )
    monkeypatch.setattr(
        loopback_proceso.comtypes, "CoUninitialize",
        lambda: com.__setitem__("uninit", com["uninit"] + 1),
    )
    monkeypatch.setattr(loopback_proceso, "_kernel32_api", lambda: object())
    monkeypatch.setattr(
        loopback_proceso.CapturadorProceso, "_abrir", lambda self: None)

    primero = loopback_proceso.CapturadorProceso(99)
    primero._ultimo_device_end = 123
    primero.close()
    primero.close()
    segundo = loopback_proceso.CapturadorProceso(99)
    segundo.close()

    assert primero.stream_id != segundo.stream_id
    assert segundo._ultimo_device_end is None
    assert com == {"init": 2, "uninit": 2}


def test_cambio_ruta_conserva_razon_tipificada_de_continuidad():
    error = loopback_proceso.DiscontinuidadPosicion(
        "device_position_reset", -100, 0, 100)

    cambio = captura_audio.CambioRutaCaptura(99, error)

    assert cambio.error_type == "DiscontinuidadPosicion"
    assert cambio.failure_reason == "device_position_reset"


def test_flag_wasapi_discontinuity_excluye_el_paquete_dudoso():
    raw = (ctypes.c_ubyte * 8)(*b"\x10\x00\x20\x00\x30\x00\x40\x00")

    class CaptureFalso:
        def __init__(self):
            self.release_calls = 0

        def GetNextPacketSize(self):
            return 2

        def GetBuffer(self):
            return (
                ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)),
                2,
                loopback_proceso.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY,
                50,
                0,
            )

        def ReleaseBuffer(self, frames):
            self.release_calls += 1

    capturador = _capturador_posicion()
    capturador._pendiente = b"pcm-previo-valido"
    capturador._capture = CaptureFalso()
    capturador.frame_bytes = 4

    with pytest.raises(loopback_proceso.DiscontinuidadWasapi):
        capturador._drenar_paquetes()

    assert capturador._pendiente == b"pcm-previo-valido"
    assert bytes(raw) not in capturador._pendiente
    assert capturador._capture.release_calls == 1
    assert capturador._continuidad["data_discontinuity_packets"] == 1
    assert capturador._continuidad["device_position_event"] == (
        "wasapi_data_discontinuity")


class _PyAudioDispositivos:
    def __init__(self, salida, loopbacks):
        self.salida = salida
        self.loopbacks = loopbacks

    def get_loopback_device_info_generator(self):
        return iter(self.loopbacks)

    def get_host_api_info_by_type(self, _tipo):
        return {"defaultOutputDevice": self.salida["index"]}

    def get_device_info_by_index(self, _index):
        return self.salida


def test_fallback_elige_loopback_de_la_salida_wasapi_predeterminada():
    realtek = {"index": 10, "name": "Altavoces Realtek [Loopback]"}
    usb = {"index": 11, "name": "Speakers USB [Loopback]"}
    p = _PyAudioDispositivos(
        {"index": 5, "name": "Speakers USB", "isLoopbackDevice": False},
        [realtek, usb],
    )

    assert captura_audio.dispositivo_loopback(p)["index"] == 11


def test_fallback_no_elige_primer_loopback_si_no_corresponde_a_salida_activa():
    p = _PyAudioDispositivos(
        {"index": 5, "name": "Headset Bluetooth", "isLoopbackDevice": False},
        [{"index": 10, "name": "Altavoces Realtek [Loopback]"}],
    )

    assert captura_audio.dispositivo_loopback(p) is None


def test_salud_audio_limita_alternancias_y_expone_diagnostico_accionable():
    salud = captura_audio.SaludRutasAudio(
        inicio=0.0, max_fallos_ruta=5, max_alternancias=3,
        timeout_sin_audio=100.0)

    salud.registrar("proceso", "failed", ahora=1.0)
    salud.registrar("dispositivo", "flat", ahora=2.0)
    salud.registrar("proceso", "failed", ahora=3.0)
    resultado = salud.registrar("dispositivo", "flat", ahora=4.0)

    assert resultado["requires_attention"] is True
    assert resultado["attention_reason"] == "fallback_cycle_limit"
    assert resultado["alternations"] == 3
    assert resultado["attempt"] == 4


def test_salud_audio_timeout_sin_valido_y_audio_valido_reinicia_contadores():
    salud = captura_audio.SaludRutasAudio(
        inicio=10.0, max_fallos_ruta=5, max_alternancias=5,
        timeout_sin_audio=20.0)
    salud.registrar("dispositivo", "silence", ahora=11.0)

    timeout = salud.evaluar(ahora=31.0)
    assert timeout["requires_attention"] is True
    assert timeout["attention_reason"] == "no_valid_audio_timeout"

    recuperado = salud.registrar("dispositivo", "valid", ahora=32.0)
    assert recuperado["requires_attention"] is False
    assert recuperado["alternations"] == 0
    assert recuperado["last_valid_at"] == 32.0


def test_timeout_sin_audio_no_consume_tiempo_durante_pausa_manual():
    salud = captura_audio.SaludRutasAudio(
        inicio=0.0, max_fallos_ruta=5, max_alternancias=5,
        timeout_sin_audio=10.0)
    salud.registrar("dispositivo", "silence", ahora=1.0)
    salud.pausar(ahora=2.0)

    assert salud.evaluar(ahora=100.0)["requires_attention"] is False
    salud.reanudar(ahora=100.0)
    assert salud.evaluar(ahora=107.0)["requires_attention"] is False
    assert salud.evaluar(ahora=109.0)["requires_attention"] is True


def test_silencio_pcm_valido_sostenido_no_agota_la_ruta_antes_de_auto_pausa():
    salud = captura_audio.SaludRutasAudio(
        inicio=0.0, max_fallos_ruta=5, max_alternancias=5,
        timeout_sin_audio=10.0)

    for instante in (1.0, 5.0, 9.0, 13.0, 17.0):
        snapshot = salud.registrar("proceso", "silence", ahora=instante)
        assert snapshot["requires_attention"] is False

    fallo = salud.registrar("proceso", "failed", ahora=28.0)
    assert fallo["requires_attention"] is True
    assert fallo["attention_reason"] == "no_valid_audio_timeout"


def test_visual_idle_con_audio_en_atencion_conserva_evidencia():
    decision = captura_pantalla.decision_idle_con_audio(
        {"idle": True, "razones": ["pantalla_estable"]},
        {"audio_requires_attention": True},
        ahora=100.0,
    )

    assert decision["omitir"] is False
    assert decision["motivo"] == "audio_requires_attention_conservador"


def test_main_ambas_rutas_sin_audio_limita_ciclo_y_espera_reintento(
        monkeypatch, tmp_path: Path):
    (tmp_path / "audio").mkdir()
    reloj = {"t": 0.0}
    estado = {
        "estado": "grabando", "fuente": "chrome", "parte": 1,
        "velocidad": 1.0, "enfoque": False, "auto_pausa": False,
        "auto_pausa_motivo": "", "dispositivo": "",
        "audio_retry_generation": 0,
    }
    captura_audio.est.escribir_estado(tmp_path, estado)
    (tmp_path / "marcas.jsonl").write_text("", encoding="utf-8")
    procesos = []
    dispositivos = []
    esperas_atencion = []
    fases_antes_de_leer = []

    class ProcesoPlano:
        def __init__(self, pid):
            self.pid = pid
            self.stream_id = f"process-{len(procesos)}"
            self.close_calls = 0
            procesos.append(self)

        def leer(self, *_args, **_kwargs):
            actual = captura_audio.est.leer_estado(tmp_path)
            fases_antes_de_leer.append((
                actual.get("audio_capture_phase"),
                actual.get("audio_capture_stream_id"),
            ))
            raise OSError("process sintetico sin audio")

        def extraer_parcial(self):
            return b"", 100, 1, {"stream_id": self.stream_id}

        def close(self):
            self.close_calls += 1

    class PyAudioPlano:
        def terminate(self):
            pass

    class DispositivoPlano:
        clave = (7, 100, 1)

        def __init__(self, *_args):
            self.stream_id = f"device-{len(dispositivos)}"
            self.close_calls = 0
            dispositivos.append(self)

        def leer(self, segundos, *_args, **_kwargs):
            actual = captura_audio.est.leer_estado(tmp_path)
            fases_antes_de_leer.append((
                actual.get("audio_capture_phase"),
                actual.get("audio_capture_stream_id"),
            ))
            reloj["t"] += 100.0
            frames = round(100 * segundos)
            return b"\x00\x00" * frames, 100, 1, {
                "stream_id": self.stream_id,
                "start_frame": 0,
                "end_frame": frames,
                "pcm_wall_start": reloj["t"],
                "pcm_wall_end": reloj["t"] + segundos,
            }

        def close(self):
            self.close_calls += 1

    def sleep(segundos):
        actual = captura_audio.est.leer_estado(tmp_path)
        if actual.get("audio_requires_attention"):
            esperas_atencion.append(segundos)
            captura_audio.est.actualizar_estado(tmp_path, estado="fin")
            return
        pytest.fail(f"espera inesperada antes de atención: {segundos}")

    SaludReal = captura_audio.SaludRutasAudio
    monkeypatch.setattr(
        captura_audio, "SaludRutasAudio",
        lambda: SaludReal(
            inicio=0.0, max_fallos_ruta=5, max_alternancias=3,
            timeout_sin_audio=1000.0),
    )
    monkeypatch.setattr(captura_audio.config, "instancia_unica", lambda _n: True)
    monkeypatch.setattr(captura_audio.config, "latido_perdido", lambda: False)
    monkeypatch.setattr(captura_audio.config, "log_error", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.config, "AUDIO_CHUNK_SEG", 1)
    monkeypatch.setattr(captura_audio.config, "VISUAL_IDLE_AUTOPAUSA", False)
    monkeypatch.setattr(captura_audio.est, "clase_activa", lambda: tmp_path)
    monkeypatch.setattr(captura_audio.est, "anexar_jsonl", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.est, "marcar", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.time, "time", lambda: reloj["t"])
    monkeypatch.setattr(captura_audio.time, "monotonic", lambda: reloj["t"])
    monkeypatch.setattr(captura_audio.time, "sleep", sleep)
    monkeypatch.setattr(captura_audio.audio_sesiones, "disponible", lambda: True)
    monkeypatch.setattr(
        captura_audio.audio_sesiones, "sesiones_navegador",
        lambda _f: [{"pid": 99, "root_pid": 99, "pico": 0.0}],
    )
    monkeypatch.setattr(
        captura_audio.audio_sesiones, "pico_navegador", lambda _f: 0.0)
    monkeypatch.setattr(captura_audio.audio_sesiones, "enfoque", lambda *_a: 0)
    monkeypatch.setattr(
        captura_audio.audio_sesiones, "restaurar_enfoques", lambda: 0)
    monkeypatch.setattr(captura_audio.loopback_proceso, "disponible", lambda: True)
    monkeypatch.setattr(
        captura_audio.loopback_proceso, "CapturadorProceso", ProcesoPlano)
    monkeypatch.setattr(captura_audio.pyaudio, "PyAudio", PyAudioPlano)
    monkeypatch.setattr(
        captura_audio, "dispositivo_loopback",
        lambda *_a, **_k: {
            "index": 7, "defaultSampleRate": 100, "maxInputChannels": 1,
            "name": "salida sintética", "selection_reason": "test",
        },
    )
    monkeypatch.setattr(captura_audio, "CapturadorDispositivo", DispositivoPlano)

    assert captura_audio.main() == 0

    eventos = audio_manifest.read_events(tmp_path)
    intentos = [e for e in eventos if e.get("status") == "capturing"]
    terminales = [
        e for e in eventos
        if e.get("status") in {"sealed", "recovered", "discarded_silence",
                               "capture_failed", "cancelled"}
    ]
    assert len(procesos) == 2
    assert len(dispositivos) == 2
    assert len(intentos) == len(terminales) == 4
    assert not any(e.get("status") in {"sealed", "recovered"} for e in eventos)
    assert any(
        e.get("status") == "capture_failed"
        and e.get("reason") == "audio_requires_attention"
        and e.get("capture_outcome") == "requires_attention"
        for e in eventos)
    assert eventos[-1]["status"] == "capture_session_closed"
    estado_final = captura_audio.est.leer_estado(tmp_path)
    assert estado_final["audio_requires_attention"] is True
    assert estado_final["auto_pausa"] is False
    assert estado_final["audio_route_health"]["alternations"] == 3
    assert esperas_atencion == [1]
    assert [fase for fase, _stream in fases_antes_de_leer] == [
        "reading_process", "trying_device_fallback",
        "reading_process", "trying_device_fallback",
    ]
    assert all(stream for _fase, stream in fases_antes_de_leer)
