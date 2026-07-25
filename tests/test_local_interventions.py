import io
import wave

import pytest

from src import estado, local_interventions, panel, student_notes


def _wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x10\x00" * 1600)
    return stream.getvalue()


def test_borrador_progresivo_es_ordenado_idempotente_y_descartable(tmp_path):
    draft = local_interventions.create_draft(
        tmp_path,
        mime="audio/webm;codecs=opus",
        device_id="mic-123",
        device_label="Auriculares Bluetooth",
        class_seconds=12.5,
    )

    first = local_interventions.append_chunk(
        tmp_path, draft["draft_id"], 0, b"header")
    duplicate = local_interventions.append_chunk(
        tmp_path, draft["draft_id"], 0, b"header")
    local_interventions.append_chunk(
        tmp_path, draft["draft_id"], 1, b"payload")
    sealed = local_interventions.seal_draft(tmp_path, draft["draft_id"])

    assert first["stored"] is True
    assert duplicate["stored"] is False
    assert sealed["status"] == "sealed"
    assert (tmp_path / sealed["preview_path"]).read_bytes() == b"headerpayload"

    report = local_interventions.discard_draft(tmp_path, draft["draft_id"])
    assert report["discarded"] is True
    assert local_interventions.list_drafts(tmp_path) == []


def test_borrador_rechaza_huecos_y_colisiones_sin_publicar(tmp_path):
    draft = local_interventions.create_draft(
        tmp_path, mime="audio/webm", device_id="mic", device_label="Mic",
        class_seconds=0,
    )
    local_interventions.append_chunk(tmp_path, draft["draft_id"], 0, b"one")
    with pytest.raises(ValueError, match="secuencia"):
        local_interventions.append_chunk(tmp_path, draft["draft_id"], 2, b"gap")
    with pytest.raises(ValueError, match="colisi"):
        local_interventions.append_chunk(tmp_path, draft["draft_id"], 0, b"other")

    assert student_notes.list_notes(tmp_path) == []


def test_guardar_intervencion_la_incluye_y_preserva_procedencia(tmp_path):
    draft = local_interventions.create_draft(
        tmp_path,
        mime="audio/wav",
        device_id="communications-default",
        device_label="Micrófono Realtek",
        class_seconds=88.25,
    )
    local_interventions.append_chunk(
        tmp_path, draft["draft_id"], 0, _wav_bytes())
    local_interventions.seal_draft(tmp_path, draft["draft_id"])

    note = local_interventions.commit_draft(tmp_path, draft["draft_id"])

    assert note["source"]["kind"] == "local_intervention"
    assert note["source"]["device_label"] == "Micrófono Realtek"
    assert note["class_time"]["seconds"] == 88.25
    assert note["inclusion"] == {"state": "included", "target": "lesson", "reason": ""}
    assert local_interventions.list_drafts(tmp_path) == []


def test_transcripcion_reutiliza_modelo_y_no_toca_evidencia_docente(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    manifest = tmp_path / "audio_manifest.jsonl"
    transcript.write_text('{"texto":"profesor"}\n', encoding="utf-8")
    manifest.write_text('{"status":"sealed"}\n', encoding="utf-8")
    before = (transcript.read_bytes(), manifest.read_bytes())
    draft = local_interventions.create_draft(
        tmp_path, mime="audio/wav", device_id="mic", device_label="Mic",
        class_seconds=42,
    )
    local_interventions.append_chunk(
        tmp_path, draft["draft_id"], 0, _wav_bytes())
    local_interventions.seal_draft(tmp_path, draft["draft_id"])
    note = local_interventions.commit_draft(tmp_path, draft["draft_id"])

    class Segment:
        text = " mi intervención "

    class Info:
        language = "es"

    class Model:
        def transcribe(self, _path, **_kwargs):
            return [Segment()], Info()

    done = student_notes.transcribe_audio_note(
        tmp_path, note["note_id"], teacher_capture_active=True, model=Model(),
        model_name="fixture-model",
    )

    assert done["extraction"]["text"] == "mi intervención"
    assert done["extraction"]["engine"] == "remote-chain:fixture-model"
    assert (transcript.read_bytes(), manifest.read_bytes()) == before


def test_panel_publica_selector_botones_y_api_de_borrador(monkeypatch, tmp_path):
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)
    client = panel.app.test_client()

    created = client.post("/api/local_interventions", json={
        "mime": "audio/webm", "device_id": "mic-x",
        "device_label": "Mic X", "class_seconds": 3,
    })
    assert created.status_code == 200
    draft_id = created.get_json()["draft"]["draft_id"]
    chunk = client.post(
        f"/api/local_interventions/{draft_id}/chunks/0",
        data=b"webm-data", content_type="audio/webm",
    )
    assert chunk.status_code == 200
    assert client.post(
        f"/api/local_interventions/{draft_id}/seal").status_code == 200
    assert client.post(
        f"/api/local_interventions/{draft_id}/discard").status_code == 200

    assert 'id="localMicSelect"' in panel.PAGINA
    assert "Actualizar micrófonos" in panel.PAGINA
    assert "Iniciar mi intervención" in panel.PAGINA
    assert "deviceId:{exact:selected}" in panel.PAGINA
    assert "localInterventionBeforeCommand" in panel.PAGINA
    assert "createdDraftId)+'/discard'" in panel.PAGINA
    assert "chrome.runtime" not in panel.PAGINA
    assert "meet.google.com" not in panel.PAGINA.casefold()


def test_original_empaquetado_de_nota_sigue_siendo_servido(monkeypatch, tmp_path):
    note = student_notes.capture_text(tmp_path, "nota")
    packaged = student_notes.relocate_to_study_package(tmp_path)
    assert packaged is not None
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)

    response = panel.app.test_client().get(
        f"/api/student_notes/{note['note_id']}/original")

    assert response.status_code == 200
    assert response.data == b"nota"


def test_estado_de_borradores_detecta_pendiente_antes_de_fin(tmp_path):
    assert local_interventions.pending_report(tmp_path)["blocking"] is False
    draft = local_interventions.create_draft(
        tmp_path, mime="audio/webm", device_id="mic", device_label="Mic",
        class_seconds=1,
    )
    report = local_interventions.pending_report(tmp_path)
    assert report["blocking"] is True
    assert report["draft_ids"] == [draft["draft_id"]]


def test_fin_servidor_bloquea_borrador_hasta_guardar_o_descartar(
        monkeypatch, tmp_path):
    estado.escribir_estado(tmp_path, {
        "inicio": 1000.0, "estado": "grabando", "parte": 1,
        "clase_id": "CLASE_01", "nombre": "Tema", "curso": "curso",
    })
    local_interventions.create_draft(
        tmp_path, mime="audio/webm", device_id="mic", device_label="Mic",
        class_seconds=5,
    )
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)

    response = panel.app.test_client().post(
        "/api/comando", json={"cmd": "fin"})

    assert response.status_code == 409
    assert "Guarda o descarta" in response.get_json()["msg"]
    assert estado.leer_estado(tmp_path)["estado"] == "grabando"
