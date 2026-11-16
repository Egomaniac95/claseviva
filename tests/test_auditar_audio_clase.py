import struct
import wave

import pytest

from scripts import auditar_audio_clase


@pytest.mark.parametrize("pcm,channels,silencio", [
    (b"\x00\x00" * 16000, 1, True),
    (b"\x01\x00" * 16000, 1, False),
    (struct.pack("<hh", 1, -1) * 16000, 2, False),
    (b"\x00\x00\x01\x00" + b"\x00\x00" * 15998, 1, False),
])
def test_auditoria_solo_considera_silencio_el_cero_digital(
        tmp_path, pcm, channels, silencio):
    audio = tmp_path / "audio"
    audio.mkdir()
    with wave.open(str(audio / "chunk_000000000100_p1_v10.wav"), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)

    report = auditar_audio_clase.auditar(tmp_path)

    assert report["wav_silencio_digital"] == int(silencio)
    assert report["wav_casi_mudos"] == int(silencio)
    assert report["wav_utiles"] == int(not silencio)
    assert report["filas"][0]["silencio_digital"] is silencio
    assert report["filas"][0]["useful_audio"] is not silencio


def test_veredicto_enumera_solo_hallazgos_reales():
    result = {
        "clase": "synthetic",
        "wav_total": 2,
        "wav_utiles": 2,
        "wav_casi_mudos": 0,
        "wav_pendientes": 0,
        "transcript_segmentos": 4,
        "chunks_transcritos": 2,
        "commits_vacios": 0,
        "commits_invalidos": 0,
        "continuidad": {
            "status": "warning", "source": "manifest", "deficit_seconds": 0,
            "wall_gap_total_seconds": 4.3, "wall_gap_p95_seconds": 4.3,
            "wall_gap_max_seconds": 4.3, "data_discontinuity_packets": 0,
            "terminal_discontinuities": 0, "terminal_capture_failures": 0,
            "recovered_capture_failures": 0, "unfinished_capture_count": 0,
            "terminal_unfinished_capture_count": 0,
            "active_unfinished_capture_count": 0,
            "terminal_capture_failure_details": [],
            "unfinished_capture_details": [],
        },
        "filas": [],
    }

    report = auditar_audio_clase.reporte_md(result)

    assert "gap de reloj máximo: 4.300 s" in report
    assert "hay WAV casi mudos" not in report
    assert "fallos terminales" not in report
    assert "capturas inconclusas" not in report
