import json
import os

import pytest

from src import config, panel, transcriptor_vivo


def _lock_payload(**overrides):
    payload = {
        "schema_version": 3,
        "pid": 42,
        "create_time": 100.0,
        "nombre": "transcriptor",
        "module": "src.transcriptor_vivo",
        "class_dir": "C:/clase",
        "started_at": 90.0,
        "heartbeat": 99.0,
        "owner_id": "owner-42",
    }
    payload.update(overrides)
    return payload


def test_lock_valido_exige_misma_instancia_comando_y_owner():
    diagnostico = config.validar_lock_instancia(
        _lock_payload(), nombre="transcriptor",
        modulo="src.transcriptor_vivo", class_dir="C:/clase", ahora=100.0,
        process_info={
            "exists": True, "create_time": 100.0,
            "cmdline": ["python.exe", "-m", "src.transcriptor_vivo"],
        },
    )

    assert diagnostico["owner_valid"] is True
    assert diagnostico["status"] == "valid_owner"


@pytest.mark.parametrize(
    ("process_info", "status"),
    [
        ({"exists": False}, "stale_pid"),
        ({"exists": True, "create_time": 200.0,
          "cmdline": ["python", "-m", "src.transcriptor_vivo"]},
         "stale_pid_reused"),
        ({"exists": True, "create_time": 100.0,
          "cmdline": ["python", "-m", "src.otro_modulo"]},
         "stale_wrong_command"),
    ],
)
def test_lock_invalido_distingue_pid_muerto_reutilizado_y_comando(
        process_info, status):
    diagnostico = config.validar_lock_instancia(
        _lock_payload(), nombre="transcriptor",
        modulo="src.transcriptor_vivo", class_dir="C:/clase", ahora=100.0,
        process_info=process_info,
    )

    assert diagnostico["owner_valid"] is False
    assert diagnostico["status"] == status


def test_lock_legado_vivo_se_valida_conservadoramente_por_comando():
    diagnostico = config.validar_lock_instancia(
        {"pid": 42}, nombre="transcriptor",
        modulo="src.transcriptor_vivo", class_dir="C:/clase", ahora=100.0,
        process_info={
            "exists": True, "create_time": 100.0,
            "cmdline": ["python", "-m", "src.transcriptor_vivo"],
        },
    )

    assert diagnostico["owner_valid"] is True
    assert diagnostico["status"] == "legacy_owner_valid"


def test_lock_owner_vivo_con_heartbeat_perdido_no_se_declara_obsoleto():
    diagnostico = config.validar_lock_instancia(
        _lock_payload(heartbeat=10.0), nombre="transcriptor",
        modulo="src.transcriptor_vivo", class_dir="C:/clase", ahora=100.0,
        process_info={
            "exists": True, "create_time": 100.0,
            "cmdline": ["python", "-m", "src.transcriptor_vivo"],
        },
    )

    assert diagnostico["owner_valid"] is True
    assert diagnostico["status"] == "heartbeat_lost"


def test_owner_vivo_de_otra_clase_bloquea_sin_eliminarse():
    diagnostico = config.validar_lock_instancia(
        _lock_payload(class_dir="C:/otra-clase"), nombre="transcriptor",
        modulo="src.transcriptor_vivo", class_dir="C:/clase", ahora=100.0,
        process_info={
            "exists": True, "create_time": 100.0,
            "cmdline": ["python", "-m", "src.transcriptor_vivo"],
        },
    )

    assert diagnostico["owner_valid"] is True
    assert diagnostico["status"] == "valid_owner_other_class"


def test_owner_vivo_no_verificable_bloquea_conservadoramente():
    diagnostico = config.validar_lock_instancia(
        _lock_payload(), nombre="transcriptor",
        modulo="src.transcriptor_vivo", class_dir="C:/clase", ahora=100.0,
        process_info={"exists": True, "create_time": 0.0, "cmdline": []},
    )

    assert diagnostico["owner_valid"] is True
    assert diagnostico["status"] == "owner_unverifiable"


def test_lock_se_adquiere_renueva_y_libera_solo_por_owner(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    monkeypatch.setattr(config, "LOGS_DIR", logs)
    monkeypatch.setattr(config, "_INSTANCE_LOCKS", {})
    monkeypatch.setattr(config, "_process_info", lambda _pid: {
        "exists": True, "create_time": 123.0,
        "cmdline": ["python", "-m", "src.transcriptor_vivo"],
    })
    reloj = {"t": 1000.0}
    monkeypatch.setattr(config.time, "time", lambda: reloj["t"])

    assert config.instancia_unica(
        "transcriptor", modulo="src.transcriptor_vivo",
        class_dir=tmp_path / "clase") is True
    lock = logs / "lock_transcriptor.pid"
    inicial = json.loads(lock.read_text(encoding="utf-8"))
    assert inicial["schema_version"] == 3
    assert inicial["pid"] == os.getpid()
    assert inicial["module"] == "src.transcriptor_vivo"
    assert inicial["class_dir"] == str((tmp_path / "clase").resolve())
    assert inicial["owner_id"]

    reloj["t"] = 1010.0
    assert config.renovar_instancia("transcriptor", class_dir=tmp_path / "clase")
    renovado = json.loads(lock.read_text(encoding="utf-8"))
    assert renovado["heartbeat"] == 1010.0
    assert renovado["owner_id"] == inicial["owner_id"]

    assert config.liberar_instancia("transcriptor") is True
    assert not lock.exists()
    assert config.liberar_instancia("transcriptor") is False


def test_owner_id_cambiado_impide_renovar_y_liberar_lock_ajeno(
        monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    monkeypatch.setattr(config, "LOGS_DIR", logs)
    monkeypatch.setattr(config, "_INSTANCE_LOCKS", {})
    monkeypatch.setattr(config, "_process_info", lambda _pid: {
        "exists": True, "create_time": 123.0,
        "cmdline": ["python", "-m", "src.transcriptor_vivo"],
    })
    assert config.instancia_unica(
        "transcriptor", modulo="src.transcriptor_vivo") is True
    lock = logs / "lock_transcriptor.pid"
    ajeno = json.loads(lock.read_text(encoding="utf-8"))
    ajeno["owner_id"] = "owner-ajeno"
    lock.write_text(json.dumps(ajeno), encoding="utf-8")

    assert config.renovar_instancia("transcriptor") is False
    assert config.liberar_instancia("transcriptor") is False
    assert lock.is_file()
    assert json.loads(lock.read_text(encoding="utf-8"))["owner_id"] == "owner-ajeno"


@pytest.mark.parametrize(
    ("returncode", "lifecycle", "pending", "expected"),
    [
        (0, {"status": "stopped_no_pending"}, 0, "stopped_no_pending"),
        (transcriptor_vivo.EXIT_LOCK_CONFLICT,
         {"status": "lock_conflict"}, 0, "lock_conflict"),
        (1, {"status": "python_exception", "device": "cuda"}, 1,
         "python_exception"),
        (-1073740791, {"status": "running", "device": "cuda",
                       "gpu_failure_attributed": True}, 1,
         "native_crash"),
        (0, {}, 1, "unknown_exit"),
    ],
)
def test_clasificador_vigilante_distingue_causas_de_salida(
        returncode, lifecycle, pending, expected):
    resultado = panel.clasificar_salida_hijo(
        "src.transcriptor_vivo", returncode=returncode,
        lifecycle=lifecycle, pending_count=pending)

    assert resultado["status"] == expected
    assert "se cayó" not in resultado["message"]
    if expected in {"stopped_no_pending", "lock_conflict"}:
        assert resultado["restart"] is False
        assert resultado["force_cpu"] is False
    if expected == "python_exception":
        assert resultado["restart"] is True
        assert resultado["force_cpu"] is False
        if expected == "native_crash":
            assert resultado["restart"] is True
            assert resultado["force_cpu"] is False


def test_heartbeat_perdido_se_clasifica_sin_confundirlo_con_exit():
    resultado = panel.clasificar_salida_hijo(
        "src.transcriptor_vivo", returncode=None,
        lifecycle={"status": "running", "heartbeat": 10.0, "device": "cuda"},
        pending_count=1, ahora=100.0, heartbeat_timeout=30.0)

    assert resultado["status"] == "heartbeat_lost"
    assert resultado["restart"] is False
    assert resultado["force_cpu"] is False


def test_heartbeat_antiguo_sin_pendientes_es_idle_y_no_error():
    resultado = panel.clasificar_salida_hijo(
        "src.transcriptor_vivo", returncode=None,
        lifecycle={"status": "waiting_audio", "heartbeat": 10.0,
                   "device": "cuda"},
        pending_count=0, ahora=100.0, heartbeat_timeout=30.0)

    assert resultado["status"] == "idle_no_pending"
    assert resultado["restart"] is False
    assert resultado["force_cpu"] is False
    assert "error" not in resultado["message"].lower()


@pytest.mark.parametrize("status", ["starting_model", "processing"])
def test_heartbeat_antiguo_no_oculta_fase_activa_aunque_el_backlog_sea_cero(
        status):
    resultado = panel.clasificar_salida_hijo(
        "src.transcriptor_vivo", returncode=None,
        lifecycle={"status": status, "heartbeat": 10.0, "device": "cuda"},
        pending_count=0, ahora=100.0, heartbeat_timeout=30.0)

    assert resultado["status"] == "heartbeat_lost"
    assert resultado["restart"] is False
    assert resultado["force_cpu"] is False


def test_entorno_stt_remoto_nunca_activa_cpu_local(monkeypatch):
    monkeypatch.delenv("CLASE_STT_CPU", raising=False)

    normal = panel._entorno_hijo("src.transcriptor_vivo", force_cpu=False)
    fallback = panel._entorno_hijo("src.transcriptor_vivo", force_cpu=True)

    assert "CLASE_STT_CPU" not in normal
    assert "CLASE_STT_CPU" not in fallback
    assert normal["CLASE_RUNTIME_DIR"] == str(panel.RUNTIME_DIR)


def test_fin_con_cola_en_cooldown_sale_deterministicamente():
    assert transcriptor_vivo.debe_estacionar_pendientes_remotos(
        estado_sesion="fin", captura_cerrada=True,
        sin_procesar=2, listos=0) is True
    assert transcriptor_vivo.debe_estacionar_pendientes_remotos(
        estado_sesion="grabando", captura_cerrada=False,
        sin_procesar=2, listos=0) is False
    result = panel.clasificar_salida_hijo(
        "src.transcriptor_vivo", returncode=0,
        lifecycle={"status": "pending_remote_stt"}, pending_count=2)
    assert result["status"] == "pending_remote_stt"
    assert result["restart"] is False


def test_fin_stt_espera_cierre_del_productor_de_audio():
    assert transcriptor_vivo.debe_cerrar_transcriptor(
        estado_sesion="fin", sin_procesar=0,
        captura_cerrada=False, latido_perdido=False) is False
    assert transcriptor_vivo.debe_cerrar_transcriptor(
        estado_sesion="fin", sin_procesar=0,
        captura_cerrada=True, latido_perdido=False) is True
    assert transcriptor_vivo.debe_cerrar_transcriptor(
        estado_sesion="fin", sin_procesar=1,
        captura_cerrada=True, latido_perdido=False) is False


def test_transcriptor_huerfano_puede_salir_sin_cierre_falso_del_productor():
    assert transcriptor_vivo.debe_cerrar_transcriptor(
        estado_sesion="grabando", sin_procesar=0,
        captura_cerrada=False, latido_perdido=True) is True


def test_metricas_commit_stt_separan_latencia_rtf_y_backlog():
    metricas = transcriptor_vivo.metricas_commit_stt(
        {"wall_start": 100.0, "wall_end": 130.0},
        committed_at=160.0,
        transcribe_seconds=6.0,
        audio_seconds=30.0,
        backlog=2,
    )

    assert metricas["commit_latency_seconds"] == 30.0
    assert metricas["real_time_factor"] == 0.2
    assert metricas["backlog_wav"] == 2
    assert metricas["source_wall_end"] == 130.0
    assert metricas["provisional"] is False


def test_main_transcriptor_libera_lock_ante_excepcion_python(
        monkeypatch, tmp_path):
    llamadas = []
    class_dir = tmp_path / "class"
    class_dir.mkdir()
    (class_dir / "session_state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        transcriptor_vivo, "_main", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        transcriptor_vivo.config, "liberar_instancia",
        lambda nombre: llamadas.append(nombre) or True)
    monkeypatch.setattr(
        transcriptor_vivo.est, "clase_activa", lambda: class_dir)

    with pytest.raises(RuntimeError, match="boom"):
        transcriptor_vivo.main()

    assert llamadas == ["transcriptor"]
    lifecycle = transcriptor_vivo.est.leer_estado(class_dir)["stt_lifecycle"]
    assert lifecycle["status"] == "python_exception"
    assert lifecycle["error_type"] == "RuntimeError"
    assert lifecycle["heartbeat"] > 0


def test_main_transcriptor_libera_lock_en_salida_normal(monkeypatch):
    llamadas = []
    monkeypatch.setattr(transcriptor_vivo, "_main", lambda: 0)
    monkeypatch.setattr(
        transcriptor_vivo.config, "liberar_instancia",
        lambda nombre: llamadas.append(nombre) or True)

    assert transcriptor_vivo.main() == 0
    assert llamadas == ["transcriptor"]


def test_perdedor_de_lock_no_sobrescribe_lifecycle_del_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriptor_vivo.config, "stdout_utf8", lambda: None)
    monkeypatch.setattr(transcriptor_vivo.sys, "argv", ["transcriptor"])
    monkeypatch.setattr(transcriptor_vivo.est, "clase_activa", lambda: tmp_path)
    monkeypatch.setattr(
        transcriptor_vivo.config, "instancia_unica", lambda *_a, **_k: False)
    monkeypatch.setattr(
        transcriptor_vivo.est, "actualizar_estado",
        lambda *_a, **_k: pytest.fail("el perdedor no publica lifecycle"))

    assert transcriptor_vivo._main() == transcriptor_vivo.EXIT_LOCK_CONFLICT


class _ProcesoTerminado:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def test_paso_vigilante_no_relanzza_salida_limpia_ni_lock_valido(
        monkeypatch, tmp_path):
    estados = iter((
        {"stt_lifecycle": {"status": "stopped_no_pending"}},
        {"stt_lifecycle": {"status": "lock_conflict"}},
    ))
    monkeypatch.setattr(panel.est, "leer_estado", lambda _d: next(estados))
    monkeypatch.setattr(
        panel.audio_manifest, "transcript_status_report",
        lambda *_a, **_k: {"unresolved_count": 0})
    monkeypatch.setattr(
        panel.config, "diagnosticar_instancia",
        lambda *_a, **_k: {"owner_valid": True, "status": "valid_owner"})
    monkeypatch.setattr(
        panel, "_spawn", lambda *_a, **_k: pytest.fail("no debe relanzar"))

    limpio, diag_limpio = panel._evaluar_hijo_una_vez({
        "mod": "src.transcriptor_vivo", "p": _ProcesoTerminado(0),
        "intentos": 0, "requested_stop": False,
    }, tmp_path)
    conflicto, diag_conflicto = panel._evaluar_hijo_una_vez({
        "mod": "src.transcriptor_vivo",
        "p": _ProcesoTerminado(transcriptor_vivo.EXIT_LOCK_CONFLICT),
        "intentos": 0, "requested_stop": False,
    }, tmp_path)

    assert limpio["p"].returncode == 0
    assert diag_limpio["status"] == "stopped_no_pending"
    assert diag_limpio["restarted"] is False
    assert conflicto["p"].returncode == transcriptor_vivo.EXIT_LOCK_CONFLICT
    assert diag_conflicto["status"] == "lock_conflict"
    assert diag_conflicto["restarted"] is False


def test_paso_vigilante_relanzza_una_vez_si_lock_ya_es_obsoleto(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        panel.est, "leer_estado",
        lambda _d: {"stt_lifecycle": {"status": "lock_conflict"}})
    monkeypatch.setattr(
        panel.audio_manifest, "transcript_status_report",
        lambda *_a, **_k: {"unresolved_count": 1})
    monkeypatch.setattr(
        panel.config, "diagnosticar_instancia",
        lambda *_a, **_k: {"owner_valid": False, "status": "stale_pid"})
    relanzados = []

    def spawn(mod, intentos, force_cpu=False):
        relanzados.append((mod, intentos, force_cpu))
        return {
            "mod": mod, "p": _ProcesoTerminado(None), "intentos": intentos,
            "force_cpu": force_cpu, "requested_stop": False,
        }

    monkeypatch.setattr(panel, "_spawn", spawn)
    nuevo, diagnostico = panel._evaluar_hijo_una_vez({
        "mod": "src.transcriptor_vivo",
        "p": _ProcesoTerminado(transcriptor_vivo.EXIT_LOCK_CONFLICT),
        "intentos": 0, "requested_stop": False,
    }, tmp_path)

    assert diagnostico["status"] == "lock_stale"
    assert diagnostico["restarted"] is True
    assert relanzados == [("src.transcriptor_vivo", 1, False)]
    assert nuevo["intentos"] == 1
