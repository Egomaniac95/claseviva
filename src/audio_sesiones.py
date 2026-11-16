"""Audio por aplicación vía sesiones de salida de Windows y captura por PID.

Permite centrar la clase en cualquier aplicación abierta, incluidos sus procesos hijos.
Los nombres *_navegador se conservan por compatibilidad con los workers anteriores:
- pico_navegador(): medidor 0..1 de las sesiones de la aplicación elegida.
- enfoque(): silencia/restaura TODAS las demás apps para que el loopback capture
  únicamente la clase.

Si pycaw no está disponible, todo degrada con gracia (None/0) y el pipeline sigue
funcionando con el RMS global de siempre.
"""
from contextlib import contextmanager
import ctypes
import re
import sys
import threading

from . import config

NAVEGADORES = {"chrome": ("chrome.exe",), "msedge": ("msedge.exe",)}
# Estos programas pueden elegirse antes de que creen una sesión de sonido.
# Cualquier otra aplicación se descubre por su sesión real de salida.
APLICACIONES_AUDIO = {
    "chrome.exe": ("Google Chrome", "chrome"),
    "msedge.exe": ("Microsoft Edge", "msedge"),
    "comet.exe": ("Comet", "app"),
    "firefox.exe": ("Mozilla Firefox", "app"),
    "brave.exe": ("Brave", "app"),
    "opera.exe": ("Opera", "app"),
    "vivaldi.exe": ("Vivaldi", "app"),
    "arc.exe": ("Arc", "app"),
    "zoom.exe": ("Zoom", "app"),
    "teams.exe": ("Microsoft Teams", "app"),
    "ms-teams.exe": ("Microsoft Teams", "app"),
    "discord.exe": ("Discord", "app"),
    "slack.exe": ("Slack", "app"),
    "whatsapp.exe": ("WhatsApp", "app"),
    "whatsapp.root.exe": ("WhatsApp", "app"),
    "chatgpt.exe": ("ChatGPT", "app"),
    "vlc.exe": ("VLC", "app"),
    "spotify.exe": ("Spotify", "app"),
    "wmplayer.exe": ("Windows Media Player", "app"),
    "mediaplayer.exe": ("Reproductor multimedia", "app"),
}
PROCESOS_SISTEMA = frozenset({
    "taskmgr.exe", "explorer.exe", "systemsettings.exe", "applicationframehost.exe",
    "textinputhost.exe", "searchhost.exe", "searchapp.exe", "runtimebroker.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "widgetservice.exe",
    "widgets.exe", "svchost.exe", "audiodg.exe", "services.exe", "system",
    "winlogon.exe", "csrss.exe", "dwm.exe", "smss.exe", "conhost.exe", "dllhost.exe",
})
MOTORES_EMBEBIDOS = frozenset({"msedgewebview2.exe", "cefhelper.exe", "qtwebengineprocess.exe"})
_disponible = None
_enfoque_snapshots: dict[str, dict[str, bool]] = {}
_enfoque_lock = threading.RLock()


def _identidad_fuente(fuente: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"proceso:([1-9][0-9]*):([1-9][0-9]*)", str(fuente))
    return (int(match[1]), int(match[2])) if match else None


def es_fuente_aplicacion(fuente: str) -> bool:
    return fuente in NAVEGADORES or _identidad_fuente(fuente) is not None


def _proceso_fuente(fuente: str):
    identidad = _identidad_fuente(fuente)
    if identidad is None:
        return None
    import psutil
    try:
        proceso = psutil.Process(identidad[0])
        if proceso.is_running() and round(proceso.create_time() * 1000) == identidad[1]:
            return proceso
    except (psutil.Error, OSError):
        pass
    return None


def fuente_valida(fuente: str) -> bool:
    if fuente == "sistema" or fuente in NAVEGADORES:
        return True
    proceso = _proceso_fuente(fuente)
    try:
        return proceso is not None and proceso.name().casefold() not in PROCESOS_SISTEMA | MOTORES_EMBEBIDOS
    except Exception:
        return False


def nombre_fuente(fuente: str) -> str:
    nombres = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "sistema": "Todo el sistema"}
    if fuente in nombres:
        return nombres[fuente]
    proceso = _proceso_fuente(fuente)
    try:
        return _nombre_aplicacion(proceso)[0] if proceso else "Aplicacion cerrada"
    except Exception:
        return "Aplicacion cerrada"


def _nombre_aplicacion(proceso) -> tuple[str, str]:
    nombre = proceso.name()
    conocido = APLICACIONES_AUDIO.get(nombre.casefold())
    if conocido:
        return conocido
    etiqueta = re.sub(r"\.exe$", "", nombre, flags=re.IGNORECASE).replace("_", " ")
    return etiqueta, "app"


def _aplicacion_de_proceso(proceso):
    """Atribuye WebView a su dueño y agrupa hijos del mismo ejecutable."""
    nombre = proceso.name().casefold()
    if nombre in MOTORES_EMBEBIDOS:
        for padre in proceso.parents():
            nombre_padre = padre.name().casefold()
            if nombre_padre in MOTORES_EMBEBIDOS:
                continue
            if nombre_padre in PROCESOS_SISTEMA:
                return None
            proceso, nombre = padre, nombre_padre
            break
        else:
            return None
    if nombre in PROCESOS_SISTEMA:
        return None
    import psutil
    pid, _ = _pid_raiz_navegador(proceso, (nombre,))
    return psutil.Process(pid)


def _coincide_fuente(proceso, fuente: str) -> bool:
    try:
        if fuente in NAVEGADORES:
            return proceso.name().lower() in NAVEGADORES[fuente]
        objetivo = _proceso_fuente(fuente)
        if objetivo is None:
            return False
        return proceso.pid == objetivo.pid or any(
            padre.pid == objetivo.pid for padre in proceso.parents())
    except Exception:
        return False


def _pids_ventanas() -> set[int]:
    """Incluye apps visibles aunque aún no hayan creado su sesión de audio."""
    if sys.platform != "win32":
        return set()
    from ctypes import wintypes
    pids = set()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    @callback_type
    def visitar(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(int(pid.value))
        return True

    user32.EnumWindows(visitar, 0)
    return pids


def aplicaciones_audio() -> list[dict]:
    """Fuentes plausibles: sesiones de salida y apps de audio conocidas abiertas."""
    import psutil
    from . import app_icons
    candidatos = {}

    def agregar(proceso, sesion=None):
        try:
            raiz = _aplicacion_de_proceso(proceso)
            if raiz is None or not raiz.is_running():
                return
            if sesion is None and raiz.name().casefold() not in APLICACIONES_AUDIO:
                return
            pid = raiz.pid
            etiqueta, icono = _nombre_aplicacion(raiz)
            nacimiento = round(raiz.create_time() * 1000)
            clave = f"proceso:{pid}:{nacimiento}"
            item = candidatos.setdefault(clave, {
                "id": clave, "pid": pid, "nombre": raiz.name(),
                "aplicacion": etiqueta, "etiqueta": etiqueta, "icono": icono,
                "icono_url": (app_icons.icon_url(raiz) if clave not in candidatos
                              else candidatos[clave].get("icono_url", "")),
                "abierto": True, "sesiones": 0, "senal_detectada": False,
                "sesiones_silenciadas": 0,
            })
            if sesion is not None:
                item["sesiones"] += 1
                try:
                    from pycaw.pycaw import IAudioMeterInformation
                    pico = float(sesion._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
                    muteada = bool(sesion.SimpleAudioVolume.GetMute())
                    item["sesiones_silenciadas"] += int(muteada)
                    if pico > .0001 and not muteada:
                        item["senal_detectada"] = True
                except Exception:
                    pass
        except (psutil.Error, OSError):
            pass

    if disponible():
        try:
            with _sesiones() as sesiones:
                for sesion in sesiones:
                    if sesion.Process and getattr(sesion, "State", None) != 2:
                        agregar(sesion.Process, sesion)
        except Exception as error:
            config.log_error("audio_sesiones", f"inventario: {type(error).__name__}")
    for pid in _pids_ventanas():
        try:
            agregar(psutil.Process(pid))
        except (psutil.Error, OSError):
            continue
    for item in candidatos.values():
        item["silenciada"] = bool(item["sesiones"] and item["sesiones_silenciadas"] == item["sesiones"])
        item["detalle"] = (
            "Audio detectado. Se capturará solo esta aplicación." if item["senal_detectada"] else
            "Silenciada en Windows. Activa su volumen para capturar." if item["silenciada"] else
            "Sesión de audio disponible. Reproduce la clase para comprobar la señal." if item["sesiones"] else
            "Abierta, sin audio todavía. Reproduce la clase y actualiza las fuentes.")
        # Mostrar el PID solo si hace falta distinguir instancias independientes.
        if sum(other["aplicacion"] == item["aplicacion"] for other in candidatos.values()) > 1:
            item["etiqueta"] = f"{item['aplicacion']} (PID {item['pid']})"
    return sorted(candidatos.values(), key=lambda item: (item["aplicacion"].casefold(), item["pid"]))


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
        sesiones = list(AudioUtilities.GetAllSessions())
        # La enumeración predeterminada de pycaw solo cubre la salida por defecto.
        # Añadir las demás salidas activas; nunca enumerar entradas de micrófono.
        try:
            from pycaw.constants import DEVICE_STATE, EDataFlow
            from pycaw.api.audiopolicy import IAudioSessionControl2
            from pycaw.utils import AudioSession
            vistas = {_clave_sesion(s) for s in sesiones}
            for dispositivo in AudioUtilities.GetAllDevices(
                    data_flow=EDataFlow.eRender.value, device_state=DEVICE_STATE.ACTIVE.value):
                try:
                    enumerador = dispositivo.AudioSessionManager.GetSessionEnumerator()
                    for indice in range(enumerador.GetCount()):
                        ctl = enumerador.GetSession(indice).QueryInterface(IAudioSessionControl2)
                        sesion = AudioSession(ctl)
                        clave = _clave_sesion(sesion)
                        if clave and clave not in vistas:
                            sesiones.append(sesion)
                            vistas.add(clave)
                except Exception:
                    continue
        except Exception:
            pass
        yield sesiones
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
    """Sesiones de la aplicación seleccionada; admite los alias de Chrome/Edge."""
    if not es_fuente_aplicacion(fuente) or not disponible():
        return []
    out = []
    try:
        from pycaw.pycaw import IAudioMeterInformation
        with _sesiones() as sesiones:
            for s in sesiones:
                try:
                    if not s.Process:
                        continue
                    nombre = s.Process.name().lower()
                    if not _coincide_fuente(s.Process, fuente):
                        continue
                    pico = 0.0
                    try:
                        medidor = s._ctl.QueryInterface(IAudioMeterInformation)
                        pico = float(medidor.GetPeakValue())
                    except Exception:
                        pass
                    muteado = None
                    volumen = None
                    try:
                        muteado = bool(s.SimpleAudioVolume.GetMute())
                        volumen = float(s.SimpleAudioVolume.GetMasterVolume())
                    except Exception:
                        pass
                    objetivo = NAVEGADORES.get(fuente, (nombre,))
                    root_pid, root_nombre = _pid_raiz_navegador(s.Process, objetivo)
                    identidad = _identidad_fuente(fuente)
                    if identidad:
                        root_pid = identidad[0]
                    out.append({"pid": int(s.Process.pid), "nombre": nombre,
                                "root_pid": root_pid, "root_nombre": root_nombre,
                                "pico": pico,
                                "muteado": muteado, "volumen": volumen})
                except Exception:
                    continue
    except Exception as e:
        config.log_error("audio_sesiones", f"sesiones_navegador: {type(e).__name__}: {e}")
    if not out:
        proceso = _proceso_fuente(fuente)
        if proceso is not None:
            try:
                out.append({"pid": proceso.pid, "root_pid": proceso.pid,
                            "nombre": proceso.name(), "root_nombre": proceso.name(),
                            "pico": 0.0, "muteado": None, "volumen": None})
            except Exception:
                pass
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
    """Pico instantáneo (0..1) de la aplicación seleccionada. None = no medible."""
    if not es_fuente_aplicacion(fuente) or not disponible():
        return None
    sesiones = sesiones_navegador(fuente)
    return max((s["pico"] for s in sesiones), default=0.0)


def enfoque(fuente: str, activar: bool) -> int:
    """Silencia apps ajenas y restaura solo sesiones que este proceso modificó."""
    if not es_fuente_aplicacion(fuente) or not disponible():
        return 0
    if activar and fuente not in NAVEGADORES and _proceso_fuente(fuente) is None:
        return 0
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
                        if not nombre or _coincide_fuente(s.Process, fuente):
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
