"""Lectura WAV y transformación WSOLA; compatibilidad opcional con PyAV.

En este equipo, Windows Smart App Control bloquea las DLL sin firma de PyAV
("Una directiva de Control de aplicaciones bloqueó este archivo"), lo que impide
la decodificación de ciertos archivos. El pipeline canónico usa ``wave`` y arrays
NumPy, por lo que no necesita PyAV para capturar ni transcribir WAV.
"""
import sys
import types
import wave

import numpy as np

_MENSAJE = ("PyAV está bloqueado por Smart App Control. Usa WAV canónico/arrays "
            "NumPy; la ingesta directa de video no está disponible.")


class _NoDisponible:
    def __getattr__(self, _):
        raise RuntimeError(_MENSAJE)

    def __call__(self, *a, **k):
        raise RuntimeError(_MENSAJE)


def asegurar_av() -> bool:
    """Garantiza que `import av` no explote. Devuelve True si el av REAL está disponible."""
    try:
        import av  # noqa: F401
        return not getattr(sys.modules.get("av"), "_es_stub", False)
    except Exception:
        stub = types.ModuleType("av")
        stub._es_stub = True
        stub.open = _NoDisponible()
        stub.audio = _NoDisponible()
        stub.error = _NoDisponible()
        sys.modules["av"] = stub
        return False


def cargar_wav16k(ruta) -> np.ndarray:
    """WAV PCM int16 -> float32 mono 16 kHz (lo que espera WhisperModel.transcribe)."""
    with wave.open(str(ruta), "rb") as wf:
        rate = wf.getframerate()
        canales = wf.getnchannels()
        ancho = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if ancho != 2:
        raise ValueError(f"se esperaba PCM int16, sampwidth={ancho}")
    datos = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if canales > 1:
        datos = datos.reshape(-1, canales).mean(axis=1)
    if rate != 16000:
        n_out = int(len(datos) * 16000 / rate)
        datos = np.interp(np.linspace(0, len(datos), n_out, endpoint=False),
                          np.arange(len(datos)), datos).astype(np.float32)
    return datos


def cambiar_tempo_wsola(datos: np.ndarray, factor: float, sr: int = 16000) -> np.ndarray:
    """Time-stretch simple por WSOLA para cambiar tempo sin re-muestrear pitch.

    A diferencia de re-muestrear con np.interp, conserva bastante mejor la altura de
    la voz. Devuelve longitud ~= len(datos) * factor.
    """
    factor = float(factor)
    if 0.99 <= factor <= 1.01 or len(datos) < sr // 2:
        return datos.astype(np.float32, copy=False)
    if factor <= 0:
        raise ValueError(f"factor WSOLA invalido: {factor}")

    x = datos.astype(np.float32, copy=False)
    frame = max(256, int(sr * 0.040))          # 40 ms
    ana_hop = max(64, frame // 4)
    syn_hop = max(32, int(round(ana_hop * factor)))
    overlap = frame - syn_hop
    if overlap < 64:
        # Factor extremo o frame pequeño: usar fallback lineal antes que crashear.
        n_out = int(len(x) * factor)
        return np.interp(np.linspace(0, len(x), n_out, endpoint=False),
                         np.arange(len(x)), x).astype(np.float32)

    search = int(sr * 0.010)                  # +/- 10 ms
    out_len = int(len(x) * factor) + frame + syn_hop
    out = np.zeros(out_len, dtype=np.float32)
    peso = np.zeros(out_len, dtype=np.float32)
    win = np.hanning(frame).astype(np.float32)
    if not np.any(win):
        win = np.ones(frame, dtype=np.float32)

    in_pos = 0
    out_pos = 0
    while in_pos + frame < len(x) and out_pos + frame < out_len:
        if out_pos == 0:
            elegido = in_pos
        else:
            esperado = in_pos
            ref = out[out_pos:out_pos + overlap]
            ref_norm = float(np.linalg.norm(ref)) + 1e-6
            mejor, mejor_score = esperado, -1e9
            lo = max(0, esperado - search)
            hi = min(len(x) - frame, esperado + search)
            for cand in range(lo, hi + 1, max(1, search // 12)):
                seg = x[cand:cand + overlap]
                score = float(np.dot(ref, seg) / (ref_norm * (np.linalg.norm(seg) + 1e-6)))
                if score > mejor_score:
                    mejor, mejor_score = cand, score
            elegido = mejor

        bloque = x[elegido:elegido + frame] * win
        out[out_pos:out_pos + frame] += bloque
        peso[out_pos:out_pos + frame] += win
        in_pos += ana_hop
        out_pos += syn_hop

    valido = peso > 1e-6
    out[valido] /= peso[valido]
    usados = np.flatnonzero(valido)
    if usados.size == 0:
        return x
    return out[:usados[-1] + 1].astype(np.float32)


def estirar_wsola(datos: np.ndarray, factor: float, sr: int = 16000) -> np.ndarray:
    """Compensa audios reproducidos a 1.5x/2x estirando el tempo."""
    factor = float(factor)
    if factor <= 1.01:
        return datos.astype(np.float32, copy=False)
    return cambiar_tempo_wsola(datos, factor, sr=sr)
