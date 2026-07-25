import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from src import config


def _competir_por_instancia(logs_dir: str, nombre: str) -> bool:
    from src import config as config_worker

    config_worker.LOGS_DIR = Path(logs_dir)
    aceptada = config_worker.instancia_unica(nombre)
    if aceptada:
        time.sleep(0.8)
    return aceptada


def test_instancia_unica_admite_un_solo_proceso_concurrente(tmp_path):
    logs = tmp_path / "logs"
    nombre = "race_synthetic"
    with ProcessPoolExecutor(max_workers=6) as pool:
        resultados = list(pool.map(
            _competir_por_instancia,
            [str(logs)] * 6,
            [nombre] * 6,
        ))

    assert resultados.count(True) == 1
    # El owner libera en atexit; el lock no debe sobrevivir al proceso ganador.
    assert not (logs / f"lock_{nombre}.pid").exists()


def test_instancia_unica_reemplaza_pid_legacy_muerto(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    nombre = "stale_synthetic"
    (logs / f"lock_{nombre}.pid").write_text("99999999", encoding="utf-8")
    monkeypatch.setattr(config, "LOGS_DIR", logs)

    assert config.instancia_unica(nombre) is True
    datos = json.loads(
        (logs / f"lock_{nombre}.pid").read_text(encoding="utf-8"))
    assert datos["pid"] == os.getpid()
