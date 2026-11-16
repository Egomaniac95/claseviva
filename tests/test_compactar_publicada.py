import os

import pytest
from PIL import Image

from scripts import compactar_publicada
from src import config, estado, post_procesar, study_package


def _published(tmp_path, monkeypatch):
    d = tmp_path / "contenido" / "curso" / "clases" / "CLASE_01"
    (d / "img").mkdir(parents=True)
    Image.new("RGB", (16, 16), "blue").save(d / "img" / "keep.jpg")
    Image.new("RGB", (16, 16), "red").save(d / "img" / "drop.jpg")
    (d / "audio").mkdir()
    (d / "audio" / "original.wav").write_bytes(b"fixture")
    estado.escribir_estado(d, {"estado": "fin"})
    text = '# Lección\n\nTexto aceptado.\n\n<img src="img/keep.jpg">\n'
    payload = {
        "recording_parts": [{"part": 1}],
        "images": study_package.image_inventory(d, text),
    }
    (d / "leccion_final.md").write_text(
        study_package.metadata_comment(payload) + "\n\n" + text, encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "CONTENIDO_DIR", tmp_path / "contenido")
    monkeypatch.setattr(post_procesar, "verificar_barrera_final", lambda *_: {"ready": True})
    coverage = {"limpieza_segura": True, "wav_total": 1,
                "wav_sin_transcript_total": 0, "stt_invalidos_total": 0,
                "continuidad_audio": {"status": "ok"}}
    monkeypatch.setattr(post_procesar, "analizar_cobertura_audio", lambda *a, **k: coverage)
    return d, coverage


def test_retry_preview_then_cleanup_preserves_published_bytes(tmp_path, monkeypatch):
    d, _ = _published(tmp_path, monkeypatch)
    before = (d / "leccion_final.md").read_bytes()
    preview = compactar_publicada.compactar(d)
    assert not preview["applied"]
    assert (d / "audio" / "original.wav").is_file()
    result = compactar_publicada.compactar(d, aplicar=True)
    assert result["applied"]
    assert (d / "leccion_final.md").read_bytes() == before
    assert sorted(p.name for p in d.iterdir()) == ["img", "leccion_final.html", "leccion_final.md"]
    assert (d / "img" / "keep.jpg").is_file()
    assert not (d / "img" / "drop.jpg").exists()
    assert result["after_bytes"] == sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
    assert result["net_bytes_saved"] == preview["before_bytes"] - result["after_bytes"]
    assert result["net_bytes_saved"] < result["bytes_removed"]  # HTML was generated
    assert len(list((tmp_path / "history").glob("cleanup-*.json"))) == 1


def test_retry_rejects_newer_evidence_before_deleting(tmp_path, monkeypatch):
    d, _ = _published(tmp_path, monkeypatch)
    transcript = d / "transcript.jsonl"
    transcript.write_text('{"texto":"nuevo"}\n', encoding="utf-8")
    newer = (d / "leccion_final.md").stat().st_mtime + 10
    os.utime(transcript, (newer, newer))
    with pytest.raises(RuntimeError, match="posterior"):
        compactar_publicada.compactar(d, aplicar=True)
    assert (d / "audio" / "original.wav").is_file()


def test_retry_never_forces_unresolved_coverage(tmp_path, monkeypatch):
    d, coverage = _published(tmp_path, monkeypatch)
    coverage.update(limpieza_segura=False, wav_sin_transcript_total=1)
    with pytest.raises(RuntimeError, match="cobertura"):
        compactar_publicada.compactar(d, aplicar=True)
    assert (d / "audio" / "original.wav").is_file()


def test_historical_acceptance_is_bound_to_exact_published_lesson(tmp_path, monkeypatch):
    d, coverage = _published(tmp_path, monkeypatch)
    coverage.update(limpieza_segura=False, continuidad_audio={"status": "warning", "wall_gap_max_seconds": 500})
    with pytest.raises(RuntimeError, match="no coincide"):
        compactar_publicada.compactar(d, aplicar=True, recuperacion_confirmada_sha256="wrong")
    before = study_package.sha256_file(d / "leccion_final.md")
    result = compactar_publicada.compactar(d, aplicar=True, recuperacion_confirmada_sha256=before)
    assert result["historical_recovery"]["accepted_lesson_sha256"] == before
    assert result["historical_recovery"]["coverage_reasons_accepted"]
    assert study_package.sha256_file(d / "leccion_final.md") == before


def test_warning_cleanup_requires_archived_source_or_historical_acceptance(tmp_path, monkeypatch):
    d, coverage = _published(tmp_path, monkeypatch)
    coverage.update(limpieza_segura=False,
                    continuidad_audio={"status": "warning", "wall_gap_max_seconds": 52})
    with pytest.raises(RuntimeError, match="cobertura"):
        compactar_publicada.compactar(d, aplicar=True)
    final = d / "leccion_final.md"
    metadata = study_package.read_metadata(d)
    body, source = study_package.attach_source_transcript(
        study_package.strip_metadata(final.read_text(encoding="utf-8")),
        [{"texto": "Texto original íntegro"}])
    metadata["source_transcript"] = source
    final.write_text(study_package.metadata_comment(metadata) + "\n\n" + body, encoding="utf-8")
    before = study_package.sha256_file(final)
    result = compactar_publicada.compactar(d, aplicar=True)
    assert result["applied"]
    assert study_package.sha256_file(final) == before


def test_historical_acceptance_never_bypasses_pending_stt(tmp_path, monkeypatch):
    d, coverage = _published(tmp_path, monkeypatch)
    coverage.update(limpieza_segura=False, wav_sin_transcript_total=1)
    monkeypatch.setattr(post_procesar, "verificar_barrera_final", lambda *_: {"ready": False})
    with pytest.raises(RuntimeError, match="commits pendientes"):
        compactar_publicada.compactar(d, aplicar=True,
                                     recuperacion_confirmada_sha256=study_package.sha256_file(d / "leccion_final.md"))
    assert (d / "audio" / "original.wav").exists()
