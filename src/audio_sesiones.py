"""Audio POR APLICACIÓN vía pycaw (ctypes puro: no lo bloquea Smart App Control).

Permite centrar la clase en el NAVEGADOR elegido (Chrome/Edge) aunque se usen audífonos:
- pico_navegador(): ¿está sonando el navegador ahora? (medidor 0..1 de sus sesiones)
  -> la auto-pausa se decide por el navegador, no por otras apps (música, WhatsApp).
- enfoque(): silencia/restaura TODAS las demás apps para que el loopback capture
  únicamente la clase.

Si pycaw no está disponible, todo degrada con gracia (None/0) y el pipeline sigue
funcionando con el RMS global de siempre.
"""
from contextlib import contextmanager
import threading

from . import config

NAVEGADORES = {"chrome": ("chrome.exe",), "msedge": ("msedge.exe",)}
_disponible = None
_enfoque_snapshots: dict[str, dict[str, bool]] = {}
_enfoque_lock = threading.RLock()


def _pid_raiz_navegador(proc, objetivo: tuple[str, ...]) -> tuple[int, str]:
    """Devuelve el ancestro navegador mas alto para capturar todo el arbol."""
    raiz = proc
    actual = proc
    for _ in range(12):
        try:
            padre = actual.parent()
            if not padre:
                break
            nombre = padre.name().lower()
            if nombre not in objetivo:
                break
            raiz = padre
            actual = padre
        except Exception:
            break
    try:
        return int(raiz.pid), raiz.name().lower()
    except Exception:
        return int(proc.pid), ""


def disponible() -> bool:
    global _disponible
    if _disponible is None:
        try:
            from pycaw.pycaw import AudioUtilities  # noqa: F401
            _disponible = True
        except Exception as e:
            config.log_error("audio_sesiones", f"pycaw no disponible: {e}")
            _disponible = False
    return _disponible


@contextmanager
def _sesiones():
    # COM debe inicializarse EN CADA HILO que lo use (los hilos de Flask no lo traen:
    # causaba "No se ha llamado a CoInitialize" y dejaba ciegos a enfoque/pico).
    comtypes_mod = None
    com_inicializado = False
    try:
        import comtypes
        comtypes.CoInitialize()
        comtypes_mod = comtypes
        com_inicializado = True
    except Exception:
        pass
    try:
        from pycaw.pycaw import AudioUtilities
        yield AudioUtilities.GetAllSessions()
    finally:
        if com_inicializado and comtypes_mod is not None:
            try:
                comtypes_mod.CoUninitialize()
            except Exception:
                pass


def _clave_sesion(sesion) -> str:
    """Clave estable por sesión; PID/proceso es el fallback conservador."""
    for atributo in ("InstanceIdentifier", "Identifier"):
        try:
            valor = getattr(sesion, atributo, None)
            valor = valor() if callable(valor) else valor
            if valor:
                return f"session:{valor}"
        except Exception:
            continue
    try:
        proceso = sesion.Process
        pid = int(proceso.pid)
        nombre = proceso.name().lower()
        return f"process:{pid}:{nombre}"
    except Exception:
        return ""


def sesiones_navegador(fuente: str) -> list[dict]:
    """Sesiones de audio del navegador con pid y pico, ordenables por actividad."""
    if fuente not in NAVEGADORES or not disponible():
        return []
    objetivo = NAVEGADORES[fuente]
    out = []
    try:
        from pycaw.pycaw import IAudioMeterInformation
        with _sesiones() as sesiones:
            for s in sesiones:
                try:
                    if not s.Process:
                        continue
                    nombre = s.Process.name().lower()
                    if nombre not in objetivo:
                        continue
                    medidor = s._ctl.QueryInterface(IAudioMeterInformation)
                    muteado = None
                    volumen = None
                    try:
                        muteado = bool(s.SimpleAudioVolume.GetMute())
                        volumen = float(s.SimpleAudioVolume.GetMasterVolume())
                    except Exception:
                        pass
                    root_pid, root_nombre = _pid_raiz_navegador(s.Process, objetivo)
                    out.append({"pid": int(s.Process.pid), "nombre": nombre,
                                "root_pid": root_pid, "root_nombre": root_nombre,
                                "pico": float(medidor.GetPeakValue()),
                                "muteado": muteado, "volumen": volumen})
                except Exception:
                    continue
    except Exception as e:
        config.log_error("audio_sesiones", f"sesiones_navegador: {type(e).__name__}: {e}")
    return out


def pid_navegador(fuente: str) -> int | None:
    """PID del navegador que conviene usar para process-loopback.

    Si hay varias sesiones (normal en Chrome), se elige la de mayor pico; si todas
    están en silencio, se usa la primera para que el stream pueda arrancar.
    """
    sesiones = sesiones_navegador(fuente)
    if not sesiones:
        return None
    sesiones.sort(key=lambda s: s.get("pico", 0.0), reverse=True)
    return int(sesiones[0].get("root_pid") or sesiones[0]["pid"])


def pico_navegador(fuente: str) -> float | None:
    """Pico instantáneo (0..1) de las sesiones del navegador. None = no medible."""
    if fuente not in NAVEGADORES or not disponible():
        return None
    sesiones = sesiones_navegador(fuente)
    return max((s["pico"] for s in sesiones), default=0.0)


def enfoque(fuente: str, activar: bool) -> int:
    """Silencia apps ajenas y restaura solo sesiones que este proceso modificó."""
    if fuente not in NAVEGADORES or not disponible():
        return 0
    objetivo = NAVEGADORES[fuente]
    n = 0
    with _enfoque_lock:
        snapshot = _enfoque_snapshots.setdefault(fuente, {}) if activar else (
            _enfoque_snapshots.get(fuente) or {})
        enumeracion_completa = False
        restauraciones_pendientes: dict[str, bool] = {}
        try:
            with _sesiones() as sesiones:
                for s in sesiones:
                    clave = ""
                    try:
                        nombre = s.Process.name().lower() if s.Process else ""
                        if not nombre or nombre in objetivo:
                            continue
                        clave = _clave_sesion(s)
                        if not clave:
                            continue
                        volumen = s.SimpleAudioVolume
                        actual = bool(volumen.GetMute())
                        if activar:
                            if not actual:
                                volumen.SetMute(1, None)
                                # Registrar propiedad solo después de cambiar la sesión.
                                # Otro proceso que la encuentre ya muteada no debe
                                # apropiarse de ella ni volver a mutearla al restaurar.
                                snapshot.setdefault(clave, False)
                                n += 1
                        elif clave in snapshot:
                            previo = bool(snapshot[clave])
                            try:
                                if actual != previo:
                                    volumen.SetMute(1 if previo else 0, None)
                                    n += 1
                            except Exception:
                                restauraciones_pendientes[clave] = previo
                    except Exception:
                        if not activar and clave in snapshot:
                            restauraciones_pendientes[clave] = bool(snapshot[clave])
                        continue
                enumeracion_completa = True
        except Exception as e:
            config.log_error("audio_sesiones", f"enfoque: {type(e).__name__}: {e}")
        if not activar and enumeracion_completa:
            if restauraciones_pendientes:
                _enfoque_snapshots[fuente] = restauraciones_pendientes
                try:
                    config.log_error(
                        "audio_sesiones",
                        "enfoque_restore_pending: "
                        f"fuente={fuente} sesiones={len(restauraciones_pendientes)}",
                    )
                except Exception:
                    pass
            else:
                _enfoque_snapshots.pop(fuente, None)
    return n


def restaurar_enfoques() -> int:
    """Restaura todos los snapshots propiedad del proceso, sin desmute global."""
    with _enfoque_lock:
        fuentes = list(_enfoque_snapshots)
    return sum(enfoque(fuente, False) for fuente in fuentes)
