import json
import subprocess
import sys
import threading
from types import SimpleNamespace

from src import audio_manifest, config, estado, panel, runtime_shutdown
from src.runtime_shutdown import (
    OwnedProcess,
    ProcessSnapshot,
    ShutdownCoordinator,
    PsutilProcessOps,
    StopPolicy,
    StopResult,
    stop_owned_children,
    stop_owned_process,
)


class FakeProcessOps:
    def __init__(self, snapshots, descendants=None, waits=None):
        self.snapshots = dict(snapshots)
        self.descendant_map = dict(descendants or {})
        self.waits = {
            pid: list(resultados) for pid, resultados in (waits or {}).items()
        }
        self.events = []

    def snapshot(self, pid):
        return self.snapshots.get(pid)

    def descendants(self, pid):
        return list(self.descendant_map.get(pid, []))

    def wait(self, snapshot, timeout):
        pid = snapshot.pid
        self.events.append(("wait", pid, timeout))
        current = self.snapshots.get(pid)
        if current is None:
            return "exited"
        if (current.create_time != snapshot.create_time
                or current.command != snapshot.command):
            return "identity_lost"
        resultados = self.waits.setdefault(pid, [True])
        resultado = resultados.pop(0) if resultados else True
        if isinstance(resultado, str):
            if resultado == "exited":
                self.snapshots.pop(pid, None)
            return resultado
        if resultado:
            self.snapshots.pop(pid, None)
        return "exited" if resultado else "timeout"

    def terminate(self, snapshot):
        self.events.append(("terminate", snapshot.pid))
        return "signaled"

    def kill(self, snapshot):
        self.events.append(("kill", snapshot.pid))
        return "signaled"


def _snapshot(pid, module, *, create_time=100.0, parent_pid=1):
    return ProcessSnapshot(
        pid=pid,
        create_time=create_time,
        command=("python.exe", "-m", module),
        parent_pid=parent_pid,
    )


def _owned(pid, module, *, instance_id="instance-a", create_time=100.0):
    return OwnedProcess(
        module=module,
        instance_id=instance_id,
        root=_snapshot(pid, module, create_time=create_time),
    )


def test_hijo_cooperativo_no_recibe_terminate_ni_kill():
    child = _owned(101, "src.captura_audio")
    ops = FakeProcessOps({101: child.root}, waits={101: [True]})

    result = stop_owned_process(
        child, ops, StopPolicy(1.0, 1.0, 1.0), instance_id="instance-a")

    assert result.status == "cooperative"
    assert not [event for event in ops.events if event[0] in {"terminate", "kill"}]


def test_terminate_real_solo_al_proceso_sintetico_con_fingerprint():
    marker = "clase_viva_shutdown_synthetic_h12"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", marker],
        creationflags=flags,
    )
    ops = PsutilProcessOps()
    snapshot = ops.snapshot(process.pid)
    assert snapshot is not None
    child = OwnedProcess(
        module=marker, instance_id="synthetic-instance", root=snapshot)
    try:
        result = stop_owned_process(
            child, ops, StopPolicy(0.01, 3.0, 1.0),
            instance_id="synthetic-instance")
        assert result.status == "terminated"
        assert process.wait(timeout=3) is not None
    finally:
        if process.poll() is None:
            current = ops.snapshot(process.pid)
            if current is not None and current.create_time == snapshot.create_time:
                process.kill()
                process.wait(timeout=3)


def test_psutil_desaparicion_entre_verificacion_y_senal_es_idempotente(
        monkeypatch):
    import psutil

    snapshot = _snapshot(601, "src.captura_audio")

    class ProcesoQueDesaparece:
        pid = snapshot.pid

        def create_time(self):
            return snapshot.create_time

        def cmdline(self):
            return list(snapshot.command)

        def ppid(self):
            return snapshot.parent_pid

        def terminate(self):
            raise psutil.NoSuchProcess(snapshot.pid)

    class PsutilFalso:
        NoSuchProcess = psutil.NoSuchProcess
        AccessDenied = psutil.AccessDenied
        TimeoutExpired = psutil.TimeoutExpired

        @staticmethod
        def Process(pid):
            assert pid == snapshot.pid
            return ProcesoQueDesaparece()

    ops = PsutilProcessOps()
    monkeypatch.setattr(ops, "_psutil", lambda: PsutilFalso)

    assert ops.terminate(snapshot) == "gone"


def test_psutil_no_senaliza_pid_reutilizado_despues_de_verificacion(
        monkeypatch):
    import psutil

    snapshot = _snapshot(602, "src.captura_audio", create_time=100.0)
    senales = []

    class ProcesoReutilizado:
        pid = snapshot.pid

        def create_time(self):
            return 200.0

        def cmdline(self):
            return ["python.exe", "-m", "otra.aplicacion"]

        def ppid(self):
            return 1

        def terminate(self):
            senales.append(self.pid)

    class PsutilFalso:
        NoSuchProcess = psutil.NoSuchProcess
        AccessDenied = psutil.AccessDenied
        TimeoutExpired = psutil.TimeoutExpired

        @staticmethod
        def Process(pid):
            assert pid == snapshot.pid
            return ProcesoReutilizado()

    ops = PsutilProcessOps()
    monkeypatch.setattr(ops, "_psutil", lambda: PsutilFalso)

    assert ops.terminate(snapshot) == "identity_lost"
    assert senales == []


def test_hijo_bloqueado_escala_a_terminate_solo_tras_timeout():
    child = _owned(102, "src.captura_pantalla")
    descendant = _snapshot(202, "src.captura_pantalla", parent_pid=102)
    ops = FakeProcessOps(
        {102: child.root, 202: descendant},
        descendants={102: [descendant]},
        waits={102: [False, True], 202: [False, True]},
    )

    result = stop_owned_process(
        child, ops, StopPolicy(2.0, 3.0, 4.0), instance_id="instance-a")

    assert result.status == "terminated"
    actions = [(event[0], event[1]) for event in ops.events]
    assert actions == [
        ("wait", 202),
        ("wait", 102),
        ("terminate", 202),
        ("terminate", 102),
        ("wait", 202),
        ("wait", 102),
    ]
    assert sum(event[2] for event in ops.events if event[0] == "wait") <= 10.0


def test_hijo_que_ignora_terminate_escala_a_kill_como_ultimo_recurso():
    child = _owned(103, "src.transcriptor_vivo")
    ops = FakeProcessOps(
        {103: child.root},
        waits={103: [False, False, True]},
    )

    result = stop_owned_process(
        child, ops, StopPolicy(1.0, 2.0, 3.0), instance_id="instance-a")

    assert result.status == "killed"
    assert ops.events == [
        ("wait", 103, 1.0),
        ("terminate", 103),
        ("wait", 103, 2.0),
        ("kill", 103),
        ("wait", 103, 3.0),
    ]


def test_pid_reutilizado_y_proceso_ajeno_no_se_terminan():
    child = _owned(104, "src.escriba", create_time=100.0)
    reused = _snapshot(104, "otra.aplicacion", create_time=200.0)
    ops = FakeProcessOps({104: reused})

    result = stop_owned_process(
        child, ops, StopPolicy(), instance_id="instance-a")

    assert result.status == "ownership_lost"
    assert not [
        event for event in ops.events if event[0] in {"terminate", "kill"}
    ]


def test_instance_id_distinto_no_toca_pid_aunque_coincida_comando():
    child = _owned(105, "src.captura_audio", instance_id="instance-a")
    ops = FakeProcessOps({105: child.root})

    result = stop_owned_process(
        child, ops, StopPolicy(), instance_id="instance-b")

    assert result.status == "ownership_lost"
    assert ops.events == []


def test_descendiente_ajeno_no_se_incluye_en_escalamiento():
    child = _owned(106, "src.captura_audio")
    own_descendant = _snapshot(206, "src.captura_audio", parent_pid=106)
    foreign = _snapshot(306, "otra.aplicacion", parent_pid=106)
    ops = FakeProcessOps(
        {106: child.root, 206: own_descendant, 306: foreign},
        descendants={106: [own_descendant, foreign]},
        waits={106: [False, True], 206: [False, True]},
    )

    stop_owned_process(
        child, ops, StopPolicy(1.0, 1.0, 1.0), instance_id="instance-a")

    assert ("terminate", 206) in ops.events
    assert ("terminate", 306) not in ops.events


def test_raiz_terminado_no_oculta_descendiente_que_requiere_kill():
    root = _owned(107, "src.captura_audio")
    descendant = _snapshot(207, "src.captura_audio", parent_pid=107)

    class RaizTerminaPrimeroOps(FakeProcessOps):
        def wait(self, snapshot, timeout):
            pid = snapshot.pid
            self.events.append(("wait", pid, timeout))
            if pid == root.root.pid:
                return (
                    "exited" if root.root.pid not in self.snapshots else "timeout")
            if pid == descendant.pid:
                return (
                    "exited" if descendant.pid not in self.snapshots else "timeout")
            return "exited"

        def terminate(self, snapshot):
            self.events.append(("terminate", snapshot.pid))
            if snapshot.pid == root.root.pid:
                self.snapshots.pop(snapshot.pid, None)
            return "signaled"

        def kill(self, snapshot):
            self.events.append(("kill", snapshot.pid))
            self.snapshots.pop(snapshot.pid, None)
            return "signaled"

    ops = RaizTerminaPrimeroOps(
        {root.root.pid: root.root, descendant.pid: descendant},
        descendants={root.root.pid: [descendant]},
    )

    result = stop_owned_process(
        root, ops, StopPolicy(0.0, 0.0, 0.0), instance_id="instance-a")

    assert result.status == "killed"
    assert ops.snapshot(descendant.pid) is None
    assert ("kill", descendant.pid) in ops.events


def test_orden_de_cierre_espera_productores_antes_de_consumidores():
    children = [
        _owned(4, "src.escriba"),
        _owned(2, "src.captura_pantalla"),
        _owned(3, "src.transcriptor_vivo"),
        _owned(1, "src.captura_audio"),
    ]
    ops = FakeProcessOps(
        {child.root.pid: child.root for child in children},
        waits={child.root.pid: [True] for child in children},
    )

    results = stop_owned_children(
        children, ops, StopPolicy(), instance_id="instance-a")

    assert [result.module for result in results] == [
        "src.captura_audio",
        "src.captura_pantalla",
        "src.transcriptor_vivo",
        "src.escriba",
    ]


def test_presupuesto_de_espera_es_por_fase_y_no_por_cada_pid(monkeypatch):
    root = _owned(108, "src.captura_audio")
    descendant = _snapshot(208, "src.captura_audio", parent_pid=108)
    clock = {"value": 0.0}

    class BudgetOps(FakeProcessOps):
        def wait(self, snapshot, timeout):
            self.events.append(("wait", snapshot.pid, timeout))
            clock["value"] += timeout
            return "timeout"

    monkeypatch.setattr(
        runtime_shutdown.time, "monotonic", lambda: clock["value"])
    ops = BudgetOps(
        {root.root.pid: root.root, descendant.pid: descendant},
        descendants={root.root.pid: [descendant]},
    )

    result = stop_owned_process(
        root, ops, StopPolicy(2.0, 3.0, 4.0), instance_id="instance-a")

    assert result.status == "unresolved"
    assert result.remaining_pids == (108, 208)
    assert clock["value"] == 9.0


def test_error_en_un_hijo_no_impide_cerrar_los_restantes():
    first = _owned(109, "src.captura_audio")
    second = _owned(110, "src.captura_pantalla")

    class OneBrokenOps(FakeProcessOps):
        def snapshot(self, pid):
            if pid == first.root.pid:
                raise RuntimeError("probe sintético roto")
            return super().snapshot(pid)

    ops = OneBrokenOps(
        {first.root.pid: first.root, second.root.pid: second.root},
        waits={second.root.pid: [True]},
    )

    results = stop_owned_children(
        [first, second], ops, StopPolicy(), instance_id="instance-a")

    assert [result.status for result in results] == [
        "unresolved", "cooperative"]
    assert results[0].remaining_pids == (first.root.pid,)


def test_shutdown_solicitado_dos_veces_ejecuta_una_sola_vez():
    empezo = threading.Event()
    liberar = threading.Event()
    llamadas = []

    def runner(reason, close_app):
        llamadas.append((reason, close_app))
        empezo.set()
        assert liberar.wait(timeout=5)
        return {"status": "complete"}

    coordinator = ShutdownCoordinator(runner)
    first = coordinator.request("api_close", close_app=True)
    assert empezo.wait(timeout=5)
    second = coordinator.request("api_close_again", close_app=True)
    liberar.set()
    assert first.completed.wait(timeout=5)

    assert first.accepted is True
    assert second.accepted is False
    assert second.completed is first.completed
    assert llamadas == [("api_close", True)]
    assert coordinator.report == {"status": "complete"}


def test_coordinador_admite_un_nuevo_ciclo_despues_de_completar_el_anterior():
    llamadas = []

    def runner(reason, close_app):
        llamadas.append((reason, close_app))
        return {"status": "complete", "reason": reason}

    coordinator = ShutdownCoordinator(runner)
    first = coordinator.request("fin_clase", close_app=False)
    assert first.completed.wait(timeout=5)
    second = coordinator.request("cerrar_app", close_app=True)
    assert second.completed.wait(timeout=5)

    assert first.accepted is True
    assert second.accepted is True
    assert first.completed is not second.completed
    assert llamadas == [("fin_clase", False), ("cerrar_app", True)]
    assert coordinator.report == {"status": "complete", "reason": "cerrar_app"}


def test_fin_persistido_declara_finalizando_antes_del_cierre(tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    estado.escribir_estado(d, {
        "estado": "grabando", "parte": 1, "inicio": 100.0,
        "auto_pausa": False,
    })

    result = estado.transicionar_captura(d, "fin", ahora=150.0)

    assert result["changed"] is True
    assert result["state"]["estado"] == "fin"
    assert result["state"]["shutdown_state"] == "finalizando"


def test_spawn_registra_instance_id_y_fingerprint_verificable(
        monkeypatch, tmp_path):
    llamadas = {}
    snapshot = _snapshot(501, "src.captura_audio", create_time=321.0)

    class PopenFalso:
        pid = 501

        def __init__(self, args, **kwargs):
            llamadas["args"] = args
            llamadas["kwargs"] = kwargs

        def poll(self):
            return None

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(panel.subprocess, "Popen", PopenFalso)
    monkeypatch.setattr(
        panel, "_PROCESS_OPS",
        SimpleNamespace(snapshot=lambda pid: snapshot if pid == 501 else None))
    monkeypatch.setattr(panel, "RUNTIME_INSTANCE_ID", "instance-test")

    child = panel._spawn("src.captura_audio")

    assert llamadas["kwargs"]["env"]["CLASE_INSTANCE_ID"] == "instance-test"
    assert child["owned"].instance_id == "instance-test"
    assert child["owned"].root == snapshot


def test_panel_mantiene_pendiente_si_sobrevive_un_descendiente_propio(
        monkeypatch):
    root = _owned(701, "src.captura_audio", instance_id="instance-test")

    class PopenTerminado:
        pid = root.root.pid

        @staticmethod
        def poll():
            return 0

    registro = {
        "mod": root.module,
        "p": PopenTerminado(),
        "owned": root,
        "requested_stop": False,
    }
    monkeypatch.setattr(panel, "RUNTIME_INSTANCE_ID", "instance-test")
    monkeypatch.setattr(panel, "HIJOS", [registro])
    monkeypatch.setattr(
        panel,
        "stop_owned_children",
        lambda *_a, **_k: [StopResult(
            root.module,
            root.root.pid,
            "unresolved",
            "descendiente vivo",
            (702,),
        )],
    )

    results = panel._cerrar_hijos_coordinado()

    assert results == [{
        "module": root.module,
        "pid": root.root.pid,
        "status": "unresolved",
        "detail": "descendiente vivo",
        "remaining_pids": [702],
        "alive": True,
    }]


def test_shutdown_runner_finaliza_hijos_antes_de_declarar_cierre(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    estado.escribir_estado(d, {
        "estado": "grabando", "parte": 1, "inicio": 100.0,
        "auto_pausa": False,
    })
    eventos = []
    monkeypatch.setattr(panel, "_clase", lambda: d)
    monkeypatch.setattr(
        panel, "_cerrar_hijos_coordinado",
        lambda: eventos.append("hijos") or [
            {"module": "src.captura_audio", "status": "cooperative"}])
    monkeypatch.setattr(
        panel, "_shutdown_server",
        lambda: eventos.append("servidor"))
    monkeypatch.setattr(
        panel.audio_sesiones, "restaurar_enfoques",
        lambda: eventos.append("enfoque") or 0)
    monkeypatch.setattr(
        panel.config, "limpiar_instancia_obsoleta", lambda *_a, **_k: False)

    report = panel._shutdown_runner("fin_api", close_app=False)

    final = estado.leer_estado(d)
    assert report["status"] == "complete"
    assert eventos == ["enfoque", "hijos"]
    assert final["estado"] == "fin"
    assert final["shutdown_state"] == "finalizada"
    assert final["audio_capture_phase"] == "closed"


def test_api_cerrar_app_solicita_coordinador_sin_os_exit(monkeypatch):
    completed = threading.Event()
    solicitudes = []

    def solicitar(reason, close_app):
        solicitudes.append((reason, close_app))
        return SimpleNamespace(accepted=True, completed=completed)

    monkeypatch.setattr(panel, "_request_runtime_shutdown", solicitar)

    data = panel.app.test_client().post("/api/cerrar_app", json={}).get_json()

    assert data["ok"] is True
    assert data["accepted"] is True
    assert solicitudes == [("api_cerrar_app", True)]
    assert "os._exit" not in panel.api_cerrar_app.__code__.co_names


def test_signal_y_tray_delegan_en_la_misma_autoridad(monkeypatch):
    solicitudes = []
    monkeypatch.setattr(
        panel, "_request_runtime_shutdown",
        lambda reason, close_app: solicitudes.append((reason, close_app)))

    panel._handle_shutdown_signal(15, None)
    panel._on_tray_exit()

    assert solicitudes == [("signal_15", True), ("tray_exit", True)]


def test_limpieza_lock_obsoleto_es_segura_y_no_borra_owner_valido(
        monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    lock = tmp_path / "lock_transcriptor.pid"
    lock.write_text(json.dumps({
        "pid": 999999, "create_time": 1.0,
        "module": "src.transcriptor_vivo", "owner_id": "stale",
    }), encoding="utf-8")

    assert config.limpiar_instancia_obsoleta(
        "transcriptor", modulo="src.transcriptor_vivo") is True
    assert lock.exists() is False

    lock.write_text(json.dumps({
        "pid": 42, "create_time": 1.0,
        "module": "src.transcriptor_vivo", "owner_id": "live",
    }), encoding="utf-8")
    monkeypatch.setattr(
        config, "diagnosticar_instancia",
        lambda *_a, **_k: {"owner_valid": True, "status": "valid_owner"})

    assert config.limpiar_instancia_obsoleta(
        "transcriptor", modulo="src.transcriptor_vivo") is False
    assert lock.exists() is True


def test_shutdown_sin_cierre_del_productor_deja_pendiente_consolidar(
        monkeypatch, tmp_path):
    d = tmp_path / "clase-pendiente"
    d.mkdir()
    estado.escribir_estado(d, {
        "estado": "fin", "parte": 1, "inicio": 100.0,
        "auto_pausa": False, "shutdown_state": "finalizando",
    })
    audio_manifest.append_capture_session_event(
        d, "capture_session_started", capture_session_id="sesion-incompleta")
    monkeypatch.setattr(panel, "_clase", lambda: d)
    monkeypatch.setattr(panel, "_cerrar_hijos_coordinado", lambda: [])
    monkeypatch.setattr(panel.audio_sesiones, "restaurar_enfoques", lambda: 0)
    monkeypatch.setattr(
        panel.config, "limpiar_instancia_obsoleta", lambda *_a, **_k: False)

    report = panel._shutdown_runner("fin_api", close_app=False)

    final = estado.leer_estado(d)
    assert report["status"] == "complete_with_pending"
    assert report["capture_session_closed"] is False
    assert final["shutdown_state"] == "pendiente_consolidar"


def test_shutdown_con_fallo_terminal_cerrado_finaliza_con_advertencia(
        monkeypatch, tmp_path):
    d = tmp_path / "clase-degradada"
    d.mkdir()
    estado.escribir_estado(d, {
        "estado": "fin", "parte": 1, "inicio": 100.0,
        "auto_pausa": False, "shutdown_state": "finalizando",
    })
    audio_manifest.append_capture_session_event(
        d, "capture_session_started", capture_session_id="sesion-degradada")
    audio_manifest.append_event(d, {
        "chunk_id": "chunk_000000000100_p1_v10",
        "status": "capture_failed",
        "sequence": 1,
        "reason": "watchdog_pcm_flat",
    })
    audio_manifest.append_capture_session_event(
        d, "capture_session_closed", capture_session_id="sesion-degradada",
        terminal_capture_failures=1)
    monkeypatch.setattr(panel, "_clase", lambda: d)
    monkeypatch.setattr(panel, "_cerrar_hijos_coordinado", lambda: [])
    monkeypatch.setattr(panel.audio_sesiones, "restaurar_enfoques", lambda: 0)
    monkeypatch.setattr(
        panel.config, "limpiar_instancia_obsoleta", lambda *_a, **_k: False)

    report = panel._shutdown_runner("fin_api", close_app=False)

    final = estado.leer_estado(d)
    assert report["status"] == "complete"
    assert report["completion_ready"] is True
    assert report["completion_warnings"] == ["terminal_capture_failures=1"]
    assert final["shutdown_state"] == "finalizada"


def test_main_cierra_servidor_y_devuelve_cero_en_salida_normal(monkeypatch):
    eventos = []
    completed = threading.Event()
    completed.set()

    class ServerFalso:
        def serve_forever(self):
            eventos.append("serve")

        def server_close(self):
            eventos.append("server_close")

    monkeypatch.setattr(panel, "_SERVER", None)
    monkeypatch.setattr(panel, "_LAST_SHUTDOWN_TICKET", None)
    monkeypatch.setattr(panel, "MODO_APP", True)
    monkeypatch.setattr(panel, "_vigilante", lambda: None)
    monkeypatch.setattr(panel, "_install_shutdown_handlers", lambda: None)
    monkeypatch.setattr(panel.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(panel, "_create_server", lambda *_a: ServerFalso())
    monkeypatch.setattr(
        panel, "_request_runtime_shutdown",
        lambda reason, close_app: (
            eventos.append((reason, close_app))
            or SimpleNamespace(accepted=True, completed=completed)))

    result = panel.main()

    assert result == 0
    assert eventos == ["serve", ("server_finally", True), "server_close"]
    assert panel._SERVER is None


def test_main_excepcion_no_controlada_cierra_y_devuelve_no_cero(monkeypatch):
    eventos = []
    completed = threading.Event()
    completed.set()

    class ServerFalso:
        def serve_forever(self):
            raise RuntimeError("fallo sintético")

        def server_close(self):
            eventos.append("server_close")

    monkeypatch.setattr(panel, "_SERVER", None)
    monkeypatch.setattr(panel, "_LAST_SHUTDOWN_TICKET", None)
    monkeypatch.setattr(panel, "MODO_APP", True)
    monkeypatch.setattr(panel, "_vigilante", lambda: None)
    monkeypatch.setattr(panel, "_install_shutdown_handlers", lambda: None)
    monkeypatch.setattr(panel.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(panel, "_create_server", lambda *_a: ServerFalso())
    monkeypatch.setattr(
        panel, "_request_runtime_shutdown",
        lambda reason, close_app: (
            eventos.append((reason, close_app))
            or SimpleNamespace(accepted=True, completed=completed)))

    result = panel.main()

    assert result == 1
    assert eventos == [("server_exception", True),
                       ("server_finally", True), "server_close"]
    assert panel._SERVER is None
