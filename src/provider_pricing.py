"""Estimaciones locales versionadas; nunca son una factura del proveedor.

Tarifa consultada el 2026-09-14 en la fuente oficial. No se aplica a registros
históricos: cada llamada nueva conserva su tarifa y sus supuestos en el log.
No hace consultas de red ni conoce claves, prompts o contenido de las clases.
"""
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit


PRICING_VERSION = "deepseek-2026-09-14"
PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
_MODELS = {
    "deepseek-flash": "flash",
    "deepseek-v4-flash": "flash",
    "deepseek-v4-flash-vision-exp": "flash",
    "deepseek-v4.1-flash": "flash",
    "deepseek-v4-pro": "pro",
    "deepseek-v4-pro-0813": "pro",
}
_OFF_PEAK = {
    "flash": (Decimal("0.003"), Decimal("0.15"), Decimal("0.60")),
    "pro": (Decimal("0.022"), Decimal("0.66"), Decimal("1.98")),
}


def is_peak_utc(timestamp: float) -> bool:
    """Lunes-viernes, intervalos UTC [01:00,04:00) y [06:00,10:00)."""
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return date.weekday() < 5 and (1 <= date.hour < 4 or 6 <= date.hour < 10)


def _token_count(value) -> bool:
    return type(value) is int and value >= 0


def estimate_deepseek(*, model: str, base_url: str, prompt_tokens,
                      completion_tokens, timestamp: float, cache_hit=None,
                      cache_miss=None, time_basis: str = "respuesta_local",
                      model_basis: str = "respuesta") -> dict:
    """Precio estimado por caché/horario o desconocido explícito.

Si falta el reparto de caché, usa todo el input como miss (cota superior),
sin inventar contadores reportados. La hora es una aproximación al cargo:
el proveedor decide el instante efectivo de facturación y puede cambiar tarifa.
"""
    detail = {
        "version": PRICING_VERSION,
        "fuente": PRICING_SOURCE,
        "consultada_fecha": "2026-09-14",
        "moneda": "USD",
        "estado": "no_disponible",
        "modelo_base": model_basis,
        "instante_base": time_basis,
        "instante": timestamp,
        "es_factura": False,
    }
    result = {"costo_usd": None, "estimacion_costo": detail}
    try:
        url = urlsplit(base_url)
        official = (
            url.scheme == "https" and url.hostname == "api.deepseek.com"
            and url.port in (None, 443) and url.path.rstrip("/") in ("", "/v1")
            and not url.username and not url.password
        )
    except ValueError:
        official = False
    if not official:
        detail["motivo"] = "endpoint_sin_tarifa_verificada"
        return result
    family = _MODELS.get(model.casefold().strip())
    if family is None:
        detail["motivo"] = "modelo_sin_tarifa_verificada"
        return result
    if not all(_token_count(value) for value in (prompt_tokens, completion_tokens)):
        detail["motivo"] = "contadores_tokens_no_validos"
        return result
    try:
        peak = is_peak_utc(timestamp)
    except (ValueError, OverflowError, OSError, TypeError):
        detail["motivo"] = "instante_no_valido"
        return result

    hit, miss = cache_hit, cache_miss
    basis = "reportado"
    if hit is None and _token_count(miss) and miss <= prompt_tokens:
        hit, basis = prompt_tokens - miss, "hit_por_diferencia"
    if miss is None and _token_count(hit) and hit <= prompt_tokens:
        miss, basis = prompt_tokens - hit, "miss_por_diferencia"
    if not (all(_token_count(value) for value in (hit, miss))
            and hit + miss == prompt_tokens):
        hit, miss = 0, prompt_tokens
        basis = "sin_cache_reportada" if cache_hit is None and cache_miss is None else "cache_inconsistente"
        detail["estado"] = "cota_superior"
    else:
        detail["estado"] = "estimado"
    rates = tuple(rate * (2 if peak else 1) for rate in _OFF_PEAK[family])
    cost = (hit * rates[0] + miss * rates[1] + completion_tokens * rates[2]) / 1_000_000
    result["costo_usd"] = round(float(cost), 9)
    detail.update({
        "modelo_tarifa": family,
        "franja_utc": "peak" if peak else "off_peak",
        "cache_base": basis,
        "tokens_hit_calculo": hit,
        "tokens_miss_calculo": miss,
        "usd_millon_hit": float(rates[0]),
        "usd_millon_miss": float(rates[1]),
        "usd_millon_salida": float(rates[2]),
    })
    return result
