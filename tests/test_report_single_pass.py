"""El reporte STT clasifica igual con una sola pasada del manifest.

Fija byte a byte la salida de ``transcript_status_report`` y ``capture_records``
sobre un escenario que ejercita cada rama (commit válido, corrupto, incompleto,
parcial huérfano, pendiente, sin audio, legacy y captura en curso), y luego
verifica que la implementación no relee el manifest ni abre WAV ya cubiertos.
"""
import json
import wave
from pathlib import Path

from src import audio_manifest


def _wav(path: Path, frames: int = 400, rate: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x01\x00" * frames)


A = "chunk_000000000100_p1_v10"
B = "chunk_000000000110_p1_v10"
C = "chunk_000000000120_p1_v10"
D = "chunk_000000000130_p1_v10"
E = "chunk_000000000140_p1_v10"
F = "chunk_000000000150_p1_v10"
G = "chunk_000000000160_p1_v10"
H = "chunk_000000000170_p1_v10"


def _sellar(clase: Path, chunk_id: str, sequence: int, con_wav: bool = True) -> str:
    ruta = clase / "audio" / f"{chunk_id}.wav"
    sha = ""
    if con_wav:
        _wav(ruta)
        sha = audio_manifest.sha256_file(ruta) or ""
    audio_manifest.append_event(clase, {
        "chunk_id": chunk_id, "status": "sealed", "sequence": sequence,
        "path": f"{chunk_id}.wav", "sha256": sha or "f" * 64,
        "transactional_stt_required": True,
        "wall_start": 100.0 + sequence * 10, "rate": 100,
        "frame_count": 400, "duration": 4.0,
    })
    return sha


def _commit(clase: Path, chunk_id: str, sha: str, lineas: list[dict],
            status: str = "complete", metadata_extra: dict | None = None) -> None:
    journal = clase / audio_manifest.JOURNAL_DIR
    journal.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1, "chunk_id": chunk_id, "status": status,
        "committed_at": 1000.0, "lines": lineas,
        "metadata": {"input_sha256": sha, **(metadata_extra or {})},
    }
    (journal / f"{chunk_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _escenario(tmp_path: Path) -> Path:
    clase = tmp_path / "clase"
    sha_a = _sellar(clase, A, 1)
    sha_b = _sellar(clase, B, 2)
    _sellar(clase, C, 3)
    _sellar(clase, D, 4)
    _sellar(clase, E, 5, con_wav=False)
    _sellar(clase, F, 6)
    # A: commit completo y válido con dos líneas y duración STT facturada.
    _commit(clase, A, sha_a, [
        {"t": 101.0, "texto": "hola"}, {"t": 102.0, "texto": "mundo"}],
        metadata_extra={"stt_submitted_audio_seconds": 31.5})
    # B: commit ilegible.
    journal = clase / audio_manifest.JOURNAL_DIR
    (journal / f"{B}.json").write_text("{corrupto", encoding="utf-8")
    # C: commit legible pero no completo.
    _commit(clase, C, sha_b, [], status="pending")
    # F: solo un staging huérfano.
    (journal / f"{F}.json.part.9999.1.2").write_text("{}", encoding="utf-8")
    # G: WAV legacy sin manifest, con línea preservada en transcript.jsonl.
    _wav(clase / "audio" / f"{G}.wav")
    (clase / "transcript.jsonl").write_text(
        json.dumps({"t": 160.0, "texto": "legacy", "chunk": f"{G}.wav"}) + "\n",
        encoding="utf-8")
    # H: productor sigue en capturing; STT debe esperar aunque el WAV exista.
    _wav(clase / "audio" / f"{H}.wav")
    audio_manifest.append_event(clase, {
        "chunk_id": H, "status": "capturing", "sequence": 7,
        "path": f"{H}.wav", "wall_start": 170.0,
    })
    return clase


def test_reporte_clasifica_cada_rama_igual_que_el_contrato(tmp_path):
    clase = _escenario(tmp_path)

    reporte = audio_manifest.transcript_status_report(clase, verify_files=True)

    assert reporte["source"] == "transcript_chunks"
    assert reporte["captured_chunks"] == sorted([A, B, C, D, E, F, G])
    assert reporte["available_audio_chunks"] == sorted([A, B, C, D, F, G])
    assert reporte["completed_chunks"] == sorted([A, G])
    assert reporte["empty_completed_chunks"] == []
    assert reporte["missing_audio_chunks"] == [E]
    assert reporte["pending_chunks"] == [D]
    assert reporte["invalid_chunks"] == sorted([B, C, F])
    assert reporte["actionable_chunks"] == sorted([B, C, D, F])
    assert reporte["unresolved_chunks"] == sorted([B, C, D, E, F])
    assert reporte["invalid_reasons"] == {
        B: "commit_corrupt", C: "commit_not_complete", F: "commit_partial_file"}
    assert reporte["chunk_states"] == {
        A: "complete", B: "invalid", C: "invalid", D: "pending",
        E: "missing_audio", F: "invalid", G: "complete_legacy"}
    assert reporte["chunk_paths"] == {
        cid: f"{cid}.wav" for cid in [A, B, C, D, E, F, G]}
    assert reporte["segment_count"] == 2
    # Solo los commits válidos suman su duración STT enviada (A: 31.5 s).
    assert reporte["stt_submitted_audio_seconds_total"] == 31.5
    assert reporte["captured_count"] == 7
    assert reporte["available_audio_count"] == 6
    assert reporte["missing_audio_count"] == 1
    assert reporte["completed_count"] == 2
    assert reporte["empty_completed_count"] == 0
    assert reporte["pending_count"] == 1
    assert reporte["invalid_count"] == 3
    assert reporte["actionable_count"] == 4
    assert reporte["unresolved_count"] == 5
    assert H not in reporte["chunk_states"]


def test_capture_records_conserva_secuencia_legacy_y_excluye_capturing(tmp_path):
    clase = _escenario(tmp_path)

    rows = audio_manifest.capture_records(clase)

    por_chunk = {row["chunk_id"]: row for row in rows}
    assert set(por_chunk) == {A, B, C, D, E, F, G}
    fila_g = por_chunk[G]
    assert fila_g["inferred_legacy"] is True
    assert fila_g["inferred_with_manifest"] is True
    # La numeración cuenta la posición en el glob completo (A..G ordenados,
    # E no tiene WAV): A=1, B=2, C=3, D=4, F=5, G=6, H=7.
    assert fila_g["sequence"] == 6


def test_reporte_lee_el_manifest_una_sola_vez(tmp_path, monkeypatch):
    clase = _escenario(tmp_path)
    llamadas = {"n": 0}
    original = audio_manifest.read_events

    def contado(target):
        llamadas["n"] += 1
        return original(target)

    monkeypatch.setattr(audio_manifest, "read_events", contado)
    audio_manifest.transcript_status_report(clase, verify_files=True)

    assert llamadas["n"] == 1


def test_capture_records_no_abre_wavs_cubiertos_por_el_manifest(tmp_path, monkeypatch):
    clase = _escenario(tmp_path)
    abiertos: list[str] = []
    original = wave.open

    def contado(path, *args, **kwargs):
        abiertos.append(Path(str(path)).stem)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(audio_manifest.wave, "open", contado)
    audio_manifest.capture_records(clase)

    # Solo el legacy G exige leer su cabecera; A..F y H ya están en el manifest.
    assert abiertos == [G]
