"""Puente local temporal para actualizar Consolidar sin reiniciar una grabación.

No inicia, pausa ni cierra capturadores. Se retira cuando cierra el panel o cuando
el panel ya anuncia la versión nueva de consolidación.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import urllib.request

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from . import app_paths, audio_manifest, config, consolidation_recovery as recovery, estado as est

PORT = 8757
app = Flask(__name__)
_lock = threading.Lock()
_running = False
_start_error = ""
_last_class = None
_logs: list[str] = []
_origins = {"http://localhost:8756", "http://127.0.0.1:8756"}


@app.before_request
def check_origin():
    origin = request.headers.get("Origin", "")
    if origin and origin not in _origins:
        return jsonify({"ok": False, "msg": "Origen no admitido"}), 403
    if request.method == "OPTIONS":
        return "", 204
    return None


@app.after_request
def cors(response):
    origin = request.headers.get("Origin", "")
    if origin in _origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store"
    return response


def _log(text):
    with _lock:
        _logs.append(str(text))
        del _logs[:-400]


class LogWriter:
    def __init__(self):
        self.buffer = ""

    def write(self, text):
        self.buffer += str(text)
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                _log(line)
        return len(text)

    def flush(self):
        if self.buffer.strip():
            _log(self.buffer)
            self.buffer = ""


@app.get("/health")
def health():
    return jsonify({"app": "ClaseVivaRecovery", "version": 1,
                    "data_root_id": app_paths.data_root_id(config.ROOT)})


@app.get("/status")
def status():
    d = est.clase_activa() or _last_class
    ready, reason = False, ""
    if d is not None:
        try:
            recovery.capture_stopped(d)
            ready = True
        except recovery.RecoveryError as error:
            reason = str(error)
    saved = recovery.read_status(d) if d is not None else {}
    with _lock:
        running, lines, start_error = _running, list(_logs), _start_error
    return jsonify({
        "data_root_id": app_paths.data_root_id(config.ROOT),
        "clase_id": d.name if d is not None else "",
        "curso": est.curso_de(d) if d is not None else "",
        "ready": ready, "reason": reason, "running": running,
        "status": "running" if running else "error" if start_error else saved.get("status", "idle"),
        "error": start_error or str(saved.get("error") or ""), "log": "\n".join(lines),
    })


@app.post("/api/consolidar")
def consolidate():
    global _running, _last_class, _start_error
    d = est.clase_activa()
    if d is None:
        return jsonify({"ok": False, "msg": "No hay clase seleccionada."}), 409
    try:
        recovery.capture_stopped(d)
    except recovery.RecoveryError as error:
        return jsonify({"ok": False, "msg": str(error)}), 409
    report = audio_manifest.transcript_status_report(d, verify_files=True)
    payload = request.get_json(silent=True) or {}
    if not report["available_audio_count"] and not report["segment_count"] and not payload.get("confirmar_sin_evidencia"):
        return jsonify({"ok": False, "requires_confirmation": True,
                        "msg": "No hay audio ni transcript disponibles. ¿Consolidar solo la evidencia visual?"})
    with _lock:
        if _running:
            return jsonify({"ok": False, "msg": "Ya hay una consolidación en curso."}), 409
        _running = True
        _start_error = ""
        _last_class = d
        _logs.clear()

    def run():
        global _running, _start_error
        writer = LogWriter()
        try:
            with contextlib.redirect_stdout(writer):
                recovery.consolidate(d, light=True, log=_log)
            _log("OK: lección y resumen consolidados. Los audios pendientes se procesaron antes de publicar.")
        except Exception as error:
            # Las excepciones de recovery ya contienen mensajes sin claves/prompts.
            message = str(error) if isinstance(error, recovery.RecoveryError) else type(error).__name__
            with _lock:
                _start_error = message
            _log("ERROR: " + message + " Vuelve a pulsar Consolidar para reintentar.")
            try:
                recovery.write_status(d, running=False, status="error", error=message, stage="error")
            except OSError:
                pass  # /status conserva el error aunque no pueda escribirlo en disco.
        finally:
            writer.flush()
            with _lock:
                _running = False

    try:
        recovery.write_status(d, running=True, status="running", error="", stage="starting")
        threading.Thread(target=run, name="recovery-consolidation", daemon=True).start()
    except Exception as error:
        message = (f"No se pudo iniciar la consolidación ({type(error).__name__}). "
                   "Los audios están conservados. Vuelve a pulsar Consolidar.")
        with _lock:
            _running = False
            _start_error = message
        _log("ERROR: " + message)
        return jsonify({"ok": False, "msg": message}), 500
    return jsonify({"ok": True, "msg": "Recuperando audios y consolidando. Sigue el avance en Post-proceso."})


def main():
    logs = config.ROOT / "logs"
    logs.mkdir(exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        handle = (logs / "recovery_service.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or handle
        sys.stderr = sys.stderr or handle
    server = make_server("127.0.0.1", PORT, app, threaded=True)

    def observe_parent():
        failures = 0
        while not stop.wait(15):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8756/api/healthz", timeout=2) as response:
                    parent = json.load(response)
                failures = 0
                if parent.get("consolidation_recovery") and not _running:
                    break
            except Exception:
                failures += 1
                if failures >= 6 and not _running:
                    break
        server.shutdown()

    stop = threading.Event()
    threading.Thread(target=observe_parent, daemon=True, name="recovery-parent").start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
