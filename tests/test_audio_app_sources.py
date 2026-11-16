"""Selección de aplicaciones abiertas, árbol de procesos y reutilización de PID."""
from contextlib import contextmanager
from types import SimpleNamespace

import psutil
import pytest

from src import audio_sesiones, captura_audio, estado, panel


class Process:
    def __init__(self, pid, name, birth, parent=None):
        self.pid, self._name, self.birth, self._parent = pid, name, birth, parent

    def name(self):
        return self._name

    def create_time(self):
        return self.birth

    def is_running(self):
        return True

    def parent(self):
        return self._parent

    def parents(self):
        return [] if self._parent is None else [self._parent, *self._parent.parents()]


def session(process, peak=0.0):
    volume = SimpleNamespace(muted=False, GetMasterVolume=lambda: 1.0)
    volume.GetMute = lambda: volume.muted
    volume.SetMute = lambda muted, _: setattr(volume, "muted", bool(muted))
    return SimpleNamespace(
        Process=process, State=0, InstanceIdentifier=f"session-{process.pid}",
        SimpleAudioVolume=volume,
        _ctl=SimpleNamespace(QueryInterface=lambda _: SimpleNamespace(GetPeakValue=lambda: peak)))


@pytest.fixture
def apps(monkeypatch):
    comet = Process(101, "comet.exe", 1000.0)
    zoom = Process(202, "Zoom.exe", 2000.0)
    child = Process(203, "Zoom.exe", 2001.0, parent=zoom)
    another_zoom = Process(204, "Zoom.exe", 2002.0)
    processes = {p.pid: p for p in [comet, zoom, child, another_zoom]}
    sessions = [session(child, .3), session(zoom), session(another_zoom)]

    def process(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]

    @contextmanager
    def enumerate_sessions():
        yield list(sessions)

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(audio_sesiones, "disponible", lambda: True)
    monkeypatch.setattr(audio_sesiones, "_sesiones", enumerate_sessions)
    monkeypatch.setattr(audio_sesiones, "_pids_ventanas", lambda: {101, 202, 999})
    monkeypatch.setattr(audio_sesiones, "_enfoque_snapshots", {})
    return SimpleNamespace(processes=processes, sessions=sessions,
                           comet="proceso:101:1000000", zoom="proceso:202:2000000")


def test_inventory_lists_comet_without_playback_and_groups_zoom_children(apps):
    sources = audio_sesiones.aplicaciones_audio()
    assert {s["pid"] for s in sources} == {101, 202, 204}
    comet = next(s for s in sources if s["pid"] == 101)
    zoom = next(s for s in sources if s["pid"] == 202)
    assert comet["id"] == apps.comet
    assert comet["sesiones"] == 0 and not comet["senal_detectada"]
    assert "Abierta, sin audio" in comet["detalle"]
    assert comet["etiqueta"] == "Comet"
    assert zoom["id"] == apps.zoom and zoom["sesiones"] == 2
    assert zoom["senal_detectada"] and "PID 202" in zoom["etiqueta"]
    # Otro Zoom independiente conserva su propio PID, aunque su EXE se llame igual.
    assert next(s for s in sources if s["pid"] == 204)["sesiones"] == 1


def test_output_sessions_include_other_speakers_without_duplicates(monkeypatch):
    from pycaw.pycaw import AudioUtilities
    from pycaw import utils
    from pycaw.constants import DEVICE_STATE, EDataFlow

    default = session(Process(101, "comet.exe", 1000.0))
    other = session(Process(202, "Zoom.exe", 2000.0))
    seen = []
    enumerator = SimpleNamespace(
        GetCount=lambda: 2,
        GetSession=lambda index: SimpleNamespace(QueryInterface=lambda _: [default, other][index]))

    def devices(**kwargs):
        seen.append(kwargs)
        return [SimpleNamespace(AudioSessionManager=SimpleNamespace(GetSessionEnumerator=lambda: enumerator))]

    monkeypatch.setattr(AudioUtilities, "GetAllSessions", lambda: [default])
    monkeypatch.setattr(AudioUtilities, "GetAllDevices", devices)
    monkeypatch.setattr(utils, "AudioSession", lambda ctl: ctl)
    with audio_sesiones._sesiones() as sessions:
        assert sessions == [default, other]
    assert seen == [{"data_flow": EDataFlow.eRender.value, "device_state": DEVICE_STATE.ACTIVE.value}]


def test_process_capture_targets_selected_root_and_not_other_instance(apps):
    sessions = audio_sesiones.sesiones_navegador(apps.zoom)
    assert {s["pid"] for s in sessions} == {202, 203}
    assert {s["root_pid"] for s in sessions} == {202}
    assert audio_sesiones.pid_navegador(apps.zoom) == 202
    assert audio_sesiones.pico_navegador(apps.zoom) == .3
    comet = audio_sesiones.sesiones_navegador(apps.comet)
    assert len(comet) == 1 and comet[0]["root_pid"] == 101


def test_reused_or_closed_pid_is_not_accepted_as_same_app(apps):
    assert audio_sesiones.fuente_valida(apps.zoom)
    apps.processes[202].birth = 3000.0
    assert not audio_sesiones.fuente_valida(apps.zoom)
    assert audio_sesiones.sesiones_navegador(apps.zoom) == []
    del apps.processes[101]
    assert not audio_sesiones.fuente_valida(apps.comet)
    assert audio_sesiones.nombre_fuente(apps.comet) == "Aplicacion cerrada"
    assert audio_sesiones.enfoque(apps.comet, True) == 0


def test_focus_preserves_target_children_and_restores_other_instance(apps):
    assert audio_sesiones.enfoque(apps.zoom, True) == 1
    assert not apps.sessions[0].SimpleAudioVolume.GetMute()
    assert not apps.sessions[1].SimpleAudioVolume.GetMute()
    assert apps.sessions[2].SimpleAudioVolume.GetMute()
    # Debe poder restaurar el audio ajeno aunque se haya cerrado la aplicación elegida.
    del apps.processes[202]
    assert audio_sesiones.restaurar_enfoques() == 1
    assert not apps.sessions[2].SimpleAudioVolume.GetMute()


def test_live_source_command_saves_selected_process_identity(apps, monkeypatch, tmp_path):
    estado.escribir_estado(tmp_path, {"estado": "grabando", "fuente": "sistema", "parte": 1})
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)
    response = panel.app.test_client().post("/api/comando", json={"cmd": "fuente " + apps.comet})
    assert response.get_json()["ok"]
    assert estado.leer_estado(tmp_path)["fuente"] == apps.comet
    assert "Comet" in response.get_json()["msg"]


def test_inventory_excludes_system_windows_but_discovers_unknown_audio_apps(apps, monkeypatch):
    extra = [Process(301, "Taskmgr.exe", 3000), Process(302, "explorer.exe", 3001),
             Process(303, "GHelper.exe", 3002), Process(304, "MiReproductor.exe", 3003)]
    apps.processes.update({p.pid: p for p in extra})
    apps.sessions.extend([session(extra[0], .2), session(extra[3], .4)])
    monkeypatch.setattr(audio_sesiones, "_pids_ventanas", lambda: {101, 202, 301, 302, 303})
    sources = audio_sesiones.aplicaciones_audio()
    assert not {301, 302, 303} & {s["pid"] for s in sources}
    assert not audio_sesiones.fuente_valida("proceso:301:3000000")
    assert not audio_sesiones.fuente_valida("proceso:302:3001000")
    unknown = next(s for s in sources if s["pid"] == 304)
    assert unknown["etiqueta"] == "MiReproductor" and unknown["senal_detectada"]


def test_edge_is_named_once_and_embedded_webview_belongs_to_its_app(apps):
    edge = Process(401, "msedge.exe", 4000)
    edge_child = Process(402, "msedge.exe", 4001, parent=edge)
    chatgpt = Process(501, "ChatGPT.exe", 5000)
    webview = Process(502, "msedgewebview2.exe", 5001, parent=chatgpt)
    orphan = Process(601, "msedgewebview2.exe", 6000)
    apps.processes.update({p.pid: p for p in [edge, edge_child, chatgpt, webview, orphan]})
    apps.sessions.extend([session(edge_child, .2), session(edge), session(webview, .3), session(orphan)])
    sources = audio_sesiones.aplicaciones_audio()
    edges = [s for s in sources if s["aplicacion"] == "Microsoft Edge"]
    assert len(edges) == 1 and edges[0]["pid"] == 401
    assert edges[0]["etiqueta"] == "Microsoft Edge" and edges[0]["icono"] == "msedge"
    embedded = next(s for s in sources if s["pid"] == 501)
    assert embedded["etiqueta"] == "ChatGPT" and embedded["senal_detectada"]
    assert not {402, 502, 601} & {s["pid"] for s in sources}
    assert not audio_sesiones.fuente_valida("proceso:502:5001000")
    assert audio_sesiones.nombre_fuente(edges[0]["id"]) == "Microsoft Edge"
    assert {s["pid"] for s in audio_sesiones.sesiones_navegador(embedded["id"])} == {502}


def test_muted_app_reports_mute_without_claiming_detected_signal(apps):
    for s in apps.sessions:
        if s.Process.pid in {202, 203}:
            s.SimpleAudioVolume.muted = True
    source = next(s for s in audio_sesiones.aplicaciones_audio() if s["pid"] == 202)
    assert source["silenciada"] and not source["senal_detectada"]
    assert "Silenciada en Windows" in source["detalle"]


def test_start_rejects_closed_source_before_creating_a_class(apps, monkeypatch):
    monkeypatch.setattr(panel, "MODO_APP", True)
    monkeypatch.setattr(panel, "_clase", lambda: pytest.fail("No debe crear ni reanudar una clase"))
    response = panel.app.test_client().post("/api/iniciar", json={"fuente": "proceso:999:1000"})
    assert response.status_code == 409


def test_watchdog_and_audio_health_apply_to_any_program(apps):
    assert captura_audio.clasificar_muestra_audio(
        apps.comet, rms=0, peak=0, pico_sesion=.2)["captura_rota"]
    with pytest.raises(captura_audio.CapturaRotaTemprana):
        watchdog = captura_audio.WatchdogCaptura(apps.zoom, obtener_pico=lambda _: .2)
        watchdog.observar(b"\0\0" * 200, rate=100, canales=1)
    classified = panel.clasificar_estado_audio({
        "estado": "grabando", "fuente": apps.zoom, "audio_capture_phase": "source_closed"})
    assert classified["phase"] == "requires_attention"
