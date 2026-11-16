import json

import pytest
from PIL import Image

from src import lesson_export, study_package


def _publish(d, segments, *, previous=""):
    (d / "img").mkdir(exist_ok=True)
    Image.new("RGB", (12, 12), "white").save(d / "img" / "keep.webp")
    body, source = study_package.attach_source_transcript(
        '# Clase\n\nDesarrollo.\n\n<img src="img/keep.webp">', segments,
        previous_markdown=previous)
    metadata = {
        "recording_parts": [{"part": 1, "useful_start_sec": 0, "useful_end_sec": 60}],
        "images": study_package.image_inventory(d, body),
        "source_transcript": source,
    }
    text = study_package.metadata_comment(metadata) + "\n\n" + body
    (d / "leccion_final.md").write_text(text, encoding="utf-8")
    return text


def test_complete_source_survives_compaction_with_repetitions_and_html(tmp_path):
    rows = [{"texto": 'No usar x < 5.\nUsar x < 50. <!-- dato --> '
             '[[IMG:ejemplo.webp]] ![ejemplo](img/ejemplo.webp)', "parte": 1, "t": 4}]
    rows *= 2
    original = _publish(tmp_path, rows)
    (tmp_path / "transcript.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    study_package.finalize_lightweight_package(tmp_path, evidence_safe=True)

    final = (tmp_path / "leccion_final.md").read_text(encoding="utf-8")
    assert final == original
    metadata = study_package.parse_metadata(final)
    assert metadata["source_transcript"]["count"] == 2
    assert study_package._source_segments(metadata["source_transcript"]) == rows
    assert study_package.validate_candidate(tmp_path)["ok"]
    assert "No usar x &lt; 5." in (tmp_path / "leccion_final.html").read_text(encoding="utf-8")
    assert not (tmp_path / "transcript.jsonl").exists()


def test_continuation_retains_previous_source_and_only_subtracts_prior_occurrences(tmp_path):
    first = {"texto": "valor 50", "parte": 1, "t": 10}
    second = {"texto": "valor 500", "parte": 2, "t": 20}
    previous = _publish(tmp_path, [first])
    body, payload = study_package.attach_source_transcript(
        "# Clase actualizada", [first, first, second], previous_markdown=previous)

    assert study_package._source_segments(payload) == [first, first, second]
    assert not payload["prior_source_missing"]
    assert body.count("CLASEVIVA_SOURCE_TRANSCRIPT_START") == 1
    assert study_package.strip_source_transcript(body) == "# Clase actualizada"


def test_legacy_continuation_discloses_missing_original_source(tmp_path):
    _, payload = study_package.attach_source_transcript(
        "# Clase", [{"texto": "nuevo"}], previous_markdown="# Lección histórica")
    assert payload["prior_source_missing"]


def test_modified_source_appendix_prevents_compaction(tmp_path):
    original = _publish(tmp_path, [{"texto": "Debe decir 500"}])
    (tmp_path / "leccion_final.md").write_text(
        original.replace("Debe decir 500", "Debe decir 50"), encoding="utf-8")
    (tmp_path / "transcript.jsonl").write_text('{"texto":"Debe decir 500"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="apéndice"):
        study_package.finalize_lightweight_package(tmp_path, evidence_safe=True)
    assert (tmp_path / "transcript.jsonl").is_file()


def test_new_segment_after_publication_prevents_compaction(tmp_path):
    _publish(tmp_path, [{"texto": "Primero"}])
    (tmp_path / "transcript.jsonl").write_text('{"texto":"Segundo"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="segmentos de transcripción sin conservar"):
        study_package.finalize_lightweight_package(tmp_path, evidence_safe=True)
    assert (tmp_path / "transcript.jsonl").is_file()


def test_export_keeps_source_code_math_and_recognized_timestamps_literal(tmp_path):
    literal = 'x < 2; $x < 2$; **literal**; <script>alert("a")</script>\n' + r'\frac{1}{2}'
    rows = [{"texto": literal, "parte": 2, "t": 1_700_000_000},
            {"texto": "Segundo segmento", "parte": 2, "t": 15}]
    previous = _publish(tmp_path, rows)
    text = _publish(tmp_path, [{"texto": "Tercero", "parte": 3, "t": 31}], previous=previous)
    page = lesson_export.render_html(tmp_path, self_contained=True)
    source_html = page.split('<summary>Registro completo de lo transcrito</summary>', 1)[1].split('</details>', 1)[0]

    assert '2023-11-14 22:13:20 UTC' in source_html
    assert 'Tiempo 00:00:15' in source_html
    assert 'Tiempo 00:00:31' in source_html
    assert '$x &lt; 2$' in source_html
    assert '**literal**' in source_html
    assert '&lt;script&gt;alert(' in source_html and '<script>' not in source_html
    assert r'\frac{1}{2}' in source_html
    assert 'CLASEVIVA_SOURCE_TRANSCRIPT' not in page
    assert 'CLASEVIVASOURCE' not in page
    assert study_package._source_segments(
        study_package.parse_metadata(text)["source_transcript"]) == [
            *rows, {"texto": "Tercero", "parte": 3, "t": 31}]
