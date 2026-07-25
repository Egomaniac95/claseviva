"""Máquina de estados de una clase: grabando / pausado / fin, partes y marcas.

Todo el IPC entre procesos (captura, transcriptor, escriba, copiloto) pasa por
`session_state.json` dentro de la carpeta de la clase — sin sockets ni colas.
"""
import json
import os
import re
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from . import config

ESTADOS = ("grabando", "pausado", "fin")
_JSONL_APPEND_LOCK = threading.Lock()
_STATE_THREAD_LOCK = threading.RLock()
_STATE_LOCK_TIMEOUT = 15.0


@contextmanager
def _bloqueo_estado(d: Path):
    """Serializa read-modify-write entre procesos sin dependencia externa."""
    lock_path = d / "session_state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_THREAD_LOCK, open(lock_path, "a+b", buffering=0) as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
        handle.seek(0)
        deadline = time.monotonic() + _STATE_LOCK_TIMEOUT
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timeout bloqueando session_state.json")
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _slug(nombre: str) -> str:
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "clase"


# --- Rutas derivadas de una clase (multi-curso): clases/<id> -> curso = 2 niveles arriba ---
def curso_de(d: Path) -> str:
    return d.parent.parent.name


def resumenes_de(d: Path) -> Path:
    p = d.parent.parent / "resumenes"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tareas_de(d: Path) -> Path:
    return d.parent.parent / "TAREAS.md"


def curso_md(d: Path) -> Path:
    """Bitácora acumulada del curso (temas vistos + notas del alumno). Se muestra en el
    panel, alimenta al copiloto y a la lección final, y el alumno puede añadir notas."""
    return d.parent.parent / "CURSO.md"


def crear_clase(nombre: str, semana: int | None = None,
                curso: str | None = None) -> Path:
    """Crea la estructura de una clase nueva (contenido/<curso>/clases/semana_NN_fecha_tema)."""
    curso = _slug(curso or config.CURSO_DEFAULT)
    clases_dir = config.dir_curso(curso) / "clases"
    clases_dir.mkdir(parents=True, exist_ok=True)
    (config.dir_curso(curso) / "resumenes").mkdir(exist_ok=True)
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if semana is None:  # auto: número de clase según carpetas existentes DEL CURSO
        semana = len([
            p for p in clases_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ]) + 1
    # Formato corto CLASE_NN_tema (la fecha/hora quedan en session_state.json -> "inicio")
    clase_id = f"CLASE_{semana:02d}_{_slug(nombre)}"
    d = clases_dir / clase_id
    if d.exists():
        clase_id = f"{clase_id}-{time.strftime('%H%M')}"
        d = clases_dir / clase_id
    (d / "audio").mkdir(parents=True)
    (d / "img").mkdir()
    st = {
        "clase_id": clase_id,
        "nombre": nombre,
        "curso": curso,
        "semana": semana,
        "estado": "grabando",
        "parte": 1,
        "origen": "vivo",
        "inicio": time.time(),
        "auto_pausa": False,   # silencio prolongado detectado (break del profesor)
        "auto_pausa_motivo": "",
        "visual_idle": {},
        "monitor": None,       # override del monitor de captura (None = config/env)
        "fuente": "sistema",   # de dónde sale el audio: chrome | msedge | sistema
        "dispositivo": "",     # loopback WASAPI preferido (vacio = salida default)
        "audio_modo": "",      # diagnostico: proceso | dispositivo | fallback
        "audio_ultimo_estado": "",  # activo | silencio | captura_rota; vacío = desconocido
        "audio_silencio_chunks": 0,
        "audio_capture_phase": "initializing",
        "audio_valid_wav_count": 0,
        "audio_requires_attention": False,
        "audio_attention_reason": "",
        "audio_retry_generation": 0,
        "velocidad": 1.0,      # 1.5 / 2.0 si se reproduce una GRABACIÓN acelerada
        "enfoque": False,      # True = las demás apps están silenciadas (pycaw)
        "resumir": False,      # True = el usuario pidió un bloque de apuntes (botón 📌)
        "region": None,        # [left,top,width,height] absoluto: recorte al video del profesor
        "apuntes_auto": 5,     # minutos de captura útil entre resúmenes (0 = solo manual)
        "resumen_clock": "captura_util",  # las pausas no consumen el intervalo pedagógico
    }
    escribir_estado(d, st)
    apuntar_clase(d)
    marcar(d, "inicio", nota=nombre)
    return d


def hidratar_clase_compactada(
        d: Path, *, ahora: float | None = None) -> dict:
    """Reconstruye IPC temporal desde la metadata embebida en la lección.

    Una clase cerrada no necesita conservar WAV, transcript ni manifests. Al
    continuarla, este método recrea únicamente el estado y las marcas históricas
    mínimas; ``reanudar`` abrirá después la parte siguiente.
    """
    from . import study_package

    d = Path(d)
    study_package.recover_interrupted_cleanup(d)
    if (d / "session_state.json").is_file():
        return leer_estado(d)
    if (d / "marcas.jsonl").exists():
        raise RuntimeError(
            "hay marcas históricas sin session_state.json; no se sobrescriben")
    metadata = study_package.read_metadata(d)
    if metadata is None:
        raise RuntimeError(
            "leccion_final.md no contiene metadata reanudable; "
            "vuelve a consolidarla con esta versión antes de compactar")

    class_meta = dict(metadata.get("class") or {})
    raw_parts = list(metadata.get("recording_parts") or [])
    parts: list[dict] = []
    for raw in raw_parts:
        try:
            number = int(raw.get("part", 0))
            useful_start = float(raw.get("useful_start_sec", 0.0))
            useful_end = float(raw.get("useful_end_sec", useful_start))
        except (AttributeError, TypeError, ValueError):
            continue
        if number <= 0 or useful_end < useful_start:
            continue
        parts.append({
            **dict(raw),
            "part": number,
            "useful_start_sec": useful_start,
            "useful_end_sec": useful_end,
        })
    parts.sort(key=lambda item: item["part"])
    if not parts:
        raise RuntimeError("metadata reanudable sin bloques de grabación válidos")

    instante = time.time() if ahora is None else float(ahora)
    try:
        started_at = float(class_meta.get("started_at") or 0.0)
    except (TypeError, ValueError):
        started_at = 0.0
    if started_at <= 0:
        try:
            started_at = float(parts[0].get("wall_start") or 0.0)
        except (TypeError, ValueError):
            started_at = 0.0
    if started_at <= 0:
        started_at = instante - float(parts[-1]["useful_end_sec"])

    marks: list[dict] = []
    previous_end = started_at
    for index, part in enumerate(parts):
        fallback_start = started_at + float(part["useful_start_sec"])
        fallback_end = started_at + float(part["useful_end_sec"])
        try:
            wall_start = float(part.get("wall_start") or fallback_start)
            wall_end = float(part.get("wall_end") or fallback_end)
        except (TypeError, ValueError):
            wall_start, wall_end = fallback_start, fallback_end
        if wall_start < previous_end or wall_end < wall_start:
            wall_start, wall_end = max(previous_end, fallback_start), fallback_end
        wall_end = max(wall_start, wall_end)
        number = int(part["part"])
        if index == 0:
            marks.append({
                "t": wall_start, "tipo": "inicio", "parte": number,
                "nota": str(class_meta.get("name") or d.name),
            })
        else:
            marks.append({
                "t": wall_start, "tipo": "reanudar_sesion", "parte": number,
                "nota": "restaurado desde paquete de estudio",
            })
        marks.append({
            "t": wall_end, "tipo": "fin", "parte": number,
            "nota": "cierre restaurado desde paquete de estudio",
        })
        if index < len(parts) - 1:
            marks.append({
                "t": wall_end, "tipo": "fin_parte", "parte": number,
                "nota": "límite restaurado desde paquete de estudio",
            })
        previous_end = wall_end

    try:
        semana = int(class_meta.get("week") or 1)
    except (TypeError, ValueError):
        semana = 1
    state = {
        "clase_id": str(class_meta.get("class_id") or d.name),
        "nombre": str(class_meta.get("name") or d.name),
        "curso": str(class_meta.get("course") or curso_de(d)),
        "semana": semana,
        "estado": "fin",
        "parte": int(parts[-1]["part"]),
        "origen": str(class_meta.get("origin") or "vivo"),
        "inicio": started_at,
        "auto_pausa": False,
        "auto_pausa_motivo": "",
        "visual_idle": {},
        "monitor": None,
        "fuente": "sistema",
        "dispositivo": "",
        "audio_modo": "",
        "audio_ultimo_estado": "cerrado",
        "audio_silencio_chunks": 0,
        "audio_capture_phase": "closed",
        "audio_valid_wav_count": 0,
        "audio_requires_attention": False,
        "audio_attention_reason": "",
        "audio_retry_generation": 0,
        "velocidad": 1.0,
        "enfoque": False,
        "resumir": False,
        "region": None,
        "apuntes_auto": 5,
        "resumen_clock": "captura_util",
        "shutdown_state": "finalizada",
        "hydrated_from_study_package": True,
        "hydrated_at": instante,
    }
    (d / "audio").mkdir(parents=True, exist_ok=True)
    with _bloqueo_estado(d):
        _escribir_estado_bloqueado(d, state)
        for mark in marks:
            _anexar_jsonl_local(d / "marcas.jsonl", mark, durable=True)
    return leer_estado(d)


def apuntar_clase(d: Path) -> None:
    """Guarda el puntero RELATIVO a la raíz: mover la carpeta clase-viva no rompe nada."""
    try:
        ruta = d.relative_to(config.ROOT)
    except ValueError:
        ruta = d
    config.PUNTERO_CLASE.write_text(str(ruta), encoding="utf-8")


def clase_activa() -> Path | None:
    if not config.PUNTERO_CLASE.is_file():
        return None
    # utf-8-sig: tolera BOM; rutas relativas se resuelven contra la raíz (portabilidad)
    p = Path(config.PUNTERO_CLASE.read_text(encoding="utf-8-sig").strip())
    if not p.is_absolute():
        p = config.ROOT / p
    return p if (p / "session_state.json").is_file() else None


def leer_estado(d: Path) -> dict:
    path = d / "session_state.json"
    ultimo_error: OSError | json.JSONDecodeError | None = None
    for _ in range(4):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            ultimo_error = error
            time.sleep(0.01)
    assert ultimo_error is not None
    raise ultimo_error


def _escribir_estado_bloqueado(d: Path, st: dict) -> None:
    destino = d / "session_state.json"
    tmp = d / (
        f".session_state.{os.getpid()}.{threading.get_ident()}.tmp")
    raw = json.dumps(st, ensure_ascii=False, indent=2)
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, destino)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def escribir_estado(d: Path, st: dict) -> None:
    """Publica un estado completo bajo bloqueo interproceso."""
    with _bloqueo_estado(d):
        _escribir_estado_bloqueado(d, dict(st))


def actualizar_estado(d: Path, **campos) -> dict:
    """Fusiona campos sin perder actualizaciones concurrentes de otros procesos."""
    with _bloqueo_estado(d):
        st = leer_estado(d)
        st.update(campos)
        _escribir_estado_bloqueado(d, st)
        return st


def mutar_estado(d: Path, mutador) -> dict:
    """Aplica una mutación dependiente del valor actual bajo el mismo lock."""
    with _bloqueo_estado(d):
        st = leer_estado(d)
        resultado = mutador(dict(st))
        nuevo = st if resultado is None else dict(resultado)
        _escribir_estado_bloqueado(d, nuevo)
        return nuevo


def cambiar_estado(d: Path, nuevo: str) -> dict:
    assert nuevo in ESTADOS, f"estado inválido: {nuevo}"
    return actualizar_estado(d, estado=nuevo)


def estado_efectivo(st: dict) -> str:
    """Interpreta en un solo lugar el estado operativo compuesto de la sesion."""
    estado = str(st.get("estado") or "")
    if estado == "fin":
        return "finalizado"
    if estado == "pausado":
        return "pausado_manual"
    if bool(st.get("audio_requires_attention")):
        return "audio_requires_attention"
    if bool(st.get("auto_pausa")):
        return "auto_pausado"
    if estado == "grabando":
        return "grabando_activo"
    return "desconocido"


def capacidades_estado(st: dict) -> dict[str, object]:
    """Politica canonica de controles; la UI solo debe representarla."""
    efectivo = estado_efectivo(st)
    finalizado = efectivo == "finalizado"
    reanudable = efectivo in {
        "pausado_manual", "auto_pausado", "audio_requires_attention"}
    return {
        "estado_efectivo": efectivo,
        "pausar": efectivo in {
            "grabando_activo", "auto_pausado", "audio_requires_attention"},
        "seguir": reanudable,
        "reintentar_audio": efectivo == "audio_requires_attention",
        "cerrar_parte": efectivo == "grabando_activo",
        "finalizar": not finalizado and efectivo != "desconocido",
        "seguir_label": (
            "Reanudar / probar audio"
            if efectivo == "audio_requires_attention" else "Seguir"),
    }


def almacenando(st: dict) -> bool:
    """True solo si se debe estar guardando información AHORA (ni pausa manual ni auto-pausa)."""
    return estado_efectivo(st) == "grabando_activo"


def capturando_visual(st: dict) -> bool:
    """La atencion de audio no apaga la evidencia visual conservadora."""
    return estado_efectivo(st) in {
        "grabando_activo", "auto_pausado", "audio_requires_attention"}


def escuchando_audio(st: dict) -> bool:
    """True si el capturador puede intentar audio sin una accion manual previa."""
    return estado_efectivo(st) in {"grabando_activo", "auto_pausado"}


def _marca_transicion(st: dict, tipo: str, ahora: float, nota: str) -> dict:
    return {
        "t": float(ahora),
        "tipo": tipo,
        "parte": st.get("parte", 0),
        "nota": nota,
    }


def transicionar_captura(
        d: Path, accion: str, *, ahora: float | None = None,
        nota: str = "", actualizaciones: dict | None = None) -> dict:
    """Serializa estado+marca para pausa, reanudacion, atencion y fin.

    La exclusion interproceso vuelve idempotentes los dobles clics. Los dos
    archivos siguen siendo artefactos separados, pero ningun otro escritor de
    esta API puede intercalarse entre la marca durable y el estado publicado.
    """
    aliases = {
        "pausa": "pausa",
        "pausa_manual": "pausa",
        "seguir": "seguir",
        "reanudar": "seguir",
        "fin": "fin",
        "audio_atencion": "audio_atencion",
        "audio_attention": "audio_atencion",
        "auto_pausa": "auto_pausa",
        "auto_seguir": "auto_seguir",
    }
    accion_normalizada = aliases.get(str(accion or "").strip().lower())
    if accion_normalizada is None:
        raise ValueError(f"transicion de captura invalida: {accion}")
    instante = time.time() if ahora is None else float(ahora)
    extras = dict(actualizaciones or {})

    with _bloqueo_estado(d):
        st = leer_estado(d)
        anterior = estado_efectivo(st)
        changed = False
        marca_tipo = ""
        mensaje = "sin cambio"

        if accion_normalizada == "pausa":
            if anterior == "finalizado":
                mensaje = "La clase ya esta finalizada."
            elif anterior == "pausado_manual":
                mensaje = "La clase ya esta pausada."
            else:
                st.update(
                    estado="pausado",
                    auto_pausa=False,
                    auto_pausa_motivo="",
                    audio_capture_phase="manual_paused",
                )
                changed = True
                marca_tipo = "pausa"
                mensaje = "Pausa manual confirmada."
        elif accion_normalizada == "seguir":
            if anterior == "finalizado":
                mensaje = "La clase ya esta finalizada."
            elif anterior == "grabando_activo":
                mensaje = "La clase ya esta grabando activamente."
            else:
                try:
                    generacion = int(st.get("audio_retry_generation") or 0)
                except (TypeError, ValueError):
                    generacion = 0
                st.update(
                    estado="grabando",
                    auto_pausa=False,
                    auto_pausa_motivo="",
                    visual_idle={},
                    audio_requires_attention=False,
                    audio_attention_reason="",
                    audio_warning="",
                    audio_diag="",
                    audio_ultimo_estado="inicializando",
                    audio_capture_phase="initializing",
                    audio_fallback_transition="manual_retry",
                    audio_retry_generation=generacion + 1,
                    audio_retry_requested_at=instante,
                )
                changed = True
                marca_tipo = "seguir"
                mensaje = "Reanudacion confirmada; se probara una nueva ruta de audio."
        elif accion_normalizada == "fin":
            if anterior == "finalizado":
                mensaje = "La clase ya esta finalizada."
            else:
                st.update(
                    estado="fin",
                    enfoque=False,
                    auto_pausa=False,
                    auto_pausa_motivo="",
                    audio_requires_attention=False,
                    audio_capture_phase="finalizing",
                    shutdown_state="finalizando",
                    shutdown_requested_at=instante,
                )
                changed = True
                marca_tipo = "fin"
                mensaje = "Clase finalizada."
        elif accion_normalizada == "audio_atencion":
            if anterior == "finalizado":
                mensaje = "La clase ya esta finalizada."
            elif anterior == "pausado_manual":
                mensaje = "La pausa manual conserva prioridad."
            elif anterior == "audio_requires_attention":
                mensaje = "La atencion de audio ya estaba registrada."
            else:
                st.update(
                    estado="grabando",
                    auto_pausa=False,
                    auto_pausa_motivo="",
                    audio_requires_attention=True,
                    audio_attention_since=instante,
                    audio_capture_phase="requires_attention",
                )
                changed = True
                marca_tipo = "audio_requires_attention"
                mensaje = "La captura de audio requiere atencion."
        elif accion_normalizada == "auto_pausa":
            if anterior == "grabando_activo":
                st.update(
                    auto_pausa=True,
                    auto_pausa_motivo=nota or "silencio",
                    audio_capture_phase="auto_paused",
                )
                changed = True
                marca_tipo = "auto_pausa"
                mensaje = "Auto-pausa confirmada."
            else:
                mensaje = "La auto-pausa ya no aplica al estado actual."
        elif accion_normalizada == "auto_seguir":
            if anterior == "auto_pausado":
                st.update(
                    auto_pausa=False,
                    auto_pausa_motivo="",
                    visual_idle={},
                    audio_warning="",
                    audio_diag="",
                    audio_capture_phase="initializing",
                )
                changed = True
                marca_tipo = "auto_seguir"
                mensaje = "El audio reanudo la captura automaticamente."
            else:
                mensaje = "La sesion no estaba en auto-pausa."

        if extras:
            st.update(extras)
        if marca_tipo:
            _anexar_jsonl_local(
                d / "marcas.jsonl",
                _marca_transicion(st, marca_tipo, instante, nota),
                durable=True,
            )
        if changed or extras:
            _escribir_estado_bloqueado(d, st)

        efectivo = estado_efectivo(st)
        return {
            "changed": changed,
            "effective_state": efectivo,
            "estado_efectivo": efectivo,
            "state": dict(st),
            "message": mensaje,
            "msg": mensaje,
        }


def intervalos_captura_util(
        st: dict, marcas: list[dict], ahora: float | None = None,
        *, desde: float | None = None) -> list[tuple[float, float]]:
    """Reconstruye captura util y tolera marcas duplicadas de formatos legacy."""
    instante = time.time() if ahora is None else float(ahora)
    try:
        inicio = float(st.get("inicio", instante) or instante)
    except (TypeError, ValueError):
        inicio = instante
    fines = []
    for marca in marcas:
        if marca.get("tipo") != "fin":
            continue
        try:
            fines.append(float(marca.get("t")))
        except (TypeError, ValueError):
            continue
    fin_pared = max(fines) if estado_efectivo(st) == "finalizado" and fines else instante
    fin_pared = max(inicio, fin_pared)
    cerrar_tipos = {
        "pausa", "auto_pausa", "audio_atencion",
        "audio_attention", "audio_requires_attention", "fin",
    }
    abrir_tipos = {
        "inicio", "seguir", "auto_seguir", "reanudar_sesion", "audio_reintento",
    }
    ordenadas: list[tuple[float, int, dict]] = []
    for indice, marca in enumerate(marcas):
        try:
            t = float(marca.get("t", inicio))
        except (TypeError, ValueError):
            continue
        if inicio <= t <= fin_pared:
            ordenadas.append((t, indice, marca))
    ordenadas.sort(key=lambda item: (item[0], item[1]))

    intervalos: list[tuple[float, float]] = []
    activo = True
    inicio_activo = inicio
    for t, _indice, marca in ordenadas:
        tipo = str(marca.get("tipo") or "")
        if tipo in cerrar_tipos and activo:
            intervalos.append((inicio_activo, max(inicio_activo, t)))
            activo = False
        elif tipo in abrir_tipos and not activo:
            activo = True
            inicio_activo = max(inicio, t)
    if activo and estado_efectivo(st) in {"grabando_activo", "finalizado"}:
        intervalos.append((inicio_activo, max(inicio_activo, fin_pared)))

    limite = inicio if desde is None else max(inicio, float(desde))
    return [
        (max(ini, limite), fin)
        for ini, fin in intervalos
        if fin > max(ini, limite)
    ]


def metricas_tiempo(
        st: dict, marcas: list[dict], ahora: float | None = None) -> dict[str, float]:
    """Separa pared, captura util, pausa y equivalencia del video."""
    instante = time.time() if ahora is None else float(ahora)
    try:
        inicio = float(st.get("inicio", instante) or instante)
    except (TypeError, ValueError):
        inicio = instante
    fines = []
    for marca in marcas:
        if marca.get("tipo") != "fin":
            continue
        try:
            fines.append(float(marca.get("t")))
        except (TypeError, ValueError):
            continue
    fin = max(fines) if estado_efectivo(st) == "finalizado" and fines else instante
    elapsed = max(0.0, fin - inicio)
    recorded = sum(
        max(0.0, tramo_fin - tramo_inicio)
        for tramo_inicio, tramo_fin in intervalos_captura_util(st, marcas, fin)
    )
    recorded = min(elapsed, max(0.0, recorded))
    try:
        velocidad = float(st.get("velocidad", 1.0) or 1.0)
    except (TypeError, ValueError):
        velocidad = 1.0
    if velocidad <= 0:
        velocidad = 1.0
    return {
        "elapsed_wall_sec": elapsed,
        "recorded_elapsed_sec": recorded,
        "paused_elapsed_sec": max(0.0, elapsed - recorded),
        "video_equivalent_elapsed_sec": recorded * velocidad,
    }


def marcar(d: Path, tipo: str, nota: str = "") -> None:
    """Append a marcas.jsonl: inicio | pausa | seguir | fin_parte | fin | reanudar_sesion."""
    with _bloqueo_estado(d):
        st = leer_estado(d) if (d / "session_state.json").is_file() else {"parte": 0}
        linea = {
            "t": time.time(),
            "tipo": tipo,
            "parte": st.get("parte", 0),
            "nota": nota,
        }
        _anexar_jsonl_local(d / "marcas.jsonl", linea, durable=True)


def leer_marcas(d: Path) -> list[dict]:
    p = d / "marcas.jsonl"
    if not p.is_file():
        return []
    raw = p.read_bytes()
    lineas = raw.split(b"\n")
    termina_en_nueva_linea = raw.endswith(b"\n")
    out = []
    for indice, linea_raw in enumerate(lineas, 1):
        if indice == len(lineas) and termina_en_nueva_linea and not linea_raw:
            continue
        linea_raw = linea_raw.rstrip(b"\r").strip()
        if not linea_raw:
            continue
        try:
            linea = linea_raw.decode("utf-8")
            out.append(json.loads(linea))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            es_cola_truncada = (
                indice == len(lineas) and not termina_en_nueva_linea)
            estado_issue = (
                "cola_truncada_ignorada" if es_cola_truncada
                else "corrupcion_intermedia")
            try:
                config.log_error(
                    "estado",
                    f"marcas_jsonl_{estado_issue}: archivo={p.name} "
                    f"fila={indice} bytes={len(linea_raw)} "
                    f"error={type(error).__name__}",
                )
            except Exception:
                pass
            if es_cola_truncada:
                break
            raise
    return out


def rangos_partes(d: Path) -> list[tuple[int, float, float]]:
    """[(parte, t_inicio, t_fin), ...] a partir de las marcas. t_fin de la última = ahora/fin."""
    marcas = leer_marcas(d)
    st = leer_estado(d)
    t0 = st.get("inicio", marcas[0]["t"] if marcas else time.time())
    cortes = [m["t"] for m in marcas if m["tipo"] == "fin_parte"]
    reanudaciones: dict[int, float] = {}
    for marca in marcas:
        if marca.get("tipo") != "reanudar_sesion":
            continue
        try:
            parte = int(marca.get("parte", 0) or 0)
            instante = float(marca["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if parte > 1:
            reanudaciones[parte] = instante
    # ÚLTIMO fin (no el primero): una clase puede cerrarse y REANUDARSE como parte siguiente
    fines = [m["t"] for m in marcas if m["tipo"] == "fin"]
    t_final = fines[-1] if fines else time.time()
    rangos, ini = [], t0
    for i, c in enumerate(cortes, start=1):
        rangos.append((i, ini, c))
        # Una continuacion puede ocurrir horas o dias despues. La parte siguiente
        # empieza en su marca explicita, no en el cierre historico de la anterior.
        # El fallback al corte conserva manifests legacy sin reanudar_sesion.
        ini = reanudaciones.get(i + 1, c)
    rangos.append((len(cortes) + 1, ini, t_final))
    return rangos


def reanudar(d: Path) -> dict:
    """Retoma una clase. Si estaba en 'fin', abre la PARTE siguiente de la misma lección."""
    from . import study_package

    study_package.recover_interrupted_cleanup(d)
    with _bloqueo_estado(d):
        st = leer_estado(d)
        if estado_efectivo(st) == "grabando_activo":
            apuntar_clase(d)
            return st
        if st["estado"] == "fin":
            fines = [m["t"] for m in leer_marcas(d) if m["tipo"] == "fin"]
            if fines:  # cerrar la parte anterior EXACTAMENTE donde terminó
                _anexar_jsonl_local(
                    d / "marcas.jsonl",
                    {"t": fines[-1], "tipo": "fin_parte",
                     "parte": st["parte"],
                     "nota": "cierre de la sesión anterior"},
                )
            st["parte"] += 1
        try:
            generacion = int(st.get("audio_retry_generation") or 0)
        except (TypeError, ValueError):
            generacion = 0
        instante = time.time()
        st.update(
            estado="grabando", auto_pausa=False,
            auto_pausa_motivo="", visual_idle={},
            audio_requires_attention=False, audio_attention_reason="",
            audio_warning="", audio_diag="", resumir=False, audio_modo="",
            audio_ultimo_estado="inicializando",
            audio_capture_phase="initializing",
            audio_fallback_transition="manual_retry",
            audio_retry_generation=generacion + 1,
            audio_retry_requested_at=instante,
        )
        _anexar_jsonl_local(
            d / "marcas.jsonl",
            _marca_transicion(st, "reanudar_sesion", instante, ""),
            durable=True,
        )
        _escribir_estado_bloqueado(d, st)
    apuntar_clase(d)
    return leer_estado(d)


def leer_jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue  # línea a medias durante escritura concurrente
    return out


def anexar_jsonl(p: Path, obj: dict, durable: bool = False) -> None:
    """Anexa UTF-8; marcas serializa reparación+append entre procesos."""
    if p.name == "marcas.jsonl":
        with _bloqueo_estado(p.parent):
            _anexar_jsonl_local(p, obj, durable=durable)
        return
    _anexar_jsonl_local(p, obj, durable=durable)


def _anexar_jsonl_local(p: Path, obj: dict, durable: bool = False) -> None:
    """Implementación local; quien llama debe poseer el lock interproceso si aplica."""
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _JSONL_APPEND_LOCK:
        with open(p, "a+b") as f:
            f.seek(0, os.SEEK_END)
            if f.tell():
                fin = f.tell()
                f.seek(-1, os.SEEK_END)
                sin_nueva_linea = f.read(1) != b"\n"
                if sin_nueva_linea and p.name == "marcas.jsonl":
                    f.seek(0)
                    existente = f.read(fin)
                    inicio_cola = existente.rfind(b"\n") + 1
                    cola = existente[inicio_cola:].rstrip(b"\r").strip()
                    cola_valida = False
                    try:
                        cola_valida = bool(cola) and isinstance(
                            json.loads(cola.decode("utf-8")), dict)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        cola_valida = False
                    if not cola_valida:
                        f.seek(inicio_cola)
                        f.truncate()
                        try:
                            config.log_error(
                                "estado",
                                "marcas_jsonl_cola_truncada_descartada: "
                                f"archivo={p.name} fila_bytes={len(cola)}",
                            )
                        except Exception:
                            pass
                    else:
                        f.seek(0, os.SEEK_END)
                        f.write(b"\n")
                elif sin_nueva_linea:
                    f.seek(0, os.SEEK_END)
                    f.write(b"\n")
            f.seek(0, os.SEEK_END)
            f.write(raw)
            f.flush()
            if durable:
                os.fsync(f.fileno())
