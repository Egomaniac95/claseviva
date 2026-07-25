import json
from pathlib import Path

import pytest
from PIL import Image

from src import ocr, post_procesar


def _image(path: Path, shade: int = 220) -> None:
    Image.new("RGB", (96, 54), (shade, shade, shade)).save(path)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_ocr_diferido_se_cachea_por_hash_sin_tocar_la_imagen(
        monkeypatch, tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    image_path = image_dir / "slide.jpg"
    _image(image_path, 120)
    original = image_path.read_bytes()
    calls = []

    def fake_ocr(path):
        calls.append(path.read_bytes())
        return f"texto recuperado {len(calls)}"

    monkeypatch.setattr(post_procesar, "_run_ocr_image", fake_ocr)
    post_procesar._OCR_CACHE_BY_CLASS.clear()
    capture = {"archivo": image_path.name}

    assert post_procesar._ocr_completo_para_captura(
        tmp_path, capture) == "texto recuperado 1"
    assert post_procesar._ocr_completo_para_captura(
        tmp_path, capture) == "texto recuperado 1"
    post_procesar._OCR_CACHE_BY_CLASS.clear()
    assert post_procesar._ocr_completo_para_captura(
        tmp_path, capture) == "texto recuperado 1"
    assert len(calls) == 1
    assert image_path.read_bytes() == original

    _image(image_path, 121)
    assert post_procesar._ocr_completo_para_captura(
        tmp_path, capture) == "texto recuperado 2"
    assert len(calls) == 2
    cache_rows = post_procesar.est.leer_jsonl(
        tmp_path / "_debug" / "ocr_cache.jsonl")
    assert len(cache_rows) == 2
    assert len({row["sha256"] for row in cache_rows}) == 2


def test_video_ranges_include_zero_and_own_later_boundaries_once(tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    for name, shade in (("zero.jpg", 20), ("boundary.jpg", 120), ("later.jpg", 220)):
        _image(image_dir / name, shade)
    _write_jsonl(tmp_path / "img_log.jsonl", [
        {"t": 0, "archivo": "zero.jpg"},
        {"t": 10, "archivo": "boundary.jpg"},
        {"t": 15, "archivo": "later.jpg"},
    ])

    first = post_procesar.capturas_rango(tmp_path, 0, 10, {"origen": "video"})
    second = post_procesar.capturas_rango(tmp_path, 10, 20, {"origen": "video"})

    assert [row["archivo"] for row in first] == ["zero.jpg", "boundary.jpg"]
    assert [row["archivo"] for row in second] == ["later.jpg"]
    assert post_procesar._tiempo_en_rango(0, 0, 10) is True
    assert post_procesar._tiempo_en_rango(10, 10, 20) is False


def test_timeline_mixto_no_aplica_esquema_nuevo_posterior_al_rango(
        monkeypatch, tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    for name, shade in (("a.jpg", 30), ("b.jpg", 140), ("future.jpg", 240)):
        _image(image_dir / name, shade)
    _write_jsonl(tmp_path / "img_log.jsonl", [
        {"t": 0, "archivo": "a.jpg", "dhash": "0000000000000000", "ocr": "Alpha"},
        {"t": 20, "archivo": "b.jpg", "dhash": "ffffffffffffffff", "ocr": "Beta"},
        {"t": 100, "archivo": "future.jpg", "dhash": "aaaaaaaaaaaaaaaa",
         "ocr": "Future", "evento": "captura"},
    ])
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 50, [], {"origen": "video"})

    assert [slide["archivo"] for slide in slides] == ["a.jpg", "b.jpg"]
    assert [slide["duracion_visual_seg"] for slide in slides] == [20.0, 30.0]
    assert post_procesar.construir_timeline_slides.ultimo_reporte[
        "cierres_conservadores_sin_fin"] == 0


def test_timeline_compara_ocr_completo_despues_del_caracter_700(
        monkeypatch, tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    _image(image_dir / "formula_1.jpg", 180)
    _image(image_dir / "formula_2.jpg", 181)
    prefix = "contenido compartido " * 45
    assert len(prefix) > post_procesar.OCR_MAX_CHARS
    _write_jsonl(tmp_path / "img_log.jsonl", [
        {"t": 0, "archivo": "formula_1.jpg", "dhash": "abababababababab",
         "ocr": prefix + " x = 1"},
        {"t": 20, "archivo": "formula_2.jpg", "dhash": "abababababababab",
         "ocr": prefix + " x = 2"},
    ])
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 30, [], {"origen": "video"})

    assert len(slides) == 2
    assert len(slides[0]["ocr"]) > post_procesar.OCR_MAX_CHARS
    assert "ocr_cambio_importante" in slides[1]["motivo"]


def test_ingesta_video_conserva_colision_dhash_si_cambia_ocr(monkeypatch):
    monkeypatch.setattr(post_procesar.imagen, "dhash", lambda _img, tam=8: 123 + tam)
    evidencias = []
    image = Image.new("RGB", (32, 20), "white")

    first = post_procesar.evaluar_evidencia_frame_video(
        image, evidencias, texto_ocr_fn=lambda _img: "loss = 1",
        cambio_evidencia_fn=ocr.cambio_evidencia)
    first["archivo"] = "first.jpg"
    evidencias.append(first)
    changed = post_procesar.evaluar_evidencia_frame_video(
        image, evidencias, texto_ocr_fn=lambda _img: "loss = 2",
        cambio_evidencia_fn=ocr.cambio_evidencia)
    changed["archivo"] = "changed.jpg"
    evidencias.append(changed)
    duplicate = post_procesar.evaluar_evidencia_frame_video(
        image, evidencias, texto_ocr_fn=lambda _img: "loss = 2",
        cambio_evidencia_fn=ocr.cambio_evidencia)

    assert first["guardar"] is True
    assert changed["guardar"] is True
    assert changed["motivo"] == "ocr_cambio_evidencia"
    assert duplicate["guardar"] is False
    assert duplicate["ref_archivo"] == "changed.jpg"


def test_ingesta_video_dhash16_separa_diagramas_sin_ocr_y_reaparicion_da_heartbeat(
        monkeypatch):
    secondary = iter((0, (1 << 256) - 1, 0))

    def fake_dhash(_img, tam=8):
        return 1 if tam == 8 else next(secondary)

    monkeypatch.setattr(post_procesar.imagen, "dhash", fake_dhash)
    image = Image.new("RGB", (32, 20), "white")
    evidencias = []
    first = post_procesar.evaluar_evidencia_frame_video(
        image, evidencias, texto_ocr_fn=lambda _img: "",
        cambio_evidencia_fn=ocr.cambio_evidencia)
    first["archivo"] = "diagram_a.jpg"
    evidencias.append(first)
    second = post_procesar.evaluar_evidencia_frame_video(
        image, evidencias, texto_ocr_fn=lambda _img: "",
        cambio_evidencia_fn=ocr.cambio_evidencia)
    second["archivo"] = "diagram_b.jpg"
    evidencias.append(second)
    revisit = post_procesar.evaluar_evidencia_frame_video(
        image, evidencias, texto_ocr_fn=lambda _img: "",
        cambio_evidencia_fn=ocr.cambio_evidencia)
    event = post_procesar.evento_timeline_frame_video(revisit, 1200.0)

    assert first["guardar"] is True
    assert second["guardar"] is True
    assert revisit["guardar"] is False
    assert revisit["ref_archivo"] == "diagram_a.jpg"
    assert event == {
        "t": 1200.0,
        "archivo": "diagram_a.jpg",
        "ref_archivo": "diagram_a.jpg",
        "parte": 1,
        "dhash": revisit["dhash"],
        "dhash16": revisit["dhash16"],
        "evento": "heartbeat",
        "motivo": "video_frame_referido",
    }


def test_ingesta_video_solo_fuerza_es_o_en():
    assert post_procesar.idioma_whisper_ingesta_video("auto") is None
    assert post_procesar.idioma_whisper_ingesta_video("") is None
    assert post_procesar.idioma_whisper_ingesta_video("es") == "es"
    assert post_procesar.idioma_whisper_ingesta_video("EN") == "en"


def test_leccion_extra_inserta_slides_y_referencias_protegen_extra_y_snapshot(
        monkeypatch, tmp_path):
    class_dir = tmp_path / "contenido" / "curso" / "clases" / "clase"
    image_dir = class_dir / "img"
    image_dir.mkdir(parents=True)
    _image(image_dir / "extra.jpg")
    slide = {
        "archivo": "extra.jpg", "t_rel": 20.0, "ocr": "Formula z = Wx",
        "transcript_ventana": "Explicacion completa de la formula.",
        "protegida_estudio": True,
    }
    monkeypatch.setattr(
        post_procesar, "construir_timeline_slides", lambda *args, **kwargs: [slide])
    monkeypatch.setattr(
        post_procesar, "seleccionar_slides_por_tema",
        lambda slides, *args, **kwargs: slides)
    monkeypatch.setattr(
        post_procesar, "preguntar_cacheado",
        lambda *args, **kwargs: "## Explicacion\nEl modelo omitio el marcador.")
    segmento = {
        "clasificacion": "otra_clase_legitima", "inicio": 0.0, "fin": 200.0,
        "duracion_seg": 200.0, "razones": ["cambio_vocabulario"],
        "texto": "otra clase con material suficiente " * 40,
        "segs": [{"t": 0.0, "fin": 190.0, "texto": "Explicacion de otra clase."}],
    }

    generated = post_procesar.generar_lecciones_extra_detectadas(
        class_dir, [segmento], {"origen": "video"})
    snapshot = class_dir / "leccion_final_PREV_20260710_010203.md"
    snapshot.write_text("![snapshot](img/snapshot.jpg)\n", encoding="utf-8")
    refs = post_procesar._referencias_img(class_dir)

    assert len(generated) == 1
    assert "img/extra.jpg" in generated[0].read_text(encoding="utf-8")
    assert {"extra.jpg", "snapshot.jpg"} <= refs


def test_merge_reubica_visuales_omitidos_en_tema_sin_titulos_de_archivo(
        tmp_path):
    class_dir = tmp_path / "contenido" / "curso" / "clases" / "clase"
    image_dir = class_dir / "img"
    image_dir.mkdir(parents=True)
    for name in ("loss.jpg", "gradient.jpg", "panel.jpg"):
        _image(image_dir / name)
    cuerpo = (
        "## Función de pérdida\n\nLa pérdida cuantifica el error del modelo.\n\n"
        "## Descenso de gradiente\n\nEl gradiente indica la dirección de cambio.\n"
    )
    merge_input = (
        "## Función de pérdida\n\nLa MSE compara predicción y objetivo.\n\n"
        "| <img src=\"img/loss.jpg\" width=\"420\"> | "
        "Contexto registrado del profesor: ejemplo. OCR de apoyo: Mean Squared Error |\n\n"
        "## Descenso de gradiente\n\nSe actualizan los pesos en dirección opuesta.\n\n"
        "| <img src=\"img/gradient.jpg\" width=\"420\"> | "
        "Gradient descent sobre la superficie de pérdida |\n\n"
        "## Panel\n\n| <img src=\"img/panel.jpg\" width=\"420\"> | "
        "ClaseViva localhost controles de clase |\n"
    )

    integrado, reporte = post_procesar._integrar_visuales_omitidos_en_leccion(
        cuerpo, merge_input, {"loss.jpg", "gradient.jpg", "panel.jpg"},
        parte=1, d=class_dir)

    assert integrado.count("img/loss.jpg") == 1
    assert integrado.count("img/gradient.jpg") == 1
    assert "img/panel.jpg" not in integrado
    assert "### Slide conservada:" not in integrado
    assert "Apendice visual de estudio" not in integrado
    assert "Contexto registrado del profesor" not in integrado
    assert integrado.index("img/loss.jpg") < integrado.index("## Descenso de gradiente")
    assert reporte["integradas"] == 2
    assert reporte["solo_debug"] == 1


def test_integracion_visual_no_impone_tope_a_evidencia_distinta(tmp_path):
    class_dir = tmp_path / "contenido" / "curso" / "clases" / "clase"
    image_dir = class_dir / "img"
    image_dir.mkdir(parents=True)
    names = [f"concepto_{idx:02d}.jpg" for idx in range(24)]
    for name in names:
        _image(image_dir / name)
    merge_input = "## Arquitecturas\n\n" + "\n\n".join(
        f'| <img src="img/{name}" width="420"> | Concepto distinto {idx}: '
        f'componente y relación pedagógica {idx} |'
        for idx, name in enumerate(names)
    )

    integrado, reporte = post_procesar._integrar_visuales_omitidos_en_leccion(
        "## Arquitecturas\n\nDescripción del tema.\n",
        merge_input, set(names), parte=1, d=class_dir)

    assert reporte["integradas"] == len(names)
    assert all(integrado.count(f"img/{name}") == 1 for name in names)


def test_cache_llm_esta_activa_por_defecto_y_admite_opt_out(
        monkeypatch, tmp_path):
    calls = []

    def fake_ask(_messages, max_tokens=0, etiqueta=""):
        calls.append((max_tokens, etiqueta))
        return "respuesta estable"

    monkeypatch.setattr(post_procesar, "preguntar", fake_ask)
    monkeypatch.delenv("CLASE_POST_CACHE", raising=False)
    messages = [{"role": "user", "content": "prompt de cache por defecto"}]
    first = post_procesar.preguntar_cacheado(
        tmp_path, messages, 100, "default-1", "cache-default-v1")
    second = post_procesar.preguntar_cacheado(
        tmp_path, messages, 100, "default-2", "cache-default-v1")

    assert first == second == "respuesta estable"
    assert len(calls) == 1

    monkeypatch.setenv("CLASE_POST_CACHE", "0")
    uncached_messages = [{"role": "user", "content": "prompt sin cache"}]
    post_procesar.preguntar_cacheado(
        tmp_path, uncached_messages, 100, "off-1", "cache-off-v1")
    post_procesar.preguntar_cacheado(
        tmp_path, uncached_messages, 100, "off-2", "cache-off-v1")
    assert len(calls) == 3


def test_continuacion_integra_cuerpo_previo_completo_localmente():
    previous = (
        "# Clase anterior\n\n"
        "> Fecha: 2026-07-15 | Idioma detectado: es\n\n"
        "## 🧭 Resumen ejecutivo\n\nResumen que debe sustituirse al actualizar.\n\n"
        "---\n\n"
        "# Parte 1\n\n## Concepto antiguo\n\nDetalle unico al final.\n"
        "![evidencia](img/old.jpg)\n"
    )
    context = {
        "texto": "resumen compacto",
        "texto_completo": previous,
        "disponible": True,
    }

    integrated = post_procesar._integrar_leccion_previa_local(
        context,
        "## Material nuevo\n\nContenido nuevo.\n"
        "![evidencia repetida](img/old.jpg)",
    )

    assert "Detalle unico al final." in integrated
    assert "img/old.jpg" in integrated
    assert integrated.count("img/old.jpg") == 1
    assert "## Material nuevo" in integrated
    assert integrated.count("# Clase anterior") == 0
    assert "Resumen que debe sustituirse" not in integrated
    assert "Leccion previa preservada integramente" not in integrated
    assert integrated.index("# Parte 1") < integrated.index("## Material nuevo")


def test_continuacion_no_rescata_partes_antiguas_desde_leccion_en_vivo(
        tmp_path):
    (tmp_path / "leccion_en_vivo.md").write_text(
        "### Contexto local · Parte 1\n\nResumen visual antiguo.\n",
        encoding="utf-8",
    )
    nuevos_parte_2 = [{
        "t_rel": 120.0,
        "fin_rel": 125.0,
        "parte": 2,
        "texto": "Contenido nuevo de la segunda parte.",
    }]

    result = post_procesar.consolidar_parte(
        tmp_path,
        parte=1,
        t_ini=0.0,
        t_fin=100.0,
        origen={"origen": "video"},
        segs_override=nuevos_parte_2,
        permitir_rescate_en_vivo=False,
    )

    assert result is None
    assert not (tmp_path / "leccion_parte1.md").exists()


def test_continuacion_identifica_solo_parte_nueva_con_forzar():
    origen = {
        "origen": "vivo",
        "inicio": 1000.0,
        "intervalos_utiles": [[1000.0, 1100.0], [10000.0, 10100.0]],
    }
    rangos = [(1, 1000.0, 1100.0), (2, 10000.0, 10100.0)]
    evidencia_nueva = [
        {"t": 10020.0, "parte": 2, "texto": "Nuevo contenido."},
        {"t": 10030.0, "parte": 2, "archivo": "nueva.jpg"},
    ]

    partes = post_procesar._partes_con_evidencia(
        evidencia_nueva, rangos, origen)

    assert partes == {2}
    assert 1 not in partes


def test_panorama_de_parte_no_publica_portada_ni_resumen_anidados():
    raw = (
        "# Clase completa\n\n"
        "> Fecha: 2026-07-15 | Idioma: es\n\n"
        "## 🧭 Resumen ejecutivo\n\nResumen global repetido.\n\n---\n\n"
        "## Entrenamiento nuevo\n\nSe explica el descenso por gradiente."
    )

    clean = post_procesar._sanear_panorama_parte(raw)

    assert "# Clase completa" not in clean
    assert "Resumen ejecutivo" not in clean
    assert "Resumen global repetido" not in clean
    assert clean.startswith("## Entrenamiento nuevo")
    assert "descenso por gradiente" in clean


def test_merge_de_parte_exige_solo_guiones_nuevos():
    prompt = post_procesar.PROMPT_MERGE_LECCION

    assert "EXCLUSIVAMENTE los guiones nuevos" in prompt
    assert "no emitas ningun encabezado `#`" in prompt
    assert "Resumen ejecutivo" in prompt


def test_resumen_de_continuacion_debe_representar_toda_la_clase():
    prompt = post_procesar.PROMPT_RESUMEN

    assert "TODA la clase" in prompt
    assert "No uses 'primera parte', 'segunda parte'" in prompt
    assert "max. 500 palabras" in prompt


def test_saneador_corrige_comentario_totensor_autocontradictorio():
    raw = (
        "```python\n"
        "transforms.ToTensor(),  # PIL -> Tensor, [0,255] -> [0,0]\n"
        "```\n"
    )

    clean = post_procesar._sanear_contenido_llm_publico(raw)

    assert "[0,255] -> [0,1]" in clean
    assert "[0,0]" not in clean


def test_rangos_publicados_usan_tiempo_util_en_todas_las_partes():
    origen = {
        "origen": "vivo",
        "inicio": 1000.0,
        "intervalos_utiles": [[1000.0, 1100.0], [10000.0, 10100.0]],
    }
    markdown = (
        "# Parte 1\n\n> Rango: 00:00 -> 01:40\n\nTexto uno.\n\n"
        "# Parte 2\n\n> Rango: 01:40 -> 2:30:00\n\nTexto dos."
    )

    clean = post_procesar._normalizar_rangos_publicados(
        markdown,
        [(1, 1000.0, 1100.0), (2, 10000.0, 10100.0)],
        origen,
    )

    assert "> Rango: 00:00 -> 01:40" in clean
    assert "> Rango: 01:40 -> 03:20" in clean
    assert "2:30:00" not in clean


def test_continuaciones_se_integran_en_secciones_estables_sin_capitulos_por_parte():
    markdown = (
        "# Parte 1\n\n"
        "> Rango: 00:00 -> 01:40:00\n\n"
        "# Panorama temático de la lección\n\n"
        "## 🧠 Fundamentos\n\nEl perceptrón abre la explicación.\n\n"
        "## 🎯 Guía de estudio\n\n"
        "### Conceptos que el alumno debe dominar\n\n- Perceptrón\n\n"
        "### Preguntas probables de examen\n\n1. ¿Qué limita al perceptrón?\n\n"
        "## 🧮 Glosario bilingüe y fórmulas\n\n"
        "- **Perceptron:** clasificador lineal.\n\n"
        "## 📚 Desarrollo exhaustivo por tramos\n\n"
        "### 🧩 Tramo 1 · 00:00 – 01:40:00\n\nDesarrollo inicial.\n\n"
        "## 🖼️ Galería visual complementaria\n\n"
        "<details><summary>Abrir galería</summary>\n\n"
        "<small>01:20:00 · Parte 1 · Evidencia.</small>\n\n</details>\n\n"
        "# Parte 2\n\n"
        "> Rango: 01:40:00 -> 03:03:48\n\n"
        "## 📐 Tensores y normalización\n\nLa continuación desarrolla tensores.\n\n"
        "## 🎯 Guía de estudio\n\n"
        "### Conceptos que debes dominar\n\n- Tensor\n\n"
        "### Preguntas probables de examen\n\n1. ¿Qué hace ToTensor?\n\n"
        "## 🧮 Glosario bilingüe y fórmulas\n\n"
        "- **Tensor:** arreglo n-dimensional.\n\n"
        "## 📚 Desarrollo exhaustivo por tramos\n\n"
        "### 🧩 Tramo 1 · 01:40:00 – 03:03:48\n\nDesarrollo nuevo.\n\n"
        "## 🖼️ Galería visual complementaria\n\n"
        "<details><summary>Abrir galería</summary>\n\n"
        "<small>02:10:00 · Parte 2 · Evidencia.</small>\n\n</details>\n"
    )

    integrated = post_procesar._integrar_partes_por_secciones(markdown)

    assert "# Parte 1" not in integrated
    assert "# Parte 2" not in integrated
    assert integrated.count("# Panorama temático de la lección") == 1
    assert integrated.count("## 🎯 Guía de estudio") == 1
    assert integrated.count("## 🧮 Glosario bilingüe y fórmulas") == 1
    assert integrated.count("## 📚 Desarrollo exhaustivo por tramos") == 1
    assert integrated.count("## 🖼️ Galería visual complementaria") == 1
    assert "00:00 -> 03:03:48" in integrated
    assert "El perceptrón abre la explicación." in integrated
    assert "La continuación desarrolla tensores." in integrated
    assert integrated.index("Desarrollo inicial.") < integrated.index("Desarrollo nuevo.")
    assert "- Perceptrón" in integrated
    assert "- Tensor" in integrated
    assert integrated.count("### 🧩 Tramo 1 ·") == 1
    assert integrated.count("### 🧩 Tramo 2 ·") == 1
    assert "1. ¿Qué limita al perceptrón?" in integrated
    assert "2. ¿Qué hace ToTensor?" in integrated
    assert "· Parte 1 ·" not in integrated
    assert "· Parte 2 ·" not in integrated


def test_integracion_tematica_admite_leccion_ya_unificada_mas_otra_continuacion():
    markdown = (
        "# Panorama temático de la lección\n\n"
        "> Cobertura útil integrada: 00:00 -> 01:40:00.\n\n"
        "## Fundamentos\n\nContenido estable.\n\n"
        "## 📚 Desarrollo exhaustivo por tramos\n\n"
        "### Tramo 1\n\nDetalle estable.\n\n"
        "# Parte 3\n\n"
        "> Rango: 03:03:48 -> 03:30:00\n\n"
        "## Optimización\n\nContenido de continuación.\n\n"
        "## 📚 Desarrollo exhaustivo por tramos\n\n"
        "### Tramo 3\n\nDetalle nuevo.\n"
    )

    integrated = post_procesar._integrar_partes_por_secciones(markdown)

    assert integrated.count("# Panorama temático de la lección") == 1
    assert integrated.count("## 📚 Desarrollo exhaustivo por tramos") == 1
    assert "Contenido estable." in integrated
    assert "Contenido de continuación." in integrated
    assert integrated.index("Detalle estable.") < integrated.index("Detalle nuevo.")
    assert "00:00 -> 03:30:00" in integrated
    assert post_procesar._integrar_partes_por_secciones(integrated) == integrated


def test_tarjeta_visual_descompone_lectura_guiada_sin_anidarla(tmp_path):
    slide = {
        "archivo": "modelo.jpg",
        "t_rel": 20.0,
        "parte": 2,
        "visual_role": "principal",
        "visual_tipo": "codigo_formula_tabla",
        "visual_score": 9,
    }
    caption = (
        "🧭 Lectura guiada — idea central: La capa lineal transforma 784 entradas "
        "en 512 activaciones.<br><br>"
        "👁️ Qué observar: Sigue las dimensiones de entrada y salida.<br><br>"
        "🧠 Cómo interpretarla: La matriz de pesos aprende una combinación lineal.<br><br>"
        "🎯 Para estudiar: Calcula cuántos pesos y sesgos contiene la capa."
    )

    card = post_procesar._visual_card_md(
        slide,
        archivo="modelo.jpg",
        caption=caption,
        origen={"origen": "video"},
        parte=2,
        caption_confiable=True,
    )

    assert card.count("Lectura guiada — idea central") == 1
    assert card.count("Sigue las dimensiones de entrada y salida") == 1
    assert card.count("La matriz de pesos aprende") == 1
    assert card.count("Calcula cuántos pesos") == 1


def test_galeria_visual_ordena_cronologia_antes_de_agrupar_ventanas(tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    for name, shade in (("late.jpg", 200), ("early.jpg", 100)):
        _image(image_dir / name, shade)
    slides = [
        {
            "archivo": "late.jpg",
            "t_rel": 1250.0,
            "visual_role": "galeria",
            "material_oficial": {
                "pagina": 2,
                "texto": "Descenso por gradiente y mínimo de la pérdida",
            },
        },
        {
            "archivo": "early.jpg",
            "t_rel": 650.0,
            "visual_role": "galeria",
            "material_oficial": {
                "pagina": 1,
                "texto": "Función de pérdida y error de predicción",
            },
        },
    ]

    gallery = post_procesar._galeria_visual_complementaria_md(
        tmp_path, slides, {"origen": "video"}, parte=2)

    assert gallery.index("10:00") < gallery.index("20:00")
    assert gallery.count("Evidencia de 10:00 a 20:00") == 1
    assert gallery.count("Evidencia de 20:00 a 30:00") == 1


def test_galeria_no_publica_transcript_sin_ancla_visual(tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    _image(image_dir / "participantes.jpg", 190)
    slide = {
        "archivo": "participantes.jpg",
        "t_rel": 20.0,
        "parte": 2,
        "visual_role": "galeria",
        "visual_tipo": "slide_textual",
        "visual_score": 9,
        "visual_estable": True,
        "ocr": "",
        "transcript_ventana": (
            "El profesor menciona que deben escribir su nombre y correo electrónico."
        ),
    }

    gallery = post_procesar._galeria_visual_complementaria_md(
        tmp_path, [slide], {"origen": "video"}, parte=2)

    assert gallery == ""
    assert "nombre y correo" not in gallery


def test_galeria_conserva_titulo_breve_corroborado(tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    _image(image_dir / "gradiente.jpg", 130)
    slide = {
        "archivo": "gradiente.jpg",
        "t_rel": 20.0,
        "parte": 2,
        "visual_role": "galeria",
        "visual_tipo": "diagrama_o_grafico_probable",
        "visual_score": 9,
        "visual_estable": True,
        "ocr": "Descenso por Gradiente",
        "transcript_ventana": (
            "El descenso por gradiente permite actualizar los parámetros del modelo."
        ),
    }

    gallery = post_procesar._galeria_visual_complementaria_md(
        tmp_path, [slide], {"origen": "video"}, parte=2)

    assert "img/gradiente.jpg" in gallery
    assert "Descenso por Gradiente" in gallery


def test_contexto_previo_extrae_resumen_con_icono(tmp_path):
    (tmp_path / "leccion_final.md").write_text(
        "# Clase\n\n## 🧭 Resumen ejecutivo\n\n"
        "La clase conecta perceptrón y gradiente.\n\n"
        "### Temas\n\n- Perceptrón.\n- Gradiente.\n\n---\n\n"
        "# Parte 1\n\nDesarrollo.",
        encoding="utf-8",
    )

    contexto = post_procesar.contexto_previo_compacto(tmp_path)

    assert contexto["resumen"].startswith(
        "La clase conecta perceptrón y gradiente.")
    assert "### Temas" in contexto["resumen"]
    assert "- Gradiente." in contexto["resumen"]


def test_compactacion_resumen_mantiene_tema_tardio():
    markdown = (
        "## Inicio\n\n" + ("Historia temprana de la IA. " * 5000)
        + "\n\n## Parte 2: entrenamiento\n\n"
        "Descenso por gradiente, embeddings y modelos generativos."
    )

    compactado = post_procesar._compactar_markdown_para_resumen(
        markdown, max_chars=4000)

    assert len(compactado) <= 4000
    assert "Historia temprana" in compactado
    assert "embeddings y modelos generativos" in compactado


def test_gate_resumen_detecta_y_cierra_respuesta_truncada():
    truncado = (
        "La clase presenta los paradigmas principales. "
        "Se estudia Deep Blue como caso histórico de IA basada en b"
    )

    assert post_procesar._resumen_ejecutivo_incompleto(truncado)
    cerrado = post_procesar._cerrar_resumen_incompleto(truncado)
    assert cerrado == "La clase presenta los paradigmas principales."
    assert not post_procesar._resumen_ejecutivo_incompleto(cerrado)

    lista_cortada = (
        "### Ideas clave\n"
        "5. La capa lineal contiene pesos y sesgos.\n"
        "6"
    )
    assert post_procesar._resumen_ejecutivo_incompleto(lista_cortada)
    assert post_procesar._cerrar_resumen_incompleto(
        lista_cortada).endswith("pesos y sesgos.")

    frase_corta = "Punto completo.\n- Descenso por grad"
    assert post_procesar._resumen_ejecutivo_incompleto(frase_corta)
    assert post_procesar._cerrar_resumen_incompleto(
        frase_corta) == "Punto completo."


def test_barrera_final_bloquea_antes_de_iniciar_postproceso(monkeypatch, tmp_path):
    started = []
    report = {
        "ready": False,
        "status": "waiting_stt",
        "reasons": ["stt_pending=1"],
    }
    monkeypatch.setattr(
        post_procesar.audio_manifest, "completion_barrier_report", lambda _d: report)
    monkeypatch.setattr(post_procesar, "_cache_stats_reset", lambda: started.append(True))

    with pytest.raises(RuntimeError, match="stt_pending=1"):
        post_procesar.consolidar_todo(tmp_path)

    assert started == []
    assert post_procesar.verificar_barrera_final(
        tmp_path, permitir_parcial=True) == report


def test_reporte_tecnico_va_a_debug_y_no_al_documento_de_estudio(tmp_path):
    public, technical = post_procesar.separar_publicacion_y_diagnostico(
        contenido="## Concepto\n\nEl gradiente actualiza los pesos.",
        cobertura={
            "cobertura_estado": "dudosa",
            "advertencias": ["transición de fuente con 4.3 s sin audio"],
        },
        reportes=[
            "## Reporte de cobertura temporal\n\n- WAV existentes: 37.",
            "## Reporte transcript-first / slide-guided\n\n- Segmentos: 221.",
        ],
    )

    assert "El gradiente" in public
    assert "Límites de la grabación" in public
    assert "WAV existentes" not in public
    assert "Segmentos: 221" not in public
    assert "WAV existentes" in technical
    assert "Segmentos: 221" in technical


def test_galeria_oficial_referencia_paginas_no_observadas_sin_fingir_aparicion(
        tmp_path):
    (tmp_path / "img").mkdir()
    for index in (1, 2):
        _image(tmp_path / "img" / f"official_{index}.jpg")
    pages = [
        {"material_id": "deck:p1", "pagina": 1, "archivo": "official_1.jpg",
         "fuente_nombre": "deck.pptx", "texto": "Loss function",
         "apariciones": [{"archivo": "cap_1.jpg"}]},
        {"material_id": "deck:p2", "pagina": 2, "archivo": "official_2.jpg",
         "fuente_nombre": "deck.pptx", "texto": "Gradient descent",
         "apariciones": []},
    ]

    gallery = post_procesar._galeria_material_oficial_no_observado_md(
        tmp_path, pages, observed_ids={"deck:p1"})

    assert "img/official_2.jpg" in gallery
    assert "img/official_1.jpg" not in gallery
    assert "aparición no verificada" in gallery


def test_rutas_curso_de_copia_externa_no_escapan_a_contenido_global(
        monkeypatch, tmp_path):
    global_content = tmp_path / "repo" / "contenido"
    external_class = (
        tmp_path / "candidate" / "contenido" / "curso-demo" / "clases" / "clase-1")
    external_class.mkdir(parents=True)
    (external_class / "session_state.json").write_text(
        json.dumps({"curso": "curso-demo", "clase_id": "clase-1"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(post_procesar.config, "CONTENIDO_DIR", global_content)

    paths = post_procesar.rutas_curso(external_class)

    assert paths["curso_dir"] == external_class.parent.parent.resolve()
    assert paths["resumenes_dir"].is_dir()
    assert not (global_content / "curso-demo").exists()


def test_fallback_visual_publica_transcript_una_vez_no_por_slide():
    phrase = "The gradient tells us how parameters change."
    slides = [
        {"archivo": "one.jpg", "ocr": "Gradient descent", "t_rel": 1,
         "transcript_ventana": phrase},
        {"archivo": "two.jpg", "ocr": "Learning rate", "t_rel": 2,
         "transcript_ventana": phrase},
    ]
    segments = [{"t": 1, "ini": 0, "fin": 2, "texto": phrase}]

    fallback = post_procesar._fallback_slide_guided_md(
        slides, segments, {"origen": "video"})

    assert fallback.count(phrase) == 1
    assert "Contexto registrado del profesor" not in fallback
    assert "OCR disponible" not in fallback
    assert "fallback" not in fallback.lower()
    assert "DeepSeek" not in fallback


def test_fallback_visual_escapa_corchetes_del_titulo_para_resolver_marker():
    fallback = post_procesar._fallback_slide_guided_md(
        [{"archivo": "array.jpg", "ocr": "Input [0, 1, 2] | shape [3]", "t_rel": 1}],
        [], {"origen": "video"}, incluir_transcripcion=False,
    )

    assert "[0, 1, 2]" not in fallback
    assert "(0, 1, 2)" in fallback


def test_ocr_multimedia_usa_region_principal_y_excluye_recomendaciones(
        monkeypatch, tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    Image.new("RGB", (1000, 600), "white").save(image_dir / "media.jpg")

    calls = []

    def fake_ocr(image):
        calls.append(image.size)
        if image.width <= 540:
            return "Convolutional Network Demo from 1989 Yann LeCun"
        return (
            "Convolutional Network Demo from 1989 Yann LeCun "
            "recommended sidebar Demo of LeNet 1 unrelated video subscribe views"
        )

    monkeypatch.setattr(ocr, "texto_de_pil", fake_ocr)
    post_procesar._OCR_CACHE_BY_CLASS.clear()
    slide = {
        "archivo": "media.jpg",
        "ocr": (
            "YouTube Convolutional Network Demo from "
            "1989 Demo of LeNet 1 sidebar recommendation"
        ),
        "transcript_ventana": "The professor presents the 1989 convolutional network demo.",
    }

    principal = post_procesar._ocr_region_principal_media(tmp_path, slide)
    cached = post_procesar._ocr_region_principal_media(tmp_path, slide)

    assert "Convolutional Network Demo" in principal
    assert "LeNet 1" not in principal
    assert "sidebar" not in principal
    assert cached == principal
    assert len(calls) == 2
    rows = post_procesar.est.leer_jsonl(tmp_path / "_debug" / "ocr_cache.jsonl")
    assert {row.get("scope") for row in rows} == {
        "media_primary_x52_y04_90", "media_primary_x66_y04_90",
    }


def test_identidad_media_no_se_toma_de_recomendacion_lateral():
    markdown = (
        "## Laboratorio\n\n"
        "### Video histórico: Modelo Lateral-7 (1989)\n\n"
        "El profesor mostró **Modelo Lateral-7**, supuesto primer sistema.\n\n"
        "- 9.000 parámetros.\n\n"
        "### Implementación\n\nContinúa el laboratorio.\n"
    )
    slide = {
        "ocr": (
            "YouTube Convolutional Network Demo from 1989 Yann LeCun "
            "recommended sidebar Modelo Lateral 7 subscribe views"
        ),
        "ocr_principal": "YouTube Convolutional Network Demo from 1989 Yann LeCun",
        "transcript_ventana": (
            "El profesor presenta un video de 1989 con nueve mil parámetros."
        ),
    }

    corrected = post_procesar._corregir_identidad_media_periferica(
        markdown, [slide])

    assert "### Video presentado: *Convolutional Network Demo from 1989*" in corrected
    assert "Título verificado en la captura principal" in corrected
    assert "supuesto primer sistema" not in corrected
    assert "- 9.000 parámetros." in corrected
    assert "### Implementación" in corrected


def test_identidad_media_no_reescribe_mencion_sin_ancla_del_material():
    markdown = "## Comparación de Modelo Lateral-7\n\nTema independiente.\n"
    slide = {
        "ocr": "YouTube Main Lecture 1989 sidebar Modelo Lateral 7",
        "ocr_principal": "YouTube Main Lecture from 1989",
        "transcript_ventana": "Se presenta la conferencia de 1989.",
    }

    assert post_procesar._corregir_identidad_media_periferica(
        markdown, [slide]) == markdown


def test_bitacora_regenerada_es_idempotente_y_preserva_notas_manuales(tmp_path):
    course = tmp_path / "CURSO.md"
    course.write_text(
        "# Bitacora del curso\n\n"
        "## CLASE_01_clase-1 (2026-07-15)\n\nResumen viejo A.\n\n"
        "- 📝 [2026-07-15 10:00] Repasar XOR.\n\n"
        "## CLASE_02_otro-tema (2026-07-16)\n\nResumen de otra clase.\n\n"
        "## CLASE_01_clase-1 (2026-07-15)\n\nResumen viejo B.\n",
        encoding="utf-8",
    )
    original = course.read_text(encoding="utf-8")
    context = post_procesar._contexto_curso_sin_clase_actual(
        original, "CLASE_01_clase-1")

    assert "Resumen viejo" not in context
    assert "Resumen de otra clase" in context
    assert "Repasar XOR" in context

    post_procesar._actualizar_bitacora_curso(
        tmp_path, course, "CLASE_01_clase-1", "2026-07-15", "es",
        "Resumen estable.")
    first = course.read_bytes()
    post_procesar._actualizar_bitacora_curso(
        tmp_path, course, "CLASE_01_clase-1", "2026-07-15", "es",
        "Resumen estable.")
    second = course.read_bytes()
    text = second.decode("utf-8")

    assert first == second
    assert text.count("## CLASE_01_clase-1") == 1
    assert text.count("Repasar XOR") == 1
    assert "Resumen de otra clase" in text
    assert "Resumen estable." in text


def test_reporte_parte_solo_se_reusa_si_corresponde_al_derivado(tmp_path):
    lesson = tmp_path / "leccion_parte1.md"
    lesson.write_text("# Parte 1\n\nContenido estable.\n", encoding="utf-8")
    report = {"parte": 1, "visuales_principales": 7, "chunks": 2}

    post_procesar._guardar_reporte_parte(tmp_path, 1, lesson, report)

    assert post_procesar._cargar_reporte_parte(tmp_path, 1, lesson) == report
    lesson.write_text("# Parte 1\n\nContenido cambiado.\n", encoding="utf-8")
    assert post_procesar._cargar_reporte_parte(tmp_path, 1, lesson) is None


def test_extraer_tareas_respeta_modo_postproceso_sin_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("CLASE_POST_LLM", "0")
    monkeypatch.setattr(
        post_procesar, "preguntar",
        lambda *_args, **_kwargs: pytest.fail("no debe llamar al LLM"),
    )

    assert post_procesar.extraer_tareas(
        tmp_path, "El profesor dejó una tarea.", "CLASE_01") is None


def test_sanear_metadatos_visuales_conserva_src_y_limpia_rotulos_internos():
    markdown = (
        "### SLIDE 12: Regla del perceptrón\n\n"
        '<img src="img/cap_000123_p1.jpg" width="620">\n\n'
        "**Relación con la explicación (parte 1):** **SLIDE 12** "
        "cap_000123_p1.jpg (hora 21:15, score 4, motivo estable): "
        "**Transcripción asociada:** se compara la suma con el umbral. "
        "**OCR:** salida = 1.\n"
    )

    clean = post_procesar._sanear_metadatos_visuales_publicos(markdown)

    assert "### Regla del perceptrón" in clean
    assert 'src="img/cap_000123_p1.jpg"' in clean
    assert clean.count("cap_000123_p1.jpg") == 1
    assert "SLIDE 12" not in clean
    assert "Conexión conceptual" in clean
    assert "Cómo se relaciona con el tema" not in clean
    assert "Explicación del profesor" in clean
    assert "La diapositiva muestra" in clean


def test_saneador_no_publica_comentarios_sobre_el_mecanismo_ocr():
    markdown = (
        "La imagen reúne dígitos manuscritos con trazos diferentes. "
        "El OCR capturó parcialmente el título de la ventana. "
        "El profesor usa la demostración para explicar la inferencia."
    )

    clean = post_procesar._sanear_metadatos_visuales_publicos(markdown)

    assert "OCR" not in clean
    assert "dígitos manuscritos" in clean
    assert "explicar la inferencia" in clean


def test_galeria_relevante_se_publica_colapsada_sin_techo_numerico(tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    slides = []
    for index in range(3):
        name = f"gallery_{index}.jpg"
        _image(image_dir / name, 100 + index)
        slides.append({
            "archivo": name,
            "t_rel": index * 10,
            "ocr": f"Actualización verificable: w_{index} = {index + 1}",
            "transcript_ventana": (
                f"El profesor explica el concepto complementario {index} y su "
                "aplicación dentro del modelo."),
            "visual_estable": True,
            "visual_score": 8,
            "visual_role": "galeria",
        })

    markdown = post_procesar._galeria_visual_complementaria_md(
        tmp_path, slides, {"origen": "video"}, parte=1)

    assert "<details>" in markdown
    assert "Abrir " in markdown
    assert "(3 visuales)" in markdown
    assert markdown.count('<img src="img/gallery_') == 3
    assert markdown.rstrip().endswith("</details>")


def test_galeria_no_publica_transcript_crudo_sin_senal_visual_verificable(tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    _image(image_dir / "oral.jpg")
    slide = {
        "archivo": "oral.jpg",
        "t_rel": 10,
        "ocr": "Pantalla de apoyo",
        "transcript_ventana": (
            "El X se puede ver aquí como un número directamente, pero viene "
            "comprimido como una imagen."),
        "visual_estable": True,
        "visual_score": 8,
        "visual_role": "galeria",
    }

    markdown = post_procesar._galeria_visual_complementaria_md(
        tmp_path, [slide], {"origen": "video"}, parte=1)

    assert markdown == ""


def test_palabra_class_en_prosa_no_se_confunde_con_codigo_python():
    assert not post_procesar._contiene_codigo_formula(
        "A world class course introduces neural networks through examples.")
    assert post_procesar._contiene_codigo_formula(
        "class Perceptron(nn.Module):")


def test_caption_determinista_no_confunde_palabra_partida_o_nombre_con_formula():
    for texto_ocr in (
        "NVIDIA Deep Learning processing=ing course catalog",
        "Israel N. Chaparro=cruz presentación",
        "Neurc=les artificiales",
        "dataset = información general",
    ):
        assert post_procesar._caption_visual_determinista(
            {"ocr": texto_ocr}) == ""

    caption = post_procesar._caption_visual_determinista(
        {"ocr": "Fórmula de la capa: z = Wx"})
    assert "z = Wx" in caption


def test_caption_determinista_de_tarea_no_publica_ocr_corrupto():
    caption = post_procesar._caption_visual_determinista({
        "ocr": (
            "Tarea 1: Clasificación de Dígitos c LA•ucs OhO "
            "iFÉro:A EslEFA'0"),
    })

    assert "actividad o entrega" in caption.lower()
    assert "LA•ucs" not in caption


def test_prompts_corrigen_stt_y_cruzan_ocr_sin_confundir_voz_y_diapositiva():
    prompt = post_procesar.PROMPT_SLIDE_GUIDED
    merge = post_procesar.PROMPT_MERGE_LECCION

    assert "ortografía y gramática" in prompt
    assert "La diapositiva añade" in prompt
    assert "barras laterales" in prompt
    assert "aparezca solo en ese texto periférico" in prompt
    assert "momento de la grabación" in prompt
    assert "cruza cifras, nombres, fórmulas y listas" in merge
    assert "texto es la explicación principal" in merge


def test_entretejer_resuelve_caption_con_corchetes_sin_filtrar_el_visual(tmp_path):
    (tmp_path / "img").mkdir()
    _image(tmp_path / "img" / "array.jpg")
    slide = {"archivo": "array.jpg", "t_rel": 1}

    result = post_procesar.entretejer(
        "[[IMG:array.jpg|Input [0, 1, 2] con shape [3]]]",
        tmp_path, [slide], {"origen": "video"},
    )

    assert "[[IMG:" not in result
    assert 'src="img/array.jpg"' in result
    assert "Input [0, 1, 2]" in result


def test_tarjeta_visual_rechaza_ocr_corrupto_y_prioriza_explicacion_docente():
    slide = {
        "archivo": "agenda.jpg",
        "t_rel": 675,
        "ocr": (
            "00000 Agenda art 1: Introducción al Deep Learning Part 2: Cómo se "
            "entrena una red neuronal Pañ 3: Redes neuronales Parl 4: Aumento y"),
        "transcript_ventana": (
            "El profesor presenta cuatro unidades: fundamentos, entrenamiento, redes "
            "convolucionales y aumento de datos. Explica que la secuencia avanza desde "
            "los conceptos hasta la práctica."),
    }

    card = post_procesar._visual_card_md(
        slide, archivo="agenda.jpg", origen={"origen": "video"}, parte=1)

    assert 'width="760"' in card
    assert "Explicación para estudiar" in card
    assert "cuatro unidades" in card
    assert "00000 Agenda" not in card
    assert "Pañ 3" not in card
    assert "Parl 4" not in card


def test_tarjeta_visual_conserva_formula_legible_que_completa_el_audio():
    slide = {
        "archivo": "formula.jpg",
        "t_rel": 42,
        "ocr": "Actualización de pesos: w = 3; learning_rate = 0.01",
        "transcript_ventana": (
            "El profesor explica que los pesos se actualizan en la dirección que reduce "
            "la función de pérdida."),
    }

    card = post_procesar._visual_card_md(
        slide, archivo="formula.jpg", origen={"origen": "video"})

    assert "learning_rate = 0.01" in card
    assert "reduce la función de pérdida" in card


def test_tarjeta_visual_no_reintroduce_fragmentos_stt_en_minuscula():
    slide = {
        "archivo": "curso.jpg",
        "t_rel": 615,
        "ocr": "Agenda del curso de Deep Learning",
        "transcript_ventana": (
            "en el School y podrían inscribirse como estudiante de maestría,. "
            "ustedes para que puedan capacitarse en ese sentido. "
            "vamos a ver la agenda del curso."
        ),
    }

    card = post_procesar._visual_card_md(
        slide,
        archivo="curso.jpg",
        caption=(
            "La agenda conecta fundamentos, entrenamiento, visión por computador "
            "y transferencia de aprendizaje."
        ),
        origen={"origen": "video"},
    )

    assert "La agenda conecta fundamentos" in card
    assert "en el School" not in card
    assert "ustedes para que" not in card
    assert "La imagen respalda el desarrollo" not in card


def test_tarjeta_visual_no_publica_cromo_de_aplicacion_ni_fragmento_oral():
    slide = {
        "archivo": "notebook.jpg",
        "t_rel": 5820,
        "ocr": (
            "Inteligencia Artificial MNIST.ipynb Archivo Editar Insertar "
            "Entorno de ejecución Herramientas Ayuda Comandos Ejecutar todas "
            "Copiar en Drive PIL Image"),
        "transcript_ventana": (
            "Nos demos cuenta directamente que es un número seis, en realidad es "
            "una imagen digitalizada. Ustedes también prueben cambiar eso."),
    }

    assert post_procesar._ocr_visual_confiable(slide) == ""
    assert post_procesar._extracto_transcript_visual(slide) == ""
    assert post_procesar._visual_card_md(
        slide, archivo="notebook.jpg", origen={"origen": "video"}) == ""


def test_caption_pedagogico_confiable_no_repite_stt_ni_ocr_crudos():
    slide = {
        "archivo": "mnist.jpg",
        "t_rel": 5820,
        "ocr": (
            "MNIST.ipynb Archivo Editar Insertar Entorno de ejecución Herramientas "
            "Ayuda Ejecutar todas Copiar en Drive"),
        "transcript_ventana": (
            "El X se puede ver aquí como un número directamente, pero en el fondo "
            "viene comprimido como una imagen."),
    }

    card = post_procesar._visual_card_md(
        slide,
        archivo="mnist.jpg",
        caption=(
            "La captura relaciona cada matriz de 28 × 28 píxeles con su etiqueta "
            "numérica y permite comprobar el par de entrenamiento (X, Y)."
        ),
        origen={"origen": "video"},
        caption_confiable=True,
    )

    assert "La captura relaciona cada matriz" in card
    assert "El X se puede ver aquí" not in card
    assert "Archivo Editar Insertar" not in card


def test_tarjeta_visual_ofrece_lectura_guiada_detallada_y_accion_de_estudio():
    slide = {
        "archivo": "dataset.jpg",
        "t_rel": 120,
        "ocr": (
            "Dataset de entrenamiento: muestras, características, etiquetas, "
            "train set y test set"
        ),
        "transcript_ventana": (
            "El profesor explica cómo se separan las muestras para entrenar y "
            "evaluar el modelo."
        ),
        "visual_role": "principal",
        "visual_tipo": "slide_textual",
        "visual_score": 8,
    }

    card = post_procesar._visual_card_md(
        slide,
        archivo="dataset.jpg",
        caption=(
            "La lámina organiza las muestras del conjunto de datos y distingue la "
            "partición usada para ajustar el modelo de la reservada para evaluarlo."
        ),
        origen={"origen": "video"},
        caption_confiable=True,
    )

    assert "Lectura guiada — idea central" in card
    assert "👁️ Qué observar" in card
    assert "🧠 Cómo interpretarla" in card
    assert "🎯 Para estudiar" in card
    assert "Diferencia muestra, características, etiqueta" in card
    assert "cuántas observaciones, variables y clases" in card
    assert "Dataset de entrenamiento:" not in card


def test_notebook_practico_publica_concepto_sin_cromo_de_aplicacion():
    slide = {
        "archivo": "split.jpg",
        "t_rel": 5820,
        "ocr": (
            "MNIST.ipynb Archivo Editar Insertar Entorno de ejecución Herramientas "
            "Comandos Ejecutar todas Copiar en Drive x_train x_valid training set "
            "validation set"
        ),
        "transcript_ventana": (
            "El profesor separa los ejemplos usados para entrenar de los reservados "
            "para validar el modelo."
        ),
        "visual_role": "principal",
        "visual_tipo": "codigo_formula_tabla",
        "visual_score": 8,
    }

    caption = post_procesar._caption_visual_determinista(slide, max_chars=900)
    card = post_procesar._visual_card_md(
        slide,
        archivo="split.jpg",
        caption=caption,
        origen={"origen": "video"},
        caption_confiable=True,
    )

    assert "datos usados para ajustar el modelo" in caption
    assert "reservados para validarlo" in caption
    assert "Archivo Editar Insertar" not in card
    assert "Lectura guiada" in card


def test_notebook_practico_tolera_espacios_ocr_alrededor_de_guion_bajo():
    slide = {
        "archivo": "split_ocr.jpg",
        "ocr": (
            "MNIST.ipynb Archivo Editar Entorno de ejecución x _ train imágenes "
            "para entrenamiento x _ valid imágenes para validar el rendimiento"
        ),
        "visual_role": "principal",
        "visual_tipo": "codigo_formula_tabla",
    }

    caption = post_procesar._caption_entorno_practico(slide)

    assert "datos usados para ajustar el modelo" in caption
    assert "reservados para validarlo" in caption


def test_placeholder_visual_se_reemplaza_por_lectura_especifica_de_demo():
    slide = {
        "archivo": "demo.jpg",
        "t_rel": 4200,
        "ocr": "Convolutional Network Demo from 1989 handwritten digits",
        "transcript_ventana": (
            "El profesor muestra cómo la red reconoce dígitos escritos a mano."
        ),
        "visual_role": "secuencia",
        "visual_tipo": "diagrama_o_grafico_probable",
        "media_reference": True,
        "media_phase": "reproduccion",
        "media_title": "Convolutional Network Demo from 1989",
    }

    card = post_procesar._visual_card_md(
        slide,
        archivo="demo.jpg",
        caption="Evidencia visual conservada para este tramo",
        origen={"origen": "video"},
        caption_confiable=True,
    )

    assert "estado interno de la demostración" in card
    assert "Convolutional Network Demo from 1989" in card
    assert "Evidencia visual conservada para este tramo" not in card
    assert "muestra inferencia, no el proceso de entrenamiento" in card
    assert "28 × 28 píxeles" in card


def test_gate_final_reintegra_toda_identidad_principal_y_secuencia(tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    _image(image_dir / "main.jpg", 140)
    _image(image_dir / "step.jpg", 180)
    slides = [
        {
            "archivo": "main.jpg",
            "t_rel": 10,
            "ocr": (
                "Training workflow input features model prediction loss update"
            ),
            "transcript_ventana": (
                "El profesor explica el flujo de entrenamiento y la actualización "
                "de parámetros."
            ),
            "visual_role": "principal",
            "visual_tipo": "slide_textual",
            "visual_score": 8,
        },
        {
            "archivo": "step.jpg",
            "t_rel": 20,
            "ocr": (
                "Training workflow input features model prediction loss update "
                "gradient feedback"
            ),
            "transcript_ventana": (
                "El profesor añade la retroalimentación del gradiente al flujo."
            ),
            "visual_role": "secuencia",
            "visual_tipo": "slide_textual",
            "visual_score": 8,
        },
    ]

    lesson, report = post_procesar._integrar_cobertura_visual_obligatoria(
        "## Entrenamiento\n\nExplicación principal.",
        tmp_path,
        slides,
        {"origen": "video"},
        parte=1,
    )

    assert lesson.count('src="img/main.jpg"') == 1
    assert lesson.count('src="img/step.jpg"') == 1
    assert "Diapositivas y secuencias adicionales" in lesson
    assert report["principales_publicadas"] == 1
    assert report["secuencias_publicadas"] == 1
    assert report["reintegradas_por_gate"] == 2
    assert report["faltantes"] == []


def test_busqueda_observada_se_publica_visible_con_consulta_y_sin_ocr_crudo(
        tmp_path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    _image(image_dir / "search.jpg", 120)
    slide = {
        "archivo": "search.jpg",
        "t_rel": 30,
        "ocr": (
            "CE)gle nvidia fundamentals of deep learning Resultados patrocinados "
            "anuncio lateral controles"
        ),
        "browsing_reference": True,
        "browsing_query": "nvidia fundamentals of deep learning",
        "visual_role": "galeria",
        "visual_tipo": "slide_textual",
        "visual_score": 8,
    }

    markdown = post_procesar._galeria_visual_complementaria_md(
        tmp_path, [slide], {"origen": "video"}, parte=1)

    assert "Recursos consultados y demostraciones observadas" in markdown
    assert "nvidia fundamentals of deep learning" in markdown
    assert 'src="img/search.jpg"' in markdown
    assert "anuncio lateral controles" not in markdown
    assert "<details>" not in markdown
    assert "👁️ Qué observar" in markdown


def test_consulta_web_corrige_ocr_con_audio_y_agrega_preposicion():
    slide = {
        "ocr": (
            "deinicion carro Visión general creada por IA Un carro es un vehículo "
            "motorizado Mostrar todo 7 sitios"
        ),
        "transcript_ventana": (
            "El profesor busca la definición de auto para mostrar que una descripción "
            "verbal no basta para reconocer todos sus píxeles."
        ),
        "browsing_reference": True,
        "browsing_query": "deinicion carro",
    }

    caption = post_procesar._caption_visual_determinista(slide)

    assert "definición de carro" in caption
    assert "deinicion" not in caption


def test_consulta_web_no_traduce_termino_ingles_por_similitud():
    slide = {
        "ocr": (
            "CE)gle nvidia fundamentals of deep learning Resultados patrocinados"
        ),
        "transcript_ventana": (
            "Fundamentos del curso de Deep Learning de NVIDIA. El profesor presenta "
            "su estructura y objetivos."
        ),
        "browsing_reference": True,
        "browsing_query": "nvidia fundamentals of deep learning",
    }

    caption = post_procesar._caption_visual_determinista(slide)

    assert "nvidia fundamentals of deep learning" in caption.casefold()
    assert "NVIDIA Fundamentos of deep learning" not in caption


def test_cuadricula_de_dataset_recibe_guia_de_datos_no_de_formula():
    slide = {
        "archivo": "mnist_grid.jpg",
        "ocr": (
            "MNIST 10x10 samples dataset train set labels "
            "[5, 0, 4, 1, 9, 2, 3]"
        ),
        "transcript_ventana": (
            "El profesor compara distintas muestras de dígitos manuscritos y sus "
            "etiquetas antes de entrenar el modelo."
        ),
        "visual_role": "principal",
        "visual_tipo": "codigo_formula_tabla",
        "visual_score": 9,
    }

    card = post_procesar._visual_card_md(
        slide,
        archivo="mnist_grid.jpg",
        caption=(
            "La cuadrícula reúne ejemplos del conjunto MNIST y permite comparar "
            "la variación de los trazos asociados a cada etiqueta."
        ),
        origen={"origen": "video"},
        caption_confiable=True,
    )

    assert "Diferencia muestra, características, etiqueta" in card
    assert "Localiza símbolos, operadores" not in card


def test_recurso_web_hereda_consulta_sin_publicar_ocr_del_resultado():
    slide = {
        "ocr": (
            "Products Solutions nVIDlA Deep Learning Institute navigation "
            "advertising panels"
        ),
        "transcript_ventana": (
            "El profesor abre el curso Fundamentals of Deep Learning de NVIDIA."
        ),
        "browsing_reference": True,
        "browsing_query": "",
        "browsing_parent_query": "nvidia fundamentals of deep learning",
        "browsing_phase": "recurso",
    }

    caption = post_procesar._caption_visual_determinista(slide)

    assert "recurso que se abrió" in caption
    assert "nvidia fundamentals of deep learning" in caption.casefold()
    assert "advertising panels" not in caption


def test_consulta_corrige_nombre_propio_con_audio_sin_traducir_vocabulario():
    slide = {
        "ocr": "Geogle rosemblat xor neurona Modo IA Todo Imágenes",
        "transcript_ventana": (
            "El profesor corrige el apellido Rosenblatt y vuelve al problema XOR."
        ),
        "browsing_reference": True,
        "browsing_query": "rosemblat xor neurona",
    }

    caption = post_procesar._caption_visual_determinista(slide)

    assert "Rosenblatt xor neurona" in caption
    assert "rosemblat" not in caption


def test_pagina_web_con_ocr_corrupto_usa_solo_tema_corroborado_por_audio():
    slide = {
        "ocr": (
            "Computer Hstory Muscum Solutlong nV1DlA Deep Blue Kasparov "
            "llistory rnad more navigatlon"
        ),
        "transcript_ventana": (
            "El profesor explica que Deep Blue venció a Kasparov mediante búsqueda "
            "y evaluación de posiciones, no mediante una red neuronal."
        ),
        "browsing_reference": True,
        "browsing_query": "",
    }

    caption = post_procesar._caption_visual_determinista(slide)

    assert "Deep Blue" in caption
    assert "Hstory" not in caption
    assert "Solutlong" not in caption
    assert "llistory" not in caption


def test_galeria_con_ocr_corrupto_no_inventa_caption_desde_transcript():
    slide = {
        "archivo": "history.jpg",
        "ocr": (
            "perfl del pcztuLsnte nvlDlA Leamlng Solutlong Hstory rnad nide"
        ),
        "transcript_ventana": (
            "El profesor presenta la evolución histórica de las redes neuronales y "
            "explica cómo los datos y la capacidad de cómputo permitieron su resurgimiento."
        ),
        "visual_role": "galeria",
        "visual_tipo": "slide_textual",
        "visual_score": 8,
        "visual_estable": True,
    }

    assert post_procesar._ocr_visual_confiable(slide) == ""
    caption = post_procesar._caption_visual_determinista(slide)

    assert caption == ""
    assert "pcztuLsnte" not in caption
    assert "Leamlng" not in caption


def test_ocr_principal_con_mayusculas_internas_rotas_no_se_publica():
    slide = {
        "ocr": (
            "nvlDlA Fundamentos del Deep Learning Parte 1 Introducción al "
            "Deep Leaming"
        ),
        "transcript_ventana": (
            "El profesor presenta los fundamentos del curso de Deep Learning."
        ),
        "visual_role": "principal",
        "visual_tipo": "slide_textual",
        "visual_score": 8,
        "visual_estable": True,
    }

    assert post_procesar._ocr_visual_confiable(slide) == ""


def test_asignacion_ocr_no_verificada_no_se_convierte_en_formula():
    slide = {
        "ocr": "dataset = ZOLS tengo que gente no pagó su préstamo",
        "transcript_ventana": "",
        "visual_role": "principal",
        "visual_tipo": "codigo_formula_tabla",
        "visual_score": 8,
    }

    caption = post_procesar._caption_visual_determinista(slide)

    assert "ZOLS" not in caption
    assert "lámina visualmente distinta" not in caption
    assert caption == ""


def test_definicion_no_activa_control_fin_de_interfaz_claseviva():
    web = (
        "deinicion carro Visión general creada por IA. Un carro es un vehículo "
        "motorizado usado para el transporte terrestre."
    )
    panel = (
        "ClaseViva Controles de clase Curso actual Pausar Seguir Fin "
        "Transcripción Copiloto"
    )

    assert post_procesar.es_captura_interfaz_claseviva(
        web, "search.jpg") is False
    assert post_procesar.es_captura_interfaz_claseviva(
        panel, "panel.jpg") is True


def test_tarjeta_visual_no_publica_logistica_oral_sin_contenido_academico():
    slide = {
        "archivo": "cierre.jpg",
        "t_rel": 5970,
        "ocr": "",
        "transcript_ventana": (
            "Creo que no me escuchan por el micrófono. Si no hay más preguntas, "
            "nos vemos a las tres p. m. mediante el link de Google."),
    }

    assert post_procesar._extracto_transcript_visual(slide) == ""
    assert post_procesar._visual_card_md(
        slide, archivo="cierre.jpg", origen={"origen": "video"}) == ""


def test_tarjeta_visual_no_duplica_caption_que_es_prefijo_del_transcript():
    slide = {
        "archivo": "gradiente.jpg",
        "t_rel": 120,
        "ocr": "",
        "transcript_ventana": (
            "El profesor explica que el descenso de gradiente ajusta los pesos en "
            "la dirección que reduce la pérdida. Después relaciona el paso con la "
            "tasa de aprendizaje."),
    }

    card = post_procesar._visual_card_md(
        slide,
        archivo="gradiente.jpg",
        caption=(
            "El profesor explica que el descenso de gradiente ajusta los pesos en "
            "la dirección que reduce la pérdida."
        ),
        origen={"origen": "video"},
    )

    assert "descenso de gradiente" in card
    assert "Conexión con la explicación" not in card


def test_ocr_principal_alimenta_roles_sin_perder_ocr_completo():
    source = [{
        "archivo": "browser.jpg",
        "ocr": "barra lateral recomendaciones correo y controles",
        "ocr_principal": (
            "Wikipedia Deep Blue chess computer Article IBM Kasparov references"
        ),
    }]

    promoted = post_procesar._promover_ocr_principal(source)

    assert promoted[0]["ocr"].startswith("Wikipedia Deep Blue")
    assert promoted[0]["ocr_completo"].startswith("barra lateral")
    assert promoted[0]["ocr_clasificacion_origen"] == "region_multimedia_principal"
    assert source[0]["ocr"].startswith("barra lateral")


def test_saneador_publico_elimina_cita_stt_y_redacta_correo_sin_perder_prosa():
    raw = (
        "El profesor explica el acceso de forma general.\n\n"
        "**Transcript (00:01-00:04):**\n"
        "> frase fonetica sin corregir del registro\n"
        "> segunda frase duplicada\n\n"
        "Contacto mostrado: alumna@example.edu.pe.\n"
    )

    clean = post_procesar._sanear_contenido_llm_publico(raw)

    assert "El profesor explica" in clean
    assert "frase fonetica" not in clean
    assert "Transcript" not in clean
    assert "alumna@example.edu.pe" not in clean
    assert "[correo omitido]" in clean


def test_saneador_integra_imagen_y_prosa_sin_rotulos_ocr_o_transcript():
    raw = (
        "## Historia de la IA\n\n"
        "### [[IMG:deep-blue.jpg|Deep Blue ilustra búsqueda en árboles]]\n\n"
        "**OCR de la diapositiva:** Geogle Modo IA Parl 4 texto roto\n\n"
        "**Lo que dijo el profesor (transcript asociado [20:39–20:57]):** "
        "Deep Blue aplicaba búsqueda y no aprendizaje automático.\n\n"
        "### Continuación sin diapositivas (20:57–25:00)\n\n"
        "La explicación conecta este paradigma con machine learning.\n\n"
        "### Resumen del rango\n\nSe repite todo lo anterior.\n"
    )

    clean = post_procesar._sanear_contenido_llm_publico(raw)

    assert "[[IMG:deep-blue.jpg" in clean
    assert "### [[IMG:" not in clean
    assert "OCR de la diapositiva" not in clean
    assert "Geogle" not in clean
    assert "Lo que dijo el profesor" not in clean
    assert "transcript asociado" not in clean
    assert "[20:39–20:57]" not in clean
    assert "Deep Blue aplicaba búsqueda" in clean
    assert "Continuación sin diapositivas" not in clean
    assert "La explicación conecta" in clean
    assert "Resumen del rango" not in clean
    assert "Se repite todo" not in clean


def test_saneador_convierte_admonition_img_inventada_al_contrato_canonico(
        tmp_path):
    (tmp_path / "img").mkdir()
    _image(tmp_path / "img" / "agenda.jpg")
    raw = (
        "## Agenda\n\n"
        "> [!IMG:agenda.jpg]  \n"
        "> La imagen organiza seis unidades y conecta fundamentos con práctica.\n\n"
        "El texto continúa con la explicación principal.\n"
    )

    clean = post_procesar._sanear_contenido_llm_publico(raw)
    rendered = post_procesar.entretejer(
        clean,
        tmp_path,
        [{
            "archivo": "agenda.jpg",
            "t_rel": 10,
            "ocr": "Agenda de seis unidades",
            "transcript_ventana": "El profesor presenta la agenda del curso.",
        }],
        {"origen": "video"},
    )

    assert "> [!IMG:" not in clean
    assert (
        "[[IMG:agenda.jpg|La imagen organiza seis unidades y conecta "
        "fundamentos con práctica.]]"
    ) in clean
    assert 'src="img/agenda.jpg"' in rendered
    assert "[[IMG:" not in rendered


def test_saneador_repara_marcador_con_un_corchete_sin_borrar_el_archivo(
        tmp_path):
    (tmp_path / "img").mkdir()
    filename = "cap_000000000010_p1.jpg"
    _image(tmp_path / "img" / filename)
    raw = (
        "## MNIST\n\n"
        f"[[IMG:{filename}|Cuadrícula de dígitos que evidencia la "
        "variación manuscrita.]\n"
    )

    clean = post_procesar._sanear_contenido_llm_publico(raw)
    rendered = post_procesar.entretejer(
        clean,
        tmp_path,
        [{
            "archivo": filename,
            "t_rel": 10,
            "ocr": "MNIST handwritten digits",
            "transcript_ventana": (
                "El profesor explica la variación de los dígitos manuscritos."
            ),
        }],
        {"origen": "video"},
    )
    published = post_procesar._sanear_metadatos_visuales_publicos(rendered)

    assert f"[[IMG:{filename}|" in clean
    assert "[[IMG:|" not in clean
    assert f'src="img/{filename}"' in published
    assert "[[IMG:" not in rendered


def test_listado_de_correos_no_se_publica_como_visual_docente():
    slide = {
        "archivo": "participantes.jpg",
        "ocr": "Lista: uno@example.edu.pe dos@example.edu.pe tres@example.edu.pe",
        "transcript_ventana": "Se resuelve el acceso de participantes.",
        "visual_estable": True,
        "visual_score": 9,
    }

    score = post_procesar.puntuar_slide_relevancia(
        slide, slide["transcript_ventana"])

    assert "listado_datos_personales" in score["penalizaciones"]
    assert post_procesar.motivos_slide_estudio(slide) == []
    assert post_procesar._visual_card_md(
        slide, archivo="participantes.jpg", origen={"origen": "video"}) == ""


def test_extraer_busqueda_admite_encabezado_con_terminos_en_backticks():
    markdown = (
        "## Desarrollo\n\nContenido.\n\n"
        "## BUSCAR\n"
        "`NVIDIA DLI certification` `Google Colab GPU`\n"
    )

    clean, queries = post_procesar._extraer_busquedas(markdown)

    assert "BUSCAR" not in clean
    assert queries == ["NVIDIA DLI certification Google Colab GPU"]


def test_extraer_busqueda_admite_blanco_y_consulta_sin_backticks():
    markdown = (
        "## Desarrollo\n\nContenido.\n\n"
        "## BUSCAR:\n\n"
        "deep learning fundamentals NVIDIA DLI instructor-led workshop\n"
    )

    clean, queries = post_procesar._extraer_busquedas(markdown)

    assert "BUSCAR" not in clean
    assert queries == [
        "deep learning fundamentals NVIDIA DLI instructor-led workshop"
    ]


def test_extraer_busqueda_admite_icono_y_negrita_inline():
    markdown = (
        "## Desarrollo\n\nContenido.\n\n"
        "🔍 **BUSCAR**: Machine Learning paradigm supervised learning cycle\n"
    )

    clean, queries = post_procesar._extraer_busquedas(markdown)

    assert "BUSCAR" not in clean
    assert queries == [
        "Machine Learning paradigm supervised learning cycle"
    ]


def test_saneador_generaliza_rotulos_plural_buscar_y_fuentes_de_pipeline():
    markdown = (
        "### Slides 5, 6 y 7 — Por qué una capa oculta representa XOR\n\n"
        "[[IMG:xor.jpg|La capa oculta transforma el espacio de entrada.]]\n\n"
        "**Qué muestran las diapositivas:** tres neuronas combinan regiones lineales.\n\n"
        "**Qué dice el profesor (00:43:03–00:43:40):** no había un método práctico "
        "para ajustar todos los pesos.\n\n"
        "## BUSCAR:\n"
        "`XOR perceptron hidden layer backpropagation`\n"
    )

    without_search, queries = post_procesar._extraer_busquedas(markdown)
    clean = post_procesar._sanear_metadatos_visuales_publicos(without_search)

    assert "### Por qué una capa oculta representa XOR" in clean
    assert "Slides 5, 6 y 7" not in clean
    assert "Qué muestran las diapositivas" not in clean
    assert "Qué dice el profesor" not in clean
    assert "Lectura visual" in clean
    assert "Explicación integrada" in clean
    assert "[[IMG:xor.jpg" in clean
    assert "BUSCAR" not in clean
    assert queries == ["XOR perceptron hidden layer backpropagation"]


def test_prompt_de_leccion_prohibe_transcript_crudo_y_datos_personales():
    prompt = post_procesar.PROMPT_SLIDE_GUIDED

    assert "No pegues bloques rotulados Transcript" in prompt
    assert "No publiques correos" in prompt
    assert "fuentes externas" in prompt
    assert "nunca publiques rótulos como 'OCR de la diapositiva'" in prompt
    assert "No titules secciones como 'Slide N'" in prompt
    assert "movimiento de presentador, cursor, resaltado o jitter de OCR" in prompt
    assert "nunca uses blockquotes, admonitions como '> [!IMG:...]'" in prompt


def test_desarrollo_exhaustivo_agrupa_tandas_y_asocia_fuentes_al_tramo():
    guiones = [
        "# Chunk 1.001\n\n> Rango: 00:00 -> 05:00\n"
        "> Slides de esta tanda: 1 de 2 deduplicadas.\n\n"
        "## Fundamentos\n\nExplicación extensa A.\n\n"
        '| 🖼️ Evidencia visual | 📖 Explicación para estudiar |\n'
        "|:---|:---|\n"
        '| <img src="img/a.jpg" width="760"> | **Lectura:** diagrama A |',
        "# Chunk 1.001_02\n\n> Rango: 00:00 -> 05:00\n"
        "> Slides de esta tanda: 1 de 2 deduplicadas.\n\n"
        '| 🖼️ Evidencia visual | 📖 Explicación para estudiar |\n'
        "|:---|:---|\n"
        '| <img src="img/b.jpg" width="760"> | **Lectura:** diagrama B |',
        "# Chunk 1.002\n\n> Rango: 05:00 -> 10:00\n"
        "> Slides de esta tanda: 0 de 0 deduplicadas.\n\n"
        "## Entrenamiento\n\nExplicación extensa B.",
    ]
    fuentes = {
        "1.001": "#### Fuentes ampliadas\n\n- Fuente oficial A",
        "1.002": "#### Fuentes ampliadas\n\n- Fuente oficial B",
    }

    detail = post_procesar._desarrollo_detallado_guiones_md(guiones, fuentes)

    assert detail.count("### 🧩 Tramo") == 2
    assert detail.count("img/a.jpg") == 1
    assert detail.count("img/b.jpg") == 1
    assert detail.index("Fuente oficial A") < detail.index("Explicación extensa B")
    assert "# Chunk" not in detail
    assert "Slides de esta tanda" not in detail


def test_ocr_rico_estable_no_es_proteccion_sin_valor_semantico():
    generica = {
        "archivo": "web.jpg",
        "ocr": "Portal de navegación con menús enlaces controles y texto descriptivo genérico",
        "visual_estable": True,
        "transcript_ventana": "",
    }
    formula = {
        "archivo": "formula.jpg",
        "ocr": "Paso iterativo con parámetro w = 2 y gradiente",
        "visual_estable": True,
        "transcript_ventana": "El profesor explica el gradiente.",
    }

    assert post_procesar.motivos_slide_estudio(generica) == []
    assert "patron_tecnico" in post_procesar.motivos_slide_estudio(formula)


def test_interfaz_notebook_no_es_tarea_pero_instruccion_accionable_si():
    interfaz = {
        "archivo": "jupyter.jpg",
        "ocr": "JupyterLab File Edit View Kernel dataset notebook Python 3",
        "visual_estable": True,
        "transcript_ventana": "El profesor sigue trabajando en el laboratorio.",
    }
    instruccion = {
        "archivo": "entrega.jpg",
        "ocr": "Actividad: descargar el dataset, completar el notebook y entregar hoy",
        "visual_estable": True,
        "transcript_ventana": "El profesor indica la actividad que deben entregar.",
    }

    assert "tarea_recurso" not in post_procesar.motivos_slide_estudio(interfaz)
    assert "tarea_recurso" in post_procesar.motivos_slide_estudio(instruccion)
    assert post_procesar._ocr_visual_confiable(interfaz) == ""
    assert "entregar" in post_procesar._ocr_visual_confiable(instruccion).lower()

    descripcion = {
        "archivo": "historia.jpg",
        "ocr": "Clasificar digitos fue una tarea historicamente dificil para los ordenadores",
        "visual_estable": True,
        "transcript_ventana": "Se describe la historia del reconocimiento de digitos.",
    }
    assert "tarea_recurso" not in post_procesar.motivos_slide_estudio(descripcion)

    menu_recurso = {
        "archivo": "menu.jpg",
        "ocr": "Archivo Editar Ejecutar todas Buscar en Drive Abrir cuaderno Subir cuaderno",
        "visual_estable": True,
        "transcript_ventana": "Continua el laboratorio.",
    }
    assert "tarea_recurso" not in post_procesar.motivos_slide_estudio(menu_recurso)


def test_consultas_web_no_quedan_limitadas_a_cuatro_tramos(monkeypatch):
    monkeypatch.delenv("CLASE_POST_WEB_MAX_CONSULTAS", raising=False)
    consultas = [f"topic {index}" for index in range(32)]
    monkeypatch.setattr(
        post_procesar, "fuentes_ampliadas_md",
        lambda query, **_kwargs: f"#### Fuentes ampliadas\n\n- {query}",
    )

    fuentes = post_procesar.fuentes_ampliadas_consultas(consultas)

    assert post_procesar._post_web_max_consultas() >= 32
    assert fuentes.count("### topic") == 32
    assert "topic 31" in fuentes


def test_consulta_web_prioriza_conceptos_tecnicos_y_rechaza_fuente_ajena():
    query = post_procesar._consulta_web_fallback(
        "Ustedes pueden entrar al curso. NVIDIA DLI presenta Deep Learning en Google Colab. "
        "Luego se estudia una red neuronal y la funcion de perdida.",
        "Clase 1",
    )

    assert "NVIDIA DLI" in query
    assert "Google Colab" in query
    assert "site:nvidia.com" in query
    assert "ustedes" not in query.lower()

    irrelevant = post_procesar.fuentes_ampliadas_md(
        query,
        buscar_fn=lambda _q, _n: [{
            "titulo": "Curso para aprender ingles",
            "url": "https://example.org/english",
            "resumen": "Frases para iniciar una conversacion.",
        }],
        extraer_fn=lambda _url: {
            "ok": True,
            "titulo": "Aprender ingles",
            "fuente": "example.org",
            "ideas": ["Como saludar en ingles."],
        },
    )
    relevant = post_procesar.fuentes_ampliadas_md(
        query,
        buscar_fn=lambda _q, _n: [{
            "titulo": "NVIDIA Deep Learning Institute",
            "url": "https://www.nvidia.com/training/",
            "resumen": "Cursos DLI de deep learning.",
        }],
        extraer_fn=lambda _url: {
            "ok": True,
            "titulo": "NVIDIA Deep Learning Institute",
            "fuente": "nvidia.com",
            "ideas": ["DLI ofrece formacion practica en deep learning."],
        },
    )

    assert irrelevant == ""
    assert "NVIDIA Deep Learning Institute" in relevant


def test_fuentes_web_prefieren_documentacion_oficial_sobre_blog_relevante():
    extraidas = []

    def extraer(url):
        extraidas.append(url)
        return {
            "ok": True,
            "titulo": "Fuente verificada",
            "fuente": url.split("/", 3)[2],
            "ideas": ["PyTorch documenta el entrenamiento de redes neuronales."],
        }

    resultado = post_procesar.fuentes_ampliadas_md(
        "PyTorch neural network official research",
        n=1,
        buscar_fn=lambda _q, _n: [
            {
                "titulo": "Tutorial PyTorch de un blog",
                "url": "https://tutoriales.example/pytorch",
                "resumen": "Entrenamiento de una neural network con PyTorch.",
            },
            {
                "titulo": "PyTorch documentation",
                "url": "https://docs.pytorch.org/tutorials/",
                "resumen": "Official PyTorch neural network tutorials.",
            },
        ],
        extraer_fn=extraer,
    )

    assert "docs.pytorch.org" in resultado
    assert "tutoriales.example" not in resultado
    assert extraidas == ["https://docs.pytorch.org/tutorials/"]


def test_fuentes_web_omiten_fuente_de_baja_autoridad_aunque_sea_relevante():
    resultado = post_procesar.fuentes_ampliadas_md(
        "PyTorch neural network official research",
        buscar_fn=lambda _q, _n: [{
            "titulo": "PyTorch neural network guide",
            "url": "https://blog.example/pytorch-neural-network",
            "resumen": "PyTorch neural network training guide.",
        }],
        extraer_fn=lambda _url: {
            "ok": True,
            "titulo": "PyTorch neural network guide",
            "fuente": "blog.example",
            "ideas": ["PyTorch neural network training."],
        },
    )

    assert resultado == ""


def test_fuentes_web_no_confunden_plataforma_oficial_con_contenido_de_tercero():
    query = "NVIDIA DLI Google Colab deep learning official research"

    assert post_procesar._puntuar_autoridad_web(
        "https://colab.research.google.com/github/usuario/curso/blob/main/demo.ipynb",
        query,
    ) == 0
    assert post_procesar._puntuar_autoridad_web(
        "https://www.nvidia.com/en-us/training/",
        query,
    ) >= 80


def test_fuentes_web_mantienen_el_concepto_principal_del_tramo():
    query = "IBM Deep Blue expert systems machine learning official research"
    fuente = post_procesar.fuentes_ampliadas_md(
        query,
        n=2,
        buscar_fn=lambda _q, _n: [
            {
                "titulo": "What is artificial intelligence?",
                "url": "https://cloud.google.com/learn/what-is-artificial-intelligence",
                "resumen": "Machine learning systems and artificial intelligence.",
            },
            {
                "titulo": "IBM Deep Blue",
                "url": "https://www.ibm.com/history/deep-blue",
                "resumen": "IBM Deep Blue chess system.",
            },
        ],
        extraer_fn=lambda url: {
            "ok": True,
            "titulo": "IBM Deep Blue" if "ibm.com" in url else "Google AI",
            "fuente": url.split("/", 3)[2],
            "ideas": [
                "IBM Deep Blue fue un sistema de ajedrez."
                if "ibm.com" in url else "Machine learning systems."
            ],
        },
    )

    assert "ibm.com" in fuente
    assert "cloud.google.com" not in fuente


def test_fuentes_web_no_tratan_portal_comercial_punto_edu_como_institucion():
    assert post_procesar._puntuar_autoridad_web(
        "https://www.academia.edu/12345/perceptron",
        "Frank Rosenblatt perceptron official documentation primary source",
    ) == 0


def test_fuentes_web_admiten_consulta_distintiva_de_un_solo_termino():
    assert post_procesar._ideas_web_relevantes(
        "perceptron",
        ["El perceptron aprende una frontera lineal."],
    ) == ["El perceptron aprende una frontera lineal."]


def test_fuentes_web_filtran_ideas_vecinas_de_una_portada_oficial():
    query = "NVIDIA DLI Google Colab official documentation primary source"
    resultado = post_procesar.fuentes_ampliadas_md(
        query,
        n=1,
        buscar_fn=lambda _q, _n: [{
            "titulo": "NVIDIA Deep Learning Institute (DLI)",
            "url": "https://www.nvidia.com/en-us/training/",
            "resumen": "NVIDIA DLI ofrece formación práctica en deep learning.",
        }],
        extraer_fn=lambda _url: {
            "ok": True,
            "titulo": "NVIDIA Deep Learning Institute Menu Menu Close Close Caret down",
            "fuente": "nvidia.com",
            "ideas": [
                "AIStore is a distributed object store for AI workloads.",
                "NemoClaw runs autonomous assistants.",
            ],
        },
    )

    assert "NVIDIA DLI ofrece" in resultado
    assert "Menu Menu" not in resultado
    assert "AIStore" not in resultado
    assert "NemoClaw" not in resultado


def test_fuentes_web_rechazan_portada_oficial_ajena_al_concepto_completo():
    query = "NVIDIA DLI Google Colab official documentation primary source"
    assert not post_procesar._fuente_web_relevante(
        query,
        {
            "titulo": "NVIDIA Documentation Hub",
            "url": "https://docs.nvidia.com/",
            "resumen": "NVIDIA product documentation.",
        },
        {
            "ok": True,
            "titulo": "NVIDIA Documentation Hub",
            "fuente": "docs.nvidia.com",
            "ideas": ["NVIDIA AIStore and autonomous agents."],
        },
    )


def test_fallback_merge_elimina_metadatos_internos_del_chunk():
    merged = post_procesar._fallback_merge_estudio([
        "# Chunk 1.001\n\n> Rango: 00:00 -> 01:00\n"
        "> Slides de esta tanda: 2 de 4 deduplicadas.\n\n"
        "## Desarrollo del tramo\n\nExplicación útil.",
        "# Chunk 1.002\n\n## Desarrollo del tramo\n\nOtra explicación.",
    ])

    assert merged.startswith("## Desarrollo de la clase")
    assert "Explicación útil" in merged
    assert "Otra explicación" in merged
    assert "# Chunk" not in merged
    assert "> Rango:" not in merged
    assert "Slides de esta tanda" not in merged


def test_fallback_resumen_preserva_resumen_previo_sin_mensaje_interno():
    previous = "La clase explica gradiente, tasa de aprendizaje y validación."

    result = post_procesar._fallback_resumen_ejecutivo({"resumen": previous})

    assert result == previous
    assert "API" not in result
    assert "regenerar" not in result


def test_fallback_resumen_descarta_placeholder_tecnico_previo():
    result = post_procesar._fallback_resumen_ejecutivo({
        "resumen": "(la API no respondio; revisar la leccion integrada y regenerar)",
    })

    assert "API" not in result
    assert "regenerar" not in result
    assert "Consulta el desarrollo completo" in result


def test_post_llm_off_omite_red_y_backoff(monkeypatch, tmp_path):
    monkeypatch.setenv("CLASE_POST_LLM", "0")
    monkeypatch.setattr(
        post_procesar, "preguntar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("no debe invocar el cliente LLM")),
    )
    post_procesar._cache_stats_reset()

    result = post_procesar.preguntar_cacheado(
        tmp_path, [{"role": "user", "content": "evidencia"}],
        max_tokens=100, etiqueta="offline", prompt_version="test:v1",
    )

    assert result is None
    stats = post_procesar.cache_llm_stats()
    assert stats["llm_omitidas"] == 1
    assert stats["llamadas_reales"] == 0
    assert post_procesar.preguntar_cacheado.ultimo_reporte["llm"] == "disabled"
