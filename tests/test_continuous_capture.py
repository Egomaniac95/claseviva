import ctypes
import struct
import sys
import threading
import time
import wave
from contextlib import contextmanager
from types import ModuleType

import pytest

from src import audio_manifest, audio_sesiones, captura_audio, loopback_proceso


class _FakeStream:
    def __init__(self, channels: int):
        self.channels = channels
        self.frame = 0
        self.stop_calls = 0
        self.close_calls = 0

    def read(self, frames: int, exception_on_overflow: bool = False) -> bytes:
        del exception_on_overflow
        out = bytearray()
        for _ in range(frames):
            value = self.frame % 32767
            out.extend(struct.pack("<h", value) * self.channels)
            self.frame += 1
        return bytes(out)

    def stop_stream(self) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _FakePyAudio:
    def __init__(self):
        self.open_calls = 0
        self.stream = None

    def open(self, **kwargs):
        self.open_calls += 1
        self.stream = _FakeStream(kwargs["channels"])
        return self.stream


def test_enfoque_restaura_mute_previo_y_tolera_sesiones_nuevas_o_ausentes(
        monkeypatch):
    class Volumen:
        def __init__(self, muteado):
            self.muteado = bool(muteado)
            self.calls = []

        def GetMute(self):
            return self.muteado

        def SetMute(self, valor, _contexto):
            self.muteado = bool(valor)
            self.calls.append(bool(valor))

    class Proceso:
        def __init__(self, pid, nombre):
            self.pid = pid
            self._nombre = nombre

        def name(self):
            return self._nombre

    class Sesion:
        def __init__(self, sid, pid, nombre, muteado):
            self.InstanceIdentifier = sid
            self.Process = Proceso(pid, nombre)
            self.SimpleAudioVolume = Volumen(muteado)

    navegador = Sesion("chrome", 1, "chrome.exe", False)
    ya_muteada = Sesion("musica", 2, "music.exe", True)
    activa = Sesion("chat", 3, "chat.exe", False)
    nueva = Sesion("nueva", 4, "new.exe", False)
    actuales = {"sesiones": [navegador, ya_muteada, activa]}

    @contextmanager
    def sesiones_falsas():
        yield list(actuales["sesiones"])

    monkeypatch.setattr(audio_sesiones, "disponible", lambda: True)
    monkeypatch.setattr(audio_sesiones, "_sesiones", sesiones_falsas)
    monkeypatch.setattr(audio_sesiones, "_enfoque_snapshots", {}, raising=False)

    assert audio_sesiones.enfoque("chrome", True) == 1
    assert audio_sesiones.enfoque("chrome", True) == 0
    assert "session:musica" not in audio_sesiones._enfoque_snapshots["chrome"]
    assert ya_muteada.SimpleAudioVolume.muteado is True
    assert ya_muteada.SimpleAudioVolume.calls == []
    assert activa.SimpleAudioVolume.calls == [True]
    assert navegador.SimpleAudioVolume.calls == []

    actuales["sesiones"] = [navegador, ya_muteada, nueva]
    assert audio_sesiones.enfoque("chrome", True) == 1
    assert nueva.SimpleAudioVolume.muteado is True
    assert audio_sesiones.enfoque("chrome", False) == 1
    assert nueva.SimpleAudioVolume.muteado is False
    assert ya_muteada.SimpleAudioVolume.muteado is True
    assert audio_sesiones.enfoque("chrome", False) == 0


def test_enfoque_dos_procesos_solo_restauran_sesiones_que_cambiaron(monkeypatch):
    class Volumen:
        def __init__(self):
            self.muteado = False
            self.calls = []

        def GetMute(self):
            return self.muteado

        def SetMute(self, valor, _contexto):
            self.muteado = bool(valor)
            self.calls.append(bool(valor))

    class Proceso:
        pid = 41

        @staticmethod
        def name():
            return "music.exe"

    class Sesion:
        InstanceIdentifier = "musica-compartida"
        Process = Proceso()
        SimpleAudioVolume = Volumen()

    @contextmanager
    def sesiones_falsas():
        yield [Sesion]

    monkeypatch.setattr(audio_sesiones, "disponible", lambda: True)
    monkeypatch.setattr(audio_sesiones, "_sesiones", sesiones_falsas)
    snapshot_panel = {}
    snapshot_captura = {}

    # Simula dos procesos: cada uno posee su propio diccionario de snapshots.
    monkeypatch.setattr(audio_sesiones, "_enfoque_snapshots", snapshot_panel)
    assert audio_sesiones.enfoque("chrome", True) == 1
    audio_sesiones._enfoque_snapshots = snapshot_captura
    assert audio_sesiones.enfoque("chrome", True) == 0
    assert snapshot_captura.get("chrome", {}) == {}

    audio_sesiones._enfoque_snapshots = snapshot_panel
    assert audio_sesiones.enfoque("chrome", False) == 1
    assert Sesion.SimpleAudioVolume.muteado is False
    audio_sesiones._enfoque_snapshots = snapshot_captura
    assert audio_sesiones.enfoque("chrome", False) == 0
    assert Sesion.SimpleAudioVolume.muteado is False
    assert Sesion.SimpleAudioVolume.calls == [True, False]


def test_enfoque_conserva_solo_restauraciones_fallidas_para_reintento(monkeypatch):
    class Volumen:
        def __init__(self):
            self.muteado = False
            self.fallar_restauracion = True
            self.calls = []

        def GetMute(self):
            return self.muteado

        def SetMute(self, valor, _contexto):
            if not valor and self.fallar_restauracion:
                self.fallar_restauracion = False
                raise RuntimeError("sesion COM transitoria")
            self.muteado = bool(valor)
            self.calls.append(bool(valor))

    class Proceso:
        pid = 42

        @staticmethod
        def name():
            return "chat.exe"

    class Sesion:
        InstanceIdentifier = "chat-reintentable"
        Process = Proceso()
        SimpleAudioVolume = Volumen()

    @contextmanager
    def sesiones_falsas():
        yield [Sesion]

    monkeypatch.setattr(audio_sesiones, "disponible", lambda: True)
    monkeypatch.setattr(audio_sesiones, "_sesiones", sesiones_falsas)
    monkeypatch.setattr(audio_sesiones, "_enfoque_snapshots", {})
    monkeypatch.setattr(audio_sesiones.config, "log_error", lambda *_args: None)

    assert audio_sesiones.enfoque("chrome", True) == 1
    assert audio_sesiones.enfoque("chrome", False) == 0
    assert Sesion.SimpleAudioVolume.muteado is True
    assert audio_sesiones._enfoque_snapshots["chrome"] == {
        "session:chat-reintentable": False,
    }

    assert audio_sesiones.restaurar_enfoques() == 1
    assert Sesion.SimpleAudioVolume.muteado is False
    assert audio_sesiones._enfoque_snapshots == {}


def test_sesiones_balancea_com_incluso_si_pycaw_falla(monkeypatch):
    llamadas = []
    comtypes = ModuleType("comtypes")
    comtypes.CoInitialize = lambda: llamadas.append("init")
    comtypes.CoUninitialize = lambda: llamadas.append("uninit")
    pycaw_pkg = ModuleType("pycaw")
    pycaw_pkg.__path__ = []
    pycaw_mod = ModuleType("pycaw.pycaw")

    class AudioUtilities:
        @staticmethod
        def GetAllSessions():
            llamadas.append("get")
            raise RuntimeError("enumeración fallida")

    pycaw_mod.AudioUtilities = AudioUtilities
    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setitem(sys.modules, "pycaw", pycaw_pkg)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", pycaw_mod)

    with pytest.raises(RuntimeError, match="enumeración fallida"):
        with audio_sesiones._sesiones():
            pass

    assert llamadas == ["init", "get", "uninit"]


def test_dispositivo_persistente_rota_frames_sin_perdida():
    p = _FakePyAudio()
    dev = {"index": 7, "defaultSampleRate": 100, "maxInputChannels": 1}

    capturador = captura_audio.CapturadorDispositivo(p, dev)
    a, _, _, _ = capturador.leer(30)
    b, _, _, _ = capturador.leer(30)
    c, _, _, _ = capturador.leer(5)
    capturador.close()

    assert p.open_calls == 1
    assert [len(a), len(b), len(c)] == [6000, 6000, 1000]
    valores = struct.unpack("<6500h", a + b + c)
    assert valores == tuple(range(6500))
    assert p.stream.stop_calls == 1
    assert p.stream.close_calls == 1


def test_watchdog_cambia_ruta_en_dos_segundos_de_pcm_plano():
    watchdog = captura_audio.WatchdogCaptura(
        "chrome", obtener_pico=lambda _fuente: 0.2,
        ventana_seg=1.0, ventanas_rotas=2)
    segundo_plano = b"\x00\x00" * 100

    watchdog.observar(segundo_plano, rate=100, canales=1)
    with pytest.raises(captura_audio.CapturaRotaTemprana) as exc:
        watchdog.observar(segundo_plano, rate=100, canales=1)

    assert exc.value.ventanas == 2
    assert exc.value.pico_sesion == pytest.approx(0.2)


def test_watchdog_no_confunde_silencio_del_navegador_con_ruta_rota():
    watchdog = captura_audio.WatchdogCaptura(
        "chrome", obtener_pico=lambda _fuente: 0.0,
        ventana_seg=1.0, ventanas_rotas=2)
    segundo_plano = b"\x00\x00" * 100

    watchdog.observar(segundo_plano, rate=100, canales=1)
    watchdog.observar(segundo_plano, rate=100, canales=1)

    assert watchdog.picos == [0.0, 0.0]


def test_process_loopback_recorta_solape_sin_inventar_gap_siguiente():
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador.frame_bytes = 2
    capturador.rate = 100
    capturador._pendiente = bytearray()
    capturador._ultimo_device_end = None
    capturador._continuidad = {
        "gap_frames": 0,
        "overlap_frames": 0,
        "device_position_start": None,
        "device_position_end": None,
    }

    capturador._anexar_paquete(b"\x01\x00" * 10, 10, 0)
    capturador._pendiente.clear()
    capturador._anexar_paquete(b"\x02\x00" * 10, 10, 5)
    assert len(capturador._pendiente) == 5 * 2
    assert capturador._continuidad["overlap_frames"] == 5
    assert capturador._ultimo_device_end == 15

    capturador._pendiente.clear()
    capturador._anexar_paquete(b"\x03\x00" * 5, 5, 15)
    assert capturador._continuidad["gap_frames"] == 0


def test_process_loopback_hace_explicito_salto_de_device_position():
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador.frame_bytes = 2
    capturador.rate = 100
    capturador._pendiente = bytearray()
    capturador._ultimo_device_end = 10
    capturador._continuidad = {
        "gap_frames": 0,
        "overlap_frames": 0,
        "device_position_start": None,
        "device_position_end": None,
    }

    capturador._anexar_paquete(b"\x04\x00" * 2, 2, 12)

    assert capturador._continuidad["gap_frames"] == 2
    assert bytes(capturador._pendiente[:4]) == b"\x00" * 4


def test_process_loopback_entrega_al_preview_pcm_despues_de_gap_y_overlap():
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador.frame_bytes = 2
    capturador.rate = 100
    capturador._pendiente = bytearray()
    capturador._ultimo_device_end = None
    capturador._continuidad = {
        "gap_frames": 0,
        "overlap_frames": 0,
        "device_position_start": None,
        "device_position_end": None,
    }

    primero = capturador._anexar_paquete(b"\x01\x00" * 4, 4, 10)
    con_gap = capturador._anexar_paquete(b"\x02\x00" * 2, 2, 16)
    con_overlap = capturador._anexar_paquete(b"\x03\x00" * 4, 4, 17)

    assert primero == b"\x01\x00" * 4
    assert con_gap == b"\x00\x00" * 2 + b"\x02\x00" * 2
    assert con_overlap == b"\x03\x00" * 3
    assert capturador._continuidad["gap_frames"] == 2
    assert capturador._continuidad["overlap_frames"] == 1


def test_process_loopback_asigna_carryover_preview_al_mismo_corte_que_el_wav():
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador._cerrado = False
    capturador.frame_bytes = 2
    capturador.rate = 100
    capturador.canales = 1
    capturador.stream_id = "process-test"
    capturador.wall_anchor = 1000.0
    capturador._delivered_frames = 0
    capturador._pendiente = bytearray(
        b"\x01\x00" * 2 + b"\x02\x00")
    first_preview = []
    second_preview = []

    first, _ = capturador.leer_frames(
        2, consumer=lambda data, _rate, _channels: first_preview.append(data))
    second, _ = capturador.leer_frames(
        1, consumer=lambda data, _rate, _channels: second_preview.append(data))

    assert first == b"\x01\x00" * 2
    assert second == b"\x02\x00"
    assert b"".join(first_preview) == first
    assert b"".join(second_preview) == second


def test_process_loopback_rechaza_salto_de_posicion_descontrolado():
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador.frame_bytes = 2
    capturador.rate = 100
    capturador._pendiente = bytearray()
    capturador._ultimo_device_end = 10
    capturador._continuidad = {
        "gap_frames": 0,
        "overlap_frames": 0,
        "device_position_start": None,
        "device_position_end": None,
    }

    with pytest.raises(OSError, match="device_position"):
        capturador._anexar_paquete(b"\x05\x00" * 2, 2, 500)


def test_rms_y_peak_submuestrean_ambos_canales_estereo():
    pcm = b"".join(struct.pack("<hh", 0, 1200) for _ in range(300))

    assert captura_audio.calcular_rms(pcm) > 700
    assert captura_audio.calcular_peak(pcm) == 1200


def test_pcm_fuerte_no_se_descarta_si_medidor_de_sesion_esta_en_cero():
    clasificacion = captura_audio.clasificar_muestra_audio(
        "chrome", rms=1200.0, peak=4000, pico_sesion=0.0, silencio_rms=300)

    assert clasificacion["pcm_util"] is True
    assert clasificacion["es_silencio"] is False
    assert clasificacion["guardar"] is True


def test_watchdog_no_publica_pcm_parcial_plano():
    assert captura_audio.pcm_parcial_publicable(
        b"\x00\x00" * 200, canales=1, silencio_rms=300) is False
    assert captura_audio.pcm_parcial_publicable(
        b"\xd0\x07" * 200, canales=1, silencio_rms=300) is True


def test_dispositivo_expone_reloj_pcm_derivado_de_frames(monkeypatch):
    monkeypatch.setattr(captura_audio.time, "time", lambda: 1234.5)
    p = _FakePyAudio()
    dev = {"index": 7, "defaultSampleRate": 100, "maxInputChannels": 1}

    capturador = captura_audio.CapturadorDispositivo(p, dev)
    _, _, _, primero = capturador.leer(0.25)
    _, _, _, segundo = capturador.leer(0.10)
    capturador.close()

    assert primero["pcm_wall_start"] == pytest.approx(1234.5)
    assert primero["pcm_wall_end"] == pytest.approx(1234.75)
    assert segundo["pcm_wall_start"] == pytest.approx(1234.75)
    assert segundo["pcm_wall_end"] == pytest.approx(1234.85)


def test_timeout_no_avanza_device_position_y_acepta_siguiente_paquete_real():
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador.frame_bytes = 2
    capturador.rate = 100
    capturador._pendiente = bytearray()
    capturador._ultimo_device_end = 100
    capturador._continuidad = {
        "gap_frames": 0,
        "overlap_frames": 0,
        "timeout_padding_frames": 0,
        "device_position_start": None,
        "device_position_end": 100,
    }

    capturador._rellenar_timeout(10)
    assert capturador._ultimo_device_end == 100
    assert capturador._continuidad["device_position_end"] == 100
    capturador._pendiente.clear()

    capturador._anexar_paquete(b"\x01\x00" * 10, 10, 100)
    assert capturador._pendiente == b"\x01\x00" * 10
    assert capturador._continuidad["overlap_frames"] == 0

    capturador._anexar_paquete(b"\x02\x00" * 5, 5, 110)
    assert capturador._pendiente == b"\x01\x00" * 10 + b"\x02\x00" * 5


def test_process_loopback_expone_reloj_pcm_derivado_de_frames():
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador.frame_bytes = 2
    capturador.rate = 100
    capturador._pendiente = bytearray(b"\x01\x00" * 20)
    capturador._ultimo_device_end = 20
    capturador._delivered_frames = 0
    capturador._cerrado = False
    capturador.stream_id = "process-test"
    capturador.wall_anchor = 2000.0

    _, primero = capturador.leer_frames(10)
    _, segundo = capturador.leer_frames(10)

    assert primero["pcm_wall_start"] == pytest.approx(2000.0)
    assert primero["pcm_wall_end"] == pytest.approx(2000.1)
    assert segundo["pcm_wall_start"] == pytest.approx(2000.1)
    assert segundo["pcm_wall_end"] == pytest.approx(2000.2)


def test_wasapi_data_discontinuity_se_declara_y_conserva_parcial():
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
            assert frames == 2
            self.release_calls += 1

    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador._capture = CaptureFalso()
    capturador.frame_bytes = 4
    capturador.rate = 100
    capturador.canales = 2
    capturador.stream_id = "process-discontinuity"
    capturador.wall_anchor = 5000.0
    parcial_previo = b"\x01\x00\x02\x00"
    capturador._pendiente = bytearray(parcial_previo)
    capturador._ultimo_device_end = None
    capturador._delivered_frames = 0
    capturador._continuidad = {
        "gap_frames": 0,
        "overlap_frames": 0,
        "timeout_padding_frames": 0,
        "data_discontinuity_packets": 0,
        "device_position_start": None,
        "device_position_end": None,
    }

    with pytest.raises(loopback_proceso.DiscontinuidadWasapi):
        capturador._drenar_paquetes()

    parcial, rate, canales, continuidad = capturador.extraer_parcial()
    assert parcial == parcial_previo
    assert (rate, canales) == (100, 2)
    assert continuidad["data_discontinuity_packets"] == 1
    assert continuidad["terminal_discontinuity"] is True
    assert continuidad["pcm_wall_end"] == pytest.approx(5000.01)
    assert capturador._capture.release_calls == 1


def test_recuperacion_huerfana_requiere_manifest_preexistente(tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    wav = audio_dir / "chunk_000000001000_p1_v10.wav"
    captura_audio.guardar_wav(wav, b"\x01\x00" * 100, 100, 1)

    assert captura_audio.recuperar_wavs_huerfanos(tmp_path) == 0
    assert not audio_manifest.manifest_path(tmp_path).exists()

    audio_manifest.append_event(tmp_path, {
        "chunk_id": wav.stem,
        "sequence": 0,
        "status": "capturing",
        "path": wav.name,
        "wall_start": 1000.0,
    })
    assert captura_audio.recuperar_wavs_huerfanos(tmp_path) == 1
    recuperado = audio_manifest.latest_by_chunk(tmp_path)[wav.stem]
    assert recuperado["status"] == "recovered"
    assert recuperado["inferred_legacy"] is False
    assert recuperado["recovered_from_orphan"] is True
    assert captura_audio.recuperar_wavs_huerfanos(tmp_path) == 0
    assert len([
        event for event in audio_manifest.read_events(tmp_path)
        if event.get("chunk_id") == wav.stem
        and event.get("status") in {"sealed", "recovered"}
    ]) == 1


def test_manifest_nuevo_no_adopta_wav_historico_sin_evento(tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    legacy = audio_dir / "chunk_000000001000_p1_v10.wav"
    captura_audio.guardar_wav(legacy, b"\x01\x00" * 100, 100, 1)
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "chunk_000000002000_p1_v10", "sequence": 1,
        "status": "sealed", "path": "chunk_000000002000_p1_v10.wav",
        "transactional_stt_required": True,
    })

    assert captura_audio.recuperar_wavs_huerfanos(tmp_path) == 0
    assert legacy.stem not in audio_manifest.latest_by_chunk(tmp_path)
    records = audio_manifest.capture_records(tmp_path)
    assert any(row["chunk_id"] == legacy.stem and row["inferred_with_manifest"]
               for row in records)


def test_recupera_wav_valido_dejado_por_error_de_persistencia(tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    wav = audio_dir / "chunk_000000001000_p1_v10.wav"
    captura_audio.guardar_wav(wav, b"\x02\x00" * 100, 100, 1)
    audio_manifest.append_event(tmp_path, {
        "chunk_id": wav.stem, "sequence": 1, "status": "capture_failed",
        "path": wav.name, "wall_start": 1000.0,
        "reason": "wav_persistence_failed",
    })

    assert captura_audio.recuperar_wavs_huerfanos(tmp_path) == 1
    recuperado = audio_manifest.latest_by_chunk(tmp_path)[wav.stem]
    assert recuperado["status"] == "recovered"
    assert recuperado["recovery_reason"] == "valid_wav_after_persistence_error"
    assert len(recuperado["sha256"]) == 64


def test_guardar_wav_fsync_antes_de_publicar(monkeypatch, tmp_path):
    destino = tmp_path / "audio.wav"
    original_fsync = captura_audio.os.fsync
    llamadas = []

    def observar_fsync(fd):
        llamadas.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(captura_audio.os, "fsync", observar_fsync)
    captura_audio.guardar_wav(destino, b"\x03\x00" * 50, 100, 1)

    assert llamadas
    assert destino.is_file()
    assert not destino.with_suffix(".part").exists()
    with wave.open(str(destino), "rb") as wav:
        assert wav.getnframes() == 50


def _trabajo_persistencia(
        class_dir, sequence: int, *, frames: int = 100, rate: int = 100,
        wall_end: float | None = None):
    ruta = class_dir / "audio" / f"chunk_{sequence:012d}_p1_v10.wav"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    wall_end = time.time() if wall_end is None else float(wall_end)
    evento = {
        "chunk_id": ruta.stem,
        "sequence": sequence,
        "status": "sealed",
        "path": ruta.name,
        "frame_count": frames,
        "expected_frames": rate * 30,
        "rate": rate,
        "channels": 1,
        "sample_width": 2,
        "duration": frames / rate,
        "pcm_wall_start": wall_end - frames / rate,
        "pcm_wall_end": wall_end,
        "transactional_stt_required": True,
    }
    audio_manifest.append_event(class_dir, {**evento, "status": "capturing"})
    return captura_audio.TrabajoPersistenciaWav(
        ruta=ruta,
        data=b"\x64\x00" * frames,
        rate=rate,
        canales=1,
        manifest_event=evento,
        audio_log={"archivo": ruta.name},
    )


def test_persistidor_publica_sealed_sin_esperar_siguiente_captura(tmp_path):
    publicado = threading.Event()

    def publicar(resultado):
        assert resultado.trabajo.ruta.is_file()
        assert not resultado.trabajo.ruta.with_suffix(".part").exists()
        final = captura_audio.publicar_resultado_persistencia(
            tmp_path, resultado)
        publicado.set()
        return final

    trabajo = _trabajo_persistencia(tmp_path, 1)
    persistidor = captura_audio.PersistidorWav(
        capacidad=1, publicador=publicar)

    assert persistidor.enviar(trabajo) is True
    # No se llama recolectar(): representa al hilo principal ya bloqueado en
    # la siguiente lectura canónica.
    assert publicado.wait(1)
    row = audio_manifest.latest_by_chunk(tmp_path)[trabajo.ruta.stem]
    resultados = persistidor.cerrar()

    assert row["status"] == "sealed"
    assert row["sequence"] == 1
    assert row["sha256"] == captura_audio.audio_manifest.sha256_file(trabajo.ruta)
    assert row["audio_to_sealed_latency_seconds"] < 1.0
    assert resultados[0].manifest_published is True
    assert persistidor.metricas()["published_immediate"] == 1
    assert len([
        event for event in audio_manifest.read_events(tmp_path)
        if event.get("chunk_id") == trabajo.ruta.stem
        and event.get("status") == "sealed"
    ]) == 1


def test_publicacion_inmediata_conserva_sequence_bajo_persistencia_lenta(
        monkeypatch, tmp_path):
    primera_iniciada = threading.Event()
    liberar_primera = threading.Event()
    dos_publicados = threading.Event()
    guardar_original = captura_audio.guardar_wav
    orden = []

    def guardar_lento(ruta, *args, **kwargs):
        if ruta.name.startswith("chunk_000000000001"):
            primera_iniciada.set()
            assert liberar_primera.wait(2)
        return guardar_original(ruta, *args, **kwargs)

    def publicar(resultado):
        final = captura_audio.publicar_resultado_persistencia(
            tmp_path, resultado)
        orden.append(final.trabajo.manifest_event["sequence"])
        if len(orden) == 2:
            dos_publicados.set()
        return final

    monkeypatch.setattr(captura_audio, "guardar_wav", guardar_lento)
    primero = _trabajo_persistencia(tmp_path, 1)
    segundo = _trabajo_persistencia(tmp_path, 2)
    persistidor = captura_audio.PersistidorWav(
        capacidad=1, timeout_envio=1.0, publicador=publicar)

    assert persistidor.enviar(primero) is True
    assert primera_iniciada.wait(1)
    assert persistidor.enviar(segundo) is True
    assert audio_manifest.latest_by_chunk(tmp_path)[primero.ruta.stem]["status"] \
        == "capturing"
    liberar_primera.set()
    assert dos_publicados.wait(2)
    resultados = persistidor.cerrar()

    terminales = [
        event for event in audio_manifest.read_events(tmp_path)
        if event.get("status") == "sealed"
    ]
    assert orden == [1, 2]
    assert [event["sequence"] for event in terminales] == [1, 2]
    assert len({event["chunk_id"] for event in terminales}) == 2
    assert all(resultado.manifest_published for resultado in resultados)


def test_ultimo_chunk_corto_se_sella_antes_de_capture_session_closed(tmp_path):
    trabajo = _trabajo_persistencia(tmp_path, 1, frames=25, rate=100)
    audio_manifest.append_capture_session_event(
        tmp_path, "capture_session_started", capture_session_id="short-final")
    persistidor = captura_audio.PersistidorWav(
        capacidad=1,
        publicador=lambda resultado: captura_audio.publicar_resultado_persistencia(
            tmp_path, resultado),
    )

    assert persistidor.enviar(trabajo) is True
    cierre = captura_audio.cerrar_sesion_captura(
        tmp_path, persistidor, "short-final", reason="fin_test")

    eventos = audio_manifest.read_events(tmp_path)
    sellado = next(event for event in eventos if event.get("status") == "sealed")
    assert sellado["frame_count"] == 25
    assert sellado["duration"] == pytest.approx(0.25)
    assert sellado["expected_frames"] == 3000
    assert eventos.index(sellado) < len(eventos) - 1
    assert eventos[-1]["status"] == "capture_session_closed"
    assert cierre["persistence_metrics"]["published_immediate"] == 1


def test_reinicio_recupera_wav_renombrado_si_falla_publicacion_manifest(
        monkeypatch, tmp_path):
    trabajo = _trabajo_persistencia(tmp_path, 1)
    append_original = captura_audio.audio_manifest.append_event

    def fallar_solo_terminal(target, event):
        if event.get("status") == "sealed":
            raise OSError("manifest temporalmente no disponible")
        return append_original(target, event)

    monkeypatch.setattr(
        captura_audio.audio_manifest, "append_event", fallar_solo_terminal)
    persistidor = captura_audio.PersistidorWav(
        capacidad=1,
        publicador=lambda resultado: captura_audio.publicar_resultado_persistencia(
            tmp_path, resultado),
    )

    assert persistidor.enviar(trabajo) is True
    resultados = persistidor.cerrar()

    assert trabajo.ruta.is_file()
    assert resultados[0].manifest_published is False
    assert resultados[0].publication_error_type == "OSError"
    assert audio_manifest.latest_by_chunk(tmp_path)[trabajo.ruta.stem]["status"] \
        == "capturing"

    # Simula el siguiente arranque: el WAV durable se reabre, hashea y publica
    # una sola vez como recovered.
    monkeypatch.setattr(
        captura_audio.audio_manifest, "append_event", append_original)
    assert captura_audio.recuperar_wavs_huerfanos(tmp_path) == 1
    assert captura_audio.recuperar_wavs_huerfanos(tmp_path) == 0
    finales = [
        event for event in audio_manifest.read_events(tmp_path)
        if event.get("chunk_id") == trabajo.ruta.stem
        and event.get("status") in {"sealed", "recovered"}
    ]
    assert [event["status"] for event in finales] == ["recovered"]


def test_fallo_de_audio_log_no_duplica_sealed_ya_durable(tmp_path):
    trabajo = _trabajo_persistencia(tmp_path, 1)
    persistidor = captura_audio.PersistidorWav(capacidad=1)
    assert persistidor.enviar(trabajo) is True
    resultado = persistidor.cerrar()[0]

    publicado = captura_audio.publicar_resultado_persistencia(
        tmp_path,
        resultado,
        registrar=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("audio_log sintético no disponible")),
    )
    repetido = captura_audio.publicar_resultado_persistencia(
        tmp_path, publicado)

    assert repetido.manifest_published is True
    assert len([
        event for event in audio_manifest.read_events(tmp_path)
        if event.get("chunk_id") == trabajo.ruta.stem
        and event.get("status") == "sealed"
    ]) == 1


def test_persistidor_bloqueado_no_detiene_lectura_continua(
        monkeypatch, tmp_path):
    inicio_escritura = threading.Event()
    liberar_escritura = threading.Event()
    guardar_original = captura_audio.guardar_wav

    def guardar_lento(*args, **kwargs):
        inicio_escritura.set()
        assert liberar_escritura.wait(2)
        return guardar_original(*args, **kwargs)

    monkeypatch.setattr(captura_audio, "guardar_wav", guardar_lento)
    destino = tmp_path / "audio" / "chunk_000000001000_p1_v10.wav"
    destino.parent.mkdir()
    data = b"\x64\x00" * 100
    trabajo = captura_audio.TrabajoPersistenciaWav(
        ruta=destino,
        data=data,
        rate=100,
        canales=1,
        manifest_event={
            "chunk_id": destino.stem,
            "sequence": 1,
            "status": "sealed",
            "path": destino.name,
            "frame_count": 100,
            "rate": 100,
            "channels": 1,
        },
        audio_log={},
    )
    persistidor = captura_audio.PersistidorWav(capacidad=1)
    persistidor.enviar(trabajo)
    assert inicio_escritura.wait(1)

    p = _FakePyAudio()
    dev = {"index": 7, "defaultSampleRate": 100, "maxInputChannels": 1}
    capturador = captura_audio.CapturadorDispositivo(p, dev)
    try:
        bloques = [capturador.leer(0.5)[0] for _ in range(4)]
        valores = struct.unpack("<200h", b"".join(bloques))
        assert valores == tuple(range(200))
        assert persistidor.recolectar() == []
    finally:
        capturador.close()
        liberar_escritura.set()
    resultados = persistidor.cerrar()

    assert len(resultados) == 1
    assert resultados[0].error_type == ""
    assert destino.is_file()


def test_cola_wav_llena_reporta_backpressure_y_cierra_despues_del_drenaje(
        monkeypatch, tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    escritura_iniciada = threading.Event()
    liberar_escritura = threading.Event()
    guardar_original = captura_audio.guardar_wav

    def guardar_bloqueado(*args, **kwargs):
        escritura_iniciada.set()
        assert liberar_escritura.wait(2)
        return guardar_original(*args, **kwargs)

    monkeypatch.setattr(captura_audio, "guardar_wav", guardar_bloqueado)

    def trabajo(indice):
        ruta = audio_dir / f"chunk_{indice}.wav"
        evento = {
            "chunk_id": ruta.stem,
            "sequence": indice,
            "status": "sealed",
            "path": ruta.name,
            "frame_count": 100,
            "rate": 100,
            "channels": 1,
        }
        audio_manifest.append_event(tmp_path, {**evento, "status": "capturing"})
        return captura_audio.TrabajoPersistenciaWav(
            ruta=ruta,
            data=b"\x64\x00" * 100,
            rate=100,
            canales=1,
            manifest_event=evento,
            audio_log={},
        )

    audio_manifest.append_capture_session_event(
        tmp_path, "capture_session_started", capture_session_id="cola-test")
    persistidor = captura_audio.PersistidorWav(
        capacidad=1, timeout_envio=0.0, timeout_cierre=2.0)
    primero, segundo, tercero = (trabajo(i) for i in range(1, 4))

    assert persistidor.enviar(primero) is True
    assert escritura_iniciada.wait(1)
    assert persistidor.enviar(segundo) is True
    assert persistidor.enviar(tercero) is False
    saturada = persistidor.metricas()

    assert saturada["queue_depth"] == 1
    assert saturada["queue_depth_max"] == 1
    assert saturada["active_jobs"] == 1
    assert saturada["pending_jobs"] == 2
    assert saturada["enqueue_wait_count"] >= 1
    assert saturada["enqueue_timeout_count"] == 1
    assert saturada["timed_out"] == 1
    assert saturada["results_pending"] == 1

    liberar_escritura.set()
    cierre = captura_audio.cerrar_sesion_captura(
        tmp_path, persistidor, "cola-test", reason="fin_test")
    finales = persistidor.metricas()
    eventos = audio_manifest.read_events(tmp_path)
    estados = audio_manifest.latest_by_chunk(tmp_path)

    assert finales["queue_depth"] == 0
    assert finales["pending_jobs"] == 0
    assert finales["active_jobs"] == 0
    assert finales["completed"] == 2
    assert finales["failed"] == 1
    assert finales["persistence_seconds_total"] >= 0
    assert estados[primero.ruta.stem]["status"] == "sealed"
    assert estados[segundo.ruta.stem]["status"] == "sealed"
    assert estados[tercero.ruta.stem]["status"] == "capture_failed"
    assert estados[tercero.ruta.stem]["reason"] == "wav_persistence_queue_timeout"
    assert eventos[-1]["status"] == "capture_session_closed"
    assert cierre["persistence_metrics"]["unresolved_at_close"] == 0
    assert cierre["persistence_metrics"]["queue_depth_max"] == 1


def test_cierre_wav_con_timeout_declara_trabajo_sin_resolver(
        monkeypatch, tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    inicio = threading.Event()
    liberar = threading.Event()
    termino = threading.Event()
    guardar_original = captura_audio.guardar_wav

    def guardar_bloqueado(*args, **kwargs):
        inicio.set()
        assert liberar.wait(2)
        try:
            return guardar_original(*args, **kwargs)
        finally:
            termino.set()

    monkeypatch.setattr(captura_audio, "guardar_wav", guardar_bloqueado)
    ruta = audio_dir / "chunk_timeout.wav"
    evento = {
        "chunk_id": ruta.stem,
        "sequence": 1,
        "status": "sealed",
        "path": ruta.name,
        "frame_count": 100,
        "rate": 100,
        "channels": 1,
    }
    audio_manifest.append_capture_session_event(
        tmp_path, "capture_session_started", capture_session_id="timeout-test")
    audio_manifest.append_event(tmp_path, {**evento, "status": "capturing"})
    persistidor = captura_audio.PersistidorWav(
        capacidad=1, timeout_envio=0.0, timeout_cierre=0.0)
    assert persistidor.enviar(captura_audio.TrabajoPersistenciaWav(
        ruta=ruta,
        data=b"\x64\x00" * 100,
        rate=100,
        canales=1,
        manifest_event=evento,
        audio_log={},
    )) is True
    assert inicio.wait(1)

    cierre = {}
    cierre_terminado = threading.Event()

    def cerrar():
        cierre.update(captura_audio.cerrar_sesion_captura(
            tmp_path, persistidor, "timeout-test", reason="timeout_test"))
        cierre_terminado.set()

    hilo_cierre = threading.Thread(target=cerrar)
    hilo_cierre.start()
    try:
        with persistidor._cond:
            assert persistidor._cond.wait_for(
                lambda: persistidor._close_timed_out, timeout=1)
        assert cierre_terminado.is_set() is False
        durante_timeout = persistidor.metricas()
        assert durante_timeout["close_timed_out"] is True
        assert durante_timeout["unresolved_at_close"] == 1
        assert durante_timeout["pending_jobs"] == 1
        assert not any(
            evento.get("status") == "capture_session_closed"
            for evento in audio_manifest.read_events(tmp_path))
    finally:
        liberar.set()

    assert termino.wait(1)
    assert cierre_terminado.wait(1)
    hilo_cierre.join(1)
    estado_chunk = audio_manifest.latest_by_chunk(tmp_path)[ruta.stem]
    assert estado_chunk["status"] == "sealed"
    assert cierre["persistence_metrics"]["close_timed_out"] is True
    assert cierre["persistence_metrics"]["unresolved_at_close"] == 1
    assert cierre["persistence_metrics"]["pending_jobs"] == 0
    assert audio_manifest.read_events(tmp_path)[-1]["status"] == "capture_session_closed"
    barrera = audio_manifest.completion_barrier_report(tmp_path)
    assert barrera["capture_session_closed"] is True
    assert "capture_session_not_closed" not in barrera["reasons"]


def test_vaciar_wav_agota_timeout_y_expone_pendientes_sin_bloquear(
        monkeypatch, tmp_path):
    inicio = threading.Event()
    liberar = threading.Event()
    retorno_vaciar = threading.Event()
    termino_escritura = threading.Event()
    guardar_original = captura_audio.guardar_wav

    def guardar_bloqueado(*args, **kwargs):
        inicio.set()
        assert liberar.wait(2)
        try:
            return guardar_original(*args, **kwargs)
        finally:
            termino_escritura.set()

    monkeypatch.setattr(captura_audio, "guardar_wav", guardar_bloqueado)
    ruta = tmp_path / "audio" / "chunk_drain_timeout.wav"
    ruta.parent.mkdir()
    evento = {
        "chunk_id": ruta.stem,
        "sequence": 1,
        "status": "sealed",
        "path": ruta.name,
        "frame_count": 100,
        "rate": 100,
        "channels": 1,
    }
    persistidor = captura_audio.PersistidorWav(
        capacidad=1, timeout_envio=0.0, timeout_cierre=0.0)
    assert persistidor.enviar(captura_audio.TrabajoPersistenciaWav(
        ruta=ruta,
        data=b"\x64\x00" * 100,
        rate=100,
        canales=1,
        manifest_event=evento,
        audio_log={},
    )) is True
    assert inicio.wait(1)

    resultados_vaciar = []

    def vaciar():
        resultados_vaciar.extend(persistidor.vaciar())
        retorno_vaciar.set()

    hilo_vaciar = threading.Thread(target=vaciar)
    hilo_vaciar.start()
    retorno_antes_de_liberar = retorno_vaciar.wait(0.5)
    metricas_durante_bloqueo = persistidor.metricas()

    liberar.set()
    hilo_vaciar.join(1)
    assert termino_escritura.wait(1)
    resultados_finales = persistidor.vaciar(timeout=1.0)
    resultados_finales.extend(persistidor.cerrar())

    assert retorno_antes_de_liberar is True
    assert resultados_vaciar == []
    assert len(resultados_finales) == 1
    assert resultados_finales[0].error_type == ""
    assert metricas_durante_bloqueo["drain_timed_out"] is True
    assert metricas_durante_bloqueo["drain_timeout_count"] == 1
    assert metricas_durante_bloqueo["drain_wait_count"] == 1
    assert metricas_durante_bloqueo["drain_wait_seconds"] >= 0
    assert metricas_durante_bloqueo["drain_unresolved_last"] == 1
    assert metricas_durante_bloqueo["pending_jobs"] == 1


def test_close_timeout_con_cola_llena_drena_antes_de_cerrar_sesion(
        monkeypatch, tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    inicio = threading.Event()
    liberar = threading.Event()
    termino_escritura = threading.Event()
    guardar_original = captura_audio.guardar_wav

    def guardar_bloqueado(*args, **kwargs):
        inicio.set()
        assert liberar.wait(2)
        try:
            return guardar_original(*args, **kwargs)
        finally:
            termino_escritura.set()

    monkeypatch.setattr(captura_audio, "guardar_wav", guardar_bloqueado)

    def trabajo(indice):
        ruta = audio_dir / f"chunk_timeout_full_{indice}.wav"
        evento = {
            "chunk_id": ruta.stem,
            "sequence": indice,
            "status": "sealed",
            "path": ruta.name,
            "frame_count": 100,
            "rate": 100,
            "channels": 1,
        }
        audio_manifest.append_event(tmp_path, {**evento, "status": "capturing"})
        return captura_audio.TrabajoPersistenciaWav(
            ruta=ruta,
            data=b"\x64\x00" * 100,
            rate=100,
            canales=1,
            manifest_event=evento,
            audio_log={},
        )

    audio_manifest.append_capture_session_event(
        tmp_path, "capture_session_started", capture_session_id="timeout-full")
    persistidor = captura_audio.PersistidorWav(
        capacidad=1, timeout_envio=0.0, timeout_cierre=0.0)
    activo, en_cola = trabajo(1), trabajo(2)
    assert persistidor.enviar(activo) is True
    assert inicio.wait(1)
    assert persistidor.enviar(en_cola) is True

    cierre = {}
    cierre_terminado = threading.Event()

    def cerrar():
        cierre.update(captura_audio.cerrar_sesion_captura(
            tmp_path, persistidor, "timeout-full", reason="timeout_test"))
        cierre_terminado.set()

    hilo_cierre = threading.Thread(target=cerrar)
    hilo_cierre.start()
    try:
        with persistidor._cond:
            assert persistidor._cond.wait_for(
                lambda: persistidor._close_timed_out, timeout=1)
        assert cierre_terminado.is_set() is False
        durante_timeout = persistidor.metricas()
        assert durante_timeout["close_timed_out"] is True
        assert durante_timeout["unresolved_at_close"] == 2
        assert durante_timeout["pending_jobs"] == 2
        assert not any(
            evento.get("status") == "capture_session_closed"
            for evento in audio_manifest.read_events(tmp_path))
    finally:
        liberar.set()

    assert termino_escritura.wait(1)
    assert cierre_terminado.wait(1)
    hilo_cierre.join(1)

    assert persistidor._thread.is_alive() is False
    assert activo.ruta.is_file()
    assert en_cola.ruta.is_file()
    eventos_finales = audio_manifest.read_events(tmp_path)
    estados_finales = audio_manifest.latest_by_chunk(tmp_path)
    assert eventos_finales[-1]["status"] == "capture_session_closed"
    assert estados_finales[activo.ruta.stem]["status"] == "sealed"
    assert estados_finales[en_cola.ruta.stem]["status"] == "sealed"
    assert not any(
        evento.get("status") == "capture_failed"
        and evento.get("chunk_id") in {activo.ruta.stem, en_cola.ruta.stem}
        for evento in eventos_finales)
    assert cierre["persistence_metrics"]["close_timed_out"] is True
    assert cierre["persistence_metrics"]["unresolved_at_close"] == 2
    assert cierre["persistence_metrics"]["pending_jobs"] == 0
    assert captura_audio.recuperar_wavs_huerfanos(tmp_path) == 0
    barrera = audio_manifest.completion_barrier_report(tmp_path)
    assert barrera["capture_session_closed"] is True
    assert "capture_session_not_closed" not in barrera["reasons"]
    metricas_finales = persistidor.metricas()
    assert metricas_finales["worker_alive"] is False
    assert metricas_finales["queue_depth"] == 0
    assert metricas_finales["pending_jobs"] == 0


def test_error_del_persistidor_se_publica_como_capture_failed_desde_main(
        monkeypatch, tmp_path):
    destino = tmp_path / "audio" / "chunk_000000001000_p1_v10.wav"
    destino.parent.mkdir()
    trabajo = captura_audio.TrabajoPersistenciaWav(
        ruta=destino,
        data=b"\x01\x00" * 10,
        rate=100,
        canales=1,
        manifest_event={
            "chunk_id": destino.stem,
            "sequence": 1,
            "status": "sealed",
            "path": destino.name,
            "frame_count": 10,
            "rate": 100,
            "channels": 1,
        },
        audio_log={},
    )
    monkeypatch.setattr(
        captura_audio,
        "guardar_wav",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disco sintetico lleno")),
    )
    persistidor = captura_audio.PersistidorWav(capacidad=1)
    persistidor.enviar(trabajo)
    resultados = persistidor.cerrar()

    hilo_main = threading.get_ident()
    hilos_manifest = []
    append_original = captura_audio.audio_manifest.append_event

    def append_observado(*args, **kwargs):
        hilos_manifest.append(threading.get_ident())
        return append_original(*args, **kwargs)

    monkeypatch.setattr(
        captura_audio.audio_manifest, "append_event", append_observado)
    captura_audio.publicar_resultados_persistencia(tmp_path, resultados)

    evento = audio_manifest.latest_by_chunk(tmp_path)[destino.stem]
    assert evento["status"] == "capture_failed"
    assert evento["reason"] == "wav_persistence_failed"
    assert evento["error_type"] == "OSError"
    assert hilos_manifest == [hilo_main]


def test_com_se_inicializa_y_finaliza_una_sola_vez(monkeypatch):
    llamadas = {"init": 0, "uninit": 0}
    monkeypatch.setattr(
        loopback_proceso.comtypes,
        "CoInitialize",
        lambda: llamadas.__setitem__("init", llamadas["init"] + 1),
    )
    monkeypatch.setattr(
        loopback_proceso.comtypes,
        "CoUninitialize",
        lambda: llamadas.__setitem__("uninit", llamadas["uninit"] + 1),
    )
    monkeypatch.setattr(loopback_proceso, "_kernel32_api", lambda: object())
    monkeypatch.setattr(loopback_proceso.CapturadorProceso, "_abrir", lambda self: None)

    capturador = loopback_proceso.CapturadorProceso(42)
    capturador.close()
    capturador.close()

    assert llamadas == {"init": 1, "uninit": 1}


@pytest.mark.skipif(
    not loopback_proceso.disponible(),
    reason="kernel32 solo esta disponible en Windows",
)
def test_kernel32_declara_handles_sin_truncar_en_x64():
    kernel32 = loopback_proceso._kernel32_api()

    assert kernel32.CreateEventW.restype is loopback_proceso.wintypes.HANDLE
    assert kernel32.WaitForSingleObject.argtypes[0] is loopback_proceso.wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes[0] is loopback_proceso.wintypes.HANDLE


def test_close_delega_release_de_interfaces_a_comtypes():
    class Referencia:
        def __init__(self):
            self.stop_calls = 0
            self.release_calls = 0

        def Stop(self):
            self.stop_calls += 1

        def Release(self):
            self.release_calls += 1

    client = Referencia()
    capture = Referencia()
    capturador = object.__new__(loopback_proceso.CapturadorProceso)
    capturador._cerrado = False
    capturador._client = client
    capturador._capture = capture
    capturador._evento = None
    capturador._kernel32 = None
    capturador._com_inicializado = False

    capturador.close()

    assert client.stop_calls == 1
    assert client.release_calls == 0
    assert capture.release_calls == 0


def test_fallo_tardio_publica_parcial_y_abre_nuevo_intento_para_fallback(
        monkeypatch, tmp_path):
    (tmp_path / "audio").mkdir()
    reloj = {"t": 1000.1}
    estado = {
        "estado": "grabando",
        "fuente": "chrome",
        "parte": 1,
        "velocidad": 1.0,
        "enfoque": False,
        "auto_pausa": False,
        "dispositivo": "",
    }
    lecturas_fallback = []

    def leer_estado(_d):
        eventos = audio_manifest.read_events(tmp_path)
        if lecturas_fallback and any(
                e.get("status") == "sealed" for e in eventos):
            return {**estado, "estado": "fin"}
        return dict(estado)

    def actualizar_estado(_d, **campos):
        estado.update(campos)

    procesos = []

    class ProcesoFallido:
        def __init__(self, pid):
            self.pid = pid
            self.close_calls = 0
            procesos.append(self)

        def leer(self, *_args, **_kwargs):
            reloj["t"] = 1000.8
            raise OSError("ruta WASAPI interrumpida")

        def extraer_parcial(self):
            return b"\xd0\x07" * 50, 100, 1, {
                "gap_frames": 0,
                "overlap_frames": 0,
                "timeout_padding_frames": 0,
                "data_discontinuity_packets": 1,
                "stream_id": "process-partial",
                "start_frame": 0,
                "end_frame": 50,
                "pcm_wall_start": 1000.1,
                "pcm_wall_end": 1000.6,
                "terminal_discontinuity": True,
            }

        def close(self):
            self.close_calls += 1

    class PyAudioFalso:
        def terminate(self):
            pass

    class DispositivoFalso:
        clave = (7, 100, 1)

        def __init__(self, *_args):
            self.closed = False

        def leer(self, segundos, *_args, **_kwargs):
            frames = round(100 * segundos)
            lecturas_fallback.append(frames)
            return b"\xe8\x03" * frames, 100, 1, {
                "gap_frames": 0,
                "overlap_frames": 0,
                "timeout_padding_frames": 0,
                "stream_id": "device-test",
                "start_frame": 0,
                "end_frame": frames,
                "pcm_wall_start": reloj["t"],
                "pcm_wall_end": reloj["t"] + segundos,
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr(captura_audio.config, "instancia_unica", lambda _n: True)
    monkeypatch.setattr(captura_audio.config, "latido_perdido", lambda: False)
    monkeypatch.setattr(captura_audio.config, "log_error", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.config, "AUDIO_CHUNK_SEG", 1)
    monkeypatch.setattr(captura_audio.config, "VISUAL_IDLE_AUTOPAUSA", False)
    monkeypatch.setattr(captura_audio.est, "clase_activa", lambda: tmp_path)
    monkeypatch.setattr(captura_audio.est, "leer_estado", leer_estado)
    monkeypatch.setattr(captura_audio.est, "actualizar_estado", actualizar_estado)
    monkeypatch.setattr(captura_audio.est, "anexar_jsonl", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.est, "marcar", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.time, "time", lambda: reloj["t"])
    monkeypatch.setattr(captura_audio.audio_sesiones, "disponible", lambda: True)
    monkeypatch.setattr(
        captura_audio.audio_sesiones,
        "sesiones_navegador",
        lambda _f: [{"pid": 99, "root_pid": 99, "pico": 0.2}],
    )
    monkeypatch.setattr(captura_audio.audio_sesiones, "pico_navegador", lambda _f: 0.2)
    monkeypatch.setattr(captura_audio.audio_sesiones, "enfoque", lambda *_a: 1)
    monkeypatch.setattr(captura_audio.loopback_proceso, "disponible", lambda: True)
    monkeypatch.setattr(
        captura_audio.loopback_proceso, "CapturadorProceso", ProcesoFallido)
    monkeypatch.setattr(captura_audio.pyaudio, "PyAudio", PyAudioFalso)
    monkeypatch.setattr(
        captura_audio,
        "dispositivo_loopback",
        lambda *_a, **_k: {
            "index": 7,
            "defaultSampleRate": 100,
            "maxInputChannels": 1,
            "name": "salida sintetica",
        },
    )
    monkeypatch.setattr(captura_audio, "CapturadorDispositivo", DispositivoFalso)

    assert captura_audio.main() == 0

    eventos = audio_manifest.read_events(tmp_path)
    sellados = [e for e in eventos if e.get("status") == "sealed"]
    parcial = next(e for e in sellados if e.get("capture_partial"))
    fallback = next(e for e in sellados if not e.get("capture_partial"))
    assert parcial["reason"] == "process_loopback_failed_after_partial"
    assert parcial["frame_count"] == 50
    assert parcial["duration"] == pytest.approx(0.5)
    assert parcial["terminal_discontinuity"] is True
    assert parcial["data_discontinuity_packets"] == 1
    assert parcial["wall_start"] == pytest.approx(1000.1)
    assert parcial["wall_end"] == pytest.approx(1000.6)
    assert parcial["chunk_id"] != fallback["chunk_id"]
    assert fallback["wall_start"] == pytest.approx(1000.8)
    assert fallback["wall_end"] == pytest.approx(1001.8)
    assert lecturas_fallback
    assert procesos[0].close_calls == 1


def test_pcm_plano_cierra_stream_y_reabre_instancia_pyaudio_fresca(
        monkeypatch, tmp_path):
    (tmp_path / "audio").mkdir()
    estado = {
        "estado": "grabando",
        "fuente": "chrome",
        "parte": 1,
        "velocidad": 1.0,
        "enfoque": False,
        "auto_pausa": False,
        "dispositivo": "",
    }
    streams = []
    capturadores = []

    class StreamFalso:
        def __init__(self, plano: bool):
            self.plano = plano
            self.read_calls = 0
            self.stop_calls = 0
            self.close_calls = 0

        def read(self, frames, exception_on_overflow=True):
            del exception_on_overflow
            self.read_calls += 1
            if self.plano and self.read_calls > 1:
                raise RuntimeError("la ruta rota fue reutilizada")
            muestra = 0 if self.plano else 2000
            return struct.pack("<h", muestra) * frames

        def stop_stream(self):
            self.stop_calls += 1

        def close(self):
            self.close_calls += 1

    class PyAudioFalso:
        def __init__(self):
            self.stream = None
            self.terminate_calls = 0

        def open(self, **_kwargs):
            self.stream = StreamFalso(plano=not streams)
            streams.append(self.stream)
            return self.stream

        def terminate(self):
            self.terminate_calls += 1

    CapturadorReal = captura_audio.CapturadorDispositivo

    class CapturadorRastreado(CapturadorReal):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            capturadores.append(self)

    def leer_estado(_d):
        if len(capturadores) >= 2 and streams[-1].read_calls:
            return {**estado, "estado": "fin"}
        return dict(estado)

    def actualizar_estado(_d, **campos):
        estado.update(campos)

    monkeypatch.setattr(captura_audio.config, "instancia_unica", lambda _n: True)
    monkeypatch.setattr(captura_audio.config, "latido_perdido", lambda: False)
    monkeypatch.setattr(captura_audio.config, "log_error", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.config, "AUDIO_CHUNK_SEG", 1)
    monkeypatch.setattr(captura_audio.config, "VISUAL_IDLE_AUTOPAUSA", False)
    monkeypatch.setattr(captura_audio.est, "clase_activa", lambda: tmp_path)
    monkeypatch.setattr(captura_audio.est, "leer_estado", leer_estado)
    monkeypatch.setattr(captura_audio.est, "actualizar_estado", actualizar_estado)
    monkeypatch.setattr(captura_audio.est, "anexar_jsonl", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.est, "marcar", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_audio.audio_sesiones, "disponible", lambda: False)
    monkeypatch.setattr(captura_audio.audio_sesiones, "pico_navegador", lambda _f: 0.2)
    monkeypatch.setattr(captura_audio.loopback_proceso, "disponible", lambda: False)
    monkeypatch.setattr(captura_audio.pyaudio, "PyAudio", PyAudioFalso)
    monkeypatch.setattr(captura_audio, "CapturadorDispositivo", CapturadorRastreado)
    monkeypatch.setattr(
        captura_audio, "dispositivo_loopback",
        lambda *_a, **_k: {
            "index": 7,
            "defaultSampleRate": 100,
            "maxInputChannels": 1,
            "name": "salida sintética",
        },
    )

    assert captura_audio.main() == 0

    assert len(capturadores) == 2
    assert capturadores[0] is not capturadores[1]
    assert streams[0].read_calls == 1
    assert streams[0].stop_calls == 1
    assert streams[0].close_calls == 1
    eventos = audio_manifest.read_events(tmp_path)
    assert any(
        e.get("status") == "capture_failed"
        and e.get("reason") == "browser_active_pcm_flat"
        for e in eventos)
    assert any(e.get("status") == "sealed" for e in eventos)
