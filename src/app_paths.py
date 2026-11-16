"""Rutas portables y comando de workers, sin imports pesados ni efectos de disco.

En fuente se mantiene el layout histórico. En distribución Windows los recursos
son de solo lectura en _internal y los datos viven junto a ClaseViva.exe.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys


WORKER_MODULES = frozenset({
    "src.captura_audio", "src.captura_pantalla", "src.transcriptor_vivo", "src.escriba",
})


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    override = os.environ.get("CLASEVIVA_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("CLASEVIVA_DATA_DIR debe ser una ruta absoluta")
        return path.resolve()
    return Path(sys.executable).resolve().parent if is_frozen() else resource_root()


def data_root_id(root: Path | None = None) -> str:
    raw = str((root or data_root()).resolve()).casefold().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def worker_command(module: str) -> list[str]:
    if module not in WORKER_MODULES:
        raise ValueError("Módulo worker no admitido")
    if is_frozen():
        return [sys.executable, "--worker", module]
    venv = resource_root() / ".venv" / "Scripts" / "python.exe"
    python = str(venv) if venv.is_file() else sys.executable.replace("pythonw.exe", "python.exe")
    return [python, "-m", module]


def prepare_portable_config() -> Path:
    """Solo el arranque explícito crea configuración; nunca reemplaza claves.

    No mover automáticamente a AppData: una carpeta no escribible debe producir
    un error accionable, no separar silenciosamente las clases del portable.
    """
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    env = root / ".env"
    if is_frozen() and not env.exists():
        template = resource_root() / ".env.example"
        content = template.read_text(encoding="utf-8")
        try:
            with env.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError:
            pass  # Otra apertura ganó la carrera; no tocar sus claves.
    (root / "logs").mkdir(exist_ok=True)
    return root
