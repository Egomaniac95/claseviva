import builtins
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src import post_procesar
from src import visual_materials
from scripts import benchmark_visual_materials


def _minimal_pptx(path: Path, titles: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for index, title in enumerate(titles, 1):
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                "<p:sld xmlns:p='http://schemas.openxmlformats.org/presentationml/2006/main' "
                "xmlns:a='http://schemas.openxmlformats.org/drawingml/2006/main'>"
                f"<p:cSld><a:t>{title}</a:t></p:cSld></p:sld>",
            )


def test_contenido_descargado_es_opt_in_local_y_excluye_txt(tmp_path):
    class_dir = tmp_path / "curso" / "clases" / "clase"
    downloaded = class_dir / "contenido_descargado"
    downloaded.mkdir(parents=True)
    pdf = downloaded / "diapositivas.pdf"
    pptx = downloaded / "anexo.pptx"
    transcript = downloaded / "transcripto.txt"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    _minimal_pptx(pptx, ["Tema"])
    transcript.write_text("official transcript", encoding="utf-8")

    resolved = visual_materials.resolver_materiales_oficiales(class_dir)

    assert resolved == [pptx.resolve(), pdf.resolve()]
    assert transcript.resolve() not in resolved


def test_material_descargado_despues_se_descubre_en_siguiente_ejecucion(
        tmp_path):
    class_dir = tmp_path / "curso" / "clases" / "clase"
    class_dir.mkdir(parents=True)
    assert visual_materials.resolver_materiales_oficiales(class_dir) == []
    downloaded = class_dir / "contenido_descargado"
    downloaded.mkdir()
    pdf = downloaded / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")

    assert visual_materials.resolver_materiales_oficiales(class_dir) == [
        pdf.resolve()]


def test_pptx_opt_in_renderiza_paginas_y_conserva_hash_procedencia(
        monkeypatch, tmp_path):
    pptx = tmp_path / "deck.pptx"
    _minimal_pptx(pptx, ["Loss function", "Gradient descent"])

    def fake_render(_source, output):
        output.mkdir(parents=True, exist_ok=True)
        _slide_sintetica(output / "Slide1.PNG")
        _slide_sintetica(output / "Slide2.PNG", progreso=True)
        return {"renderer": "fake-powerpoint", "pages": 2}

    monkeypatch.setattr(visual_materials, "_render_pptx", fake_render)
    img_dir = tmp_path / "img"
    manifest = tmp_path / "visual_material_manifest.json"

    pages, report = visual_materials.preparar_materiales_oficiales(
        [pptx], img_dir, manifest_path=manifest)

    assert report["materiales_validos"] == 1
    assert report["paginas_preparadas"] == 2
    assert [page["tipo_fuente"] for page in pages] == ["pptx", "pptx"]
    assert [page["texto"] for page in pages] == ["Loss function", "Gradient descent"]
    assert len({page["fuente_sha256"] for page in pages}) == 1
    assert pages[0]["renderizador"] == "fake-powerpoint"
    assert Path(pages[0]["fuente"]) == pptx.resolve()


def test_preflight_pptx_falla_con_mensaje_accionable_sin_powerpoint(
        monkeypatch):
    monkeypatch.setattr(visual_materials.os, "name", "nt")
    monkeypatch.setattr(visual_materials.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="PowerPoint Desktop") as error:
        visual_materials.preflight_pptx_renderer()

    assert "exporta el PPTX a PDF" in str(error.value)


def _slide_sintetica(path: Path, *, progreso: bool = False) -> None:
    """Fixture derivado del patrón real, sin copiar material del curso."""
    image = Image.new("RGB", (960, 540), "#f5f7fb")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 74), fill="#17324d")
    draw.text((42, 24), "NEURAL NETWORK TRAINING", fill="white")
    for row in range(5):
        for col in range(8):
            x = 80 + col * 96
            y = 125 + row * 68
            color = "#e45756" if (row + col) % 3 == 0 else "#4c78a8"
            draw.ellipse((x, y, x + 20, y + 20), fill=color)
            if col < 7:
                draw.line((x + 20, y + 10, x + 88, y + 10), fill="#777", width=2)
    draw.text((62, 478), "loss -> gradients -> parameter update", fill="#111")
    if progreso:
        draw.rounded_rectangle((610, 392, 900, 462), 10, fill="#72b7b2")
        draw.text((638, 414), "learning rate = 0.01", fill="#071a19")
    image.save(path, quality=96)


def _captura_con_overlay(source: Path, target: Path, variant: int) -> None:
    slide = Image.open(source).convert("RGB").resize((900, 506))
    frame = Image.new("RGB", (1280, 720), "#20242b")
    frame.paste(slide, (300, 55))
    draw = ImageDraw.Draw(frame)
    # El presentador tapa y modifica una parte distinta del frame completo.
    x = 230 + variant * 22
    draw.ellipse((x, 128, x + 210, 338), fill=(80 + variant * 18, 55, 45))
    draw.rectangle((0, 610, 1280, 720), fill=(12, 12, 16))
    draw.rectangle((470 + variant * 35, 650, 800 + variant * 35, 674), fill="#eceff4")
    draw.text((24, 650), f"player controls {variant}", fill="white")
    frame.save(target, quality=92)


def _superficie_marca(path: Path, title: str, body: str, *, ui: bool = False) -> None:
    """Plantilla compartida: el branding no demuestra identidad de contenido."""
    image = Image.new("RGB", (960, 540), "#f8fafc")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 64), fill="#76b900")
    draw.rectangle((0, 64, 168, 540), fill="#eef5e6")
    for idx in range(6):
        y = 98 + idx * 62
        draw.rounded_rectangle((24, y, 142, y + 38), 6, outline="#5b8c18", width=3)
        draw.ellipse((42, y + 10, 58, y + 26), fill="#76b900")
        draw.line((68, y + 18, 126, y + 18), fill="#42651c", width=3)
    draw.rounded_rectangle((198, 92, 928, 492), 12, outline="#92a0ad", width=4)
    draw.text((224, 116), title, fill="#111827")
    if ui:
        draw.rectangle((224, 164, 900, 206), fill="#dbe5ee")
        for idx, label in enumerate(("FILES", "TERMINAL", "KERNELS", "NOTEBOOKS")):
            x = 246 + idx * 158
            draw.rounded_rectangle((x, 236, x + 126, 332), 8, outline="#47637a", width=3)
            draw.text((x + 18, 274), label, fill="#1f3345")
    else:
        for idx in range(5):
            y = 190 + idx * 48
            draw.ellipse((242, y + 4, 254, y + 16), fill="#76b900")
            draw.line((272, y + 10, 820 - idx * 24, y + 10), fill="#34495e", width=4)
    draw.text((224, 448), body, fill="#263442")
    image.save(path, quality=96)


def _workspace_frame(
        path: Path, *, presenter_x: int, cursor_x: int, progress: str = "") -> None:
    image = Image.new("RGB", (960, 540), "#182028")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((330, 36, 928, 492), 8, fill="#f7f9fb")
    draw.rectangle((330, 36, 928, 84), fill="#dce4eb")
    draw.rectangle((350, 106, 492, 464), fill="#eef3f7")
    draw.text((520, 116), "JUPYTER WORKSPACE", fill="#172b3a")
    for idx, label in enumerate(("data", "images", "lesson.ipynb")):
        draw.rectangle((370, 142 + idx * 42, 386, 158 + idx * 42), fill="#76b900")
        draw.text((400, 140 + idx * 42), label, fill="#243746")
    draw.rectangle((520, 164, 894, 388), outline="#8797a5", width=3)
    draw.text((548, 196), "Notebook environment", fill="#263b4b")
    if progress:
        draw.rounded_rectangle((548, 260, 858, 344), 8, fill="#cfe8c2")
        draw.text((574, 292), progress, fill="#17320f")
    draw.ellipse((presenter_x, 154, presenter_x + 260, 500), fill="#8a5d45")
    draw.polygon(
        ((cursor_x, 122), (cursor_x + 13, 150), (cursor_x + 18, 136)),
        fill="#111827",
    )
    image.save(path, quality=94)


def _formula_frame(
        path: Path, *, presenter_x: int, cursor_x: int,
        formula: str = "loss = 1.0", highlight: bool = False) -> None:
    image = Image.new("RGB", (960, 540), "#edf2f7")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((180, 35, 930, 505), 8, fill="white")
    draw.text((225, 72), "XOR WITH A HIDDEN LAYER", fill="#16243a")
    for row, label in enumerate(("x1", "x2", "h1", "h2", "h3", "y")):
        x = 255 + row * 105
        y = 245 if row < 2 else 210 + (row % 3) * 72
        draw.ellipse((x, y, x + 48, y + 48), fill="#5aa7a7")
        draw.text((x + 15, y + 16), label, fill="white")
        if row:
            draw.line((x - 58, y + 24, x, y + 24), fill="#40536b", width=3)
    draw.text((565, 425), formula, fill="#172b4d")
    if highlight:
        draw.rounded_rectangle(
            (540, 398, 810, 470), 8, outline="#d9485f", width=5)
    draw.ellipse((presenter_x, 220, presenter_x + 175, 515), fill="#8a5d45")
    draw.polygon(
        ((cursor_x, 125), (cursor_x + 13, 153), (cursor_x + 18, 139)),
        fill="#111827",
    )
    image.save(path, quality=94)


def _slide_timeline(archivo: str, t: float) -> dict:
    return {
        "archivo": archivo,
        "t_rel": t,
        "t_abs": t,
        "t_inicio_rel": t,
        "t_fin_rel": t + 8,
        "intervalos_visuales": [{"inicio": t, "fin": t + 8}],
        "grupo_total": 1,
        "capturas_colapsadas": 1,
        "ocr": "Neural network training loss gradients parameter update",
        "transcript_ventana": "The instructor explains gradients and parameter updates.",
        "sin_transcript_cercano": False,
        "visual_estable": True,
        "visual_tipo": "diagrama_o_grafico_probable",
        "visual_score": 8,
        "score": 22,
    }


def test_material_oficial_colapsa_overlays_y_conserva_apariciones(tmp_path):
    img_dir = tmp_path / "img"
    deck_dir = tmp_path / "deck"
    img_dir.mkdir()
    deck_dir.mkdir()
    source = deck_dir / "001.png"
    _slide_sintetica(source)

    slides = []
    for idx in range(3):
        nombre = f"cap_{idx}.jpg"
        _captura_con_overlay(source, img_dir / nombre, idx)
        slides.append(_slide_timeline(nombre, 10.0 + idx * 12))

    manifest = tmp_path / "visual_material_manifest.json"
    pages, prep = visual_materials.preparar_materiales_oficiales(
        [deck_dir], img_dir, manifest_path=manifest)
    enriched, report = visual_materials.integrar_material_oficial(
        img_dir, slides, pages, manifest_path=manifest)

    assert prep["paginas_preparadas"] == 1
    assert report["estados_emparejados"] == 3
    assert report["duplicados_colapsados"] == 2
    assert len(enriched) == 1
    selected = enriched[0]
    assert selected["visual_role"] == "principal"
    assert selected["archivo"].startswith("material_oficial_")
    assert selected["archivo_evidencia"] == "cap_0.jpg"
    assert {item["archivo"] for item in selected["evidencias_aparicion"]} == {
        "cap_0.jpg", "cap_1.jpg", "cap_2.jpg",
    }

    data = json.loads(manifest.read_text(encoding="utf-8"))
    page = data["paginas"][0]
    assert Path(page["fuente"]).resolve() == deck_dir.resolve()
    assert len(page["fuente_sha256"]) == 64
    assert {item["archivo"] for item in page["apariciones"]} == {
        "cap_0.jpg", "cap_1.jpg", "cap_2.jpg",
    }


def test_paginas_progresivas_distintas_no_se_fusionan(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    first = img_dir / "material_1.jpg"
    second = img_dir / "material_2.jpg"
    _slide_sintetica(first, progreso=False)
    _slide_sintetica(second, progreso=True)
    slides = [
        {
            **_slide_timeline("material_1.jpg", 10),
            "material_oficial": {
                "material_id": "deck-sha:p0001", "fuente_sha256": "deck-sha",
                "pagina": 1,
            },
            "archivo_evidencia": "cap_1.jpg",
        },
        {
            **_slide_timeline("material_2.jpg", 30),
            "material_oficial": {
                "material_id": "deck-sha:p0002", "fuente_sha256": "deck-sha",
                "pagina": 2,
            },
            "archivo_evidencia": "cap_2.jpg",
        },
    ]

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert len(result) == 2
    assert [slide["visual_role"] for slide in result] == ["principal", "secuencia"]


def test_salto_lejano_a_pagina_casi_duplicada_conserva_captura_progresiva(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    for name, color in (("p1.jpg", "#17324d"), ("p2.jpg", "#f5f7fb"),
                        ("p3.jpg", "#4c78a8"), ("p20.jpg", "#f5f7fb"),
                        ("capture.jpg", "#f5f7fb")):
        Image.new("RGB", (320, 180), color).save(img_dir / name)
    source = "deck-sha"
    pages = [
        {"fuente_sha256": source, "pagina": 1, "archivo": "p1.jpg", "texto": "Intro"},
        {"fuente_sha256": source, "pagina": 2, "archivo": "p2.jpg", "texto": "Loss curve"},
        {"fuente_sha256": source, "pagina": 3, "archivo": "p3.jpg", "texto": "Gradient"},
        {"fuente_sha256": source, "pagina": 20, "archivo": "p20.jpg",
         "texto": "Loss curve appendix"},
    ]
    slides = [
        {"archivo": "p1.jpg", "t_rel": 0, "material_oficial": {
            "fuente_sha256": source, "pagina": 1}},
        {"archivo": "p20.jpg", "archivo_evidencia": "capture.jpg", "t_rel": 10,
         "material_match": {"score": 50}, "material_oficial": {
             "fuente_sha256": source, "pagina": 20}},
        {"archivo": "p3.jpg", "t_rel": 20, "material_oficial": {
            "fuente_sha256": source, "pagina": 3}},
    ]

    result, rejected = visual_materials._reject_temporal_official_outliers(
        img_dir, slides, pages)

    assert len(rejected) == 1
    assert result[1]["archivo"] == "capture.jpg"
    assert "material_oficial" not in result[1]
    assert result[1]["material_match_rejected"]["pagina_rechazada"] == 20


def test_salto_real_a_pagina_visual_unica_no_se_rechaza(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    for name, color in (("p1.jpg", "#17324d"), ("p2.jpg", "#f5f7fb"),
                        ("p3.jpg", "#4c78a8"), ("p20.jpg", "#e45756")):
        Image.new("RGB", (320, 180), color).save(img_dir / name)
    source = "deck-sha"
    pages = [
        {"fuente_sha256": source, "pagina": 1, "archivo": "p1.jpg", "texto": "Intro"},
        {"fuente_sha256": source, "pagina": 2, "archivo": "p2.jpg", "texto": "Loss curve"},
        {"fuente_sha256": source, "pagina": 3, "archivo": "p3.jpg", "texto": "Gradient"},
        {"fuente_sha256": source, "pagina": 20, "archivo": "p20.jpg",
         "texto": "Appendix formula"},
    ]
    slides = [
        {"archivo": "p1.jpg", "t_rel": 0, "material_oficial": {
            "fuente_sha256": source, "pagina": 1}},
        {"archivo": "p20.jpg", "t_rel": 10, "material_oficial": {
            "fuente_sha256": source, "pagina": 20}},
        {"archivo": "p3.jpg", "t_rel": 20, "material_oficial": {
            "fuente_sha256": source, "pagina": 3}},
    ]

    result, rejected = visual_materials._reject_temporal_official_outliers(
        img_dir, slides, pages)

    assert rejected == []
    assert result[1]["material_oficial"]["pagina"] == 20


def test_material_oficial_no_impone_un_techo_arbitrario(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    slides = []
    for idx in range(20):
        archivo = f"p{idx:02d}.jpg"
        Image.new("RGB", (160, 90), (idx * 7, 30, 50)).save(img_dir / archivo)
        slides.append({
            **_slide_timeline(archivo, idx * 10),
            "material_oficial": {
                "material_id": f"deck:p{idx + 1:04d}",
                "fuente_sha256": "deck",
                "pagina": idx + 1,
            },
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert len(result) == 20
    assert all(slide["visual_role"] in {"principal", "secuencia"} for slide in result)


def test_pagina_oficial_solo_de_marca_no_se_publica_como_principal(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    Image.new("RGB", (320, 180), "#78be20").save(img_dir / "brand.jpg")
    slide = {
        **_slide_timeline("brand.jpg", 10),
        "ocr": "NVIDIA",
        "visual_tipo": "captura_generica",
        "material_oficial": {
            "material_id": "deck:p0039", "fuente_sha256": "deck",
            "pagina": 39, "texto": "",
        },
    }

    result = visual_materials.clasificar_roles_visuales(img_dir, [slide])

    assert result[0]["visual_role"] == "sin_valor"


def test_thumbnail_en_interfaz_externa_no_suplanta_slide_presentada(tmp_path):
    img_dir = tmp_path / "img"
    deck_dir = tmp_path / "deck"
    img_dir.mkdir()
    deck_dir.mkdir()
    source = deck_dir / "001.png"
    _slide_sintetica(source)
    _captura_con_overlay(source, img_dir / "course_ui.jpg", 1)
    pages, _ = visual_materials.preparar_materiales_oficiales([deck_dir], img_dir)
    pages[0]["texto"] = "Neural network training loss gradients parameter update"
    slide = _slide_timeline("course_ui.jpg", 10)
    slide["ocr"] = (
        "https://learn.example.com/course progress tab all bookmarks course catalog "
        "shop drivers support instructor-led workshops profile account"
    )

    result, report = visual_materials.integrar_material_oficial(img_dir, [slide], pages)

    assert report["estados_emparejados"] == 0
    assert result[0]["archivo"] == "course_ui.jpg"
    assert result[0]["visual_role"] == "sin_valor"


def test_branding_compartido_no_empareja_interfaz_tecnica_incompatible(tmp_path):
    img_dir = tmp_path / "img"
    deck_dir = tmp_path / "deck"
    material_dir = tmp_path / "material"
    img_dir.mkdir()
    deck_dir.mkdir()
    official = deck_dir / "001.png"
    capture = img_dir / "external_ui.jpg"
    _superficie_marca(
        official,
        "DEEP LEARNING TRANSFORMS THE WORLD",
        "Official lecture overview",
    )
    _superficie_marca(
        capture,
        "DEEP LEARNING INSTITUTE",
        "JupyterLab dashboard",
        ui=True,
    )
    pages, _ = visual_materials.preparar_materiales_oficiales(
        [deck_dir], material_dir)
    pages[0]["texto"] = "Deep Learning transforms the world"
    slide = {
        **_slide_timeline(capture.name, 10),
        "ocr": (
            "Deep Learning Institute JupyterLab dashboard terminal kernels "
            "notebooks folders environment"
        ),
        "visual_tipo": "codigo_formula_tabla",
    }

    result, report = visual_materials.integrar_material_oficial(
        img_dir, [slide], pages, material_img_dir=material_dir)

    assert report["estados_emparejados"] == 0
    assert result[0]["archivo"] == capture.name
    assert result[0]["visual_role"] == "principal"


def test_plantilla_similar_no_sustituye_pagina_textualmente_incompatible(tmp_path):
    img_dir = tmp_path / "img"
    deck_dir = tmp_path / "deck"
    material_dir = tmp_path / "material"
    img_dir.mkdir()
    deck_dir.mkdir()
    official = deck_dir / "001.png"
    capture = img_dir / "course_start.jpg"
    _superficie_marca(official, "OVERVIEW OF THE COURSE", "Official structure")
    _superficie_marca(
        capture,
        "COURSE START",
        "Load lectures and interactive notebooks",
    )
    pages, _ = visual_materials.preparar_materiales_oficiales(
        [deck_dir], material_dir)
    pages[0]["texto"] = "Overview of the course"
    slide = {
        **_slide_timeline(capture.name, 10),
        "ocr": (
            "Course start load lecture decks interactive notebooks launch "
            "workspace five minutes"
        ),
        "visual_tipo": "captura_generica",
    }

    result, report = visual_materials.integrar_material_oficial(
        img_dir, [slide], pages, material_img_dir=material_dir)

    assert report["estados_emparejados"] == 0
    assert result[0]["archivo"] == capture.name


def test_movimiento_de_presentador_y_cursor_no_crea_secuencia(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    slides = []
    for idx, presenter_x in enumerate((16, 42, 10, 58)):
        name = f"workspace_{idx}.jpg"
        _workspace_frame(
            img_dir / name,
            presenter_x=presenter_x,
            cursor_x=700 + idx * 9,
        )
        slides.append({
            **_slide_timeline(name, 10 + idx * 2),
            "ocr": (
                "Jupyter workspace data images lesson notebook environment"
            ),
            "visual_tipo": "codigo_formula_tabla",
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert sum(slide["visual_role"] == "principal" for slide in result) == 1
    assert all(slide["visual_role"] != "secuencia" for slide in result)
    assert sum(
        bool(slide.get("duplicado_pedagogico")) for slide in result
    ) == 3


def test_capturas_sin_deck_colapsan_misma_slide_y_eligen_ocr_mas_completo(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    slides = []
    texts = (
        "1989 Samy Bengio neural networks marginal NeurIPS 350 attendees "
        "less than 100 hidden units",
        "1989 Samy Bengio neural networks marginal NeurIPS 350 attendees "
        "less than 100 hidden units one doctoral student zero professors",
    )
    for index, text in enumerate(texts):
        name = f"lecture_{index}.jpg"
        _workspace_frame(
            img_dir / name, presenter_x=18 + index * 80, cursor_x=710 + index * 20)
        slide = _slide_timeline(name, 10 + index * 8)
        slide.update({
            "ocr": text,
            "visual_tipo": "captura_generica",
            "transcript_ventana": "The professor discusses Samy Bengio and NeurIPS 350.",
        })
        slides.append(slide)

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert [slide["visual_role"] for slide in result] == ["sin_valor", "principal"]
    assert result[0]["duplicado_pedagogico"] is True
    assert result[1]["representante"] == "captura_mas_completa_o_final"


def test_video_con_movimiento_sin_novedad_publica_un_representante_no_una_secuencia(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    slides = []
    for index, color in enumerate(("#203040", "#704020", "#204070", "#702040")):
        name = f"video_{index}.jpg"
        Image.new("RGB", (640, 360), color).save(img_dir / name)
        slides.append({
            "archivo": name,
            "t_rel": 10 + index * 3,
            "t_inicio_rel": 10 + index * 3,
            "t_fin_rel": 11 + index * 3,
            "ocr": "Convolutional Network Demo from 1989 Yann LeCun",
            "transcript_ventana": "The instructor comments on the 1989 network demo.",
            "sin_transcript_cercano": False,
            "visual_estable": False,
            "visual_tipo": "captura_generica",
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert [slide["visual_role"] for slide in result].count("galeria") == 1
    assert all(slide["visual_role"] != "secuencia" for slide in result)
    assert sum(slide["visual_role"] == "sin_valor" for slide in result) == 3


def test_demo_audiovisual_conserva_ficha_y_un_estado_no_todos_los_frames(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    rows = [
        (
            "metadata.jpg",
            10,
            (
                "YouTube Convolutional Network Demo from 1989 Yann LeCun "
                "Suscribirse Compartir Guardar 146541 vistas This is a demo of "
                "LeNet 1. It was developed at Bell Labs."
            ),
            "ruido_visual",
            -8,
        ),
        (
            "playback_1.jpg",
            20,
            "Convolutional Network Demo from 1989",
            "diagrama_o_grafico_probable",
            15,
        ),
        (
            "playback_2.jpg",
            35,
            "Network Demo from 1989",
            "diagrama_o_grafico_probable",
            20,
        ),
        (
            "playback_3.jpg",
            50,
            "Convolutional Network Demo from 1989",
            "diagrama_o_grafico_probable",
            20,
        ),
        (
            "playback_4.jpg",
            65,
            "Demo from 1989",
            "slide_textual",
            8,
        ),
    ]
    slides = []
    for index, (name, timestamp, text, visual_type, score) in enumerate(rows):
        Image.new(
            "RGB", (960, 540), (30 + index * 35, 45 + index * 20, 60)
        ).save(img_dir / name)
        slide = _slide_timeline(name, timestamp)
        slide.update({
            "ocr": text,
            "transcript_ventana": "",
            "sin_transcript_cercano": True,
            "visual_estable": False,
            "visual_tipo": visual_type,
            "visual_score": score,
        })
        slides.append(slide)

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)
    by_name = {slide["archivo"]: slide for slide in result}

    assert sum(slide["visual_role"] == "principal" for slide in result) == 1
    assert sum(slide["visual_role"] == "secuencia" for slide in result) == 1
    assert sum(slide["visual_role"] == "sin_valor" for slide in result) == 3
    assert by_name["metadata.jpg"]["representante"] == (
        "ficha_audiovisual_mas_informativa")
    assert by_name["playback_3.jpg"]["representante"] == (
        "estado_audiovisual_observable")
    assert by_name["playback_1.jpg"]["visual_role"] == "sin_valor"
    assert all(slide.get("media_reference") for slide in result)


def test_cambio_de_contenido_real_si_crea_secuencia_progresiva(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _workspace_frame(
        img_dir / "before.jpg", presenter_x=18, cursor_x=720)
    _workspace_frame(
        img_dir / "after.jpg", presenter_x=48, cursor_x=760,
        progress="UPLOAD COMPLETE: model.ipynb",
    )
    slides = [
        {
            **_slide_timeline("before.jpg", 10),
            "ocr": "Jupyter workspace data images lesson notebook environment",
            "visual_tipo": "codigo_formula_tabla",
        },
        {
            **_slide_timeline("after.jpg", 18),
            "ocr": (
                "Jupyter workspace data images lesson notebook environment "
                "upload complete model ipynb"
            ),
            "transcript_ventana": (
                "The instructor uploads the model notebook and confirms it is complete."
            ),
            "visual_tipo": "codigo_formula_tabla",
        },
    ]

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert [slide["visual_role"] for slide in result] == [
        "principal", "secuencia",
    ]
    assert result[1]["secuencia_progresiva"] is True
    assert result[1]["cambio_pedagogico"]["ocr_tokens_nuevos"] >= 2


def test_pantalla_distinta_con_texto_compartido_no_es_secuencia(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _workspace_frame(
        img_dir / "workspace.jpg", presenter_x=18, cursor_x=720)
    replacement = Image.new("RGB", (960, 540), "white")
    draw = ImageDraw.Draw(replacement)
    draw.rectangle((20, 20, 940, 520), fill="#14213d")
    for row in range(6):
        draw.rectangle(
            (80, 70 + row * 70, 880, 105 + row * 70),
            fill="#fca311" if row % 2 else "#e5e5e5",
        )
    replacement.save(img_dir / "replacement.jpg", quality=94)
    slides = [
        {
            **_slide_timeline("workspace.jpg", 10),
            "ocr": "Jupyter workspace data images lesson notebook environment",
            "visual_tipo": "codigo_formula_tabla",
        },
        {
            **_slide_timeline("replacement.jpg", 18),
            "ocr": (
                "Jupyter workspace data images lesson notebook environment "
                "upload complete model ipynb"
            ),
            "transcript_ventana": (
                "The instructor confirms upload complete model ipynb."
            ),
            "visual_tipo": "codigo_formula_tabla",
        },
    ]

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert result[0]["visual_role"] == "principal"
    assert result[1]["visual_role"] == "galeria"
    assert result[1].get("secuencia_progresiva") is not True


def test_roles_residuales_separan_codigo_de_diagrama_generico(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    code = {
        **_slide_timeline("code.jpg", 10),
        "ocr": "import tensorflow as tf model.fit epochs=10 batch_size=32",
        "visual_tipo": "codigo_formula_tabla",
    }
    diagram = {
        **_slide_timeline("diagram.jpg", 20),
        "ocr": "Architecture overview with several connected components",
        "visual_tipo": "diagrama_o_grafico_probable",
    }
    for name in ("code.jpg", "diagram.jpg"):
        Image.new("RGB", (320, 180), "white").save(img_dir / name)

    result = visual_materials.clasificar_roles_visuales(img_dir, [code, diagram])

    assert [slide["visual_role"] for slide in result] == ["principal", "galeria"]


def test_invitacion_administrativa_no_se_confunde_con_formula_o_slide(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    Image.new("RGB", (960, 540), "white").save(img_dir / "invite.jpg")
    invitation = {
        **_slide_timeline("invite.jpg", 10),
        "ocr": (
            "Invitation Inicio de Inteligencia Artificial 8:30am (GMT-5) "
            "Unirse con Google Meet 56 invitados Proponer otro horario"
        ),
        "transcript_ventana": "Several invited students cannot join the meeting.",
        "visual_tipo": "captura_generica",
    }

    result = visual_materials.clasificar_roles_visuales(img_dir, [invitation])

    assert visual_materials._looks_technical_content(invitation["ocr"]) is False
    assert visual_materials._looks_like_external_ui(invitation["ocr"]) is True
    assert result[0]["visual_role"] == "sin_valor"
    assert visual_materials._looks_technical_content(
        "weights = 0.25 and values [1, 2, 3]") is True


def test_biblioteca_de_presentaciones_es_ui_aunque_comparta_tema_docente(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    Image.new("RGB", (960, 540), "white").save(img_dir / "library.jpg")
    library = {
        **_slide_timeline("library.jpg", 10),
        "ocr": (
            "Presentaciones Presentaciones recientes Unidad II Ciencia de Datos "
            "Investigación en Deep Learning Introducción al Análisis de Datos "
            "Cualquiera 01 propietario Sistemas Expertos Inteligencia Artificial"
        ),
        "transcript_ventana": (
            "El profesor busca la presentación para continuar la explicación de XOR."
        ),
        "visual_tipo": "codigo_formula_tabla",
        "visual_estable": False,
    }

    result = visual_materials.clasificar_roles_visuales(img_dir, [library])

    assert visual_materials._looks_like_external_ui(library["ocr"]) is True
    assert result[0]["visual_role"] == "sin_valor"


def test_jitter_de_formula_presentador_y_resaltado_no_crea_tres_estados(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    variants = (
        ("xor_1.jpg", 5, 820, "h1 = 1 h2 = 0 h3 = 1 y = 1", False),
        ("xor_2.jpg", 18, 840, "h1 = 1 h2 = 0 y = 1 x1 = 0", False),
        ("xor_3.jpg", 34, 850, "h1 = 1 h2 = 0 h3 = 1 y = 1", True),
    )
    slides = []
    for index, (name, presenter_x, cursor_x, atoms, highlight) in enumerate(
            variants):
        _formula_frame(
            img_dir / name,
            presenter_x=presenter_x,
            cursor_x=cursor_x,
            highlight=highlight,
        )
        slides.append({
            **_slide_timeline(name, 10 + index * 5),
            "ocr": (
                "XOR hidden layer three neurons weighted sum truth table "
                f"linear classifiers cannot solve this {atoms}"
            ),
            "transcript_ventana": (
                "El profesor reitera que una capa oculta permite representar XOR, "
                "aunque todavía no había explicado el algoritmo de entrenamiento."
            ),
            "visual_tipo": "codigo_formula_tabla",
            "visual_estable": False,
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert all(slide["visual_role"] != "secuencia" for slide in result)
    assert sum(
        slide["visual_role"] in {"principal", "galeria"} for slide in result
    ) == 1
    assert sum(
        bool(slide.get("duplicado_pedagogico")) for slide in result[1:]
    ) >= 1


def test_formula_estable_realmente_actualizada_si_es_secuencia(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _formula_frame(
        img_dir / "loss_before.jpg", presenter_x=5, cursor_x=820,
        formula="loss = 1.0",
    )
    _formula_frame(
        img_dir / "loss_after.jpg", presenter_x=5, cursor_x=820,
        formula="loss = 0.5", highlight=True,
    )
    slides = [
        {
            **_slide_timeline("loss_before.jpg", 10),
            "ocr": (
                "Gradient descent parameter update learning rate objective "
                "function training iteration loss = 1.0"
            ),
            "transcript_ventana": "El profesor inicia una iteración de entrenamiento.",
            "visual_tipo": "codigo_formula_tabla",
            "visual_estable": True,
        },
        {
            **_slide_timeline("loss_after.jpg", 18),
            "ocr": (
                "Gradient descent parameter update learning rate objective "
                "function training iteration loss = 0.5"
            ),
            "transcript_ventana": (
                "La nueva iteración reduce la función objetivo: loss = 0.5."
            ),
            "visual_tipo": "codigo_formula_tabla",
            "visual_estable": True,
        },
    ]

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert [slide["visual_role"] for slide in result] == [
        "principal", "secuencia",
    ]
    assert result[1]["cambio_pedagogico"]["cambio_formula_confirmado"] is True


def test_frame_breve_ruidoso_no_se_publica_aunque_ocr_parezca_codigo(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    Image.new("RGB", (960, 540), "white").save(img_dir / "noise.jpg")
    frame = {
        **_slide_timeline("noise.jpg", 10),
        "ocr": "Jupyter import tensorflow model.fit epoch batch_size",
        "visual_tipo": "ruido_visual",
        "visual_estable": False,
    }

    result = visual_materials.clasificar_roles_visuales(img_dir, [frame])

    assert result[0]["visual_role"] == "sin_valor"


def test_lamina_narrativa_rica_mal_clasificada_como_tabla_llega_al_cuerpo(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    for name, shade in (("previous.jpg", 80), ("history.jpg", 210)):
        Image.new("RGB", (960, 540), (shade, shade, shade)).save(img_dir / name)
    previous = {
        **_slide_timeline("previous.jpg", 10),
        "ocr": "Perceptron limitations and hidden layers without training algorithm",
        "transcript_ventana": "The professor explains hidden layers and training.",
        "visual_tipo": "codigo_formula_tabla",
    }
    history_text = (
        "According to Samy Bengio neural networks were marginal while expert systems "
        "dominated. NeurIPS had 350 attendees, networks had fewer than 100 hidden "
        "units, one doctoral student worked on neural networks and no professor in "
        "that university specialized in the field. Yoshua worked at nearby McGill."
    )
    history = {
        **_slide_timeline("history.jpg", 20),
        "ocr": history_text,
        "transcript_ventana": history_text,
        "visual_tipo": "codigo_formula_tabla",
        "visual_estable": False,
    }

    result = visual_materials.clasificar_roles_visuales(
        img_dir, [previous, history])

    assert visual_materials._looks_technical_content(history_text) is False
    assert result[1]["visual_role"] == "principal"


def test_galeria_colapsa_jitter_ocr_y_conserva_pantalla_distinta(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    slides = []
    common = (
        "MNIST notebook Archivo Editar Ver Entorno ejecucion cargar datos crear red "
        "entrenar modelo evaluar exactitud"
    )
    for index, suffix in enumerate(("guardando", "guardardo", "guardado")):
        path = img_dir / f"jitter_{index}.jpg"
        Image.new("RGB", (960, 540), (220 + index, 220 + index, 220 + index)).save(path)
        slides.append({
            **_slide_timeline(path.name, 10 + index * 2),
            "ocr": f"{common} {suffix}",
            "transcript_ventana": "The instructor trains and evaluates the MNIST model.",
            "visual_tipo": "codigo_formula_tabla",
            "visual_estable": False,
        })
    distinct = img_dir / "confusion.jpg"
    Image.new("RGB", (960, 540), "#13294b").save(distinct)
    slides.append({
        **_slide_timeline(distinct.name, 18),
        "ocr": (
            "Confusion matrix precision recall false positives false negatives "
            "per class evaluation results"
        ),
        "transcript_ventana": (
            "The instructor explains confusion matrix precision and recall results."
        ),
        "visual_tipo": "codigo_formula_tabla",
        "visual_estable": False,
    })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert sum(slide["visual_role"] == "galeria" for slide in result[:3]) == 1
    assert sum(slide["visual_role"] == "sin_valor" for slide in result[:3]) == 2
    assert result[3]["visual_role"] in {"principal", "galeria"}


def test_navegacion_web_conserva_consulta_y_recurso_del_mismo_tema(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    texts = (
        "Google deep blue kasparov Modo IA Imágenes Todo Noticias Videos cortos",
        "Wikipedia Deep Blue chess computer Article Talk Read Edit Contents Kasparov",
        "Wikipedia Deep Blue chess computer Article history IBM Kasparov references",
    )
    slides = []
    for index, text in enumerate(texts):
        name = f"browser_{index}.jpg"
        Image.new("RGB", (960, 540), (210 - index * 20,) * 3).save(img_dir / name)
        slides.append({
            **_slide_timeline(name, 20 + index * 25),
            "ocr": text,
            "transcript_ventana": (
                "The professor explains how IBM Deep Blue played Kasparov using search."
            ),
            "visual_tipo": "slide_textual",
            "visual_estable": True,
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert sum(slide["visual_role"] == "galeria" for slide in result) == 2
    assert sum(slide["visual_role"] == "sin_valor" for slide in result) == 1
    assert all(slide.get("visual_role") != "secuencia" for slide in result)
    assert {
        slide.get("browsing_phase")
        for slide in result if slide["visual_role"] == "galeria"
    } == {"consulta", "recurso"}
    assert any(slide.get("representante") == "referencia_web_mas_informativa"
               for slide in result)


def test_busquedas_con_ocr_imperfecto_conservan_un_representante_por_consulta(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    texts = (
        (
            "nvidia_1.jpg",
            "CE)gle nvidia fundamentals of deep learning Resultados patrocinados "
            "NVIDIA Fundamentals of Deep Learning Workshop",
        ),
        (
            "nvidia_2.jpg",
            "Ceogle nvidia fundamentals of deep learning Resultados patrocinados "
            "NVIDIA course training learning objectives",
        ),
        (
            "car.jpg",
            "Ceogle definicion carro Visión general creada por IA 7 sitios "
            "RAE definición de automóvil Mostrar todo",
        ),
    )
    slides = []
    for index, (name, text) in enumerate(texts):
        Image.new("RGB", (960, 540), (220 - index * 20,) * 3).save(
            img_dir / name)
        slide = _slide_timeline(name, 20 + index * 25)
        slide.update({
            "ocr": text,
            "transcript_ventana": "",
            "sin_transcript_cercano": True,
            "visual_estable": False,
            "visual_tipo": "slide_textual",
        })
        slides.append(slide)

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)
    published = [
        slide for slide in result if slide["visual_role"] == "galeria"
    ]

    assert len(published) == 2
    assert all(slide.get("browsing_reference") for slide in result)
    assert any(
        "nvidia fundamentals" in slide.get("browsing_query", "").casefold()
        for slide in published
    )
    assert any(
        "carro" in slide.get("browsing_query", "").casefold()
        for slide in published
    )


def test_consulta_se_recupera_si_el_recorte_perdio_logo_pero_conserva_panel():
    text = (
        "deinicion carro Visión general creada por IA Un carro es un vehículo "
        "motorizado de cuatro ruedas Mostrar todo 7 sitios"
    )

    assert visual_materials._looks_like_browsing_reference(text) is True
    assert visual_materials._extract_browsing_query(text) == "deinicion carro"


def test_notebook_google_no_se_confunde_con_resultado_de_busqueda():
    text = (
        "Inteligencia Artificial - MNIST.ipynb Archivo Editar Ver Insertar "
        "Entorno de ejecución Comandos + Código + Texto Ejecutar todas "
        "Herramientas Ayuda Copiar en Drive Recursos del backend de Guogle Compute"
    )

    assert visual_materials._looks_like_browsing_reference(text) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "Geogle deep blue kasparov Moda IA Todo Imágenes Noticias "
            "Visión general creada por IA",
            "deep blue kasparov",
        ),
        (
            "Geogle definicion carro Moco IA Todo Imágenes Noticias "
            "Visión general creada por IA",
            "definicion carro",
        ),
        (
            "Geogle deeper blue algorithm M0d0 IA Tcxio Irnágenes "
            "Visión general creada por IA",
            "deeper blue algorithm",
        ),
        (
            "Geogle Stockfish is based on deep learning tv10d0 IA "
            "Visión general creada por IA",
            "Stockfish is based on deep learning",
        ),
        (
            "Geogle 5090 gpu cuda cores Irnggenes Nolit ias "
            "Visión general creada por IA",
            "5090 gpu cuda cores",
        ),
    ),
)
def test_consulta_descarta_pestanas_deformadas_por_ocr(text, expected):
    assert visual_materials._extract_browsing_query(text) == expected


def test_busquedas_distintas_no_se_colapsan_por_texto_denso_de_resultados(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    rows = (
        (
            "perceptron.jpg",
            10,
            (
                "Ceogle definicion perceptron Visión general creada por IA "
                "Wikipedia definición historia mostrar todo 7 sitios resultados "
                "investigación computadora modelo aprendizaje"
            ),
        ),
        (
            "car.jpg",
            40,
            (
                "Ceogle definicion carro Visión general creada por IA Wikipedia "
                "definición historia mostrar todo 7 sitios resultados vehículo "
                "automóvil transporte ruedas"
            ),
        ),
    )
    slides = []
    for name, moment, text in rows:
        Image.new("RGB", (960, 540), "#eceff4").save(img_dir / name)
        slides.append({
            **_slide_timeline(name, moment),
            "ocr": text,
            "transcript_ventana": "",
            "sin_transcript_cercano": True,
            "visual_tipo": "slide_textual",
            "visual_estable": False,
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert [slide["visual_role"] for slide in result] == ["galeria", "galeria"]
    assert result[0]["browsing_query"] == "definicion perceptron"
    assert result[1]["browsing_query"] == "definicion carro"


def test_slide_rica_breve_a_velocidad_alta_se_conserva_sin_forzarla_a_principal(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    rows = (
        (
            "perceptron.jpg",
            "Perceptron weighted inputs activation threshold linear separation",
            "The instructor explains the perceptron activation threshold.",
        ),
        (
            "gradient.jpg",
            "Gradient descent loss surface learning rate parameter update iteration",
            "The instructor explains gradient descent and the learning rate.",
        ),
    )
    slides = []
    for index, (name, text, transcript) in enumerate(rows):
        Image.new("RGB", (960, 540), (205 - index * 25,) * 3).save(
            img_dir / name)
        slide = _slide_timeline(name, 10 + index * 3)
        slide.update({
            "ocr": text,
            "transcript_ventana": transcript,
            "visual_estable": False,
            "t_fin_rel": 12 + index * 3,
            "intervalos_visuales": [{
                "inicio": 10 + index * 3,
                "fin": 12 + index * 3,
            }],
            "visual_tipo": "slide_textual",
        })
        slides.append(slide)

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert all(
        slide["visual_role"] in {"principal", "galeria"} for slide in result)
    assert all(slide["visual_role"] != "secuencia" for slide in result)


def test_navegacion_se_detecta_en_ocr_completo_sin_contaminar_tema_principal(
        tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    rows = (
        (
            "search.jpg",
            "Deep Blue IBM Kasparov chess computer search strategy",
            "Google Modo IA Imágenes Todo Noticias Videos cortos Deep Blue IBM",
        ),
        (
            "article.jpg",
            "Deep Blue IBM Kasparov chess computer search strategy history",
            "Wikipedia Article Talk Read Edit Contents Deep Blue IBM references",
        ),
    )
    slides = []
    for index, (name, principal, complete) in enumerate(rows):
        Image.new("RGB", (960, 540), (205 - index * 25,) * 3).save(img_dir / name)
        slides.append({
            **_slide_timeline(name, 30 + index * 20),
            "ocr": principal,
            "ocr_completo": complete,
            "transcript_ventana": (
                "The professor explains IBM Deep Blue and Kasparov as an example."
            ),
            "visual_tipo": "slide_textual",
            "visual_estable": True,
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert all(slide.get("browsing_reference") for slide in result)
    assert sum(slide["visual_role"] == "galeria" for slide in result) == 1
    assert sum(slide["visual_role"] == "sin_valor" for slide in result) == 1
    topic_tokens = visual_materials._browsing_topic_tokens(result[0])
    assert {"blue", "chess", "computer", "deep", "kasparov", "strategy"} <= (
        topic_tokens
    )
    assert not {"google", "imagenes", "noticias", "videos"} & topic_tokens


def test_navegacion_tecnica_no_se_convierte_en_formula_principal(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    path = img_dir / "search_technical.jpg"
    Image.new("RGB", (960, 540), "#f7f7f7").save(path)
    slide = {
        **_slide_timeline(path.name, 20),
        "ocr": (
            "Google deep blue kasparov Modo IA Imágenes Noticias Videos Web "
            "Alpha-Beta Pruning 200 million chess positions per second"
        ),
        "transcript_ventana": (
            "The professor searches for Deep Blue and explains alpha-beta pruning."
        ),
        "visual_tipo": "codigo_formula_tabla",
        "visual_estable": False,
    }

    result = visual_materials.clasificar_roles_visuales(img_dir, [slide])

    assert result[0]["browsing_reference"] is True
    assert result[0]["browsing_reference_origin"] == "directa"
    assert result[0]["visual_role"] == "galeria"


def test_sesion_web_propaga_estados_intermedios_pero_no_codigo_real(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    rows = (
        (
            "search.jpg",
            10,
            "Google Deep Blue Kasparov Modo IA Imágenes Noticias Videos Web IBM chess",
            False,
        ),
        (
            "answer.jpg",
            45,
            (
                "Deep Blue IBM Kasparov chess decision tree alpha beta pruning "
                "evaluation rules processors working together branches opponent "
                "moves outcome computer history explanation"
            ),
            False,
        ),
        (
            "sources.jpg",
            75,
            (
                "Deep Blue IBM Kasparov chess alpha beta pruning Haz una pregunta "
                "11 sitios Wikipedia IBM Mostrar todo"
            ),
            False,
        ),
        (
            "code.jpg",
            82,
            (
                "import chess def minimax(board, depth): score = evaluate(board) "
                "return score Deep Blue Kasparov alpha beta pruning"
            ),
            False,
        ),
        (
            "slide.jpg",
            88,
            (
                "Deep Blue and Kasparov Search trees Minimax Alpha-Beta Pruning "
                "historical example of explicit search"
            ),
            True,
        ),
    )
    slides = []
    for name, moment, ocr, stable in rows:
        Image.new("RGB", (960, 540), "#eceff4").save(img_dir / name)
        slides.append({
            **_slide_timeline(name, moment),
            "ocr": ocr,
            "transcript_ventana": (
                "The professor explains Deep Blue, Kasparov and alpha-beta pruning."
            ),
            "visual_tipo": "codigo_formula_tabla",
            "visual_estable": stable,
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)
    by_name = {slide["archivo"]: slide for slide in result}

    assert by_name["answer.jpg"]["browsing_reference_origin"] == (
        "sesion_tematico_temporal"
    )
    assert by_name["answer.jpg"]["visual_role"] in {"galeria", "sin_valor"}
    assert by_name["code.jpg"].get("browsing_reference") is not True
    assert by_name["slide.jpg"].get("browsing_reference") is not True


def test_sesion_web_no_absorbe_notebook_con_cromo_de_aplicacion(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    rows = (
        (
            "search.jpg",
            10,
            "Google MNIST dataset Modo IA Imágenes Noticias Videos Web",
            "slide_textual",
        ),
        (
            "notebook.jpg",
            45,
            (
                "MNIST.ipynb Archivo Editar Insertar Entorno de ejecución "
                "Herramientas Comandos Ejecutar todas Copiar en Drive train_set "
                "validation_set dataset tensors labels"
            ),
            "codigo_formula_tabla",
        ),
    )
    slides = []
    for name, moment, ocr, visual_type in rows:
        Image.new("RGB", (960, 540), "#eceff4").save(img_dir / name)
        slides.append({
            **_slide_timeline(name, moment),
            "ocr": ocr,
            "transcript_ventana": (
                "El profesor carga MNIST y separa los datos de entrenamiento "
                "y validación."
            ),
            "visual_tipo": visual_type,
            "visual_estable": False,
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)
    by_name = {slide["archivo"]: slide for slide in result}

    assert by_name["search.jpg"]["browsing_reference"] is True
    assert by_name["notebook.jpg"].get("browsing_reference") is not True


def test_navegacion_web_de_temas_distintos_no_se_colapsa_por_ser_wikipedia(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    rows = (
        ("deep_blue.jpg", "Wikipedia Deep Blue chess computer Article IBM Kasparov"),
        ("crispr.jpg", "Wikipedia CRISPR gene editing Article Cas9 genome biology"),
    )
    slides = []
    for index, (name, text) in enumerate(rows):
        Image.new("RGB", (960, 540), (190 + index * 30,) * 3).save(img_dir / name)
        slides.append({
            **_slide_timeline(name, 20 + index * 40),
            "ocr": text,
            "transcript_ventana": text,
            "visual_tipo": "slide_textual",
            "visual_estable": True,
        })

    result = visual_materials.clasificar_roles_visuales(img_dir, slides)

    assert [slide["visual_role"] for slide in result] == ["galeria", "galeria"]


def test_pdf_se_renderiza_como_material_limpio(tmp_path):
    pytest.importorskip("pypdfium2")
    pdf = tmp_path / "slides.pdf"
    page = Image.new("RGB", (960, 540), "white")
    ImageDraw.Draw(page).text((80, 80), "Synthetic official slide", fill="black")
    page.save(pdf, "PDF")
    img_dir = tmp_path / "img"

    pages, report = visual_materials.preparar_materiales_oficiales([pdf], img_dir)

    assert report["paginas_preparadas"] == 1
    assert len(pages) == 1
    assert (img_dir / pages[0]["archivo"]).is_file()
    assert pages[0]["pagina"] == 1
    assert pages[0]["tipo_fuente"] == "pdf"


def test_preflight_pdf_sin_renderer_da_comando_windows_accionable(
        monkeypatch, tmp_path):
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% synthetic preflight only\n")
    real_import = builtins.__import__

    def missing_pdfium(name, *args, **kwargs):
        if name == "pypdfium2":
            raise ModuleNotFoundError("synthetic missing pypdfium2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_pdfium)

    pages, report = visual_materials.preparar_materiales_oficiales(
        [pdf], tmp_path / "derived")

    assert pages == []
    assert report["paginas_preparadas"] == 0
    assert "pypdfium2==5.11.0" in report["errores"][0]
    assert r".\.venv\Scripts\python.exe -m pip install -r requirements.txt" \
        in report["errores"][0]
    assert not (tmp_path / "derived").exists()


def test_limpieza_protege_material_y_captura_de_aparicion(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    for name in ("material.jpg", "cap_original.jpg", "descartable.jpg"):
        Image.new("RGB", (10, 10), "white").save(img_dir / name)
    (tmp_path / "leccion_final.md").write_text("# Leccion\n", encoding="utf-8")
    (tmp_path / "transcript.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "visual_material_manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "paginas": [{
            "archivo": "material.jpg",
            "apariciones": [{"archivo": "cap_original.jpg", "t_rel": 12.0}],
        }],
    }), encoding="utf-8")

    refs = post_procesar._referencias_img(tmp_path)

    assert {"material.jpg", "cap_original.jpg"} <= refs
    assert "descartable.jpg" not in refs


def test_preparacion_idempotente_preserva_o_reinicia_apariciones(tmp_path):
    deck = tmp_path / "deck"
    img_dir = tmp_path / "img"
    deck.mkdir()
    _slide_sintetica(deck / "001.png")
    manifest = tmp_path / "visual_material_manifest.json"
    pages, _ = visual_materials.preparar_materiales_oficiales(
        [deck], img_dir, manifest_path=manifest)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["paginas"][0]["apariciones"] = [{"archivo": "cap.jpg", "t_rel": 5}]
    manifest.write_text(json.dumps(data), encoding="utf-8")

    visual_materials.preparar_materiales_oficiales(
        [deck], img_dir, manifest_path=manifest)
    preserved = json.loads(manifest.read_text(encoding="utf-8"))
    assert preserved["paginas"][0]["apariciones"][0]["archivo"] == "cap.jpg"

    visual_materials.preparar_materiales_oficiales(
        [deck], img_dir, manifest_path=manifest, reset_appearances=True)
    reset = json.loads(manifest.read_text(encoding="utf-8"))
    assert reset["paginas"][0]["apariciones"] == []
    assert pages[0]["material_id"] == reset["paginas"][0]["material_id"]


def test_consolidar_parte_usa_limpia_y_no_publica_overlays_repetidos(
        monkeypatch, tmp_path):
    contenido = tmp_path / "contenido"
    class_dir = contenido / "curso" / "clases" / "CLASE_01"
    img_dir = class_dir / "img"
    deck_dir = tmp_path / "deck"
    img_dir.mkdir(parents=True)
    deck_dir.mkdir()
    source = deck_dir / "001.png"
    _slide_sintetica(source)
    (class_dir / "session_state.json").write_text(json.dumps({
        "curso": "curso", "nombre": "Clase visual", "origen": "video", "inicio": 0,
    }), encoding="utf-8")
    transcript = [{
        "t": 0, "fin": 80, "parte": 1,
        "texto": "The instructor explains neural network loss gradients and updates. " * 12,
    }]
    (class_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(row) for row in transcript) + "\n", encoding="utf-8")
    rows = []
    for idx in range(3):
        name = f"cap_{idx}.jpg"
        _captura_con_overlay(source, img_dir / name, idx)
        rows.append({
            "t": 10 + idx * 12, "archivo": name, "parte": 1,
            "ocr": "Neural network training loss gradients parameter update",
        })
    (class_dir / "img_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(post_procesar.config, "CONTENIDO_DIR", contenido)

    def fake_llm(_d, messages, max_tokens, etiqueta, prompt_version):
        del max_tokens, prompt_version
        if etiqueta.startswith("post-chunk"):
            refs = [
                line.split(": ", 1)[1].split(" ", 1)[0]
                for line in messages[-1]["content"].splitlines()
                if line.startswith("SLIDE ")
            ]
            return "\n".join(
                f"[[IMG:{ref}|Official explanation of gradients and updates]]"
                for ref in refs)
        return messages[-1]["content"]

    monkeypatch.setattr(post_procesar, "preguntar_cacheado", fake_llm)

    result = post_procesar.consolidar_parte(
        class_dir, 1, 0, 80, {"origen": "video"}, forzar=True,
        materiales=[str(deck_dir)],
    )

    lesson = result.read_text(encoding="utf-8")
    clean_refs = [ref for ref in post_procesar.extraer_referencias_img(lesson)
                  if ref.startswith("material_oficial_")]
    assert len(clean_refs) == 1
    assert all(f"img/cap_{idx}.jpg" not in lesson for idx in range(3))
    report = post_procesar.consolidar_parte.ultimo_reporte
    assert report["estados_emparejados_material"] == 1
    # El timeline sintético ya colapsa estos frames por dHash; el contrato entre
    # timeline y material oficial debe conservar igualmente las tres apariciones.
    assert report["duplicados_material_colapsados"] == 0
    manifest = json.loads(
        (class_dir / "visual_material_manifest.json").read_text(encoding="utf-8"))
    assert {item["archivo"] for item in manifest["paginas"][0]["apariciones"]} == {
        "cap_0.jpg", "cap_1.jpg", "cap_2.jpg",
    }


def test_benchmark_material_es_read_only_y_ejercita_pipeline(tmp_path):
    class_dir = tmp_path / "class"
    img_dir = class_dir / "img"
    deck_dir = tmp_path / "deck"
    img_dir.mkdir(parents=True)
    deck_dir.mkdir()
    source = deck_dir / "001.png"
    _slide_sintetica(source)
    _captura_con_overlay(source, img_dir / "cap.jpg", 0)
    (class_dir / "session_state.json").write_text(json.dumps({
        "curso": "synthetic", "nombre": "Benchmark", "origen": "video", "inicio": 0,
    }), encoding="utf-8")
    (class_dir / "marcas.jsonl").write_text(
        json.dumps({"t": 60, "tipo": "fin", "parte": 1, "nota": "synthetic"}) + "\n",
        encoding="utf-8")
    (class_dir / "transcript.jsonl").write_text(json.dumps({
        "t": 0, "fin": 60, "parte": 1,
        "texto": "Neural network training loss gradients parameter update.",
    }) + "\n", encoding="utf-8")
    (class_dir / "img_log.jsonl").write_text(json.dumps({
        "t": 10, "archivo": "cap.jpg", "parte": 1,
        "ocr": "Neural network training loss gradients parameter update",
    }) + "\n", encoding="utf-8")

    result = benchmark_visual_materials.run_benchmark(
        class_dir, [deck_dir], expected_pages="1")

    assert result["read_only_verified"] is True
    assert result["input_fingerprint_before"] == result["input_fingerprint_after"]
    assert result["official_pages_detected"] == [1]
    assert result["expected_page_recall"] == 1.0
    assert not (class_dir / "visual_material_manifest.json").exists()
    assert not any(path.name.startswith("material_oficial_") for path in img_dir.iterdir())
