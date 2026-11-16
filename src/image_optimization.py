"""WebP 85 para capturas nuevas y conversión controlada de bibliotecas."""
from __future__ import annotations

import hashlib
import os
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
WEBP_QUALITY = 85


def write_bytes_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def encode_webp(image: Image.Image, *, quality: int = WEBP_QUALITY,
                method: int = 4, prefer_lossless: bool = False) -> bytes:
    """Conserva resolución, transparencia, orientación y perfil de color."""
    options = {key: image.info[key] for key in ("icc_profile", "exif", "xmp")
               if image.info.get(key)}
    mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
    output = BytesIO()
    converted = image.convert(mode)
    converted.save(output, "WEBP", quality=int(quality), method=method,
                   exact=True, **options)
    result = output.getvalue()
    if prefer_lossless:
        # Diagramas y texto de pocos colores pueden pesar menos sin pérdida.
        # Se evalúa al importar materiales, fuera del bucle de captura en vivo.
        lossless = BytesIO()
        converted.save(lossless, "WEBP", lossless=True, quality=int(quality),
                       method=4, exact=True, **options)
        if lossless.tell() < len(result):
            result = lossless.getvalue()
    return result


def optimize_bytes(data: bytes, *, quality: int = WEBP_QUALITY) -> tuple[bytes, dict]:
    """Convierte una sola vez a WebP; conserva el original si no ahorra espacio.

    WebP 85 es una conversión con pérdida autorizada. La validación exige las
    mismas dimensiones, alpha, EXIF e ICC, sin afirmar igualdad de píxeles RGB.
    Los WebP existentes no se recomprimen y las animaciones se conservan.
    """
    with Image.open(BytesIO(data)) as original:
        report = {"format_before": original.format, "dimensions": list(original.size),
                  "before_bytes": len(data), "before_sha256": hashlib.sha256(data).hexdigest(),
                  "optimized": False, "quality": int(quality)}
        if original.format == "WEBP" or getattr(original, "n_frames", 1) != 1:
            reason = "already_webp" if original.format == "WEBP" else "animation_preserved"
            return data, dict(report, after_bytes=len(data),
                              after_sha256=report["before_sha256"], reason=reason)
        candidate = encode_webp(original, quality=quality, method=6)
        with Image.open(BytesIO(candidate)) as converted:
            if (original.size != converted.size
                    or original.info.get("icc_profile") != converted.info.get("icc_profile")
                    or original.getexif() != converted.getexif()):
                raise RuntimeError("WebP alteró dimensiones, orientación o perfil de color")
            if "A" in original.getbands() or "transparency" in original.info:
                if original.convert("RGBA").getchannel("A").tobytes() != converted.convert("RGBA").getchannel("A").tobytes():
                    raise RuntimeError("WebP alteró la transparencia")
            report["dimensions_verified"] = True
        smaller = len(candidate) < len(data)
        result = candidate if smaller else data
        report.update(optimized=smaller, after_bytes=len(result),
                      after_sha256=hashlib.sha256(result).hexdigest(),
                      format_after="WEBP" if smaller else original.format,
                      reason="converted_webp_85" if smaller else "original_is_smaller")
    return result, report


def prepare_image(path: Path) -> tuple[Path, bytes, dict]:
    result, report = optimize_bytes(path.read_bytes())
    return path, result, report
