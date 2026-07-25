from pathlib import Path

from PIL import Image, ImageDraw

from scripts.benchmark_slide_recall import evaluar, parse_pages


def _page(path: Path, variant: int) -> None:
    image = Image.new("RGB", (240, 140), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10 + variant * 8, 10, 80 + variant * 12, 55), fill="black")
    draw.line((20, 80 + variant * 5, 220, 110 - variant * 3), fill="black", width=5)
    draw.ellipse((150 - variant * 4, 20, 220, 70 + variant * 5), outline="black", width=4)
    image.save(path)


def test_parse_pages_rangos():
    assert parse_pages("1,3-5,7", 8) == [1, 3, 4, 5, 7]


def test_benchmark_mide_recall_precision_y_duplicados_sinteticos(tmp_path):
    deck = tmp_path / "deck"
    clase = tmp_path / "clase"
    img_dir = clase / "img"
    deck.mkdir()
    img_dir.mkdir(parents=True)
    for i in range(1, 4):
        _page(deck / f"page_{i:02d}.png", i)
    _page(img_dir / "captura_1.jpg", 1)
    _page(img_dir / "captura_2.jpg", 2)
    _page(img_dir / "captura_2_dup.jpg", 2)
    _page(img_dir / "ruido.jpg", 8)
    (clase / "leccion_final.md").write_text(
        "![uno](img/captura_1.jpg)\n"
        "![dos](img/captura_2.jpg)\n"
        "![dos dup](img/captura_2_dup.jpg)\n"
        "![ruido](img/ruido.jpg)\n",
        encoding="utf-8",
    )

    result = evaluar(
        clase, deck, shown_pages="1-3", use_ocr=False,
        visual_threshold=0.98)

    assert result["capture_recall"] == 0.6667
    assert result["selected_recall"] == 0.6667
    assert result["missing_from_capture"] == [3]
    assert result["selected_duplicate_rate"] == 0.25
    assert result["selected_precision"] == 0.75


def test_benchmark_no_reutiliza_una_captura_para_dos_paginas(tmp_path):
    deck = tmp_path / "deck"
    clase = tmp_path / "clase"
    img_dir = clase / "img"
    deck.mkdir()
    img_dir.mkdir(parents=True)
    _page(deck / "page_01.png", 1)
    _page(deck / "page_02.png", 1)
    _page(img_dir / "captura_unica.jpg", 1)
    (clase / "leccion_final.md").write_text(
        "![unica](img/captura_unica.jpg)\n", encoding="utf-8")

    result = evaluar(
        clase, deck, shown_pages="1-2", use_ocr=False,
        visual_threshold=0.98)

    assert result["capture_recall"] == 0.5
    assert result["selected_recall"] == 0.5
    assert len([m for m in result["selected_matches"] if m["matched"]]) == 1
