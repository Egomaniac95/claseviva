from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from src import copiloto, deepseek_client, panel, web


RSS = b'''<?xml version="1.0"?><rss><channel>
<item><title>Modelos: fuente verificable</title><link>https://example.org/article</link>
<description>Resumen para la consulta</description></item>
<item><title>Invalido</title><link>file:///secret</link></item>
</channel></rss>'''


class RespuestaHTTP:
    def __init__(self, contenido=RSS):
        self.contenido = contenido

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, limite):
        return self.contenido[:limite]


def test_boton_web_usa_fetch_y_chat_sin_herramientas_deepseek(monkeypatch, tmp_path):
    solicitudes = []
    llamadas_chat = []

    def fetch(req, timeout):
        solicitudes.append((req.full_url, timeout))
        return RespuestaHTTP()

    def create(**kwargs):
        llamadas_chat.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="Respuesta con fuente"), finish_reason="stop")])

    monkeypatch.setattr(web, "urlopen", fetch)
    monkeypatch.setattr(web, "buscar", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("el botón Web no debe usar otro buscador")))
    monkeypatch.setattr(deepseek_client, "_cliente", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(deepseek_client, "_log_uso", lambda *_a, **_k: None)
    monkeypatch.setattr(copiloto, "contexto_previo", lambda _d: "")
    monkeypatch.setattr(copiloto, "notas_hoy", lambda _d: "")
    monkeypatch.setattr(copiloto, "transcript_reciente", lambda _d: "")
    monkeypatch.setattr(copiloto, "registrar_qa", lambda *_a: None)
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)

    respuesta = panel.app.test_client().post(
        "/api/pregunta", json={"q": "modelos de IA", "web": True})

    assert respuesta.get_json()["ok"] is True
    assert len(solicitudes) == 1
    url, timeout = solicitudes[0]
    assert urlsplit(url).hostname == "www.bing.com"
    assert parse_qs(urlsplit(url).query)["q"] == ["modelos de IA"]
    assert timeout == 8.0
    assert len(llamadas_chat) == 1
    assert "tools" not in llamadas_chat[0] and "tool_choice" not in llamadas_chat[0]
    assert "https://example.org/article" in llamadas_chat[0]["messages"][1]["content"]
    assert "file:///secret" not in llamadas_chat[0]["messages"][1]["content"]


def test_fetch_fallido_avisa_sin_inventar_fuentes(monkeypatch):
    def falla(*_args, **_kwargs):
        raise URLError("sin conexión")
    monkeypatch.setattr(web, "urlopen", falla)
    monkeypatch.setattr(web.config, "log_error", lambda *_a: None)
    contexto = web.bloque_contexto("pregunta")
    assert "WEB NO DISPONIBLE" in contexto
    assert "no inventes enlaces" in contexto


def test_fetch_rechaza_respuesta_no_rss_y_limita_tamano(monkeypatch):
    monkeypatch.setattr(web.config, "log_error", lambda *_a: None)
    monkeypatch.setattr(web, "urlopen", lambda *_a, **_k: RespuestaHTTP(b"<html>error"))
    assert web.buscar_por_fetch("pregunta") == []
    monkeypatch.setattr(web, "urlopen", lambda *_a, **_k: RespuestaHTTP(b"x" * 500_001))
    assert web.buscar_por_fetch("pregunta") == []


def test_fetch_descarta_resultados_ajenos_a_la_consulta(monkeypatch):
    monkeypatch.setattr(web, "urlopen", lambda *_a, **_k: RespuestaHTTP())
    assert web.buscar_por_fetch("diabetes mellitus") == []
