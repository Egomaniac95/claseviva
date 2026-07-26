import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src import config, estado
from src import costos_api, deepseek_client, incremental_jsonl, panel, post_procesar


class _Usage:
    prompt_tokens = 1000
    completion_tokens = 500


class _Respuesta:
    usage = _Usage()


def _aislar_entorno(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "CONTENIDO_DIR", tmp_path / "contenido")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setattr(panel, "_dispositivos", lambda: [])
    monkeypatch.setattr(panel, "_monitores", lambda: [])
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _fijar_rango(d: Path, base: float, duracion: float = 120.0) -> None:
    st = estado.leer_estado(d)
    st.update({"inicio": base, "estado": "fin", "parte": 1})
    estado.escribir_estado(d, st)
    (d / "marcas.jsonl").write_text("", encoding="utf-8")
    estado.anexar_jsonl(d / "marcas.jsonl",
                        {"t": base, "tipo": "inicio", "parte": 1, "nota": st["nombre"]})
    estado.anexar_jsonl(d / "marcas.jsonl",
                        {"t": base + duracion, "tipo": "fin", "parte": 1, "nota": ""})


def _log_api(**campos) -> None:
    fila = {"t": 0.0, "etiqueta": "test", "modelo": "deepseek-test",
            "tokens_entrada": 1, "tokens_salida": 1, "costo_usd": 0.0}
    fila.update(campos)
    with open(config.LOGS_DIR / "uso_api.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(fila, ensure_ascii=False) + "\n")


def _ruta_log(d: Path) -> str:
    try:
        return d.relative_to(config.ROOT).as_posix()
    except ValueError:
        return d.as_posix()


def test_deepseek_log_uso_atribuye_clase_activa(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Costo metadata", curso="curso-costos")

    deepseek_client._log_uso(_Respuesta(), "unit-cost")

    filas = estado.leer_jsonl(config.LOGS_DIR / "uso_api.jsonl")
    assert len(filas) == 1
    fila = filas[0]
    assert fila["curso"] == "curso-costos"
    assert fila["clase_id"] == d.name
    assert fila["clase_path"].endswith(f"curso-costos/clases/{d.name}")
    assert fila["parte"] == 1
    assert "api_key" not in fila
    assert "sk-" not in json.dumps(fila, ensure_ascii=False)
    assert fila["costo_usd"] > 0
    # Sin campos de caché del proveedor, la fila no inventa contadores.
    assert "tokens_cache_hit" not in fila
    assert "tokens_cache_miss" not in fila


def test_deepseek_log_uso_registra_tokens_de_cache_del_proveedor(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    estado.crear_clase("Costo cache", curso="curso-costos")

    class _UsageConCache:
        prompt_tokens = 1000
        completion_tokens = 500
        prompt_cache_hit_tokens = 640
        prompt_cache_miss_tokens = 360

    class _RespuestaConCache:
        usage = _UsageConCache()

    deepseek_client._log_uso(_RespuestaConCache(), "unit-cache")

    fila = estado.leer_jsonl(config.LOGS_DIR / "uso_api.jsonl")[-1]
    assert fila["tokens_cache_hit"] == 640
    assert fila["tokens_cache_miss"] == 360
    # El costo registrado no cambia: los contadores son solo visibilidad.
    assert fila["costo_usd"] == round(
        (1000 * config.COSTO_ENTRADA_USD_M
         + 500 * config.COSTO_SALIDA_USD_M) / 1_000_000, 6)


def test_deepseek_log_uso_acepta_cached_tokens_formato_openai(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    estado.crear_clase("Costo cache openai", curso="curso-costos")

    class _Detalles:
        cached_tokens = 128

    class _UsageOpenAI:
        prompt_tokens = 1000
        completion_tokens = 500
        prompt_tokens_details = _Detalles()

    class _RespuestaOpenAI:
        usage = _UsageOpenAI()

    deepseek_client._log_uso(_RespuestaOpenAI(), "unit-cache-openai")

    fila = estado.leer_jsonl(config.LOGS_DIR / "uso_api.jsonl")[-1]
    assert fila["tokens_cache_hit"] == 128
    assert "tokens_cache_miss" not in fila


def test_api_estado_muestra_gasto_sesion_y_curso_sin_total_global(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)

    d1 = estado.crear_clase("Tema A", curso="curso-a")
    _fijar_rango(d1, base=1000.0)
    d2 = estado.crear_clase("Tema B", curso="curso-a")
    _fijar_rango(d2, base=2000.0)
    d3 = estado.crear_clase("Otro curso", curso="curso-b")
    _fijar_rango(d3, base=3000.0)
    estado.apuntar_clase(d2)

    _log_api(t=50.0, costo_usd=9.0)  # historico fuera de cualquier curso
    _log_api(t=1020.0, costo_usd=0.1)  # log antiguo sin metadata dentro de d1
    _log_api(t=9999.0, costo_usd=0.2, curso="curso-a", clase_id=d2.name,
             clase_path=_ruta_log(d2))
    _log_api(t=3020.0, costo_usd=0.7)  # log antiguo de otro curso
    _log_api(t=9999.0, costo_usd=3.0, curso="curso-b", clase_id=d3.name,
             clase_path=_ruta_log(d3))

    data = panel.app.test_client().get("/api/estado").get_json()

    assert data["curso"] == "curso-a"
    assert data["costo_clase"] == "0.200"
    assert data["costo_curso"] == "0.300"
    assert data["costo"] == "0.200"
    assert data["costo_sesion"] == "0.200"
    assert data["costo_display"] == "Sesión $0.200 · Curso $0.300"
    assert data["costo_etiqueta"] == "Gasto API"
    assert "costo_global" not in data
    assert "costo_global_sistema" not in data
    assert len(data["costos_lecciones"]) == 2
    assert {x["clase_id"]: x["costo_usd"] for x in data["costos_lecciones"]} == {
        d1.name: 0.1,
        d2.name: 0.2,
    }


def test_api_estado_sin_clase_no_muestra_historico_como_curso(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    _log_api(t=100.0, costo_usd=4.25)

    data = panel.app.test_client().get("/api/estado").get_json()

    assert data["estado"] == "sin clase"
    assert data["costo"] == "0.000"
    assert data["costo_sesion"] == "0.000"
    assert data["costo_curso"] == "0.000"
    assert data["costo_display"] == "Sesión $0.000 · Curso $0.000"
    assert "costo_global" not in data
    assert "costo_global_sistema" not in data


def test_postproceso_reporta_solo_gasto_sesion_y_curso(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    d = estado.crear_clase("Sesión auditada", curso="curso-costos")
    _fijar_rango(d, base=1000.0)
    _log_api(t=1020.0, costo_usd=0.125)
    _log_api(t=9999.0, costo_usd=0.375, curso="curso-costos",
             clase_id="otra", clase_path="contenido/curso-costos/clases/otra")

    resumen = costos_api.resumen_curso(
        "curso-costos", ahora=1200.0, clase_actual=d)
    lineas = post_procesar._lineas_gasto_api(resumen)

    assert lineas == [
        "[post] gasto API en esta sesión: $0.1250 USD",
        "[post] gasto API en este curso hasta ahora: $0.5000 USD",
    ]
    assert "total" not in "\n".join(lineas).casefold()


def test_log_costos_concurrente_es_jsonl_valido_y_no_pierde_llamadas(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    monkeypatch.setattr(deepseek_client, "_contexto_uso_api", lambda: {
        "curso": "curso-concurrente", "clase_id": "clase-a",
        "clase_path": "contenido/curso-concurrente/clases/clase-a",
    })

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(
            lambda indice: deepseek_client._log_uso(
                _Respuesta(), "concurrente", usage_id=f"call-{indice}"),
            range(40),
        ))

    filas = estado.leer_jsonl(config.LOGS_DIR / "uso_api.jsonl")
    assert len(filas) == 40
    assert {fila["usage_id"] for fila in filas} == {
        f"call-{indice}" for indice in range(40)}
    assert all(fila["curso"] == "curso-concurrente" for fila in filas)


def test_usage_id_persistido_evitar_duplicado_tras_reintento_o_reinicio(
        monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    monkeypatch.setattr(deepseek_client, "_contexto_uso_api", lambda: {
        "curso": "curso-a", "clase_id": "clase-reinicio",
    })

    deepseek_client._log_uso(
        _Respuesta(), "reinicio", usage_id="request-estable-1")
    deepseek_client._log_uso(
        _Respuesta(), "reinicio", usage_id="request-estable-1")

    filas = estado.leer_jsonl(config.LOGS_DIR / "uso_api.jsonl")
    assert len(filas) == 1
    assert filas[0]["usage_id"] == "request-estable-1"


def test_resumen_costos_lee_solo_append_nuevo(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    incremental_jsonl.clear_readers()
    costos_api.clear_caches()
    d = estado.crear_clase("Costo incremental", curso="curso-incremental")
    _fijar_rango(d, base=1000.0)
    _log_api(t=1020.0, costo_usd=0.1)

    first = costos_api.resumen_curso(
        "curso-incremental", ahora=1100.0, clase_actual=d)
    reader = incremental_jsonl.reader_for(config.LOGS_DIR / "uso_api.jsonl")
    initial_metrics = reader.metrics()
    initial_index = costos_api.cache_metrics()
    second = costos_api.resumen_curso(
        "curso-incremental", ahora=1100.0, clase_actual=d)

    assert first == second
    assert reader.metrics()["bytes_read"] == initial_metrics["bytes_read"]
    assert costos_api.cache_metrics()["usage"]["rows_indexed"] == 1
    assert costos_api.cache_metrics()["classes"]["reads"] == 1
    assert costos_api.cache_metrics()["classes"]["hits"] > (
        initial_index["classes"]["hits"])
    _log_api(t=1030.0, costo_usd=0.2)
    third = costos_api.resumen_curso(
        "curso-incremental", ahora=1100.0, clase_actual=d)
    assert third["total_usd"] == 0.3
    assert reader.metrics()["incremental_reads"] == 1
    final_metrics = costos_api.cache_metrics()
    assert final_metrics["usage"]["rows_indexed"] == 2
    assert final_metrics["usage"]["aggregate_rebuilds"] == 1
    assert final_metrics["classes"]["reads"] == 1


def test_indice_costos_conserva_atribucion_metadata_y_legacy(monkeypatch, tmp_path):
    _aislar_entorno(monkeypatch, tmp_path)
    incremental_jsonl.clear_readers()
    costos_api.clear_caches()
    first = estado.crear_clase("Primera", curso="curso-mixto")
    _fijar_rango(first, base=1000.0)
    second = estado.crear_clase("Segunda", curso="curso-mixto")
    _fijar_rango(second, base=2000.0)
    rows = [
        {"t": 1010.0, "costo_usd": 0.10},
        {"t": 2010.0, "costo_usd": 0.20},
        {"t": 9999.0, "costo_usd": 0.30, "curso": "curso-mixto"},
        {"t": 9999.0, "costo_usd": 0.40, "clase_path": _ruta_log(first)},
        {"t": 9999.0, "costo_usd": 0.50, "clase_id": second.name},
        {"t": 9999.0, "costo_usd": 0.60, "curso": "curso-mixto",
         "clase_id": first.name},
        {"t": 9999.0, "costo_usd": 9.00, "curso": "otro-curso",
         "clase_path": _ruta_log(first)},
    ]
    for row in rows:
        _log_api(**row)

    classes = costos_api._clases("curso-mixto", 3000.0)
    raw = estado.leer_jsonl(config.LOGS_DIR / "uso_api.jsonl")
    expected_course = round(sum(
        costos_api._costo(row) for row in raw
        if costos_api._es_de_curso(row, "curso-mixto", classes)), 6)
    expected_classes = {
        d.name: round(sum(
            costos_api._costo(row) for row in raw
            if costos_api._es_de_clase(row, d, st, span)), 6)
        for d, st, span in classes
    }

    result = costos_api.resumen_curso(
        "curso-mixto", ahora=3000.0, clase_actual=second)

    assert result["total_usd"] == expected_course
    assert {item["clase_id"]: item["costo_usd"]
            for item in result["lecciones"]} == expected_classes
