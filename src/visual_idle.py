"""Heuristicas locales para detectar pantallas de espera sin material docente.

Se usa junto con silencio de audio. No clasifica una clase solo por imagen: si hay audio,
la grabacion continua aunque la pantalla sea oscura.
"""
from __future__ import annotations

import math

from PIL import Image, ImageFilter, ImageStat

from . import config


def _thumb_gris(img: Image.Image) -> Image.Image:
    ancho = 96
    alto = max(24, round(img.height * ancho / max(1, img.width)))
    return img.convert("L").resize((ancho, alto))


def analizar_imagen(img: Image.Image) -> dict:
    """Devuelve metricas y decision para pantalla idle/espera.

    La regla es conservadora: busca fondo oscuro dominante, poco contraste y poco borde.
    Eso cubre Meet oscuro, pantalla negra de reconexion y sala con participantes sin clase,
    pero evita marcar como idle una diapositiva oscura con mucho texto o codigo.
    """
    return analizar_miniatura(_thumb_gris(img))


def analizar_miniatura(thumb: Image.Image) -> dict:
    """Analiza una miniatura ya reducida sin reconvertir el frame completo."""
    if thumb.mode != "L" or thumb.width != 96:
        alto = max(24, round(thumb.height * 96 / max(1, thumb.width)))
        thumb = thumb.resize((96, alto), Image.Resampling.BILINEAR).convert("L")
    hist = thumb.histogram()
    total = max(1, sum(hist))
    mean = sum(i * c for i, c in enumerate(hist)) / total
    var = sum(((i - mean) ** 2) * c for i, c in enumerate(hist)) / total
    std = math.sqrt(var)
    dark_ratio = sum(hist[:56]) / total
    bright_ratio = sum(hist[210:]) / total
    edges = thumb.filter(ImageFilter.FIND_EDGES)
    edge_mean = ImageStat.Stat(edges).mean[0]

    razones: list[str] = []
    pantalla_oscura = (
        dark_ratio >= config.VISUAL_IDLE_DARK_RATIO
        and mean <= config.VISUAL_IDLE_DARK_MEAN
        and edge_mean < 16
    )
    fondo_plano = mean <= 38 and std <= 28
    baja_info = dark_ratio >= 0.58 and std <= config.VISUAL_IDLE_LOW_STD and edge_mean < 11
    if pantalla_oscura:
        razones.append("pantalla_oscura")
    if fondo_plano:
        razones.append("fondo_oscuro_plano")
    if baja_info:
        razones.append("baja_informacion_visual")
    idle = bool(razones) and bright_ratio < 0.18
    return {
        "idle": idle,
        "razones": razones,
        "mean": round(mean, 2),
        "std": round(std, 2),
        "dark_ratio": round(dark_ratio, 3),
        "bright_ratio": round(bright_ratio, 3),
        "edge_mean": round(edge_mean, 2),
    }


def umbral_silencio(chunks_base: int, visual_idle: bool) -> int:
    """Chunks silenciosos requeridos antes de auto-pausa."""
    chunks_base = max(1, int(chunks_base))
    if visual_idle and config.VISUAL_IDLE_AUTOPAUSA:
        return min(chunks_base, config.VISUAL_IDLE_CHUNKS)
    return chunks_base


def analizar_pantalla_actual(monitor=None, region=None) -> dict:
    """Captura una imagen actual sin guardarla y la analiza.

    Import diferido para evitar dependencias de captura al importar tests logicos.
    """
    from . import captura_pantalla

    img = captura_pantalla.capturar_ahora(monitor=monitor, region=region)
    return analizar_imagen(img)
