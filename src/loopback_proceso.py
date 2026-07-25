"""Loopback WASAPI filtrado por proceso (Chrome/Edge).

Windows 10 build 20348+ expone VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK: permite
capturar solo el audio renderizado por un proceso y sus hijos. Este modulo lo
usa de forma opcional; si falla, captura_audio vuelve al loopback de dispositivo
con enfoque continuo.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import POINTER, byref, c_int, c_long, c_ubyte, c_uint32, c_uint64, c_void_p
from ctypes import c_ushort, cast, string_at, wintypes

from comtypes import COMObject, COMMETHOD, GUID, HRESULT, IUnknown

from . import config

if sys.platform == "win32":
    import comtypes


S_OK = 0
VT_BLOB = 65
WAVE_FORMAT_PCM = 1

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x00000001
AUDCLNT_BUFFERFLAGS_SILENT = 0x00000002

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
MAX_DEVICE_POSITION_JUMP_SECONDS = 2.0
QPC_TICKS_PER_SECOND = 10_000_000
MAX_QPC_INFERRED_GAP_SECONDS = 120.0


class DiscontinuidadWasapi(OSError):
    """WASAPI declaro perdida de datos aunque la posicion parezca continua."""


class DiscontinuidadPosicion(DiscontinuidadWasapi):
    """La posición del dispositivo exige cerrar esta época sin usar el paquete."""

    def __init__(self, kind: str, delta: int, device_position: int,
                 previous_end: int):
        self.kind = str(kind)
        self.reason = self.kind
        self.delta = int(delta)
        self.device_position = int(device_position)
        self.previous_end = int(previous_end)
        super().__init__(
            f"{self.kind}: delta={self.delta} frames "
            f"device_position={self.device_position} previous_end={self.previous_end}")

IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", c_ushort),
        ("nChannels", c_ushort),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", c_ushort),
        ("wBitsPerSample", c_ushort),
        ("cbSize", c_ushort),
    ]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", c_int),
    ]


class _AUDIOCLIENT_ACTIVATION_UNION(ctypes.Union):
    _fields_ = [("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("ActivationType", c_int),
        ("u", _AUDIOCLIENT_ACTIVATION_UNION),
    ]


class BLOB(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("pBlobData", POINTER(c_ubyte)),
    ]


class _PROPVARIANT_UNION(ctypes.Union):
    _fields_ = [
        ("blob", BLOB),
        ("ptr", c_void_p),
    ]


class PROPVARIANT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("u", _PROPVARIANT_UNION),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")


IActivateAudioInterfaceAsyncOperation._methods_ = [
    COMMETHOD(
        [],
        HRESULT,
        "GetActivateResult",
        (["out"], POINTER(HRESULT), "activateResult"),
        (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
    )
]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")


IActivateAudioInterfaceCompletionHandler._methods_ = [
    COMMETHOD(
        [],
        HRESULT,
        "ActivateCompleted",
        (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"),
    )
]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = []


class IAudioClient(IUnknown):
    _iid_ = IID_IAudioClient


IAudioClient._methods_ = [
    COMMETHOD([], HRESULT, "Initialize",
              (["in"], c_int, "ShareMode"),
              (["in"], wintypes.DWORD, "StreamFlags"),
              (["in"], c_long, "hnsBufferDuration"),
              (["in"], c_long, "hnsPeriodicity"),
              (["in"], POINTER(WAVEFORMATEX), "pFormat"),
              (["in"], c_void_p, "AudioSessionGuid")),
    COMMETHOD([], HRESULT, "GetBufferSize",
              (["out"], POINTER(c_uint32), "pNumBufferFrames")),
    COMMETHOD([], HRESULT, "GetStreamLatency",
              (["out"], POINTER(c_long), "phnsLatency")),
    COMMETHOD([], HRESULT, "GetCurrentPadding",
              (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
    COMMETHOD([], HRESULT, "IsFormatSupported",
              (["in"], c_int, "ShareMode"),
              (["in"], POINTER(WAVEFORMATEX), "pFormat"),
              (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch")),
    COMMETHOD([], HRESULT, "GetMixFormat",
              (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
    COMMETHOD([], HRESULT, "GetDevicePeriod",
              (["out"], POINTER(c_long), "phnsDefaultDevicePeriod"),
              (["out"], POINTER(c_long), "phnsMinimumDevicePeriod")),
    COMMETHOD([], HRESULT, "Start"),
    COMMETHOD([], HRESULT, "Stop"),
    COMMETHOD([], HRESULT, "Reset"),
    COMMETHOD([], HRESULT, "SetEventHandle",
              (["in"], wintypes.HANDLE, "eventHandle")),
    COMMETHOD([], HRESULT, "GetService",
              (["in"], POINTER(GUID), "riid"),
              (["out"], POINTER(c_void_p), "ppv")),
]


class IAudioCaptureClient(IUnknown):
    _iid_ = IID_IAudioCaptureClient


IAudioCaptureClient._methods_ = [
    COMMETHOD([], HRESULT, "GetBuffer",
              (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
              (["out"], POINTER(c_uint32), "pNumFramesToRead"),
              (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
              (["out"], POINTER(c_uint64), "pu64DevicePosition"),
              (["out"], POINTER(c_uint64), "pu64QPCPosition")),
    COMMETHOD([], HRESULT, "ReleaseBuffer",
              (["in"], c_uint32, "NumFramesRead")),
    COMMETHOD([], HRESULT, "GetNextPacketSize",
              (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
]


class _CompletionHandler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = threading.Event()
        self.result_hr: int | None = None
        self.activated = None
        self.error: BaseException | None = None

    def ActivateCompleted(self, activateOperation):
        try:
            self.result_hr, self.activated = activateOperation.GetActivateResult()
        except BaseException as e:  # callback COM: registrar y despertar al llamador
            self.error = e
        finally:
            self.event.set()
        return S_OK


_FUNC = None


def disponible() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "oledll")


def _kernel32_api():
    """Declara firmas que preservan HANDLE de 64 bits en ctypes."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [
        c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _activar_audio_client(pid: int, timeout: float = 5.0):
    global _FUNC
    if not disponible():
        raise RuntimeError("process loopback solo esta disponible en Windows")
    if _FUNC is None:
        func = ctypes.oledll.mmdevapi.ActivateAudioInterfaceAsync
        func.argtypes = [
            wintypes.LPCWSTR,
            POINTER(GUID),
            POINTER(PROPVARIANT),
            POINTER(IActivateAudioInterfaceCompletionHandler),
            POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
        ]
        func.restype = HRESULT
        _FUNC = func

    params = AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.ProcessLoopbackParams.TargetProcessId = int(pid)
    params.ProcessLoopbackParams.ProcessLoopbackMode = (
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
    )

    prop = PROPVARIANT()
    prop.vt = VT_BLOB
    prop.blob.cbSize = ctypes.sizeof(params)
    prop.blob.pBlobData = cast(byref(params), POINTER(c_ubyte))

    handler = _CompletionHandler()
    handler_iface = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    async_op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    hr = _FUNC(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(IID_IAudioClient),
               byref(prop), handler_iface, byref(async_op))
    if hr != S_OK:
        raise OSError(f"ActivateAudioInterfaceAsync fallo hr=0x{hr & 0xffffffff:08x}")
    if not handler.event.wait(timeout):
        raise TimeoutError("timeout activando process loopback")
    if handler.error:
        raise handler.error
    if handler.result_hr is None or handler.result_hr != S_OK:
        raise OSError(f"activacion process loopback fallo hr=0x{(handler.result_hr or 0) & 0xffffffff:08x}")
    if handler.activated is None:
        raise OSError("process loopback no devolvio IAudioClient")
    return handler.activated.QueryInterface(IAudioClient)


def _formato_pcm() -> WAVEFORMATEX:
    fmt = WAVEFORMATEX()
    fmt.wFormatTag = WAVE_FORMAT_PCM
    fmt.nChannels = 2
    fmt.nSamplesPerSec = 44100
    fmt.wBitsPerSample = 16
    fmt.nBlockAlign = fmt.nChannels * fmt.wBitsPerSample // 8
    fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign
    fmt.cbSize = 0
    return fmt


class CapturadorProceso:
    """Stream process-loopback persistente con cortes exactos por frame.

    WASAPI entrega paquetes con limites propios. El remanente de un paquete que
    cruza el limite de un WAV queda en ``_pendiente`` para el siguiente corte; el
    cliente de audio solo se detiene al cambiar PID, pausar o finalizar la clase.
    """

    def __init__(self, pid: int):
        self.pid = int(pid)
        self.stream_id = f"process-{self.pid}-{time.monotonic_ns()}"
        self.formato = _formato_pcm()
        self.rate = int(self.formato.nSamplesPerSec)
        self.canales = int(self.formato.nChannels)
        self.frame_bytes = int(self.formato.nBlockAlign)
        self._com_inicializado = False
        try:
            comtypes.CoInitialize()
            self._com_inicializado = True
        except Exception:
            # Puede estar inicializado con otro apartment; no se equilibra una
            # llamada que fallo.
            pass
        self._kernel32 = None
        self._evento = None
        self._client = None
        self._capture = None
        self._pendiente = bytearray()
        self._ultimo_device_end: int | None = None
        self._ultimo_device_position_raw: int | None = None
        self._ultimo_qpc_position: int | None = None
        self._ultimo_qpc_packet_frames = 0
        self._qpc_padding_frames = 0
        self._device_position_progress_seen = False
        self._delivered_frames = 0
        self.wall_anchor: float | None = None
        self._cerrado = False
        self._continuidad = {
            "gap_frames": 0,
            "overlap_frames": 0,
            "timeout_padding_frames": 0,
            "data_discontinuity_packets": 0,
            "device_position_start": None,
            "device_position_end": None,
            "device_position_event": "",
            "device_position_unavailable_packets": 0,
            "qpc_position_start": None,
            "qpc_position_end": None,
            "qpc_inferred_gap_frames": 0,
        }
        try:
            self._kernel32 = _kernel32_api()
            self._abrir()
        except Exception:
            self.close()
            raise

    def _abrir(self) -> None:
        client = _activar_audio_client(self.pid)
        self._client = client
        flags = (
            AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
        )
        client.Initialize(
            AUDCLNT_SHAREMODE_SHARED, flags, 0, 0, byref(self.formato), None)
        raw_capture = client.GetService(byref(IID_IAudioCaptureClient))
        self._capture = cast(raw_capture, POINTER(IAudioCaptureClient))
        self._evento = self._kernel32.CreateEventW(None, False, False, None)
        if not self._evento:
            raise OSError("CreateEventW fallo")
        client.SetEventHandle(self._evento)
        client.Start()
        self.wall_anchor = time.time()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _anexar_paquete(self, datos: bytes, nframes: int, device_pos: int,
                        qpc_position: int | None = None) -> bytes:
        """Anexa y devuelve exactamente el PCM corregido que recibió el timeline.

        El retorno incluye gaps sintetizados y excluye overlaps. Así un consumidor
        provisional no observa paquetes raw distintos de los que formarán el WAV.
        """
        device_pos_raw = int(device_pos)
        self._continuidad.setdefault("qpc_inferred_gap_frames", 0)
        corregido = bytearray()
        qpc = int(qpc_position) if qpc_position is not None else None
        ultimo_qpc = getattr(self, "_ultimo_qpc_position", None)
        ultimo_raw = getattr(self, "_ultimo_device_position_raw", None)
        progreso_visto = bool(getattr(
            self, "_device_position_progress_seen", False))
        if qpc is not None and ultimo_qpc is not None and qpc < ultimo_qpc:
            self._continuidad["device_position_event"] = "qpc_out_of_order"
            self._continuidad["terminal_discontinuity"] = True
            raise DiscontinuidadPosicion(
                "qpc_out_of_order", qpc - ultimo_qpc, device_pos_raw,
                int(self._ultimo_device_end or 0))

        device_pos_efectivo = device_pos_raw
        qpc_creciente = bool(
            qpc is not None and ultimo_qpc is not None and qpc > ultimo_qpc)
        qpc_inferred = False
        if (
            device_pos_raw == 0
            and ultimo_raw not in (None, 0)
            and progreso_visto
            and qpc_creciente
        ):
            delta_reset = -int(self._ultimo_device_end or 0)
            self._continuidad["device_position_event"] = "device_position_reset"
            self._continuidad["device_position_delta"] = delta_reset
            self._continuidad["terminal_discontinuity"] = True
            raise DiscontinuidadPosicion(
                "device_position_reset", delta_reset, device_pos_raw,
                int(self._ultimo_device_end or 0))
        if (
            device_pos_raw == 0
            and ultimo_raw == 0
            and self._ultimo_device_end is not None
            and qpc_creciente
            and not progreso_visto
        ):
            # El endpoint virtual process-loopback observado en Windows puede dejar
            # pu64DevicePosition fijo en cero. QPC creciente prueba orden de llegada;
            # en ese caso se concatena sin inventar overlap.
            elapsed_frames = round(
                (qpc - ultimo_qpc) * self.rate / QPC_TICKS_PER_SECOND)
            raw_missing = max(
                0, elapsed_frames - int(getattr(
                    self, "_ultimo_qpc_packet_frames", 0) or 0))
            already_padded = max(
                0, int(getattr(self, "_qpc_padding_frames", 0) or 0))
            missing = max(0, raw_missing - already_padded)
            if not config.loopback_qpc_recovery_enabled():
                missing = 0
            tolerance = max(1, round(self.rate * 0.005))
            if missing <= tolerance:
                missing = 0
            if missing > round(self.rate * MAX_QPC_INFERRED_GAP_SECONDS):
                self._continuidad["device_position_event"] = "qpc_gap_invalid_jump"
                self._continuidad["terminal_discontinuity"] = True
                raise DiscontinuidadPosicion(
                    "qpc_gap_invalid_jump", missing, device_pos_raw,
                    int(self._ultimo_device_end or 0))
            device_pos_efectivo = int(self._ultimo_device_end) + missing
            qpc_inferred = missing > 0
            if qpc_inferred:
                self._continuidad["qpc_inferred_gap_frames"] = (
                    int(self._continuidad.get("qpc_inferred_gap_frames", 0)) + missing)
                self._continuidad["device_position_event"] = "qpc_silence_recovered"
            else:
                self._continuidad["device_position_event"] = (
                    "device_position_unavailable")
            self._continuidad["device_position_unavailable_packets"] = (
                int(self._continuidad.get(
                    "device_position_unavailable_packets", 0)) + 1)

        packet_end = device_pos_efectivo + int(nframes)
        if self._continuidad["device_position_start"] is None:
            self._continuidad["device_position_start"] = device_pos_raw
        if qpc is not None and self._continuidad.get("qpc_position_start") is None:
            self._continuidad["qpc_position_start"] = qpc
        if self._ultimo_device_end is not None:
            delta = device_pos_efectivo - int(self._ultimo_device_end)
            max_jump = round(self.rate * MAX_DEVICE_POSITION_JUMP_SECONDS)
            if abs(delta) > max_jump and not qpc_inferred:
                kind = (
                    "device_position_reset"
                    if delta < 0 and device_pos_raw <= max_jump
                    else "device_position_invalid_jump"
                )
                self._continuidad["device_position_event"] = kind
                self._continuidad["device_position_delta"] = delta
                self._continuidad["terminal_discontinuity"] = True
                raise DiscontinuidadPosicion(
                    kind, delta, device_pos_raw, int(self._ultimo_device_end))
            if delta > 0:
                self._continuidad["gap_frames"] += delta
                # Mantiene el reloj PCM monotono. El manifest conserva que estos
                # frames fueron sintetizados y no audio observado.
                gap = b"\x00" * (delta * self.frame_bytes)
                self._pendiente.extend(gap)
                corregido.extend(gap)
            elif delta < 0:
                self._continuidad["device_position_event"] = "out_of_order_packet"
                solape = min(nframes, -delta)
                self._continuidad["overlap_frames"] += solape
                datos = datos[solape * self.frame_bytes:]
                nframes -= solape
        if nframes > 0:
            self._pendiente.extend(datos)
            corregido.extend(datos)
            self._ultimo_device_end = packet_end
            self._continuidad["device_position_end"] = self._ultimo_device_end
        if ultimo_raw is not None and device_pos_raw > ultimo_raw:
            self._device_position_progress_seen = True
        self._ultimo_device_position_raw = device_pos_raw
        if qpc is not None:
            self._ultimo_qpc_position = qpc
            self._ultimo_qpc_packet_frames = max(0, int(nframes))
            self._qpc_padding_frames = 0
            self._continuidad["qpc_position_end"] = qpc
        return bytes(corregido)

    def _drenar_paquetes(self, inspector=None) -> int:
        drenados = 0
        while True:
            disponibles = self._capture.GetNextPacketSize()
            if not disponibles:
                return drenados
            data, nframes, cap_flags, devpos, qpc = self._capture.GetBuffer()
            try:
                nframes = int(nframes)
                nbytes = nframes * self.frame_bytes
                discontinuidad = bool(
                    cap_flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY)
                if discontinuidad:
                    self._continuidad["data_discontinuity_packets"] = (
                        int(self._continuidad.get(
                            "data_discontinuity_packets", 0)) + 1)
                    self._continuidad["device_position_event"] = (
                        "wasapi_data_discontinuity")
                    self._continuidad["terminal_discontinuity"] = True
                    raise DiscontinuidadWasapi(
                        "WASAPI marco AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY")
                paquete = (b"\x00" * nbytes if cap_flags & AUDCLNT_BUFFERFLAGS_SILENT
                           else string_at(data, nbytes))
                self._anexar_paquete(
                    paquete, nframes, int(devpos), qpc_position=int(qpc))
                if inspector and paquete:
                    inspector(paquete, self.rate, self.canales)
                drenados += nframes
            finally:
                self._capture.ReleaseBuffer(nframes)

    def _rellenar_timeout(self, faltan_frames: int) -> None:
        """Rellena una ausencia y adelanta el reloj del dispositivo conocido."""
        faltan_frames = max(0, int(faltan_frames))
        if not faltan_frames:
            return
        padding = b"\x00" * (faltan_frames * self.frame_bytes)
        self._pendiente.extend(padding)
        self._continuidad["timeout_padding_frames"] += faltan_frames
        self._qpc_padding_frames = max(
            0, int(getattr(self, "_qpc_padding_frames", 0) or 0)) + faltan_frames
        # El relleno mantiene el timeline entregado al consumidor, pero no es una
        # observación del reloj hardware. Mutar device_position aquí fabricaba el
        # salto -1_323_000 al llegar el siguiente paquete real de 44.1 kHz.

    def extraer_parcial(self) -> tuple[bytes, int, int, dict]:
        """Entrega PCM util acumulado cuando el stream termina con error."""
        frames = len(self._pendiente) // self.frame_bytes
        nbytes = frames * self.frame_bytes
        data = bytes(self._pendiente[:nbytes])
        del self._pendiente[:nbytes]
        start_frame = self._delivered_frames
        self._delivered_frames += frames
        continuidad = dict(self._continuidad)
        continuidad.update({
            "stream_id": self.stream_id,
            "start_frame": start_frame,
            "end_frame": self._delivered_frames,
            "terminal_discontinuity": True,
        })
        if self.wall_anchor is not None:
            continuidad["pcm_wall_start"] = (
                self.wall_anchor + start_frame / self.rate)
            continuidad["pcm_wall_end"] = (
                self.wall_anchor + self._delivered_frames / self.rate)
        return data, self.rate, self.canales, continuidad

    def leer_frames(self, frames_objetivo: int, sonda=None, inspector=None,
                    consumer=None,
                    timeout_extra: float = 2.0) -> tuple[bytes, dict]:
        """Lee exactamente ``frames_objetivo`` sin reiniciar el stream."""
        if self._cerrado:
            raise RuntimeError("capturador process-loopback cerrado")
        frames_objetivo = max(0, int(frames_objetivo))
        bytes_objetivo = frames_objetivo * self.frame_bytes
        stream_start_frame = self._delivered_frames
        inicio = time.monotonic()
        limite = inicio + frames_objetivo / self.rate + max(0.5, timeout_extra)
        proxima_sonda = inicio
        proxima_inspeccion_ausencia = inicio + 1.0
        consumer_delivered_bytes = 0

        def deliver_consumer() -> None:
            nonlocal consumer_delivered_bytes
            if consumer is None:
                return
            available = min(len(self._pendiente), bytes_objetivo)
            if available <= consumer_delivered_bytes:
                return
            block = bytes(self._pendiente[consumer_delivered_bytes:available])
            consumer_delivered_bytes = available
            if block:
                consumer(block, self.rate, self.canales)

        self._continuidad = {
            "gap_frames": 0,
            "overlap_frames": 0,
            "timeout_padding_frames": 0,
            "data_discontinuity_packets": 0,
            "device_position_start": None,
            "device_position_end": None,
            "device_position_event": "",
            "device_position_unavailable_packets": 0,
            "qpc_position_start": None,
            "qpc_position_end": None,
            "qpc_inferred_gap_frames": 0,
        }
        deliver_consumer()
        while len(self._pendiente) < bytes_objetivo and time.monotonic() < limite:
            self._kernel32.WaitForSingleObject(self._evento, 250)
            drenados = self._drenar_paquetes(inspector=inspector)
            deliver_consumer()
            ahora = time.monotonic()
            if drenados:
                proxima_inspeccion_ausencia = ahora + 1.0
            elif inspector and ahora >= proxima_inspeccion_ausencia:
                inspector(
                    b"\x00" * (self.rate * self.frame_bytes),
                    self.rate,
                    self.canales,
                )
                proxima_inspeccion_ausencia = ahora + 1.0
            if sonda and ahora >= proxima_sonda:
                try:
                    sonda()
                except Exception:
                    pass
                proxima_sonda = ahora + 2.0
        if len(self._pendiente) < bytes_objetivo:
            faltan = bytes_objetivo - len(self._pendiente)
            faltan_frames = (faltan + self.frame_bytes - 1) // self.frame_bytes
            self._rellenar_timeout(faltan_frames)
            deliver_consumer()
        resultado = bytes(self._pendiente[:bytes_objetivo])
        del self._pendiente[:bytes_objetivo]
        self._delivered_frames += frames_objetivo
        self._continuidad["stream_id"] = self.stream_id
        self._continuidad["start_frame"] = stream_start_frame
        self._continuidad["end_frame"] = self._delivered_frames
        if self.wall_anchor is not None:
            self._continuidad["pcm_wall_start"] = (
                self.wall_anchor + stream_start_frame / self.rate)
            self._continuidad["pcm_wall_end"] = (
                self.wall_anchor + self._delivered_frames / self.rate)
        return resultado, dict(self._continuidad)

    def leer(self, segundos: float, sonda=None, inspector=None,
             consumer=None) -> tuple[bytes, int, int, dict]:
        frames = round(self.rate * max(0.0, float(segundos)))
        data, continuidad = self.leer_frames(
            frames, sonda=sonda, inspector=inspector, consumer=consumer)
        return data, self.rate, self.canales, continuidad

    def close(self) -> None:
        if self._cerrado:
            return
        self._cerrado = True
        if self._client is not None:
            try:
                self._client.Stop()
            except Exception:
                pass
        if self._evento:
            try:
                self._kernel32.CloseHandle(self._evento)
            except Exception:
                pass
            self._evento = None
        # Los punteros de interfaz de comtypes liberan su referencia al perder
        # la ultima referencia Python. Invocar Release() aqui los liberaba dos veces.
        self._capture = None
        self._client = None
        if self._com_inicializado:
            try:
                comtypes.CoUninitialize()
            finally:
                self._com_inicializado = False


def grabar_por_pid(pid: int, segundos: int, sonda=None) -> tuple[bytes, int, int]:
    """Compatibilidad: captura aislada; el runtime usa ``CapturadorProceso``."""
    with CapturadorProceso(pid) as capturador:
        data, rate, canales, _continuidad = capturador.leer(segundos, sonda=sonda)
    return data, rate, canales


def modo_test(pid: int, segundos: int = 5) -> int:
    from .captura_audio import calcular_rms, guardar_wav

    config.ensure_dirs()
    destino = config.LOGS_DIR / f"test_proceso_{pid}.wav"
    data, rate, canales = grabar_por_pid(pid, segundos)
    guardar_wav(destino, data, rate, canales)
    print(f"TEST_OK process_loopback pid={pid} archivo={destino} rms={calcular_rms(data):.0f}")
    return 0


def main() -> int:
    config.stdout_utf8()
    if len(sys.argv) < 2:
        print("Uso: python -m src.loopback_proceso <pid> [segundos]")
        return 2
    return modo_test(int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 5)


if __name__ == "__main__":
    raise SystemExit(main())
