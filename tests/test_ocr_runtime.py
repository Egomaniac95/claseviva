from __future__ import annotations

import sys
import types

import pytest
from PIL import Image, ImageDraw, ImageFont

from src import config, ocr


def _reset_motor(monkeypatch) -> None:
    monkeypatch.setattr(ocr, "_motor", None)
    monkeypatch.setattr(ocr, "_motor_info", {
        "requested": config.OCR_ENGINE,
        "engine": "uninitialized",
        "operational": None,
    })


def test_rapidocr_fallback_receives_bounded_onnx_threads(monkeypatch) -> None:
    created = []

    class FakeRapidOCR:
        def __init__(self, **kwargs):
            created.append(kwargs)

    rapid = types.ModuleType("rapidocr_onnxruntime")
    rapid.RapidOCR = FakeRapidOCR
    ort = types.ModuleType("onnxruntime")
    ort.get_available_providers = lambda: ["CPUExecutionProvider"]
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", rapid)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setattr(config, "OCR_ENGINE", "rapidocr")
    monkeypatch.setattr(config, "OCR_RAPID_INTRA_THREADS", 2)
    monkeypatch.setattr(config, "OCR_RAPID_INTER_THREADS", 1)
    _reset_motor(monkeypatch)

    motor = ocr._iniciar()
    state = ocr.estado_motor()

    assert motor is not False
    assert created == [{
        "intra_op_num_threads": 2,
        "inter_op_num_threads": 1,
    }]
    assert state["engine"] == "rapidocr_onnxruntime"
    assert state["intra_threads"] == 2
    assert state["inter_threads"] == 1
    assert state["operational"] is True


def test_cerrar_motor_libera_backend_y_es_idempotente(monkeypatch) -> None:
    calls = []

    class FakeBackend:
        def close(self):
            calls.append("close")

    monkeypatch.setattr(ocr, "_motor", FakeBackend())
    monkeypatch.setattr(ocr, "_motor_info", {
        "requested": "windows",
        "engine": "windows_media_ocr",
        "operational": True,
    })

    ocr.cerrar_motor()
    ocr.cerrar_motor()

    assert calls == ["close"]
    assert ocr._motor is None
    assert ocr.estado_motor()["engine"] == "uninitialized"


@pytest.mark.skipif(sys.platform != "win32", reason="requiere Windows.Media.Ocr")
def test_windows_native_ocr_operates_in_memory_without_gpu(monkeypatch) -> None:
    monkeypatch.setattr(config, "OCR_ENGINE", "windows")
    monkeypatch.setattr(config, "OCR_WINDOWS_LANGUAGES", ("es-MX", "en-US"))
    _reset_motor(monkeypatch)
    state = ocr.estado_motor(iniciar=True)
    if state.get("engine") != "windows_media_ocr":
        pytest.skip(f"Windows OCR no operativo: {state}")

    image = Image.new("RGB", (900, 300), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("arial.ttf", 44)
    draw.text((40, 45), "Descenso de gradiente", font=font, fill="black")
    draw.text((40, 145), "w = w - eta * gradiente", font=font, fill="black")

    try:
        text = ocr.texto_de_pil(image).lower()
        state = ocr.estado_motor()

        assert "gradiente" in text
        assert "eta" in text
        assert state["provider"] == "Windows.Media.Ocr"
        assert state["operational"] is True
        assert "cuda" not in state["provider"].lower()
    finally:
        ocr.cerrar_motor()
