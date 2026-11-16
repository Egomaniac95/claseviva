import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src import panel


def test_system_icon_picker_javascript_is_valid():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node no está disponible")
    script = Path(__file__).resolve().parents[1] / "assets" / "source_picker.js"
    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_audio_refresh_distinguishes_process_session_and_signal(monkeypatch):
    monkeypatch.setattr(panel.audio_sesiones, "aplicaciones_audio", lambda: [
        {"id": "proceso:101:1000", "etiqueta": "Comet", "icono": "app",
         "abierto": True, "senal_detectada": False, "detalle": "ventana abierta"},
        {"id": "proceso:202:2000", "etiqueta": "Zoom", "icono": "app",
         "abierto": True, "senal_detectada": True, "detalle": "audio detectado ahora"},
    ])
    sources = panel._fuentes_audio_disponibles()
    comet, zoom, system = sources
    assert comet["abierto"] and not comet["senal_detectada"]
    assert zoom["senal_detectada"]
    assert "otras apps" in system["detalle"]
    assert 'id="source-app"' in panel.PAGINA


def test_repeated_validated_fallback_is_active_not_trying():
    st = {"estado": "grabando", "fuente": "msedge", "audio_capture_phase": "trying_device_fallback",
          "audio_capture_stream_id": "stream-a", "audio_capture_mode": "msedge:dispositivo",
          "audio_route_health": {"last_result": "valid", "stream_id": "stream-a", "mode": "msedge:dispositivo"}}
    result = panel.clasificar_estado_audio(st, valid_wav_count=2)
    assert result["phase"] == "active"
    assert "mezcla del sistema" in result["label"]
    st["audio_capture_stream_id"] = "stream-b"
    result = panel.clasificar_estado_audio(st, valid_wav_count=2)
    assert result["phase"] == "fallback"


def test_audio_selection_shows_one_status_and_the_correct_browser_icon():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node no está disponible")
    match = re.search(r"(function fillSourceSelect[\s\S]+?)async function refreshSources", panel.PAGINA)
    assert match
    script = r"""
const assert=require('node:assert/strict');
let AUDIO_SOURCES=[];
class Element {
 constructor(){this.children=[];this.dataset={};this.attrs={};this._text='';}
 set textContent(s){this._text=s;this.children=[];}
 get textContent(){return this.children.length?this.children.map(c=>c.textContent).join(' '):this._text;}
 setAttribute(k,v){this.attrs[k]=v;}
 replaceChildren(...nodes){this.children=[...nodes];}
}
class Select extends Element {
 constructor(){super();this.value='sistema';this.children=[Object.assign(new Element(),{value:'sistema',textContent:'Todo el sistema'})];}
 get options(){return this.children;}
 get selectedOptions(){return this.children.filter(o=>o.value===this.value);}
 appendChild(option){this.children.push(option);}
}
const nodes={ifuente:new Select(),self:new Select(),ifuenteIcon:new Element(),selfIcon:new Element(),
 startAudioSourcesStatus:new Element(),audioSourcesStatus:new Element()};
const document={getElementById:id=>nodes[id],createElement:()=>new Element()};
""" + match[1] + r"""
const edge={id:'proceso:101:1000',etiqueta:'Microsoft Edge',aplicacion:'Microsoft Edge',icono:'msedge',pid:101,
 senal_detectada:true,detalle:'Audio detectado. Solo esta aplicación.'};
const comet={id:'proceso:202:2000',etiqueta:'Comet',aplicacion:'Comet',icono:'app',pid:202,
 senal_detectada:false,detalle:'Abierta, sin audio todavía.'};
renderAudioSources([edge,comet,{id:'sistema',etiqueta:'Todo el sistema'}]);
nodes.ifuente.value=edge.id;updateSourceIcon('ifuente');
assert.equal(nodes.ifuenteIcon.attrs.href,'#source-msedge');
assert.equal(nodes.startAudioSourcesStatus.dataset.state,'signal');
assert(nodes.startAudioSourcesStatus.textContent.includes('Microsoft Edge'));
assert(!nodes.startAudioSourcesStatus.textContent.includes('Comet'));
assert.equal(nodes.startAudioSourcesStatus.children.length,3);
nodes.ifuente.value=comet.id;updateSourceIcon('ifuente');
assert.equal(nodes.ifuenteIcon.attrs.href,'#source-app');
assert.equal(nodes.startAudioSourcesStatus.dataset.state,'ready');
assert(!nodes.startAudioSourcesStatus.textContent.includes('Microsoft Edge'));
nodes.self.value=edge.id;updateSourceIcon('self');
assert.equal(nodes.audioSourcesStatus.dataset.state,'signal');
assert(nodes.startAudioSourcesStatus.textContent.includes('Comet'));
renderAudioSources([comet,{id:'sistema',etiqueta:'Todo el sistema'}]);
assert.equal(nodes.self.value,edge.id);
assert.equal(nodes.audioSourcesStatus.dataset.state,'missing');
assert(nodes.audioSourcesStatus.textContent.includes('no está disponible'));
"""
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8",
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
