import json
import builtins
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from src import estado


def _actualizar_en_otro_proceso(args: tuple[str, str, int]) -> None:
    ruta, campo, valor = args
    estado.actualizar_estado(Path(ruta), **{campo: valor})


class _ArchivoAppendSincronizado:
    """Orquesta la carrera reparación/append sin introducir sleeps."""

    def __init__(self, archivo, rol, segundo_leyo, primero_termino):
        self._archivo = archivo
        self._rol = rol
        self._segundo_leyo = segundo_leyo
        self._primero_termino = primero_termino
        self._sincronizado = False

    def __enter__(self):
        self._archivo.__enter__()
        return self

    def __exit__(self, *args):
        return self._archivo.__exit__(*args)

    def __getattr__(self, nombre):
        return getattr(self._archivo, nombre)

    def read(self, size=-1):
        data = self._archivo.read(size)
        if size > 1 and not self._sincronizado:
            self._sincronizado = True
            if self._rol == 1:
                self._segundo_leyo.wait(timeout=2)
            else:
                self._segundo_leyo.set()
                self._primero_termino.wait(timeout=2)
        return data


def _anexar_marca_en_carrera(args) -> None:
    ruta, indice, inicio, segundo_leyo, primero_termino = args
    original_open = builtins.open

    def abrir_sincronizado(archivo, mode="r", *open_args, **open_kwargs):
        handle = original_open(archivo, mode, *open_args, **open_kwargs)
        if Path(archivo).name == "marcas.jsonl" and mode == "a+b":
            return _ArchivoAppendSincronizado(
                handle, indice, segundo_leyo, primero_termino)
        return handle

    builtins.open = abrir_sincronizado
    estado.config.log_error = lambda *_args, **_kwargs: None
    try:
        inicio.wait(timeout=10)
        estado.anexar_jsonl(
            Path(ruta), {"t": indice + 1, "tipo": "evento", "indice": indice})
    finally:
        if indice == 1:
            primero_termino.set()
        builtins.open = original_open


def _reanudar_en_otro_proceso(ruta: str, puntero: str) -> None:
    estado.config.PUNTERO_CLASE = Path(puntero)
    estado.reanudar(Path(ruta))


def test_actualizaciones_multiproceso_no_pierden_campos(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    estado.escribir_estado(clase, {"estado": "grabando", "parte": 1})
    trabajos = [
        (str(clase), f"campo_{indice:02d}", indice)
        for indice in range(24)
    ]

    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_actualizar_en_otro_proceso, trabajos))

    final = estado.leer_estado(clase)
    assert final["estado"] == "grabando"
    assert final["parte"] == 1
    assert all(final[f"campo_{indice:02d}"] == indice for indice in range(24))
    assert not list(clase.glob(".session_state.*.tmp"))


def test_leer_marcas_ignora_solo_cola_truncada_y_conserva_utf8(
        monkeypatch, tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    marcas = clase / "marcas.jsonl"
    validas = [
        {"t": 1, "tipo": "inicio", "nota": "Álgebra"},
        {"t": 2, "tipo": "pausa", "nota": "receso"},
    ]
    raw = "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in validas).encode(
        "utf-8")
    marcas.write_bytes(raw + b'{"t":3,"tipo":"fin","nota":"\xc3')
    diagnosticos = []
    monkeypatch.setattr(estado.config, "log_error", lambda *args: diagnosticos.append(args))

    assert estado.leer_marcas(clase) == validas
    assert len(diagnosticos) == 1
    mensaje = str(diagnosticos[0][1])
    assert "fila=3" in mensaje
    assert "bytes=" in mensaje
    assert "nota" not in mensaje and "fin" not in mensaje


def test_leer_marcas_no_oculta_corrupcion_intermedia(monkeypatch, tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    marcas = clase / "marcas.jsonl"
    marcas.write_text(
        json.dumps({"t": 1, "tipo": "inicio"}) + "\n"
        + '{"t":2,"tipo":}' + "\n"
        + json.dumps({"t": 3, "tipo": "fin"}) + "\n",
        encoding="utf-8",
    )
    diagnosticos = []
    monkeypatch.setattr(estado.config, "log_error", lambda *args: diagnosticos.append(args))

    with pytest.raises(json.JSONDecodeError):
        estado.leer_marcas(clase)

    assert len(diagnosticos) == 1
    assert "fila=2" in str(diagnosticos[0][1])


def test_anexar_marca_descarta_cola_truncada_y_preserva_orden(
        monkeypatch, tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    marcas = clase / "marcas.jsonl"
    primera = {"t": 1, "tipo": "inicio", "nota": "válida"}
    ultima = {"t": 3, "tipo": "fin", "nota": "posterior"}
    marcas.write_text(
        json.dumps(primera, ensure_ascii=False) + "\n" + '{"t":2,"tipo":"pausa"',
        encoding="utf-8",
    )
    diagnosticos = []
    monkeypatch.setattr(estado.config, "log_error", lambda *args: diagnosticos.append(args))

    estado.anexar_jsonl(marcas, ultima)

    assert estado.leer_marcas(clase) == [primera, ultima]
    assert marcas.read_text(encoding="utf-8").endswith("\n")
    assert any("cola_truncada_descartada" in str(item[1]) for item in diagnosticos)


def test_reparacion_y_append_de_marcas_se_serializan_entre_procesos(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    marcas = clase / "marcas.jsonl"
    primera = {"t": 1, "tipo": "inicio", "nota": "válida"}
    marcas.write_text(
        json.dumps(primera, ensure_ascii=False) + "\n" + '{"t":2,"tipo":"pausa"',
        encoding="utf-8",
    )

    with multiprocessing.Manager() as manager:
        inicio = manager.Barrier(2)
        segundo_leyo = manager.Event()
        primero_termino = manager.Event()
        trabajos = [
            (str(marcas), indice, inicio, segundo_leyo, primero_termino)
            for indice in (1, 2)
        ]
        with ProcessPoolExecutor(max_workers=2) as pool:
            list(pool.map(_anexar_marca_en_carrera, trabajos))

    filas = estado.leer_marcas(clase)
    assert filas[0] == primera
    assert {fila["indice"] for fila in filas[1:]} == {1, 2}
    assert len(filas) == 3


def test_reanudar_no_reentra_el_lock_interproceso_de_marcas(tmp_path):
    clase = tmp_path / "clase"
    clase.mkdir()
    estado.escribir_estado(
        clase, {"estado": "fin", "parte": 1, "fuente": "sistema"})
    estado.anexar_jsonl(
        clase / "marcas.jsonl", {"t": 1, "tipo": "inicio", "parte": 1})
    estado.anexar_jsonl(
        clase / "marcas.jsonl", {"t": 2, "tipo": "fin", "parte": 1})

    contexto = multiprocessing.get_context("spawn")
    proceso = contexto.Process(
        target=_reanudar_en_otro_proceso,
        args=(str(clase), str(tmp_path / "clase_actual.txt")),
    )
    proceso.start()
    proceso.join(timeout=10)
    if proceso.is_alive():
        proceso.terminate()
        proceso.join(timeout=5)
        pytest.fail("reanudar quedó bloqueado al anexar marcas bajo el lock de sesión")

    assert proceso.exitcode == 0
    assert estado.leer_estado(clase)["parte"] == 2
    tipos = [fila["tipo"] for fila in estado.leer_marcas(clase)]
    assert tipos == ["inicio", "fin", "fin_parte", "reanudar_sesion"]
