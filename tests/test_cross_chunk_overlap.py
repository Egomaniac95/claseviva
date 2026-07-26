import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from src import audio_manifest, transcriptor_vivo


def test_recorte_descarta_segmento_completo_del_contexto_previo():
    lineas = [
        {"t": 98.0, "ini": 0.1, "fin": 0.8, "texto": "solo anterior"},
        {"t": 99.5, "ini": 1.0, "fin": 1.8, "texto": "cruza frontera"},
    ]

    out = transcriptor_vivo._recortar_contexto_previo(
        lineas, solape_reloj=1.5, inicio_actual=100.0)

    assert [x["texto"] for x in out] == ["cruza frontera"]
    assert out[0]["ini"] == 0.0
    assert out[0]["fin"] == 0.3
    assert out[0]["t"] == 100.0


def test_deduplicacion_de_frontera_conserva_solo_texto_nuevo():
    anteriores = [{"t": 99.0, "texto": "los modelos profundos"}]
    actuales = [{
        "t": 100.0,
        "ini": 0.0,
        "fin": 1.0,
        "texto": "Los modelos profundos aprenden representaciones utiles",
    }]

    out = transcriptor_vivo.deduplicar_frontera(
        actuales, anteriores, inicio_actual=100.0)

    assert len(out) == 1
    assert out[0]["texto"] == "aprenden representaciones utiles"


def test_transcribir_chunk_usa_cola_anterior_y_emite_frase_una_vez(
        monkeypatch, tmp_path):
    anterior = tmp_path / "chunk_000000000100_p1_v10.wav"
    actual = tmp_path / "chunk_000000000130_p1_v10.wav"
    anterior.touch()
    actual.touch()

    def fake_cargar(path):
        if str(path).endswith(anterior.name):
            return np.ones(32000, dtype=np.float32) * 0.01
        return np.ones(32000, dtype=np.float32) * 0.02

    monkeypatch.setattr(transcriptor_vivo, "cargar_wav16k", fake_cargar)

    class FakeModel:
        def transcribe(self, datos, **_kwargs):
            assert len(datos) == 56000  # 1.5 s previos + 2 s actuales
            segmento = SimpleNamespace(
                start=1.0,
                end=2.2,
                text=" Los modelos profundos aprenden representaciones utiles",
                avg_logprob=-0.1,
                no_speech_prob=0.0,
                compression_ratio=1.0,
            )
            info = SimpleNamespace(language="es", language_probability=0.99)
            return [segmento], info

    lineas = transcriptor_vivo.transcribir_chunk(
        FakeModel(),
        actual,
        inicio_abs=130.25,
        anterior=anterior,
        lineas_anteriores=[{"t": 129.0, "texto": "los modelos profundos"}],
    )

    assert len(lineas) == 1
    assert lineas[0]["texto"] == "aprenden representaciones utiles"
    assert lineas[0]["chunk"] == actual.name
    assert lineas[0]["t"] == 130.25
    assert lineas[0]["solape_previo_seg"] == 1.5


def test_segmento_que_cruza_solape_interno_conserva_palabras_nuevas(monkeypatch):
    monkeypatch.setattr(
        transcriptor_vivo,
        "_ventanas_audio",
        lambda _datos: [(0.0, np.ones(10)), (10.0, np.ones(10))],
    )

    class FakeModel:
        llamadas = 0

        def transcribe(self, _frag, **_kwargs):
            self.llamadas += 1
            if self.llamadas == 1:
                texto, inicio, fin = "modelos profundos", 8.0, 12.0
            else:
                texto, inicio, fin = "modelos profundos generalizan mejor", 0.5, 3.0
            segmento = SimpleNamespace(
                start=inicio, end=fin, text=texto, avg_logprob=-0.1,
                no_speech_prob=0.0, compression_ratio=1.0,
            )
            return [segmento], SimpleNamespace(
                language="es", language_probability=0.99)

    lineas = transcriptor_vivo._transcribir_audio(
        FakeModel(), np.ones(20), epoch=100.0, parte=1, vel=1.0,
        contexto="", kw={})

    assert [linea["texto"] for linea in lineas] == [
        "modelos profundos", "generalizan mejor"]
    assert lineas[1]["ini"] == 12.0


def test_overlap_entre_wav_exige_continuidad_manifest():
    previo = {
        "stream_id": "stream-a", "start_frame": 0, "end_frame": 3000,
        "rate": 100, "wall_start": 100.0, "wall_end": 130.0,
    }
    continuo = {
        "stream_id": "stream-a", "start_frame": 3000, "end_frame": 6000,
        "rate": 100, "wall_start": 130.0, "wall_end": 160.0,
    }
    cambio_stream = dict(continuo, stream_id="stream-b")
    con_padding = dict(continuo, timeout_padding_frames=20)

    assert transcriptor_vivo.chunks_contiguos(previo, continuo) is True
    assert transcriptor_vivo.chunks_contiguos(previo, cambio_stream) is False
    assert transcriptor_vivo.chunks_contiguos(previo, con_padding) is False


def test_retry_de_chunk_anterior_no_deduplica_con_lineas_del_futuro():
    actuales = [{
        "t": 100.0, "ini": 0.0, "fin": 1.0,
        "texto": "modelos profundos generalizan mejor",
    }]
    posteriores = [{
        "t": 130.0, "texto": "modelos profundos generalizan mejor",
    }]

    out = transcriptor_vivo.deduplicar_frontera(
        actuales, posteriores, inicio_actual=100.0)

    assert out == actuales


def test_revalidacion_detecta_sellado_o_wav_cambiado_durante_stt(tmp_path):
    clase = tmp_path / "clase"
    audio = clase / "audio"
    audio.mkdir(parents=True)
    ruta = audio / "chunk_000000000100_p1_v10.wav"
    ruta.write_bytes(b"audio-original")
    hash_original = hashlib.sha256(ruta.read_bytes()).hexdigest()
    row = audio_manifest.append_event(clase, {
        "chunk_id": ruta.stem, "status": "sealed", "sequence": 1,
        "path": ruta.name, "sha256": hash_original,
    })
    snapshot = transcriptor_vivo.snapshot_audio_input(clase, ruta, row)

    ruta.write_bytes(b"audio-cambiado!")
    hash_nuevo = hashlib.sha256(ruta.read_bytes()).hexdigest()
    audio_manifest.append_event(clase, {
        "chunk_id": ruta.stem, "status": "sealed", "sequence": 2,
        "path": ruta.name, "sha256": hash_nuevo,
    })

    with pytest.raises(transcriptor_vivo.AudioCambioDuranteStt):
        transcriptor_vivo.validar_audio_input(clase, ruta, snapshot, row)


def test_revalidacion_acepta_wav_legacy_estable_sin_evento_manifest(tmp_path):
    clase = tmp_path / "clase"
    ruta = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    ruta.parent.mkdir(parents=True)
    ruta.write_bytes(b"audio-legacy-estable")
    row = {"status": "sealed", "inferred_legacy": True, "path": ruta.name}

    snapshot = transcriptor_vivo.snapshot_audio_input(clase, ruta, row)

    assert transcriptor_vivo.validar_audio_input(
        clase, ruta, snapshot, row) == row


def _wav16k(path, muestras):
    import wave

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(muestras.astype("<i2").tobytes())


def _patron(semilla: int, muestras: int = 32000):
    return (((np.arange(muestras) * semilla) % 997) - 498) * 6


def _modelo_grabador():
    class Grabador:
        whole_chunk = True

        def __init__(self):
            self.recibido = []

        def transcribe(self, datos, **_kwargs):
            self.recibido.append(np.array(datos, copy=True))
            segmento = SimpleNamespace(
                start=0.1, end=0.5, text=" hola", avg_logprob=-0.1,
                no_speech_prob=0.0, compression_ratio=1.0)
            info = SimpleNamespace(language="es", language_probability=0.99)
            return [segmento], info

    return Grabador()


def test_tail_solape_cacheado_es_bit_identico_al_recompute(monkeypatch, tmp_path):
    a = tmp_path / "chunk_000000000200_p1_v10.wav"
    b = tmp_path / "chunk_000000000230_p1_v10.wav"
    _wav16k(a, _patron(7))
    _wav16k(b, _patron(11))
    cargas: list[str] = []
    original_cargar = transcriptor_vivo.cargar_wav16k

    def cargar_contado(path):
        cargas.append(str(path))
        return original_cargar(path)

    monkeypatch.setattr(transcriptor_vivo, "cargar_wav16k", cargar_contado)

    # Pasada con cache: el tail del chunk A se reutiliza al procesar B.
    monkeypatch.setattr(transcriptor_vivo, "_ultimo_tail_solape", None)
    modelo_cache = _modelo_grabador()
    transcriptor_vivo.transcribir_chunk(modelo_cache, a, inicio_abs=200.0)
    transcriptor_vivo.transcribir_chunk(
        modelo_cache, b, inicio_abs=230.0, anterior=a)
    cargas_con_cache = len(cargas)

    # Pasada de control sin cache: B recarga y re-deriva el WAV anterior.
    monkeypatch.setattr(transcriptor_vivo, "_ultimo_tail_solape", None)
    monkeypatch.setattr(
        transcriptor_vivo, "_guardar_tail_solape", lambda *_a, **_k: None)
    modelo_control = _modelo_grabador()
    transcriptor_vivo.transcribir_chunk(modelo_control, a, inicio_abs=200.0)
    transcriptor_vivo.transcribir_chunk(
        modelo_control, b, inicio_abs=230.0, anterior=a)

    # 1.5 s de solape + 2 s del chunk actual, bit a bit iguales en ambas rutas.
    assert len(modelo_cache.recibido[1]) == 24000 + 32000
    assert np.array_equal(modelo_cache.recibido[1], modelo_control.recibido[1])
    assert cargas_con_cache == 2  # A y B una vez; el anterior no se recarga
    assert len(cargas) == 5  # el control sí recarga A al procesar B


def test_tail_solape_se_invalida_si_el_wav_anterior_cambia(monkeypatch, tmp_path):
    a = tmp_path / "chunk_000000000200_p1_v10.wav"
    b = tmp_path / "chunk_000000000230_p1_v10.wav"
    _wav16k(a, _patron(7))
    _wav16k(b, _patron(11))
    monkeypatch.setattr(transcriptor_vivo, "_ultimo_tail_solape", None)
    modelo = _modelo_grabador()
    transcriptor_vivo.transcribir_chunk(modelo, a, inicio_abs=200.0)

    # El WAV anterior cambia en disco (otro tamaño): el tail retenido ya no
    # es válido y el solape debe recomputarse desde el archivo real.
    _wav16k(a, _patron(13, muestras=33600))
    cargas: list[str] = []
    original_cargar = transcriptor_vivo.cargar_wav16k

    def cargar_contado(path):
        cargas.append(str(path))
        return original_cargar(path)

    monkeypatch.setattr(transcriptor_vivo, "cargar_wav16k", cargar_contado)
    transcriptor_vivo.transcribir_chunk(modelo, b, inicio_abs=230.0, anterior=a)

    assert any(ruta.endswith(a.name) for ruta in cargas)
    assert len(modelo.recibido[1]) == 24000 + 32000
