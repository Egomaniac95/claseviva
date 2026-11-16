import base64
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from scripts import optimizar_imagenes
from src import (
    captura_pantalla,
    image_optimization,
    lesson_export,
    panel,
    study_package,
)


def _jpeg():
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    for y in range(20, 340, 20):
        draw.text((20, y), "Contenido de la diapositiva: 0123456789", fill="black")
    output = BytesIO()
    exif = Image.Exif()
    exif[274] = 1
    image.save(output, "JPEG", quality=85, exif=exif, icc_profile=b"synthetic-profile")
    return output.getvalue()


def _class(tmp_path):
    d = tmp_path / "clase"
    (d / "img").mkdir(parents=True)
    (d / "img" / "example.jpg").write_bytes(_jpeg())
    body = '# Tema\n\n<img src="img/example.jpg">\n\nTexto aceptado.\n'
    meta = {"recording_parts": [{"part": 1}],
            "images": study_package.image_inventory(d, body)}
    (d / "leccion_final.md").write_text(study_package.metadata_comment(meta) + "\n\n" + body,
                                        encoding="utf-8")
    return d


def test_webp_conversion_preserves_dimensions_exif_and_profile():
    original = _jpeg()
    optimized, report = image_optimization.optimize_bytes(original)
    assert report["dimensions_verified"] and report["optimized"]
    assert report["quality"] == 85 and len(optimized) < len(original)
    with Image.open(BytesIO(original)) as a, Image.open(BytesIO(optimized)) as b:
        assert b.format == "WEBP"
        assert a.size == b.size
        assert a.getexif() == b.getexif()
        assert a.info["icc_profile"] == b.info["icc_profile"]
    second, check = image_optimization.optimize_bytes(optimized)
    assert second == optimized and check["reason"] == "already_webp"


def test_png_alpha_survives_exactly():
    image = Image.new("RGBA", (64, 64), (23, 72, 108, 0))
    image.putpixel((16, 16), (45, 100, 200, 127))
    output = BytesIO()
    image.save(output, "PNG", compress_level=0)
    optimized, report = image_optimization.optimize_bytes(output.getvalue())
    assert report["dimensions_verified"]
    with Image.open(BytesIO(optimized)) as result:
        assert image.getchannel("A").tobytes() == result.getchannel("A").tobytes()


def test_batch_updates_references_hashes_and_html_without_changing_explanation(tmp_path):
    d = _class(tmp_path)
    before = study_package.strip_metadata((d / "leccion_final.md").read_text(encoding="utf-8"))
    result = optimizar_imagenes.optimize_directory(d / "img", workers=2)
    assert result["images_optimized"] == result["images_verified"] == 1
    assert not (d / "img" / "example.jpg").exists()
    assert (d / "img" / "example.webp").is_file()
    assert study_package.validate_candidate(d)["image_hashes_verified"]
    after = study_package.strip_metadata((d / "leccion_final.md").read_text(encoding="utf-8"))
    assert after == before.replace("img/example.jpg", "img/example.webp")
    page = (d / "leccion_final.html").read_text(encoding="utf-8")
    assert 'src="img/example.webp"' in page and "data:image/" not in page
    second = optimizar_imagenes.optimize_directory(d / "img")
    assert second["images_optimized"] == 0
    assert not list(tmp_path.glob(".image-optimization-*"))


def test_failure_restores_original_images_markdown_and_html(tmp_path, monkeypatch):
    d = _class(tmp_path)
    (d / "leccion_final.html").write_text("existing html", encoding="utf-8")
    paths = [d / "leccion_final.md", d / "leccion_final.html", d / "img" / "example.jpg"]
    before = {p: p.read_bytes() for p in paths}

    def fail(*args, **kwargs):
        raise RuntimeError("export failed")

    monkeypatch.setattr(lesson_export, "export_html", fail)
    with pytest.raises(RuntimeError, match="export failed"):
        optimizar_imagenes.optimize_directory(d / "img")
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not (d / "img" / "example.webp").exists()
    assert study_package.validate_candidate(d)["ok"]
    assert not list(tmp_path.glob(".image-optimization-*"))


def test_batch_rejects_altered_inventory_before_touching_files(tmp_path):
    d = _class(tmp_path)
    path = d / "img" / "example.jpg"
    path.write_bytes(path.read_bytes() + b"tampered")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="validar antes"):
        optimizar_imagenes.optimize_directory(d / "img")
    assert path.read_bytes() == before


def test_download_embeds_webp_without_overwriting_lightweight_html(tmp_path, monkeypatch):
    d = _class(tmp_path)
    optimizar_imagenes.optimize_directory(d / "img")
    saved = (d / "leccion_final.html").read_bytes()
    monkeypatch.setattr(panel, "_clase", lambda: d)
    response = panel.app.test_client().get("/leccion_final.html")
    assert response.status_code == 200
    assert b"data:image/webp;base64," in response.data
    assert (d / "leccion_final.html").read_bytes() == saved
    assert panel.app.test_client().get("/img/example.webp").mimetype == "image/webp"


def test_new_capture_uses_webp_with_same_dimensions(tmp_path):
    image = Image.open(BytesIO(_jpeg())).convert("RGB")
    data = captura_pantalla.codificar_webp(image)
    with Image.open(BytesIO(data)) as result:
        assert result.format == "WEBP" and result.size == image.size
    path = tmp_path / "capture.webp"
    captura_pantalla.guardar_liviana(image, path)
    assert Image.open(path).format == "WEBP"


def test_html_only_class_updates_both_embedded_and_relative_images(tmp_path):
    d = _class(tmp_path)
    (d / "leccion_final.md").unlink()
    encoded = base64.b64encode((d / "img" / "example.jpg").read_bytes()).decode("ascii")
    html = d / "leccion_final.html"
    html.write_text(f'<h1>Texto aceptado</h1><img src="data:image/jpeg;base64,{encoded}"><img src="img/example.jpg">',
                    encoding="utf-8")
    result = optimizar_imagenes.optimize_directory(d / "img")
    assert result["images_verified"] == 1
    assert html.read_text(encoding="utf-8") == '<h1>Texto aceptado</h1><img src="img/example.webp"><img src="img/example.webp">'


def test_collision_never_overwrites_existing_webp(tmp_path):
    d = _class(tmp_path)
    target = d / "img" / "example.webp"
    target.write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="Colisión"):
        optimizar_imagenes.optimize_directory(d / "img")
    assert target.read_bytes() == b"existing"
    assert (d / "img" / "example.jpg").is_file()


def test_animated_images_are_preserved():
    output = BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, "GIF", save_all=True,
        append_images=[Image.new("RGB", (16, 16), "blue")], duration=100)
    result, report = image_optimization.optimize_bytes(output.getvalue())
    assert result == output.getvalue() and report["reason"] == "animation_preserved"
