import ast
import inspect
import json
import time
from pathlib import Path

from PIL import Image

from src import incremental_jsonl
from src import (
    audio_manifest,
    captura_audio,
    escriba,
    panel,
    post_procesar,
    transcriptor_vivo,
)
from src.incremental_jsonl import AppendOnlyJsonlReader


def _append_json(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _sealed(class_dir: Path, number: int, *, sha: str | None = None) -> tuple[str, Path]:
    chunk_id = f"chunk_{1_700_000_000 + number}_p1"
    wav = class_dir / "audio" / f"{chunk_id}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF-test")
    audio_manifest.append_event(class_dir, {
        "chunk_id": chunk_id,
        "sequence": number,
        "status": "sealed",
        "path": wav.name,
        "wall_start": float(number * 30),
        "wall_end": float(number * 30 + 30),
        "sha256": sha or f"sha-{number}",
    })
    return chunk_id, wav


def test_jsonl_incremental_lee_sufijo_y_recupera_truncado_o_reemplazo(tmp_path):
    path = tmp_path / "queue.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":')
    reader = AppendOnlyJsonlReader(path)

    assert reader.refresh() == [{"a": 1}]
    first = reader.metrics()
    assert first["full_rebuilds"] == 1
    assert first["pending_bytes"] > 0

    assert reader.refresh() == [{"a": 1}]
    assert reader.metrics()["bytes_read"] == first["bytes_read"]

    with path.open("ab") as handle:
        handle.write(b'2}\n')
    assert reader.refresh() == [{"a": 1}, {"b": 2}]
    appended = reader.metrics()
    assert appended["full_rebuilds"] == 1
    assert appended["incremental_reads"] == 1
    assert appended["bytes_read"] == path.stat().st_size

    path.write_text('{"c":3}\n', encoding="utf-8")
    assert reader.refresh() == [{"c": 3}]
    assert reader.metrics()["full_rebuilds"] == 2

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text('{"d":4}\n', encoding="utf-8")
    replacement.replace(path)
    assert reader.refresh() == [{"d": 4}]
    assert reader.metrics()["full_rebuilds"] == 3


def test_indice_temporal_filtra_ventana_y_solo_agrega_append(tmp_path):
    path = tmp_path / "timeline.jsonl"
    for number in range(1000):
        _append_json(path, {"t": float(number), "value": number})
    incremental_jsonl.clear_readers()
    index = incremental_jsonl.time_index_for(path)

    first = index.between(490.0, 500.0)
    initial = index.metrics()
    assert [row["value"] for row in first] == list(range(491, 501))
    assert initial["rebuilds"] == 1
    assert initial["appended_rows"] == 0

    assert index.between(490.0, 500.0) == first
    assert index.metrics()["reader"]["bytes_read"] == initial["reader"]["bytes_read"]

    _append_json(path, {"t": 1000.0, "value": 1000})
    _append_json(path, {"t": 1001.0, "value": 1001})
    assert [row["value"] for row in index.between(999.0, 1001.0)] == [1000, 1001]
    final = index.metrics()
    assert final["rebuilds"] == 1
    assert final["appended_rows"] == 2


def test_indice_transcript_usa_solo_commits_nuevos_tras_arranque(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    journal = tmp_path / audio_manifest.JOURNAL_DIR
    journal.mkdir()
    initial_rows = [
        {"t": float(number), "chunk": f"legacy_{number}.wav", "texto": str(number)}
        for number in range(500)
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in initial_rows), encoding="utf-8")
    escriba.clear_evidence_indexes()
    incremental_jsonl.clear_readers()
    index = escriba._transcript_index(tmp_path)

    assert len(index.between(489.0, 499.0)) == 10
    initial = index.metrics()
    assert initial["rebuilds"] == 1
    assert initial["commit_files_read"] == 0

    new_rows = [
        {"t": 500.0, "chunk": "chunk_new.wav", "texto": "nuevo a"},
        {"t": 501.0, "chunk": "chunk_new.wav", "texto": "nuevo b"},
    ]
    (journal / "chunk_new.json").write_text(json.dumps({
        "status": "complete", "chunk_id": "chunk_new", "lines": new_rows,
        "metadata": {},
    }), encoding="utf-8")
    replacement = tmp_path / "transcript.next"
    replacement.write_text(
        "".join(json.dumps(row) + "\n" for row in [*initial_rows, *new_rows]),
        encoding="utf-8",
    )
    replacement.replace(transcript)

    assert [row["texto"] for row in index.between(499.0, 501.0)] == [
        "nuevo a", "nuevo b"]
    appended = index.metrics()
    assert appended["rebuilds"] == 1
    assert appended["commit_files_read"] == 1
    assert appended["rows_appended"] == 2

    index.between(499.0, 501.0)
    assert index.metrics()["commit_files_read"] == 1


def test_timeline_incremental_conserva_slide_previa_sin_recorrer_historial(tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    for name, color in (("prior.jpg", "navy"), ("current.jpg", "orange")):
        Image.new("RGB", (96, 64), color).save(image_dir / name)
    _append_json(tmp_path / "img_log.jsonl", {
        "t": 1000.0, "archivo": "prior.jpg", "parte": 1,
        "evento": "captura", "ocr": "Concepto previo",
    })
    for timestamp in range(1100, 1300, 2):
        _append_json(tmp_path / "visual_timeline.jsonl", {
            "t": float(timestamp), "archivo": "prior.jpg",
            "ref_archivo": "prior.jpg", "parte": 1, "evento": "heartbeat",
        })
    _append_json(tmp_path / "img_log.jsonl", {
        "t": 1350.0, "archivo": "current.jpg", "parte": 1,
        "evento": "captura", "ocr": "Concepto nuevo",
    })
    _append_json(tmp_path / "visual_timeline.jsonl", {
        "t": 1370.0, "archivo": "current.jpg", "ref_archivo": "current.jpg",
        "parte": 1, "evento": "heartbeat",
    })
    incremental_jsonl.clear_readers()
    origin = {"origen": "vivo", "inicio": 1000.0}
    segments = [{"t": 1360.0, "texto": "Explicación del concepto nuevo."}]

    complete = post_procesar.construir_timeline_slides(
        tmp_path, 1300.0, 1400.0, segments, origin)
    bounded_rows = escriba._filas_visuales_ventana(
        tmp_path, 1300.0, 1400.0, include_start=False)
    bounded = post_procesar.construir_timeline_slides(
        tmp_path, 1300.0, 1400.0, segments, origin,
        filas_visuales=bounded_rows)

    assert bounded_rows is not None
    assert len(bounded_rows) == 35  # captura previa + 32 eventos + 2 actuales
    assert [slide["archivo"] for slide in bounded] == [
        slide["archivo"] for slide in complete]
    assert [slide["t_inicio_rel"] for slide in bounded] == [
        slide["t_inicio_rel"] for slide in complete]
    assert bounded[0]["archivo"] == "prior.jpg"


def test_cola_stt_no_relee_historial_en_idle_y_descubre_append(tmp_path):
    first_id, first_wav = _sealed(tmp_path, 1)
    queue = transcriptor_vivo.SttQueueIndex(tmp_path)

    initial = queue.snapshot(now=100.0)
    assert initial.ready == [first_wav]
    initial_metrics = queue.metrics()

    for tick in range(10):
        assert queue.snapshot(now=101.0 + tick / 10) is initial

    idle_metrics = queue.metrics()
    assert idle_metrics["rebuilds"] == 1
    assert idle_metrics["cache_hits"] == 10
    assert idle_metrics["manifest"]["bytes_read"] == (
        initial_metrics["manifest"]["bytes_read"])
    assert idle_metrics["manifest"]["full_rebuilds"] == 1

    second_id, second_wav = _sealed(tmp_path, 2)
    after_append = queue.snapshot(now=102.0)
    assert after_append.ready == [first_wav, second_wav]
    assert queue.metrics()["manifest"]["incremental_reads"] == 1
    assert queue.metrics()["manifest"]["full_rebuilds"] == 1

    queue.mark_processed(first_id)
    after_commit = queue.snapshot(now=103.0)
    assert after_commit.unprocessed == [second_wav]
    assert first_id not in {path.stem for path in after_commit.unprocessed}
    assert second_id in {path.stem for path in after_commit.unprocessed}


def test_cola_stt_respeta_retry_crash_y_no_infiere_huerfano_con_manifest(tmp_path):
    first_id, first_wav = _sealed(tmp_path, 1, sha="hash-first")
    second_id, second_wav = _sealed(tmp_path, 2, sha="hash-second")
    orphan = tmp_path / "audio" / "chunk_1700000099_p1.wav"
    orphan.write_bytes(b"RIFF-orphan")
    now = time.time()
    audio_manifest.record_transcription_attempt(
        tmp_path, first_id, "failed", input_sha256="hash-first",
        retryable=True, retry_after=now + 10.0)

    queue = transcriptor_vivo.SttQueueIndex(tmp_path, processed={second_id})
    waiting = queue.snapshot(now=now)
    assert waiting.unprocessed == [first_wav]
    assert waiting.ready == []
    assert orphan not in waiting.all_wav

    # El deadline cambia la elegibilidad aunque ningun archivo haya cambiado.
    due = queue.snapshot(now=now + 11.0)
    assert due.ready == [first_wav]

    # Una instancia nueva reconstruye commits/attempts y llega a la misma cola.
    recovered = transcriptor_vivo.SttQueueIndex(
        tmp_path, processed={second_id}).snapshot(now=now + 11.0)
    assert recovered.ready == [first_wav]
    assert second_wav not in recovered.unprocessed


def test_helpers_incrementales_conservan_semantica_publica(tmp_path):
    chunk_id, _wav = _sealed(tmp_path, 1, sha="same-hash")
    now = time.time()
    audio_manifest.record_transcription_attempt(
        tmp_path, chunk_id, "failed", input_sha256="same-hash",
        retryable=True, retry_after=now + 20)
    events = audio_manifest.read_events(tmp_path)
    attempts = audio_manifest.transcription_attempts(tmp_path)

    assert audio_manifest.latest_by_chunk_rows(events) == (
        audio_manifest.latest_by_chunk(tmp_path))
    assert audio_manifest.capture_records_from_events(events) == (
        transcriptor_vivo.registros_sellados_para_stt(tmp_path))
    assert audio_manifest.transcription_retry_state_from_rows(
        events, attempts, now=now) == audio_manifest.transcription_retry_state(
            tmp_path, now=now)


def test_runtime_conserva_watchdog_y_un_solo_heartbeat_de_propiedad():
    capture_tree = ast.parse(inspect.getsource(captura_audio.main))
    probes = [
        node for node in ast.walk(capture_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "sonda"
    ]
    assert len(probes) == 1
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "pico_navegador"
        for node in ast.walk(probes[0])
    )
    assert "WatchdogCaptura" in inspect.getsource(captura_audio.main)

    transcriber_source = inspect.getsource(transcriptor_vivo._main)
    assert transcriber_source.count("iniciar_heartbeat_instancia") == 1
    assert "renovar_instancia" not in transcriber_source


def test_supervisor_reutiliza_inventario_stt_mientras_no_cambia(
        monkeypatch, tmp_path):
    calls = []
    panel._panel_cache_clear()
    monkeypatch.setattr(panel.est, "leer_estado", lambda _d: {
        "stt_lifecycle": {
            "status": "waiting_audio", "heartbeat": time.time(),
        },
    })
    monkeypatch.setattr(
        panel.audio_manifest, "transcript_status_report",
        lambda *_a, **_k: calls.append(1) or {"unresolved_count": 0},
    )

    class Running:
        @staticmethod
        def poll():
            return None

    child = {
        "mod": "src.transcriptor_vivo", "p": Running(), "intentos": 0,
        "requested_stop": False,
    }
    panel._evaluar_hijo_una_vez(child, tmp_path)
    panel._evaluar_hijo_una_vez(child, tmp_path)

    assert calls == [1]
