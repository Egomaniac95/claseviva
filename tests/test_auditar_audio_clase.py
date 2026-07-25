from scripts import auditar_audio_clase


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
