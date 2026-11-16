"""Duplicados de Enter/clic, reintentos HTTP y registro único sin consumir API."""
import re
import shutil
import subprocess
import threading
from collections import OrderedDict

import pytest

from src import panel


@pytest.fixture
def chat(monkeypatch, tmp_path):
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)
    monkeypatch.setattr(panel, "_COPILOTO_ACTIVOS", set())
    monkeypatch.setattr(panel, "_COPILOTO_RESPUESTAS", OrderedDict())
    return tmp_path


def test_repeated_request_has_one_answer_and_one_saved_pair(monkeypatch, chat):
    calls = []
    monkeypatch.setattr(panel.copiloto, "responder", lambda _d, q, **_kw:
                        calls.append(q) or "Respuesta de prueba")
    client = panel.app.test_client()
    payload = {"q": "Consulta", "request_id": "submission-1"}
    first = client.post("/api/pregunta", json=payload)
    repeated = client.post("/api/pregunta", json=payload)
    assert first.status_code == repeated.status_code == 200
    assert first.get_json() == repeated.get_json()
    assert calls == ["Consulta"]
    assert (chat / "qa.md").read_text(encoding="utf-8").count("### ❓") == 1
    conflict = client.post("/api/pregunta", json={**payload, "q": "Otra consulta"})
    assert conflict.status_code == 409
    # Preguntar lo mismo de forma intencional después sí es una nueva consulta.
    assert client.post("/api/pregunta", json={**payload, "request_id": "submission-2"}).status_code == 200
    assert calls == ["Consulta", "Consulta"]


def test_concurrent_http_requests_do_not_call_model_twice(monkeypatch, chat):
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []

    def respond(_d, q, **_kw):
        calls.append(q)
        entered.set()
        assert release.wait(5)
        return "Una respuesta"

    monkeypatch.setattr(panel.copiloto, "responder", respond)
    payload = {"q": "Consulta", "request_id": "concurrent-1"}

    def send():
        with panel.app.test_client() as client:
            results.append(client.post("/api/pregunta", json=payload).status_code)

    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert entered.wait(5)
        with panel.app.test_client() as client:
            assert client.post("/api/pregunta", json=payload).status_code == 409
            assert client.post("/api/pregunta", json={**payload, "request_id": "concurrent-2"}).status_code == 409
    finally:
        release.set()
        thread.join(5)
    assert results == [200]
    assert calls == ["Consulta"]
    assert (chat / "qa.md").read_text(encoding="utf-8").count("### ❓") == 1


def test_screenshot_retry_is_recorded_once(monkeypatch, chat):
    calls = []

    def respond(d, q, name, **_kw):
        calls.append(name)
        panel.copiloto.registrar_qa(d, q, "Respuesta con captura")
        return "Respuesta con captura"

    monkeypatch.setattr(panel.copiloto, "responder_con_captura", respond)
    client = panel.app.test_client()
    payload = {"q": "Explica", "captura": "consulta_123.jpg", "request_id": "screenshot-1"}
    assert client.post("/api/pregunta", json=payload).get_json()["ok"]
    assert client.post("/api/pregunta", json=payload).get_json()["ok"]
    assert calls == ["consulta_123.jpg"]
    assert (chat / "qa.md").read_text(encoding="utf-8").count("### ❓") == 1


def test_enter_repeat_and_click_render_only_one_pair_and_keep_next_draft():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node no está disponible")
    match = re.search(r"(async function enviarPregunta[\s\S]+?)async function descartarCapturaPrevia", panel.PAGINA)
    assert match
    script = r"""
const assert=require('node:assert/strict');
const crypto={randomUUID:()=> 'test-request'};
let CAPTURA=null,COPILOTO_EN_CURSO=false,pending,calls=0;
const input={value:'Consulta'},send={disabled:false};
const chat={children:[],append(...nodes){this.children.push(...nodes);this.lastElementChild=nodes.at(-1)}};
const nodes={preg:input,btnPregunta:send,chat,webchk:{checked:false},
 cap_previa:{replaceChildren(){},style:{}}};
const document={getElementById:id=>nodes[id],createElement:()=>({setAttribute(){},offsetTop:0})};
const resetPregunta=()=>{input.value=''};
const setBusy=(el,on)=>{if(el)el.disabled=on};
const runBusy=async(el,label,fn)=>fn();
const decorateMarkdown=()=>{};
const esc=s=>s;
const api=()=>{calls++;return new Promise(resolve=>pending=resolve)};
""" + match[1] + r"""
(async()=>{
 const first=enviarPregunta(null);
 await enviarPregunta(null);await enviarPregunta(send);
 assert.equal(calls,1);assert.equal(chat.children.length,2);assert.equal(send.disabled,true);
 input.value='Borrador siguiente';
 pending({ok:true,respuesta_html:'Respuesta'});await first;
 assert.equal(chat.children.length,2);assert.equal(chat.children[1].innerHTML,'Respuesta');
 assert.equal(input.value,'Borrador siguiente');assert.equal(send.disabled,false);
 const second=enviarPregunta(send);assert.equal(calls,2);
 pending({ok:true,respuesta_html:'Segunda respuesta'});await second;
 assert.equal(chat.children.length,4);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8",
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
