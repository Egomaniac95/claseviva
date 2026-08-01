import json
from pathlib import Path

import pytest
from PIL import Image

from src import audio_manifest, copiloto, escriba, estado, post_procesar


class _DetenerCiclo(RuntimeError):
    pass


def _crear_sesion(d: Path, *, resumir: bool = False) -> None:
    d.mkdir(parents=True)
    estado.escribir_estado(d, {
        "inicio": 1000.0,
        "estado": "grabando",
        "parte": 1,
        "nombre": "Fixture calendario",
        "resumir": resumir,
        "resumen_estado": "",
        "resumen_msg": "",
        "apuntes_auto": 10,
    })


def _parchar_main(monkeypatch, d: Path, clock: dict[str, float]) -> list[tuple]:
    errores: list[tuple] = []
    monkeypatch.setattr(escriba.config, "stdout_utf8", lambda: None)
    monkeypatch.setattr(escriba.config, "instancia_unica", lambda _nombre: True)
    monkeypatch.setattr(escriba.config, "latido_perdido", lambda: False)
    monkeypatch.setattr(escriba.config, "log_error", lambda *args: errores.append(args))
    monkeypatch.setattr(escriba.est, "clase_activa", lambda: d)
    monkeypatch.setattr(escriba.sys, "argv", ["escriba"])
    monkeypatch.setattr(escriba.time, "time", lambda: clock["now"])
    return errores


def _fake_bloque_exitoso(llamadas: list[dict]):
    def fake(_d, desde, hasta, parte, forzar=False, disparo=None):
        llamadas.append({
            "desde": desde,
            "hasta": hasta,
            "parte": parte,
            "forzar": forzar,
            "disparo": disparo,
        })
        return True

    fake.ultimo_estado = {"estado": "generado", "msg": "Resumen generado."}
    return fake


def test_plan_auto_cuenta_captura_util_y_no_pared_durante_pausa():
    st = {
        "inicio": 1000.0, "estado": "grabando", "auto_pausa": False,
        "resumen_clock": "captura_util",
    }
    marcas = [
        {"t": 1000.0, "tipo": "inicio"},
        {"t": 1300.0, "tipo": "pausa"},
        {"t": 1900.0, "tipo": "seguir"},
    ]

    antes = escriba.planificar_auto_captura_util(
        st, marcas, ultimo_auto_t=1000.0, ahora=2199.0, intervalo=600.0)
    frontera = escriba.planificar_auto_captura_util(
        st, marcas, ultimo_auto_t=1000.0, ahora=2245.0, intervalo=600.0)

    assert antes["recorded_elapsed_sec"] == pytest.approx(599.0)
    assert antes["boundary_wall_t"] is None
    assert frontera["recorded_elapsed_sec"] == pytest.approx(645.0)
    assert frontera["boundary_recorded_sec"] == 600.0
    assert frontera["boundary_wall_t"] == pytest.approx(2200.0)
    assert frontera["due_wall_t"] == pytest.approx(2245.0)
    assert frontera["due"] is True


def test_plan_auto_no_dispara_en_pausa_y_reinicio_avanza_una_frontera():
    st_pausa = {
        "inicio": 1000.0, "estado": "grabando", "auto_pausa": True,
        "resumen_clock": "captura_util",
    }
    marcas = [
        {"t": 1000.0, "tipo": "inicio"},
        {"t": 1600.0, "tipo": "auto_pausa"},
    ]
    pausado = escriba.planificar_auto_captura_util(
        st_pausa, marcas, ultimo_auto_t=1000.0, ahora=3000.0, intervalo=600.0)
    assert pausado["recorded_elapsed_sec"] == 600.0
    assert pausado["due"] is False

    st_activo = {**st_pausa, "auto_pausa": False}
    marcas.append({"t": 3000.0, "tipo": "seguir"})
    reinicio = escriba.planificar_auto_captura_util(
        st_activo, marcas, ultimo_auto_t=1600.0, ahora=3645.0, intervalo=600.0)
    doble_poll = escriba.planificar_auto_captura_util(
        st_activo, marcas, ultimo_auto_t=reinicio["boundary_wall_t"],
        ahora=3645.0, intervalo=600.0)

    assert reinicio["boundary_recorded_sec"] == 1200.0
    assert reinicio["boundary_wall_t"] == pytest.approx(3600.0)
    assert reinicio["due"] is True
    assert doble_poll["boundary_recorded_sec"] == 1800.0
    assert doble_poll["due"] is False


def test_ancla_parcial_conserva_grilla_cinco_diez_y_ventana_acumulada():
    st = {
        "inicio": 1000.0, "estado": "grabando", "auto_pausa": False,
        "resumen_clock": "captura_util",
    }
    marcas = [{"t": 1000.0, "tipo": "inicio"}]
    cinco = escriba.planificar_auto_captura_util(
        st, marcas, ultimo_auto_t=1000.0, ahora=1345.0, intervalo=300.0)
    diez = escriba.planificar_auto_captura_util(
        st, marcas, ultimo_auto_t=cinco["boundary_wall_t"],
        ahora=1645.0, intervalo=300.0)

    assert cinco["boundary_recorded_sec"] == 300.0
    assert cinco["boundary_wall_t"] == 1300.0
    assert diez["boundary_recorded_sec"] == 600.0
    assert diez["boundary_wall_t"] == 1600.0
    # El ancla agenda 10:00, pero el cursor de contenido no se movió: la próxima
    # ventana del loop sigue empezando en 00:00 y recupera ambos tramos.
    ultimo_cursor_contenido = 1000.0
    assert (ultimo_cursor_contenido, diez["boundary_wall_t"]) == (1000.0, 1600.0)


def test_main_auto_espera_diez_minutos_utiles_tras_reanudar(
        monkeypatch, tmp_path):
    d = tmp_path / "clase-util"
    _crear_sesion(d)
    estado.actualizar_estado(
        d, resumen_clock="captura_util", auto_pausa=True,
        auto_pausa_motivo="silencio")
    estado.anexar_jsonl(
        d / "marcas.jsonl", {"t": 1000.0, "tipo": "inicio", "parte": 1})
    estado.anexar_jsonl(
        d / "marcas.jsonl", {"t": 1300.0, "tipo": "auto_pausa", "parte": 1})
    clock = {"now": 1900.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    sleeps = 0

    def sleep(_segundos):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            assert llamadas == []
            estado.anexar_jsonl(
                d / "marcas.jsonl",
                {"t": 1900.0, "tipo": "seguir", "parte": 1})
            estado.actualizar_estado(
                d, auto_pausa=False, auto_pausa_motivo="")
            clock["now"] = 2245.0
            return
        raise _DetenerCiclo

    monkeypatch.setattr(escriba.time, "sleep", sleep)

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert llamadas == [{
        "desde": 1000.0, "hasta": 2200.0, "parte": 1,
        "forzar": False, "disparo": "auto",
    }]


def test_manual_exitoso_no_mueve_grilla_auto_y_no_duplica_contenido(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d, resumir=True)
    clock = {"now": 1300.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    sleeps = 0

    def sleep(_segundos):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            clock["now"] = 1599.0
            return
        if sleeps == 2:
            assert len(llamadas) == 1
            clock["now"] = 1600.0
            return
        raise _DetenerCiclo

    monkeypatch.setattr(escriba.time, "sleep", sleep)

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert llamadas == [
        {"desde": 1000.0, "hasta": 1255.0, "parte": 1,
         "forzar": True, "disparo": "manual"},
        {"desde": 1255.0, "hasta": 1555.0, "parte": 1,
         "forzar": False, "disparo": "auto"},
    ]
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj == {
        "schema_version": escriba.ESTADO_ESCRIBA_SCHEMA_VERSION,
        "ultimo_t": 1555.0,
        "ultimo_auto_t": 1555.0,
        "ultimo_manual_t": 1255.0,
        "ultimo_final_t": 1000.0,
        "proximo_auto_t": 2200.0,
        "auto_anchor_t": 1555.0,
    }
    st = estado.leer_estado(d)
    assert st["resumir"] is False
    assert st["resumen_estado"] == "generado"
    assert st["resumen_msg"] == "Resumen automático de 10 min generado."


def test_plan_auto_default_cinco_minutos_de_captura_util():
    st = {
        "inicio": 1000.0, "estado": "grabando", "auto_pausa": False,
        "resumen_clock": "captura_util",
    }
    marcas = [{"t": 1000.0, "tipo": "inicio"}]

    before = escriba.planificar_auto_captura_util(
        st, marcas, ultimo_auto_t=1000.0, ahora=1299.0, margen=45.0)
    waiting_stt = escriba.planificar_auto_captura_util(
        st, marcas, ultimo_auto_t=1000.0, ahora=1300.0, margen=45.0)
    due = escriba.planificar_auto_captura_util(
        st, marcas, ultimo_auto_t=1000.0, ahora=1345.0, margen=45.0)

    assert before["boundary_wall_t"] is None
    assert waiting_stt["boundary_recorded_sec"] == 300.0
    assert waiting_stt["boundary_wall_t"] == 1300.0
    assert waiting_stt["due"] is False
    assert due["due"] is True


def test_manual_no_reancla_frontera_cinco_minutos_y_no_deja_hueco(
        monkeypatch, tmp_path):
    d = tmp_path / "clase-manual-util"
    _crear_sesion(d, resumir=True)
    estado.actualizar_estado(
        d, resumen_clock="captura_util", apuntes_auto=5,
        resumen_solicitado_t=1180.0)
    estado.anexar_jsonl(
        d / "marcas.jsonl", {"t": 1000.0, "tipo": "inicio", "parte": 1})
    clock = {"now": 1180.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    sleeps = 0

    def sleep(_segundos):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            clock["now"] = 1344.0
            return
        if sleeps == 2:
            assert len(llamadas) == 1
            clock["now"] = 1345.0
            return
        if sleeps == 3:
            assert len(llamadas) == 2
            clock["now"] = 1644.0
            return
        if sleeps == 4:
            assert len(llamadas) == 2
            clock["now"] = 1645.0
            return
        raise _DetenerCiclo

    monkeypatch.setattr(escriba.time, "sleep", sleep)

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert llamadas == [
        {"desde": 1000.0, "hasta": 1135.0, "parte": 1,
         "forzar": True, "disparo": "manual"},
        {"desde": 1135.0, "hasta": 1300.0, "parte": 1,
         "forzar": False, "disparo": "auto"},
        {"desde": 1300.0, "hasta": 1600.0, "parte": 1,
         "forzar": False, "disparo": "auto"},
    ]
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj["schema_version"] == escriba.ESTADO_ESCRIBA_SCHEMA_VERSION
    assert reloj["auto_anchor_t"] == 1600.0
    assert reloj["ultimo_auto_t"] == 1600.0
    assert reloj["ultimo_manual_t"] == 1135.0
    assert reloj["proximo_auto_t"] == 1945.0


def test_reinicio_ignora_reanclaje_manual_legacy_y_recupera_grilla_fija(
        tmp_path):
    d = tmp_path / "clase-reinicio-reset"
    d.mkdir()
    path = d / "escriba_estado.json"
    path.write_text(json.dumps({
        "schema_version": 4,
        "ultimo_auto_t": 1000.0,
        "ultimo_manual_t": 1135.0,
        "ultimo_final_t": 1000.0,
        "proximo_auto_t": 1480.0,
        "auto_anchor_t": 1180.0,
    }), encoding="utf-8")
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "resumen_id": "manual:p1:1000:1135",
        "disparo": "manual", "parte": 1,
        "t_ini": 1000.0, "t_fin": 1135.0,
        "schedule_reset_t": 1180.0,
        "estado": "generado",
    })

    ultimo_auto, _manual, _final, siguiente = escriba._cargar_estado_escriba(
        path, inicio=1000.0, intervalo=300.0)
    anchor = escriba._cargar_auto_anchor(path, 1000.0, ultimo_auto)

    assert siguiente == 1300.0
    assert anchor == 1000.0


def test_reinicio_wall_clock_respeta_siguiente_frontera_tras_parcial(tmp_path):
    d = tmp_path / "clase-reinicio-parcial-wall"
    d.mkdir()
    path = d / "escriba_estado.json"
    path.write_text(json.dumps({
        "schema_version": escriba.ESTADO_ESCRIBA_SCHEMA_VERSION,
        "ultimo_auto_t": 1000.0,
        "ultimo_manual_t": 1000.0,
        "ultimo_final_t": 1000.0,
        "proximo_auto_t": 2200.0,
        "auto_anchor_t": 1555.0,
    }), encoding="utf-8")

    ultimo_auto, _manual, _final, siguiente = escriba._cargar_estado_escriba(
        path, inicio=1000.0, intervalo=600.0)

    assert ultimo_auto == 1000.0
    assert siguiente == 2200.0
    assert escriba._cargar_auto_anchor(path, 1000.0, ultimo_auto) == 1555.0


def test_migra_schema_cinco_desde_semilla_util_y_no_desde_parcial_vacio(
        tmp_path):
    d = tmp_path / "clase-migracion-parciales"
    d.mkdir()
    path = d / "escriba_estado.json"
    path.write_text(json.dumps({
        "schema_version": 5,
        "ultimo_auto_t": 1600.0,
        "ultimo_manual_t": 1000.0,
        "ultimo_final_t": 1000.0,
        "proximo_auto_t": 1945.0,
        "auto_anchor_t": 1600.0,
    }), encoding="utf-8")
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "disparo": "auto", "estado": "parcial", "parte": 1,
        "t_ini": 1000.0, "t_fin": 1300.0, "auto_deadline_t": 1345.0,
        "estado_pedagogico": "### Panorama acumulado de la clase\nSEMILLA_UTIL",
    })
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "disparo": "auto", "estado": "parcial", "parte": 1,
        "t_ini": 1300.0, "t_fin": 1600.0, "auto_deadline_t": 1645.0,
        "estado_pedagogico": "", "motivos": ["api_sin_respuesta"],
    })

    ultimo_auto, manual, final, siguiente = escriba._cargar_estado_escriba(
        path, inicio=1000.0, intervalo=300.0)
    anchor = escriba._cargar_auto_anchor(path, 1000.0, ultimo_auto)

    assert (ultimo_auto, manual, final) == (1300.0, 1000.0, 1000.0)
    assert anchor == 1600.0
    assert siguiente == 1945.0


def test_ocho_horas_mantienen_estado_compacto_y_preservan_tramo_nuevo(
        monkeypatch, tmp_path):
    d = tmp_path / "clase-ocho-horas"
    d.mkdir()
    for idx in range(96):
        estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
            "resumen_id": f"auto:{idx}",
            "disparo": "auto", "parte": 1,
            "t_ini": 1000.0 + idx * 300,
            "t_fin": 1000.0 + (idx + 1) * 300,
            "rango": f"{idx:02d}:00-{idx:02d}:05",
            "cuerpo": (f"### Explicación del tramo\nDetalle histórico {idx}. " * 80),
            "estado_pedagogico": (
                "### Panorama acumulado de la clase\n"
                f"La clase conecta todos los conceptos hasta el tramo {idx}.\n\n"
                "### Contexto acumulado para continuar\n"
                f"Base conceptual de detalle medio actualizada en el tramo {idx}."),
        })

    contexto = escriba.resumenes_acumulados_md(d, parte=1)
    transcript = "NUEVO_CONTENIDO " * 650 + "SENTINEL_NUEVO_AL_FINAL"
    capturado = {}

    def fake_detalle(messages, **_kwargs):
        capturado["messages"] = messages
        return {
            "texto": (
                "### Panorama acumulado de la clase\n"
                "La clase construye una explicación conectada del tema central.\n\n"
                "### Desarrollo detallado del tramo actual\n"
                + (
                    "La información nueva se conecta con el estado anterior, "
                    "explica el mecanismo paso a paso y conserva el sentinel. "
                ) * 12
                + "\n\n"
                "### Contexto acumulado para continuar\n"
                "Las definiciones y relaciones previas quedan explicadas con detalle medio "
                "para poder pausar, leer y retomar la clase sin perder el hilo.\n\n"
                "### Comprueba tu comprensión\n- ¿Cuál fue la idea nueva?\n\n"
                "BUSCAR: bounded incremental summary"
            ),
            "finish_reason": "stop", "error": None,
        }

    monkeypatch.setattr(escriba, "preguntar_detalle", fake_detalle)
    monkeypatch.setattr(escriba, "_quitar_buscar_y_enriquecer", lambda body: body)

    result = escriba.generar_resumen_en_vivo(
        transcript, "OCR_NUEVO", "07:55:00–08:00:00", contexto)
    prompt = capturado["messages"][-1]["content"]

    assert result["estado"] == "generado"
    assert len(contexto) <= escriba.MAX_CONTEXTO_PEDAGOGICO_CHARS
    assert "tramo 95" in contexto
    assert "detalle medio" in contexto
    assert "SENTINEL_NUEVO_AL_FINAL" in prompt
    assert "Detalle histórico 0" not in prompt
    assert len(prompt) <= escriba.MAX_PROMPT_USUARIO_CHARS
    assert "Panorama acumulado de la clase" in escriba.PROMPT_SISTEMA
    assert "Desarrollo detallado del tramo actual" in escriba.PROMPT_SISTEMA
    assert "Contexto acumulado para continuar" in escriba.PROMPT_SISTEMA
    assert "Panorama acumulado de la clase" in result["estado_pedagogico"]
    assert "Contexto acumulado para continuar" in result["estado_pedagogico"]


def test_web_off_acepta_ficha_completa_sin_buscar_en_una_llamada(monkeypatch):
    llamadas = []

    def fake_detalle(messages, **kwargs):
        llamadas.append((messages, kwargs))
        return {
            "texto": (
                "### Panorama acumulado de la clase\nEl modelo aprende de ejemplos.\n\n"
                "### Desarrollo detallado del tramo actual\nSe comparan predicción y "
                "etiqueta para ajustar los pesos.\n\n"
                "### Contexto acumulado para continuar\nEl error guía la actualización."
            ),
            "finish_reason": "stop", "error": None,
        }

    monkeypatch.setenv("CLASE_ESCRIBA_WEB", "0")
    monkeypatch.setattr(escriba, "preguntar_detalle", fake_detalle)

    resultado = escriba.generar_resumen_en_vivo(
        "El modelo compara su predicción con la etiqueta y ajusta los pesos.",
        "", "00:00:00–00:05:00")

    assert resultado["estado"] == "generado"
    assert len(llamadas) == 1
    assert llamadas[0][1]["thinking"] is False
    assert "AL FINAL" not in llamadas[0][0][0]["content"]


def test_gate_rechaza_prosa_larga_sin_secciones_nucleo(monkeypatch):
    monkeypatch.setenv("CLASE_ESCRIBA_WEB", "0")
    transcript = "Evidencia docente con conceptos y ejemplos. " * 30
    prosa_plana = "Texto coherente de salida. " * 40
    estructurada = (
        "### Panorama acumulado de la clase\nPanorama integrado.\n\n"
        "### Desarrollo detallado del tramo actual\nDesarrollo suficiente.\n\n"
        "### Contexto acumulado para continuar\nContexto útil."
    )

    motivos_planos = escriba.motivos_respuesta_incompleta(
        prosa_plana, "stop", transcript, "")
    motivos_estructurados = escriba.motivos_respuesta_incompleta(
        estructurada, "stop", transcript, "")

    assert any(m.startswith("faltan_secciones_nucleo=") for m in motivos_planos)
    assert not any(
        m.startswith("faltan_secciones_nucleo=") for m in motivos_estructurados)


def test_gate_rechaza_transcript_volcado_dentro_de_secciones(monkeypatch):
    monkeypatch.setenv("CLASE_ESCRIBA_WEB", "0")
    transcript = " ".join(f"concepto{i}" for i in range(180))
    volcado = (
        "### Panorama acumulado de la clase\nPanorama nominal.\n\n"
        "### Desarrollo detallado del tramo actual\n" + transcript + "\n\n"
        "### Contexto acumulado para continuar\nContexto nominal."
    )
    didactico = (
        "### Panorama acumulado de la clase\nSe presentó una progresión conceptual.\n\n"
        "### Desarrollo detallado del tramo actual\nLos conceptos se relacionaron "
        "mediante ejemplos y contrastes, no como una transcripción literal.\n\n"
        "### Contexto acumulado para continuar\nLa idea central debe conectarse "
        "con el siguiente procedimiento."
    )

    assert "copia_excesiva_transcript" in escriba.motivos_respuesta_incompleta(
        volcado, "stop", transcript, "")
    assert "copia_excesiva_transcript" not in escriba.motivos_respuesta_incompleta(
        didactico, "stop", transcript, "")


def test_gate_rechaza_encabezados_nucleo_sin_desarrollo(monkeypatch):
    monkeypatch.setenv("CLASE_ESCRIBA_WEB", "0")
    transcript = "La evidencia explica un concepto mediante ejemplos y contrastes. " * 4
    vacio = (
        "### Panorama acumulado de la clase\n\n"
        "### Desarrollo detallado del tramo actual\n-\n\n"
        "### Contexto acumulado para continuar\n"
    )

    motivos = escriba.motivos_respuesta_incompleta(
        vacio, "stop", transcript, "")

    assert any(m.startswith("faltan_secciones_nucleo=") for m in motivos)


def test_reintento_de_escriba_es_unico_compacto_y_sin_thinking(monkeypatch):
    llamadas = []

    def fake_detalle(messages, **kwargs):
        llamadas.append((messages, kwargs))
        if len(llamadas) == 1:
            return {
                "texto": "### Panorama acumulado de la clase\nBorrador incom",
                "finish_reason": "length", "error": None,
            }
        return {
            "texto": (
                "### Panorama acumulado de la clase\nPanorama completo.\n\n"
                "### Desarrollo detallado del tramo actual\n"
                + ("La evidencia explica el mecanismo y conecta cada paso. " * 38)
                + "\n\n"
                "### Contexto acumulado para continuar\n"
                "SENTINEL_CONTEXTO_PREVIO permanece conectado."
            ),
            "finish_reason": "stop", "error": None,
        }

    monkeypatch.setenv("CLASE_ESCRIBA_WEB", "0")
    monkeypatch.setattr(escriba, "preguntar_detalle", fake_detalle)
    transcript = "SENTINEL_NUEVO " + ("evidencia técnica extensa " * 900)

    resultado = escriba.generar_resumen_en_vivo(
        transcript, "OCR_IMPORTANTE " * 200, "00:00:00–00:05:00",
        "### Panorama acumulado de la clase\nSENTINEL_CONTEXTO_PREVIO")

    assert resultado["estado"] == "generado"
    assert len(llamadas) == 2
    assert [call[1]["thinking"] for call in llamadas] == [False, False]
    assert llamadas[1][1]["etiqueta"] == "escriba-reintento-compacto"
    assert llamadas[1][1]["max_tokens"] == 1800
    assert len(llamadas[1][0][-1]["content"]) <= 9800
    assert len(llamadas[1][0][-1]["content"]) < len(llamadas[0][0][-1]["content"])
    assert "SENTINEL_CONTEXTO_PREVIO" in llamadas[1][0][-1]["content"]
    assert "SENTINEL_CONTEXTO_PREVIO" in resultado["cuerpo"]
    assert "Transcripción cruda preservada" not in resultado["cuerpo"]


def test_respuesta_vacia_tiene_un_solo_rescate_compacto(monkeypatch):
    llamadas = []

    def fake_detalle(_messages, **kwargs):
        llamadas.append(kwargs)
        if len(llamadas) == 1:
            return {"texto": None, "finish_reason": "length", "error": None}
        return {
            "texto": (
                "### Panorama acumulado de la clase\nBase recuperada.\n\n"
                "### Desarrollo detallado del tramo actual\nConcepto recuperado.\n\n"
                "### Contexto acumulado para continuar\nContexto recuperado."
            ),
            "finish_reason": "stop", "error": None,
        }

    monkeypatch.setenv("CLASE_ESCRIBA_WEB", "0")
    monkeypatch.setattr(escriba, "preguntar_detalle", fake_detalle)

    resultado = escriba.generar_resumen_en_vivo(
        "La evidencia explica un concepto nuevo con un ejemplo concreto.",
        "", "00:00:00–00:05:00")

    assert resultado["estado"] == "generado"
    assert len(llamadas) == 2
    assert llamadas[1]["etiqueta"] == "escriba-reintento-compacto"
    assert all(llamada["thinking"] is False for llamada in llamadas)


def test_resumen_parcial_no_publica_journal_ni_markdown(monkeypatch, tmp_path):
    d = tmp_path / "clase-parcial"
    d.mkdir()
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1005.0, "parte": 1,
        "texto": "Contenido técnico durable para una ficha pedagógica. " * 5,
    })
    monkeypatch.setattr(escriba, "generar_resumen_en_vivo", lambda *_a, **_k: {
        "estado": "parcial",
        "msg": "Resumen parcial; transcript conservado",
        "cuerpo": "### Resumen parcial\nBorrador incompleto.",
        "motivos": ["finish_reason=length"],
        "estado_pedagogico": "",
    })

    assert escriba.procesar_bloque(
        d, 1000.0, 1010.0, 1, disparo="auto") is False
    assert not (d / "resumen_tramos.jsonl").exists()
    assert not (d / "leccion_en_vivo.md").exists()
    assert escriba.procesar_bloque.ultimo_estado["estado"] == "parcial"


def test_auto_parcial_avanza_solo_ancla_y_no_cursor(monkeypatch, tmp_path):
    d = tmp_path / "clase-auto-parcial"
    _crear_sesion(d)
    clock = {"now": 1645.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas = []

    def fake_parcial(_d, desde, hasta, parte, forzar=False, disparo=None):
        llamadas.append((desde, hasta, parte, forzar, disparo))
        return False

    fake_parcial.ultimo_estado = {
        "estado": "parcial", "msg": "pendiente",
        "motivos": ["respuesta_vacia"],
    }
    monkeypatch.setattr(escriba, "procesar_bloque", fake_parcial)
    monkeypatch.setattr(
        escriba.time, "sleep", lambda _s: (_ for _ in ()).throw(_DetenerCiclo()))

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert llamadas == [(1000.0, 1555.0, 1, False, "auto")]
    assert reloj["ultimo_auto_t"] == 1000.0
    assert reloj["ultimo_manual_t"] == 1000.0
    assert reloj["auto_anchor_t"] == 1555.0
    assert escriba._cargar_auto_anchor(
        d / "escriba_estado.json", 1000.0, 1000.0) == 1555.0
    assert estado.leer_estado(d)["resumen_estado"] == "parcial"


def test_manual_parcial_consume_pulsacion_pero_no_contenido(monkeypatch, tmp_path):
    d = tmp_path / "clase-manual-parcial"
    _crear_sesion(d, resumir=True)
    clock = {"now": 1300.0}
    _parchar_main(monkeypatch, d, clock)

    def fake_parcial(*_args, **_kwargs):
        return False

    fake_parcial.ultimo_estado = {
        "estado": "parcial", "msg": "pendiente",
        "motivos": ["respuesta_vacia"],
    }
    monkeypatch.setattr(escriba, "procesar_bloque", fake_parcial)
    monkeypatch.setattr(
        escriba.time, "sleep", lambda _s: (_ for _ in ()).throw(_DetenerCiclo()))

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    st = estado.leer_estado(d)
    assert reloj["ultimo_auto_t"] == 1000.0
    assert reloj["ultimo_manual_t"] == 1000.0
    assert st["resumir"] is False
    assert st["resumen_estado"] == "parcial"


def test_rango_de_resumen_usa_tiempo_relativo_de_captura_util(tmp_path):
    d = tmp_path / "clase-rango"
    _crear_sesion(d)
    estado.actualizar_estado(d, resumen_clock="captura_util")
    estado.anexar_jsonl(d / "marcas.jsonl", {"t": 1000.0, "tipo": "inicio"})
    estado.anexar_jsonl(d / "marcas.jsonl", {"t": 1120.0, "tipo": "pausa"})
    estado.anexar_jsonl(d / "marcas.jsonl", {"t": 1600.0, "tipo": "seguir"})

    assert escriba._rango_clase(d, 1000.0, 1780.0) == "00:00:00–00:05:00"


def test_vista_viva_muestra_una_ficha_actual_sin_repetir_panorama_ni_stt_crudo(
        tmp_path):
    d = tmp_path / "clase-vista"
    _crear_sesion(d)
    for index in range(12):
        cuerpo = (
            "### Panorama acumulado de la clase\n"
            f"PANORAMA_{index}: base conceptual conectada.\n\n"
            "### Desarrollo detallado del tramo actual\n"
            f"NOVEDAD_{index}: mecanismo explicado paso a paso.\n\n"
            "### Contexto acumulado para continuar\n"
            f"CONTEXTO_{index}: detalle medio para retomar.\n\n"
            "### Comprueba tu comprensión\n- ¿Qué cambió en este tramo?"
        )
        estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
            "parte": 1,
            "t_fin": 1300.0 + index * 300,
            "rango": f"00:{index * 5:02d}:00–00:{(index + 1) * 5:02d}:00",
            "cuerpo": cuerpo,
        })
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 4705.0, "ini": 1.97, "fin": 2.90, "parte": 1,
        "texto": "PENDIENTE_NO_PUBLICAR " * 12,
    })
    estado.actualizar_estado(d, stt_transcribed_until_wall=4707.9)

    vista = escriba.vista_leccion_vivo_resumen(d, parte=1)

    assert vista.startswith("# Pausa de estudio")
    assert vista.count("🧭 Panorama hasta ahora") == 1
    assert "PANORAMA_11" in vista and "PANORAMA_0" not in vista
    assert "🎯 Lo nuevo en este tramo" in vista and "NOVEDAD_11" in vista
    assert "🧩 Contexto para continuar" in vista and "CONTEXTO_11" in vista
    assert "✅ Comprueba tu comprensión" in vista
    assert "## ⏳ Tramo en curso" in vista
    assert "PENDIENTE_NO_PUBLICAR" not in vista
    assert "00:00:00" not in vista


def test_vista_conserva_ficha_valida_ante_parcial_posterior(tmp_path):
    d = tmp_path / "clase-vista-parcial"
    _crear_sesion(d)
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1300.0, "rango": "00:00:00–00:05:00",
        "estado": "generado",
        "cuerpo": (
            "### Panorama acumulado de la clase\nFICHA_ESTABLE.\n\n"
            "### Desarrollo detallado del tramo actual\nContenido válido."
        ),
    })
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1600.0, "rango": "00:05:00–00:10:00",
        "estado": "parcial",
        "cuerpo": (
            "### Resumen parcial del tramo\nPARCIAL_NO_PUBLICAR.\n\n"
            "### Transcripción cruda preservada\n> STT_CRUDO_NO_PUBLICAR"
        ),
    })

    vista = escriba.vista_leccion_vivo_resumen(d, parte=1)

    assert "FICHA_ESTABLE" in vista
    assert "PARCIAL_NO_PUBLICAR" not in vista
    assert "STT_CRUDO_NO_PUBLICAR" not in vista
    assert "Resumen pendiente" in vista


def test_cierre_corto_crudo_no_reemplaza_ficha_pedagogica(tmp_path):
    d = tmp_path / "clase-vista-cierre-corto"
    _crear_sesion(d)
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1300.0, "rango": "00:00:00–00:05:00",
        "estado": "generado", "motivos": [],
        "cuerpo": (
            "### Panorama acumulado de la clase\nFICHA_PEDAGOGICA_ESTABLE.\n\n"
            "### Desarrollo detallado del tramo actual\nDesarrollo válido.\n\n"
            "### Contexto acumulado para continuar\nContexto válido."
        ),
    })
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1320.0, "rango": "00:05:00–00:05:20",
        "estado": "generado", "motivos": ["cierre_corto_preservado"],
        "cuerpo": "### Transcripción cruda preservada\n> FRASE_CRUDA_FINAL",
    })

    vista = escriba.vista_leccion_vivo_resumen(d, parte=1)

    assert "FICHA_PEDAGOGICA_ESTABLE" in vista
    assert "FRASE_CRUDA_FINAL" not in vista
    assert "Resumen pendiente" in vista


def test_solo_parciales_rescata_estado_pedagogico_sin_publicar_raw(tmp_path):
    d = tmp_path / "clase-vista-solo-parciales"
    _crear_sesion(d)
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1300.0, "rango": "00:00:00–00:05:00",
        "estado": "parcial",
        "estado_pedagogico": (
            "### Panorama acumulado de la clase\nSEMILLA_PEDAGOGICA.\n\n"
            "### Contexto acumulado para continuar\nBASE_PARA_CONTINUAR."
        ),
        "cuerpo": "### Resumen parcial\nBODY_PARCIAL_NO_PUBLICAR.",
    })
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1600.0, "rango": "00:05:00–00:10:00",
        "estado": "parcial", "estado_pedagogico": "",
        "cuerpo": (
            "### Resumen parcial\nULTIMO_VACIO_NO_PUBLICAR.\n\n"
            "### Transcripción cruda preservada\n> STT_CRUDO_NO_PUBLICAR"
        ),
    })

    vista = escriba.vista_leccion_vivo_resumen(d, parte=1)
    contexto = escriba.resumenes_acumulados_md(d, parte=1)

    assert "SEMILLA_PEDAGOGICA" in vista
    assert "BASE_PARA_CONTINUAR" in vista
    assert "Resumen pedagógico pendiente" in vista
    assert "BODY_PARCIAL_NO_PUBLICAR" not in vista
    assert "ULTIMO_VACIO_NO_PUBLICAR" not in vista
    assert "STT_CRUDO_NO_PUBLICAR" not in vista
    assert "SEMILLA_PEDAGOGICA" in contexto
    assert "ULTIMO_VACIO_NO_PUBLICAR" not in contexto


def test_auto_dispara_en_diez_no_en_diez_cuarenta_y_cinco(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    clock = {"now": 1599.9}
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    sleeps = 0

    def sleep(_segundos):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            assert llamadas == []
            clock["now"] = 1600.0
            return
        raise _DetenerCiclo

    monkeypatch.setattr(escriba.time, "sleep", sleep)

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert len(llamadas) == 1
    assert llamadas[0]["disparo"] == "auto"
    assert llamadas[0]["hasta"] == 1555.0


@pytest.mark.parametrize(
    ("modo", "estado_esperado"),
    [("sin_transcript", "sin_transcript"), ("error", "error")],
)
def test_auto_fallido_no_avanza_ningun_cursor(
        monkeypatch, tmp_path, modo, estado_esperado):
    d = tmp_path / "clase"
    _crear_sesion(d)
    (d / "escriba_estado.json").write_text(
        json.dumps({"ultimo_t": 1000.0}), encoding="utf-8")
    clock = {"now": 1600.0}
    errores = _parchar_main(monkeypatch, d, clock)
    llamadas = []

    def fake(_d, desde, hasta, parte, forzar=False, disparo=None):
        llamadas.append((desde, hasta, parte, forzar, disparo))
        if modo == "error":
            raise RuntimeError("fallo sintetico")
        return False

    fake.ultimo_estado = {"estado": "", "msg": ""}
    monkeypatch.setattr(escriba, "procesar_bloque", fake)
    monkeypatch.setattr(
        escriba.time, "sleep", lambda _segundos: (_ for _ in ()).throw(_DetenerCiclo()))

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert llamadas == [(1000.0, 1555.0, 1, False, "auto")]
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj == {
        "schema_version": escriba.ESTADO_ESCRIBA_SCHEMA_VERSION,
        "ultimo_t": 1000.0,
        "ultimo_auto_t": 1000.0,
        "ultimo_manual_t": 1000.0,
        "ultimo_final_t": 1000.0,
        "proximo_auto_t": 1600.0,
        "auto_anchor_t": 1000.0,
    }
    assert estado.leer_estado(d)["resumen_estado"] == estado_esperado
    assert bool(errores) is (modo == "error")


@pytest.mark.parametrize("modo", ["sin_transcript", "error"])
def test_manual_fallido_permanece_pendiente(monkeypatch, tmp_path, modo):
    d = tmp_path / "clase"
    _crear_sesion(d, resumir=True)
    clock = {"now": 1300.0}
    _parchar_main(monkeypatch, d, clock)

    def fake(*_args, **_kwargs):
        if modo == "error":
            raise RuntimeError("fallo manual sintetico")
        return False

    fake.ultimo_estado = {"estado": "sin_transcript", "msg": "pendiente"}
    monkeypatch.setattr(escriba, "procesar_bloque", fake)
    monkeypatch.setattr(
        escriba.time, "sleep",
        lambda _segundos: (_ for _ in ()).throw(_DetenerCiclo()))

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert estado.leer_estado(d)["resumir"] is True
    assert not (d / "resumen_tramos.jsonl").exists()


def test_manual_y_auto_coincidentes_hacen_una_generacion(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d, resumir=True)
    clock = {"now": 1600.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    monkeypatch.setattr(
        escriba.time, "sleep", lambda _segundos: (_ for _ in ()).throw(_DetenerCiclo()))

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert len(llamadas) == 1
    assert llamadas[0]["forzar"] is True
    assert llamadas[0]["disparo"] == "auto"
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj["proximo_auto_t"] == 2200.0
    assert estado.leer_estado(d)["resumir"] is False


def test_estado_legacy_separa_manual_sin_saltar_contenido_auto(tmp_path):
    path = tmp_path / "escriba_estado.json"
    path.write_text(json.dumps({"ultimo_t": 1255.0}), encoding="utf-8")

    ultimo_auto, ultimo_manual, ultimo_final, proximo = escriba._cargar_estado_escriba(
        path, inicio=1000.0, intervalo=600.0)
    escriba._guardar_estado_escriba(
        path, ultimo_auto, ultimo_manual, proximo, ultimo_final_t=ultimo_final)

    assert (ultimo_auto, ultimo_manual, ultimo_final, proximo) == (
        1000.0, 1255.0, 1000.0, 1600.0)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": escriba.ESTADO_ESCRIBA_SCHEMA_VERSION,
        "ultimo_t": 1000.0,
        "ultimo_auto_t": 1000.0,
        "ultimo_manual_t": 1255.0,
        "ultimo_final_t": 1000.0,
        "proximo_auto_t": 1600.0,
        "auto_anchor_t": 1000.0,
    }


def test_estado_legacy_tardio_recupera_desde_primera_frontera_sin_bloque_gigante(
        tmp_path):
    path = tmp_path / "escriba_estado.json"
    path.write_text(json.dumps({
        "ultimo_t": 2155.0,
        "proximo_auto_t": 2800.0,
    }), encoding="utf-8")

    ultimo_auto, ultimo_manual, ultimo_final, proximo = escriba._cargar_estado_escriba(
        path, inicio=1000.0, intervalo=600.0)

    assert (ultimo_auto, ultimo_manual, ultimo_final, proximo) == (
        1000.0, 2155.0, 1000.0, 1600.0)


def test_auto_atrasado_recupera_una_frontera_fija_por_iteracion(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    clock = {"now": 2860.0}  # 31 minutos desde el inicio
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    sleeps = 0

    def sleep(_segundos):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise _DetenerCiclo

    monkeypatch.setattr(escriba.time, "sleep", sleep)

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert [(x["desde"], x["hasta"]) for x in llamadas] == [
        (1000.0, 1555.0),
        (1555.0, 2155.0),
        (2155.0, 2755.0),
    ]
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj["ultimo_auto_t"] == 2755.0
    assert reloj["ultimo_manual_t"] == 1000.0
    assert reloj["proximo_auto_t"] == 3400.0


def test_ventana_auto_vacia_no_bloquea_la_siguiente_con_contenido(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    clock = {"now": 2200.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas = []

    def fake(_d, desde, hasta, parte, forzar=False, disparo=None):
        llamadas.append((desde, hasta, disparo))
        return len(llamadas) > 1

    fake.ultimo_estado = {"estado": "generado", "msg": "Resumen generado."}
    monkeypatch.setattr(escriba, "procesar_bloque", fake)
    monkeypatch.setattr(
        escriba, "_ventana_auto_resuelta_sin_texto", lambda *_args: True)
    sleeps = 0

    def sleep(_segundos):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            raise _DetenerCiclo

    monkeypatch.setattr(escriba.time, "sleep", sleep)

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert llamadas == [
        (1000.0, 1555.0, "auto"),
        (1555.0, 2155.0, "auto"),
    ]
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj["ultimo_auto_t"] == 2155.0
    assert reloj["proximo_auto_t"] == 2800.0
    registros = estado.leer_jsonl(d / "resumen_tramos.jsonl")
    assert registros[0]["motivos"] == ["ventana_vacia_confirmada"]


def test_reinicio_reconcilia_auto_confirmado_antes_de_persistir_estado(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    (d / "escriba_estado.json").write_text(json.dumps({
        "schema_version": 2,
        "ultimo_t": 1000.0,
        "ultimo_auto_t": 1000.0,
        "ultimo_manual_t": 1000.0,
        "proximo_auto_t": 1600.0,
    }), encoding="utf-8")
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "resumen_id": "auto:p1:1000.000000:1555.000000",
        "t_ini": 1000.0,
        "t_fin": 1555.0,
        "auto_deadline_t": 1600.0,
        "parte": 1,
        "disparo": "auto",
        "estado": "generado",
    })
    clock = {"now": 1600.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    monkeypatch.setattr(
        escriba.time, "sleep", lambda _segundos: (_ for _ in ()).throw(_DetenerCiclo()))

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert llamadas == []
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj["ultimo_auto_t"] == 1555.0
    assert reloj["proximo_auto_t"] == 2200.0


def test_reinicio_repara_append_markdown_parcial_sin_duplicar(tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    resumen_id = "auto:p1:1000.000000:1555.000000"
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "resumen_id": resumen_id,
        "t_ini": 1000.0,
        "t_fin": 1555.0,
        "rango": "00:00–00:09",
        "parte": 1,
        "disparo": "auto",
        "estado": "generado",
        "msg": "Resumen generado.",
        "imagenes": ["slide.jpg"],
        "links_extra": [],
        "cuerpo": "### Apuntes del tramo\n\nBloque recuperado.",
    })
    (d / "leccion_en_vivo.md").write_text(
        f"# Encabezado\n<!-- resumen-inicio:{resumen_id} -->\nBloque truncado",
        encoding="utf-8",
    )

    escriba._reparar_bloques_confirmados(d)
    escriba._reparar_bloques_confirmados(d)

    leccion = (d / "leccion_en_vivo.md").read_text(encoding="utf-8")
    assert "Bloque truncado" not in leccion
    assert leccion.count("Bloque recuperado.") == 1
    assert leccion.count(f"<!-- resumen-fin:{resumen_id} -->") == 1


def test_reinicio_no_republica_parcial_ni_transcript_crudo(tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "resumen_id": "auto:p1:1000.000000:1300.000000",
        "t_ini": 1000.0,
        "t_fin": 1300.0,
        "rango": "00:00–00:05",
        "parte": 1,
        "disparo": "auto",
        "estado": "generado",
        "imagenes": [],
        "links_extra": [],
        "cuerpo": "### Panorama acumulado de la clase\nFicha didáctica válida.",
    })
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "resumen_id": "auto:p1:1300.000000:1600.000000",
        "t_ini": 1300.0,
        "t_fin": 1600.0,
        "rango": "00:05–00:10",
        "parte": 1,
        "disparo": "auto",
        "estado": "parcial",
        "imagenes": [],
        "links_extra": [],
        "cuerpo": (
            "## Resumen parcial\nNo publicable.\n\n"
            "### Transcripción cruda preservada\nVOLCADO_LITERAL_SENTINEL"
        ),
    })

    escriba._reparar_bloques_confirmados(d)

    leccion = (d / "leccion_en_vivo.md").read_text(encoding="utf-8")
    assert "Ficha didáctica válida." in leccion
    assert "VOLCADO_LITERAL_SENTINEL" not in leccion
    assert "Resumen parcial" not in leccion


def test_parcial_historico_no_confirma_ventana_ni_bloquea_reintento(tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    parcial = {
        "resumen_id": "auto:p1:1000.000000:1300.000000",
        "t_ini": 1000.0,
        "t_fin": 1300.0,
        "rango": "00:00–00:05",
        "parte": 1,
        "disparo": "auto",
        "estado": "parcial",
        "cuerpo": "### Transcripción cruda preservada\nTexto sin transformar.",
    }
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", parcial)

    assert escriba._resumen_confirmado(
        d, "auto", 1, 1000.0, 1300.0) is None

    generado = {
        **parcial,
        "estado": "generado",
        "cuerpo": "### Panorama acumulado de la clase\nFicha recuperada.",
    }
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", generado)
    confirmado = escriba._resumen_confirmado(
        d, "auto", 1, 1000.0, 1300.0)

    assert confirmado is not None
    assert confirmado["estado"] == "generado"
    assert "Ficha recuperada." in (d / "leccion_en_vivo.md").read_text(
        encoding="utf-8")


def test_post_y_copiloto_ignoran_parcial_crudo_y_usan_ficha_publicable(tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    generado = {
        "resumen_id": "auto:p1:1000.000000:1300.000000",
        "t_ini": 1000.0,
        "t_fin": 1300.0,
        "rango": "00:00–00:05",
        "parte": 1,
        "disparo": "auto",
        "estado": "generado",
        "imagenes": ["principal.jpg"],
        "cuerpo": (
            "### Panorama acumulado de la clase\nPanorama pedagógico limpio.\n\n"
            "### Desarrollo detallado del tramo actual\nExplicación con relaciones.\n\n"
            "### Contexto acumulado para continuar\nBase para el siguiente concepto."
        ),
    }
    parcial = {
        "resumen_id": "auto:p1:1300.000000:1600.000000",
        "t_ini": 1300.0,
        "t_fin": 1600.0,
        "rango": "00:05–00:10",
        "parte": 1,
        "disparo": "auto",
        "estado": "parcial",
        "estado_pedagogico": "### Contexto acumulado para continuar\nSemilla limpia.",
        "cuerpo": (
            "## Resumen parcial\nPARCIAL_SENTINEL\n\n"
            "### Transcripción cruda preservada\nVOLCADO_LITERAL_SENTINEL"
        ),
    }
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", generado)
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", parcial)
    (d / "leccion_en_vivo.md").write_text(
        parcial["cuerpo"], encoding="utf-8")

    contexto_post = post_procesar.resumenes_tramos_rango(
        d, 0.0, 600.0, {"origen": "vivo", "inicio": 1000.0})
    contexto_copiloto = copiloto.notas_hoy(d)

    for contexto in (contexto_post, contexto_copiloto):
        assert "Panorama pedagógico limpio." in contexto
        assert "PARCIAL_SENTINEL" not in contexto
        assert "VOLCADO_LITERAL_SENTINEL" not in contexto


def test_post_usa_solo_semilla_limpia_si_no_hay_ficha_publicable(tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
        "t_ini": 1000.0,
        "t_fin": 1300.0,
        "rango": "00:00–00:05",
        "parte": 1,
        "estado": "parcial",
        "estado_pedagogico": "### Contexto acumulado para continuar\nSEMILLA_LIMPIA",
        "cuerpo": "### Transcripción cruda preservada\nVOLCADO_LITERAL_SENTINEL",
    })

    contexto = post_procesar.resumenes_tramos_rango(
        d, 0.0, 300.0, {"origen": "vivo", "inicio": 1000.0})

    assert "SEMILLA_LIMPIA" in contexto
    assert "estado=semilla_pedagogica" in contexto
    assert "VOLCADO_LITERAL_SENTINEL" not in contexto


def test_fin_inicia_en_cursor_mas_avanzado_y_preserva_cursores_separados(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    estado.actualizar_estado(d, estado="fin")
    (d / "escriba_estado.json").write_text(json.dumps({
        "schema_version": 2,
        "ultimo_t": 1000.0,
        "ultimo_auto_t": 1000.0,
        "ultimo_manual_t": 1255.0,
        "proximo_auto_t": 1600.0,
    }), encoding="utf-8")
    clock = {"now": 1700.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    monkeypatch.setattr(
        escriba, "_esperar_transcriptor_fin",
        lambda _d: {"ready": True, "timed_out": False})

    assert escriba.main() == 0

    assert llamadas == [{
        "desde": 1255.0,
        "hasta": 1701.0,
        "parte": 1,
        "forzar": False,
        "disparo": "fin",
    }]
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj["ultimo_auto_t"] == 1000.0
    assert reloj["ultimo_manual_t"] == 1255.0
    assert reloj["ultimo_final_t"] == 1701.0
    assert reloj["proximo_auto_t"] == 1600.0


def test_reanudar_despues_de_cierre_no_resume_de_nuevo_el_tramo_final(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    (d / "escriba_estado.json").write_text(json.dumps({
        "schema_version": 3,
        "ultimo_t": 1000.0,
        "ultimo_auto_t": 1000.0,
        "ultimo_manual_t": 1255.0,
        "ultimo_final_t": 1701.0,
        "proximo_auto_t": 1600.0,
    }), encoding="utf-8")
    clock = {"now": 2200.0}
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque_exitoso(llamadas))
    monkeypatch.setattr(
        escriba.time, "sleep",
        lambda _segundos: (_ for _ in ()).throw(_DetenerCiclo()),
    )

    with pytest.raises(_DetenerCiclo):
        escriba.main()

    assert llamadas == [{
        "desde": 1701.0,
        "hasta": 2155.0,
        "parte": 1,
        "forzar": False,
        "disparo": "auto",
    }]
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert reloj["ultimo_auto_t"] == 2155.0
    assert reloj["ultimo_manual_t"] == 1255.0
    assert reloj["ultimo_final_t"] == 1701.0
    assert reloj["proximo_auto_t"] == 2800.0


def test_fin_relee_reloj_despues_de_espera_e_incluye_ultima_frase(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    estado.actualizar_estado(d, estado="fin")
    clock = {"now": 1700.0}
    _parchar_main(monkeypatch, d, clock)
    wav = d / "audio" / "chunk_ultima.wav"
    wav.parent.mkdir()
    wav.touch()
    audio_manifest.append_event(d, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "transactional_stt_required": True,
    })
    ultima = {
        "t": 1725.0, "parte": 1, "chunk": wav.name,
        "texto": "ÚLTIMA_FRASE_COMMITIDA durante la espera",
    }

    def esperar(_d):
        audio_manifest.commit_transcript_chunk(
            d, wav.stem, [ultima], {"path": wav.name}, materialize=False)
        clock["now"] = 1730.0
        return {"ready": True, "timed_out": False, "pending_stt_count": 0}

    monkeypatch.setattr(escriba, "_esperar_transcriptor_fin", esperar)
    monkeypatch.setattr(
        escriba, "generar_resumen_en_vivo",
        lambda *_a, **_k: pytest.fail("el cierre corto debe usar fallback local"))

    assert escriba.main() == 0

    leccion = (d / "leccion_en_vivo.md").read_text(encoding="utf-8")
    registros = estado.leer_jsonl(d / "resumen_tramos.jsonl")
    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    assert "ÚLTIMA_FRASE_COMMITIDA" in leccion
    assert registros[-1]["disparo"] == "fin"
    assert registros[-1]["t_fin"] == 1731.0
    assert reloj["ultimo_final_t"] == 1731.0


def test_fin_con_timeout_declara_pendiente_y_no_avanza_cursores(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    estado.actualizar_estado(d, estado="fin")
    estado_previo = {
        "schema_version": 3,
        "ultimo_t": 1000.0,
        "ultimo_auto_t": 1000.0,
        "ultimo_manual_t": 1255.0,
        "ultimo_final_t": 1000.0,
        "proximo_auto_t": 1600.0,
    }
    (d / "escriba_estado.json").write_text(
        json.dumps(estado_previo), encoding="utf-8")
    clock = {"now": 1700.0}
    _parchar_main(monkeypatch, d, clock)
    monkeypatch.setattr(escriba, "_esperar_transcriptor_fin", lambda _d: {
        "ready": False,
        "timed_out": True,
        "status": "waiting_stt",
        "pending_stt_count": 1,
        "pending_stt_chunks": ["chunk_pendiente"],
    })
    monkeypatch.setattr(
        escriba, "procesar_bloque",
        lambda *_args, **_kwargs: pytest.fail("no debe confirmar un cierre incompleto"))

    assert escriba.main() == 0

    reloj = json.loads((d / "escriba_estado.json").read_text(encoding="utf-8"))
    st_final = estado.leer_estado(d)
    assert reloj == {
        **estado_previo,
        "schema_version": escriba.ESTADO_ESCRIBA_SCHEMA_VERSION,
        "auto_anchor_t": 1000.0,
    }
    assert st_final["resumen_estado"] == "pendiente"
    assert "timeout" in st_final["resumen_msg"].lower()
    assert st_final["cierre_stt"]["timed_out"] is True


def test_espera_transcriptor_reporta_timeout_sin_sleeps(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    audio_manifest.append_event(d, {
        "chunk_id": "chunk_pendiente", "status": "capturing", "sequence": 1,
        "path": "chunk_pendiente.wav", "transactional_stt_required": True,
    })
    monkeypatch.setenv("CLASE_ESCRIBA_FIN_WAIT", "0")

    reporte = escriba._esperar_transcriptor_fin(d)

    assert reporte["ready"] is False
    assert reporte["timed_out"] is True
    assert reporte["status"] == "waiting_capture"
    assert reporte["waited_seconds"] >= 0


@pytest.mark.parametrize(
    ("disparo", "forzar", "esperado"),
    [(None, True, "manual"), (None, False, "auto"), ("fin", False, "fin")],
)
def test_resumen_tramos_registra_tipo_de_disparo(
        monkeypatch, tmp_path, disparo, forzar, esperado):
    d = tmp_path / "clase"
    d.mkdir()
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1005.0,
        "texto": "Contenido tecnico detallado para el resumen sintetico. " * 5,
    })
    monkeypatch.setattr(escriba, "generar_resumen_en_vivo", lambda *_args, **_kwargs: {
        "estado": "generado",
        "msg": "Resumen generado.",
        "cuerpo": "### Apuntes del tramo\n\nContenido sintetico completo.",
        "motivos": [],
    })

    generado = escriba.procesar_bloque(
        d, 1000.0, 1010.0, 1, forzar=forzar, disparo=disparo)

    assert generado is True
    registro = estado.leer_jsonl(d / "resumen_tramos.jsonl")
    assert len(registro) == 1
    assert registro[0]["disparo"] == esperado
    if esperado == "manual":
        assert "schedule_reset_t" not in registro[0]


def test_procesar_bloque_es_idempotente_por_ventana(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1005.0,
        "parte": 1,
        "texto": "Contenido tecnico durable para probar reinicios. " * 5,
    })
    generaciones = 0

    def generar(*_args, **_kwargs):
        nonlocal generaciones
        generaciones += 1
        return {
            "estado": "generado",
            "msg": "Resumen generado.",
            "cuerpo": "### Apuntes del tramo\n\nContenido durable.",
            "motivos": [],
        }

    monkeypatch.setattr(escriba, "generar_resumen_en_vivo", generar)

    assert escriba.procesar_bloque(
        d, 1000.0, 1555.0, 1, disparo="auto") is True
    assert escriba.procesar_bloque(
        d, 1000.0, 1555.0, 1, disparo="auto") is True

    assert generaciones == 1
    registros = estado.leer_jsonl(d / "resumen_tramos.jsonl")
    assert len(registros) == 1
    assert registros[0]["resumen_id"] == "auto:p1:1000.000000:1555.000000"
    leccion = (d / "leccion_en_vivo.md").read_text(encoding="utf-8")
    assert leccion.count("Contenido durable.") == 1


def test_capturas_se_ocran_antes_de_seleccion_temporal_y_se_acotan(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    (d / "img").mkdir(parents=True)
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1005.0,
        "parte": 1,
        "texto": "Explicacion extensa del modelo y de sus ecuaciones. " * 5,
    })
    for indice in range(1, 13):
        estado.anexar_jsonl(d / "img_log.jsonl", {
            "t": 1000.0 + indice,
            "parte": 1,
            "archivo": f"captura_{indice:02d}.jpg",
        })
    estado.anexar_jsonl(d / "img_log.jsonl", {
        "t": 1013.0,
        "parte": 2,
        "archivo": "otra_parte.jpg",
    })
    ocr_llamadas: list[str] = []

    def texto_de_imagen(path):
        ocr_llamadas.append(path.name)
        if path.name == "captura_07.jpg":
            return "def forward(x): return matriz @ x"
        return f"contenido visual unico de {path.stem}"

    entrada_llm: dict[str, str] = {}

    def generar(texto, ocr_junto, _rango, _contexto=""):
        entrada_llm["texto"] = texto
        entrada_llm["ocr"] = ocr_junto
        return {
            "estado": "generado",
            "msg": "Resumen generado.",
            "cuerpo": "### Apuntes del tramo\n\nSintesis visual.",
            "motivos": [],
        }

    monkeypatch.setattr(escriba.ocr, "texto_de_imagen", texto_de_imagen)
    monkeypatch.setattr(escriba, "generar_resumen_en_vivo", generar)

    assert escriba.procesar_bloque(
        d, 1000.0, 1020.0, 1, disparo="auto") is True

    assert ocr_llamadas == [
        *(f"captura_{i:02d}.jpg" for i in range(1, 13)),
        "otra_parte.jpg",
    ]
    assert "forward" in entrada_llm["ocr"]
    registro = estado.leer_jsonl(d / "resumen_tramos.jsonl")[0]
    assert registro["capturas_disponibles"] == 13
    assert len(registro["imagenes"]) == escriba.MAX_CAPTURAS_RESUMEN
    assert "captura_01.jpg" in registro["imagenes"]
    assert "captura_07.jpg" in registro["imagenes"]
    assert "otra_parte.jpg" in registro["imagenes"]
    leccion = (d / "leccion_en_vivo.md").read_text(encoding="utf-8")
    assert leccion.count("![captura]") == escriba.MAX_CAPTURAS_RESUMEN


def test_flush_final_preserva_partes_pendientes_y_las_separa_en_prompt(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1010.0,
        "parte": 1,
        "texto": "Detalle pendiente de la primera parte. " * 3,
    })
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1020.0,
        "parte": 2,
        "texto": "Detalle pendiente de la segunda parte. " * 3,
    })
    entrada: dict[str, str] = {}

    def generar(texto, _ocr, _rango, _contexto=""):
        entrada["texto"] = texto
        return {
            "estado": "generado",
            "msg": "Resumen generado.",
            "cuerpo": "### Apuntes del tramo\n\nCierre completo.",
            "motivos": [],
        }

    monkeypatch.setattr(escriba, "generar_resumen_en_vivo", generar)

    assert escriba.procesar_bloque(
        d, 1000.0, 1030.0, 2, disparo="fin") is True

    assert "[PARTE 1]" in entrada["texto"]
    assert "[PARTE 2]" in entrada["texto"]
    registro = estado.leer_jsonl(d / "resumen_tramos.jsonl")[0]
    assert registro["partes"] == [1, 2]
    assert "Cierre completo" in escriba.resumenes_acumulados_md(d, parte=1)
    assert "Cierre completo" in escriba.vista_leccion_vivo_resumen(d, parte=1)


def test_auto_que_atraviesa_cambio_de_parte_no_consume_evidencia(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    for parte, t, texto in (
        (1, 1010.0, "Cierre tecnico de la primera parte. " * 3),
        (2, 1020.0, "Inicio tecnico de la segunda parte. " * 3),
    ):
        estado.anexar_jsonl(d / "transcript.jsonl", {
            "t": t, "parte": parte, "texto": texto})
    entrada = {}

    def generar(texto, _ocr, _rango, _contexto=""):
        entrada["texto"] = texto
        return {
            "estado": "generado", "msg": "Resumen generado.",
            "cuerpo": "### Apuntes del tramo\n\nAmbas partes.", "motivos": [],
        }

    monkeypatch.setattr(escriba, "generar_resumen_en_vivo", generar)

    assert escriba.procesar_bloque(
        d, 1000.0, 1030.0, 2, disparo="auto") is True

    assert "[PARTE 1]" in entrada["texto"]
    assert "[PARTE 2]" in entrada["texto"]
    assert estado.leer_jsonl(d / "resumen_tramos.jsonl")[0]["partes"] == [1, 2]


def test_journal_resumen_separa_cola_truncada_de_siguiente_fila(tmp_path):
    path = tmp_path / "resumen_tramos.jsonl"
    path.write_bytes(b'{"resumen_id":"roto"')

    estado.anexar_jsonl(
        path, {"resumen_id": "valido", "disparo": "auto"}, durable=True)

    assert estado.leer_jsonl(path) == [
        {"resumen_id": "valido", "disparo": "auto"}]


def test_segmento_exactamente_en_inicio_de_clase_no_se_pierde(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1000.0, "parte": 1,
        "texto": "Definicion inicial esencial de la clase. " * 5,
    })
    entrada = {}

    def generar(texto, _ocr, _rango, _contexto=""):
        entrada["texto"] = texto
        return {
            "estado": "generado", "msg": "ok",
            "cuerpo": "### Apuntes del tramo\n\nInicio conservado.", "motivos": [],
        }

    monkeypatch.setattr(escriba, "generar_resumen_en_vivo", generar)

    assert escriba.procesar_bloque(
        d, 1000.0, 1100.0, 1, disparo="auto") is True
    assert "Definicion inicial" in entrada["texto"]


def test_resumen_hereda_slide_activa_antes_del_rango(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    (d / "img").mkdir()
    Image.new("RGB", (80, 60), "white").save(d / "img" / "activa.jpg")
    estado.anexar_jsonl(d / "img_log.jsonl", {
        "t": 1000.0, "parte": 1, "archivo": "activa.jpg",
        "evento": "captura", "dhash": "aaaaaaaaaaaaaaaa",
        "ocr": "Arquitectura neuronal activa",
    })
    estado.anexar_jsonl(d / "visual_timeline.jsonl", {
        "t": 1020.0, "parte": 1, "ref_archivo": "activa.jpg",
        "evento": "heartbeat",
    })
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "t": 1015.0, "parte": 1,
        "texto": "Explicacion detallada de la arquitectura neuronal. " * 4,
    })
    monkeypatch.setattr(post_procesar, "_analizar_visual_local", lambda *_args: {})
    monkeypatch.setattr(escriba, "generar_resumen_en_vivo", lambda *_args, **_kwargs: {
        "estado": "generado", "msg": "ok",
        "cuerpo": "### Apuntes del tramo\n\nSlide heredada.", "motivos": [],
    })

    assert escriba.procesar_bloque(
        d, 1010.0, 1030.0, 1, disparo="auto") is True

    registro = estado.leer_jsonl(d / "resumen_tramos.jsonl")[0]
    assert registro["imagenes"] == ["activa.jpg"]


def test_ventana_vacia_espera_stt_y_avanza_con_commit_vacio(tmp_path):
    d = tmp_path / "clase"
    audio = d / "audio"
    audio.mkdir(parents=True)
    wav = audio / "chunk_000000001000_p1_v10.wav"
    wav.touch()
    input_hash = audio_manifest.sha256_file(wav)
    audio_manifest.append_event(d, {
        "chunk_id": wav.stem, "status": "sealed", "sequence": 1,
        "path": wav.name, "wall_start": 1000.0, "wall_end": 1030.0,
        "sha256": input_hash, "transactional_stt_required": True,
    })

    assert escriba._ventana_auto_resuelta_sin_texto(
        d, 1000.0, 1555.0, 1600.0, 1700.0) is False

    audio_manifest.commit_transcript_chunk(d, wav.stem, [], {
        "path": wav.name, "input_sha256": input_hash}, materialize=False)

    assert escriba._ventana_auto_resuelta_sin_texto(
        d, 1000.0, 1555.0, 1600.0, 1700.0) is True


def test_espera_final_usa_commits_incluso_si_son_vacios(tmp_path):
    d = tmp_path / "clase"
    audio = d / "audio"
    audio.mkdir(parents=True)
    wav_vacio = audio / "chunk_000000001000_p1_v10.wav"
    wav_parcial = audio / "chunk_000000001030_p1_v10.wav"
    wav_vacio.touch()
    wav_parcial.touch()
    for sequence, wav in enumerate((wav_vacio, wav_parcial), 1):
        input_hash = audio_manifest.sha256_file(wav)
        audio_manifest.append_event(d, {
            "chunk_id": wav.stem, "status": "sealed", "sequence": sequence,
            "path": wav.name, "transactional_stt_required": True,
            "sha256": input_hash,
        })
    audio_manifest.commit_transcript_chunk(d, wav_vacio.stem, [], {
        "path": wav_vacio.name,
        "input_sha256": audio_manifest.sha256_file(wav_vacio),
    }, materialize=False)
    estado.anexar_jsonl(d / "transcript.jsonl", {
        "chunk": wav_parcial.name, "t": 1030.0, "texto": "vista parcial"})

    assert escriba._chunks_pendientes_transcript(d) == [wav_parcial]


def test_espera_final_no_ignora_chunk_aun_capturing(tmp_path):
    d = tmp_path / "clase"
    audio_manifest.append_event(d, {
        "chunk_id": "chunk_000000001000_p1_v10", "status": "capturing",
        "sequence": 1, "path": "chunk_000000001000_p1_v10.wav",
        "transactional_stt_required": True,
    })

    assert escriba._chunks_pendientes_transcript(d) == [
        d / "audio" / "chunk_000000001000_p1_v10.wav"]
