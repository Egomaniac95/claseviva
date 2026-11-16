import json
import sys

import pytest
from PIL import Image

from src import config, estado, panel, post_procesar, student_notes, study_package


def _payload(parts=None):
    return {
        "kind": study_package.KIND,
        "schema_version": study_package.SCHEMA_VERSION,
        "class": {
            "class_id": "CLASE_01_tema",
            "name": "Tema",
            "course": "curso",
            "week": 1,
            "origin": "vivo",
            "started_at": 1000.0,
        },
        "recording_parts": parts or [{
            "part": 1,
            "useful_start_sec": 0.0,
            "useful_end_sec": 300.0,
            "wall_start": 1000.0,
            "wall_end": 1300.0,
        }],
        "useful_duration_sec": 300.0,
        "package_policy": "lesson_and_referenced_assets",
    }


def _lesson(payload=None, image="keep.jpg"):
    data = payload or _payload()
    return (
        study_package.metadata_comment(data)
        + "\n\n# Tema\n\n"
        + study_package.recording_map_markdown(data["recording_parts"])
        + "\n\n## Resumen ejecutivo\n\nResumen.\n\n---\n\n"
        + f'<img src="img/{image}" width="760">\n'
    )


def test_metadata_y_mapa_de_bloques_son_reanudables_y_no_dividen_temas():
    parts = [
        {
            "part": 1, "useful_start_sec": 0, "useful_end_sec": 5975,
            "wall_start": 1000, "wall_end": 6975,
        },
        {
            "part": 2, "useful_start_sec": 5975, "useful_end_sec": 11028,
            "wall_start": 9000, "wall_end": 14053,
        },
    ]
    markdown = _lesson(_payload(parts))
    parsed = study_package.parse_metadata(markdown)

    assert parsed is not None
    assert parsed["recording_parts"] == parts
    assert "Parte 1 | 00:00:00 → 01:39:35" in markdown
    assert "Parte 2 | 01:39:35 → 03:03:48" in markdown
    assert "el contenido de estudio está organizado por temas" in markdown
    assert study_package.MARKER not in study_package.strip_metadata(markdown)


def test_migracion_legacy_solo_agrega_metadata_y_mapa_sin_llm(tmp_path):
    d = tmp_path / "curso" / "clases" / "CLASE_01_tema"
    (d / "img").mkdir(parents=True)
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    original = (
        '# Tema\n\n> Fecha: 2026-07-20\n\n## Resumen ejecutivo\n\nTexto.\n\n'
        '<img src="img/keep.jpg" width="760">\n'
    )
    (d / "leccion_final.md").write_text(original, encoding="utf-8")
    estado.escribir_estado(d, {
        "inicio": 1000.0, "estado": "fin", "parte": 1,
        "clase_id": d.name, "nombre": "Tema", "curso": "curso",
        "semana": 1, "origen": "vivo",
    })
    estado.anexar_jsonl(d / "marcas.jsonl", {
        "t": 1000.0, "tipo": "inicio", "parte": 1,
    })
    estado.anexar_jsonl(d / "marcas.jsonl", {
        "t": 1300.0, "tipo": "fin", "parte": 1,
    })

    report = study_package.migrate_legacy_lesson(d)
    migrated = (d / "leccion_final.md").read_text(encoding="utf-8")

    assert report["changed"] is True
    assert study_package.strip_metadata(migrated).replace(
        study_package.recording_map_markdown(
            report["metadata"]["recording_parts"]) + "\n\n",
        "",
    ) == original
    assert "Parte 1 | 00:00:00 → 00:05:00" in migrated
    assert study_package.validate_candidate(d)["image_hashes_verified"] is True
    assert study_package.migrate_legacy_lesson(d)["changed"] is False


def test_hidrata_paquete_cerrado_y_abre_parte_siguiente_sin_audio_historico(
        monkeypatch, tmp_path):
    d = tmp_path / "contenido" / "curso" / "clases" / "CLASE_01_tema"
    (d / "img").mkdir(parents=True)
    parts = [
        {
            "part": 1, "useful_start_sec": 0, "useful_end_sec": 300,
            "wall_start": 1000, "wall_end": 1300,
        },
        {
            "part": 2, "useful_start_sec": 300, "useful_end_sec": 600,
            "wall_start": 2000, "wall_end": 2300,
        },
    ]
    (d / "leccion_final.md").write_text(_lesson(_payload(parts)), encoding="utf-8")
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setattr(estado.time, "time", lambda: 3000.0)

    hydrated = estado.hidratar_clase_compactada(d, ahora=3000.0)
    resumed = estado.reanudar(d)
    estado.transicionar_captura(d, "fin", ahora=3400.0)
    ranges = estado.rangos_partes(d)
    origin = post_procesar._origen_postproceso(d, estado.leer_estado(d))
    blocks = post_procesar._bloques_grabacion(ranges, origin)

    assert hydrated["estado"] == "fin"
    assert resumed["parte"] == 3
    assert not list((d / "audio").glob("*.wav"))
    assert [(part, start, end) for part, start, end in ranges] == [
        (1, 1000.0, 1300.0),
        (2, 2000.0, 2300.0),
        (3, 3000.0, 3400.0),
    ]
    assert [
        (item["part"], item["useful_start_sec"], item["useful_end_sec"])
        for item in blocks
    ] == [
        (1, 0.0, 300.0),
        (2, 300.0, 600.0),
        (3, 600.0, 1000.0),
    ]


def test_paquete_ligero_conserva_solo_final_e_imagenes_referenciadas(tmp_path):
    d = tmp_path / "curso" / "clases" / "CLASE_01_tema"
    (d / "img").mkdir(parents=True)
    (d / "audio").mkdir()
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    Image.new("RGB", (20, 20), "black").save(d / "img" / "drop.jpg")
    (d / "audio" / "chunk.wav").write_bytes(b"RIFFfixture")
    (d / "transcript.jsonl").write_text(
        json.dumps({"texto": "contenido"}) + "\n", encoding="utf-8")
    (d / "session_state.json").write_text("{}", encoding="utf-8")
    (d / "_debug").mkdir()
    (d / "_debug" / "trace.txt").write_text("debug", encoding="utf-8")
    (d / "leccion_final.md").write_text(_lesson(), encoding="utf-8")

    report = study_package.finalize_lightweight_package(
        d, evidence_safe=True)

    assert report["ok"] is True
    assert sorted(path.name for path in d.iterdir()) == ["img", "leccion_final.html", "leccion_final.md"]
    assert sorted(path.name for path in (d / "img").iterdir()) == ["keep.jpg"]
    assert study_package.validate_candidate(d)["ok"] is True


def test_paquete_no_elimina_nada_si_falta_una_imagen_publicada(tmp_path):
    d = tmp_path / "clase"
    (d / "img").mkdir(parents=True)
    (d / "audio").mkdir()
    (d / "audio" / "chunk.wav").write_bytes(b"audio")
    (d / "leccion_final.md").write_text(
        _lesson(image="missing.jpg"), encoding="utf-8")

    with pytest.raises(RuntimeError, match="imágenes referenciadas ausentes o alteradas"):
        study_package.finalize_lightweight_package(d, evidence_safe=True)

    assert (d / "audio" / "chunk.wav").is_file()
    assert (d / "leccion_final.md").is_file()


def test_hidratacion_revierte_staging_huerfano_antes_de_continuar(tmp_path):
    d = tmp_path / "clases" / "CLASE_01_tema"
    (d / "img").mkdir(parents=True)
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    (d / "leccion_final.md").write_text(_lesson(), encoding="utf-8")
    stage = d.parent / f".{d.name}.compactando-20260717-fixture"
    stage.mkdir()
    saved = {
        "inicio": 1000.0,
        "estado": "fin",
        "parte": 1,
        "clase_id": d.name,
        "nombre": "Tema",
        "curso": "curso",
    }
    (stage / "session_state.json").write_text(
        json.dumps(saved), encoding="utf-8")

    restored = estado.hidratar_clase_compactada(d)

    assert restored == saved
    assert (d / "session_state.json").is_file()
    assert not stage.exists()


def test_reanudar_estado_existente_tambien_recupera_staging_parcial(
        monkeypatch, tmp_path):
    d = tmp_path / "clases" / "CLASE_01_tema"
    d.mkdir(parents=True)
    estado.escribir_estado(d, {
        "inicio": 1000.0,
        "estado": "fin",
        "parte": 1,
        "clase_id": d.name,
        "nombre": "Tema",
        "curso": "curso",
    })
    estado.anexar_jsonl(
        d / "marcas.jsonl",
        {"t": 1300.0, "tipo": "fin", "parte": 1},
    )
    stage = d.parent / f".{d.name}.compactando-20260717-parcial"
    stage.mkdir()
    (stage / "transcript.jsonl").write_text(
        json.dumps({"texto": "recuperado"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setattr(estado.time, "time", lambda: 2000.0)

    resumed = estado.reanudar(d)

    assert resumed["parte"] == 2
    assert (d / "transcript.jsonl").is_file()
    assert not stage.exists()


def test_notas_se_materializan_en_final_y_originales_se_eliminan(tmp_path):
    d = tmp_path / "clase"
    (d / "img").mkdir(parents=True)
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    note = student_notes.capture_text(d, "Mi observación", class_seconds=10)
    student_notes.set_inclusion(d, note["note_id"], "included", target="lesson")
    lesson = _lesson().rstrip() + "\n\n" + student_notes.lesson_section(d)
    (d / "leccion_final.md").write_text(lesson, encoding="utf-8")

    study_package.finalize_lightweight_package(d, evidence_safe=True)
    final = (d / "leccion_final.md").read_text(encoding="utf-8")

    assert not (d / "student_notes").exists()
    assert not (d / "img" / ".student_notes").exists()
    assert "Mi observación" in final
    assert "sha256:" in final


def test_leccion_adicional_incrustada_conserva_solo_su_visual(tmp_path):
    d = tmp_path / "clase"
    (d / "img").mkdir(parents=True)
    (d / "lecciones_detectadas").mkdir()
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    Image.new("RGB", (20, 20), "blue").save(d / "img" / "extra.jpg")
    Image.new("RGB", (20, 20), "black").save(d / "img" / "drop.jpg")
    extra = d / "lecciones_detectadas" / "01_otro_tema.md"
    extra.write_text(
        '# Otro tema\n\n<img src="img/extra.jpg" width="760">\n',
        encoding="utf-8",
    )
    lesson = (
        _lesson().rstrip()
        + "\n\n## Lecciones adicionales\n\n"
        + extra.read_text(encoding="utf-8")
    )
    (d / "leccion_final.md").write_text(lesson, encoding="utf-8")

    study_package.finalize_lightweight_package(d, evidence_safe=True)
    assert not (d / "img" / ".additional_lessons").exists()
    assert 'src="img/extra.jpg"' in (d / "leccion_final.md").read_text(
        encoding="utf-8")
    assert (d / "img" / "extra.jpg").is_file()
    assert not (d / "img" / "drop.jpg").exists()
    assert not (d / "lecciones_detectadas").exists()


def test_paquete_rechaza_imagen_alterada_segun_manifest_sin_eliminar(tmp_path):
    d = tmp_path / "clase"
    (d / "img").mkdir(parents=True)
    (d / "audio").mkdir()
    image = d / "img" / "keep.jpg"
    Image.new("RGB", (20, 20), "white").save(image)
    payload = _payload()
    payload["images"] = study_package.image_inventory(
        d, '<img src="img/keep.jpg" width="760">')
    (d / "leccion_final.md").write_text(
        _lesson(payload), encoding="utf-8")
    Image.new("RGB", (20, 20), "black").save(image)
    (d / "audio" / "chunk.wav").write_bytes(b"evidence")

    with pytest.raises(RuntimeError, match="sha256 distinto"):
        study_package.finalize_lightweight_package(d, evidence_safe=True)

    assert (d / "audio" / "chunk.wav").is_file()
    assert image.is_file()


def test_nota_visual_publicada_sobrevive_y_su_manifest_no(tmp_path):
    d = tmp_path / "clase"
    (d / "img").mkdir(parents=True)
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    source = tmp_path / "note.png"
    Image.new("RGB", (32, 18), "blue").save(source)
    note = student_notes.capture_file(
        d, source.read_bytes(), original_name="apunte.png", mime="image/png",
        source_kind="photo", user_action="select", class_seconds=20,
    )
    student_notes.set_inclusion(d, note["note_id"], "included", target="lesson")
    section = student_notes.lesson_section(d, publish_assets=True)
    lesson = _lesson().rstrip() + "\n\n" + section
    (d / "leccion_final.md").write_text(lesson, encoding="utf-8")

    study_package.finalize_lightweight_package(d, evidence_safe=True)

    final = (d / "leccion_final.md").read_text(encoding="utf-8")
    refs = study_package.local_image_references(final)
    assert any(name.startswith("nota_estudiante_") for name in refs)
    assert all((d / "img" / name).is_file() for name in refs)
    assert not (d / "student_notes").exists()


def test_cli_cierra_en_paquete_ligero_por_defecto_y_auditoria_es_opt_in(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    estado.escribir_estado(d, {"estado": "fin", "parte": 1, "inicio": 1000.})
    (d / "img").mkdir()
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    (d / "leccion_final.md").write_text(_lesson(), encoding="utf-8")
    from src.lesson_export import export_html
    export_html(d)
    calls = []
    monkeypatch.setattr(
        post_procesar, "consolidar_todo",
        lambda *_args, **kwargs: calls.append(kwargs),
    )

    monkeypatch.setattr(sys, "argv", ["post_procesar", "--todo", "--clase", str(d)])
    assert post_procesar.main() == 0
    monkeypatch.setattr(sys, "argv", [
        "post_procesar", "--todo", "--clase", str(d), "--conservar-evidencia",
    ])
    assert post_procesar.main() == 0

    assert calls[0]["paquete_ligero"] is True
    assert calls[0]["limpiar_al_final"] is True
    assert calls[1]["paquete_ligero"] is False
    assert calls[1]["limpiar_al_final"] is False


def test_panel_continua_paquete_sin_session_state(monkeypatch, tmp_path):
    content = tmp_path / "contenido"
    d = content / "curso" / "clases" / "CLASE_01_tema"
    (d / "img").mkdir(parents=True)
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.jpg")
    (d / "leccion_final.md").write_text(_lesson(), encoding="utf-8")
    monkeypatch.setattr(config, "CONTENIDO_DIR", content)
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setattr(panel, "MODO_APP", True)
    monkeypatch.setattr(panel, "_clase", lambda: None)
    monkeypatch.setattr(panel, "_lanzar_hijos", lambda: None)
    monkeypatch.setattr(panel.stt_backend, "preflight", lambda **_kwargs: {
        "ok": True, "provider": "remote-chain", "model": "remote-failover"})

    response = panel.app.test_client().post("/api/iniciar", json={
        "tipo": "continuar",
        "curso": "curso",
        "clase": "CLASE_01_tema",
        "fuente": "sistema",
        "stt_backend": "remote",
        "stt_remote_consent": True,
    })

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert "parte 2" in response.get_json()["msg"]
    assert estado.leer_estado(d)["parte"] == 2
