import io
import wave

from PIL import Image

from src import config, panel, student_notes


def _wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x10\x00" * 1600)
    return stream.getvalue()


def test_texto_conserva_original_procedencia_revision_e_inclusion(tmp_path):
    note = student_notes.capture_text(
        tmp_path,
        "Mi explicación original",
        class_seconds=123.5,
    )

    original = tmp_path / note["original"]["path"]
    assert original.read_bytes() == "Mi explicación original".encode("utf-8")
    assert note["source"]["kind"] == "free_text"
    assert note["class_time"] == {"mode": "relative", "seconds": 123.5}
    assert note["inclusion"] == {
        "state": "included", "target": "lesson", "reason": "explicit_capture"}

    student_notes.correct_note(tmp_path, note["note_id"], "Versión corregida")
    student_notes.set_inclusion(
        tmp_path, note["note_id"], "included", target="lesson")
    index = student_notes.load_index(tmp_path)
    current = index["notes"][note["note_id"]]
    assert current["correction"]["text"] == "Versión corregida"
    assert current["revision"] == 3
    assert current["inclusion"]["state"] == "included"


def test_archivo_duplicado_reusa_blob_pero_conserva_dos_procedencias(tmp_path):
    first = student_notes.capture_file(
        tmp_path, b"same bytes", original_name="uno.txt", mime="text/plain",
        source_kind="document", user_action="select", class_seconds=1,
    )
    second = student_notes.capture_file(
        tmp_path, b"same bytes", original_name="dos.txt", mime="text/plain",
        source_kind="document", user_action="select", class_seconds=2,
    )

    assert first["note_id"] != second["note_id"]
    assert first["original"]["path"] == second["original"]["path"]
    assert len(list((tmp_path / "student_notes" / "originals").iterdir())) == 1
    assert len(student_notes.load_index(tmp_path)["notes"]) == 2


def test_imagen_extrae_ocr_sin_modificar_original(monkeypatch, tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (24, 16), "white").save(image, "PNG")
    monkeypatch.setattr(student_notes, "_ocr_image", lambda _path: "peso = peso - lr * grad")

    note = student_notes.capture_file(
        tmp_path, image.getvalue(), original_name="foto.png", mime="image/png",
        source_kind="photo", user_action="select", class_seconds=4,
    )

    assert note["extraction"]["status"] == "ready"
    assert note["extraction"]["kind"] == "ocr"
    assert "grad" in note["extraction"]["text"]
    assert (tmp_path / note["original"]["path"]).read_bytes() == image.getvalue()


def test_audio_de_nota_se_transcribe_solo_fuera_de_captura_docente(
        monkeypatch, tmp_path):
    note = student_notes.capture_file(
        tmp_path, _wav_bytes(), original_name="nota.wav", mime="audio/wav",
        source_kind="microphone", user_action="record", class_seconds=8,
    )
    assert note["extraction"]["status"] == "pending"
    monkeypatch.setattr(
        student_notes, "_transcribe_wav", lambda _path, **_kwargs: {
            "text": "recordar comparar train y validation",
            "engine": "fake-whisper",
            "language": "es",
        })

    done = student_notes.transcribe_audio_note(
        tmp_path, note["note_id"], teacher_capture_active=False)

    assert done["extraction"]["status"] == "ready"
    assert done["extraction"]["text"].startswith("recordar")
    assert done["extraction"]["derived_paths"][0].endswith("audio.wav")


def test_leccion_separa_notas_y_nunca_escribe_transcript_docente(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"texto":"voz del profesor"}\n', encoding="utf-8")
    before = transcript.read_bytes()
    note = student_notes.capture_text(tmp_path, "Mi duda", class_seconds=9)
    student_notes.set_inclusion(tmp_path, note["note_id"], "included", target="lesson")

    section = student_notes.lesson_section(tmp_path)

    assert "## Notas del estudiante" in section
    assert "no forman parte de la transcripción del profesor" in section
    assert "Mi duda" in section
    assert transcript.read_bytes() == before


def test_intervencion_local_incluida_se_publica_con_atribucion_separada(tmp_path):
    note = student_notes.capture_file(
        tmp_path, b"mi aporte", original_name="intervencion.txt",
        mime="text/plain", source_kind="local_intervention",
        user_action="record", class_seconds=42,
    )

    section = student_notes.lesson_section(tmp_path)

    assert note["inclusion"]["state"] == "included"
    assert "## Notas del estudiante" in section
    assert "Intervenci\u00f3n del estudiante" in section
    assert "mi aporte" in section


def test_nota_legacy_sin_decision_bloquea_paquete_y_no_se_pierde(tmp_path):
    note = student_notes.capture_text(tmp_path, "nota durable")
    current = student_notes._current(tmp_path, note["note_id"])
    current["inclusion"] = {"state": "pending", "target": "none", "reason": ""}
    student_notes._revise(tmp_path, current, "legacy_pending_fixture")

    report = student_notes.package_readiness_report(tmp_path)

    assert report["ok"] is False
    assert report["issues"] == [{
        "note_id": note["note_id"], "reason": "student_decision_pending"}]
    assert (tmp_path / note["original"]["path"]).is_file()


def test_panel_expone_modulo_y_api_multipart(monkeypatch, tmp_path):
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)
    client = panel.app.test_client()

    response = client.post(
        "/api/student_notes",
        data={"text": "nota desde panel", "class_seconds": "2.5"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    listing = client.get("/api/student_notes").get_json()
    assert listing["ok"] is True
    assert len(listing["notes"]) == 1
    assert 'id="studentNotes"' in panel.PAGINA
    assert "Grabar voz" in panel.PAGINA


def test_modulo_visible_por_defecto_y_rollback_no_borra_notas(
        monkeypatch, tmp_path):
    monkeypatch.delenv("CLASE_STUDENT_NOTES", raising=False)
    assert config.student_notes_enabled() is True
    student_notes.capture_text(tmp_path, "original preservado")
    monkeypatch.setenv("CLASE_STUDENT_NOTES", "0")
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)

    response = panel.app.test_client().get("/api/student_notes")

    assert response.status_code == 503
    assert response.get_json()["ok"] is False
    assert len(student_notes.list_notes(tmp_path)) == 1
    assert "student_notes_enabled===false" in panel.PAGINA


def test_descartar_nota_es_recuperable_conserva_original_y_no_publica(tmp_path):
    note = student_notes.capture_text(tmp_path, "borrador que no quiero incluir")
    original = tmp_path / note["original"]["path"]

    discarded = student_notes.discard_note(tmp_path, note["note_id"])

    assert discarded["inclusion"]["state"] == "discarded"
    assert discarded["event_type"] == "discarded"
    assert original.read_text(encoding="utf-8") == "borrador que no quiero incluir"
    assert student_notes.list_notes(tmp_path) == []
    assert len(student_notes.list_notes(tmp_path, include_discarded=True)) == 1
    assert student_notes.lesson_section(tmp_path) == ""


def test_api_descarta_nota_y_la_oculta_sin_borrar_blob(monkeypatch, tmp_path):
    note = student_notes.capture_text(tmp_path, "nota de voz simulada")
    original = tmp_path / note["original"]["path"]
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)

    response = panel.app.test_client().patch(
        f"/api/student_notes/{note['note_id']}", json={"discarded": True})

    assert response.status_code == 200
    assert response.get_json()["note"]["inclusion"]["state"] == "discarded"
    assert original.is_file()
    assert panel.app.test_client().get(
        "/api/student_notes").get_json()["notes"] == []
    assert "studentNoteDiscard" in panel.PAGINA
    assert "studentNoteDiscardDraft" in panel.PAGINA
