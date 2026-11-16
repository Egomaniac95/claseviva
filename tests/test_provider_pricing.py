from datetime import datetime, timezone

import pytest

from src.provider_pricing import estimate_deepseek, is_peak_utc


def _timestamp(value="2026-09-14T02:00:00+00:00"):
    return datetime.fromisoformat(value).timestamp()


def _estimate(**overrides):
    args = dict(model="deepseek-flash", base_url="https://api.deepseek.com",
                prompt_tokens=1000, completion_tokens=500, cache_hit=640,
                cache_miss=360, timestamp=_timestamp())
    args.update(overrides)
    return estimate_deepseek(**args)


@pytest.mark.parametrize("hour,minute,second,expected", [
    (0, 59, 59, False), (1, 0, 0, True), (3, 59, 59, True),
    (4, 0, 0, False), (5, 59, 59, False), (6, 0, 0, True),
    (9, 59, 59, True), (10, 0, 0, False), (23, 59, 59, False),
])
def test_peak_limites_utc(hour, minute, second, expected):
    dt = datetime(2026, 9, 14, hour, minute, second, tzinfo=timezone.utc)
    assert is_peak_utc(dt.timestamp()) is expected


@pytest.mark.parametrize("day,expected", [(14, True), (18, True), (19, False), (20, False)])
def test_peak_solo_lunes_a_viernes(day, expected):
    dt = datetime(2026, 9, day, 2, tzinfo=timezone.utc)
    assert is_peak_utc(dt.timestamp()) is expected


def test_peak_no_depende_de_zona_local():
    # Domingo 20:00 Perú ya es lunes 01:00 UTC.
    assert is_peak_utc(_timestamp("2026-09-13T20:00:00-05:00")) is True


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-flash",
                                   "deepseek-v4-flash-vision-exp", "DeepSeek-V4.1-Flash"])
def test_aliases_flash_tarifa_actual_y_cache(model):
    peak = _estimate(model=model)
    offpeak = _estimate(model=model, timestamp=_timestamp("2026-09-13T02:00:00+00:00"))
    assert peak["costo_usd"] == pytest.approx((640 * .006 + 360 * .3 + 500 * 1.2) / 1e6)
    assert peak["costo_usd"] == pytest.approx(offpeak["costo_usd"] * 2)
    assert peak["estimacion_costo"]["estado"] == "estimado"
    assert peak["estimacion_costo"]["es_factura"] is False


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-pro-0813"])
def test_pro_sigue_soportado_sin_tarifarlo_como_flash(model):
    result = _estimate(model=model)
    assert result["costo_usd"] == pytest.approx((640 * .044 + 360 * 1.32 + 500 * 3.96) / 1e6)
    assert result["estimacion_costo"]["modelo_tarifa"] == "pro"


@pytest.mark.parametrize("kwargs,reason", [
    ({"model": "deepseek-futuro-pro"}, "modelo_sin_tarifa_verificada"),
    ({"base_url": "https://proxy.example/v1"}, "endpoint_sin_tarifa_verificada"),
    ({"base_url": "https://api.deepseek.com.example"}, "endpoint_sin_tarifa_verificada"),
    ({"prompt_tokens": -1}, "contadores_tokens_no_validos"),
    ({"completion_tokens": None}, "contadores_tokens_no_validos"),
    ({"timestamp": float("nan")}, "instante_no_valido"),
])
def test_precio_desconocido_no_se_inventa(kwargs, reason):
    result = _estimate(**kwargs)
    assert result["costo_usd"] is None
    assert result["estimacion_costo"]["estado"] == "no_disponible"
    assert result["estimacion_costo"]["motivo"] == reason


def test_cached_tokens_parcial_usa_diferencia_explicita():
    result = _estimate(cache_miss=None)
    assert result["costo_usd"] == _estimate()["costo_usd"]
    assert result["estimacion_costo"]["cache_base"] == "miss_por_diferencia"
    assert result["estimacion_costo"]["tokens_miss_calculo"] == 360


@pytest.mark.parametrize("hit,miss", [(None, None), (640, 400), (-10, 1010), (True, 999)])
def test_cache_ausente_o_inconsistente_solo_cota_superior(hit, miss):
    result = _estimate(cache_hit=hit, cache_miss=miss)
    assert result["costo_usd"] == pytest.approx((1000 * .3 + 500 * 1.2) / 1e6)
    assert result["estimacion_costo"]["estado"] == "cota_superior"
    assert result["estimacion_costo"]["tokens_hit_calculo"] == 0
