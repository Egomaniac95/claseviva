from types import SimpleNamespace
import json

from src import deepseek_client
import pytest


@pytest.mark.parametrize("reason", ["length", "content_filter", "insufficient_system_resource", "aborted"])
def test_text_caller_rejects_incomplete_completion(monkeypatch, reason):
    monkeypatch.setattr(deepseek_client, "preguntar_detalle", lambda *a, **kw: {
        "texto": "Una explicación que quedó cortada", "finish_reason": reason, "error": None,
    })
    errors = []
    monkeypatch.setattr(deepseek_client.config, "log_error", lambda *args: errors.append(args))
    assert deepseek_client.preguntar([{"role": "user", "content": "clase"}]) is None
    assert reason in errors[0][1]


def test_usage_records_completion_status_and_latency(monkeypatch, tmp_path):
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="length")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
    )
    monkeypatch.setattr(deepseek_client.config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(deepseek_client.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(deepseek_client, "_contexto_uso_api", lambda: {})
    monkeypatch.setattr(deepseek_client.time, "time", lambda: 125.5)
    deepseek_client._log_uso(response, "post-chunk", request_started=120.0)
    row = json.loads((tmp_path / "uso_api.jsonl").read_text(encoding="utf-8"))
    assert row["finish_reason"] == "length"
    assert row["duration_seconds"] == 5.5


def test_thinking_es_opt_in_por_llamada_y_no_expone_razonamiento(monkeypatch):
    llamadas = []
    respuesta = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="contenido visible",
                reasoning_content="SECRETO_RAZONAMIENTO",
            ),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )

    def create(**kwargs):
        llamadas.append(kwargs)
        return respuesta

    cliente = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(deepseek_client, "_cliente", lambda: cliente)
    monkeypatch.setattr(deepseek_client, "_log_uso", lambda *_args, **_kwargs: None)

    por_defecto = deepseek_client.preguntar_detalle(
        [{"role": "user", "content": "copiloto"}])
    desactivado = deepseek_client.preguntar_detalle(
        [{"role": "user", "content": "escriba"}], thinking=False)
    activado = deepseek_client.preguntar(
        [{"role": "user", "content": "opt-in"}], thinking=True)

    assert "extra_body" not in llamadas[0]
    assert llamadas[1]["extra_body"] == {
        "thinking": {"type": "disabled"},
    }
    assert llamadas[2]["extra_body"] == {
        "thinking": {"type": "enabled"},
    }
    assert por_defecto == {
        "texto": "contenido visible", "finish_reason": "stop", "error": None,
    }
    assert activado == "contenido visible"
    assert desactivado["texto"] == "contenido visible"
    assert "reasoning" not in repr(por_defecto).lower()
    assert "SECRETO_RAZONAMIENTO" not in repr(por_defecto)


def test_log_de_uso_no_persiste_reasoning_content(monkeypatch, tmp_path):
    respuesta = SimpleNamespace(
        id="usage-test",
        reasoning_content="SECRETO_RAZONAMIENTO",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    monkeypatch.setattr(deepseek_client.config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(deepseek_client.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(deepseek_client, "_contexto_uso_api", lambda: {})

    deepseek_client._log_uso(respuesta, "test", usage_id="usage-test")

    registro = (tmp_path / "uso_api.jsonl").read_text(encoding="utf-8")
    assert "SECRETO_RAZONAMIENTO" not in registro
    assert "reasoning_content" not in registro


def test_log_conserva_modelo_efectivo_tarifa_y_historico(monkeypatch, tmp_path):
    respuesta = SimpleNamespace(
        model="deepseek-v4-pro",
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500,
                              prompt_cache_hit_tokens=640, prompt_cache_miss_tokens=360),
    )
    monkeypatch.setattr(deepseek_client.config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(deepseek_client.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(deepseek_client.config, "DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setattr(deepseek_client.config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(deepseek_client, "_contexto_uso_api", lambda: {})
    path = tmp_path / "uso_api.jsonl"
    legacy = b'{"t":1,"costo_usd":0.123,"modelo":"legacy"}\n'
    path.write_bytes(legacy)
    deepseek_client._log_uso(respuesta, "test", usage_id="test-new")
    assert path.read_bytes().startswith(legacy)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["modelo"] == "deepseek-v4-pro"
    assert row["modelo_solicitado"] == "deepseek-v4-flash"
    assert row["modelo_respuesta"] == "deepseek-v4-pro"
    assert row["estimacion_costo"]["modelo_tarifa"] == "pro"
    assert row["estimacion_costo"]["modelo_base"] == "respuesta"


def test_log_modelo_desconocido_no_asume_tarifa_solicitada(monkeypatch, tmp_path):
    respuesta = SimpleNamespace(
        model="modelo-sin-tarifa",
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500),
    )
    monkeypatch.setattr(deepseek_client.config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(deepseek_client.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(deepseek_client.config, "DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setattr(deepseek_client.config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(deepseek_client, "_contexto_uso_api", lambda: {})
    deepseek_client._log_uso(respuesta, "test", usage_id="test-unknown")
    row = json.loads((tmp_path / "uso_api.jsonl").read_text(encoding="utf-8"))
    assert row["costo_usd"] is None
    assert row["estimacion_costo"]["motivo"] == "modelo_sin_tarifa_verificada"
    assert deepseek_client.costo_acumulado() == 0.0
