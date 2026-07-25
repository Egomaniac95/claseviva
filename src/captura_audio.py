"""Captura el AUDIO DEL SISTEMA (voz del profesor en Meet) vía WASAPI loopback
en chunks WAV de 30 s dentro de clases/<id>/audio/.

AUTO-PAUSA POR SILENCIO: si pasan CLASE_SILENCIO_MIN minutos (default 2) sin audio
(RMS < CLASE_SILENCIO_RMS), se asume break/receso: se activa `auto_pausa` en el estado
(pantalla y escriba también dejan de almacenar) y los chunks silenciosos se DESCARTAN.
Se sigue escuchando en silencio; al volver el audio se reanuda todo solo.

- Respeta el estado: pausado -> no graba; fin -> termina.
- Escritura atómica: primero .part, luego rename a .wav.
- Test aislado:  python -m src.captura_audio --test 10
"""
import array
import hashlib
import math
import os
import queue
import sys
import threading
import time
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

import pyaudiowpatch as pyaudio

from . import audio_sesiones
from . import audio_manifest
from . import config
from . import estado as est
from . import loopback_proceso
from . import visual_idle

FRAMES_BUF = 1024
PICO_SESION_SILENCIO = 0.005
PICO_SESION_ACTIVA = float(os.environ.get(
    "CLASE_PICO_SESION_ACTIVA", str(PICO_SESION_SILENCIO)))
PCM_CASI_CERO_RMS = float(os.environ.get("CLASE_PCM_CASI_CERO_RMS", "5"))
PCM_CASI_CERO_PEAK = int(os.environ.get("CLASE_PCM_CASI_CERO_PEAK", "8"))
MENSAJE_CHROME_MUTEADO = (
    "Chrome parece silenciado o sin audio. Si lo silencias desde el mezclador, "
    "ClaseViva no puede capturarlo."
)
MENSAJE_EDGE_MUTEADO = (
    "Microsoft Edge parece silenciado o sin audio. Si lo silencias desde el mezclador, "
    "ClaseViva no puede capturarlo."
)


def nombre_fuente_audio(fuente: str) -> str:
    return {"chrome": "Chrome", "msedge": "Microsoft Edge"}.get(fuente, fuente or "navegador")


def mensaje_muteado(fuente: str) -> str:
    return MENSAJE_EDGE_MUTEADO if fuente == "msedge" else MENSAJE_CHROME_MUTEADO


def mensaje_video_pausado(fuente: str) -> str:
    return (f"No se detecta audio de {nombre_fuente_audio(fuente)}. "
            "Si el video está pausado, esto es normal.")


def _chunks_warning(chunks_umbral: int) -> int:
    try:
        cfg = int(os.environ.get("CLASE_AUDIO_WARNING_CHUNKS", "2"))
    except ValueError:
        cfg = 2
    return max(2, min(max(2, int(chunks_umbral)), cfg))


def diagnostico_silencio_proceso(fuente: str, modo: str, rms: float,
                                 pico_sesion: float | None,
                                 chunks_silencio: int,
                                 chunks_umbral: int,
                                 silencio_rms: int) -> dict:
    """Diagnostico puro para distinguir silencio normal de navegador muteado/sin audio.

    No intenta adivinar si la clase deberia estar sonando. Solo avisa cuando el
    process-loopback del navegador entrega silencio durante varios chunks y el medidor
    de la sesion del navegador tambien esta en cero.
    """
    if fuente not in audio_sesiones.NAVEGADORES or ":proceso" not in (modo or ""):
        return {"warning": "", "detalle": ""}
    if chunks_silencio < _chunks_warning(chunks_umbral):
        return {"warning": "", "detalle": ""}
    if rms >= silencio_rms:
        return {"warning": "", "detalle": ""}
    if pico_sesion is None or pico_sesion >= PICO_SESION_SILENCIO:
        return {"warning": "", "detalle": ""}

    nombre = nombre_fuente_audio(fuente)
    warning = mensaje_muteado(fuente)
    detalle = (f"{nombre}: process-loopback con RMS {rms:.0f} y pico de sesion "
               f"{pico_sesion:.3f} durante {chunks_silencio} chunks. Aviso no fatal: "
               f"{mensaje_video_pausado(fuente)} Si suena otro navegador, "
               f"no se captura porque la fuente elegida es {nombre}.")
    return {"warning": warning, "detalle": detalle}


def proceso_loopback_silencioso_con_pantalla_activa(
        fuente: str, modo: str, rms: float, pico_sesion: float | None,
        visual_es_idle: bool, silencio_rms: int) -> bool:
    """Detecta PID de navegador probablemente obsoleto antes de auto-pausar.

    Si el process-loopback del navegador cae bajo el umbral pero la pantalla sigue activa,
    se prueba fallback de dispositivo con enfoque antes de declarar un break de clase.
    """
    if fuente not in audio_sesiones.NAVEGADORES or ":proceso" not in (modo or ""):
        return False
    if visual_es_idle:
        return False
    if rms >= silencio_rms:
        return False
    if pico_sesion is not None and pico_sesion < PICO_SESION_SILENCIO:
        return False
    return True


def calcular_rms(data: bytes) -> float:
    """RMS de PCM int16 submuestreado sin fijarse en un solo canal estereo."""
    usable = len(data) - (len(data) % 2)
    # Un paso par muestrea siempre el mismo canal en PCM estereo intercalado.
    muestras = array.array("h", data[:usable])[::15]
    if not muestras:
        return 0.0
    return math.sqrt(sum(x * x for x in muestras) / len(muestras))


def calcular_peak(data: bytes) -> int:
    """Pico absoluto aproximado de PCM int16."""
    usable = len(data) - (len(data) % 2)
    muestras = array.array("h", data[:usable])[::15]
    if not muestras:
        return 0
    return max(abs(int(x)) for x in muestras)


def pcm_tiene_senal_util(rms: float, peak: int, silencio_rms: int | None = None) -> bool:
    """Distingue voz baja de PCM casi plano sin depender solo del RMS global."""
    silencio_rms = config.SILENCIO_RMS if silencio_rms is None else int(silencio_rms)
    return (
        rms >= silencio_rms
        or (
            rms >= max(12.0, silencio_rms * 0.25)
            and peak >= max(100, silencio_rms * 2)
        )
    )


def pcm_parcial_publicable(data: bytes, canales: int,
                           silencio_rms: int | None = None) -> bool:
    """Impide publicar como evidencia un parcial vacio o plano del watchdog."""
    frame_bytes = max(2, int(canales) * 2)
    if len(data) < frame_bytes:
        return False
    return pcm_tiene_senal_util(
        calcular_rms(data), calcular_peak(data), silencio_rms)


def clasificar_muestra_audio(fuente: str, rms: float, peak: int,
                             pico_sesion: float | None,
                             silencio_rms: int | None = None) -> dict:
    """Clasifica si un chunk debe guardarse, descartarse o denunciar captura rota."""
    silencio_rms = config.SILENCIO_RMS if silencio_rms is None else int(silencio_rms)
    fuente_nav = fuente in audio_sesiones.NAVEGADORES
    navegador_activo = (
        fuente_nav and pico_sesion is not None and pico_sesion >= PICO_SESION_ACTIVA)
    pcm_casi_cero = rms <= PCM_CASI_CERO_RMS and peak <= PCM_CASI_CERO_PEAK
    captura_rota = navegador_activo and pcm_casi_cero
    pcm_util = pcm_tiene_senal_util(rms, peak, silencio_rms)
    if captura_rota:
        return {
            "es_silencio": True,
            "captura_rota": True,
            "guardar": False,
            "navegador_activo": navegador_activo,
            "pcm_util": False,
        }
    if navegador_activo:
        return {
            "es_silencio": False,
            "captura_rota": False,
            "guardar": True,
            "navegador_activo": True,
            "pcm_util": pcm_util,
        }
    # El medidor de sesion puede quedar en cero aunque el loopback entregue PCM
    # valido. La evidencia PCM fuerte prevalece para no descartar voz real.
    es_silencio = not pcm_util
    return {
        "es_silencio": es_silencio,
        "captura_rota": False,
        "guardar": not es_silencio,
        "navegador_activo": False,
        "pcm_util": pcm_util,
    }


def seleccionar_sesion_audio(
        sesiones: list[dict],
        pid_bloqueados: dict[int, float],
        ahora: float | None = None) -> tuple[dict | None, str]:
    """Elige sesion activa sin reutilizar PIDs bloqueados por captura plana."""
    if not sesiones:
        return None, "sin_sesion"
    ahora = time.time() if ahora is None else float(ahora)
    candidatas = []
    for sesion in sesiones:
        pid = int(sesion.get("root_pid") or sesion.get("pid") or 0)
        if pid and pid_bloqueados.get(pid, 0.0) <= ahora:
            candidatas.append(sesion)
    if not candidatas:
        return None, "pid_bloqueado"
    candidatas.sort(key=lambda s: float(s.get("pico") or 0.0), reverse=True)
    return candidatas[0], "ok"


def metricas_wav(ruta: Path, ventana_seg: float = 1.0,
                 silencio_rms: int | None = None) -> dict:
    """Métricas stdlib para auditar WAV sin modificar evidencia real."""
    silencio_rms = config.SILENCIO_RMS if silencio_rms is None else int(silencio_rms)
    with wave.open(str(ruta), "rb") as wf:
        rate = int(wf.getframerate())
        canales = int(wf.getnchannels())
        sampwidth = int(wf.getsampwidth())
        frames = int(wf.getnframes())
        raw = wf.readframes(frames)
    if sampwidth != 2:
        raise ValueError(f"metricas_wav espera PCM int16, sampwidth={sampwidth}")
    muestras = array.array("h", raw)
    if not muestras or rate <= 0 or canales <= 0:
        return {
            "duration_sec": 0.0, "sample_rate": rate, "channels": canales,
            "rms": 0.0, "peak": 0, "rms_ventanas": [],
            "silent_window_pct": 100.0, "clipping_pct": 0.0,
            "useful_audio": False,
        }
    mono = []
    for i in range(0, len(muestras), canales):
        frame = muestras[i:i + canales]
        if frame:
            mono.append(sum(frame) / len(frame))
    if not mono:
        rms = 0.0
        peak = 0
    else:
        rms = math.sqrt(sum(x * x for x in mono) / len(mono))
        peak = max(abs(int(x)) for x in mono)
    win = max(1, int(rate * max(0.1, float(ventana_seg))))
    rms_ventanas = []
    for i in range(0, len(mono), win):
        seg = mono[i:i + win]
        if seg:
            rms_ventanas.append(math.sqrt(sum(x * x for x in seg) / len(seg)))
    silenciosas = sum(1 for v in rms_ventanas if v < silencio_rms)
    clipping = sum(1 for v in mono if abs(v) >= 32767)
    silent_pct = silenciosas / max(1, len(rms_ventanas)) * 100.0
    return {
        "duration_sec": frames / rate,
        "sample_rate": rate,
        "channels": canales,
        "sample_width_bytes": sampwidth,
        "rms": rms,
        "peak": peak,
        "rms_ventanas": rms_ventanas,
        "silent_window_pct": silent_pct,
        "clipping_pct": clipping / max(1, len(mono)) * 100.0,
        "useful_audio": pcm_tiene_senal_util(rms, peak, silencio_rms),
    }


def loopbacks_disponibles(p) -> list[dict]:
    """Lista dispositivos WASAPI loopback disponibles."""
    try:
        return list(p.get_loopback_device_info_generator())
    except Exception:
        return []


def _normalizar(s: str) -> str:
    return " ".join((s or "").lower().split())


def dispositivos_info() -> list[dict]:
    """Opciones para la UI: id estable por indice PyAudio + nombre legible."""
    out = [{"id": "", "etiqueta": "Salida predeterminada"}]
    try:
        with pyaudio.PyAudio() as p:
            for dev in loopbacks_disponibles(p):
                out.append({
                    "id": str(dev["index"]),
                    "etiqueta": f"{dev['name']} · {int(dev['defaultSampleRate'])} Hz",
                })
    except Exception as e:
        config.log_error("audio", f"listar dispositivos: {type(e).__name__}: {e}")
    return out


def dispositivo_loopback(p, preferencia: str | None = None) -> dict | None:
    """Encuentra el dispositivo loopback preferido o el de la salida por defecto."""
    preferencia = (preferencia or "").strip()
    loopbacks = loopbacks_disponibles(p)
    if preferencia:
        for lb in loopbacks:
            if str(lb.get("index")) == preferencia:
                return {**lb, "selection_reason": "explicit_index"}
        pref = _normalizar(preferencia)
        for lb in loopbacks:
            if pref and pref in _normalizar(lb.get("name", "")):
                return {**lb, "selection_reason": "explicit_name"}
        return None
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        salida = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    except OSError:
        return None
    if salida.get("isLoopbackDevice"):
        return {**salida, "selection_reason": "wasapi_default_loopback"}
    salida_nombre = _normalizar(str(salida.get("name") or ""))
    for lb in loopbacks:
        if salida_nombre and salida_nombre in _normalizar(str(lb.get("name") or "")):
            return {
                **lb,
                "selection_reason": "wasapi_default_name_match",
                "default_output_name": str(salida.get("name") or ""),
            }
    # Elegir el primer loopback podía capturar Realtek aunque la salida activa
    # fuera USB/Bluetooth. Sin correspondencia verificable se exige selección.
    return None


class SaludRutasAudio:
    """Límite de reintentos de rutas sin confundirlos con audio válido."""

    def __init__(self, *, inicio: float | None = None,
                 max_fallos_ruta: int = 3, max_alternancias: int = 3,
                 timeout_sin_audio: float | None = None):
        self.inicio = time.monotonic() if inicio is None else float(inicio)
        self.max_fallos_ruta = max(1, int(max_fallos_ruta))
        self.max_alternancias = max(1, int(max_alternancias))
        self.timeout_sin_audio = max(
            1.0,
            float(timeout_sin_audio)
            if timeout_sin_audio is not None
            else max(60.0, float(config.AUDIO_CHUNK_SEG) * 4.0),
        )
        self.intentos = 0
        self.alternancias = 0
        self.ultima_ruta = ""
        self.ultimo_resultado = ""
        self.ultimo_valido = None
        self.ultimo_transporte_valido = None
        self.fallos_ruta: dict[str, int] = {}
        self.requires_attention = False
        self.attention_reason = ""
        self.suspendido_desde: float | None = None

    def reiniciar(self, ahora: float | None = None) -> dict:
        ahora = time.monotonic() if ahora is None else float(ahora)
        self.inicio = ahora
        self.intentos = 0
        self.alternancias = 0
        self.ultima_ruta = ""
        self.ultimo_resultado = ""
        self.ultimo_valido = None
        self.ultimo_transporte_valido = None
        self.fallos_ruta.clear()
        self.requires_attention = False
        self.attention_reason = ""
        self.suspendido_desde = None
        return self.evaluar(ahora=ahora)

    def pausar(self, ahora: float | None = None) -> None:
        ahora = time.monotonic() if ahora is None else float(ahora)
        if self.suspendido_desde is None:
            self.suspendido_desde = ahora

    def reanudar(self, ahora: float | None = None) -> None:
        ahora = time.monotonic() if ahora is None else float(ahora)
        if self.suspendido_desde is None:
            return
        pausa = max(0.0, ahora - self.suspendido_desde)
        self.inicio += pausa
        if self.ultimo_valido is not None:
            self.ultimo_valido += pausa
        if self.ultimo_transporte_valido is not None:
            self.ultimo_transporte_valido += pausa
        self.suspendido_desde = None

    def registrar(self, ruta: str, resultado: str,
                  ahora: float | None = None) -> dict:
        ahora = time.monotonic() if ahora is None else float(ahora)
        self.reanudar(ahora)
        ruta = str(ruta or "desconocida")
        resultado = str(resultado or "failed")
        self.intentos += 1
        if self.ultima_ruta and ruta != self.ultima_ruta:
            self.alternancias += 1
        self.ultima_ruta = ruta
        self.ultimo_resultado = resultado
        if resultado == "valid":
            self.ultimo_valido = ahora
            self.ultimo_transporte_valido = ahora
            self.alternancias = 0
            self.fallos_ruta.clear()
            self.requires_attention = False
            self.attention_reason = ""
        elif resultado == "partial":
            self.ultimo_valido = ahora
            self.ultimo_transporte_valido = ahora
            self.fallos_ruta[ruta] = self.fallos_ruta.get(ruta, 0) + 1
        elif resultado == "silence":
            # PCM silencioso sigue demostrando que stream, buffers y reloj
            # funcionan. La auto-pausa decide la semantica del silencio; no es
            # un fallo de transporte ni debe agotar el fallback.
            self.ultimo_transporte_valido = ahora
        elif resultado in {"failed", "flat"}:
            self.fallos_ruta[ruta] = self.fallos_ruta.get(ruta, 0) + 1
        return self.evaluar(ahora=ahora)

    def evaluar(self, ahora: float | None = None) -> dict:
        ahora = time.monotonic() if ahora is None else float(ahora)
        if self.suspendido_desde is not None:
            ahora = min(ahora, self.suspendido_desde)
        sin_valido_desde = (
            self.inicio
            if self.ultimo_transporte_valido is None
            else self.ultimo_transporte_valido)
        sin_valido = max(0.0, ahora - sin_valido_desde)
        if not self.requires_attention:
            if self.alternancias >= self.max_alternancias:
                self.requires_attention = True
                self.attention_reason = "fallback_cycle_limit"
            elif any(
                    fallos >= self.max_fallos_ruta
                    for fallos in self.fallos_ruta.values()):
                self.requires_attention = True
                self.attention_reason = "route_failure_limit"
            elif sin_valido >= self.timeout_sin_audio:
                self.requires_attention = True
                self.attention_reason = "no_valid_audio_timeout"
        return {
            "requires_attention": self.requires_attention,
            "attention_reason": self.attention_reason,
            "attempt": self.intentos,
            "alternations": self.alternancias,
            "route": self.ultima_ruta,
            "last_result": self.ultimo_resultado,
            "route_failures": dict(self.fallos_ruta),
            "seconds_without_valid_audio": round(sin_valido, 3),
            "last_valid_at": self.ultimo_valido,
            "last_transport_valid_at": self.ultimo_transporte_valido,
            "max_route_failures": self.max_fallos_ruta,
            "max_alternations": self.max_alternancias,
            "no_valid_audio_timeout": self.timeout_sin_audio,
            "suspended": self.suspendido_desde is not None,
        }


class CapturaRotaTemprana(RuntimeError):
    """La sesion del navegador esta activa pero la ruta PCM entrega ceros."""

    def __init__(self, rms: float, peak: int, pico_sesion: float, ventanas: int):
        super().__init__(
            f"PCM plano durante {ventanas} ventanas con pico de sesion {pico_sesion:.3f}")
        self.rms = float(rms)
        self.peak = int(peak)
        self.pico_sesion = float(pico_sesion)
        self.ventanas = int(ventanas)


class CambioRutaCaptura(RuntimeError):
    """Finaliza un intento fallido antes de iniciar otra ruta de captura."""

    def __init__(self, pid: int, error: BaseException,
                 parcial: tuple[bytes, int, int, dict] | None = None):
        super().__init__(
            f"process-loopback pid={int(pid)}: {type(error).__name__}: {error}")
        self.pid = int(pid)
        self.error_type = type(error).__name__
        self.failure_reason = str(getattr(error, "reason", "") or (
            "wasapi_data_discontinuity"
            if isinstance(error, loopback_proceso.DiscontinuidadWasapi)
            else "process_loopback_failed"))
        self.parcial = parcial


def _es_overflow_portaudio(error: OSError) -> bool:
    codigos = [getattr(error, "errno", None)]
    codigos.extend(arg for arg in error.args if isinstance(arg, int))
    return -9981 in codigos or "overflow" in str(error).casefold()


class OverflowCapturaDispositivo(RuntimeError):
    """PortAudio perdio frames; conserva PCM previo y declara discontinuidad."""

    def __init__(self, error: OSError, parcial: tuple[bytes, int, int, dict]):
        super().__init__(f"PyAudio input overflow: {error}")
        self.error_type = type(error).__name__
        self.parcial = parcial


class WatchdogCaptura:
    """Inspecciona PCM incremental y falla rapido ante una ruta zombi."""

    def __init__(self, fuente: str, obtener_pico=None, ventana_seg: float = 1.0,
                 ventanas_rotas: int = 2):
        self.fuente = fuente
        self.obtener_pico = obtener_pico or audio_sesiones.pico_navegador
        self.ventana_seg = max(0.25, float(ventana_seg))
        self.ventanas_rotas = max(2, int(ventanas_rotas))
        self.picos: list[float] = []
        self._buffer = bytearray()
        self._consecutivas = 0

    def observar(self, data: bytes, rate: int, canales: int) -> None:
        if self.fuente not in audio_sesiones.NAVEGADORES or not data:
            return
        frame_bytes = max(2, int(canales) * 2)
        bytes_ventana = max(frame_bytes, round(
            float(rate) * self.ventana_seg) * frame_bytes)
        self._buffer.extend(data)
        while len(self._buffer) >= bytes_ventana:
            ventana = bytes(self._buffer[:bytes_ventana])
            del self._buffer[:bytes_ventana]
            pico_sesion = self.obtener_pico(self.fuente)
            if pico_sesion is None:
                self._consecutivas = 0
                continue
            pico_sesion = float(pico_sesion)
            self.picos.append(pico_sesion)
            rms = calcular_rms(ventana)
            peak = calcular_peak(ventana)
            rota = (
                pico_sesion >= PICO_SESION_ACTIVA
                and rms <= PCM_CASI_CERO_RMS
                and peak <= PCM_CASI_CERO_PEAK
            )
            self._consecutivas = self._consecutivas + 1 if rota else 0
            if self._consecutivas >= self.ventanas_rotas:
                raise CapturaRotaTemprana(
                    rms, peak, pico_sesion, self._consecutivas)


class CapturadorDispositivo:
    """Stream PyAudio persistente; cada lectura corta por frames, no por buffers."""

    def __init__(self, p, dev: dict):
        self.p = p
        self.dev = dict(dev)
        self.rate = int(dev["defaultSampleRate"])
        self.canales = max(1, int(dev["maxInputChannels"]))
        self.frame_bytes = self.canales * 2
        self.stream_id = f"device-{int(dev['index'])}-{time.monotonic_ns()}"
        self._delivered_frames = 0
        self._pendiente = bytearray()
        self._cerrado = False
        self.stream = p.open(
            format=pyaudio.paInt16,
            channels=self.canales,
            rate=self.rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=FRAMES_BUF,
        )
        self.wall_anchor = time.time()

    @property
    def clave(self) -> tuple[int, int, int]:
        return int(self.dev["index"]), self.rate, self.canales

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def leer_frames(self, frames_objetivo: int, sonda=None,
                    inspector=None, consumer=None) -> tuple[bytes, dict]:
        if self._cerrado:
            raise RuntimeError("capturador de dispositivo cerrado")
        frames_objetivo = max(0, int(frames_objetivo))
        bytes_objetivo = frames_objetivo * self.frame_bytes
        stream_start_frame = self._delivered_frames
        leidos_desde_sonda = 0
        frames_sonda = max(1, self.rate * 2)
        while len(self._pendiente) < bytes_objetivo:
            faltan_frames = max(
                1, (bytes_objetivo - len(self._pendiente)) // self.frame_bytes)
            pedir = min(FRAMES_BUF, faltan_frames)
            try:
                bloque = self.stream.read(pedir, exception_on_overflow=True)
            except OSError as error:
                if not _es_overflow_portaudio(error):
                    raise
                parcial = bytes(self._pendiente)
                frames_parcial = len(parcial) // self.frame_bytes
                self._pendiente.clear()
                continuidad = {
                    "gap_frames": 0,
                    "overlap_frames": 0,
                    "timeout_padding_frames": 0,
                    "data_discontinuity_packets": 1,
                    "terminal_discontinuity": True,
                    "portaudio_overflow": True,
                    "overflow_unknown_frames": True,
                    "device_position_start": None,
                    "device_position_end": None,
                    "stream_id": self.stream_id,
                    "start_frame": stream_start_frame,
                    "end_frame": stream_start_frame + frames_parcial,
                    "pcm_wall_start": self.wall_anchor + stream_start_frame / self.rate,
                    "pcm_wall_end": (
                        self.wall_anchor + (stream_start_frame + frames_parcial) / self.rate),
                }
                raise OverflowCapturaDispositivo(
                    error, (parcial, self.rate, self.canales, continuidad)) from error
            self._pendiente.extend(bloque)
            frames_bloque = len(bloque) // self.frame_bytes
            leidos_desde_sonda += frames_bloque
            if inspector and bloque:
                inspector(bloque, self.rate, self.canales)
            if consumer and bloque:
                consumer(bloque, self.rate, self.canales)
            if sonda and leidos_desde_sonda >= frames_sonda:
                try:
                    sonda()
                except Exception:
                    pass
                leidos_desde_sonda = 0
        resultado = bytes(self._pendiente[:bytes_objetivo])
        del self._pendiente[:bytes_objetivo]
        self._delivered_frames += frames_objetivo
        return resultado, {
            "gap_frames": 0,
            "overlap_frames": 0,
            "timeout_padding_frames": 0,
            "data_discontinuity_packets": 0,
            "device_position_start": None,
            "device_position_end": None,
            "stream_id": self.stream_id,
            "start_frame": stream_start_frame,
            "end_frame": self._delivered_frames,
            "pcm_wall_start": self.wall_anchor + stream_start_frame / self.rate,
            "pcm_wall_end": self.wall_anchor + self._delivered_frames / self.rate,
        }

    def leer(self, segundos: float, sonda=None,
             inspector=None, consumer=None) -> tuple[bytes, int, int, dict]:
        frames = round(self.rate * max(0.0, float(segundos)))
        data, continuidad = self.leer_frames(
            frames, sonda=sonda, inspector=inspector, consumer=consumer)
        return data, self.rate, self.canales, continuidad

    def close(self) -> None:
        if self._cerrado:
            return
        self._cerrado = True
        try:
            self.stream.stop_stream()
        except Exception:
            pass
        try:
            self.stream.close()
        except Exception:
            pass


def grabar(p, dev: dict, segundos: int, sonda=None) -> tuple[bytes, int, int]:
    """Wrapper compatible; el runtime mantiene ``CapturadorDispositivo`` abierto."""
    with CapturadorDispositivo(p, dev) as capturador:
        data, rate, canales, _continuidad = capturador.leer(
            segundos, sonda=sonda)
    return data, rate, canales


def guardar_wav(ruta_wav: Path, data: bytes, rate: int, canales: int,
                sampwidth: int = 2) -> None:
    tmp = ruta_wav.with_suffix(".part")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(canales)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(data)
    with open(tmp, "rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(ruta_wav)


def recuperar_wavs_huerfanos(d: Path) -> int:
    """Resuelve intentos abandonados de una clase que ya adopto el manifest."""
    if not audio_manifest.manifest_path(d).is_file():
        return 0
    conocidos = audio_manifest.latest_by_chunk(d)
    recuperados = 0
    for chunk_id, previo in sorted(
            conocidos.items(), key=lambda item: (
                int(float(item[1].get("sequence") or 0)), item[0])):
        status = str(previo.get("status") or "")
        razon_fallo = str(previo.get("reason") or "")
        recuperacion_persistencia = bool(
            status == "capture_failed"
            and razon_fallo in {
                "wav_persistence_failed",
                "wav_persistence_close_timeout",
            }
        )
        if status != "capturing" and not recuperacion_persistencia:
            continue
        wav_path = d / "audio" / str(previo.get("path") or f"{chunk_id}.wav")
        wav_info = None
        try:
            wav_info = verificar_wav_sellado(wav_path)
        except (OSError, EOFError, wave.Error):
            wav_info = None
        wav_valido = bool(
            wav_info
            and int(wav_info.get("rate") or 0) > 0
            and int(wav_info.get("channels") or 0) > 0
            and int(wav_info.get("frame_count") or 0) > 0
        )
        if wav_valido:
            recuperado = {
                **previo,
                **wav_info,
                "observed_at": time.time(),
                "status": "recovered",
                "duration": (
                    int(wav_info["frame_count"]) / max(1, int(wav_info["rate"]))),
                "inferred_legacy": False,
                "recovered_from_orphan": True,
                "transactional_stt_required": True,
                "recovery_reason": (
                    "valid_wav_after_close_timeout"
                    if razon_fallo == "wav_persistence_close_timeout"
                    else "valid_wav_after_persistence_error"
                    if recuperacion_persistencia
                    else "valid_wav_after_abandoned_capture"),
            }
            audio_manifest.append_event(d, recuperado)
            recuperados += 1
            continue
        if status == "capturing":
            fallido = {
                **previo,
                "observed_at": time.time(),
                "status": "capture_failed",
                "reason": "abandoned_capture_without_valid_wav",
                "error_type": "CaptureProcessInterrupted",
                "transactional_stt_required": True,
            }
            audio_manifest.append_event(d, fallido)
    return recuperados


def _ruta_chunk_unica(audio_dir: Path, wall_start: float, parte: int,
                       velocidad: float, reservados: set[str]) -> tuple[int, Path]:
    """Reserva un chunk sin reutilizar el intento terminal del mismo segundo."""
    epoch = int(wall_start)
    v_inicio = int(round(float(velocidad) * 10))
    while True:
        ruta = audio_dir / f"chunk_{epoch:012d}_p{int(parte)}_v{v_inicio:02d}.wav"
        if ruta.stem not in reservados and not ruta.exists():
            reservados.add(ruta.stem)
            return epoch, ruta
        epoch += 1


def verificar_wav_sellado(ruta_wav: Path) -> dict:
    """Reabre el WAV publicado y devuelve metadata/hash para el manifest."""
    with wave.open(str(ruta_wav), "rb") as wav:
        info = {
            "rate": int(wav.getframerate()),
            "channels": int(wav.getnchannels()),
            "sample_width": int(wav.getsampwidth()),
            "frame_count": int(wav.getnframes()),
        }
    hasher = hashlib.sha256()
    with open(ruta_wav, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    info["bytes"] = ruta_wav.stat().st_size
    info["sha256"] = hasher.hexdigest()
    return info


@dataclass(frozen=True)
class TrabajoPersistenciaWav:
    """Trabajo inmutable; los bytes PCM pueden capturarse mientras se persiste."""

    ruta: Path
    data: bytes
    rate: int
    canales: int
    manifest_event: Mapping[str, object]
    audio_log: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ruta", Path(self.ruta))
        object.__setattr__(self, "data", bytes(self.data))
        object.__setattr__(self, "rate", int(self.rate))
        object.__setattr__(self, "canales", int(self.canales))
        object.__setattr__(
            self, "manifest_event", MappingProxyType(dict(self.manifest_event)))
        object.__setattr__(self, "audio_log", MappingProxyType(dict(self.audio_log)))


@dataclass(frozen=True)
class ResultadoPersistenciaWav:
    trabajo: TrabajoPersistenciaWav
    wav_info: Mapping[str, object] | None = None
    error_type: str = ""
    error_message: str = ""
    failure_reason: str = ""
    manifest_published: bool = False
    manifest_published_at: float | None = None
    manifest_publication_seconds: float = 0.0
    audio_to_sealed_latency_seconds: float | None = None
    publication_error_type: str = ""
    publication_error_message: str = ""


@dataclass(frozen=True)
class _TrabajoPersistenciaEncolado:
    job_id: int
    trabajo: TrabajoPersistenciaWav
    registrado: threading.Event


class PersistidorWav:
    """Worker unico para publicar WAV sin detener el drenaje del siguiente tramo."""

    def __init__(self, capacidad: int = 3, timeout_envio: float = 1.0,
                 timeout_cierre: float = 30.0,
                 publicador: Callable[
                     [ResultadoPersistenciaWav], ResultadoPersistenciaWav
                 ] | None = None):
        self._capacidad = max(1, int(capacidad))
        self._timeout_envio = max(0.0, float(timeout_envio))
        self._timeout_cierre = max(0.0, float(timeout_cierre))
        self._publicador = publicador
        self._trabajos = queue.Queue(maxsize=self._capacidad)
        self._resultados = queue.SimpleQueue()
        self._fin = object()
        self._cerrado = False
        self._submit_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._session_close_lock = threading.Lock()
        self._session_close_result: dict | None = None
        self._cond = threading.Condition(threading.RLock())
        self._next_job_id = 0
        self._pendientes: dict[int, TrabajoPersistenciaWav] = {}
        self._activos: set[int] = set()
        self._profundidad = 0
        self._profundidad_max = 0
        self._resultados_pendientes = 0
        self._submitted = 0
        self._accepted = 0
        self._completed = 0
        self._failed = 0
        self._timed_out = 0
        self._enqueue_wait_count = 0
        self._enqueue_wait_seconds = 0.0
        self._enqueue_timeout_count = 0
        self._persistence_seconds_last = 0.0
        self._persistence_seconds_max = 0.0
        self._persistence_seconds_total = 0.0
        self._publication_seconds_last = 0.0
        self._publication_seconds_max = 0.0
        self._publication_seconds_total = 0.0
        self._published_immediate = 0
        self._publication_failed = 0
        self._sealed_latency_seconds_last = 0.0
        self._sealed_latency_seconds_max = 0.0
        self._sealed_latency_seconds_total = 0.0
        self._close_timed_out = False
        self._unresolved_at_close = 0
        self._drain_timed_out = False
        self._drain_timeout_count = 0
        self._drain_wait_count = 0
        self._drain_wait_seconds = 0.0
        self._drain_unresolved_last = 0
        self._thread = threading.Thread(
            target=self._ejecutar,
            name="clase-viva-wav-writer",
            daemon=True,
        )
        self._thread.start()

    def metricas(self) -> dict:
        with self._cond:
            return {
                "queue_capacity": self._capacidad,
                "queue_depth": self._profundidad,
                "queue_depth_max": self._profundidad_max,
                "enqueue_wait_count": self._enqueue_wait_count,
                "enqueue_wait_seconds": round(self._enqueue_wait_seconds, 6),
                "enqueue_timeout_count": self._enqueue_timeout_count,
                "persistence_seconds_last": round(
                    self._persistence_seconds_last, 6),
                "persistence_seconds_max": round(
                    self._persistence_seconds_max, 6),
                "persistence_seconds_total": round(
                    self._persistence_seconds_total, 6),
                "publication_seconds_last": round(
                    self._publication_seconds_last, 6),
                "publication_seconds_max": round(
                    self._publication_seconds_max, 6),
                "publication_seconds_total": round(
                    self._publication_seconds_total, 6),
                "published_immediate": self._published_immediate,
                "publication_failed": self._publication_failed,
                "sealed_latency_seconds_last": round(
                    self._sealed_latency_seconds_last, 6),
                "sealed_latency_seconds_max": round(
                    self._sealed_latency_seconds_max, 6),
                "sealed_latency_seconds_total": round(
                    self._sealed_latency_seconds_total, 6),
                "active_jobs": len(self._activos),
                "pending_jobs": len(self._pendientes),
                "results_pending": self._resultados_pendientes,
                "submitted": self._submitted,
                "accepted": self._accepted,
                "completed": self._completed,
                "failed": self._failed,
                "timed_out": self._timed_out,
                "closed": self._cerrado,
                "close_timed_out": self._close_timed_out,
                "unresolved_at_close": self._unresolved_at_close,
                "drain_timed_out": self._drain_timed_out,
                "drain_timeout_count": self._drain_timeout_count,
                "drain_wait_count": self._drain_wait_count,
                "drain_wait_seconds": round(self._drain_wait_seconds, 6),
                "drain_unresolved_last": self._drain_unresolved_last,
                "worker_alive": self._thread.is_alive(),
            }

    def enviar(self, trabajo: TrabajoPersistenciaWav) -> bool:
        inicio = time.monotonic()
        with self._submit_lock:
            if self._cerrado:
                raise RuntimeError("persistidor WAV cerrado")
            with self._cond:
                self._next_job_id += 1
                job_id = self._next_job_id
                self._submitted += 1
            registrado = threading.Event()
            encolado = _TrabajoPersistenciaEncolado(job_id, trabajo, registrado)
            esperaba = self._trabajos.full()
            try:
                if self._timeout_envio <= 0:
                    self._trabajos.put_nowait(encolado)
                else:
                    self._trabajos.put(encolado, timeout=self._timeout_envio)
            except queue.Full:
                espera = max(0.0, time.monotonic() - inicio)
                resultado = ResultadoPersistenciaWav(
                    trabajo,
                    error_type="PersistenciaWavQueueTimeout",
                    error_message=(
                        "cola de persistencia WAV llena; trabajo no aceptado"),
                    failure_reason="wav_persistence_queue_timeout",
                )
                resultado = self._publicar_inmediato(resultado)
                with self._cond:
                    self._enqueue_wait_count += 1
                    self._enqueue_wait_seconds += espera
                    self._enqueue_timeout_count += 1
                    self._failed += 1
                    self._timed_out += 1
                    self._resultados.put(resultado)
                    self._resultados_pendientes += 1
                    self._cond.notify_all()
                return False

            espera = max(0.0, time.monotonic() - inicio)
            with self._cond:
                self._pendientes[job_id] = trabajo
                self._profundidad += 1
                self._profundidad_max = max(
                    self._profundidad_max, self._profundidad)
                self._accepted += 1
                if esperaba or espera >= 0.001:
                    self._enqueue_wait_count += 1
                    self._enqueue_wait_seconds += espera
                registrado.set()
                self._cond.notify_all()
            return True

    def _publicar_inmediato(
            self, resultado: ResultadoPersistenciaWav
    ) -> ResultadoPersistenciaWav:
        """Completa el evento terminal en el mismo worker que selló el WAV."""
        if self._publicador is None or resultado.manifest_published:
            return resultado
        inicio = time.monotonic()
        try:
            publicado = self._publicador(resultado)
            if not isinstance(publicado, ResultadoPersistenciaWav):
                raise TypeError("publicador WAV no devolvió ResultadoPersistenciaWav")
            resultado = publicado
        except Exception as error:
            resultado = replace(
                resultado,
                publication_error_type=type(error).__name__,
                publication_error_message=str(error),
            )
        duracion = max(0.0, time.monotonic() - inicio)
        if resultado.manifest_publication_seconds <= 0:
            resultado = replace(
                resultado, manifest_publication_seconds=duracion)
        with self._cond:
            self._publication_seconds_last = duracion
            self._publication_seconds_max = max(
                self._publication_seconds_max, duracion)
            self._publication_seconds_total += duracion
            if resultado.manifest_published:
                self._published_immediate += 1
                latencia = resultado.audio_to_sealed_latency_seconds
                if latencia is not None:
                    latencia = max(0.0, float(latencia))
                    self._sealed_latency_seconds_last = latencia
                    self._sealed_latency_seconds_max = max(
                        self._sealed_latency_seconds_max, latencia)
                    self._sealed_latency_seconds_total += latencia
            else:
                self._publication_failed += 1
        return resultado

    def _ejecutar(self) -> None:
        while True:
            encolado = self._trabajos.get()
            try:
                if encolado is self._fin:
                    return
                encolado.registrado.wait()
                job_id = encolado.job_id
                trabajo = encolado.trabajo
                with self._cond:
                    self._profundidad = max(0, self._profundidad - 1)
                    if job_id not in self._pendientes:
                        self._cond.notify_all()
                        continue
                    self._activos.add(job_id)
                    self._cond.notify_all()
                inicio_persistencia = time.monotonic()
                try:
                    guardar_wav(
                        trabajo.ruta,
                        trabajo.data,
                        trabajo.rate,
                        trabajo.canales,
                    )
                    info = verificar_wav_sellado(trabajo.ruta)
                    esperado = trabajo.manifest_event
                    if (
                        int(info["frame_count"]) != int(esperado["frame_count"])
                        or int(info["rate"]) != trabajo.rate
                        or int(info["channels"]) != trabajo.canales
                        or int(info["sample_width"]) != 2
                    ):
                        raise RuntimeError(
                            "WAV sellado no coincide con el PCM capturado")
                    resultado = ResultadoPersistenciaWav(
                        trabajo,
                        wav_info=MappingProxyType(dict(info)),
                    )
                except Exception as error:
                    resultado = ResultadoPersistenciaWav(
                        trabajo,
                        error_type=type(error).__name__,
                        error_message=str(error),
                        failure_reason="wav_persistence_failed",
                    )
                duracion = max(0.0, time.monotonic() - inicio_persistencia)
                resultado = self._publicar_inmediato(resultado)
                with self._cond:
                    self._activos.discard(job_id)
                    if self._pendientes.pop(job_id, None) is not None:
                        self._completed += 1
                        if resultado.error_type:
                            self._failed += 1
                        self._persistence_seconds_last = duracion
                        self._persistence_seconds_max = max(
                            self._persistence_seconds_max, duracion)
                        self._persistence_seconds_total += duracion
                        self._resultados.put(resultado)
                        self._resultados_pendientes += 1
                    self._cond.notify_all()
            finally:
                self._trabajos.task_done()

    def recolectar(self) -> list[ResultadoPersistenciaWav]:
        resultados = []
        while True:
            try:
                resultados.append(self._resultados.get_nowait())
                with self._cond:
                    self._resultados_pendientes = max(
                        0, self._resultados_pendientes - 1)
            except queue.Empty:
                return resultados

    def vaciar(self, timeout: float | None = None) -> list[ResultadoPersistenciaWav]:
        espera_maxima = (
            self._timeout_cierre if timeout is None else max(0.0, float(timeout)))
        inicio = time.monotonic()
        limite = inicio + espera_maxima
        with self._cond:
            tenia_pendientes = bool(self._pendientes)
            self._drain_timed_out = False
            while self._pendientes:
                restante = limite - time.monotonic()
                if restante <= 0:
                    break
                self._cond.wait(restante)
            espera = max(0.0, time.monotonic() - inicio)
            if tenia_pendientes:
                self._drain_wait_count += 1
                self._drain_wait_seconds += espera
            self._drain_unresolved_last = len(self._pendientes)
            if self._pendientes:
                self._drain_timed_out = True
                self._drain_timeout_count += 1
        return self.recolectar()

    def _insertar_fin_tras_timeout(self) -> None:
        """Cancela cola ya terminalizada y garantiza que el worker pueda salir."""
        while True:
            try:
                self._trabajos.put_nowait(self._fin)
                return
            except queue.Full:
                try:
                    descartado = self._trabajos.get_nowait()
                except queue.Empty:
                    continue
                self._trabajos.task_done()
                if descartado is self._fin:
                    return
                with self._cond:
                    self._profundidad = max(0, self._profundidad - 1)
                    self._cond.notify_all()

    def cerrar(self) -> list[ResultadoPersistenciaWav]:
        with self._close_lock:
            return self._cerrar_bajo_lock()

    def _cerrar_bajo_lock(self) -> list[ResultadoPersistenciaWav]:
        with self._submit_lock:
            if self._cerrado:
                return self.recolectar()
            self._cerrado = True
        limite = time.monotonic() + self._timeout_cierre
        with self._cond:
            while self._pendientes:
                restante = limite - time.monotonic()
                if restante <= 0:
                    break
                self._cond.wait(restante)
            if self._pendientes:
                self._unresolved_at_close = len(self._pendientes)
                self._close_timed_out = True
                # El timeout es telemetría, no un resultado terminal inventado.
                # Un trabajo activo puede estar escribiendo todavía; publicarlo como
                # fallido permitiría que aparezca un WAV válido después del evento de
                # cierre. Mantenemos la barrera abierta hasta que cada trabajo emita su
                # resultado real. El coordinador externo de shutdown conserva la
                # responsabilidad de escalar si el proceso completo no coopera.
                self._cond.notify_all()
                while self._pendientes:
                    self._cond.wait()
        self._trabajos.put(self._fin)
        self._thread.join()
        return self.recolectar()


def publicar_resultado_persistencia(
        d: Path,
        resultado: ResultadoPersistenciaWav,
        registrar=None,
        clock: Callable[[], float] = time.time,
) -> ResultadoPersistenciaWav:
    """Publica una sola transición terminal durable e idempotente en memoria."""
    if resultado.manifest_published:
        return resultado
    registrar = registrar or (lambda *_args, **_kwargs: None)
    trabajo = resultado.trabajo
    evento = dict(trabajo.manifest_event)
    publicado_at = float(clock())
    inicio_publicacion = time.monotonic()
    if resultado.error_type:
        evento.update({
            "status": "capture_failed",
            "reason": resultado.failure_reason or "wav_persistence_failed",
            "error_type": resultado.error_type,
            "error_message": resultado.error_message,
            "observed_at": publicado_at,
            "persistence_completed_at": publicado_at,
        })
        audio_manifest.append_event(d, evento)
        try:
            registrar(
                "persistencia_wav_fallida",
                chunk_id=evento.get("chunk_id"),
                archivo=trabajo.ruta.name,
                error_type=resultado.error_type,
            )
        except Exception:
            # El log auxiliar no revierte el evento terminal ya fsync-eado.
            pass
        return replace(
            resultado,
            manifest_published=True,
            manifest_published_at=publicado_at,
            manifest_publication_seconds=max(
                0.0, time.monotonic() - inicio_publicacion),
            publication_error_type="",
            publication_error_message="",
        )
    info = dict(resultado.wav_info or {})
    source_wall_end = evento.get("pcm_wall_end", evento.get("wall_end"))
    try:
        sealed_latency = max(0.0, publicado_at - float(source_wall_end)) \
            if source_wall_end is not None else None
    except (TypeError, ValueError):
        sealed_latency = None
    evento.update({
        "status": "sealed",
        "bytes": info["bytes"],
        "sha256": info["sha256"],
        "observed_at": publicado_at,
        "persistence_completed_at": publicado_at,
        "sealed_at": publicado_at,
        "audio_to_sealed_latency_seconds": (
            round(sealed_latency, 6) if sealed_latency is not None else None),
    })
    audio_manifest.append_event(d, evento)
    try:
        registrar("guardado", **dict(trabajo.audio_log))
    except Exception:
        # audio_log es telemetría secundaria; el manifest durable prevalece.
        pass
    return replace(
        resultado,
        manifest_published=True,
        manifest_published_at=publicado_at,
        manifest_publication_seconds=max(
            0.0, time.monotonic() - inicio_publicacion),
        audio_to_sealed_latency_seconds=sealed_latency,
        publication_error_type="",
        publication_error_message="",
    )


def publicar_resultados_persistencia(
        d: Path,
        resultados: list[ResultadoPersistenciaWav],
        registrar=None) -> list[ResultadoPersistenciaWav]:
    """Publica solo resultados aún pendientes; evita duplicar el callback inmediato."""
    return [
        publicar_resultado_persistencia(d, resultado, registrar)
        for resultado in resultados
    ]


def cerrar_sesion_captura(
        d: Path, persistidor: PersistidorWav, capture_session_id: str,
        registrar=None, reason: str = "fin") -> dict:
    """Drena persistencias, recupera WAV validos y solo entonces publica cierre."""
    with persistidor._session_close_lock:
        if persistidor._session_close_result is not None:
            return dict(persistidor._session_close_result)
        resultados = publicar_resultados_persistencia(
            d, persistidor.cerrar(), registrar)
        metricas_persistencia = persistidor.metricas()
        if (d / "session_state.json").is_file():
            try:
                est.actualizar_estado(d, wav_persistence=metricas_persistencia)
            except Exception:
                pass
        cierre_incompleto = bool(
            metricas_persistencia["pending_jobs"]
            or metricas_persistencia["active_jobs"]
            or metricas_persistencia["queue_depth"]
            or metricas_persistencia["results_pending"]
            or metricas_persistencia["worker_alive"]
            or metricas_persistencia["completed"]
            != metricas_persistencia["accepted"]
        )
        if cierre_incompleto:
            raise RuntimeError(
                "persistencia WAV no resolvió todos los trabajos; "
                "capture_session_closed no fue publicado")
        recuperados = recuperar_wavs_huerfanos(d)
        unfinished = audio_manifest.unfinished_capture_report(d)
        if unfinished["count"]:
            raise RuntimeError(
                "quedan capturas sin estado terminal; "
                "capture_session_closed no fue publicado")
        failures = audio_manifest.capture_failure_report(d)
        cierre = audio_manifest.append_capture_session_event(
            d,
            audio_manifest.CAPTURE_SESSION_CLOSED,
            capture_session_id,
            reason=str(reason or "fin"),
            persistence_results=len(resultados),
            recovered_before_close=recuperados,
            unfinished_before_close=unfinished["terminal_count"],
            terminal_capture_failures=failures["terminal_count"],
            persistence_metrics=metricas_persistencia,
        )
        persistidor._session_close_result = dict(cierre)
        return cierre


def modo_test(segundos: int) -> int:
    config.ensure_dirs()
    destino = config.LOGS_DIR / "test_audio.wav"
    with pyaudio.PyAudio() as p:
        dev = dispositivo_loopback(p)
        if dev is None:
            print("TEST_FALLO: no se encontró dispositivo loopback WASAPI. "
                  "Revisa que haya un dispositivo de salida activo (parlantes/audífonos).")
            return 1
        print(f"[test] grabando {segundos}s del dispositivo: {dev['name']} "
              f"({int(dev['defaultSampleRate'])} Hz). REPRODUCE ALGO DE AUDIO AHORA...")
        data, rate, ch = grabar(p, dev, segundos)
        guardar_wav(destino, data, rate, ch)
        rms = calcular_rms(data)
    kb = destino.stat().st_size // 1024
    print(f"TEST_OK archivo={destino} tamano={kb}KB rms={rms:.0f} "
          f"(rms > {config.SILENCIO_RMS} = se oye audio; menor = silencio)")
    return 0


def main() -> int:
    config.stdout_utf8()
    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        seg = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 10
        return modo_test(seg)

    if not config.instancia_unica("captura_audio"):
        print("[audio] ya hay otro capturador vivo (candado). Saliendo para no duplicar.")
        return 0
    d = est.clase_activa()
    if d is None:
        print("[audio] no hay clase activa (usa iniciar_clase.py). Saliendo.")
        return 1
    audio_dir = d / "audio"
    # Una clase legacy sin manifest conserva su contrato anterior. Solo se
    # recuperan huerfanos cuando el manifest ya existia antes de este arranque.
    recuperar_wavs_huerfanos(d)
    capture_session_id = f"capture-{os.getpid()}-{time.time_ns()}"
    # El preview PCM/STT fue retirado: el capturador solo produce WAV canónicos.
    preview_publisher = None
    preview_enabled = False
    preview_reason = "feature_removed"
    try:
        est.actualizar_estado(d, stt_provisional_capture={
            "enabled": preview_publisher is not None,
            "requested": preview_enabled,
            "status": preview_reason,
            "runtime_ephemeral": True,
            "canonical_audio_dropped_seconds": 0.0,
        })
    except Exception:
        pass
    audio_manifest.append_capture_session_event(
        d,
        audio_manifest.CAPTURE_SESSION_STARTED,
        capture_session_id,
        pid=os.getpid(),
    )
    conocidos = audio_manifest.latest_by_chunk(d)
    chunk_ids_reservados = set(conocidos)
    secuencia = audio_manifest.next_sequence(d)
    print(f"[audio] chunks de {config.AUDIO_CHUNK_SEG}s -> {audio_dir} | "
          f"auto-pausa tras {config.SILENCIO_MIN} min de silencio "
          f"(rms<{config.SILENCIO_RMS}); visual-idle={config.VISUAL_IDLE_AUTOPAUSA}")

    silencio_seguidos = 0
    modo_actual = ""
    audio_diag_actual = ("", "")
    proceso_bloqueado_hasta = 0.0
    pid_bloqueados: dict[int, float] = {}
    capturador_proceso = None
    pid_capturador = None
    p_audio = None
    capturador_dispositivo = None
    salud_rutas = SaludRutasAudio()
    try:
        retry_generation = int(est.leer_estado(d).get("audio_retry_generation") or 0)
    except (TypeError, ValueError):
        retry_generation = 0
    wavs_validos = len(audio_manifest.capture_records(d))

    def preview_wall_start(capturador, fallback: float) -> float:
        anchor = getattr(capturador, "wall_anchor", None)
        delivered = getattr(capturador, "_delivered_frames", 0)
        rate = max(1, int(getattr(capturador, "rate", 1)))
        if anchor is None:
            return float(fallback)
        return float(anchor) + max(0, int(delivered)) / rate

    def preview_runtime_allowed(estado_chunk: dict) -> bool:
        runtime = estado_chunk.get("stt_provisional")
        if not isinstance(runtime, dict):
            return True
        status = str(runtime.get("status") or "")
        return not (
            status.startswith("disabled_")
            or status.startswith("runtime_error")
            or status == "missing_runtime_dir"
        )

    def iniciar_preview_chunk(capturador, estado_chunk: dict, chunk: str) -> object:
        return None

    def terminar_preview_chunk(*, valid: bool, reason: str) -> None:
        return None

    def cerrar_proceso() -> None:
        nonlocal capturador_proceso, pid_capturador
        if capturador_proceso is not None:
            capturador_proceso.close()
        capturador_proceso = None
        pid_capturador = None

    def cerrar_dispositivo() -> None:
        nonlocal capturador_dispositivo, p_audio
        if capturador_dispositivo is not None:
            capturador_dispositivo.close()
        capturador_dispositivo = None
        if p_audio is not None:
            try:
                p_audio.terminate()
            except Exception:
                pass
        p_audio = None

    def cerrar_capturadores() -> None:
        cerrar_proceso()
        cerrar_dispositivo()

    def registrar_audio(evento: str, **campos) -> None:
        obj = {"t": time.time(), "evento": evento}
        obj.update({k: v for k, v in campos.items() if v is not None})
        try:
            est.anexar_jsonl(d / "audio_log.jsonl", obj)
        except Exception:
            pass

    def registrar_salud_ruta(
            ruta: str, resultado: str, *, modo: str = "",
            dispositivo: dict | None = None, pid: int | None = None,
            session_pid: int | None = None, stream_id: str = "",
            rms: float | None = None, peak: int | None = None,
            session_peak: float | None = None,
            transition_cause: str = "") -> dict:
        instante_salud = time.time()
        snapshot = salud_rutas.registrar(ruta, resultado)
        diagnostico = {
            **snapshot,
            "mode": str(modo or ""),
            "device_id": (
                str(dispositivo.get("index")) if dispositivo else ""),
            "device_name": (
                str(dispositivo.get("name") or "") if dispositivo else ""),
            "device_selection": (
                str(dispositivo.get("selection_reason") or "")
                if dispositivo else ""),
            "root_pid": int(pid) if pid else None,
            "session_pid": int(session_pid) if session_pid else None,
            "stream_id": str(stream_id or ""),
            "rms": round(float(rms), 1) if rms is not None else None,
            "peak": int(peak) if peak is not None else None,
            "session_peak": (
                round(float(session_peak), 3)
                if session_peak is not None else None),
            "transition_cause": str(transition_cause or resultado),
            "valid_wav_count": wavs_validos,
        }
        campos_estado = {
            "audio_route_health": diagnostico,
            "audio_requires_attention": bool(snapshot["requires_attention"]),
            "audio_attention_reason": str(snapshot["attention_reason"] or ""),
            "audio_fallback_transition": str(transition_cause or resultado),
            "audio_capture_phase": {
                "valid": "active",
                "silence": "silent",
                "partial": "trying_fallback",
                "flat": "trying_fallback",
                "failed": "trying_fallback",
            }.get(resultado, "signal_analysis"),
            "audio_capture_phase_started_at": instante_salud,
        }
        if resultado in {"valid", "partial"}:
            campos_estado["audio_ultimo_pcm_valido_t"] = instante_salud
        if snapshot["requires_attention"]:
            campos_estado.update({
                # La captura de audio espera una acción, pero la evidencia visual
                # debe seguir activa y conservadora.
                "auto_pausa": False,
                "auto_pausa_motivo": "",
                "audio_ultimo_estado": "requiere_atencion",
                "audio_warning": "La captura de audio requiere atención",
                "audio_diag": (
                    "Se agotó el presupuesto de rutas sin obtener audio válido; "
                    "usa Reanudar / probar audio tras verificar la salida."),
                "audio_capture_phase": "requires_attention",
            })
        try:
            if snapshot["requires_attention"]:
                est.transicionar_captura(
                    d,
                    "audio_atencion",
                    ahora=instante_salud,
                    nota=str(snapshot["attention_reason"] or transition_cause),
                    actualizaciones=campos_estado,
                )
            else:
                est.actualizar_estado(d, **campos_estado)
        except Exception:
            pass
        return diagnostico

    persistidor = PersistidorWav(publicador=lambda resultado: (
        publicar_resultado_persistencia(d, resultado, registrar_audio)))

    def reportar_metricas_persistencia() -> dict:
        metricas = persistidor.metricas()
        try:
            est.actualizar_estado(d, wav_persistence=metricas)
        except Exception:
            pass
        return metricas

    def publicar_resultados(
            resultados: list[ResultadoPersistenciaWav]) -> None:
        nonlocal wavs_validos
        # El worker ya publica el manifest apenas termina fsync/rename. Esta ruta
        # consume telemetría y reintenta solo un resultado que no pudo anexarse.
        resultados = publicar_resultados_persistencia(
            d, resultados, registrar_audio)
        completados = sum(
            1 for resultado in resultados
            if not resultado.error_type and resultado.manifest_published)
        if completados:
            wavs_validos += completados
            try:
                exitosos = [
                    resultado.trabajo.manifest_event
                    for resultado in resultados if not resultado.error_type]
                wall_ends = [
                    float(evento.get("pcm_wall_end", evento.get("wall_end")))
                    for evento in exitosos
                    if evento.get("pcm_wall_end", evento.get("wall_end")) is not None
                ]
                wall_starts = [
                    float(evento.get("pcm_wall_start", evento.get("wall_start")))
                    for evento in exitosos
                    if evento.get("pcm_wall_start", evento.get("wall_start")) is not None
                ]
                estado_actual = est.leer_estado(d)
                primer_wall = estado_actual.get("audio_first_valid_wall_start")
                if wall_starts:
                    primer_wall = min(
                        ([float(primer_wall)] if primer_wall is not None else [])
                        + wall_starts)
                est.actualizar_estado(
                    d,
                    audio_valid_wav_count=wavs_validos,
                    audio_ultimo_wav_valido_t=time.time(),
                    audio_first_valid_wall_start=primer_wall,
                    audio_last_valid_wall_end=(
                        max(wall_ends) if wall_ends else
                        estado_actual.get("audio_last_valid_wall_end")),
                )
            except Exception:
                pass
        reportar_metricas_persistencia()

    def enviar_persistencia(trabajo: TrabajoPersistenciaWav) -> bool:
        aceptado = persistidor.enviar(trabajo)
        metricas = reportar_metricas_persistencia()
        if not aceptado:
            registrar_audio(
                "persistencia_wav_backpressure",
                chunk_id=trabajo.manifest_event.get("chunk_id"),
                archivo=trabajo.ruta.name,
                reason="wav_persistence_queue_timeout",
                queue_depth=metricas["queue_depth"],
                pending_jobs=metricas["pending_jobs"],
            )
        return aceptado

    def sesion_navegador_disponible(fuente: str) -> tuple[dict | None, str]:
        sesiones = audio_sesiones.sesiones_navegador(fuente)
        return seleccionar_sesion_audio(sesiones, pid_bloqueados, time.time())

    def reportar_modo(modo: str) -> None:
        nonlocal modo_actual
        if modo != modo_actual:
            modo_actual = modo
            try:
                est.actualizar_estado(d, audio_modo=modo)
            except Exception:
                pass
            print(f"[audio] modo: {modo}", flush=True)

    def reportar_fase_captura(
            fase: str, *, modo: str, pid: int | None = None,
            session_pid: int | None = None, stream_id: str = "",
            dispositivo: dict | None = None, causa: str = "") -> None:
        """Publica el intento antes de entrar en una lectura bloqueante."""
        campos = {
            "audio_capture_phase": str(fase),
            "audio_capture_phase_started_at": time.time(),
            "audio_capture_attempt": int(salud_rutas.intentos) + 1,
            "audio_capture_mode": str(modo or ""),
            "audio_capture_root_pid": int(pid) if pid else None,
            "audio_capture_session_pid": (
                int(session_pid) if session_pid else None),
            "audio_capture_stream_id": str(stream_id or ""),
            "audio_capture_transition_cause": str(causa or fase),
        }
        if dispositivo:
            campos.update({
                "audio_capture_device_id": str(dispositivo.get("index") or ""),
                "audio_capture_device_name": str(dispositivo.get("name") or ""),
                "audio_capture_device_selection": str(
                    dispositivo.get("selection_reason") or ""),
            })
        try:
            est.actualizar_estado(d, **campos)
        except Exception:
            pass

    def reportar_audio_diag(warning: str = "", detalle: str = "") -> None:
        nonlocal audio_diag_actual
        warning = warning or ""
        detalle = detalle or ""
        nuevo = (warning, detalle)
        if nuevo != audio_diag_actual:
            audio_diag_actual = nuevo
            try:
                est.actualizar_estado(d, audio_warning=warning, audio_diag=detalle)
            except Exception:
                pass

    while True:
        publicar_resultados(persistidor.recolectar())
        st = est.leer_estado(d)
        efectivo = est.estado_efectivo(st)
        try:
            generacion_actual = int(st.get("audio_retry_generation") or 0)
        except (TypeError, ValueError):
            generacion_actual = retry_generation
        if generacion_actual != retry_generation:
            retry_generation = generacion_actual
            salud_rutas.reiniciar()
            est.actualizar_estado(
                d,
                audio_requires_attention=False,
                audio_attention_reason="",
                audio_route_health=salud_rutas.evaluar(),
            )
            st = est.leer_estado(d)
            efectivo = est.estado_efectivo(st)
        if not st.get("enfoque"):
            # También cubre OFF/cambio de fuente durante una pausa, cuando la
            # sonda del chunk no se ejecuta en este proceso.
            audio_sesiones.restaurar_enfoques()
        if efectivo == "finalizado" or config.latido_perdido():
            cerrar_capturadores()
            if preview_publisher is not None:
                preview_publisher.close(
                    reason="fin" if efectivo == "finalizado" else "panel_lost",
                    timeout=2.0,
                )
                try:
                    est.actualizar_estado(
                        d, stt_provisional_capture={
                            "enabled": True,
                            "requested": True,
                            "status": "stopped",
                            "runtime_ephemeral": True,
                            **preview_publisher.metrics(),
                        })
                except Exception:
                    pass
            cerrar_sesion_captura(
                d,
                persistidor,
                capture_session_id,
                registrar=registrar_audio,
                reason="fin" if efectivo == "finalizado" else "latido_perdido",
            )
            # Cada proceso restaura solo las sesiones que realmente muteó.
            audio_sesiones.restaurar_enfoques()
            print("[audio] fin de clase (o panel cerrado). Cerrando.")
            return 0
        if efectivo == "pausado_manual":
            salud_rutas.pausar()
            if preview_publisher is not None:
                preview_publisher.set_capture_active(
                    False, reason="paused_manual")
            # No conservar audio anterior a la pausa en buffers WASAPI/PyAudio.
            cerrar_capturadores()
            publicar_resultados(persistidor.vaciar())
            time.sleep(1)
            continue
        salud_rutas.reanudar()
        if preview_publisher is not None:
            preview_publisher.set_capture_active(
                efectivo == "grabando_activo" and preview_runtime_allowed(st),
                reason=("recording" if efectivo == "grabando_activo" else efectivo),
                wall_start=time.time())
        if efectivo == "audio_requires_attention":
            cerrar_capturadores()
            if preview_publisher is not None:
                preview_publisher.set_capture_active(
                    False, reason="audio_requires_attention")
            time.sleep(1)
            continue
        wall_start = time.time()
        monotonic_start_ns = time.monotonic_ns()
        epoch, ruta = _ruta_chunk_unica(
            audio_dir,
            wall_start,
            int(st.get("parte", 1)),
            float(st.get("velocidad", 1.0)),
            chunk_ids_reservados,
        )
        chunk_id = ruta.stem
        secuencia_intento = secuencia
        secuencia += 1

        def manifestar(status: str, publicar: bool = True, **campos) -> dict:
            base = {
                "chunk_id": chunk_id,
                "sequence": secuencia_intento,
                "status": status,
                "path": ruta.name,
                "part": int(st.get("parte", 1)),
                "speed": float(st.get("velocidad", 1.0)),
                "wall_start": wall_start,
                "monotonic_start_ns": monotonic_start_ns,
                "requested_duration": float(config.AUDIO_CHUNK_SEG),
                "transactional_stt_required": True,
            }
            if status in {
                    "sealed", "recovered", "discarded_silence", "capture_failed"}:
                if campos.get("pcm_wall_start") is not None:
                    base["wall_start"] = float(campos["pcm_wall_start"])
                if campos.get("pcm_wall_end") is not None:
                    campos["wall_end"] = float(campos["pcm_wall_end"])
            base.update({k: v for k, v in campos.items() if v is not None})
            if publicar:
                audio_manifest.append_event(d, base)
            return base

        try:
            manifestar("capturing")
            fuente = st.get("fuente", "sistema")
            picos: list[float] = []
            data = None
            rate = ch = None
            continuidad = {}
            modo_chunk = "sistema"
            pid_chunk = None
            sesion_pid_chunk = None
            dev = None
            watchdog = WatchdogCaptura(fuente)

            def sonda():
                # El watchdog ya toma una muestra fresca por segundo y conserva
                # los picos. Repetir aqui GetAllSessions cada dos segundos no
                # aportaba ninguna decision y duplicaba trabajo COM durante LIVE.
                estado_sonda = est.leer_estado(d)
                if preview_publisher is not None:
                    efectivo_sonda = est.estado_efectivo(estado_sonda)
                    preview_publisher.set_capture_active(
                        efectivo_sonda == "grabando_activo"
                        and preview_runtime_allowed(estado_sonda),
                        reason=(
                            "fin" if efectivo_sonda == "finalizado"
                            else "paused_manual"
                            if efectivo_sonda == "pausado_manual"
                            else efectivo_sonda),
                        wall_start=time.time(),
                    )
                if estado_sonda.get("enfoque"):
                    audio_sesiones.enfoque(fuente, True)  # vigilante continuo
                else:
                    audio_sesiones.restaurar_enfoques()

            if (fuente in audio_sesiones.NAVEGADORES and
                    audio_sesiones.disponible() and
                    loopback_proceso.disponible() and
                    time.time() >= proceso_bloqueado_hasta and
                    os.environ.get("CLASE_LOOPBACK_PROCESO", "1") != "0"):
                sesion_audio, razon_sesion = sesion_navegador_disponible(fuente)
                if sesion_audio:
                    pid = int(sesion_audio.get("root_pid") or sesion_audio["pid"])
                    sesion_pid = int(sesion_audio.get("pid") or pid)
                    pid_chunk = pid
                    sesion_pid_chunk = sesion_pid
                    sufijo = f", sesion={sesion_pid}" if sesion_pid != pid else ""
                    modo_chunk = f"{fuente}:proceso(pid={pid}{sufijo})"
                    try:
                        if capturador_proceso is None or pid_capturador != pid:
                            cerrar_proceso()
                            capturador_proceso = loopback_proceso.CapturadorProceso(pid)
                            pid_capturador = pid
                        cerrar_dispositivo()
                        reportar_modo(modo_chunk)
                        reportar_fase_captura(
                            "reading_process",
                            modo=modo_chunk,
                            pid=pid,
                            session_pid=sesion_pid,
                            stream_id=str(getattr(
                                capturador_proceso, "stream_id", "")),
                            causa="process_loopback_selected",
                        )
                        preview_consumer = iniciar_preview_chunk(
                            capturador_proceso, st, chunk_id)
                        data, rate, ch, continuidad = capturador_proceso.leer(
                            config.AUDIO_CHUNK_SEG,
                            sonda=sonda,
                            inspector=watchdog.observar,
                            consumer=preview_consumer,
                        )
                    except CapturaRotaTemprana:
                        raise
                    except Exception as e:
                        parcial = None
                        extraer_parcial = getattr(
                            capturador_proceso, "extraer_parcial", None)
                        if callable(extraer_parcial):
                            try:
                                parcial = extraer_parcial()
                            except Exception as parcial_error:
                                config.log_error(
                                    "audio",
                                    "no se pudo extraer PCM parcial: "
                                    f"{type(parcial_error).__name__}: {parcial_error}",
                                )
                        cerrar_proceso()
                        proceso_bloqueado_hasta = time.time() + 90
                        config.log_error("audio", f"process-loopback fallo pid={pid}: "
                                                  f"{type(e).__name__}: {e}")
                        print("[audio] process-loopback fallo; cerrando intento",
                              flush=True)
                        try:
                            if not est.leer_estado(d).get("enfoque"):
                                n = audio_sesiones.enfoque(fuente, True)
                                est.actualizar_estado(d, enfoque=True)
                                print(f"[audio] fallback: enfoque ON ({n} apps)", flush=True)
                        except Exception as enfoque_error:
                            config.log_error(
                                "audio",
                                "no se pudo preparar enfoque para fallback: "
                                f"{type(enfoque_error).__name__}: {enfoque_error}",
                            )
                        raise CambioRutaCaptura(pid, e, parcial) from e
                else:
                    cerrar_proceso()
                    if razon_sesion == "pid_bloqueado":
                        warning = (f"{nombre_fuente_audio(fuente)} exclusivo por PID "
                                   "bloqueado; usando salida del sistema")
                        detalle = ("El ultimo PID exclusivo entrego PCM casi plano. "
                                   "Durante 90s se evita reusarlo y se prueba el "
                                   "fallback de salida del sistema.")
                        if not st.get("enfoque"):
                            n = audio_sesiones.enfoque(fuente, True)
                            est.actualizar_estado(d, enfoque=True)
                            print(f"[audio] fallback por PID bloqueado: enfoque ON "
                                  f"({n} apps)", flush=True)
                        reportar_audio_diag(warning, detalle)
                        modo_chunk = f"{fuente}:fallback(pid_bloqueado)"
                    else:
                        modo_chunk = f"{fuente}:sin_sesion_audio"

            if data is None:
                # Si la ruta exclusiva no se usara en este corte, no dejarla
                # corriendo mientras el fallback acumula 90 s de buffers.
                cerrar_proceso()
                if p_audio is None:
                    p_audio = pyaudio.PyAudio()
                dev = dispositivo_loopback(p_audio, st.get("dispositivo"))
                if dev is None:
                    try:
                        est.actualizar_estado(
                            d,
                            audio_dispositivo_seleccion=(
                                "explicit_unmatched"
                                if st.get("dispositivo") else "wasapi_default_unmatched"),
                        )
                    except Exception:
                        pass
                    raise OSError("dispositivo loopback no encontrado")
                clave_dev = (
                    int(dev["index"]),
                    int(dev["defaultSampleRate"]),
                    max(1, int(dev["maxInputChannels"])),
                )
                if (capturador_dispositivo is None
                        or capturador_dispositivo.clave != clave_dev):
                    if capturador_dispositivo is not None:
                        capturador_dispositivo.close()
                    capturador_dispositivo = CapturadorDispositivo(p_audio, dev)
                modo_chunk = f"{fuente}:dispositivo({dev['name']})"
                try:
                    est.actualizar_estado(
                        d,
                        audio_dispositivo_id=str(dev.get("index")),
                        audio_dispositivo_nombre=str(dev.get("name") or ""),
                        audio_dispositivo_seleccion=str(
                            dev.get("selection_reason") or ""),
                    )
                except Exception:
                    pass
                reportar_modo(modo_chunk)
                reportar_fase_captura(
                    "reading_system" if fuente == "sistema"
                    else "trying_device_fallback",
                    modo=modo_chunk,
                    stream_id=str(getattr(
                        capturador_dispositivo, "stream_id", "")),
                    dispositivo=dev,
                    causa=(
                        "system_output_selected" if fuente == "sistema"
                        else "browser_route_fallback"),
                )
                preview_consumer = iniciar_preview_chunk(
                    capturador_dispositivo, st, chunk_id)
                data, rate, ch, continuidad = capturador_dispositivo.leer(
                    config.AUDIO_CHUNK_SEG,
                    sonda if (
                        fuente in audio_sesiones.NAVEGADORES
                        or preview_publisher is not None) else None,
                    inspector=watchdog.observar,
                    consumer=preview_consumer,
                )

            picos.extend(watchdog.picos)

            reportar_modo(modo_chunk)
            rms = calcular_rms(data)
            peak = calcular_peak(data)
            pico_nav = max(picos) if picos else None
            if pico_nav is None and fuente in audio_sesiones.NAVEGADORES:
                try:
                    pico_nav = audio_sesiones.pico_navegador(fuente)
                except Exception:
                    pico_nav = None
            try:
                est.actualizar_estado(
                    d,
                    audio_ultimo_modo=modo_chunk,
                    audio_ultimo_rms=round(float(rms), 1),
                    audio_ultimo_peak=int(peak),
                    audio_ultimo_pico_sesion=(
                        round(float(pico_nav), 3) if pico_nav is not None else None),
                    audio_ultimo_pid=int(pid_chunk) if pid_chunk else None,
                )
            except Exception:
                pass
            st_final = est.leer_estado(d)  # pudo cambiar durante el corte

            clasif = clasificar_muestra_audio(
                fuente, rms, peak, pico_nav, config.SILENCIO_RMS)
            es_silencio = bool(clasif["es_silencio"])
            # ANTI-ZOMBI: si el NAVEGADOR suena (pico alto) pero capturamos ceros,
            # la captura está rota (p. ej. FreeBuds Bluetooth reconectándose), NO es
            # un break: avisar y NO caer/quedarse en auto-pausa.
            captura_rota = bool(clasif["captura_rota"])
            ruta_actual = "proceso" if ":proceso" in modo_chunk else "dispositivo"
            stream_id_actual = str(continuidad.get("stream_id") or "")
            estado_senal = (
                "captura_rota" if captura_rota
                else "silencio" if es_silencio
                else "activo"
            )
            try:
                est.actualizar_estado(
                    d,
                    audio_ultimo_estado=estado_senal,
                    audio_ultimo_t=time.time(),
                    audio_silencio_chunks=(
                        0 if captura_rota or not es_silencio
                        else silencio_seguidos + 1),
                )
            except Exception:
                pass
            if captura_rota:
                terminar_preview_chunk(valid=False, reason="capture_flat")
                salud = registrar_salud_ruta(
                    ruta_actual, "flat", modo=modo_chunk, dispositivo=dev,
                    pid=pid_chunk, session_pid=sesion_pid_chunk,
                    stream_id=stream_id_actual, rms=rms, peak=peak,
                    session_peak=pico_nav, transition_cause="pcm_flat")
                warning = (f"{nombre_fuente_audio(fuente)} suena, pero ClaseViva "
                           "captura audio casi mudo")
                detalle = (f"PCM casi plano (rms={rms:.1f}, peak={peak}, "
                           f"pico={pico_nav:.2f}, modo={modo_chunk}). No se guarda "
                           "este WAV; se reintentara otra ruta de captura.")
                config.log_error("audio", f"CAPTURA CASI MUDA con navegador sonando "
                                          f"(rms={rms:.1f}, peak={peak}, "
                                          f"pico={pico_nav:.2f}, modo={modo_chunk}): "
                                          "reintentando")
                config.log_error("audio", f"CAPTURA EN CERO con navegador sonando "
                                          f"(pico={pico_nav:.2f}, modo={modo_chunk}): reintentando")
                print("[audio] ⚠️ captura en cero con la clase sonando; reintentando...")
                if ":proceso" in modo_chunk:
                    if pid_chunk:
                        pid_bloqueados[int(pid_chunk)] = time.time() + 90
                    else:
                        proceso_bloqueado_hasta = time.time() + 90
                es_silencio = False
                silencio_seguidos = 0
                reportar_audio_diag(warning, detalle)
                registrar_audio(
                    "captura_rota",
                    epoch=epoch, parte=st.get("parte"), modo=modo_chunk,
                    pid=pid_chunk, sesion_pid=sesion_pid_chunk,
                    rms=round(float(rms), 1), peak=int(peak),
                    pico_sesion=round(float(pico_nav), 3)
                    if pico_nav is not None else None,
                    ruta_invalidada=True,
                    siguiente_intento="instancia_nueva")
                manifestar(
                    "capture_failed",
                    wall_end=time.time(),
                    monotonic_end_ns=time.monotonic_ns(),
                    reason=(
                        "audio_requires_attention"
                        if salud["requires_attention"]
                        else "browser_active_pcm_flat"),
                    capture_outcome=(
                        "requires_attention"
                        if salud["requires_attention"] else "route_failed"),
                    route_health=salud,
                    mode=modo_chunk,
                    pid=pid_chunk,
                    session_pid=sesion_pid_chunk,
                    rate=rate,
                    channels=ch,
                    frame_count=len(data) // max(1, int(ch) * 2),
                    rms=round(float(rms), 1),
                    peak=int(peak),
                    session_peak=pico_nav,
                    **continuidad,
                )
                cerrar_capturadores()
                try:
                    est.actualizar_estado(
                        d,
                        audio_fallback_transition=(
                            "ruta_rota_cerrada_reintento_instancia_nueva"),
                    )
                except Exception:
                    pass
                if st_final.get("auto_pausa") and not salud["requires_attention"]:
                    est.transicionar_captura(
                        d, "auto_seguir",
                        nota="captura rota detectada; reanudado")
                continue  # no guardar el chunk de ceros; reintentar con PyAudio fresco

            if es_silencio:
                terminar_preview_chunk(valid=False, reason="discarded_silence")
                silencio_seguidos += 1
                visual_diag = {}
                visual_es_idle = False
                if config.VISUAL_IDLE_AUTOPAUSA:
                    try:
                        visual_diag = visual_idle.analizar_pantalla_actual(
                            monitor=st.get("monitor"), region=st.get("region"))
                        visual_es_idle = bool(visual_diag.get("idle"))
                        if visual_es_idle:
                            est.actualizar_estado(d, visual_idle=visual_diag)
                    except Exception as e:
                        config.log_error("visual_idle", f"{type(e).__name__}: {e}")
                umbral_actual = visual_idle.umbral_silencio(
                    config.SILENCIO_CHUNKS, visual_es_idle)
                diag = diagnostico_silencio_proceso(
                    fuente, modo_chunk, rms, pico_nav, silencio_seguidos,
                    umbral_actual, config.SILENCIO_RMS)
                reportar_audio_diag(diag["warning"], diag["detalle"])
                salud = registrar_salud_ruta(
                    ruta_actual, "silence", modo=modo_chunk, dispositivo=dev,
                    pid=pid_chunk, session_pid=sesion_pid_chunk,
                    stream_id=stream_id_actual, rms=rms, peak=peak,
                    session_peak=pico_nav, transition_cause="pcm_silence")
                manifestar(
                    "capture_failed" if salud["requires_attention"]
                    else "discarded_silence",
                    wall_end=time.time(),
                    monotonic_end_ns=time.monotonic_ns(),
                    mode=modo_chunk,
                    pid=pid_chunk,
                    session_pid=sesion_pid_chunk,
                    rate=rate,
                    channels=ch,
                    frame_count=len(data) // max(1, int(ch) * 2),
                    rms=round(float(rms), 1),
                    peak=int(peak),
                    session_peak=pico_nav,
                    reason=(
                        "audio_requires_attention"
                        if salud["requires_attention"] else None),
                    capture_outcome=(
                        "requires_attention"
                        if salud["requires_attention"] else "silence"),
                    route_health=salud,
                    **continuidad,
                )
                if salud["requires_attention"]:
                    cerrar_capturadores()
                    continue
                if st_final.get("auto_pausa"):
                    continue  # break confirmado: seguir escuchando SIN almacenar
                if silencio_seguidos >= umbral_actual:
                    if proceso_loopback_silencioso_con_pantalla_activa(
                            fuente, modo_chunk, rms, pico_nav, visual_es_idle,
                            config.SILENCIO_RMS):
                        proceso_bloqueado_hasta = time.time() + 90
                        silencio_seguidos = 0
                        warning = (f"{nombre_fuente_audio(fuente)} por PID quedo "
                                   "silencioso; probando salida del sistema")
                        detalle = (f"Process-loopback bajo (rms={rms:.0f}, "
                                   f"pico={pico_nav if pico_nav is not None else 'n/d'}) "
                                   "con pantalla activa. Se bloquea el PID 90s y se "
                                   "activa enfoque para probar fallback de dispositivo "
                                   "sin capturar otras apps.")
                        if not st_final.get("enfoque"):
                            n = audio_sesiones.enfoque(fuente, True)
                            est.actualizar_estado(d, enfoque=True)
                            print(f"[audio] 🎯 fallback por PID silencioso: enfoque ON "
                                  f"({n} apps)", flush=True)
                        reportar_audio_diag(warning, detalle)
                        print("[audio] ⚠️ process-loopback silencioso con pantalla activa; "
                              "reintentando por dispositivo antes de auto-pausar",
                              flush=True)
                        continue
                    motivo = "silencio_visual_idle" if visual_es_idle else "silencio"
                    extra = ""
                    if visual_es_idle:
                        extra = f"; visual={','.join(visual_diag.get('razones', []))}"
                    est.transicionar_captura(
                        d,
                        "auto_pausa",
                        nota=(f"{silencio_seguidos * config.AUDIO_CHUNK_SEG}s de "
                              f"silencio (rms={rms:.0f}){extra}"),
                        actualizaciones={
                            "auto_pausa_motivo": motivo,
                            "visual_idle": visual_diag,
                        },
                    )
                    print("[audio] 🔇 silencio prolongado -> AUTO-PAUSA "
                          "(no se almacena nada hasta que vuelva el audio)")
                    continue  # este chunk silencioso tampoco se guarda
                continue  # silencio temprano: no guardar WAV basura antes del umbral
            else:
                silencio_seguidos = 0
                reportar_audio_diag()
                registrar_salud_ruta(
                    ruta_actual, "valid", modo=modo_chunk, dispositivo=dev,
                    pid=pid_chunk, session_pid=sesion_pid_chunk,
                    stream_id=stream_id_actual, rms=rms, peak=peak,
                    session_peak=pico_nav, transition_cause="pcm_valid")
                if st_final.get("auto_pausa"):
                    est.transicionar_captura(
                        d, "auto_seguir",
                        nota=f"audio de vuelta (rms={rms:.0f})")
                    print("[audio] 🔊 audio detectado -> se reanuda el almacenamiento")

            # v = velocidad de reproducción cuando se ve una GRABACIÓN (10=1x, 15=1.5x, 20=2x):
            # el transcriptor la usa para estirar el audio y compensar la aceleración.
            # El metadato pertenece al estado al iniciar el corte. Etiquetarlo con
            # el estado final mezclaba velocidades/partes si la UI cambiaba a mitad.
            wall_end = time.time()
            monotonic_end_ns = time.monotonic_ns()
            frame_count = len(data) // max(1, int(ch) * 2)
            terminar_preview_chunk(valid=True, reason="sealed")
            evento_sellado = manifestar(
                "sealed",
                publicar=False,
                wall_end=wall_end,
                monotonic_end_ns=monotonic_end_ns,
                duration=frame_count / max(1, int(rate)),
                rate=int(rate),
                channels=int(ch),
                sample_width=2,
                frame_count=frame_count,
                expected_frames=round(float(rate) * config.AUDIO_CHUNK_SEG),
                mode=modo_chunk,
                pid=pid_chunk,
                session_pid=sesion_pid_chunk,
                rms=round(float(rms), 1),
                peak=int(peak),
                session_peak=pico_nav,
                **continuidad,
            )
            audio_log = {
                "epoch": epoch,
                "parte": st.get("parte"),
                "archivo": ruta.name,
                "modo": modo_chunk,
                "pid": pid_chunk,
                "sesion_pid": sesion_pid_chunk,
                "rms": round(float(rms), 1),
                "peak": int(peak),
                "pico_sesion": (
                    round(float(pico_nav), 3) if pico_nav is not None else None),
                "rate": rate,
                "canales": ch,
                "velocidad": float(st.get("velocidad", 1.0)),
                "wall_start": continuidad.get("pcm_wall_start", wall_start),
                "wall_end": continuidad.get("pcm_wall_end", wall_end),
                "monotonic_start_ns": monotonic_start_ns,
                "monotonic_end_ns": monotonic_end_ns,
                "frame_count": frame_count,
                **continuidad,
            }
            enviar_persistencia(TrabajoPersistenciaWav(
                ruta=ruta,
                data=data,
                rate=int(rate),
                canales=int(ch),
                manifest_event=evento_sellado,
                audio_log=audio_log,
            ))
        except CambioRutaCaptura as e:
            cerrar_proceso()
            parcial_publicado = False
            parcial_data = b""
            parcial_rate = parcial_ch = 0
            parcial_continuidad: dict = {}
            if e.parcial:
                parcial_data, parcial_rate, parcial_ch, parcial_continuidad = e.parcial
            parcial_rms = calcular_rms(parcial_data) if parcial_data else 0.0
            parcial_peak = calcular_peak(parcial_data) if parcial_data else 0
            parcial_frames = len(parcial_data) // max(1, parcial_ch * 2)
            parcial_continuidad.setdefault("terminal_discontinuity", True)
            parcial_util = pcm_parcial_publicable(
                parcial_data, parcial_ch, config.SILENCIO_RMS)
            terminar_preview_chunk(
                valid=parcial_util,
                reason=("sealed_partial" if parcial_util else "route_changed"),
            )
            if parcial_util:
                salud = registrar_salud_ruta(
                    "proceso", "partial", modo=modo_chunk, pid=e.pid,
                    session_pid=sesion_pid_chunk,
                    stream_id=str(parcial_continuidad.get("stream_id") or ""),
                    rms=parcial_rms, peak=parcial_peak,
                    transition_cause=e.failure_reason)
                parcial_fin_ns = time.monotonic_ns()
                evento_parcial = manifestar(
                    "sealed",
                    publicar=False,
                    wall_end=time.time(),
                    monotonic_end_ns=parcial_fin_ns,
                    duration=parcial_frames / max(1, parcial_rate),
                    rate=parcial_rate,
                    channels=parcial_ch,
                    sample_width=2,
                    frame_count=parcial_frames,
                    expected_frames=round(
                        float(parcial_rate) * config.AUDIO_CHUNK_SEG),
                    mode=modo_chunk,
                    pid=e.pid,
                    session_pid=sesion_pid_chunk,
                    rms=round(float(parcial_rms), 1),
                    peak=int(parcial_peak),
                    reason="process_loopback_failed_after_partial",
                    error_type=e.error_type,
                    capture_partial=True,
                    capture_outcome="failed_after_partial",
                    route_health=salud,
                    **parcial_continuidad,
                )
                enviar_persistencia(TrabajoPersistenciaWav(
                    ruta=ruta,
                    data=parcial_data,
                    rate=parcial_rate,
                    canales=parcial_ch,
                    manifest_event=evento_parcial,
                    audio_log={
                        "epoch": epoch,
                        "parte": st.get("parte"),
                        "archivo": ruta.name,
                        "modo": modo_chunk,
                        "pid": e.pid,
                        "sesion_pid": sesion_pid_chunk,
                        "rms": round(float(parcial_rms), 1),
                        "peak": int(parcial_peak),
                        "rate": parcial_rate,
                        "canales": parcial_ch,
                        "wall_start": parcial_continuidad.get(
                            "pcm_wall_start", wall_start),
                        "wall_end": parcial_continuidad.get(
                            "pcm_wall_end", time.time()),
                        "monotonic_start_ns": monotonic_start_ns,
                        "monotonic_end_ns": parcial_fin_ns,
                        "frame_count": parcial_frames,
                        "capture_partial": True,
                        **parcial_continuidad,
                    },
                ))
                parcial_publicado = True
            else:
                salud = registrar_salud_ruta(
                    "proceso", "failed", modo=modo_chunk, pid=e.pid,
                    session_pid=sesion_pid_chunk,
                    stream_id=str(parcial_continuidad.get("stream_id") or ""),
                    rms=parcial_rms, peak=parcial_peak,
                    transition_cause=e.failure_reason)
                manifestar(
                    "capture_failed",
                    wall_end=time.time(),
                    monotonic_end_ns=time.monotonic_ns(),
                    reason=(
                        "audio_requires_attention"
                        if salud["requires_attention"] else e.failure_reason),
                    capture_outcome=(
                        "requires_attention"
                        if salud["requires_attention"] else "route_failed"),
                    route_health=salud,
                    error_type=e.error_type,
                    mode=modo_chunk,
                    pid=e.pid,
                    partial_frame_count=parcial_frames,
                    partial_pcm_useful=False,
                    **parcial_continuidad,
                )
            registrar_audio(
                "cambio_ruta_captura",
                epoch=epoch,
                parte=st.get("parte"),
                modo=modo_chunk,
                pid=e.pid,
                error_type=e.error_type,
                reason=e.failure_reason,
                parcial_publicado=parcial_publicado,
                partial_frame_count=parcial_frames,
            )
            continue
        except OverflowCapturaDispositivo as e:
            cerrar_dispositivo()
            parcial_data, parcial_rate, parcial_ch, parcial_continuidad = e.parcial
            parcial_frames = len(parcial_data) // max(1, parcial_ch * 2)
            parcial_rms = calcular_rms(parcial_data) if parcial_data else 0.0
            parcial_peak = calcular_peak(parcial_data) if parcial_data else 0
            parcial_fin_ns = time.monotonic_ns()
            parcial_util = pcm_parcial_publicable(
                parcial_data, parcial_ch, config.SILENCIO_RMS)
            terminar_preview_chunk(
                valid=parcial_util,
                reason=("sealed_partial" if parcial_util else "device_overflow"),
            )
            salud = registrar_salud_ruta(
                "dispositivo", "partial" if parcial_util else "failed",
                modo=modo_chunk, dispositivo=dev,
                stream_id=str(parcial_continuidad.get("stream_id") or ""),
                rms=parcial_rms, peak=parcial_peak,
                transition_cause="pyaudio_input_overflow")
            if parcial_util:
                evento_parcial = manifestar(
                    "sealed",
                    publicar=False,
                    wall_end=parcial_continuidad.get("pcm_wall_end", time.time()),
                    monotonic_end_ns=parcial_fin_ns,
                    duration=parcial_frames / max(1, parcial_rate),
                    rate=parcial_rate,
                    channels=parcial_ch,
                    sample_width=2,
                    frame_count=parcial_frames,
                    expected_frames=round(
                        float(parcial_rate) * config.AUDIO_CHUNK_SEG),
                    mode="dispositivo",
                    rms=round(float(parcial_rms), 1),
                    peak=int(parcial_peak),
                    reason="pyaudio_input_overflow_after_partial",
                    error_type=e.error_type,
                    capture_partial=True,
                    capture_outcome="portaudio_overflow",
                    route_health=salud,
                    **parcial_continuidad,
                )
                enviar_persistencia(TrabajoPersistenciaWav(
                    ruta=ruta,
                    data=parcial_data,
                    rate=parcial_rate,
                    canales=parcial_ch,
                    manifest_event=evento_parcial,
                    audio_log={
                        "epoch": epoch,
                        "parte": st.get("parte"),
                        "archivo": ruta.name,
                        "modo": "dispositivo",
                        "rms": round(float(parcial_rms), 1),
                        "peak": int(parcial_peak),
                        "rate": parcial_rate,
                        "canales": parcial_ch,
                        "frame_count": parcial_frames,
                        "capture_partial": True,
                        **parcial_continuidad,
                    },
                ))
            else:
                manifestar(
                    "capture_failed",
                    wall_end=time.time(),
                    monotonic_end_ns=parcial_fin_ns,
                    reason=(
                        "audio_requires_attention"
                        if salud["requires_attention"]
                        else "pyaudio_input_overflow_without_useful_partial"),
                    capture_outcome=(
                        "requires_attention"
                        if salud["requires_attention"] else "route_failed"),
                    route_health=salud,
                    error_type=e.error_type,
                    mode="dispositivo",
                    partial_frame_count=0,
                    **parcial_continuidad,
                )
            registrar_audio(
                "pyaudio_input_overflow",
                epoch=epoch,
                parte=st.get("parte"),
                parcial_publicado=parcial_util,
                partial_frame_count=parcial_frames,
            )
            reportar_audio_diag(
                "Se detecto overflow en la captura de audio",
                f"PortAudio perdio frames; se conservaron {parcial_frames} frames previos.",
            )
            continue
        except CapturaRotaTemprana as e:
            cerrar_capturadores()
            terminar_preview_chunk(valid=False, reason="watchdog_pcm_flat")
            if pid_chunk:
                pid_bloqueados[int(pid_chunk)] = time.time() + 90
            proceso_bloqueado_hasta = time.time() + 90
            ruta_fallida = "proceso" if ":proceso" in modo_chunk else "dispositivo"
            salud = registrar_salud_ruta(
                ruta_fallida, "flat", modo=modo_chunk, dispositivo=dev,
                pid=pid_chunk, session_pid=sesion_pid_chunk,
                rms=e.rms, peak=e.peak, session_peak=e.pico_sesion,
                transition_cause="watchdog_pcm_flat")
            registrar_audio(
                "captura_rota_temprana",
                epoch=epoch,
                parte=st.get("parte"),
                modo=modo_chunk,
                pid=pid_chunk,
                rms=round(e.rms, 1),
                peak=e.peak,
                pico_sesion=round(e.pico_sesion, 3),
                ventanas=e.ventanas,
                deteccion_seg=round(e.ventanas * watchdog.ventana_seg, 2),
            )
            manifestar(
                "capture_failed",
                wall_end=time.time(),
                monotonic_end_ns=time.monotonic_ns(),
                reason=(
                    "audio_requires_attention"
                    if salud["requires_attention"] else "watchdog_pcm_flat"),
                capture_outcome=(
                    "requires_attention"
                    if salud["requires_attention"] else "route_failed"),
                route_health=salud,
                mode=modo_chunk,
                pid=pid_chunk,
                rms=round(e.rms, 1),
                peak=e.peak,
                session_peak=e.pico_sesion,
                watchdog_windows=e.ventanas,
            )
            reportar_audio_diag(
                f"{nombre_fuente_audio(fuente)} suena, pero la ruta de captura esta muda",
                f"Watchdog cambio de ruta en {e.ventanas * watchdog.ventana_seg:.1f}s "
                f"(rms={e.rms:.1f}, peak={e.peak}, pico={e.pico_sesion:.3f}).",
            )
            print("[audio] captura plana detectada temprano; cambiando de ruta", flush=True)
            continue
        except Exception as e:
            cerrar_capturadores()
            terminar_preview_chunk(
                valid=False, reason=f"capture_error:{type(e).__name__}")
            ruta_fallida = "proceso" if ":proceso" in modo_chunk else "dispositivo"
            salud = registrar_salud_ruta(
                ruta_fallida, "failed", modo=modo_chunk, dispositivo=dev,
                pid=pid_chunk, session_pid=sesion_pid_chunk,
                transition_cause=type(e).__name__)
            try:
                manifestar(
                    "capture_failed",
                    wall_end=time.time(),
                    monotonic_end_ns=time.monotonic_ns(),
                    reason=(
                        "audio_requires_attention"
                        if salud["requires_attention"]
                        else f"{type(e).__name__}: {e}"),
                    capture_outcome=(
                        "requires_attention"
                        if salud["requires_attention"] else "route_failed"),
                    route_health=salud,
                )
            except Exception as manifest_error:
                config.log_error(
                    "audio_manifest",
                    f"{type(manifest_error).__name__}: {manifest_error}",
                )
            config.log_error("audio", f"{type(e).__name__}: {e}")
            print(f"[audio] error ({e}); reintento en 5s", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
