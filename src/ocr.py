"""OCR local con backend nativo de Windows y fallback portable RapidOCR."""
import atexit
import asyncio
import difflib
import gc
import re
import sys
import unicodedata
from pathlib import Path

from PIL import Image

from . import config

_motor = None  # None = no inicializado; False = no disponible
_motor_info: dict = {
    "requested": getattr(config, "OCR_ENGINE", "auto"),
    "engine": "uninitialized",
    "operational": None,
}


def _texto_resultado(resultado) -> str:
    if not resultado:
        return ""
    return "\n".join(
        str(item[1]).strip()
        for item in resultado
        if len(item) > 1 and str(item[1]).strip()
    )


def _preparar_imagen(img: Image.Image) -> Image.Image:
    # convert() al mismo modo copia ~6 MB por frame; los backends solo leen.
    candidata = img if img.mode == "RGB" else img.convert("RGB")
    ancho_max = max(320, int(getattr(config, "CAPTURA_ANCHO_MAX", 1920)))
    if candidata.width > ancho_max:
        alto = max(1, round(candidata.height * ancho_max / candidata.width))
        candidata = candidata.resize((ancho_max, alto), Image.LANCZOS)
    return candidata


class _RapidOcrBackend:
    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        intra = int(getattr(config, "OCR_RAPID_INTRA_THREADS", 2))
        inter = int(getattr(config, "OCR_RAPID_INTER_THREADS", 1))
        kwargs = {}
        if intra > 0:
            kwargs["intra_op_num_threads"] = intra
        if inter > 0:
            kwargs["inter_op_num_threads"] = inter
        self._engine = RapidOCR(**kwargs)
        try:
            import onnxruntime as ort
            providers = list(ort.get_available_providers())
        except Exception:
            providers = []
        self.info = {
            "engine": "rapidocr_onnxruntime",
            "provider": "CPUExecutionProvider",
            "available_providers": providers,
            "intra_threads": intra if intra > 0 else "auto",
            "inter_threads": inter if inter > 0 else "auto",
        }

    def recognize(self, img: Image.Image) -> str:
        import numpy as np

        resultado, _ = self._engine(np.asarray(_preparar_imagen(img)))
        return _texto_resultado(resultado)


class _WindowsOcrBackend:
    """Puente síncrono y sin worker a ``Windows.Media.Ocr``."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows.Media.Ocr solo está disponible en Windows")
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import (
            BitmapAlphaMode,
            BitmapPixelFormat,
            SoftwareBitmap,
        )
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import Buffer

        self._Language = Language
        self._BitmapAlphaMode = BitmapAlphaMode
        self._BitmapPixelFormat = BitmapPixelFormat
        self._SoftwareBitmap = SoftwareBitmap
        self._Buffer = Buffer
        # Enumerar la colección WinRT completa exige otra proyección opcional;
        # probar solo las preferencias configuradas mantiene el binding mínimo.
        disponibles = []
        motor = None
        for tag in getattr(config, "OCR_WINDOWS_LANGUAGES", ("es-MX", "en-US")):
            language = Language(str(tag))
            if OcrEngine.is_language_supported(language):
                disponibles.append(str(tag))
                if motor is None:
                    motor = OcrEngine.try_create_from_language(language)
        if motor is None:
            motor = OcrEngine.try_create_from_user_profile_languages()
        if motor is None:
            raise RuntimeError(
                "no hay paquete de idioma compatible para Windows.Media.Ocr")
        self._engine = motor
        self._max_dimension = int(OcrEngine.max_image_dimension)
        language = str(motor.recognizer_language.language_tag)
        self.info = {
            "engine": "windows_media_ocr",
            "provider": "Windows.Media.Ocr",
            "language": language,
            "supported_preferred_languages": disponibles,
            "max_dimension": self._max_dimension,
        }

    async def _recognize_async(self, bitmap):
        return await self._engine.recognize_async(bitmap)

    def close(self) -> None:
        """Libera referencias WinRT antes de la descarga de extensiones nativas."""
        self._engine = None
        self._Buffer = None
        self._SoftwareBitmap = None
        self._BitmapPixelFormat = None
        self._BitmapAlphaMode = None
        self._Language = None

    def recognize(self, img: Image.Image) -> str:
        candidata = _preparar_imagen(img)
        mayor = max(candidata.size)
        if mayor > self._max_dimension:
            escala = self._max_dimension / mayor
            candidata = candidata.resize(
                (max(1, round(candidata.width * escala)),
                 max(1, round(candidata.height * escala))),
                Image.LANCZOS,
            )
        raw = candidata.convert("RGBA").tobytes("raw", "BGRA")
        buffer = self._Buffer(len(raw))
        buffer.length = len(raw)
        vista = memoryview(buffer)
        try:
            vista[:] = raw
        finally:
            vista.release()
        bitmap = self._SoftwareBitmap.create_copy_with_alpha_from_buffer(
            buffer,
            self._BitmapPixelFormat.BGRA8,
            candidata.width,
            candidata.height,
            self._BitmapAlphaMode.STRAIGHT,
        )
        try:
            resultado = asyncio.run(self._recognize_async(bitmap))
            return str(resultado.text or "")
        finally:
            bitmap.close()


def _iniciar():
    global _motor, _motor_info
    if _motor is None:
        solicitado = str(getattr(config, "OCR_ENGINE", "auto") or "auto").lower()
        if solicitado not in {"auto", "windows", "rapidocr", "off"}:
            solicitado = "auto"
        errores = []
        if solicitado == "off":
            _motor = False
            _motor_info = {
                "requested": solicitado, "engine": "off", "operational": False}
            return _motor
        if solicitado in {"auto", "windows"}:
            try:
                _motor = _WindowsOcrBackend()
            except Exception as exc:
                errores.append(f"windows:{type(exc).__name__}:{exc}")
        if _motor is None:
            try:
                _motor = _RapidOcrBackend()
            except Exception as exc:
                errores.append(f"rapidocr:{type(exc).__name__}:{exc}")
        if _motor is None:
            config.log_error("ocr", "ningún backend disponible: " + " | ".join(errores))
            _motor = False
            _motor_info = {
                "requested": solicitado,
                "engine": "unavailable",
                "operational": False,
                "fallback_reasons": errores,
            }
        else:
            _motor_info = {
                "requested": solicitado,
                "operational": True,
                **_motor.info,
            }
            if errores:
                _motor_info["fallback_reasons"] = errores
    return _motor


def estado_motor(*, iniciar: bool = False) -> dict:
    if iniciar:
        _iniciar()
    return dict(_motor_info)


def cerrar_motor() -> None:
    """Cierra el backend global mientras CPython y COM siguen operativos.

    PyWinRT puede descargar sus proyecciones después de que los objetos globales
    empiecen a finalizar. Soltar el motor desde ``atexit`` evita que esa liberación
    ocurra dentro del teardown nativo del intérprete.
    """
    global _motor, _motor_info
    motor = _motor
    _motor = None
    if motor not in {None, False}:
        close = getattr(motor, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    _motor_info = {
        "requested": getattr(config, "OCR_ENGINE", "auto"),
        "engine": "uninitialized",
        "operational": None,
    }
    gc.collect()


def _degradar_a_rapidocr(exc: Exception):
    global _motor, _motor_info
    if not isinstance(_motor, _WindowsOcrBackend):
        return _motor
    motivo = f"windows_runtime:{type(exc).__name__}:{exc}"
    try:
        _motor = _RapidOcrBackend()
        _motor_info = {
            "requested": _motor_info.get("requested", "auto"),
            "operational": True,
            **_motor.info,
            "fallback_reasons": [motivo],
        }
        config.log_error("ocr", f"Windows OCR falló; fallback RapidOCR: {exc}")
    except Exception as fallback_exc:
        config.log_error(
            "ocr", "fallaron Windows OCR y RapidOCR: "
            f"{exc} | {type(fallback_exc).__name__}: {fallback_exc}")
        _motor = False
        _motor_info = {
            "requested": _motor_info.get("requested", "auto"),
            "engine": "unavailable",
            "operational": False,
            "fallback_reasons": [
                motivo,
                f"rapidocr:{type(fallback_exc).__name__}:{fallback_exc}",
            ],
        }
    return _motor


def texto_de_imagen(ruta: Path | str) -> str:
    try:
        with Image.open(ruta) as imagen:
            imagen.load()
            return texto_de_pil(imagen)
    except Exception as e:
        config.log_error("ocr", f"{ruta}: {type(e).__name__}: {e}")
        return ""


def texto_de_pil(img: Image.Image) -> str:
    """Ejecuta OCR sobre una imagen en memoria sin crear archivos temporales."""
    motor = _iniciar()
    if not motor:
        return ""
    try:
        return motor.recognize(img)
    except Exception as e:
        fallback = _degradar_a_rapidocr(e)
        if fallback:
            try:
                return fallback.recognize(img)
            except Exception as fallback_exc:
                config.log_error(
                    "ocr", "imagen_en_memoria fallback: "
                    f"{type(fallback_exc).__name__}: {fallback_exc}")
        else:
            config.log_error("ocr", f"imagen_en_memoria: {type(e).__name__}: {e}")
        return ""


def normalizar_texto(texto: str) -> str:
    normal = unicodedata.normalize("NFKD", texto or "")
    normal = "".join(ch for ch in normal if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", normal).strip().lower()


def _tokens_evidencia(texto: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]{3,}", normalizar_texto(texto)))


def _firma_formula(texto: str) -> tuple[str, ...]:
    normal = normalizar_texto(texto)
    if not re.search(r"(?:==|!=|<=|>=|->|[=+*/^<>])|\b\d+(?:[.,]\d+)?\b", normal):
        return ()
    return tuple(re.findall(
        r"[a-z_][a-z0-9_]*|\d+(?:[.,]\d+)?|==|!=|<=|>=|->|[=+\-*/^<>]",
        normal,
    ))


def cambio_evidencia(anterior: str, actual: str) -> bool:
    """Detecta texto nuevo antes de que dHash pueda colapsar el frame.

    Es deliberadamente mas sensible que el agrupador de postproceso: los builds
    progresivos deben llegar al consumidor para que este elija su representante final.
    """
    a = normalizar_texto(anterior)
    b = normalizar_texto(actual)
    if a == b:
        return False
    if not a or not b:
        return bool(a or b)
    firma_a = _firma_formula(a)
    firma_b = _firma_formula(b)
    if firma_a != firma_b and (firma_a or firma_b):
        return True
    if _tokens_evidencia(a) != _tokens_evidencia(b):
        return True
    if max(len(a), len(b)) <= 120:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() < 0.985


def parece_codigo(texto: str) -> bool:
    """Heurística barata para decidir si el OCR contiene código fuente."""
    if not texto:
        return False
    señales = ("def ", "class ", "import ", "return", "print(", "for ", "while ",
               "==", "=>", "();", "{", "}", "&&", "||", "np.", "tf.", "torch.")
    puntos = sum(1 for s in señales if s in texto)
    return puntos >= 2


atexit.register(cerrar_motor)
