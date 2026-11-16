import threading

import pytest

from src import estado, panel, post_procesar


def _clase_final(tmp_path):
    d = tmp_path / "clase"
    d.mkdir()
    estado.escribir_estado(d, {
        "inicio": 1000.0,
        "estado": "fin",
        "parte": 1,
        "nombre": "Fixture consolidacion",
    })
    return d


@pytest.fixture(autouse=True)
def _restaurar_estado_post():
    with panel._POST_LOCK:
        panel._POST_CORRIENDO = False
        panel._POST_ERROR = ""
    panel.POST_LOG.clear()
    yield
    with panel._POST_LOCK:
        panel._POST_CORRIENDO = False
        panel._POST_ERROR = ""
    panel.POST_LOG.clear()


def test_consolidar_reserva_antes_del_worker_ante_requests_concurrentes(
        monkeypatch, tmp_path):
    d = _clase_final(tmp_path)
    monkeypatch.setattr(panel, "_clase", lambda: d)
    workers: list = []
    workers_lock = threading.Lock()

    def diferir_worker(target):
        with workers_lock:
            workers.append(target)

    monkeypatch.setattr(panel, "_iniciar_worker_post", diferir_worker)
    consolidaciones: list[tuple] = []
    monkeypatch.setattr(
        post_procesar,
        "consolidar_todo",
        lambda *args, **kwargs: consolidaciones.append((args, kwargs)),
    )

    barrera = threading.Barrier(3)
    respuestas: list[dict | None] = [None, None]

    def solicitar(indice):
        with panel.app.test_client() as client:
            barrera.wait()
            respuestas[indice] = client.post(
                "/api/consolidar",
                json={"confirmar_sin_evidencia": True},
            ).get_json()

    hilos = [threading.Thread(target=solicitar, args=(i,)) for i in range(2)]
    for hilo in hilos:
        hilo.start()
    barrera.wait()
    for hilo in hilos:
        hilo.join(timeout=5)

    assert all(not hilo.is_alive() for hilo in hilos)
    assert sorted(respuesta["ok"] for respuesta in respuestas if respuesta) == [False, True]
    assert len(workers) == 1
    assert panel._POST_CORRIENDO is True

    workers[0]()

    assert len(consolidaciones) == 1
    assert consolidaciones[0][1]["limpiar_al_final"] is True
    assert consolidaciones[0][1]["paquete_ligero"] is True
    assert panel._POST_CORRIENDO is False


def test_consolidar_libera_reserva_si_no_puede_iniciar_worker(monkeypatch, tmp_path):
    d = _clase_final(tmp_path)
    monkeypatch.setattr(panel, "_clase", lambda: d)
    monkeypatch.setattr(
        panel,
        "_iniciar_worker_post",
        lambda _target: (_ for _ in ()).throw(RuntimeError("sin hilos")),
    )

    respuesta = panel.app.test_client().post(
        "/api/consolidar",
        json={"confirmar_sin_evidencia": True},
    ).get_json()

    assert respuesta["ok"] is False
    assert "RuntimeError: sin hilos" in respuesta["msg"]
    assert panel._POST_CORRIENDO is False
    assert panel._POST_ERROR == "RuntimeError: sin hilos"


def test_consolidar_libera_reserva_en_finally_si_worker_falla(monkeypatch, tmp_path):
    d = _clase_final(tmp_path)
    monkeypatch.setattr(panel, "_clase", lambda: d)
    workers: list = []
    monkeypatch.setattr(panel, "_iniciar_worker_post", workers.append)
    monkeypatch.setattr(
        post_procesar,
        "consolidar_todo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("fallo sintetico")),
    )

    respuesta = panel.app.test_client().post(
        "/api/consolidar",
        json={"confirmar_sin_evidencia": True},
    ).get_json()
    assert respuesta["ok"] is True
    assert panel._POST_CORRIENDO is True

    workers[0]()

    assert panel._POST_CORRIENDO is False
    assert "ValueError" in panel._POST_ERROR
    assert "conservados" in panel._POST_ERROR
    assert "Consolidar" in panel._POST_ERROR
