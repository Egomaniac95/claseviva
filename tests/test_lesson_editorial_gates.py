import hashlib
import wave

import pytest
from PIL import Image

from src import audio_manifest, config, estado, study_package
from src import post_procesar as post


@pytest.mark.parametrize("ocr,kind", [
    ("Calidad de datos y veracidad: una muestra hospitalaria", "calidad_datos"),
    ("La era de los Zettabytes: volumen de datos", "escala_datos"),
    ("Tabla de observaciones: muestra de población", "datos_generales"),
    ("Dataset de entrenamiento: features y labels para aprendizaje supervisado", "datos"),
    ("x_train y_train validation set", "datos"),
    ("Imagen MNIST: 28x28 píxeles y etiqueta correcta", "datos"),
    ("Variables del modelo lineal: y = 2*x + 1", "formula"),
    ("Agenda: tablas, muestras y observaciones", "agenda"),
    ("Comparación de conjuntos de datos: ventajas y diferencias", "comparacion"),
])
def test_guidance_requires_complementary_visual_evidence(ocr, kind):
    slide = {"ocr": ocr, "transcript_ventana": "Imagen con etiquetas para entrenar una red neuronal"}
    assert post._tipo_lectura_visual(slide, slide["transcript_ventana"]) == kind


@pytest.mark.parametrize("ocr", ["Calidad de datos: se muestra parte de la tabla",
                                     "Imagen: comparar datos de población", "matrix de información"])
def test_nearby_mnist_speech_does_not_invent_pixels_on_unrelated_slide(ocr):
    slide = {"ocr": ocr, "visual_role": "principal", "visual_tipo": "codigo_formula_tabla",
             "transcript_ventana": "La imagen tiene 28x28 píxeles y una etiqueta, un par x, y"}
    assert "matriz de píxeles" not in post._caption_entorno_practico(slide)


def test_genuine_progressive_notebook_does_not_invent_reading_instructions():
    slide = {"ocr": "MNIST image label", "visual_role": "secuencia",
             "visual_tipo": "codigo_formula_tabla"}
    assert "matriz de píxeles" in post._caption_entorno_practico(slide)
    guide = post._guia_lectura_visual(slide, "")
    assert len(guide) == 3
    assert guide[1:] == ("", "")


def test_noise_title_not_published_as_corroborated():
    assert not post._rotulo_visual_limpio("00000 aBCDX xYZQP ••")
    assert post._rotulo_visual_limpio("Necesidad o problema") == "Necesidad o problema"
    assert post._rotulo_visual_limpio("Descripción extensa " * 50) == ""


def test_uncertain_coverage_does_not_require_deleted_transcript():
    result = post._cobertura_estudiante_md({"cobertura_dudosa": True})
    assert "consulta el transcript" not in result


def test_guidance_does_not_truncate_original_prose():
    long_text = "Desarrollo explicado con detalle y ejemplos. " * 300
    slide = {"ocr": "Calidad de datos y veracidad", "visual_role": "principal"}
    original = dict(slide)
    post._guia_lectura_visual(slide, long_text)
    assert slide == original
    assert len(long_text) > 12000


def test_transcript_optimization_keeps_numeric_and_logical_corrections():
    sentences = [
        "Para este experimento se usa una tasa de aprendizaje de 0.01.",
        "Para este experimento se usa una tasa de aprendizaje de 0.02.",
        "La normalización se calcula utilizando todos los datos disponibles.",
        "La normalización no se calcula utilizando todos los datos disponibles.",
        "El parámetro A es la matriz que corresponde a la transformación.",
        "El parámetro a es la matriz que corresponde a la transformación.",
    ]
    segments = [{"t": i * 5, "fin": i * 5 + 4, "texto": text}
                for i, text in enumerate(sentences)]
    result = post.optimizar_transcript_llm(segments, {"origen": "video"})
    assert result["segmentos_enviados"] == len(sentences)
    assert result["segmentos_deduplicados"] == 0
    assert "0.01" in result["texto"] and "0.02" in result["texto"]


def test_transcript_optimization_still_collapses_identical_repetition():
    segments = [
        {"t": 1, "fin": 2, "texto": "um okay Hebb 1947 propuso una regla."},
        {"t": 3, "fin": 4, "texto": "okay Hebb 1947 propuso una regla."},
    ]
    result = post.optimizar_transcript_llm(segments, {"origen": "video"})
    assert result["segmentos_enviados"] == 1


def test_filler_cleanup_preserves_sql_operators_and_direction():
    text = "RIGHT JOIN conserva las filas de la tabla de la derecha; este es el criterio."
    assert post._texto_segmento_limpio(text) == text


def test_image_captions_and_metadata_cannot_pass_textual_coverage():
    segments = [{"texto": "Explicación de la clase con ejemplos y procedimientos. " * 400}]
    fake_lesson = (
        "## Desarrollo\n\nBreve panorama.\n\n"
        + '| <img src="img/slide.webp"> | ' + "Lectura guiada de la figura. " * 600 + " |\n"
    )
    assert post._leccion_demasiado_corta(fake_lesson, segments)
    assert post._reporte_cobertura_textual(fake_lesson, segments)["ratio"] < 0.12


def test_compression_alert_uses_35_percent_of_prose_without_source():
    segments = [{"texto": "La explicación tiene contenido único. " * 200}]
    assert post._leccion_demasiado_corta("A" * 2000, segments)
    assert not post._leccion_demasiado_corta("A" * 3000, segments)
    body, source = study_package.attach_source_transcript("A" * 2000, segments)
    assert post._leccion_demasiado_corta(study_package.metadata_comment({"source_transcript": source}) + body, segments)


def test_detail_retry_preserves_supported_code_and_runs_once(monkeypatch, tmp_path):
    monkeypatch.setenv("CLASE_POST_LLM", "1")
    segments = [{"texto": "El código ejecuta print(valor) y permite verificar la salida. " * 100}]
    requests = []
    improved = "## Demostración\n\n```python\nprint(valor)\n```\n\n" + "Se explica su salida. " * 120

    def ask(_d, messages, **kwargs):
        requests.append((messages, kwargs))
        return improved

    monkeypatch.setattr(post, "preguntar_cacheado", ask)
    result, calls = post._ampliar_guion_breve(tmp_path, "Síntesis breve.", segments,
                                           "TRANSCRIPCIÓN: print(valor)", "post-chunk-p1-001")
    assert result == improved.rstrip()
    assert len(requests) == calls == 1
    assert "fragmento sustentado" in requests[0][0][0]["content"]
    assert requests[0][1]["etiqueta"].endswith("-detalle")


def test_caption_keeps_specific_explanation_without_generic_formula_template():
    caption = "El callback actualizar_barras recibe el año y la selección para filtrar países."
    card = post._visual_card_md(
        {"ocr": "def actualizar_barras(ano, seleccion):", "visual_tipo": "codigo_formula_tabla"},
        archivo="callback.webp", caption=caption, caption_confiable=True)
    assert caption in card
    assert "La fórmula condensa" not in card
    assert "Localiza símbolos" not in card
    assert "Define cada símbolo" not in card


def test_summary_samples_late_tranches_under_single_development_heading():
    lesson = "## Desarrollo exhaustivo por tramos\n\n"
    for number in range(1, 8):
        lesson += f"### Tramo {number}\n\n" + f"Contenido del tramo {number}. " * 600
    lesson += "\n\n### Tramo 8\n\nLa función actualizar_barras recibe la selección y el año."
    sample = post._compactar_markdown_para_resumen(lesson, max_chars=4000)
    assert len(sample) <= 4000
    assert "Contenido del tramo 1" in sample
    assert "actualizar_barras recibe la selección y el año" in sample


def test_tasks_include_late_assignment_and_preserve_technical_tables(monkeypatch):
    monkeypatch.setenv("CLASE_POST_LLM", "1")
    sent = []

    def extract(messages, **kwargs):
        block = messages[-1]["content"]
        sent.append(block)
        return "- [ ] Entregar cuaderno.ipynb el viernes." if "cuaderno.ipynb" in block else "SIN_TAREAS"

    monkeypatch.setattr(post, "preguntar", extract)
    lesson = ("## Primer tema\n\n" + "Una explicación detallada.\n\n" * 2400
              + "\n## Encargo final\n\n| Archivo | Fecha |\n| cuaderno.ipynb | viernes |\n")
    result = post._consultar_tareas(lesson)
    assert result == "- [ ] Entregar cuaderno.ipynb el viernes."
    assert len(sent) > 1
    assert all(len(block) <= 30000 for block in sent)
    assert "| cuaderno.ipynb | viernes |" in sent[-1]


@pytest.mark.parametrize("light", [False, True])
def test_final_publication_keeps_source_tasks_and_warns_before_cleanup(monkeypatch, tmp_path, light):
    monkeypatch.setattr(config, "CONTENIDO_DIR", tmp_path / "contenido")
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setenv("CLASE_POST_LLM", "1")
    d = estado.crear_clase("Lección detallada", curso="curso")
    state = estado.leer_estado(d)
    state.update(estado="fin", inicio=1000.0, parte=1)
    estado.escribir_estado(d, state)
    (d / "marcas.jsonl").write_text("", encoding="utf-8")
    estado.anexar_jsonl(d / "marcas.jsonl", {"t": 1000., "tipo": "inicio", "parte": 1})
    estado.anexar_jsonl(d / "marcas.jsonl", {"t": 1120., "tipo": "fin", "parte": 1})
    rows = [{"t": 1010., "fin": 1015., "parte": 1,
             "chunk": "chunk_000000001010_p1_v10.wav",
             "texto": "La tasa se corrige de 0.01 a 0.02. Entregar cuaderno.ipynb el viernes."}]
    estado.anexar_jsonl(d / "transcript.jsonl", rows[0])
    wav = d / "audio" / rows[0]["chunk"]
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(100)
        handle.writeframes(b"\x10\x10" * 500)
    digest = hashlib.sha256(wav.read_bytes()).hexdigest()
    audio_manifest.append_event(d, {
        "chunk_id": wav.stem, "path": wav.name, "status": "sealed",
        "sequence": 1, "sha256": digest, "duration": 5.,
        "wall_start": 1010., "wall_end": 1015.,
        "transactional_stt_required": True,
    })
    audio_manifest.commit_transcript_chunk(d, wav.stem, rows, {
        "path": wav.name, "input_sha256": digest})
    Image.new("RGB", (20, 20), "white").save(d / "img" / "keep.webp")

    def part(class_dir, number, *_args, **_kwargs):
        target = class_dir / f"leccion_parte{number}.md"
        target.write_text(
            '## Tasa de aprendizaje\n\nSe corrige de 0.01 a 0.02.\n\n'
            '<img src="img/keep.webp">\n\nEntregar cuaderno.ipynb el viernes.\n',
            encoding="utf-8")
        part.ultimo_reporte = {"parte": number, "chunks": 1, "guiones": 1}
        return target

    monkeypatch.setattr(post, "consolidar_parte", part)
    monkeypatch.setattr(post, "fuentes_ampliadas_consultas", lambda *_: "")
    monkeypatch.setattr(post, "continuidad_audio_contextual", lambda *_: {
        "status": "warning", "terminal_capture_failures": 4, "wall_gap_max_seconds": 51.939})
    monkeypatch.setattr(post, "preguntar", lambda _messages, **kwargs:
                        "- [ ] Entregar cuaderno.ipynb el viernes." if kwargs["etiqueta"] == "post-tareas"
                        else "La clase desarrolla la tasa de aprendizaje.")
    post.consolidar_todo(d, forzar=True, limpiar_al_final=light, paquete_ligero=light)
    final = (d / "leccion_final.md").read_text(encoding="utf-8")
    page = (d / "leccion_final.html").read_text(encoding="utf-8")
    metadata = study_package.parse_metadata(final)
    assert metadata["source_transcript"]["count"] == 1
    assert study_package._source_segments(metadata["source_transcript"]) == rows
    assert study_package.validate_candidate(d)["ok"]
    assert "Tareas detectadas en esta clase" in final and "Tareas detectadas en esta clase" in page
    assert "4 interrupciones" in page and "51.9 segundos" in page
    if light:
        assert sorted(p.name for p in d.iterdir()) == ["img", "leccion_final.html", "leccion_final.md"]
    else:
        assert (d / "transcript.jsonl").exists()


def test_english_generation_only_sends_study_prose(monkeypatch, tmp_path):
    body, source = study_package.attach_source_transcript(
        "# Clase\n\nDesarrollo técnico.", [{"texto": "REGISTRO_ORIGINAL_" * 5000}])
    final = study_package.metadata_comment({"source_transcript": source}) + "\n\n" + body
    sent = []

    def translate(messages, **_kwargs):
        sent.append(messages[-1]["content"])
        return "## Technical development\n\nDetailed explanation."

    monkeypatch.setattr(post, "preguntar", translate)
    post.generar_leccion_ingles(tmp_path, final, {"nombre": "Class"})
    assert sent == ["# Clase\n\nDesarrollo técnico."]


def test_legacy_markdown_cleanup_preserves_literal_source_markdown(tmp_path):
    body, source = study_package.attach_source_transcript(
        "# Clase\n\nDesarrollo.", [{"texto": "Escribe ![ejemplo](img/ejemplo.webp)."}])
    final = study_package.metadata_comment({"source_transcript": source}) + "\n\n" + body
    path = tmp_path / "leccion_final.md"
    path.write_text(final, encoding="utf-8")
    post._sanear_markdown_final(tmp_path)
    cleaned = path.read_text(encoding="utf-8")
    assert study_package.validate_source_transcript(cleaned, study_package.parse_metadata(cleaned)) == []


def test_merge_preserves_case_and_indentation_changes_in_code():
    blocks = ["```python\nA = 1\n```", "```python\na = 1\n```",
              "```python\nif A:\n    print(A)\n```", "```python\nif A:\n  print(A)\n```"]
    merged = post._fusionar_contenidos_sin_perdida(blocks + blocks[:1])
    assert all(block in merged for block in blocks)
    assert merged.count("A = 1") == 1


@pytest.mark.parametrize("fence", ["```", "~~~~", "````"])
def test_editorial_transforms_preserve_fenced_comments_and_whitespace(fence):
    code = (f"{fence}python\n# comentario\n## Panorama de código\n"
            "### Tramo 99\n# Chunk 2.001\n\n\nA = 1  \n"
            f"{fence}\n")
    original = "# Portada\n\n## Desarrollo\n\n" + code + "\n## Cierre\n\nExplicación."
    _, sections = post._extraer_secciones_mayores(original)
    assert [s["titulo"] for s in sections] == ["Portada", "Desarrollo", "Cierre"]
    assert code.rstrip("\n") in sections[1]["cuerpo"]
    for transform in (post._sanear_panorama_parte, post._sanear_contenido_llm_publico,
                      post._subir_nivel_encabezados, post._adornar_encabezados_pedagogicos,
                      post._renumerar_tramos_integrados):
        assert code in transform(original)
    _, _, cleaned = post._guion_sin_metadatos("# Chunk 1.001\n\n" + original)
    assert code in cleaned


def test_open_fence_does_not_create_sections_from_code_comments():
    source = "## Código\n\n```python\n# comentario\n## También comentario\nvalor = 1"
    _, sections = post._extraer_secciones_mayores(source)
    assert len(sections) == 1
    assert "## También comentario" in sections[0]["cuerpo"]


def test_integrating_parts_keeps_code_comments_and_blank_lines():
    code = "```python\n# Parte 99\n## Panorama temático de la lección\n\n\nA = 1  \n```"
    source = "# Parte 1\n\n## Código\n\n" + code + "\n\n# Parte 2\n\n## Código\n\n```python\na = 1\n```"
    integrated = post._integrar_partes_por_secciones(source)
    assert code in integrated
    assert "```python\na = 1\n```" in integrated
    assert "CLASEVIVACODE" not in integrated


def test_literal_image_examples_in_code_survive_visual_transforms(tmp_path):
    code = "```markdown\n![ejemplo](img/example.webp)\n![ejemplo](img/example.webp)\n[[IMG:example.webp|Ejemplo de sintaxis]]\n```"
    assert post.entretejer(code, tmp_path, [], "vivo").rstrip("\n") == code
    deduped, report = post.deduplicar_referencias_img(code)
    assert deduped == code
    assert report["duplicados_eliminados"] == 0
