"""Entrada Windows compartida por fuente y EXE; sin captura al abrir el panel."""
from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from . import app_paths

PORT = 8756
URL = f"http://localhost:{PORT}"


def panel_identity() -> dict | None:
    """No aceptar cualquier servicio TCP como ClaseViva ni seguir redirects/proxies."""
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=0.6)
    try:
        connection.request("GET", "/api/healthz")
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = json.loads(response.read(4096))
        return data if isinstance(data, dict) and data.get("app") == "ClaseViva" else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def port_busy() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            return True
    except OSError:
        return False


def same_panel(identity: dict | None) -> bool:
    return bool(identity and identity.get("app") == "ClaseViva" and identity.get("version") == 1
                and identity.get("data_root_id") == app_paths.data_root_id())


def show_error(message: str) -> None:
    if os.environ.get("CLASEVIVA_NO_DIALOG") == "1":
        return
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "ClaseViva", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


class LaunchGuard:
    """Objeto de kernel: una sola apertura por sesión/puerto; el SO libera tras crash."""
    def __init__(self) -> None:
        self.handle = None
        self.acquired = True

    def __enter__(self):
        if sys.platform == "win32":
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.restype = wintypes.BOOL
            self.handle = self.kernel.CreateMutexW(None, False, f"Local\\ClaseViva-panel-{PORT}")
            if not self.handle:
                raise OSError("No se pudo reservar el arranque de ClaseViva")
            self.acquired = ctypes.get_last_error() != 183
        return self

    def __exit__(self, *_):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def open_browser() -> None:
    if os.environ.get("CLASEVIVA_NO_BROWSER") != "1":
        webbrowser.open(URL)


def wait_for_panel(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if same_panel(panel_identity()):
            return True
        time.sleep(0.25)
    return False


def open_when_ready() -> None:
    if wait_for_panel():
        open_browser()


def launch_panel() -> int:
    with LaunchGuard() as guard:
        if not guard.acquired:
            if wait_for_panel():
                open_browser()
                return 0
            show_error("Otra apertura de ClaseViva sigue iniciándose o usa otra carpeta. "
                       "Cierra esa instancia desde su panel antes de abrir esta copia.")
            return 2
        identity = panel_identity()
        if same_panel(identity):
            open_browser()
            return 0
        if identity or port_busy():
            show_error("El puerto 8756 está ocupado por otra copia o servicio. "
                       "Cierra la otra app desde su panel; no se iniciará otra captura.")
            return 2
        os.environ["CLASE_PANEL_MODO"] = "app"
        threading.Thread(target=open_when_ready, daemon=True, name="open-browser").start()
        from . import panel
        return panel.main()


def run_worker(module: str) -> int:
    if module not in app_paths.WORKER_MODULES:
        raise ValueError("Módulo worker no admitido")
    # El comando original conserva el módulo para fingerprints psutil/manifest.
    # Los parsers de cada worker no deben interpretar los switches del launcher.
    sys.argv = [module]
    worker = importlib.import_module(module)
    return int(worker.main() or 0)


def _redirect_windowless_output(root: Path, worker: str | None) -> None:
    if sys.stdout is None or sys.stderr is None:
        name = worker.rsplit(".", 1)[-1] if worker else "lanzador"
        log = (root / "logs" / f"{name}.log").open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


def self_test() -> dict:
    """Diagnóstico explícito offline: ni micrófono, ni escritorio, ni APIs/clases."""
    checks = {}
    # Respetar el mismo orden DLL que usa el backend nativo/fallback de OCR.
    imports = ["onnxruntime", *sorted(app_paths.WORKER_MODULES), "src.panel", "src.post_procesar",
               "av", "pyaudiowpatch", "pycaw.pycaw", "winrt.windows.media.ocr",
               "httpx", "markdown", "markdown.extensions.tables",
               "markdown.extensions.fenced_code", "markdown.extensions.sane_lists",
               "latex2mathml.converter", "ddgs", "cv2",
               "shapely", "pyclipper", "rapidocr_onnxruntime"]
    for name in imports:
        try:
            importlib.import_module(name)
            checks[name] = {"ok": True}
        except Exception as error:
            checks[name] = _diagnostic_error(error)
    checks["lesson_render"] = lesson_render_check()
    try:
        from PIL import Image, features
        import pypdfium2
        checks["webp_codec"] = {"ok": features.check("webp")}
        doc = pypdfium2.PdfDocument.new()
        page = doc.new_page(64, 64)
        bitmap = page.render()
        checks["pdfium_render"] = {"ok": bitmap.width == 64}
        bitmap.close()
        page.close()
        doc.close()
        with Image.open(app_paths.resource_root() / "assets" / "claseviva.ico") as icon:
            checks["icon"] = {"ok": icon.format == "ICO"}
        from .ocr import _RapidOcrBackend
        ocr = _RapidOcrBackend()
        ocr.recognize(Image.new("RGB", (128, 64), "white"))
        checks["ocr_models"] = {"ok": True}
        from . import audio_vad
        import numpy as np
        scores = audio_vad.probabilities(np.zeros(3 * 16000, dtype=np.float32))
        checks["silero_vad"] = {
            "ok": bool(scores.shape == (94,) and np.isfinite(scores).all()
                       and float(scores.max()) < .2),
            "model_sha256": audio_vad.MODEL_SHA256,
            "providers": audio_vad._session().get_providers(),
        }
    except Exception as error:
        checks["native_assets"] = _diagnostic_error(error)
    return {"ok": all(item["ok"] for item in checks.values()),
            "frozen": app_paths.is_frozen(), "checks": checks}


def lesson_render_check() -> dict:
    """Probar el contrato visible, no aceptar el fallback de texto plano como PASS."""
    try:
        from .panel import a_html
        sample = (
            '<!-- CLASEVIVA_STUDY_PACKAGE_V1 {"fixture":true} -->\n\n'
            '# Estudio\n\n| Concepto | Valor |\n| --- | --- |\n| A | B |\n\n'
            '```python\nx = 2\n```\n\n' + r'\[x^2 = 4\]' + '\n\n'
            '![Diagrama](img/synthetic_123.png)\n\n**FIN_DE_LECCION**\n'
        )
        rendered = a_html(sample)
        results = {
            "heading": "<h1>Estudio</h1>" in rendered,
            "table": "<table>" in rendered,
            "code": "<pre><code" in rendered,
            "mathml": "<math " in rendered,
            "image": 'src="/img/synthetic_123.png"' in rendered,
            "metadata_hidden": "CLASEVIVA_STUDY_PACKAGE_V1" not in rendered,
            "complete": "<strong>FIN_DE_LECCION</strong>" in rendered,
        }
        return {"ok": all(results.values()), **results}
    except Exception as error:
        return _diagnostic_error(error)


def _diagnostic_error(error: Exception) -> dict:
    detail = str(error)
    for name, value in os.environ.items():
        if name.endswith("API_KEY") and value:
            detail = detail.replace(value, "[REDACTED]")
    return {"ok": False, "error_type": type(error).__name__,
            "dependency": getattr(error, "name", None), "detail": detail[:500]}


def cli(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="ClaseViva para Windows")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--worker", choices=sorted(app_paths.WORKER_MODULES))
    modes.add_argument("--self-test", action="store_true")
    parser.add_argument("--report", type=Path, help="Resultado JSON del diagnóstico offline")
    args = parser.parse_args(argv)
    if args.report and not args.self_test:
        parser.error("--report requiere --self-test")
    if not app_paths.is_frozen() and not args.worker and not args.self_test:
        pythonw = app_paths.resource_root() / ".venv" / "Scripts" / "pythonw.exe"
        if pythonw.is_file() and Path(sys.executable).parent != pythonw.parent:
            subprocess.Popen([str(pythonw), str(app_paths.resource_root() / "lanzador.py")],
                             cwd=str(app_paths.resource_root()),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return 0
    try:
        root = app_paths.prepare_portable_config()
        _redirect_windowless_output(root, args.worker)
        if args.self_test:
            result = self_test()
            if args.report:
                args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 1
        if args.worker:
            return run_worker(args.worker)
        return launch_panel()
    except Exception as error:
        # No imprimir excepciones HTTP/config que pudieran contener credenciales.
        if sys.stderr is not None:
            print(f"[arranque] {type(error).__name__}", file=sys.stderr)
        if not args.worker:
            show_error("ClaseViva no pudo iniciar. Descomprime la carpeta completa en una "
                       "ubicación escribible (Documentos, por ejemplo), configura .env y "
                       "revisa logs/lanzador.log. No necesita permisos de administrador.")
        return 1
