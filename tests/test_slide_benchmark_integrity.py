import sys
import types
from pathlib import Path

from PIL import Image

from scripts import benchmark_slide_recall


def _image(path: Path, shade: int) -> None:
    Image.new("RGB", (80, 50), (shade, shade, shade)).save(path)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    deck = tmp_path / "deck"
    image_dir = tmp_path / "class" / "img"
    deck.mkdir()
    image_dir.mkdir(parents=True)
    _image(deck / "page_1.png", 10)
    _image(deck / "page_2.png", 120)
    _image(deck / "page_10.png", 240)
    _image(image_dir / "capture.jpg", 120)
    return image_dir.parent, deck


def test_deck_pages_use_natural_numeric_order(tmp_path):
    class_dir, deck = _fixture(tmp_path)

    result = benchmark_slide_recall.evaluar(
        class_dir, deck, shown_pages="1-3", use_ocr=False,
        visual_threshold=1.0,
    )

    assert [item["deck_file"] for item in result["capture_matches"]] == [
        "page_1.png", "page_2.png", "page_10.png",
    ]


def test_ocr_enabled_is_false_when_engine_cannot_load(monkeypatch, tmp_path):
    class_dir, deck = _fixture(tmp_path)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)

    result = benchmark_slide_recall.evaluar(
        class_dir, deck, shown_pages="1", use_ocr=True,
    )

    assert result["thresholds"]["ocr_requested"] is True
    assert result["thresholds"]["ocr_enabled"] is False
    assert result["thresholds"]["ocr_successful_calls"] == 0


def test_ocr_enabled_requires_successful_inference(monkeypatch, tmp_path):
    class_dir, deck = _fixture(tmp_path)
    module = types.ModuleType("rapidocr_onnxruntime")

    class FakeRapidOCR:
        def __call__(self, _path):
            return [[None, "recognized text", 0.99]], None

    module.RapidOCR = FakeRapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", module)

    result = benchmark_slide_recall.evaluar(
        class_dir, deck, shown_pages="1", use_ocr=True,
    )

    assert result["thresholds"]["ocr_requested"] is True
    assert result["thresholds"]["ocr_enabled"] is True
    assert result["thresholds"]["ocr_successful_calls"] > 0
