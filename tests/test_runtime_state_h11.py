from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import captura_pantalla, config, estado, panel


def _sesion(d: Path, **campos) -> None:
    d.mkdir(parents=True, exist_ok=True)
    base = {
        "inicio": 100.0, "estado": "grabando", "parte": 1,
        "auto_pausa": False, "auto_pausa_motivo": "",
        "audio_requires_attention": False, "audio_attention_reason": "",
        "audio_warning": "", "audio_diag": "", "audio_retry_generation": 0,
        "velocidad": 1.0, "nombre": "Runtime H1.1", "curso": "curso-h11",
        "resumir": False, "apuntes_auto": 10,
    }
    base.update(campos)
    estado.escribir_estado(d, base)
    (d / "marcas.jsonl").write_text("", encoding="utf-8")


@pytest.mark.parametrize(
    ("campos", "esperado"),
    [
        ({"estado": "grabando"}, "grabando_activo"),
        ({"estado": "grabando", "auto_pausa": True}, "auto_pausado"),
        ({"estado": "pausado"}, "pausado_manual"),
        ({"estado": "grabando", "audio_requires_attention": True},
         "audio_requires_attention"),
        ({"estado": "fin", "auto_pausa": True,
          "audio_requires_attention": True}, "finalizado"),
    ],
)
def test_estado_efectivo_canonico(campos, esperado):
    assert estado.estado_efectivo(campos) == esperado


def test_seguir_desde_auto_pausa_limpia_diagnostico_y_abre_nueva_epoca(tmp_path):
    d = tmp_path / "clase"
    _sesion(
        d, auto_pausa=True, auto_pausa_motivo="silencio",
        audio_warning="audio en cero", audio_diag="fallback agotado")
    estado.anexar_jsonl(d / "marcas.jsonl", {
        "t": 120.0, "tipo": "auto_pausa", "parte": 1})

    primera = estado.transicionar_captura(d, "seguir", ahora=130.0)
    segunda = estado.transicionar_captura(d, "seguir", ahora=131.0)

    final = estado.leer_estado(d)
    marcas = estado.leer_marcas(d)
    assert primera["changed"] is True
    assert segunda["changed"] is False
    assert estado.estado_efectivo(final) == "grabando_activo"
    assert final["auto_pausa"] is False
    assert final["auto_pausa_motivo"] == ""
    assert final["audio_warning"] == ""
    assert final["audio_diag"] == ""
    assert final["audio_retry_generation"] == 1
    assert [m["tipo"] for m in marcas].count("seguir") == 1


def test_pausa_y_seguir_dobles_son_idempotentes_y_no_duplican_marcas(tmp_path):
    d = tmp_path / "clase"
    _sesion(d)

    pausa_1 = estado.transicionar_captura(d, "pausa", ahora=120.0)
    pausa_2 = estado.transicionar_captura(d, "pausa", ahora=121.0)
    seguir_1 = estado.transicionar_captura(d, "seguir", ahora=140.0)
    seguir_2 = estado.transicionar_captura(d, "seguir", ahora=141.0)

    assert [pausa_1["changed"], pausa_2["changed"]] == [True, False]
    assert [seguir_1["changed"], seguir_2["changed"]] == [True, False]
    assert [m["tipo"] for m in estado.leer_marcas(d)] == ["pausa", "seguir"]


def test_auto_pausa_a_pausa_manual_y_seguir_queda_coherente(tmp_path):
    d = tmp_path / "clase"
    _sesion(d, auto_pausa=True, auto_pausa_motivo="silencio")
    estado.anexar_jsonl(d / "marcas.jsonl", {
        "t": 120.0, "tipo": "auto_pausa", "parte": 1})

    pausa = estado.transicionar_captura(d, "pausa", ahora=125.0)
    seguir = estado.transicionar_captura(d, "seguir", ahora=140.0)

    assert pausa["effective_state"] == "pausado_manual"
    assert seguir["effective_state"] == "grabando_activo"
    assert [m["tipo"] for m in estado.leer_marcas(d)] == [
        "auto_pausa", "pausa", "seguir"]


def test_reintento_desde_audio_attention_limpia_estado_y_genera_intento(tmp_path):
    d = tmp_path / "clase"
    _sesion(
        d, audio_requires_attention=True,
        audio_attention_reason="fallback_cycle_limit",
        audio_warning="requiere atención", audio_diag="sin WAV válido")

    resultado = estado.transicionar_captura(d, "seguir", ahora=150.0)

    final = estado.leer_estado(d)
    assert resultado["effective_state"] == "grabando_activo"
    assert final["audio_requires_attention"] is False
    assert final["audio_attention_reason"] == ""
    assert final["audio_retry_generation"] == 1
    assert final["audio_fallback_transition"] == "manual_retry"


@pytest.mark.parametrize("estado_inicial", ["auto_pausado", "pausado_manual"])
def test_fin_desde_cualquier_pausa_es_idempotente(tmp_path, estado_inicial):
    d = tmp_path / "clase"
    if estado_inicial == "auto_pausado":
        _sesion(d, auto_pausa=True)
    else:
        _sesion(d, estado="pausado")

    primero = estado.transicionar_captura(d, "fin", ahora=200.0)
    segundo = estado.transicionar_captura(d, "fin", ahora=201.0)

    assert primero["effective_state"] == "finalizado"
    assert primero["changed"] is True
    assert segundo["changed"] is False
    assert [m["tipo"] for m in estado.leer_marcas(d)] == ["fin"]


def test_metricas_tiempo_separan_pared_captura_pausa_y_video_x2():
    st = {"inicio": 100.0, "estado": "fin", "velocidad": 2.0}
    marcas = [
        {"t": 120.0, "tipo": "pausa"},
        {"t": 160.0, "tipo": "seguir"},
        {"t": 200.0, "tipo": "auto_pausa"},
        {"t": 230.0, "tipo": "seguir"},
        {"t": 300.0, "tipo": "fin"},
    ]

    metricas = estado.metricas_tiempo(st, marcas, ahora=999.0)

    assert metricas == {
        "elapsed_wall_sec": 200.0,
        "recorded_elapsed_sec": 130.0,
        "paused_elapsed_sec": 70.0,
        "video_equivalent_elapsed_sec": 260.0,
    }


def test_reloj_pared_avanza_durante_auto_pausa_y_captura_util_se_congela():
    st = {
        "inicio": 100.0, "estado": "grabando", "auto_pausa": True,
        "velocidad": 1.0,
    }
    marcas = [{"t": 120.0, "tipo": "auto_pausa"}]

    metricas = estado.metricas_tiempo(st, marcas, ahora=200.0)

    assert metricas["elapsed_wall_sec"] == 100.0
    assert metricas["recorded_elapsed_sec"] == 20.0
    assert metricas["paused_elapsed_sec"] == 80.0


def test_audio_attention_detiene_audio_pero_conserva_captura_visual():
    st = {
        "estado": "grabando", "audio_requires_attention": True,
        "auto_pausa": False,
    }

    assert estado.almacenando(st) is False
    assert estado.capturando_visual(st) is True


@pytest.mark.parametrize(
    ("efectivo", "pausar", "seguir", "parte", "finalizar"),
    [
        ("grabando_activo", True, False, True, True),
        ("auto_pausado", True, True, False, True),
        ("pausado_manual", False, True, False, True),
        ("audio_requires_attention", True, True, False, True),
        ("finalizado", False, False, False, False),
    ],
)
def test_capacidades_de_controles_derivan_del_estado_efectivo(
        efectivo, pausar, seguir, parte, finalizar):
    estados = {
        "grabando_activo": {"estado": "grabando"},
        "auto_pausado": {"estado": "grabando", "auto_pausa": True},
        "pausado_manual": {"estado": "pausado"},
        "audio_requires_attention": {
            "estado": "grabando", "audio_requires_attention": True},
        "finalizado": {"estado": "fin"},
    }

    capacidades = estado.capacidades_estado(estados[efectivo])

    assert capacidades["estado_efectivo"] == efectivo
    assert capacidades["pausar"] is pausar
    assert capacidades["seguir"] is seguir
    assert capacidades["cerrar_parte"] is parte
    assert capacidades["finalizar"] is finalizar
    if efectivo == "audio_requires_attention":
        assert capacidades["seguir_label"] == "Reanudar / probar audio"


def test_auto_pausa_attention_reintento_deja_historia_y_un_solo_retry(tmp_path):
    d = tmp_path / "clase"
    _sesion(d)
    estado.transicionar_captura(d, "auto_pausa", ahora=120.0, nota="silencio")

    atencion = estado.transicionar_captura(
        d, "audio_atencion", ahora=140.0,
        nota="fallback_cycle_limit",
        actualizaciones={"audio_attention_reason": "fallback_cycle_limit"})
    reintento = estado.transicionar_captura(d, "seguir", ahora=160.0)
    audio_vuelve = estado.transicionar_captura(
        d, "auto_seguir", ahora=170.0, nota="audio de vuelta")

    assert atencion["effective_state"] == "audio_requires_attention"
    assert reintento["effective_state"] == "grabando_activo"
    assert audio_vuelve["changed"] is False
    assert estado.leer_estado(d)["audio_retry_generation"] == 1
    assert [m["tipo"] for m in estado.leer_marcas(d)] == [
        "auto_pausa", "audio_requires_attention", "seguir"]


def test_attention_se_incluye_en_pausa_y_fin_congela_todos_los_relojes():
    st = {"inicio": 100.0, "estado": "fin", "velocidad": 2.0}
    marcas = [
        {"t": 150.0, "tipo": "audio_requires_attention"},
        {"t": 180.0, "tipo": "seguir"},
        {"t": 200.0, "tipo": "fin"},
    ]

    al_cerrar = estado.metricas_tiempo(st, marcas, ahora=200.0)
    mucho_despues = estado.metricas_tiempo(st, marcas, ahora=999.0)

    assert al_cerrar == mucho_despues
    assert al_cerrar == {
        "elapsed_wall_sec": 100.0,
        "recorded_elapsed_sec": 70.0,
        "paused_elapsed_sec": 30.0,
        "video_equivalent_elapsed_sec": 140.0,
    }


def test_doble_pausa_concurrente_serializa_una_sola_marca(tmp_path):
    d = tmp_path / "clase"
    _sesion(d)
    barrera = threading.Barrier(3)

    def pausar():
        barrera.wait(timeout=5)
        return estado.transicionar_captura(d, "pausa", ahora=120.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futuros = [pool.submit(pausar) for _ in range(2)]
        barrera.wait(timeout=5)
        resultados = [f.result(timeout=5) for f in futuros]

    assert sorted(r["changed"] for r in resultados) == [False, True]
    assert [m["tipo"] for m in estado.leer_marcas(d)] == ["pausa"]


def test_reanudar_sesion_final_limpia_attention_y_abre_epoca(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _sesion(
        d, estado="fin", audio_requires_attention=True,
        audio_attention_reason="sin_audio", audio_retry_generation=4)
    estado.anexar_jsonl(
        d / "marcas.jsonl", {"t": 180.0, "tipo": "fin", "parte": 1})
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")

    final = estado.reanudar(d)

    assert estado.estado_efectivo(final) == "grabando_activo"
    assert final["audio_requires_attention"] is False
    assert final["audio_retry_generation"] == 5
    assert final["parte"] == 2
    assert [m["tipo"] for m in estado.leer_marcas(d)][-2:] == [
        "fin_parte", "reanudar_sesion"]


def test_captura_pantalla_consume_predicado_visual_durante_attention(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    (d / "img").mkdir(parents=True)
    estados = iter([
        {"estado": "grabando", "audio_requires_attention": True,
         "auto_pausa": False, "parte": 1},
        {"estado": "fin", "parte": 1},
    ])
    capturas = []

    class _MssFalso:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(config, "instancia_unica", lambda _nombre: True)
    monkeypatch.setattr(config, "latido_perdido", lambda: False)
    monkeypatch.setattr(captura_pantalla.est, "clase_activa", lambda: d)
    monkeypatch.setattr(captura_pantalla.est, "leer_estado", lambda _d: next(estados))
    monkeypatch.setattr(captura_pantalla.mss, "mss", _MssFalso)
    monkeypatch.setattr(captura_pantalla, "capturar_ahora", lambda *_a, **_k: object())
    monkeypatch.setattr(
        captura_pantalla.VisualEvidencePipeline, "process",
        lambda *_a, **_k: (
            capturas.append("capturada")
            or captura_pantalla.VisualObservationOutcome(reason="test_attention")))
    monkeypatch.setattr(captura_pantalla, "registrar_fin_visual", lambda *_a, **_k: None)
    monkeypatch.setattr(captura_pantalla.time, "sleep", lambda _s: None)
    monkeypatch.setattr(config, "CAPTURA_INTERVALO", 0.0)
    monkeypatch.setattr(config, "VISUAL_IDLE_SKIP_CAPTURA", False)

    assert captura_pantalla.main() == 0
    assert capturas == ["capturada"]


def test_api_comando_idempotente_y_api_expone_estado_y_cuatro_relojes(
        monkeypatch, tmp_path):
    contenido = tmp_path / "contenido"
    logs = tmp_path / "logs"
    monkeypatch.setattr(config, "CONTENIDO_DIR", contenido)
    monkeypatch.setattr(config, "LOGS_DIR", logs)
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setattr(panel, "_dispositivos", lambda: [])
    monkeypatch.setattr(panel, "_monitores", lambda: [])
    logs.mkdir()
    d = estado.crear_clase("Controles H1.1", curso="curso-h11")
    estado.actualizar_estado(d, inicio=100.0, velocidad=2.0)
    monkeypatch.setattr(panel.time, "time", lambda: 200.0)

    pausa_1 = panel.app.test_client().post(
        "/api/comando", json={"cmd": "pausa"}).get_json()
    pausa_2 = panel.app.test_client().post(
        "/api/comando", json={"cmd": "pausa"}).get_json()
    seguir = panel.app.test_client().post(
        "/api/comando", json={"cmd": "seguir"}).get_json()
    data = panel.app.test_client().get("/api/estado").get_json()

    assert pausa_1["ok"] is True and pausa_1["changed"] is True
    assert pausa_2["ok"] is True and pausa_2["changed"] is False
    assert seguir["ok"] is True and seguir["changed"] is True
    assert data["estado_efectivo"] == "grabando_activo"
    assert data["elapsed_wall_sec"] == 100.0
    assert data["recorded_elapsed_sec"] == pytest.approx(100.0)
    assert data["paused_elapsed_sec"] == pytest.approx(0.0)
    assert data["video_equivalent_elapsed_sec"] == pytest.approx(200.0)


def test_pagina_deriva_controles_del_estado_efectivo_y_rotula_reintento():
    pagina = panel.PAGINA

    assert "s.estado_efectivo" in pagina
    assert "Reanudar / probar audio" in pagina
    assert "Captura útil" in pagina
    assert "Tiempo de pared" in pagina
