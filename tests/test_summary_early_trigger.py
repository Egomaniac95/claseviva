"""El resumen automático se adelanta solo con la evidencia del tramo cerrada.

El margen fijo de 45 s es un techo para el peor caso (un chunk que cruza la
frontera más su commit). Cuando audio, voz del estudiante y pipeline visual ya
cerraron la ventana, esperar ese techo solo añade latencia: el tramo resultante
es idéntico porque ``hasta`` es la frontera de captura útil, no el instante del
disparo. Estas pruebas fijan ambas mitades del contrato.
"""
import pytest

from src import audio_manifest, escriba, estado

INICIO = 1000.0
FRONTERA = 1300.0          # 5 min de captura útil desde INICIO
TECHO = FRONTERA + escriba.MARGEN_TRANSCRIPTOR_SEG   # 1345.0
ASENTADO = FRONTERA + escriba.VISUAL_ASENTAMIENTO_SEG  # 1305.0


class _DetenerCiclo(RuntimeError):
    pass


def _crear_sesion(d, *, apuntes_auto: int = 5) -> None:
    d.mkdir(parents=True, exist_ok=True)
    estado.escribir_estado(d, {
        "inicio": INICIO,
        "estado": "grabando",
        "parte": 1,
        "nombre": "Fixture disparo temprano",
        "resumir": False,
        "resumen_estado": "",
        "resumen_msg": "",
        "apuntes_auto": apuntes_auto,
        "resumen_clock": "captura_util",
    })
    estado.anexar_jsonl(
        d / "marcas.jsonl", {"t": INICIO, "tipo": "inicio", "parte": 1})


def _chunk(d, chunk_id: str, wall_start: float, *, status: str = "sealed",
           sha: str = "", commit: bool = True) -> None:
    """Publica un evento de manifest y, opcionalmente, su commit STT."""
    sha = sha or (chunk_id * 4)[:64]
    audio_manifest.append_event(d, {
        "chunk_id": chunk_id, "status": status, "sequence": 1,
        "path": f"{chunk_id}.wav", "sha256": sha,
        "transactional_stt_required": True,
        "wall_start": wall_start, "wall_end": wall_start + 30.0,
        "duration": 30.0, "rate": 16000, "frame_count": 480000,
    })
    if commit and status == "sealed":
        audio_manifest.commit_transcript_chunk(
            d, chunk_id,
            [{"t": wall_start + 1, "texto": "contenido del tramo"}],
            metadata={"input_sha256": sha, "path": f"{chunk_id}.wav"},
            materialize=False)


def _sin_notas_pendientes(monkeypatch) -> None:
    monkeypatch.setattr(
        escriba.local_interventions, "pending_report",
        lambda _d: {"blocking": False, "count": 0})
    monkeypatch.setattr(
        escriba.student_notes, "pending_audio_notes",
        lambda _d, **_kwargs: [])
    monkeypatch.setattr(
        escriba.student_notes, "unresolved_local_interventions",
        lambda _d: [])


def test_evidencia_completa_habilita_disparo_antes_del_techo(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    _chunk(d, "chunk_000000001270_p1_v10", 1270.0)
    _sin_notas_pendientes(monkeypatch)

    assert escriba.evidencia_tramo_completa(d, INICIO, FRONTERA, ASENTADO)
    # La holgura visual todavía no transcurrió.
    assert not escriba.evidencia_tramo_completa(
        d, INICIO, FRONTERA, FRONTERA + 1.0)


def test_chunk_en_captura_sobre_la_frontera_bloquea_el_adelanto(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    _chunk(d, "chunk_000000001240_p1_v10", 1240.0)
    # El chunk que cruza la frontera sigue grabándose: falta su cola.
    _chunk(d, "chunk_000000001290_p1_v10", 1290.0,
           status="capturing", commit=False)
    _sin_notas_pendientes(monkeypatch)

    assert not escriba.evidencia_tramo_completa(d, INICIO, FRONTERA, ASENTADO)


def test_chunk_sellado_sin_commit_stt_bloquea_el_adelanto(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    _chunk(d, "chunk_000000001270_p1_v10", 1270.0, commit=False)
    _sin_notas_pendientes(monkeypatch)

    assert not escriba.evidencia_tramo_completa(d, INICIO, FRONTERA, ASENTADO)


def test_ventana_sin_audio_final_no_se_adelanta(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    _sin_notas_pendientes(monkeypatch)

    # Sin filas finales que solapen, la ventana vacía la resuelve el camino
    # normal con su propia gracia, no el disparo por cobertura.
    assert not escriba.evidencia_tramo_completa(d, INICIO, FRONTERA, ASENTADO)


@pytest.mark.parametrize("bloqueo", [
    "borrador_abierto", "nota_pendiente", "intervencion_sin_texto"])
def test_voz_del_estudiante_pendiente_bloquea_el_adelanto(
        monkeypatch, tmp_path, bloqueo):
    d = tmp_path / "clase"
    _crear_sesion(d)
    _chunk(d, "chunk_000000001270_p1_v10", 1270.0)
    _sin_notas_pendientes(monkeypatch)
    if bloqueo == "borrador_abierto":
        monkeypatch.setattr(
            escriba.local_interventions, "pending_report",
            lambda _d: {"blocking": True, "count": 1})
    elif bloqueo == "nota_pendiente":
        monkeypatch.setattr(
            escriba.student_notes, "pending_audio_notes",
            lambda _d, **_kwargs: [{"note_id": "n1"}])
    else:
        monkeypatch.setattr(
            escriba.student_notes, "unresolved_local_interventions",
            lambda _d: [{"note_id": "n1"}])

    assert not escriba.evidencia_tramo_completa(d, INICIO, FRONTERA, ASENTADO)


def test_error_leyendo_evidencia_manda_al_techo(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    _chunk(d, "chunk_000000001270_p1_v10", 1270.0)
    _sin_notas_pendientes(monkeypatch)
    errores: list = []
    monkeypatch.setattr(escriba.config, "log_error",
                        lambda *args: errores.append(args))

    def explota(*_args, **_kwargs):
        raise OSError("manifest ilegible")

    monkeypatch.setattr(escriba.audio_manifest, "latest_by_chunk", explota)

    assert not escriba.evidencia_tramo_completa(d, INICIO, FRONTERA, ASENTADO)
    assert errores and errores[0][0] == "escriba-cobertura"


def _parchar_main(monkeypatch, d, clock) -> None:
    monkeypatch.setattr(escriba.config, "stdout_utf8", lambda: None)
    monkeypatch.setattr(escriba.config, "instancia_unica", lambda _nombre: True)
    monkeypatch.setattr(escriba.config, "latido_perdido", lambda: False)
    monkeypatch.setattr(escriba.config, "log_error", lambda *args: None)
    monkeypatch.setattr(escriba.est, "clase_activa", lambda: d)
    monkeypatch.setattr(escriba.sys, "argv", ["escriba"])
    monkeypatch.setattr(escriba.time, "time", lambda: clock["now"])


def _fake_bloque(llamadas: list[dict], clock: dict):
    def fake(_d, desde, hasta, parte, forzar=False, disparo=None):
        # El instante del disparo distingue el adelanto por cobertura del
        # techo de 45 s: sin él, ambos caminos serían indistinguibles.
        llamadas.append({"desde": desde, "hasta": hasta, "parte": parte,
                         "forzar": forzar, "disparo": disparo,
                         "disparado_en": clock["now"]})
        return True

    fake.ultimo_estado = {"estado": "generado", "msg": "Resumen generado."}
    return fake


def _correr_main(monkeypatch, d, instantes: list[float]) -> list[dict]:
    """Ejecuta main() evaluando el bucle en cada instante de la lista."""
    clock = {"now": instantes[0]}
    _parchar_main(monkeypatch, d, clock)
    llamadas: list[dict] = []
    monkeypatch.setattr(escriba, "procesar_bloque", _fake_bloque(llamadas, clock))
    restantes = list(instantes[1:])

    def sleep(_segundos):
        if not restantes:
            raise _DetenerCiclo
        clock["now"] = restantes.pop(0)

    monkeypatch.setattr(escriba.time, "sleep", sleep)
    with pytest.raises(_DetenerCiclo):
        escriba.main()
    return llamadas


def test_main_adelanta_el_resumen_con_evidencia_cerrada(monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    _chunk(d, "chunk_000000001270_p1_v10", 1270.0)
    _sin_notas_pendientes(monkeypatch)

    # Se evalúa en la frontera (aún sin holgura visual) y justo después.
    llamadas = _correr_main(monkeypatch, d, [FRONTERA + 1.0, ASENTADO])

    # Disparó en la holgura visual, 40 s antes del techo, y no en la frontera.
    assert llamadas == [{
        "desde": INICIO, "hasta": FRONTERA, "parte": 1,
        "forzar": False, "disparo": "auto", "disparado_en": ASENTADO,
    }]


def test_main_espera_al_techo_si_la_voz_del_estudiante_sigue_pendiente(
        monkeypatch, tmp_path):
    d = tmp_path / "clase"
    _crear_sesion(d)
    _chunk(d, "chunk_000000001270_p1_v10", 1270.0)
    _sin_notas_pendientes(monkeypatch)
    pendientes = [{"note_id": "n1"}]
    monkeypatch.setattr(
        escriba.student_notes, "unresolved_local_interventions",
        lambda _d: list(pendientes))

    llamadas = _correr_main(monkeypatch, d, [ASENTADO, TECHO - 1.0, TECHO])

    # El adelanto quedó bloqueado, pero el techo de 45 s sigue disparando y el
    # tramo es exactamente el mismo que habría producido el adelanto.
    assert llamadas == [{
        "desde": INICIO, "hasta": FRONTERA, "parte": 1,
        "forzar": False, "disparo": "auto", "disparado_en": TECHO,
    }]


def test_main_produce_el_mismo_tramo_adelantado_o_al_techo(
        monkeypatch, tmp_path):
    """El instante del disparo no altera la ventana ni el resumen_id."""
    temprano = tmp_path / "temprano"
    tardio = tmp_path / "tardio"
    resultados = []
    for destino, instantes in ((temprano, [ASENTADO]), (tardio, [TECHO])):
        _crear_sesion(destino)
        _chunk(destino, "chunk_000000001270_p1_v10", 1270.0)
        _sin_notas_pendientes(monkeypatch)
        resultados.append(_correr_main(monkeypatch, destino, instantes))

    def _tramo(llamadas):
        return [{k: v for k, v in fila.items() if k != "disparado_en"}
                for fila in llamadas]

    # Mismo desde/hasta/parte/disparo: el resumen_id y la evidencia de entrada
    # son idénticos aunque el disparo ocurra 40 s antes.
    assert _tramo(resultados[0]) == _tramo(resultados[1])
    assert resultados[0][0]["disparado_en"] == ASENTADO
    assert resultados[1][0]["disparado_en"] == TECHO
    assert escriba._resumen_id("auto", 1, INICIO, FRONTERA) == (
        f"auto:p1:{INICIO:.6f}:{FRONTERA:.6f}")
