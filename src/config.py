"""Configuración central: rutas, constantes y carga segura de ``.env``.

Todo módulo del pipeline importa desde aquí. No tiene dependencias externas.
"""
import atexit
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# contenido/<curso>/ = material pesado POR CURSO (multi-curso: una sola app para toda la
# maestría; ya no hace falta copiarla por curso). memoria_cursos.md = historial ligero global.
CONTENIDO_DIR = ROOT / "contenido"
CURSO_DEFAULT = "maestria-inteligencia-artificial"
# Compatibilidad: rutas del curso por defecto (los módulos derivan las reales desde la clase)
CLASES_DIR = CONTENIDO_DIR / CURSO_DEFAULT / "clases"
RESUMENES_DIR = CONTENIDO_DIR / CURSO_DEFAULT / "resumenes"
TAREAS_MD = CONTENIDO_DIR / CURSO_DEFAULT / "TAREAS.md"
MEMORIA_CURSOS = ROOT / "memoria_cursos.md"

CURSO_DIRS_EXCLUIDOS = {
    "clases", "resumenes", "_debug", "_backup", "backup", "backups",
    "log", "logs", "tmp", "temp", "temporales", "__pycache__",
}


def es_dir_curso_valido(p: Path) -> bool:
    """True si p representa un curso real bajo contenido/.

    contenido/clases y contenido/resumenes son ubicaciones legacy del esquema viejo:
    se auditan/migran con herramienta explicita, pero no deben aparecer como cursos.
    """
    if not p.is_dir():
        return False
    nombre = p.name.strip()
    bajo = nombre.lower()
    if not nombre or bajo in CURSO_DIRS_EXCLUIDOS:
        return False
    if bajo.startswith((".", "_", "backup", "tmp", "temp")):
        return False
    return True


def cursos_existentes() -> list[str]:
    if not CONTENIDO_DIR.is_dir():
        return []
    return sorted(p.name for p in CONTENIDO_DIR.iterdir() if es_dir_curso_valido(p))


def dir_curso(curso: str):
    return CONTENIDO_DIR / curso
LOGS_DIR = ROOT / "logs"
PUNTERO_CLASE = ROOT / "clase_actual.txt"


def _cargar_env() -> None:
    """Parser mínimo de .env (sin dependencia python-dotenv)."""
    env = ROOT / ".env"
    if not env.is_file():
        return
    for linea in env.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        k, _, v = linea.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_cargar_env()

# --- API DeepSeek ---
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# OJO: 'deepseek-chat' queda DEPRECADO el 2026-07-24. El nombre vigente es 'deepseek-v4-flash'.
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
COSTO_ENTRADA_USD_M = 0.14   # USD por millón de tokens; revisar al cambiar proveedor.
COSTO_SALIDA_USD_M = 0.28

# --- Transcripción ---
# es | en | auto — 'auto' detecta el idioma POR CHUNK (clases mixtas español/inglés):
# los proveedores multilingües transcriben cada tramo en su idioma real.
IDIOMA = os.environ.get("CLASE_IDIOMA", "auto").lower()
AUDIO_CHUNK_SEG = int(os.environ.get("CLASE_AUDIO_CHUNK", "30"))


def student_notes_enabled() -> bool:
    """Módulo visible por defecto; permite rollback sin alterar notas guardadas."""
    return os.environ.get("CLASE_STUDENT_NOTES", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def loopback_qpc_recovery_enabled() -> bool:
    """Rollback explícito de la reconstrucción de silencios del loopback virtual."""
    return os.environ.get("CLASE_LOOPBACK_QPC_RECOVERY", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def stt_runtime_dir() -> Path | None:
    """Directorio efímero compartido por los hijos (no contiene transcript)."""
    raw = os.environ.get("CLASE_RUNTIME_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else None

# --- Escriba (lección en vivo) ---
# Por defecto el Escriba trabaja bajo demanda; si CLASE_ESCRIBA_AUTO=1, genera un bloque
# cada 5 minutos de captura útil. El estado por clase puede cambiarlo o desactivarlo.
BLOQUE_SEG = int(os.environ.get("CLASE_BLOQUE_SEG", "300"))

# --- Auto-pausa por silencio (break/receso: no almacenar nada hasta que vuelva el audio) ---
SILENCIO_RMS = int(os.environ.get("CLASE_SILENCIO_RMS", "120"))       # RMS int16 bajo = silencio
SILENCIO_MIN = float(os.environ.get("CLASE_SILENCIO_MIN", "2"))       # minutos seguidos de silencio
SILENCIO_CHUNKS = max(1, round(SILENCIO_MIN * 60 / AUDIO_CHUNK_SEG))
VISUAL_IDLE_AUTOPAUSA = os.environ.get("CLASE_VISUAL_IDLE_AUTOPAUSA", "1").lower() not in {
    "0", "false", "no", "off"
}
VISUAL_IDLE_CHUNKS = max(1, int(os.environ.get("CLASE_VISUAL_IDLE_CHUNKS", "1")))
VISUAL_IDLE_DARK_RATIO = float(os.environ.get("CLASE_VISUAL_IDLE_DARK_RATIO", "0.72"))
VISUAL_IDLE_DARK_MEAN = float(os.environ.get("CLASE_VISUAL_IDLE_DARK_MEAN", "62"))
VISUAL_IDLE_LOW_STD = float(os.environ.get("CLASE_VISUAL_IDLE_LOW_STD", "18"))
VISUAL_IDLE_SKIP_CAPTURA = os.environ.get("CLASE_VISUAL_IDLE_SKIP_CAPTURA", "1").lower() not in {
    "0", "false", "no", "off"
}

# --- Captura de pantalla ---
CAPTURA_INTERVALO = float(os.environ.get("CLASE_CAPTURA_SEG", "2"))
MONITOR = int(os.environ.get("CLASE_MONITOR", "1"))
UMBRAL_DHASH = int(os.environ.get("CLASE_UMBRAL_DHASH", "6"))
UMBRAL_DHASH_DETALLE = int(os.environ.get("CLASE_UMBRAL_DHASH_DETALLE", "18"))
CAPTURA_OCR_MAX_CHARS = int(os.environ.get("CLASE_CAPTURA_OCR_MAX_CHARS", "12000"))
CAPTURA_CHECKPOINT_SIN_OCR_SEG = float(
    os.environ.get("CLASE_CAPTURA_CHECKPOINT_SIN_OCR_SEG", "10"))
# OCR vivo: en Windows se prefiere el motor nativo, sin GPU y sin modelos
# residentes. RapidOCR queda como fallback portable y limita ONNX para reducir
# carga sostenida. Un valor 0 en intra/inter conserva el auto-tuning de ONNX.
OCR_ENGINE = os.environ.get("CLASE_OCR_ENGINE", "auto").strip().lower()
if OCR_ENGINE not in {"auto", "windows", "rapidocr", "off"}:
    OCR_ENGINE = "auto"
_CPU_LOGICOS = max(1, int(os.cpu_count() or 1))
OCR_RAPID_INTRA_THREADS = min(
    _CPU_LOGICOS,
    max(0, int(os.environ.get("CLASE_OCR_RAPID_INTRA_THREADS", "2"))),
)
OCR_RAPID_INTER_THREADS = min(
    _CPU_LOGICOS,
    max(0, int(os.environ.get("CLASE_OCR_RAPID_INTER_THREADS", "1"))),
)
OCR_WINDOWS_LANGUAGES = tuple(
    tag.strip()
    for tag in os.environ.get(
        "CLASE_OCR_WINDOWS_LANGUAGES", "es-MX,en-US").split(",")
    if tag.strip()
)
# Fast path visual: una sola miniatura alimenta estabilidad, hashes e idle. El
# polling conserva su cadencia; solo se acota el trabajo semántico/persistente.
CAPTURA_PROBE_ANCHO = max(
    96, int(os.environ.get("CLASE_CAPTURA_PROBE_ANCHO", "320")))
CAPTURA_PIXEL_DELTA = max(
    1, int(os.environ.get("CLASE_CAPTURA_PIXEL_DELTA", "12")))
CAPTURA_CAMBIO_RATIO = max(
    0.0, float(os.environ.get("CLASE_CAPTURA_CAMBIO_RATIO", "0.001")))
CAPTURA_CAMBIO_MAYOR_RATIO = max(
    CAPTURA_CAMBIO_RATIO,
    float(os.environ.get("CLASE_CAPTURA_CAMBIO_MAYOR_RATIO", "0.08")),
)
CAPTURA_CAMBIO_MEDIA = max(
    0.0, float(os.environ.get("CLASE_CAPTURA_CAMBIO_MEDIA", "0.35")))
CAPTURA_OCR_INTERVALO_MIN = max(
    CAPTURA_INTERVALO,
    float(os.environ.get("CLASE_CAPTURA_OCR_INTERVALO_MIN", "6")),
)
CAPTURA_OCR_REFRESH_SEG = max(
    CAPTURA_OCR_INTERVALO_MIN,
    float(os.environ.get("CLASE_CAPTURA_OCR_REFRESH_SEG", "30")),
)
CAPTURA_CAMBIO_SUTIL_INTERVALO = max(
    CAPTURA_INTERVALO,
    float(os.environ.get("CLASE_CAPTURA_CAMBIO_SUTIL_INTERVALO", "2")),
)
CAPTURA_HEARTBEAT_SEG = max(
    CAPTURA_INTERVALO,
    float(os.environ.get("CLASE_CAPTURA_HEARTBEAT_SEG", "10")),
)
CAPTURA_TELEMETRIA_SEG = max(
    5.0, float(os.environ.get("CLASE_CAPTURA_TELEMETRIA_SEG", "30")))
CAPTURA_METRICAS_MUESTRAS = max(
    32, int(os.environ.get("CLASE_CAPTURA_METRICAS_MUESTRAS", "256")))
REGION = os.environ.get("CLASE_REGION", "").strip()  # "left,top,width,height" o vacío
# Capturas livianas: JPEG reescalado (una pantalla 34" en PNG pesaría 3-6 MB por captura)
CAPTURA_ANCHO_MAX = int(os.environ.get("CLASE_CAPTURA_ANCHO", "1920"))
CAPTURA_CALIDAD = int(os.environ.get("CLASE_CAPTURA_CALIDAD", "85"))


def region_captura() -> dict | None:
    """Devuelve la región configurada como dict de mss, o None (monitor completo)."""
    if not REGION:
        return None
    try:
        left, top, width, height = (int(x) for x in REGION.split(","))
        return {"left": left, "top": top, "width": width, "height": height}
    except ValueError:
        print(f"[config] CLASE_REGION inválida ({REGION!r}); usando monitor completo")
        return None


def ensure_dirs() -> None:
    for d in (CONTENIDO_DIR, CLASES_DIR, RESUMENES_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def stdout_utf8() -> None:
    """Consola Windows a UTF-8 para no crashear con tildes/emoji."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def hhmm(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def hhmmss_rel(segundos: float) -> str:
    s = int(segundos)
    # El panel interpola este mismo reloj en JavaScript entre polls. Mantener
    # exactamente HH:MM:SS evita que el valor del servidor (antes H:MM:SS)
    # alterne de ancho con el valor local cada vez que llega /api/estado.
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# UTF-8 SIEMPRE al importar config (todos los módulos lo importan): así ningún print con
# emoji/tildes crashea aunque el llamador no pase por un main() (REGLA 6 a prueba de agentes).
stdout_utf8()

# --- Latido del modo app: si el panel (dueño de los hijos) muere, los hijos se apagan ---
LATIDO = LOGS_DIR / "panel_latido.txt"
_INSTANCE_LOCKS: dict[str, dict] = {}
_INSTANCE_HEARTBEATS: dict[str, threading.Event] = {}
INSTANCE_HEARTBEAT_TIMEOUT = 60.0


def latido_perdido() -> bool:
    """True SOLO en modo app (CLASE_APP=1) si el panel lleva >60 s sin dar señales de vida.
    Garantiza que cerrar la app nunca deje procesos grabando en segundo plano."""
    if os.environ.get("CLASE_APP") != "1":
        return False
    try:
        return (time.time() - LATIDO.stat().st_mtime) > 60
    except OSError:
        return True


def _process_info(pid: int) -> dict:
    try:
        import psutil
        proc = psutil.Process(int(pid))
        return {
            "exists": True,
            "create_time": float(proc.create_time()),
            "cmdline": [str(parte) for parte in proc.cmdline()],
        }
    except Exception:
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return {"exists": False, "create_time": 0.0, "cmdline": []}
        return {"exists": True, "create_time": 0.0, "cmdline": []}


def _cmdline_contiene_modulo(cmdline, modulo: str, nombre: str) -> bool:
    partes = [str(parte).strip() for parte in (cmdline or []) if str(parte).strip()]
    if modulo:
        if modulo in partes:
            return True
        for indice, parte in enumerate(partes[:-1]):
            if parte == "-m" and partes[indice + 1] == modulo:
                return True
        return any(Path(parte).stem == modulo.rsplit(".", 1)[-1] for parte in partes)
    return any(Path(parte).stem == nombre for parte in partes)


def validar_lock_instancia(
        datos, *, nombre: str, modulo: str = "", class_dir=None,
        ahora: float | None = None, process_info: dict | None = None,
        heartbeat_timeout: float = INSTANCE_HEARTBEAT_TIMEOUT) -> dict:
    """Clasifica el propietario sin convertir un PID vivo ambiguo en lock obsoleto."""
    ahora = time.time() if ahora is None else float(ahora)
    if isinstance(datos, (int, str)):
        try:
            datos = {"pid": int(datos)}
        except (TypeError, ValueError):
            datos = {}
    if not isinstance(datos, dict):
        datos = {}
    try:
        pid = int(datos.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return {"owner_valid": False, "status": "stale_invalid_lock", "pid": pid}
    info = dict(process_info) if process_info is not None else _process_info(pid)
    if not info.get("exists"):
        return {"owner_valid": False, "status": "stale_pid", "pid": pid}

    schema = int(datos.get("schema_version") or 0)
    creado_lock = float(datos.get("create_time") or 0.0)
    creado_proceso = float(info.get("create_time") or 0.0)
    cmdline = info.get("cmdline") or []
    comando_valido = (
        _cmdline_contiene_modulo(cmdline, modulo, nombre)
        if modulo else str(datos.get("nombre") or nombre) == nombre)
    legado = bool(
        schema < 3 or not datos.get("owner_id") or not datos.get("module"))
    if legado:
        if modulo and cmdline and not comando_valido:
            return {
                "owner_valid": False, "status": "stale_wrong_command", "pid": pid}
        return {
            "owner_valid": True,
            "status": "legacy_owner_valid" if comando_valido else "legacy_owner_unknown",
            "pid": pid,
        }
    if creado_lock and creado_proceso and abs(creado_lock - creado_proceso) >= 0.01:
        return {"owner_valid": False, "status": "stale_pid_reused", "pid": pid}
    if modulo and cmdline and not comando_valido:
        return {"owner_valid": False, "status": "stale_wrong_command", "pid": pid}
    if modulo and str(datos.get("module") or "") != modulo:
        return {"owner_valid": False, "status": "stale_wrong_module", "pid": pid}

    if modulo and (not creado_proceso or not cmdline):
        return {
            "owner_valid": True,
            "status": "owner_unverifiable",
            "pid": pid,
            "owner_id": str(datos.get("owner_id") or ""),
            "heartbeat": float(datos.get("heartbeat") or 0.0),
        }

    esperado_clase = str(Path(class_dir).resolve()) if class_dir else ""
    clase_lock = str(datos.get("class_dir") or "")
    status = "valid_owner"
    if esperado_clase and clase_lock and Path(clase_lock) != Path(esperado_clase):
        status = "valid_owner_other_class"
    heartbeat = float(datos.get("heartbeat") or 0.0)
    if heartbeat and ahora - heartbeat > max(1.0, float(heartbeat_timeout)):
        status = "heartbeat_lost"
    return {
        "owner_valid": True,
        "status": status,
        "pid": pid,
        "owner_id": str(datos.get("owner_id") or ""),
        "heartbeat": heartbeat,
    }


def _leer_lock(path: Path):
    try:
        raw = path.read_text(encoding="utf-8").strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return int(raw)
    except (OSError, ValueError):
        return {}


def _escribir_lock(path: Path, datos: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def renovar_instancia(nombre: str, class_dir=None) -> bool:
    local = _INSTANCE_LOCKS.get(nombre)
    if not local:
        return False
    lock = LOGS_DIR / f"lock_{nombre}.pid"
    actual = _leer_lock(lock)
    if not isinstance(actual, dict) or actual.get("owner_id") != local.get("owner_id"):
        return False
    actual["heartbeat"] = time.time()
    if class_dir is not None:
        actual["class_dir"] = str(Path(class_dir).resolve())
    try:
        _escribir_lock(lock, actual)
    except OSError:
        return False
    _INSTANCE_LOCKS[nombre] = dict(actual)
    return True


def iniciar_heartbeat_instancia(
        nombre: str, *, class_dir=None, intervalo: float = 10.0) -> bool:
    if nombre not in _INSTANCE_LOCKS:
        return False
    previo = _INSTANCE_HEARTBEATS.get(nombre)
    if previo is not None and not previo.is_set():
        return True
    detener = threading.Event()
    _INSTANCE_HEARTBEATS[nombre] = detener

    def latir() -> None:
        while not detener.wait(max(0.1, float(intervalo))):
            if not renovar_instancia(nombre, class_dir=class_dir):
                return

    threading.Thread(
        target=latir,
        name=f"instance-heartbeat-{nombre}",
        daemon=True,
    ).start()
    return True


def diagnosticar_instancia(nombre: str, *, modulo: str = "", class_dir=None) -> dict:
    datos = _leer_lock(LOGS_DIR / f"lock_{nombre}.pid")
    diagnostico = validar_lock_instancia(
        datos, nombre=nombre, modulo=modulo, class_dir=class_dir)
    if isinstance(datos, dict):
        diagnostico.update({
            "module": str(datos.get("module") or ""),
            "class_dir": str(datos.get("class_dir") or ""),
            "owner_id": str(datos.get("owner_id") or ""),
            "heartbeat": float(datos.get("heartbeat") or 0.0),
        })
    return diagnostico


def liberar_instancia(nombre: str, owner_id: str | None = None) -> bool:
    detener = _INSTANCE_HEARTBEATS.pop(nombre, None)
    if detener is not None:
        detener.set()
    local = _INSTANCE_LOCKS.get(nombre)
    if not local:
        return False
    esperado = str(owner_id or local.get("owner_id") or "")
    lock = LOGS_DIR / f"lock_{nombre}.pid"
    actual = _leer_lock(lock)
    if not isinstance(actual, dict) or str(actual.get("owner_id") or "") != esperado:
        _INSTANCE_LOCKS.pop(nombre, None)
        return False
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    _INSTANCE_LOCKS.pop(nombre, None)
    return True


def limpiar_instancia_obsoleta(
        nombre: str, *, modulo: str = "", class_dir=None) -> bool:
    """Elimina un lock solo tras confirmar que no conserva un owner real."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOGS_DIR / f"lock_{nombre}.pid"
    claim = LOGS_DIR / f"lock_{nombre}.claim"
    adquirido = False
    for _ in range(200):
        try:
            claim.mkdir()
            adquirido = True
            break
        except FileExistsError:
            try:
                if time.time() - claim.stat().st_mtime > 10:
                    claim.rmdir()
                    continue
            except OSError:
                pass
            time.sleep(0.01)
    if not adquirido:
        return False
    try:
        diagnostico = diagnosticar_instancia(
            nombre, modulo=modulo, class_dir=class_dir)
        if diagnostico.get("owner_valid"):
            return False
        existia = lock.exists()
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return existia
    finally:
        try:
            claim.rmdir()
        except OSError:
            pass


def instancia_unica(nombre: str, *, modulo: str = "", class_dir=None) -> bool:
    """Candado por módulo: si otro proceso vivo ya corre este módulo, devuelve False.
    Evita pipelines duplicados (doble clic, doble panel, relanzamientos manuales),
    que pelean por el loopback WASAPI y producen capturas zombi ('Stream not open')."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOGS_DIR / f"lock_{nombre}.pid"
    claim = LOGS_DIR / f"lock_{nombre}.claim"

    adquirido = False
    for _ in range(200):
        try:
            claim.mkdir()
            adquirido = True
            break
        except FileExistsError:
            try:
                if time.time() - claim.stat().st_mtime > 10:
                    claim.rmdir()
                    continue
            except OSError:
                pass
            time.sleep(0.01)
    if not adquirido:
        return False

    def liberar_claim() -> None:
        try:
            claim.rmdir()
        except OSError:
            pass

    if lock.is_file():
        datos = _leer_lock(lock)
        try:
            pid_lock = int(datos.get("pid") if isinstance(datos, dict) else datos)
        except (TypeError, ValueError):
            pid_lock = 0
        if pid_lock == os.getpid() and nombre in _INSTANCE_LOCKS:
            local = _INSTANCE_LOCKS[nombre]
            if (isinstance(datos, dict)
                    and datos.get("owner_id") == local.get("owner_id")):
                liberar_claim()
                return True
        diagnostico = validar_lock_instancia(
            datos,
            nombre=nombre,
            modulo=modulo,
            class_dir=class_dir,
        )
        if diagnostico.get("owner_valid"):
            liberar_claim()
            return False
    try:
        info_propio = _process_info(os.getpid())
        creado = float(info_propio.get("create_time") or time.time())
        ahora = time.time()
        owner_id = uuid.uuid4().hex
        datos_nuevos = {
            "schema_version": 3,
            "pid": os.getpid(),
            "create_time": creado,
            "nombre": nombre,
            "module": str(modulo or nombre),
            "command": f"{Path(sys.executable).name} -m {modulo or nombre}",
            "class_dir": str(Path(class_dir).resolve()) if class_dir else "",
            "started_at": ahora,
            "heartbeat": ahora,
            "owner_id": owner_id,
        }
        _escribir_lock(lock, datos_nuevos)
        _INSTANCE_LOCKS[nombre] = dict(datos_nuevos)
        atexit.register(liberar_instancia, nombre, owner_id)
        return True
    except OSError:
        return False
    finally:
        liberar_claim()


def log_error(origen: str, msg: str) -> None:
    """Append a logs/errores.log sin romper nunca al llamador."""
    try:
        ensure_dirs()
        marca = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOGS_DIR / "errores.log", "a", encoding="utf-8") as f:
            f.write(f"[{marca}] [{origen}] {msg}\n")
    except Exception:
        pass
