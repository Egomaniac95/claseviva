"""Lanzador de la APP (doble clic vía ClaseViva.vbs / ClaseViva.bat, sin consola).

- Instancia única: si el panel ya corre en :8756, solo abre el navegador y termina.
- Arranca el panel en MODO APP (asistente de inicio en la interfaz, dueño de los
  procesos capturadores, botón Cerrar app) y abre el navegador cuando responde.
"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
URL = "http://localhost:8756"

# Si alguien abre lanzador.py con OTRO Python (doble clic al .py = intérprete del sistema,
# sin las librerías), re-lanzarse con el pythonw del venv y salir. Evita pipelines duplicados
# con dependencias rotas.
_VENV_PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
if _VENV_PYW.is_file() and ".venv" not in sys.executable.lower():
    import subprocess
    subprocess.Popen([str(_VENV_PYW), str(ROOT / "lanzador.py")], cwd=str(ROOT))
    sys.exit(0)


def puerto_ocupado() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8756), timeout=0.6):
            return True
    except OSError:
        return False


def abrir_cuando_liste() -> None:
    for _ in range(30):
        if puerto_ocupado():
            break
        time.sleep(0.5)
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def main() -> int:
    if puerto_ocupado():  # ya hay una app corriendo: solo mostrarla
        webbrowser.open(URL)
        return 0
    os.environ["CLASE_PANEL_MODO"] = "app"
    threading.Thread(target=abrir_cuando_liste, daemon=True).start()
    from src import panel
    return panel.main()


if __name__ == "__main__":
    sys.exit(main())
