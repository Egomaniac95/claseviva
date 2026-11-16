import hashlib
import json
import os
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts import auditar_audio_clase
from src import audio_manifest, post_procesar, transcriptor_vivo


def _wav(path: Path, frames: int, rate: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * frames)


def test_manifest_append_only_tolera_ultima_linea_rota(tmp_path):
    clase = tmp_path / "clase"
    row = audio_manifest.append_event(clase, {
        "chunk_id": "chunk_1_p1_v10",
        "status": "sealed",
        "sequence": 1,
    })
    with open(audio_manifest.manifest_path(clase), "a", encoding="utf-8") as handle:
        handle.write('{"chunk_id":"incompleto"')

    assert row["schema_version"] == 1
    assert audio_manifest.read_events(clase) == [row]


def test_manifest_anexa_evento_valido_despues_de_ultima_linea_rota(tmp_path):
    clase = tmp_path / "clase"
    primero = audio_manifest.append_event(clase, {
        "chunk_id": "chunk_1_p1_v10", "status": "capturing", "sequence": 1})
    with open(audio_manifest.manifest_path(clase), "ab") as handle:
        handle.write(b'{"chunk_id":"incompleto"')
    segundo = audio_manifest.append_event(clase, {
        "chunk_id": "chunk_2_p1_v10", "status": "sealed", "sequence": 2})

    assert audio_manifest.read_events(clase) == [primero, segundo]


def test_manifest_append_es_seguro_entre_captura_y_persistidor(tmp_path):
    clase = tmp_path / "clase"

    def append(index):
        audio_manifest.append_event(clase, {
            "chunk_id": f"chunk_{index}", "status": "sealed", "sequence": index})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(40)))

    rows = audio_manifest.read_events(clase)
    assert len(rows) == 40
    assert {row["chunk_id"] for row in rows} == {f"chunk_{i}" for i in range(40)}


def test_inferencia_legacy_es_read_only_y_reporta_hueco(tmp_path):
    clase = tmp_path / "clase"
    _wav(clase / "audio" / "chunk_000000000100_p1_v10.wav", 900)
    _wav(clase / "audio" / "chunk_000000000110_p1_v10.wav", 900)

    rows = audio_manifest.infer_legacy(clase)
    reporte = audio_manifest.continuity_report(
        clase, expected_chunk_seconds=10.0)

    assert len(rows) == 2
    assert rows[0]["duration"] == 9.0
    assert rows[0]["inferred_legacy"] is True
    assert not audio_manifest.manifest_path(clase).exists()
    assert reporte["status"] == "warning"
    assert reporte["deficit_frames"] == 100
    assert reporte["wall_gap_total_seconds"] == 1.0
    assert reporte["terminal_capture_failures"] == 0
    assert reporte["recovered_capture_failures"] == 0
    assert reporte["unfinished_capture_count"] == 0


def test_capture_failed_terminal_es_visible_aunque_no_sea_capture_record(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_ok", "status": "sealed", "sequence": 1,
        "path": "chunk_ok.wav", "rate": 100, "frame_count": 1000,
        "expected_frames": 1000, "wall_start": 0.0, "wall_end": 10.0,
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_roto", "status": "capture_failed", "sequence": 2,
        "path": "chunk_roto.wav", "wall_start": 10.0, "wall_end": 10.2,
        "reason": "watchdog_pcm_flat", "error_type": "CaptureTimeout",
    })

    reporte = audio_manifest.continuity_report(
        clase, expected_chunk_seconds=10.0)

    assert [row["chunk_id"] for row in audio_manifest.capture_records(clase)] == [
        "chunk_ok"
    ]
    assert reporte["status"] == "warning"
    assert reporte["chunks"] == 1
    assert reporte["capture_failure_events"] == 1
    assert reporte["terminal_capture_failures"] == 1
    assert reporte["recovered_capture_failures"] == 0
    assert len(reporte["terminal_capture_failure_details"]) == 1
    detalle = reporte["terminal_capture_failure_details"][0]
    assert detalle["chunk_id"] == "chunk_roto"
    assert detalle["sequence"] == 2
    assert detalle["reason"] == "watchdog_pcm_flat"
    assert detalle["error_type"] == "CaptureTimeout"
    assert detalle["failure_events"] == 1


def test_capture_failed_recuperado_no_duplica_ni_fuerza_warning(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_ok", "status": "sealed", "sequence": 1,
        "path": "chunk_ok.wav", "rate": 100, "frame_count": 1000,
        "expected_frames": 1000, "wall_start": 0.0, "wall_end": 10.0,
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_recuperado", "status": "capture_failed", "sequence": 2,
        "path": "chunk_recuperado.wav", "wall_start": 10.0,
        "reason": "wav_persistence_failed", "error_type": "OSError",
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_recuperado", "status": "recovered", "sequence": 3,
        "path": "chunk_recuperado.wav", "rate": 100, "frame_count": 1000,
        "expected_frames": 1000, "wall_start": 10.0, "wall_end": 20.0,
    })

    reporte = audio_manifest.continuity_report(
        clase, expected_chunk_seconds=10.0)

    assert [row["chunk_id"] for row in audio_manifest.capture_records(clase)] == [
        "chunk_ok", "chunk_recuperado",
    ]
    assert reporte["status"] == "ok"
    assert reporte["capture_failure_events"] == 1
    assert reporte["terminal_capture_failures"] == 0
    assert reporte["recovered_capture_failures"] == 1
    assert reporte["terminal_capture_failure_details"] == []
    assert reporte["recovered_capture_failure_details"][0]["chunk_id"] == (
        "chunk_recuperado")
    assert reporte["recovered_capture_failure_details"][0]["recovery_status"] == (
        "recovered")
    auditoria = auditar_audio_clase.auditar(clase)
    markdown = auditar_audio_clase.reporte_md(auditoria)
    assert auditoria["capture_failures_terminales"] == 0
    assert auditoria["capture_failures_recuperados"] == 1
    assert "Fallos de captura: terminales=0; recuperados=1" in markdown
    assert "## Veredicto" not in markdown


def test_barrera_permite_cierre_degradado_y_conserva_advertencia(tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    _wav(wav, 100)
    sha256 = hashlib.sha256(wav.read_bytes()).hexdigest()
    audio_manifest.append_capture_session_event(
        clase, "capture_session_started", capture_session_id="sesion-degradada")
    audio_manifest.append_event(clase, {
        "chunk_id": wav.stem,
        "status": "sealed",
        "sequence": 1,
        "path": wav.name,
        "sha256": sha256,
        "rate": 100,
        "frame_count": 100,
        "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(
        clase, wav.stem, [], {
            "path": wav.name,
            "sequence": 1,
            "input_sha256": sha256,
        })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_000000000101_p1_v10",
        "status": "capture_failed",
        "sequence": 2,
        "path": "chunk_000000000101_p1_v10.wav",
        "reason": "watchdog_pcm_flat",
        "capture_outcome": "route_failed",
        "transactional_stt_required": True,
    })
    audio_manifest.append_capture_session_event(
        clase, "capture_session_closed", capture_session_id="sesion-degradada",
        terminal_capture_failures=1)

    reporte = audio_manifest.completion_barrier_report(clase)

    assert reporte["ready"] is True
    assert reporte["status"] == "ready_with_warnings"
    assert reporte["capture_ready"] is True
    assert reporte["capture_integrity_ok"] is False
    assert reporte["terminal_capture_failure_count"] == 1
    assert reporte["warnings"] == ["terminal_capture_failures=1"]
    assert reporte["blocking_reasons"] == []


def test_auditor_muestra_capture_failed_terminal_en_json_y_markdown(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_ok", "status": "sealed", "sequence": 1,
        "path": "chunk_ok.wav", "rate": 100, "frame_count": 1000,
        "expected_frames": 1000, "wall_start": 0.0, "wall_end": 10.0,
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_roto", "status": "capture_failed", "sequence": 2,
        "path": "chunk_roto.wav", "reason": "watchdog_pcm_flat",
        "error_type": "CaptureTimeout",
    })

    resultado = auditar_audio_clase.auditar(clase)
    markdown = auditar_audio_clase.reporte_md(resultado)

    assert resultado["capture_failures_terminales"] == 1
    assert resultado["capture_failures_recuperados"] == 0
    assert "Fallos de captura: terminales=1; recuperados=0" in markdown
    assert "## Fallos terminales de captura" in markdown
    assert "chunk_roto" in markdown
    assert "watchdog_pcm_flat" in markdown


def test_capturing_inconcluso_alerta_en_auditoria_pero_no_durante_sesion_activa(
        tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_ok", "status": "sealed", "sequence": 1,
        "path": "chunk_ok.wav", "rate": 100, "frame_count": 1000,
        "expected_frames": 1000, "wall_start": 0.0, "wall_end": 10.0,
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_inconcluso", "status": "capturing", "sequence": 2,
        "path": "chunk_inconcluso.wav", "wall_start": 10.0,
        "capture_session_id": "session-viva",
    })

    final = audio_manifest.continuity_report(clase)
    en_vivo = audio_manifest.continuity_report(
        clase, capture_session_active=True)

    assert final["status"] == "warning"
    assert final["unfinished_capture_count"] == 1
    assert final["terminal_unfinished_capture_count"] == 1
    assert final["active_unfinished_capture_count"] == 0
    assert final["unfinished_capture_details"][0]["chunk_id"] == "chunk_inconcluso"
    assert final["unfinished_capture_details"][0]["terminal"] is True
    assert en_vivo["status"] == "ok"
    assert en_vivo["unfinished_capture_count"] == 1
    assert en_vivo["terminal_unfinished_capture_count"] == 0
    assert en_vivo["active_unfinished_capture_count"] == 1
    assert en_vivo["unfinished_capture_details"][0]["active_session"] is True

    auditoria = auditar_audio_clase.auditar(clase)
    markdown = auditar_audio_clase.reporte_md(auditoria)
    assert auditoria["capturas_inconclusas"] == 1
    assert "Capturas inconclusas: total=1; terminales=1; activas=0" in markdown
    assert "## Capturas inconclusas" in markdown
    assert "chunk_inconcluso" in markdown
    assert "## Veredicto" in markdown


def test_sesion_activa_no_oculta_capturing_anterior_abandonado(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_abandonado", "status": "capturing", "sequence": 1,
        "path": "chunk_abandonado.wav", "wall_start": 0.0,
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_actual", "status": "capturing", "sequence": 2,
        "path": "chunk_actual.wav", "wall_start": 10.0,
    })

    reporte = audio_manifest.continuity_report(
        clase, capture_session_active=True)

    assert reporte["status"] == "warning"
    assert reporte["unfinished_capture_count"] == 2
    assert reporte["terminal_unfinished_capture_count"] == 1
    assert reporte["active_unfinished_capture_count"] == 1
    estados = {
        row["chunk_id"]: row["terminal"]
        for row in reporte["unfinished_capture_details"]
    }
    assert estados == {"chunk_abandonado": True, "chunk_actual": False}


def test_padding_por_timeout_cuenta_como_discontinuidad(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_a",
        "status": "sealed",
        "sequence": 1,
        "path": "chunk_a.wav",
        "rate": 100,
        "frame_count": 1000,
        "expected_frames": 1000,
        "wall_start": 10.0,
        "wall_end": 20.0,
        "timeout_padding_frames": 75,
    })

    reporte = audio_manifest.continuity_report(clase)

    assert reporte["status"] == "warning"
    assert reporte["explicit_gap_frames"] == 75
    assert reporte["explicit_gap_seconds"] == 0.75


def test_flag_wasapi_fuerza_warning_aunque_frames_coincidan(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_a", "status": "sealed", "sequence": 1,
        "path": "chunk_a.wav", "rate": 100, "frame_count": 1000,
        "expected_frames": 1000, "wall_start": 10.0, "wall_end": 20.0,
        "data_discontinuity_packets": 1, "terminal_discontinuity": True,
    })

    reporte = audio_manifest.continuity_report(clase)

    assert reporte["status"] == "warning"
    assert reporte["data_discontinuity_packets"] == 1
    assert reporte["terminal_discontinuities"] == 1


def test_percentil_95_de_gaps_usa_fronteras_positivas_ordenadas(tmp_path):
    clase = tmp_path / "clase"
    starts = [0.0, 10.01, 20.03, 30.23]
    for index, start in enumerate(starts, 1):
        audio_manifest.append_event(clase, {
            "chunk_id": f"chunk_{index}", "status": "sealed", "sequence": index,
            "path": f"chunk_{index}.wav", "rate": 100, "frame_count": 1000,
            "expected_frames": 1000, "wall_start": start, "wall_end": start + 10.0,
        })

    reporte = audio_manifest.continuity_report(
        clase, expected_chunk_seconds=10.0)

    assert reporte["wall_gap_p95_seconds"] == 0.2
    assert reporte["wall_gap_max_seconds"] == 0.2
    assert reporte["status"] == "warning"


def test_gap_entre_streams_cubierto_por_pausa_declarada_no_es_perdida(tmp_path):
    clase = tmp_path / "clase"
    for sequence, start, stream in (
            (1, 100.0, "stream-a"), (2, 230.0, "stream-b")):
        audio_manifest.append_event(clase, {
            "chunk_id": f"chunk_{sequence}", "status": "sealed",
            "sequence": sequence, "path": f"chunk_{sequence}.wav",
            "rate": 100, "frame_count": 1000, "expected_frames": 1000,
            "pcm_wall_start": start, "pcm_wall_end": start + 10.0,
            "stream_id": stream, "start_frame": 0, "end_frame": 1000,
        })

    reporte = audio_manifest.continuity_report(
        clase,
        expected_chunk_seconds=10.0,
        declared_inactive_intervals=[(109.5, 230.5)],
    )

    assert reporte["status"] == "ok"
    assert reporte["wall_gap_total_seconds"] == 0.0
    assert reporte["declared_inactive_gap_seconds"] == 120.0
    assert reporte["boundaries"][0]["declared_inactivity"] is True


def test_gap_fisico_fuera_de_pausa_sigue_bloqueando(tmp_path):
    clase = tmp_path / "clase"
    for sequence, start in ((1, 100.0), (2, 112.0)):
        audio_manifest.append_event(clase, {
            "chunk_id": f"chunk_{sequence}", "status": "sealed",
            "sequence": sequence, "path": f"chunk_{sequence}.wav",
            "rate": 100, "frame_count": 1000, "expected_frames": 1000,
            "pcm_wall_start": start, "pcm_wall_end": start + 10.0,
            "stream_id": f"stream-{sequence}",
        })

    reporte = audio_manifest.continuity_report(
        clase,
        expected_chunk_seconds=10.0,
        declared_inactive_intervals=[(105.0, 110.0)],
    )

    assert reporte["status"] == "warning"
    assert reporte["wall_gap_total_seconds"] == 2.0
    assert reporte["declared_inactive_gap_seconds"] == 0.0


@pytest.mark.parametrize("requested,stream,frame,explicit,expected", [
    (230.99, "new", 0, 0, "ok"),
    (None, "new", 0, 0, "warning"),
    (228.0, "new", 0, 0, "warning"),
    (231.4, "new", 0, 0, "warning"),
    (230.0, "new", 0, 0, "warning"),
    (230.99, "old", 13107, 0, "warning"),
    (230.99, "new", 100, 0, "warning"),
    (230.99, "new", 0, 50, "warning"),
])
def test_resume_poll_and_stream_open_require_provenance(
        tmp_path, requested, stream, frame, explicit, expected):
    """A delayed consolidation must not confuse a closed pause with lost PCM."""
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "before", "status": "discarded_silence", "sequence": 1,
        "rate": 100, "frame_count": 1000, "expected_frames": 1000,
        "pcm_wall_start": 100.0, "pcm_wall_end": 110.0,
        "stream_id": "old", "start_frame": 0, "end_frame": 1000,
    })
    if requested is not None:
        audio_manifest.append_event(tmp_path, {
            "chunk_id": "after", "status": "capturing", "sequence": 2,
            "observed_at": requested,
        })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "after", "status": "sealed", "sequence": 2,
        "rate": 100, "frame_count": 1000, "expected_frames": 1000,
        "pcm_wall_start": 231.07, "pcm_wall_end": 241.07,
        "stream_id": stream, "start_frame": frame, "end_frame": frame + 1000,
        "gap_frames": explicit,
    })
    report = audio_manifest.continuity_report(
        tmp_path, declared_inactive_intervals=[(109.0, 230.0)])
    assert report["status"] == expected
    if expected == "ok":
        assert report["wall_gap_total_seconds"] == 0
        assert report["declared_inactive_gap_seconds"] == 121.07
        assert report["boundaries"][0]["resume_startup_seconds"] == 1.07
        assert post_procesar._wav_limpieza_segura_por_chunks({
            "wav_total": 1, "continuidad_audio": report,
        })


def test_resume_without_closed_pause_still_blocks(tmp_path):
    for name, start, stream in (("before", 100.0, "a"), ("after", 231.07, "b")):
        audio_manifest.append_event(tmp_path, {
            "chunk_id": name, "status": "capturing", "observed_at": start - 0.1,
        })
        audio_manifest.append_event(tmp_path, {
            "chunk_id": name, "status": "sealed", "rate": 100,
            "frame_count": 1000, "expected_frames": 1000,
            "pcm_wall_start": start, "pcm_wall_end": start + 10,
            "stream_id": stream, "start_frame": 0, "end_frame": 1000,
        })
    assert audio_manifest.continuity_report(tmp_path)["status"] == "warning"


def test_silencio_descartado_declara_cobertura_sin_convertirse_en_wav_stt(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_1", "status": "sealed", "sequence": 1,
        "path": "chunk_1.wav", "rate": 100, "frame_count": 1000,
        "expected_frames": 1000, "wall_start": 0.0, "wall_end": 10.0,
        "stream_id": "stream", "start_frame": 0, "end_frame": 1000,
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_2", "status": "discarded_silence", "sequence": 2,
        "path": "chunk_2.wav", "rate": 100, "frame_count": 1000,
        "wall_start": 10.0, "wall_end": 20.0,
        "stream_id": "stream", "start_frame": 1000, "end_frame": 2000,
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_3", "status": "sealed", "sequence": 3,
        "path": "chunk_3.wav", "rate": 100, "frame_count": 1000,
        "expected_frames": 1000, "wall_start": 20.0, "wall_end": 30.0,
        "stream_id": "stream", "start_frame": 2000, "end_frame": 3000,
    })

    report = audio_manifest.continuity_report(clase, expected_chunk_seconds=10)

    assert report["status"] == "ok"
    assert report["declared_silence_seconds"] == 10.0
    assert report["wall_gap_total_seconds"] == 0.0
    assert [row["chunk_id"] for row in audio_manifest.capture_records(clase)] == [
        "chunk_1", "chunk_3"]


def test_orphan_report_no_mueve_ni_borra_archivos(tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    _wav(wav, 100)
    part = clase / "audio" / "chunk_000000000101_p1_v10.part"
    part.write_bytes(b"partial")

    reporte = audio_manifest.orphan_report(clase)

    assert reporte["wav_without_manifest"] == [wav.name]
    assert reporte["part_files"] == [part.name]
    assert wav.exists() and part.exists()


def test_commit_vacio_es_completo_y_materializa_vista_vacia(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_100_p1_v10",
        "status": "sealed",
        "sequence": 1,
    })

    audio_manifest.commit_transcript_chunk(
        clase, "chunk_100_p1_v10", [], {"sequence": 1})

    assert audio_manifest.completed_transcript_chunks(clase) == {
        "chunk_100_p1_v10"
    }
    assert (clase / "transcript.jsonl").read_text(encoding="utf-8") == ""
    assert not list((clase / "transcript_chunks").glob("*.part"))


def test_estado_stt_comun_distingue_vacio_ausente_parcial_corrupto_y_hash(tmp_path):
    clase = tmp_path / "clase"
    ids = {
        "vacio": "chunk_vacio",
        "ausente": "chunk_ausente",
        "parcial": "chunk_parcial",
        "corrupto": "chunk_corrupto",
        "hash": "chunk_hash",
    }
    hash_original = ""
    for sequence, chunk_id in enumerate(ids.values(), 1):
        wav = clase / "audio" / f"{chunk_id}.wav"
        _wav(wav, 100)
        evento = {
            "chunk_id": chunk_id,
            "status": "sealed",
            "sequence": sequence,
            "path": f"{chunk_id}.wav",
            "transactional_stt_required": True,
        }
        if chunk_id == ids["hash"]:
            hash_original = hashlib.sha256(wav.read_bytes()).hexdigest()
            evento["sha256"] = hash_original
        audio_manifest.append_event(clase, evento)
    audio_manifest.commit_transcript_chunk(
        clase, ids["vacio"], [], {"sequence": 1}, materialize=False)
    audio_manifest.commit_transcript_chunk(
        clase, ids["hash"], [], {"input_sha256": hash_original},
        materialize=False)
    audio_manifest.append_event(clase, {
        "chunk_id": ids["hash"], "status": "sealed", "sequence": 6,
        "path": f"{ids['hash']}.wav", "sha256": "hash-reemplazo",
        "transactional_stt_required": True,
    })
    journal = clase / "transcript_chunks"
    (journal / f"{ids['parcial']}.json").write_text(
        json.dumps({"chunk_id": ids["parcial"], "status": "partial"}),
        encoding="utf-8",
    )
    (journal / f"{ids['corrupto']}.json").write_text(
        '{"status":"complete"', encoding="utf-8")

    reporte = audio_manifest.transcript_status_report(clase, verify_files=False)

    assert reporte["completed_chunks"] == [ids["vacio"]]
    assert reporte["empty_completed_chunks"] == [ids["vacio"]]
    assert reporte["pending_chunks"] == [ids["ausente"]]
    assert reporte["invalid_chunks"] == sorted([
        ids["corrupto"], ids["hash"], ids["parcial"]])
    assert reporte["unresolved_chunks"] == sorted([
        ids["ausente"], ids["corrupto"], ids["hash"], ids["parcial"]])


def test_wav_sellado_inexistente_es_integridad_faltante_no_trabajo_stt(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_faltante",
        "status": "sealed",
        "sequence": 1,
        "path": "chunk_faltante.wav",
        "transactional_stt_required": True,
    })

    reporte = audio_manifest.transcript_status_report(clase, verify_files=True)

    assert reporte["missing_audio_chunks"] == ["chunk_faltante"]
    assert reporte["pending_chunks"] == []
    assert reporte["actionable_chunks"] == []
    assert reporte["unresolved_chunks"] == ["chunk_faltante"]


def test_wav_residual_de_capture_failed_no_revive_como_pendiente(tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_fallido.wav"
    _wav(wav, 100)
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_fallido", "status": "capturing", "sequence": 1,
        "path": wav.name, "transactional_stt_required": True,
    })
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_fallido", "status": "capture_failed", "sequence": 1,
        "path": wav.name, "reason": "route_failed",
        "transactional_stt_required": True,
    })

    reporte = audio_manifest.transcript_status_report(clase, verify_files=True)

    assert reporte["captured_chunks"] == []
    assert reporte["pending_chunks"] == []
    assert reporte["actionable_chunks"] == []


def test_estado_stt_rechaza_commits_estructuralmente_invalidos_sin_caer(tmp_path):
    clase = tmp_path / "clase"
    casos = {
        "metadata_lista": {
            "schema_version": 1, "status": "complete", "lines": [], "metadata": [],
        },
        "lines_cadena": {
            "schema_version": 1, "status": "complete", "lines": "silencio",
            "metadata": {},
        },
        "linea_no_objeto": {
            "schema_version": 1, "status": "complete", "lines": ["segmento"],
            "metadata": {},
        },
        "schema_desconocido": {
            "schema_version": 99, "status": "complete", "lines": [], "metadata": {},
        },
        "lines_ausente": {
            "schema_version": 1, "status": "complete", "metadata": {},
        },
    }
    journal = clase / "transcript_chunks"
    journal.mkdir(parents=True)
    for sequence, (chunk_id, payload) in enumerate(casos.items(), 1):
        audio_manifest.append_event(clase, {
            "chunk_id": chunk_id, "status": "sealed", "sequence": sequence,
            "path": f"{chunk_id}.wav", "transactional_stt_required": True,
        })
        (journal / f"{chunk_id}.json").write_text(json.dumps({
            "chunk_id": chunk_id, **payload,
        }), encoding="utf-8")

    reporte = audio_manifest.transcript_status_report(clase, verify_files=False)

    assert reporte["completed_chunks"] == []
    assert reporte["invalid_chunks"] == sorted(casos)
    assert set(reporte["invalid_reasons"]) == set(casos)


def test_commit_invalido_no_resucita_como_transcript_legacy(tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_legacy.wav"
    _wav(wav, 20)
    (clase / "transcript.jsonl").write_text(json.dumps({
        "t": 10.0, "chunk": wav.name, "texto": "línea previa no canónica",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    journal = clase / "transcript_chunks"
    journal.mkdir()
    (journal / f"{wav.stem}.json").write_text(json.dumps({
        "schema_version": 1, "chunk_id": wav.stem, "status": "complete",
        "lines": [], "metadata": [],
    }), encoding="utf-8")

    reporte = audio_manifest.transcript_status_report(clase, verify_files=False)

    assert reporte["completed_chunks"] == []
    assert reporte["invalid_chunks"] == [wav.stem]
    assert reporte["source"] == "transcript_chunks"


def test_barrera_rechaza_wav_reemplazado_aunque_manifest_no_cambie(tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_fisico.wav"
    _wav(wav, 100)
    original_hash = hashlib.sha256(wav.read_bytes()).hexdigest()
    audio_manifest.append_event(clase, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "sha256": original_hash,
        "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(
        clase, wav.stem, [], {"path": wav.name, "input_sha256": original_hash})
    audio_manifest.append_capture_session_event(
        clase, "capture_session_closed", capture_session_id="sesion-hash")
    assert audio_manifest.completion_barrier_report(clase)["ready"] is True

    raw = bytearray(wav.read_bytes())
    raw[-2:] = b"\x7f\x7f"
    wav.write_bytes(raw)

    reporte = audio_manifest.completion_barrier_report(clase)
    assert reporte["ready"] is False
    assert reporte["invalid_stt_chunks"] == [wav.stem]


def test_barrera_y_cobertura_usan_commit_vacio_como_terminado(tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    _wav(wav, 100)
    audio_manifest.append_event(clase, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(
        clase, wav.stem, [], {"sequence": 1, "path": wav.name})
    audio_manifest.append_capture_session_event(
        clase, "capture_session_closed", capture_session_id="sesion-test")

    barrera = audio_manifest.completion_barrier_report(clase)
    temporal = post_procesar.analizar_cobertura_temporal(
        clase, [], {"origen": "vivo", "inicio": 100.0})
    cobertura = post_procesar.analizar_cobertura_audio(
        clase, [], [], {"origen": "vivo", "inicio": 100.0})
    auditoria = auditar_audio_clase.auditar(clase)

    assert barrera["ready"] is True
    assert barrera["pending_stt_count"] == 0
    assert barrera["empty_completed_stt_chunk_count"] == 1
    assert temporal["wav_sin_transcript_total"] == 0
    assert temporal["commits_vacios_total"] == 1
    assert temporal["transcript_llega_ultimo_wav"] is True
    assert cobertura["wav_sin_transcript_total"] == 0
    assert cobertura["commits_vacios_total"] == 1
    assert auditoria["wav_pendientes"] == 0
    assert auditoria["commits_vacios"] == 1


def test_paquete_ligero_admite_solo_wav_vacios_con_commit_valido():
    cobertura = {
        "wav_total": 2,
        "wav_sin_transcript_total": 0,
        "transcript_wav_faltante_total": 0,
        "continuidad_audio": {"status": "ok"},
        "wav_util_sin_transcript": [
            {"archivo": "completado_sin_voz.wav"},
            {"archivo": "sin_commit.wav"},
        ],
        "commits_vacios": ["completado_sin_voz.wav"],
    }
    assert not post_procesar._wav_limpieza_segura_por_chunks(cobertura)
    cobertura["wav_util_sin_transcript"].pop()
    assert post_procesar._wav_limpieza_segura_por_chunks(cobertura)


def test_paquete_ligero_bloquea_pendientes_e_invalidos_con_advertencia_pcm():
    cobertura = {
        "wav_total": 1,
        "wav_sin_transcript_total": 1,
        "transcript_wav_faltante_total": 0,
        "continuidad_audio": {"status": "ok"},
        "wav_util_sin_transcript": [{"archivo": "chunk.wav"}],
        "commits_vacios": ["chunk.wav"],
    }
    assert not post_procesar._wav_limpieza_segura_por_chunks(cobertura)
    cobertura.update({"wav_sin_transcript_total": 0,
                      "stt_invalidos_total": 1,
                      "continuidad_audio": {"status": "warning"}})
    assert not post_procesar._wav_limpieza_segura_por_chunks(cobertura)
    cobertura["stt_invalidos_total"] = 0
    assert post_procesar._wav_limpieza_segura_por_chunks(cobertura)
    cobertura["transcript_wav_faltante_total"] = 1
    assert not post_procesar._wav_limpieza_segura_por_chunks(cobertura)


def test_paquete_ligero_admite_audio_procesado_con_cuatro_fallos_historicos():
    cobertura = {
        "wav_total": 721,
        "wav_sin_transcript_total": 0,
        "stt_invalidos_total": 0,
        "transcript_wav_faltante_total": 0,
        "limpieza_segura": False,
        "cobertura_estado": "parcial",
        "continuidad_audio": {
            "status": "warning", "terminal_capture_failures": 4,
            "wall_gap_max_seconds": 51.939,
        },
    }
    assert post_procesar._wav_limpieza_segura_por_chunks(cobertura)
    warning = post_procesar._cobertura_estudiante_md(cobertura)
    assert "4 interrupciones" in warning
    assert "51.9 segundos" in warning


def test_cobertura_reusa_metricas_manifest_despues_de_barrera_verificada(
        monkeypatch, tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    _wav(wav, 100)
    sha256 = hashlib.sha256(wav.read_bytes()).hexdigest()
    audio_manifest.append_event(clase, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "sha256": sha256, "rms": 321.5, "peak": 5000,
        "duration": 1.0, "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(
        clase, wav.stem, [], {"path": wav.name, "input_sha256": sha256})
    audio_manifest.append_capture_session_event(
        clase, "capture_session_closed", capture_session_id="sesion-metricas")
    assert audio_manifest.completion_barrier_report(clase)["ready"] is True
    monkeypatch.setattr(
        post_procesar, "_wav_metricas",
        lambda _path: (_ for _ in ()).throw(AssertionError("lectura PCM redundante")),
    )

    cobertura = post_procesar.analizar_cobertura_audio(
        clase, [], [], {"origen": "vivo", "inicio": 100.0},
        manifest_verificado=True,
    )

    assert cobertura["wav_con_rms_util"] == 1
    assert cobertura["metricas_wav_desde_manifest"] == 1
    assert cobertura["metricas_wav_recalculadas"] == 0


def test_cobertura_verifica_silencio_legacy_con_wav_real(tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    _wav(wav, 100)
    sha256 = hashlib.sha256(wav.read_bytes()).hexdigest()
    audio_manifest.append_event(clase, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "sha256": sha256, "rms": 0, "peak": 0,
        "duration": 1.0, "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(
        clase, wav.stem, [], {"path": wav.name, "input_sha256": sha256})
    coverage = post_procesar.analizar_cobertura_audio(
        clase, [], [], {"origen": "vivo", "inicio": 100.0}, manifest_verificado=True)
    assert coverage["wav_con_rms_util"] == 1
    assert coverage["metricas_wav_desde_manifest"] == 0
    assert coverage["metricas_wav_recalculadas"] == 1


def test_parcial_legacy_no_cuenta_como_commit_con_manifest(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_100_p1_v10",
        "status": "sealed",
        "sequence": 1,
        "transactional_stt_required": True,
    })
    (clase / "transcript.jsonl").write_text(
        json.dumps({"chunk": "chunk_100_p1_v10.wav", "texto": "parcial"}) + "\n",
        encoding="utf-8",
    )

    assert audio_manifest.completed_transcript_chunks(clase) == set()
    assert audio_manifest.materialize_transcript(clase) == 0
    assert (clase / "transcript.jsonl").read_text(encoding="utf-8") == ""


def test_temporal_unico_huerfano_bloquea_rescate_legacy_y_es_auditable(tmp_path):
    clase = tmp_path / "clase"
    wav = clase / "audio" / "chunk_100_p1_v10.wav"
    _wav(wav, 100)
    audio_manifest.append_event(clase, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "transactional_stt_required": True,
    })
    (clase / "transcript.jsonl").write_text(
        json.dumps({"chunk": wav.name, "texto": "línea sin commit"}) + "\n",
        encoding="utf-8",
    )
    journal = clase / "transcript_chunks"
    journal.mkdir()
    (journal / f"{wav.stem}.json.part.123.456.789").write_text(
        "parcial", encoding="utf-8")

    reporte = audio_manifest.transcript_status_report(
        clase, verify_files=False)

    assert audio_manifest.completed_transcript_chunks(clase) == set()
    assert reporte["invalid_chunks"] == [wav.stem]
    assert reporte["invalid_reasons"][wav.stem] == "commit_partial_file"


def test_primer_chunk_nuevo_no_borra_transcript_legacy(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    legacy = {
        "chunk": "chunk_000000000100_p1_v10.wav", "t": 100.0,
        "texto": "evidencia historica",
    }
    parcial_nuevo = {
        "chunk": "chunk_000000000130_p1_v10.wav", "t": 130.0,
        "texto": "no comprometido",
    }
    (clase / "transcript.jsonl").write_text(
        json.dumps(legacy) + "\n" + json.dumps(parcial_nuevo) + "\n",
        encoding="utf-8",
    )
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_000000000130_p1_v10", "status": "sealed",
        "sequence": 1, "path": "chunk_000000000130_p1_v10.wav",
        "transactional_stt_required": True,
    })

    assert audio_manifest.completed_transcript_chunks(clase) == {
        "chunk_000000000100_p1_v10"
    }
    assert audio_manifest.materialize_transcript(clase) == 1
    rows = [json.loads(line) for line in (
        clase / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == [legacy]


def test_evento_inferred_legacy_no_invalida_transcript_historico(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    legacy = {"chunk": "chunk_100_p1_v10.wav", "t": 100.0, "texto": "legacy"}
    (clase / "transcript.jsonl").write_text(
        json.dumps(legacy) + "\n", encoding="utf-8")
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_100_p1_v10", "status": "recovered", "sequence": 1,
        "inferred_legacy": True,
    })

    assert audio_manifest.completed_transcript_chunks(clase) == {"chunk_100_p1_v10"}
    assert audio_manifest.materialize_transcript(clase) == 1


def test_manifest_legacy_sin_hash_ni_flag_no_invalida_transcript(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    legacy = {"chunk": "chunk_100_p1_v10.wav", "t": 100.0, "texto": "legacy"}
    (clase / "transcript.jsonl").write_text(
        json.dumps(legacy) + "\n", encoding="utf-8")
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_100_p1_v10", "status": "sealed", "sequence": 1,
        "path": "chunk_100_p1_v10.wav",
    })

    assert audio_manifest.completed_transcript_chunks(clase) == {"chunk_100_p1_v10"}
    assert audio_manifest.materialize_transcript(clase) == 1


def test_clase_sin_manifest_conserva_chunks_legacy_como_completos(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    (clase / "transcript.jsonl").write_text(
        json.dumps({"chunk": "chunk_100_p1_v10.wav", "texto": "completo legacy"})
        + "\n",
        encoding="utf-8",
    )

    assert audio_manifest.completed_transcript_chunks(clase) == {
        "chunk_100_p1_v10"
    }


def test_commit_nuevo_sin_manifest_preserva_otros_chunks_legacy(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    legacy = {"chunk": "chunk_100_p1_v10.wav", "t": 100.0, "texto": "legacy"}
    (clase / "transcript.jsonl").write_text(
        json.dumps(legacy) + "\n", encoding="utf-8")
    nuevo = {"chunk": "chunk_130_p1_v10.wav", "t": 130.0, "texto": "nuevo"}

    audio_manifest.commit_transcript_chunk(
        clase, "chunk_130_p1_v10", [nuevo], {"sequence": 2})

    rows = [json.loads(line) for line in (
        clase / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == [legacy, nuevo]
    assert audio_manifest.completed_transcript_chunks(clase) == {
        "chunk_100_p1_v10", "chunk_130_p1_v10",
    }


def test_manifest_parcial_no_expone_wav_capturing_ni_capture_failed(tmp_path):
    clase = tmp_path / "clase"
    wav_capturing = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    wav_failed = clase / "audio" / "chunk_000000000130_p1_v10.wav"
    _wav(wav_capturing, 100)
    _wav(wav_failed, 100)
    audio_manifest.append_event(clase, {
        "chunk_id": wav_capturing.stem, "status": "capturing", "sequence": 1})
    audio_manifest.append_event(clase, {
        "chunk_id": wav_failed.stem, "status": "capture_failed", "sequence": 2})

    records = audio_manifest.capture_records(clase)

    assert records == []


def test_stt_remoto_no_expone_wav_huerfano_si_existe_manifest(tmp_path):
    clase = tmp_path / "clase"
    publicado = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    huerfano = clase / "audio" / "chunk_000000000130_p1_v10.wav"
    _wav(publicado, 100)
    _wav(huerfano, 100)
    audio_manifest.append_event(clase, {
        "chunk_id": publicado.stem, "status": "sealed", "sequence": 1,
        "path": publicado.name,
    })

    rows = transcriptor_vivo.registros_sellados_para_stt(clase)

    assert [row["chunk_id"] for row in rows] == [publicado.stem]


def test_capture_failed_no_invalida_transcript_legacy(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    legacy = {"chunk": "chunk_100_p1_v10.wav", "t": 100.0, "texto": "legacy"}
    (clase / "transcript.jsonl").write_text(
        json.dumps(legacy) + "\n", encoding="utf-8")
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_100_p1_v10", "status": "capture_failed",
        "sequence": 1, "reason": "fallo previo",
    })

    assert audio_manifest.completed_transcript_chunks(clase) == {"chunk_100_p1_v10"}
    assert audio_manifest.materialize_transcript(clase) == 1


def test_commits_reconstruyen_sin_duplicar_y_en_orden(tmp_path):
    clase = tmp_path / "clase"
    linea_a = {"chunk": "a.wav", "t": 10.0, "texto": "A"}
    linea_b = {"chunk": "b.wav", "t": 20.0, "texto": "B"}
    audio_manifest.commit_transcript_chunk(
        clase, "b", [linea_b], {"sequence": 2}, materialize=False)
    audio_manifest.commit_transcript_chunk(
        clase, "a", [linea_a], {"sequence": 1}, materialize=False)

    assert audio_manifest.materialize_transcript(clase) == 2
    assert audio_manifest.materialize_transcript(clase) == 2
    rows = [json.loads(x) for x in (
        clase / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == [linea_a, linea_b]


def test_primer_commit_valido_gana_y_respuesta_tardia_no_lo_sobrescribe(tmp_path):
    clase = tmp_path / "clase"
    primero = {"chunk": "a.wav", "t": 1.0, "texto": "ganador"}
    tardio = {"chunk": "a.wav", "t": 1.0, "texto": "duplicado tardio"}

    winner = audio_manifest.commit_transcript_chunk(
        clase, "a", [primero], {"sequence": 1}, materialize=False)
    returned = audio_manifest.commit_transcript_chunk(
        clase, "a", [tardio], {"sequence": 1}, materialize=False)

    assert winner["lines"] == [primero]
    assert returned["lines"] == [primero]
    assert audio_manifest.read_transcript_commit(clase, "a")["lines"] == [primero]
    assert not list((clase / "transcript_chunks").glob("*.part*"))


def test_carrera_de_respuestas_publica_un_solo_ganador(tmp_path):
    clase = tmp_path / "clase"
    gate = threading.Barrier(6)

    def publish(index):
        gate.wait()
        return audio_manifest.commit_transcript_chunk(
            clase, "a", [{"chunk": "a.wav", "t": 1.0,
                           "texto": f"candidate-{index}"}],
            {"sequence": 1}, materialize=False)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(publish, range(6)))

    winner = audio_manifest.read_transcript_commit(clase, "a")
    assert winner is not None
    assert len({result["lines"][0]["texto"] for result in results}) == 1
    assert results[0]["lines"] == winner["lines"]
    assert len(list((clase / "transcript_chunks").glob("a.json"))) == 1


def test_reintentos_fallidos_persisten(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.record_transcription_attempt(
        clase, "chunk_a", "failed", error="cuda")
    audio_manifest.record_transcription_attempt(
        clase, "chunk_a", "failed", error="cuda")
    audio_manifest.record_transcription_attempt(
        clase, "chunk_b", "started")

    assert audio_manifest.failed_attempt_counts(clase) == {"chunk_a": 2}


def test_reintentos_se_reinician_al_completar_y_no_cruzan_hash(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_a", "status": "sealed", "sequence": 1,
        "sha256": "hash-nuevo",
    })
    audio_manifest.record_transcription_attempt(
        clase, "chunk_a", "failed", input_sha256="hash-viejo")
    audio_manifest.record_transcription_attempt(
        clase, "chunk_a", "failed", input_sha256="hash-nuevo")
    audio_manifest.record_transcription_attempt(
        clase, "chunk_a", "complete", input_sha256="hash-nuevo")

    assert audio_manifest.failed_attempt_counts(clase) == {}


def test_dos_fallos_stt_siguen_retryable_despues_del_backoff(tmp_path, monkeypatch):
    clase = tmp_path / "clase"
    monkeypatch.setattr(audio_manifest.time, "time", lambda: 100.0)
    audio_manifest.record_transcription_attempt(clase, "chunk_a", "failed")
    monkeypatch.setattr(audio_manifest.time, "time", lambda: 110.0)
    audio_manifest.record_transcription_attempt(clase, "chunk_a", "failed")

    esperando = audio_manifest.transcription_retry_state(
        clase, now=150.0, base_delay=30.0)
    listo = audio_manifest.transcription_retry_state(
        clase, now=171.0, base_delay=30.0)

    assert esperando["chunk_a"]["failures"] == 2
    assert esperando["chunk_a"]["ready"] is False
    assert listo["chunk_a"]["retryable"] is True
    assert listo["chunk_a"]["ready"] is True


def test_retry_persistido_respeta_retry_after_y_estado_job(tmp_path, monkeypatch):
    clase = tmp_path / "clase"
    monkeypatch.setattr(audio_manifest.time, "time", lambda: 100.0)
    audio_manifest.record_transcription_attempt(
        clase, "chunk_a", "pending_remote_job",
        provider="assemblyai", request_id="job-1", retry_after=175.0,
        retryable=True, input_sha256="")

    waiting = audio_manifest.transcription_retry_state(clase, now=150.0)
    ready = audio_manifest.transcription_retry_state(clase, now=176.0)

    assert waiting["chunk_a"]["ready"] is False
    assert waiting["chunk_a"]["retry_after"] == 175.0
    assert waiting["chunk_a"]["request_id"] == "job-1"
    assert ready["chunk_a"]["ready"] is True


def test_hash_nuevo_invalida_commit_del_mismo_nombre(tmp_path):
    clase = tmp_path / "clase"
    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_a",
        "status": "sealed",
        "sequence": 1,
        "path": "chunk_a.wav",
        "sha256": "hash-original",
    })
    audio_manifest.commit_transcript_chunk(
        clase, "chunk_a", [], {"sequence": 1, "input_sha256": "hash-original"})
    assert audio_manifest.completed_transcript_chunks(clase) == {"chunk_a"}

    audio_manifest.append_event(clase, {
        "chunk_id": "chunk_a",
        "status": "sealed",
        "sequence": 2,
        "path": "chunk_a.wav",
        "sha256": "hash-reemplazo",
    })

    assert audio_manifest.completed_transcript_chunks(clase) == set()
    assert audio_manifest.read_transcript_commit(clase, "chunk_a") is None


def test_wav_reemplazado_sin_evento_invalida_commit(tmp_path):
    clase = tmp_path / "clase"
    wav_path = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    _wav(wav_path, 100)
    original_hash = hashlib.sha256(wav_path.read_bytes()).hexdigest()
    audio_manifest.append_event(clase, {
        "chunk_id": wav_path.stem, "status": "sealed", "sequence": 1,
        "path": wav_path.name, "sha256": original_hash,
    })
    audio_manifest.commit_transcript_chunk(
        clase, wav_path.stem, [], {"input_sha256": original_hash})
    assert audio_manifest.completed_transcript_chunks(clase) == {wav_path.stem}

    _wav(wav_path, 101)

    assert audio_manifest.completed_transcript_chunks(clase) == set()
    assert audio_manifest.read_transcript_commit(clase, wav_path.stem) is None


def test_wav_reemplazado_mismo_tamano_y_mtime_invalida_commit(tmp_path):
    clase = tmp_path / "clase"
    wav_path = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    _wav(wav_path, 100)
    stat_original = wav_path.stat()
    original_hash = hashlib.sha256(wav_path.read_bytes()).hexdigest()
    audio_manifest.append_event(clase, {
        "chunk_id": wav_path.stem, "status": "sealed", "sequence": 1,
        "path": wav_path.name, "sha256": original_hash,
    })
    audio_manifest.commit_transcript_chunk(
        clase, wav_path.stem, [], {
            "path": wav_path.name, "input_sha256": original_hash})

    raw = bytearray(wav_path.read_bytes())
    raw[-2:] = b"\x7f\x7f"
    wav_path.write_bytes(raw)
    os.utime(wav_path, ns=(stat_original.st_atime_ns, stat_original.st_mtime_ns))

    assert audio_manifest.read_transcript_commit(clase, wav_path.stem) is None


def test_postproceso_no_declara_ok_si_wavs_legacy_tienen_gaps(tmp_path):
    clase = tmp_path / "clase"
    _wav(clase / "audio" / "chunk_000000000100_p1_v10.wav", 900)
    _wav(clase / "audio" / "chunk_000000000110_p1_v10.wav", 900)
    segs = [
        {"t": 100.0, "ini": 0.0, "fin": 1.0, "texto": "a",
         "chunk": "chunk_000000000100_p1_v10.wav"},
        {"t": 110.0, "ini": 0.0, "fin": 1.0, "texto": "b",
         "chunk": "chunk_000000000110_p1_v10.wav"},
    ]

    reporte = post_procesar.analizar_cobertura_temporal(
        clase, segs, {"origen": "vivo", "inicio": 100.0})

    assert reporte["estado"] == "advertencia"
    assert reporte["continuidad_audio"]["wall_gap_total_seconds"] == 1.0
    assert any("continuidad PCM" in adv for adv in reporte["advertencias"])


def test_consolidar_reconstruye_vista_desde_commits_antes_de_cualquier_post(
        tmp_path, monkeypatch):
    clase = tmp_path / "clase"
    line = {"chunk": "a.wav", "t": 1.0, "texto": "desde commit"}
    audio_manifest.commit_transcript_chunk(
        clase, "a", [line], {"sequence": 1}, materialize=False)
    (clase / "transcript.jsonl").write_text("corrupto\n", encoding="utf-8")

    class StopAfterBarrier(RuntimeError):
        pass

    def barrier(path, **_kwargs):
        rows = [json.loads(raw) for raw in (
            path / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows == [line]
        raise StopAfterBarrier

    monkeypatch.setattr(post_procesar, "verificar_barrera_final", barrier)
    with pytest.raises(StopAfterBarrier):
        post_procesar.consolidar_todo(clase)


def test_limpieza_conserva_wavs_aunque_todos_tengan_transcript_si_hay_gaps(tmp_path):
    clase = tmp_path / "clase"
    wav_a = clase / "audio" / "chunk_000000000100_p1_v10.wav"
    wav_b = clase / "audio" / "chunk_000000000110_p1_v10.wav"
    _wav(wav_a, 900)
    _wav(wav_b, 900)
    segs = [
        {"t": 100.0, "ini": 0.0, "fin": 8.0, "texto": "audio a",
         "chunk": wav_a.name},
        {"t": 110.0, "ini": 0.0, "fin": 8.0, "texto": "audio b",
         "chunk": wav_b.name},
    ]
    (clase / "transcript.jsonl").write_text(
        "".join(json.dumps(seg) + "\n" for seg in segs), encoding="utf-8")
    (clase / "leccion_final.md").write_text("# Leccion\n", encoding="utf-8")
    cobertura = post_procesar.analizar_cobertura_audio(
        clase, segs, [], {"origen": "vivo", "inicio": 100.0})

    post_procesar.limpiar(clase, cobertura=cobertura)

    assert cobertura["continuidad_audio"]["status"] == "warning"
    assert wav_a.exists() and wav_b.exists()
