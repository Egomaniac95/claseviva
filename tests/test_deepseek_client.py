from types import SimpleNamespace

from src import deepseek_client


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
