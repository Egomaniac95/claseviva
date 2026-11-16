import json
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw

from src import captura_pantalla, estado, post_procesar, visual_idle


def _imagen(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (96, 54), color).save(path, format="JPEG")


def _filas(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_visual_idle_combina_audio_activo_y_desconocido_conservadoramente():
    diagnostico = {"idle": True, "razones": ["pantalla_oscura"]}

    con_audio = captura_pantalla.decision_idle_con_audio(
        diagnostico,
        {"audio_ultimo_estado": "activo", "audio_silencio_chunks": 0,
         "audio_ultimo_t": 100.0},
        ahora=100.0,
    )
    sin_audio = captura_pantalla.decision_idle_con_audio(diagnostico, {})

    assert con_audio["omitir"] is False
    assert con_audio["audio_estado"] == "activo"
    assert con_audio["motivo"] == "audio_activo"
    assert sin_audio["omitir"] is False
    assert sin_audio["audio_estado"] == "desconocido"
    assert sin_audio["motivo"] == "audio_desconocido_conservador"


def test_visual_idle_solo_omite_con_silencio_sostenido_o_autopausa():
    diagnostico = {"idle": True, "razones": ["baja_informacion_visual"]}
    umbral = visual_idle.umbral_silencio(
        captura_pantalla.config.SILENCIO_CHUNKS, True)

    temprano = captura_pantalla.decision_idle_con_audio(
        diagnostico,
        {"audio_ultimo_estado": "silencio", "audio_silencio_chunks": umbral - 1,
         "audio_ultimo_t": 100.0},
        ahora=100.0,
    )
    sostenido = captura_pantalla.decision_idle_con_audio(
        diagnostico,
        {"audio_ultimo_estado": "silencio", "audio_silencio_chunks": umbral,
         "audio_ultimo_t": 100.0},
        ahora=100.0,
    )
    autopausa = captura_pantalla.decision_idle_con_audio(
        diagnostico,
        {"auto_pausa": True, "auto_pausa_motivo": "silencio_visual_idle"},
    )

    assert temprano["omitir"] is False
    assert temprano["motivo"] == "silencio_no_confirmado_conservador"
    assert sostenido["omitir"] is True
    assert sostenido["motivo"] == "silencio_sostenido"
    assert autopausa["omitir"] is True
    assert autopausa["motivo"] == "auto_pausa"


def test_visual_idle_silencio_obsoleto_degrada_a_audio_desconocido():
    diagnostico = {"idle": True, "razones": ["pantalla_estable"]}
    decision = captura_pantalla.decision_idle_con_audio(
        diagnostico,
        {"audio_ultimo_estado": "silencio", "audio_silencio_chunks": 999,
         "audio_ultimo_t": 10.0},
        ahora=10.0 + captura_pantalla.AUDIO_SIGNAL_MAX_AGE_SEG + 1.0,
    )

    assert decision["omitir"] is False
    assert decision["audio_estado"] == "desconocido"
    assert decision["motivo"] == "audio_desconocido_conservador"
    assert decision["audio_signal_fresh"] is False


def test_decision_idle_integrada_actualiza_diagnostico_y_timeline(monkeypatch, tmp_path):
    timeline = tmp_path / "visual_timeline.jsonl"
    estados: list[dict] = []
    monkeypatch.setattr(
        captura_pantalla.est, "actualizar_estado",
        lambda _d, **campos: estados.append(campos) or campos,
    )
    umbral = visual_idle.umbral_silencio(
        captura_pantalla.config.SILENCIO_CHUNKS, True)
    st = {
        "audio_ultimo_estado": "silencio",
        "audio_silencio_chunks": umbral,
        "audio_ultimo_t": 100.0,
    }

    decision, cerrada = captura_pantalla.aplicar_decision_idle(
        tmp_path, timeline, {"idle": True, "razones": ["pantalla_estable"]}, st,
        parte=2, ultimo_hash=123, ultimo_archivo="slide.jpg",
        cerrada_por_idle=False, epoch=100, ahora=100.0,
    )

    assert decision["omitir"] is True
    assert cerrada is True
    assert estados[-1]["visual_idle"]["decision_conjunta"]["motivo"] == (
        "silencio_sostenido")
    eventos = _filas(timeline)
    assert eventos[-1]["evento"] == "fin"
    assert eventos[-1]["motivo"] == "visual_idle_silencio_sostenido"
    assert eventos[-1]["archivo"] == "slide.jpg"


def test_slide_oscura_con_contenido_no_es_visual_idle():
    slide = Image.new("RGB", (320, 180), (8, 8, 12))
    draw = ImageDraw.Draw(slide)
    for y in range(25, 150, 20):
        draw.rectangle((20, y, 280, y + 5), fill=(235, 235, 240))

    diagnostico = visual_idle.analizar_imagen(slide)
    decision = captura_pantalla.decision_idle_con_audio(
        diagnostico,
        {"audio_ultimo_estado": "silencio", "audio_silencio_chunks": 999},
    )

    assert diagnostico["idle"] is False
    assert decision["omitir"] is False
    assert decision["motivo"] == "silent_visual_study"
    assert decision["preserve_canonical"] is True
    assert decision["extend_dwell"] is True
    assert decision["provenance"] == "visual_sin_narracion_confirmada"


def test_auto_pausa_conserva_estudio_visual_pero_pausa_manual_no():
    assert estado.capturando_visual({
        "estado": "grabando", "auto_pausa": True,
        "audio_requires_attention": False,
    }) is True
    assert estado.capturando_visual({
        "estado": "pausado", "auto_pausa": False,
        "audio_requires_attention": False,
    }) is False


def test_heartbeat_referencia_jpeg_sin_duplicarlo(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    log_path = tmp_path / "img_log.jsonl"
    timeline_path = tmp_path / "visual_timeline.jsonl"
    monkeypatch.setattr(
        captura_pantalla.imagen, "dhash",
        lambda img, tam=8: int(img.getpixel((0, 0))[0]),
    )
    monkeypatch.setattr(
        captura_pantalla.ocr, "texto_de_pil", lambda _img: "Slide estable")

    estable = Image.new("RGB", (96, 54), "white")
    ultimo_hash, ultimo_archivo, ultimo_ocr, ultimo_t, cambio = (
        captura_pantalla.registrar_observacion_visual(
            estable, img_dir, log_path, epoch=10, parte=1,
            ultimo_hash=None, ultimo_archivo=None, umbral=6))
    assert cambio is True
    assert len(list(img_dir.glob("*.webp"))) == 1

    ultimo_hash, ultimo_archivo, ultimo_ocr, ultimo_t, cambio = (
        captura_pantalla.registrar_observacion_visual(
            estable, img_dir, log_path, epoch=12, parte=1,
            ultimo_hash=ultimo_hash, ultimo_archivo=ultimo_archivo,
            ultimo_ocr=ultimo_ocr, ultimo_guardado_t=ultimo_t, umbral=6))
    assert cambio is False
    assert len(list(img_dir.glob("*.webp"))) == 1

    captura_pantalla.registrar_fin_visual(
        timeline_path, epoch=14, parte=1, ultimo_hash=ultimo_hash,
        ultimo_archivo=ultimo_archivo, motivo="fin_test")
    assert len(_filas(log_path)) == 1
    filas = _filas(log_path) + _filas(timeline_path)
    assert [fila["evento"] for fila in filas] == ["captura", "heartbeat", "fin"]
    assert filas[1]["ref_archivo"] == filas[0]["archivo"]
    assert filas[2]["ref_archivo"] == filas[0]["archivo"]


def test_cache_ocr_ejecuta_una_vez_por_estado_visual_estable(
        monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    log_path = tmp_path / "img_log.jsonl"
    llamadas = []
    monkeypatch.setattr(
        captura_pantalla.ocr, "texto_de_pil",
        lambda _img: llamadas.append("ocr") or "Documento técnico estable")
    cache = captura_pantalla.VisualOcrCache(max_entries=8)
    img = Image.new("RGB", (96, 54), "white")

    estado = captura_pantalla.registrar_observacion_visual(
        img, img_dir, log_path, epoch=10, parte=1,
        ultimo_hash=None, ultimo_archivo=None, cache_ocr=cache)
    captura_pantalla.registrar_observacion_visual(
        img, img_dir, log_path, epoch=12, parte=1,
        ultimo_hash=estado[0], ultimo_archivo=estado[1],
        ultimo_ocr=estado[2], ultimo_guardado_t=estado[3], cache_ocr=cache)

    assert llamadas == ["ocr"]
    assert cache.misses == 1
    assert cache.hits == 1
    assert cache.captures_avoided == 1
    assert len(list(img_dir.glob("*.webp"))) == 1


def test_productor_guarda_formula_distinta_con_mismo_dhash(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    log_path = tmp_path / "img_log.jsonl"
    monkeypatch.setattr(
        captura_pantalla.imagen, "dhash", lambda _img, tam=8: 123)
    textos = iter(("x = 1", "x = 2"))
    monkeypatch.setattr(
        captura_pantalla.ocr, "texto_de_pil", lambda _img: next(textos))
    img = Image.new("RGB", (96, 54), "white")

    h, archivo, texto, guardado_t, guardada = (
        captura_pantalla.registrar_observacion_visual(
            img, img_dir, log_path, epoch=10, parte=1,
            ultimo_hash=None, ultimo_archivo=None))
    assert guardada is True
    h, archivo, texto, guardado_t, guardada = (
        captura_pantalla.registrar_observacion_visual(
            img, img_dir, log_path, epoch=12, parte=1,
            ultimo_hash=h, ultimo_archivo=archivo, ultimo_ocr=texto,
            ultimo_guardado_t=guardado_t))

    assert guardada is True
    assert len(list(img_dir.glob("*.webp"))) == 2
    assert [fila["ocr"] for fila in _filas(log_path)] == ["x = 1", "x = 2"]
    assert "ocr_cambio" in _filas(log_path)[1]["motivo"]


def test_productor_guarda_build_progresivo_con_mismo_dhash(
        monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    log_path = tmp_path / "img_log.jsonl"
    monkeypatch.setattr(
        captura_pantalla.imagen, "dhash", lambda _img, tam=8: 321)
    textos = iter(("Training loop", "Training loop\nGradient update"))
    monkeypatch.setattr(
        captura_pantalla.ocr, "texto_de_pil", lambda _img: next(textos))
    img = Image.new("RGB", (96, 54), "white")

    h, archivo, texto, guardado_t, _ = (
        captura_pantalla.registrar_observacion_visual(
            img, img_dir, log_path, epoch=20, parte=1,
            ultimo_hash=None, ultimo_archivo=None))
    *_, guardada = captura_pantalla.registrar_observacion_visual(
        img, img_dir, log_path, epoch=22, parte=1,
        ultimo_hash=h, ultimo_archivo=archivo, ultimo_ocr=texto,
        ultimo_guardado_t=guardado_t)

    assert guardada is True
    assert len(_filas(log_path)) == 2


def test_productor_checkpoint_acotado_cuando_ocr_no_esta_disponible(
        monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    log_path = tmp_path / "img_log.jsonl"
    timeline_path = tmp_path / "visual_timeline.jsonl"
    monkeypatch.setattr(
        captura_pantalla.imagen, "dhash", lambda _img, tam=8: 77)
    monkeypatch.setattr(captura_pantalla.ocr, "texto_de_pil", lambda _img: "")
    monkeypatch.setattr(
        captura_pantalla.config, "CAPTURA_CHECKPOINT_SIN_OCR_SEG", 10)
    img = Image.new("RGB", (96, 54), "white")

    h, archivo, texto, guardado_t, _ = (
        captura_pantalla.registrar_observacion_visual(
            img, img_dir, log_path, epoch=30, parte=1,
            ultimo_hash=None, ultimo_archivo=None, timeline_path=timeline_path))
    estado = captura_pantalla.registrar_observacion_visual(
        img, img_dir, log_path, epoch=35, parte=1,
        ultimo_hash=h, ultimo_archivo=archivo, ultimo_ocr=texto,
        ultimo_guardado_t=guardado_t, timeline_path=timeline_path)
    assert estado[-1] is False
    estado = captura_pantalla.registrar_observacion_visual(
        img, img_dir, log_path, epoch=40, parte=1,
        ultimo_hash=estado[0], ultimo_archivo=estado[1],
        ultimo_ocr=estado[2], ultimo_guardado_t=estado[3],
        timeline_path=timeline_path)

    assert estado[-1] is True
    assert "checkpoint_sin_ocr" in _filas(log_path)[-1]["motivo"]


def test_comparador_ocr_detecta_cambio_despues_de_700_caracteres():
    prefijo = "shared technical context " * 40
    assert len(prefijo) > 700
    assert captura_pantalla.ocr.cambio_evidencia(
        prefijo + " objective alpha", prefijo + " objective beta")


def test_productor_detecta_diagrama_sin_ocr_aunque_dhash_64_colisione(
        monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    log_path = tmp_path / "img_log.jsonl"
    textos = iter(("Previous textual slide", ""))
    monkeypatch.setattr(
        captura_pantalla.ocr, "texto_de_pil", lambda _img: next(textos))

    def dhash_doble(img, tam=8):
        if tam == 8:
            return 99
        return 0 if img.convert("L").getpixel((0, 0)) > 127 else (1 << 256) - 1

    monkeypatch.setattr(captura_pantalla.imagen, "dhash", dhash_doble)
    previa = Image.new("RGB", (96, 54), "white")
    diagrama = Image.new("RGB", (96, 54), "black")
    h, archivo, texto, guardado_t, _ = (
        captura_pantalla.registrar_observacion_visual(
            previa, img_dir, log_path, epoch=50, parte=1,
            ultimo_hash=None, ultimo_archivo=None))

    *_, guardada = captura_pantalla.registrar_observacion_visual(
        diagrama, img_dir, log_path, epoch=52, parte=1,
        ultimo_hash=h, ultimo_archivo=archivo, ultimo_ocr=texto,
        ultimo_guardado_t=guardado_t)

    assert guardada is True
    assert "dhash_detalle" in _filas(log_path)[-1]["motivo"]


def test_timeline_usa_ocr_antes_de_merge_y_cierra_duracion(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "a.jpg", (245, 245, 245))
    _imagen(img_dir / "b.jpg", (240, 240, 240))
    log_path = tmp_path / "img_log.jsonl"
    filas = [
        {"t": 10, "archivo": "a.jpg", "dhash": "aaaaaaaaaaaaaaaa",
         "ocr": "Alpha beta gamma delta epsilon zeta theta", "evento": "captura"},
        {"t": 25, "archivo": "a.jpg", "ref_archivo": "a.jpg",
         "dhash": "aaaaaaaaaaaaaaaa", "evento": "heartbeat"},
        {"t": 40, "archivo": "b.jpg", "dhash": "aaaaaaaaaaaaaaaa",
         "ocr": "Volta Galvani Frankenstein frog muscle current", "evento": "captura"},
        {"t": 70, "archivo": "b.jpg", "ref_archivo": "b.jpg",
         "dhash": "aaaaaaaaaaaaaaaa", "evento": "fin"},
    ]
    log_path.write_text(
        "".join(json.dumps(fila) + "\n" for fila in (filas[0], filas[2])),
        encoding="utf-8")
    (tmp_path / "visual_timeline.jsonl").write_text(
        "".join(json.dumps(fila) + "\n" for fila in (filas[1], filas[3])),
        encoding="utf-8")
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})
    monkeypatch.setattr(post_procesar, "_umbral_visual_estable", lambda _d: 10.0)

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 100, [], {"origen": "video"})

    assert len(slides) == 2
    assert "ocr_cambio_importante" in slides[1]["motivo"]
    assert slides[0]["duracion_visual_seg"] == 30.0
    assert slides[1]["duracion_visual_seg"] == 30.0
    assert slides[0]["visual_estable"] is True
    assert slides[1]["visual_estable"] is True
    assert len(post_procesar.capturas_rango(tmp_path, 0, 100)) == 2


def test_timeline_legacy_infiere_hasta_siguiente_captura_y_fin(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "legacy_a.jpg", (255, 255, 255))
    _imagen(img_dir / "legacy_b.jpg", (0, 0, 0))
    (tmp_path / "img_log.jsonl").write_text(
        json.dumps({"t": 5, "archivo": "legacy_a.jpg",
                    "dhash": "0000000000000000", "ocr": "Primera slide tecnica"}) + "\n"
        + json.dumps({"t": 35, "archivo": "legacy_b.jpg",
                      "dhash": "ffffffffffffffff", "ocr": "Segunda slide tecnica"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})
    monkeypatch.setattr(post_procesar, "_umbral_visual_estable", lambda _d: 10.0)

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 60, [], {"origen": "video"})

    assert [slide["duracion_visual_seg"] for slide in slides] == [30.0, 25.0]


def test_timeline_no_cuenta_pausa_como_visibilidad(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "estable.jpg", (245, 245, 245))
    (tmp_path / "img_log.jsonl").write_text(
        json.dumps({"t": 10, "archivo": "estable.jpg",
                    "dhash": "aaaaaaaaaaaaaaaa", "ocr": "Slide estable"}) + "\n",
        encoding="utf-8",
    )
    eventos = [
        {"t": 14, "archivo": "estable.jpg", "ref_archivo": "estable.jpg",
         "evento": "fin", "motivo": "pausa_captura"},
        {"t": 20, "archivo": "estable.jpg", "ref_archivo": "estable.jpg",
         "evento": "heartbeat"},
        {"t": 30, "archivo": "estable.jpg", "ref_archivo": "estable.jpg",
         "evento": "fin", "motivo": "fin_clase"},
    ]
    (tmp_path / "visual_timeline.jsonl").write_text(
        "".join(json.dumps(evento) + "\n" for evento in eventos), encoding="utf-8")
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 40, [], {"origen": "video"})

    assert len(slides) == 1
    assert slides[0]["intervalos_visuales"] == [
        {"inicio": 10.0, "fin": 14.0},
        {"inicio": 20.0, "fin": 30.0},
    ]
    assert slides[0]["duracion_visual_seg"] == 14.0


def test_timeline_no_cuenta_visual_idle_entre_fin_y_reanudacion(
        monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "estable.jpg", (245, 245, 245))
    (tmp_path / "img_log.jsonl").write_text(
        json.dumps({"t": 10, "archivo": "estable.jpg",
                    "dhash": "aaaaaaaaaaaaaaaa", "ocr": "Slide estable"}) + "\n",
        encoding="utf-8")
    eventos = [
        {"t": 15, "ref_archivo": "estable.jpg", "evento": "fin",
         "motivo": "visual_idle"},
        {"t": 40, "ref_archivo": "estable.jpg", "evento": "heartbeat"},
        {"t": 50, "ref_archivo": "estable.jpg", "evento": "fin",
         "motivo": "fin_clase"},
    ]
    (tmp_path / "visual_timeline.jsonl").write_text(
        "".join(json.dumps(evento) + "\n" for evento in eventos),
        encoding="utf-8")
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 60, [], {"origen": "video"})

    assert slides[0]["intervalos_visuales"] == [
        {"inicio": 10.0, "fin": 15.0},
        {"inicio": 40.0, "fin": 50.0},
    ]


def test_seleccion_global_asigna_cada_slide_a_un_solo_chunk():
    chunks = [
        {"t_ini": 0.0, "t_fin": 100.0,
         "segs": [{"t_rel": 10.0, "fin_rel": 90.0, "texto": "alpha gradient"}]},
        {"t_ini": 100.0, "t_fin": 200.0,
         "segs": [{"t_rel": 110.0, "fin_rel": 190.0, "texto": "beta network"}]},
    ]
    slides = [
        {"archivo": "a.jpg", "t_rel": 10.0, "ocr": "Alpha gradient formula W x",
         "intervalos_visuales": [{"inicio": 10.0, "fin": 120.0}], "grupo_total": 2,
         "visual_estable": True},
        {"archivo": "b.jpg", "t_rel": 90.0, "ocr": "Beta neural network diagram",
         "intervalos_visuales": [{"inicio": 90.0, "fin": 190.0}], "grupo_total": 2,
         "visual_estable": True},
        {"archivo": "c.jpg", "t_rel": 195.0, "ocr": "Closing technical summary",
         "intervalos_visuales": [{"inicio": 195.0, "fin": 200.0}], "grupo_total": 1},
    ]

    asignadas, seleccionadas, reporte = (
        post_procesar.seleccionar_slides_globales_por_chunks(
            slides, chunks, tema_actual="deep learning", parte=1))
    archivos_asignados = [s["archivo"] for grupo in asignadas for s in grupo]
    archivos_seleccionados = [s["archivo"] for grupo in seleccionadas for s in grupo]

    assert sorted(archivos_asignados) == ["a.jpg", "b.jpg", "c.jpg"]
    assert len(archivos_asignados) == len(set(archivos_asignados))
    assert len(archivos_seleccionados) == len(set(archivos_seleccionados))
    assert sum(reporte["candidatas_por_chunk"]) == len(slides)
    assert [s["archivo"] for s in asignadas[0]] == ["a.jpg"]
    assert [s["archivo"] for s in asignadas[1]] == ["b.jpg", "c.jpg"]


def test_slides_protegidas_no_se_pierden_por_hard_max(monkeypatch):
    monkeypatch.setenv("CLASE_POST_SLIDES_HARD_MAX_PER_CHUNK", "4")
    slides = [
        {
            "archivo": f"slide_{i:02d}.jpg",
            "t_rel": float(i),
            "ocr": f"Definicion tecnica distinta numero {i} con formula x = {i}",
            "visual_estable": True,
            "transcript_ventana": "definicion tecnica formula",
        }
        for i in range(20)
    ]

    seleccion = post_procesar.seleccionar_slides_por_tema(
        slides, "definicion tecnica formula", presupuesto=2)
    reporte = post_procesar.seleccionar_slides_por_tema.ultimo_reporte

    assert len(seleccion) == 20
    assert reporte["hard_max_base"] < len(slides)
    assert reporte["slides_protegidas_estudio"] == 20
    assert reporte["slides_descartadas_presupuesto"] == 0


def test_timeline_reconstruye_slide_activa_antes_del_rango(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "activa.jpg", (240, 240, 240))
    (tmp_path / "img_log.jsonl").write_text(
        json.dumps({
            "t": 0, "archivo": "activa.jpg", "dhash": "aaaaaaaaaaaaaaaa",
            "ocr": "Neural network architecture", "evento": "captura",
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "visual_timeline.jsonl").write_text(
        "\n".join(json.dumps(row) for row in [
            {"t": 10, "ref_archivo": "activa.jpg", "evento": "heartbeat"},
            {"t": 20, "ref_archivo": "activa.jpg", "evento": "heartbeat"},
            {"t": 30, "ref_archivo": "activa.jpg", "evento": "fin"},
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 15, 25, [], {"origen": "video"})

    assert len(slides) == 1
    assert slides[0]["intervalos_visuales"] == [{"inicio": 15.0, "fin": 25.0}]
    assert slides[0]["duracion_visual_seg"] == 10.0


def test_heartbeat_posterior_confirma_slide_activa_desde_inicio_rango(
        monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "activa.jpg", (240, 240, 240))
    (tmp_path / "img_log.jsonl").write_text(json.dumps({
        "t": 0, "archivo": "activa.jpg", "dhash": "aaaaaaaaaaaaaaaa",
        "ocr": "Neural network architecture", "evento": "captura",
    }) + "\n", encoding="utf-8")
    (tmp_path / "visual_timeline.jsonl").write_text(
        "\n".join(json.dumps(row) for row in [
            {"t": 30, "ref_archivo": "activa.jpg", "evento": "heartbeat"},
            {"t": 40, "ref_archivo": "activa.jpg", "evento": "fin"},
        ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 20, 40, [], {"origen": "video"})

    assert slides[0]["intervalos_visuales"] == [{"inicio": 20.0, "fin": 40.0}]
    assert slides[0]["duracion_visual_seg"] == 20.0


def test_timeline_nuevo_cierra_crash_sin_extender_hasta_fin(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "crash.jpg", (220, 220, 220))
    (tmp_path / "img_log.jsonl").write_text(
        json.dumps({
            "t": 10, "archivo": "crash.jpg", "dhash": "bbbbbbbbbbbbbbbb",
            "ocr": "Loss function", "evento": "captura",
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "visual_timeline.jsonl").write_text(
        json.dumps({
            "t": 12, "ref_archivo": "crash.jpg", "evento": "heartbeat",
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 100, [], {"origen": "video"})
    reporte = post_procesar.construir_timeline_slides.ultimo_reporte

    assert len(slides) == 1
    assert slides[0]["duracion_visual_seg"] < 15
    assert "cierre_conservador_sin_fin" in slides[0]["motivo"]
    assert reporte["cierres_conservadores_sin_fin"] == 1


def test_ocr_vacio_no_fusiona_archivos_distintos_por_dhash(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "plantilla_a.jpg", (230, 230, 230))
    _imagen(img_dir / "plantilla_b.jpg", (225, 225, 225))
    (tmp_path / "img_log.jsonl").write_text(
        "\n".join(json.dumps({
            "t": t, "archivo": archivo, "dhash": "cccccccccccccccc", "ocr": "",
        }) for t, archivo in [(10, "plantilla_a.jpg"), (20, "plantilla_b.jpg")]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 30, [], {"origen": "video"})

    assert [slide["archivo"] for slide in slides] == [
        "plantilla_a.jpg", "plantilla_b.jpg"]
    assert "ocr_ausente_no_fusiona_por_hash" in slides[1]["motivo"]


def test_formula_corta_distinta_no_se_colapsa(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "formula_1.jpg", (210, 210, 210))
    _imagen(img_dir / "formula_2.jpg", (205, 205, 205))
    (tmp_path / "img_log.jsonl").write_text(
        "\n".join(json.dumps({
            "t": t, "archivo": archivo, "dhash": "dddddddddddddddd", "ocr": ocr,
        }) for t, archivo, ocr in [
            (10, "formula_1.jpg", "x = 1"),
            (20, "formula_2.jpg", "x = 2"),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 30, [], {"origen": "video"})

    assert len(slides) == 2
    assert "ocr_cambio_importante" in slides[1]["motivo"]


def test_titulos_cortos_distintos_no_se_colapsan(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    _imagen(img_dir / "agenda.jpg", (210, 210, 210))
    _imagen(img_dir / "grading.jpg", (205, 205, 205))
    (tmp_path / "img_log.jsonl").write_text(
        "\n".join(json.dumps({
            "t": t, "archivo": archivo, "dhash": "dddddddddddddddd", "ocr": ocr,
        }) for t, archivo, ocr in [
            (10, "agenda.jpg", "Agenda"),
            (20, "grading.jpg", "Grading"),
        ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 30, [], {"origen": "video"})

    assert [slide["archivo"] for slide in slides] == ["agenda.jpg", "grading.jpg"]
    assert "ocr_cambio_importante" in slides[1]["motivo"]


def test_build_progresivo_conserva_representante_final(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    filas = []
    for indice, ocr in enumerate((
        "Titulo",
        "Titulo\nprimer bullet",
        "Titulo\nprimer bullet\nsegundo bullet\nformula y = f(Wx+b)",
    ), 1):
        archivo = f"build_{indice}.jpg"
        _imagen(img_dir / archivo, (210 - indice, 210 - indice, 210 - indice))
        filas.append({
            "t": indice * 10, "archivo": archivo,
            "dhash": "abababababababab", "ocr": ocr,
        })
    (tmp_path / "img_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in filas) + "\n", encoding="utf-8")
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 40, [], {"origen": "video"})

    assert len(slides) == 1
    assert slides[0]["archivo"] == "build_3.jpg"
    assert slides[0]["grupo_total"] == 3


def test_identidad_visual_reaparece_despues_de_180_segundos(monkeypatch, tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    for nombre, color in [
        ("tema_a.jpg", (240, 240, 240)),
        ("tema_b.jpg", (20, 20, 20)),
        ("tema_a_repite.jpg", (238, 238, 238)),
    ]:
        _imagen(img_dir / nombre, color)
    filas = [
        {"t": 10, "archivo": "tema_a.jpg", "dhash": "1111111111111111",
         "ocr": "Gradient descent update rule technical slide"},
        {"t": 50, "archivo": "tema_b.jpg", "dhash": "eeeeeeeeeeeeeeee",
         "ocr": "Different neural architecture overview"},
        {"t": 250, "archivo": "tema_a_repite.jpg", "dhash": "1111111111111111",
         "ocr": "Gradient descent update rule technical slide"},
    ]
    (tmp_path / "img_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in filas) + "\n", encoding="utf-8")
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})

    slides = post_procesar.construir_timeline_slides(
        tmp_path, 0, 300, [], {"origen": "video"})
    reporte = post_procesar.construir_timeline_slides.ultimo_reporte

    assert len(slides) == 2
    repetida = next(slide for slide in slides if slide["grupo_total"] == 2)
    assert len(repetida["intervalos_visuales"]) == 2
    assert "identidad_visual_reaparecida" in repetida["motivo"]
    assert reporte["identidades_reaparecidas_global"] == 1


def test_slide_seleccionada_se_inserta_aunque_llm_omita_marcador(tmp_path):
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    for nombre in ("omitida_1.jpg", "omitida_2.jpg"):
        _imagen(img_dir / nombre, (200, 200, 200))
    slides = [
        {
            "archivo": f"omitida_{i}.jpg", "t_rel": float(i * 10),
            "ocr": f"Formula x = {i}", "transcript_ventana": f"Explicacion {i}",
            "protegida_estudio": True,
        }
        for i in (1, 2)
    ]

    tejido = post_procesar.entretejer(
        "## Teoria\nEl modelo omitio todos los marcadores.",
        tmp_path, slides, {"origen": "video"})
    asegurado = post_procesar.asegurar_slides_seleccionadas_en_guion(
        tejido, tmp_path, slides, {"origen": "video"}, parte=1)

    assert set(post_procesar.extraer_referencias_img(asegurado)) == {
        "omitida_1.jpg", "omitida_2.jpg"}
    assert "Evidencia visual integrada" in asegurado
    assert "Contexto registrado del profesor" not in asegurado
    assert "OCR de apoyo" not in asegurado


def test_lotes_protegidos_suplementarios_son_deterministicos_sin_llm(
        monkeypatch, tmp_path):
    d = tmp_path / "contenido" / "curso" / "clases" / "clase"
    (d / "img").mkdir(parents=True)
    (d / "session_state.json").write_text(
        json.dumps({"curso": "curso", "nombre": "Clase densa"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(post_procesar.config, "CONTENIDO_DIR", tmp_path / "contenido")
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *args: {})
    monkeypatch.setattr(
        post_procesar, "_hash_captura",
        lambda _d, cap: int.from_bytes(
            hashlib.sha256(str(cap["archivo"]).encode()).digest()[:8], "big"),
    )
    transcript = [{
        "t": 10, "fin": 180, "parte": 1,
        "texto": "Explicacion transcript-first de optimizacion y redes neuronales. " * 40,
    }]
    (d / "transcript.jsonl").write_text(
        "\n".join(json.dumps(row) for row in transcript) + "\n", encoding="utf-8")
    filas = []
    for i in range(13):
        nombre = f"densa_{i:02d}.jpg"
        _imagen(d / "img" / nombre, (180 + i, 180 + i, 180 + i))
        filas.append({
            "t": 20 + i * 10, "archivo": nombre, "parte": 1,
            "ocr": f"Formula tecnica distinta w = {i} y gradiente {i}",
        })
    (d / "img_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in filas) + "\n", encoding="utf-8")
    llamadas = []

    def fake_preguntar(_d, msgs, max_tokens, etiqueta, prompt_version):
        llamadas.append({
            "etiqueta": etiqueta,
            "system": msgs[0]["content"],
            "user": msgs[-1]["content"],
            "version": prompt_version,
        })
        refs = [
            line.split(": ", 1)[1].split(" ", 1)[0]
            for line in msgs[-1]["content"].splitlines()
            if line.startswith("SLIDE ")
        ]
        if etiqueta.startswith("post-chunk"):
            markers = "\n".join(
                f"[[IMG:{ref}|contexto tecnico de la ficha]]" for ref in refs)
            if msgs[0]["content"] == post_procesar.PROMPT_SLIDES_SUPLEMENTARIAS:
                return (
                    "## LECCION_COMPLETA_DUPLICADA\n"
                    "Explicacion transcript-first repetida indebidamente.\n"
                    + markers)
            # Una respuesta primaria completa evita activar el reintento de
            # desarrollo breve; aquí se verifica el flujo de imágenes extra.
            return "## Desarrollo\n\n" + transcript[0]["texto"] + "\n\n" + markers
        return msgs[-1]["content"]

    monkeypatch.setattr(post_procesar, "preguntar_cacheado", fake_preguntar)

    destino = post_procesar.consolidar_parte(
        d, 1, 0, 200, {"origen": "video"}, forzar=True)

    chunk_calls = [
        call for call in llamadas if call["etiqueta"].startswith("post-chunk")]
    assert len(chunk_calls) == 1
    assert "TRANSCRIPCION FILTRADA DEL RANGO" in chunk_calls[0]["user"]
    assert chunk_calls[0]["system"] == post_procesar.PROMPT_SLIDE_GUIDED
    assert all(call["system"] != post_procesar.PROMPT_SLIDES_SUPLEMENTARIAS
               for call in llamadas)
    assert post_procesar.consolidar_parte.ultimo_reporte["llamadas_llm_reales"] == 2
    final = destino.read_text(encoding="utf-8")
    assert all(f"img/densa_{i:02d}.jpg" in final for i in range(13))
    assert "LECCION_COMPLETA_DUPLICADA" not in final
