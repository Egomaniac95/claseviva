"""Contrato focalizado del pipeline visual adaptativo y sin cola.

Supuestos de API acordados con la implementacion:

* ``VisualEvidencePipeline(img_dir, img_log_path, timeline_path, *,
  ocr_fn=None, save_jpeg=True, gate=None, metrics=None)`` es sincrono;
* ``process(img, *, epoch, parte, state, commit_validator=None)`` devuelve
  ``saved``, ``heartbeat``, ``ocr_ran``, ``discarded`` y ``reason`` como
  atributos o claves;
* ``close(*, epoch, parte, motivo)`` es idempotente y ``snapshot()`` expone
  contadores, ``closed`` y una seccion ``queue`` que declara explicitamente
  que no existe cola ni trabajo pendiente.

Estas pruebas usan solo imagenes sinteticas y ``tmp_path``. No certifican la
captura MSS, OCR real, monitores ni rendimiento del hardware.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src import captura_pantalla, post_procesar


def _filas(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _campo(resultado, nombre: str):
    if isinstance(resultado, Mapping):
        assert nombre in resultado, f"resultado sin campo {nombre!r}"
        return resultado[nombre]
    assert hasattr(resultado, nombre), f"resultado sin atributo {nombre!r}"
    return getattr(resultado, nombre)


def _estado_grabando(*, audio: str = "activo", epoch: float = 0.0) -> dict:
    return {
        "estado": "grabando",
        "parte": 1,
        "auto_pausa": False,
        "audio_requires_attention": False,
        "audio_ultimo_estado": audio,
        "audio_silencio_chunks": 999 if audio == "silencio" else 0,
        "audio_ultimo_t": float(epoch),
    }


def _slide(variante: int) -> Image.Image:
    """Dos layouts con cambio grande, ambos con contenido visual util."""
    img = Image.new("RGB", (320, 180), "white")
    draw = ImageDraw.Draw(img)
    if variante % 2:
        draw.rectangle((18, 20, 140, 158), fill=(20, 30, 70))
        draw.rectangle((175, 28, 300, 50), fill=(30, 120, 210))
        draw.line((175, 85, 300, 145), fill=(10, 10, 10), width=8)
    else:
        draw.rectangle((180, 20, 302, 158), fill=(70, 25, 20))
        draw.rectangle((20, 28, 145, 50), fill=(30, 170, 90))
        draw.line((20, 145, 145, 85), fill=(10, 10, 10), width=8)
    return img


def _crear_pipeline(tmp_path: Path, **kwargs):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    img_log = tmp_path / "img_log.jsonl"
    timeline = tmp_path / "visual_timeline.jsonl"
    pipeline = captura_pantalla.VisualEvidencePipeline(
        img_dir, img_log, timeline, save_jpeg=True, **kwargs)
    return pipeline, img_dir, img_log, timeline


def _assert_sin_cola(snapshot: dict) -> None:
    cola = snapshot["queue"]
    assert cola["mode"] == "none"
    assert cola["capacity"] == 0
    assert cola["max_depth"] == 0
    assert cola["final_depth"] == 0
    assert cola["dropped"] == 0
    assert cola["drained"] is True
    assert cola["max_in_flight"] <= 1


def test_estabilidad_larga_hace_un_jpeg_un_ocr_y_heartbeats_espaciados(
        monkeypatch, tmp_path):
    monkeypatch.setattr(captura_pantalla.config, "CAPTURA_HEARTBEAT_SEG", 10.0)
    monkeypatch.setattr(
        captura_pantalla.config, "CAPTURA_OCR_INTERVALO_MIN", 10.0)
    monkeypatch.setattr(
        captura_pantalla.config, "CAPTURA_OCR_REFRESH_SEG", 10_000.0)
    llamadas_ocr: list[int] = []

    pipeline, img_dir, img_log, timeline = _crear_pipeline(
        tmp_path,
        ocr_fn=lambda _img: llamadas_ocr.append(1) or "Slide estable",
    )
    estable = _slide(0)
    resultados = [
        pipeline.process(
            estable.copy(), epoch=float(epoch), parte=1,
            state=_estado_grabando(epoch=epoch),
        )
        for epoch in range(100, 141)
    ]
    pipeline.close(epoch=141.0, parte=1, motivo="fin_test")

    assert sum(bool(_campo(r, "saved")) for r in resultados) == 1
    assert sum(bool(_campo(r, "ocr_ran")) for r in resultados) == 1
    assert sum(bool(_campo(r, "heartbeat")) for r in resultados) == 4
    assert not any(bool(_campo(r, "discarded")) for r in resultados)
    assert llamadas_ocr == [1]
    assert len(list(img_dir.glob("*.webp"))) == 1
    assert len(_filas(img_log)) == 1
    eventos = _filas(timeline)
    assert [fila["t"] for fila in eventos if fila["evento"] == "heartbeat"] == [
        110, 120, 130, 140,
    ]
    assert [fila["evento"] for fila in eventos].count("fin") == 1

    snapshot = pipeline.snapshot()
    assert snapshot["frames_observed"] == 41
    assert snapshot["frames_saved"] == 1
    assert snapshot["ocr_runs"] == 1
    assert snapshot["heartbeats"] == 4
    _assert_sin_cola(snapshot)


def test_cambio_visual_relevante_se_conserva_durante_silencio_sin_forzar_ocr(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        captura_pantalla.config, "CAPTURA_OCR_INTERVALO_MIN", 60.0)
    monkeypatch.setattr(captura_pantalla.config, "CAPTURA_HEARTBEAT_SEG", 30.0)
    llamadas_ocr: list[int] = []
    pipeline, img_dir, img_log, _timeline = _crear_pipeline(
        tmp_path,
        ocr_fn=lambda _img: llamadas_ocr.append(1) or "Contenido tecnico",
    )

    inicial = pipeline.process(
        _slide(0), epoch=200.0, parte=1,
        state=_estado_grabando(audio="silencio", epoch=200.0),
    )
    cambio = pipeline.process(
        _slide(1), epoch=201.0, parte=1,
        state=_estado_grabando(audio="silencio", epoch=201.0),
    )
    pipeline.close(epoch=202.0, parte=1, motivo="fin_test")

    assert _campo(inicial, "saved") is True
    assert _campo(inicial, "ocr_ran") is True
    assert _campo(cambio, "saved") is True
    assert _campo(cambio, "ocr_ran") is False
    assert _campo(cambio, "discarded") is False
    assert len(llamadas_ocr) == 1
    assert len(list(img_dir.glob("*.webp"))) == 2
    filas = _filas(img_log)
    assert len(filas) == 2
    assert filas[-1]["audio_estado"] == "silencio"
    assert filas[-1]["provenance"] == "visual_sin_narracion_confirmada"
    assert filas[-1]["ocr_estado"] == "deferred"
    monkeypatch.setattr(
        captura_pantalla.ocr, "texto_de_imagen",
        lambda path: f"OCR postclase {Path(path).name}")
    assert post_procesar._ocr_completo_para_captura(
        tmp_path, filas[-1]).startswith("OCR postclase cap_")


def test_cambios_frecuentes_no_generan_backlog_y_acotan_ocr(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        captura_pantalla.config, "CAPTURA_OCR_INTERVALO_MIN", 10.0)
    monkeypatch.setattr(
        captura_pantalla.config, "CAPTURA_OCR_REFRESH_SEG", 10_000.0)
    monkeypatch.setattr(captura_pantalla.config, "CAPTURA_HEARTBEAT_SEG", 60.0)
    llamadas_ocr: list[int] = []
    pipeline, img_dir, _img_log, _timeline = _crear_pipeline(
        tmp_path,
        ocr_fn=lambda _img: llamadas_ocr.append(1) or "OCR rate limited",
    )

    resultados = []
    for indice in range(25):
        epoch = 300.0 + indice
        resultados.append(pipeline.process(
            _slide(indice), epoch=epoch, parte=1,
            state=_estado_grabando(epoch=epoch),
        ))

    assert all(bool(_campo(r, "saved")) for r in resultados)
    assert any(
        bool(_campo(r, "saved")) and not bool(_campo(r, "ocr_ran"))
        for r in resultados
    )
    assert 1 <= len(llamadas_ocr) <= 3
    assert len(list(img_dir.glob("*.webp"))) == 25

    snapshot = pipeline.snapshot()
    assert snapshot["frames_observed"] == 25
    assert snapshot["frames_saved"] == 25
    assert snapshot["ocr_runs"] == len(llamadas_ocr)
    _assert_sin_cola(snapshot)
    pipeline.close(epoch=325.0, parte=1, motivo="fin_test")


@pytest.mark.parametrize("estado_terminal", ["pausado", "fin"])
def test_revalida_pausa_manual_o_fin_antes_de_commit(
        monkeypatch, tmp_path, estado_terminal):
    monkeypatch.setattr(
        captura_pantalla.config, "CAPTURA_OCR_INTERVALO_MIN", 1.0)
    efectivo = _estado_grabando(epoch=400.0)

    def ocr_y_transicion(_img):
        efectivo["estado"] = estado_terminal
        return "Resultado que ya no se debe persistir"

    pipeline, img_dir, img_log, timeline = _crear_pipeline(
        tmp_path, ocr_fn=ocr_y_transicion)
    observado = dict(efectivo)
    resultado = pipeline.process(
        _slide(0), epoch=400.0, parte=1, state=observado,
        commit_validator=lambda: captura_pantalla.est.capturando_visual(efectivo),
    )

    assert _campo(resultado, "discarded") is True
    assert _campo(resultado, "saved") is False
    assert _campo(resultado, "heartbeat") is False
    assert _campo(resultado, "ocr_ran") is True
    assert str(_campo(resultado, "reason")).strip()
    assert list(img_dir.iterdir()) == []
    assert _filas(img_log) == []
    assert _filas(timeline) == []
    assert pipeline.snapshot()["frames_discarded"] == 1
    pipeline.close(epoch=401.0, parte=1, motivo=f"test_{estado_terminal}")
    assert _filas(timeline) == []


def test_close_es_idempotente_drena_sin_threads_ni_timers(tmp_path):
    threads_antes = {(thread.ident, id(thread)) for thread in threading.enumerate()}
    pipeline, _img_dir, _img_log, timeline = _crear_pipeline(
        tmp_path, ocr_fn=lambda _img: "Slide final")
    pipeline.process(
        _slide(0), epoch=500.0, parte=2,
        state=_estado_grabando(epoch=500.0),
    )

    pipeline.close(epoch=501.0, parte=2, motivo="shutdown")
    pipeline.close(epoch=999.0, parte=2, motivo="duplicado")

    finales = [fila for fila in _filas(timeline) if fila["evento"] == "fin"]
    assert len(finales) == 1
    assert finales[0]["t"] == 501
    assert finales[0]["motivo"] == "shutdown"
    snapshot = pipeline.snapshot()
    assert snapshot["closed"] is True
    _assert_sin_cola(snapshot)
    assert {(thread.ident, id(thread)) for thread in threading.enumerate()} == threads_antes
    assert not any(
        isinstance(value, (threading.Thread, threading.Timer))
        for value in vars(pipeline).values()
    )
