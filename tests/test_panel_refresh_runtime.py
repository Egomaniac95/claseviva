import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src import captura_pantalla, estado, panel


def test_javascript_servido_es_sintacticamente_valido():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node no está disponible para validar JavaScript")

    match = re.search(r"(?s)<script>(.*)</script>", panel.PAGINA)
    assert match is not None
    result = subprocess.run(
        (node, "--check", "-"),
        input=match.group(1),
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_render_scrollable_no_reemplaza_html_igual_y_preserva_lectura():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node no está disponible para validar el helper de scroll")
    match = re.search(
        r"(?s)(function renderScrollableHtml\(.*?\n})\nfunction renderFollowingText",
        panel.PAGINA,
    )
    assert match is not None
    source = f"""
const RENDERED_SCROLLABLE={{}};
function decorateMarkdown(_root) {{}}
{match.group(1)}
const root={{scrollTop:600,scrollHeight:1000,clientHeight:200,writes:0}};
Object.defineProperty(root,'innerHTML',{{
 get(){{return this._html||'';}},
 set(value){{this._html=value;this.writes+=1;this.scrollHeight=1400;}}
}});
if(!renderScrollableHtml(root,'primero','lesson','curso/a/1'))process.exit(1);
if(root.scrollTop!==0||root.writes!==1)process.exit(2);
root.scrollTop=320;
if(renderScrollableHtml(root,'primero','lesson','curso/a/1'))process.exit(3);
if(root.scrollTop!==320||root.writes!==1)process.exit(4);
if(!renderScrollableHtml(root,'segundo','lesson','curso/a/1'))process.exit(5);
if(root.scrollTop!==320||root.writes!==2)process.exit(6);
root.scrollTop=500;
renderScrollableHtml(root,'otra clase','lesson','curso/b/1');
if(root.scrollTop!==0||root.writes!==3)process.exit(7);
"""
    result = subprocess.run(
        (node, "-"), input=source, text=True, capture_output=True,
        encoding="utf-8", errors="replace", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_recargar_fuentes_reenumera_solo_bajo_accion_explicita(monkeypatch):
    monkeypatch.setattr(
        panel, "_MONITORES", [{"idx": 1, "etiqueta": "Pantalla 1 anterior"}])
    monkeypatch.setattr(
        panel, "_DISPOSITIVOS", [{"id": "old", "etiqueta": "Salida anterior"}])
    monkeypatch.setattr(
        captura_pantalla,
        "monitores_info",
        lambda: [
            {"idx": 1, "etiqueta": "Pantalla principal"},
            {"idx": 2, "etiqueta": "Pantalla nueva"},
        ],
    )
    monkeypatch.setattr(
        panel.captura_audio,
        "dispositivos_info",
        lambda: [{"id": "new", "etiqueta": "Salida nueva"}],
    )

    response = panel.app.test_client().post("/api/recargar_fuentes", json={})

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert [item["idx"] for item in data["monitores"]] == [1, 2]
    assert data["dispositivos"] == [{"id": "new", "etiqueta": "Salida nueva"}]


def test_cambiar_monitor_invalida_region_de_la_pantalla_anterior(
        monkeypatch, tmp_path: Path):
    estado.escribir_estado(tmp_path, {
        "estado": "grabando",
        "parte": 1,
        "monitor": 1,
        "region": [10, 20, 300, 200],
        "fuente": "sistema",
    })
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)
    monkeypatch.setattr(
        panel,
        "_monitores",
        lambda refresh=False: [
            {"idx": 1, "etiqueta": "Pantalla 1"},
            {"idx": 2, "etiqueta": "Pantalla 2"},
        ],
    )

    response = panel.app.test_client().post(
        "/api/comando", json={"cmd": "monitor 2"})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    current = estado.leer_estado(tmp_path)
    assert current["monitor"] == 2
    assert current["region"] is None


def test_pagina_usa_poll_single_flight_y_conserva_clase_final_al_recargar():
    page = panel.PAGINA

    assert "REFRESH_IN_FLIGHT" in page
    assert "setInterval(refrescar,1000)" not in page
    assert "document.hidden" in page
    assert "const landing=(s.modo_app&&sinClaseReal);" in page
    assert 'id="refreshSources"' in page


def test_contenido_largo_no_se_recorta_y_tiene_navegacion_estable(tmp_path: Path):
    estado.escribir_estado(tmp_path, {
        "estado": "grabando", "parte": 1, "inicio": 1000.0,
        "nombre": "fixture larga", "curso": "fixture",
    })
    body = (
        "### Panorama acumulado de la clase\n"
        "MARCADOR_INICIO\n\n"
        + ("Explicación pedagógica completa y verificable. " * 700)
        + "\n\n### Desarrollo detallado del tramo actual\nMARCADOR_FINAL"
    )
    estado.anexar_jsonl(tmp_path / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1300.0, "rango": "00:00:00–00:05:00",
        "cuerpo": body,
    })
    panel._panel_cache_clear()

    rendered = panel._panel_live_lesson_html(tmp_path, 1, "")

    assert "MARCADOR_INICIO" in rendered
    assert "MARCADOR_FINAL" in rendered
    assert len(rendered) > 18_000
    page = panel.PAGINA
    assert "vista_leccion[-18000:]" not in Path(panel.__file__).read_text(
        encoding="utf-8")
    assert "renderScrollableHtml(" in page
    assert "scrollContent('leccion','start')" in page
    assert "scrollContent('leccion','end')" in page
    assert "leccion.innerHTML=s.leccion_html" not in page
    assert "scrollbar-gutter:stable" in page


def test_markdown_largo_se_renderiza_una_vez_mientras_no_cambia(
        monkeypatch, tmp_path: Path):
    estado.anexar_jsonl(tmp_path / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1300.0, "rango": "00:00:00–00:05:00",
        "cuerpo": "### Panorama acumulado de la clase\nContenido estable.",
    })
    calls = []
    original = panel.a_html
    monkeypatch.setattr(
        panel, "a_html", lambda text: calls.append(text) or original(text))
    panel._panel_cache_clear()

    first = panel._panel_live_lesson_html(tmp_path, 1, "")
    second = panel._panel_live_lesson_html(tmp_path, 1, "")

    assert first == second
    assert len(calls) == 1
    estado.anexar_jsonl(tmp_path / "resumen_tramos.jsonl", {
        "parte": 1, "t_fin": 1600.0, "rango": "00:05:00–00:10:00",
        "cuerpo": "### Panorama acumulado de la clase\nContenido actualizado.",
    })
    third = panel._panel_live_lesson_html(tmp_path, 1, "")
    assert "Contenido actualizado" in third
    assert len(calls) == 2


def test_memoria_curso_larga_conserva_inicio_y_final(monkeypatch, tmp_path: Path):
    course = tmp_path / "CURSO.md"
    course.write_text(
        "# MARCADOR_CURSO_INICIO\n\n"
        + ("Contexto acumulado del curso. " * 400)
        + "\n\n## MARCADOR_CURSO_FINAL\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(panel.est, "curso_md", lambda _d: course)
    panel._panel_cache_clear()

    rendered = panel._panel_course_html(tmp_path)

    assert "MARCADOR_CURSO_INICIO" in rendered
    assert "MARCADOR_CURSO_FINAL" in rendered
    assert "tail=5000" not in Path(panel.__file__).read_text(encoding="utf-8")


def test_reloj_cliente_interpola_cada_segundo_sin_aumentar_polling():
    page = panel.PAGINA

    assert "function actualizarRelojLocal" in page
    assert "CLOCK_ANCHOR" in page
    assert "setInterval(actualizarRelojLocal,1000)" in page
    assert "setInterval(refrescar,1000)" not in page
    assert "estado_efectivo==='grabando_activo'" in page
    assert "estado_efectivo==='finalizado'" in page
    assert "05:00, 10:00, 15:00…" in page
    assert "sin mover esos cortes" in page
    assert "reinicia el contador" not in page
    assert "return 5000;" in page
    assert "return 2000;" not in page


def test_reloj_sin_clase_no_avanza_y_pausa_solo_detiene_tiempo_util():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node no está disponible para validar el reloj")
    match = re.search(
        r"(?s)(function formatElapsed\(.*?\n})\nfunction scheduleRefresh", panel.PAGINA)
    assert match is not None
    script = """
let CLOCK_ANCHOR=null, now=0;
const performance={now:()=>now}, values={};
const document={hidden:false,getElementById:(id)=>(values[id]??={textContent:'-'})};
""" + match.group(1) + """
anclarRelojLocal({estado_efectivo:'sin_clase'});
now=4000;actualizarRelojLocal();
if(CLOCK_ANCHOR!==null||Object.keys(values).length)process.exit(1);
anclarRelojLocal({clase_id:'fixture',estado_efectivo:'grabando_activo',
 elapsed_wall_sec:100,recorded_elapsed_sec:60});
now=5000;actualizarRelojLocal();
if(values.duracion.textContent!=='00:01:01')process.exit(2);
anclarRelojLocal({clase_id:'fixture',estado_efectivo:'pausado_manual',
 elapsed_wall_sec:101,recorded_elapsed_sec:61});
now=6000;actualizarRelojLocal();
if(values.duracion.textContent!=='00:01:01'||values.tiempo_pared.textContent!=='00:01:42')process.exit(3);
anclarRelojLocal({clase_id:'fixture',estado_efectivo:'finalizado',
 elapsed_wall_sec:102,recorded_elapsed_sec:61});
now=10000;actualizarRelojLocal();
if(values.tiempo_pared.textContent!=='00:01:42')process.exit(4);
"""
    result = subprocess.run((node, "-"), input=script, text=True,
                            capture_output=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr


def test_cache_panel_no_repite_loader_si_firma_no_cambio(monkeypatch):
    panel._panel_cache_clear()
    calls = []
    clock = iter((10.0, 11.0, 20.0))
    monkeypatch.setattr(panel.time, "monotonic", lambda: next(clock))

    first = panel._panel_cached(
        "fixture", "same", ttl=5.0,
        loader=lambda: calls.append("load") or {"value": len(calls)},
    )
    second = panel._panel_cached(
        "fixture", "same", ttl=5.0,
        loader=lambda: calls.append("load") or {"value": len(calls)},
    )
    third = panel._panel_cached(
        "fixture", "same", ttl=5.0,
        loader=lambda: calls.append("load") or {"value": len(calls)},
    )

    assert first is second
    assert third is not second
    assert calls == ["load", "load"]


def test_estado_get_no_migra_ni_escribe_session_state(monkeypatch, tmp_path: Path):
    (tmp_path / "audio").mkdir()
    (tmp_path / "img").mkdir()
    estado.escribir_estado(tmp_path, {
        "estado": "fin", "parte": 1, "inicio": 100.0,
        "nombre": "fixture", "curso": "fixture", "fuente": "sistema",
        "velocidad": 1.0, "resumir": True, "apuntes_auto": 0,
    })
    state_path = tmp_path / "session_state.json"
    before = state_path.read_bytes()
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)
    panel._panel_cache_clear()

    response = panel.app.test_client().get("/api/estado")

    assert response.status_code == 200
    assert response.get_json()["estado"] == "fin"
    assert response.get_json()["apuntes_auto"] == 0
    assert state_path.read_bytes() == before


def test_descartar_captura_copiloto_solo_borra_preview_no_referenciado(
        monkeypatch, tmp_path: Path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    preview = image_dir / "consulta_1784000000000.jpg"
    preview.write_bytes(b"synthetic-preview")
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)

    response = panel.app.test_client().post(
        "/api/captura_previa/descartar", json={"captura": preview.name})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert not preview.exists()

    referenced = image_dir / "consulta_1784000000001.jpg"
    referenced.write_bytes(b"referenced-preview")
    (tmp_path / "qa.md").write_text(
        f"![captura](img/{referenced.name})\n", encoding="utf-8")
    refused = panel.app.test_client().post(
        "/api/captura_previa/descartar", json={"captura": referenced.name})

    assert refused.status_code == 409
    assert referenced.exists()


def test_ultima_captura_lee_solo_cola_jsonl_sin_parser_completo(
        monkeypatch, tmp_path: Path):
    image_dir = tmp_path / "img"
    image_dir.mkdir()
    target = image_dir / "cap_final.jpg"
    target.write_bytes(b"fixture")
    log = tmp_path / "img_log.jsonl"
    with log.open("w", encoding="utf-8") as handle:
        for index in range(5000):
            handle.write('{"archivo":"missing_%05d.jpg","t":%d}\n' % (
                index, index))
        handle.write('{"archivo":"cap_final.jpg","t":5001}\n')
    monkeypatch.setattr(
        panel.est, "leer_jsonl",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("no debe parsear el JSONL completo")),
    )
    panel._panel_cache_clear()

    assert panel._panel_last_capture(tmp_path)["archivo"] == "cap_final.jpg"


def test_estado_stt_live_usa_firma_o1_y_reserva_inventario_para_fin(
        monkeypatch, tmp_path: Path):
    (tmp_path / "audio").mkdir()
    (tmp_path / "transcript_chunks").mkdir()
    monkeypatch.setattr(
        panel, "_directory_files_signature",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("inventario profundo no permitido durante LIVE")),
    )
    monkeypatch.setattr(
        panel.audio_manifest, "transcript_status_report",
        lambda _d, verify_files: {"verify_files": verify_files},
    )
    panel._panel_cache_clear()

    report = panel._panel_stt_status(tmp_path, verify_files=False)

    assert report == {"verify_files": False}
