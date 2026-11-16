import base64
import hashlib
import re

import pytest
from PIL import Image

from src import audio_manifest, lesson_export, panel, study_package


@pytest.mark.parametrize("status,observed,expected", [
    ("discarded_silence", 145.0, "ok"),
    ("sealed", 145.0, "warning"),
    ("discarded_silence", 140.0, "warning"),
    ("discarded_silence", 150.0, "warning"),
])
def test_pause_after_silence_requires_acquisition_proof(tmp_path, status, observed, expected):
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "before", "status": "capturing", "observed_at": 115.0,
    })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "before", "status": status, "observed_at": observed,
        "pcm_wall_start": 110.0, "pcm_wall_end": 140.0,
        "stream_id": "old", "start_frame": 0, "end_frame": 3000,
        "rate": 100, "frame_count": 3000, "expected_frames": 3000,
    })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "after", "status": "capturing", "observed_at": 500.5,
    })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "after", "status": "sealed", "observed_at": 531.0,
        "pcm_wall_start": 500.6, "pcm_wall_end": 530.6,
        "stream_id": "new", "start_frame": 0, "end_frame": 3000,
        "rate": 100, "frame_count": 3000, "expected_frames": 3000,
    })
    report = audio_manifest.continuity_report(
        tmp_path, declared_inactive_intervals=[(143.0, 500.0)])
    assert report["status"] == expected
    if expected == "ok":
        assert report["boundaries"][0]["pause_silence_drain_seconds"] == 3


def test_html_embeds_image_bytes_and_preserves_markdown(tmp_path):
    (tmp_path / "img").mkdir()
    image = tmp_path / "img" / "example.png"
    Image.new("RGB", (20, 30), "blue").save(image)
    body = '# Tema\n\n![Ejemplo](img/example.png)\n\n**FINAL COMPLETO**\n'
    meta = {"kind": study_package.KIND, "schema_version": study_package.SCHEMA_VERSION,
            "recording_parts": [{"part": 1, "useful_start_sec": 0, "useful_end_sec": 30}],
            "images": study_package.image_inventory(tmp_path, body)}
    md = tmp_path / "leccion_final.md"
    md.write_text(study_package.metadata_comment(meta) + body, encoding="utf-8")
    before = md.read_bytes()
    lightweight = lesson_export.export_html(tmp_path).read_text(encoding="utf-8")
    assert 'src="img/example.png"' in lightweight
    assert 'data:image/' not in lightweight
    assert 'loading="lazy"' in lightweight
    out = lesson_export.render_html(tmp_path, self_contained=True)
    assert md.read_bytes() == before
    assert 'src="/img/' not in out
    assert 'href="/leccion_final.md"' not in out
    encoded = re.search(r'src="data:image/png;base64,([^"]+)"', out).group(1)
    assert hashlib.sha256(base64.b64decode(encoded)).digest() == hashlib.sha256(image.read_bytes()).digest()
    assert 'FINAL COMPLETO' in out
    saved_html = (tmp_path / "leccion_final.html").read_bytes()
    # Generar la copia autocontenida no infla el HTML guardado.
    assert saved_html == lightweight.encode("utf-8")
    image.unlink()
    with pytest.raises(RuntimeError):
        lesson_export.export_html(tmp_path)


def test_unclosed_legacy_source_link_does_not_consume_following_image():
    rendered = panel.a_html(
        '[Fuente](https://example.org/tema\n\n### Pregunta siguiente\n\n'
        '![Captura](img/consulta.jpg)\n\nExplicación conservada.')
    assert 'src="/img/consulta.jpg"' in rendered
    assert '<h3>Pregunta siguiente</h3>' in rendered
    assert 'Explicación conservada.' in rendered
    assert 'href="https://example.org/tema"' in rendered


def test_image_names_and_link_urls_are_not_normalized_as_algebra():
    rendered = panel.a_html(
        '![captura](img/consulta_1784827624093.jpg)\n\n'
        '[y_i docs](https://example.org/a_b?x=1)\n\nLa variable (y_i).')
    assert 'src="/img/consulta_1784827624093.jpg"' in rendered
    assert 'href="https://example.org/a_b?x=1"' in rendered
    assert '<math ' in rendered


def test_copilot_keeps_response_identity_and_does_not_scroll_to_end():
    start = panel.PAGINA.index('async function preguntar(')
    end = panel.PAGINA.index('async function descartarCapturaPrevia', start)
    js = panel.PAGINA[start:end]
    assert 'answer.innerHTML=' in js
    assert 'ch.lastElementChild.innerHTML=' not in js
    assert 'ch.scrollTop=ch.scrollHeight' not in js
    assert 'ch.scrollTop=question.offsetTop' in js
    assert 'Ampliar respuestas' in panel.PAGINA


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_copilot_output_budget_and_explicit_truncation(tmp_path, monkeypatch, finish):
    from src import copiloto

    calls = []
    monkeypatch.setattr(copiloto, "contexto_previo", lambda d: "")
    monkeypatch.setattr(copiloto, "notas_hoy", lambda d: "")
    monkeypatch.setattr(copiloto, "transcript_reciente", lambda d: "")

    def fake(messages, **kwargs):
        calls.append(kwargs)
        return {"texto": "Explicación conservada", "finish_reason": finish}

    monkeypatch.setattr(copiloto, "preguntar_detalle", fake)
    result = copiloto.responder(tmp_path, "Explica")
    assert result.startswith("Explicación conservada")
    assert ("puede estar incompleta" in result) == (finish == "length")
    assert calls == [{"max_tokens": 3200, "etiqueta": "copiloto", "thinking": False}]
