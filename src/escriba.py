"""EL ESCRIBA: construye la lección Markdown EN VIVO bajo demanda o cada ~5 min.

Por cada bloque toma: transcript nuevo + capturas nuevas (+OCR) -> DeepSeek V4 Flash
-> anexa a leccion_en_vivo.md un bloque de apuntes, conceptos, código explicado y enlaces.
Las IMÁGENES las inserta este script de forma determinista (el LLM nunca inventa rutas).
Si la API falla, guarda el transcript crudo del bloque: NUNCA se pierde información.

Test (1 llamada real a la API):  python -m src.escriba --test
"""
import bisect
import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path

from . import audio_manifest
from . import config
from . import estado as est
from . import incremental_jsonl
from . import local_interventions
from . import ocr
from . import student_notes
from .deepseek_client import preguntar, preguntar_detalle

RE_LINK = re.compile(r"https?://[^\s\)\]\}\"']+")

PROMPT_SISTEMA = (
    "Eres el Escriba pedagógico de una clase universitaria. Recibes únicamente evidencia "
    "ya registrada: un estado breve de lo visto antes, la transcripción del tramo nuevo y "
    "OCR de pantallas. La clase puede estar en español, inglés o ser bilingüe. Produce SOLO "
    "Markdown en español, manteniendo términos técnicos en su idioma original cuando ayude. "
    "No anticipes lo que el profesor dirá ni completes silencios con suposiciones. El "
    "estudiante debe poder pausar, leer este bloque y retomar la sesión con las bases "
    "necesarias. Usa estas secciones y omite solo las que de verdad queden vacías:\n"
    "### Panorama acumulado de la clase\n(introducción clara de todo lo visto hasta ahora; "
    "actualiza el panorama anterior con lo nuevo sin convertirlo en una lista telegráfica)\n"
    "### Desarrollo detallado del tramo actual\n(explica a fondo la evidencia nueva: conceptos, "
    "razonamiento, pasos, ejemplos, fórmulas y decisiones)\n"
    "### Contexto acumulado para continuar\n(recuerda con detalle medio las definiciones, "
    "relaciones y procedimientos previos indispensables para comprender el tramo actual y "
    "seguir la clase; no copies literalmente el panorama)\n"
    "### Conceptos y procedimientos clave\n(- **término o paso**: definición, función y "
    "relación con el tema)\n"
    "### Código o procedimiento\n(si aparece: reconstruye solo lo respaldado, corrige OCR "
    "obvio y explica cada paso)\n"
    "### Comprueba tu comprensión\n(1-2 preguntas de recuperación cuya respuesta esté en "
    "la evidencia recibida)\n"
    "### Dudas o correcciones\n(ambigüedades, cobertura insuficiente o corrección explícita "
    "del profesor; no corrijas silenciosamente)\n"
    "### Enlaces y referencias\n(solo URLs, papers o libros realmente mencionados)\n"
    "### Fuera de guion\n(si el profesor divagó, consérvalo separado)\n"
    "⚠️ Un cambio de tema (aunque sea a otro tema o de otro profesor) es contenido LEGÍTIMO: "
    "desarróllalo, no lo descartes. Solo trata como contaminación el audio claramente de OTRA "
    "fuente sonando a la vez (outros de YouTube 'suscríbete/nos vemos', música, anuncios); si "
    "de verdad lo hay, una sola línea al final: '🗑️ Posible contaminación: <resumen breve>'.\n"
    "No agregues encabezados de nivel 1 ni 2. No repitas la transcripción literal completa, "
    "pero conserva todos los datos técnicos, pasos, ejemplos y correcciones relevantes.\n"
    "AL FINAL, en una línea aparte y sola, escribe exactamente: BUSCAR: <3-5 palabras clave "
    "en inglés del concepto central> (la usamos para adjuntar artículos reales de la web)."
)

MAX_TOKENS_ESCRIBA = int(os.environ.get("CLASE_ESCRIBA_MAX_TOKENS", "3600"))
RESUMEN_TRAMOS_JSONL = "resumen_tramos.jsonl"
RE_IMG_MD_LOCAL = re.compile(r"!\[[^\]]*\]\([^)]*\)")
AUTO_RESUMEN_SEG = 5 * 60.0
# Techo de espera: cubre el peor caso (un chunk que cruza la frontera mas su
# commit). Con STT remoto la evidencia suele cerrarse mucho antes; el disparo
# por cobertura confirmada usa este valor solo como respaldo.
MARGEN_TRANSCRIPTOR_SEG = 45.0
# El pipeline visual anexa su fila a img_log despues del OCR/JPEG del ciclo en
# curso: una holgura corta evita adelantarse a una captura ya decidida.
VISUAL_ASENTAMIENTO_SEG = float(
    os.environ.get("CLASE_ESCRIBA_ASENTAMIENTO_VISUAL_SEG", "5"))
DISPAROS_RESUMEN = frozenset({"manual", "auto", "fin"})
ESTADO_ESCRIBA_SCHEMA_VERSION = 5
MAX_CAPTURAS_RESUMEN = 8
AUTO_EMPTY_GRACE_SEG = 120.0
MAX_CONTEXTO_PEDAGOGICO_CHARS = 6000
MAX_TRANSCRIPT_RESUMEN_CHARS = 12000
MAX_OCR_RESUMEN_CHARS = 5000
MAX_PROMPT_USUARIO_CHARS = (
    MAX_CONTEXTO_PEDAGOGICO_CHARS + MAX_TRANSCRIPT_RESUMEN_CHARS
    + MAX_OCR_RESUMEN_CHARS + 1200
)


def _evidence_path_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (
        int(stat_result.st_mtime_ns), int(stat_result.st_size),
        int(getattr(stat_result, "st_ino", 0)),
    )


class _TranscriptWindowIndex:
    """Indice temporal de transcript alimentado por commits STT inmutables.

    ``transcript.jsonl`` es una proyeccion atomica y por ello cambia de identidad
    con cada commit. Tras reconstruirla una vez, este indice descubre archivos de
    ``transcript_chunks`` nuevos y agrega solo sus lineas. Una modificacion legacy
    sin commit o una mutacion de un commit fuerza reconstruccion conservadora.
    """

    def __init__(self, class_dir: Path):
        self.class_dir = Path(class_dir)
        self.transcript = self.class_dir / "transcript.jsonl"
        self.journal = self.class_dir / audio_manifest.JOURNAL_DIR
        self._lock = threading.RLock()
        self._times: list[float] = []
        self._rows: list[dict] = []
        self._seen_commits: dict[str, tuple[int, int, int] | None] = {}
        self._transcript_signature = None
        self._journal_signature = None
        self._loaded = False
        self.rebuilds = 0
        self.commit_files_read = 0
        self.rows_appended = 0
        self.cache_hits = 0

    @staticmethod
    def _timestamp(row: dict) -> float | None:
        for field in ("t", "t_rel"):
            try:
                value = float(row.get(field))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return value
        return None

    @staticmethod
    def _chunk_id(row: dict) -> str:
        return Path(str(row.get("chunk") or "")).stem

    def _commit_paths(self) -> dict[str, tuple[Path, tuple[int, int, int] | None]]:
        if not self.journal.is_dir():
            return {}
        return {
            path.name: (path, _evidence_path_signature(path))
            for path in self.journal.glob("*.json")
            if path.is_file()
        }

    def _read_commit(self, path: Path) -> list[dict] | None:
        self.commit_files_read += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("status") != "complete":
            return None
        if str(payload.get("chunk_id") or "") != path.stem:
            return None
        lines = payload.get("lines") if "lines" in payload else payload.get("segments")
        if not isinstance(lines, list) or any(not isinstance(row, dict) for row in lines):
            return None
        return [dict(row) for row in lines]

    def _replace_rows(self, rows: list[dict]) -> None:
        entries = []
        for order, row in enumerate(rows):
            timestamp = self._timestamp(row)
            if timestamp is not None:
                entries.append((timestamp, order, row))
        entries.sort(key=lambda item: (item[0], item[1]))
        self._times = [item[0] for item in entries]
        self._rows = [item[2] for item in entries]

    def _append_rows(self, rows: list[dict]) -> None:
        for row in rows:
            timestamp = self._timestamp(row)
            if timestamp is None:
                continue
            position = bisect.bisect_right(self._times, timestamp)
            self._times.insert(position, timestamp)
            self._rows.insert(position, row)
            self.rows_appended += 1

    def _rebuild(self) -> None:
        projection = list(incremental_jsonl.read_jsonl(self.transcript))
        commits = self._commit_paths()
        present = {self._chunk_id(row) for row in projection if self._chunk_id(row)}
        for name, (path, _signature) in sorted(commits.items()):
            if path.stem in present:
                continue
            lines = self._read_commit(path)
            if lines is not None:
                projection.extend(lines)
        self._replace_rows(projection)
        self._seen_commits = {name: signature for name, (_path, signature) in commits.items()}
        self._transcript_signature = _evidence_path_signature(self.transcript)
        self._journal_signature = _evidence_path_signature(self.journal)
        self._loaded = True
        self.rebuilds += 1

    def refresh(self) -> None:
        with self._lock:
            transcript_signature = _evidence_path_signature(self.transcript)
            journal_signature = _evidence_path_signature(self.journal)
            if not self._loaded:
                self._rebuild()
                return
            if (
                transcript_signature == self._transcript_signature
                and journal_signature == self._journal_signature
            ):
                self.cache_hits += 1
                return

            commits = self._commit_paths()
            current = {name: signature for name, (_path, signature) in commits.items()}
            known_names = set(self._seen_commits)
            if (
                not known_names.issubset(current)
                or any(current[name] != self._seen_commits[name] for name in known_names)
            ):
                self._rebuild()
                return
            new_names = sorted(set(current) - known_names)
            if new_names:
                for name in new_names:
                    path, _signature = commits[name]
                    lines = self._read_commit(path)
                    if lines is None:
                        self._rebuild()
                        return
                    self._append_rows(lines)
                self._seen_commits = current
                # El commit se publica antes que la proyeccion. Guardar la firma
                # observada permite detectar y reconstruir esa rara carrera luego.
                self._transcript_signature = transcript_signature
                self._journal_signature = journal_signature
                return
            if transcript_signature != self._transcript_signature:
                self._rebuild()
                return
            self._journal_signature = journal_signature
            self.cache_hits += 1

    def between(self, start: float, end: float, *,
                include_start: bool = False) -> list[dict]:
        self.refresh()
        with self._lock:
            left = (
                bisect.bisect_left(self._times, float(start))
                if include_start
                else bisect.bisect_right(self._times, float(start))
            )
            right = bisect.bisect_right(self._times, float(end))
            return list(self._rows[left:right])

    def all(self) -> list[dict]:
        self.refresh()
        with self._lock:
            return list(self._rows)

    def metrics(self) -> dict:
        with self._lock:
            return {
                "rows": len(self._rows),
                "commits": len(self._seen_commits),
                "rebuilds": self.rebuilds,
                "commit_files_read": self.commit_files_read,
                "rows_appended": self.rows_appended,
                "cache_hits": self.cache_hits,
            }


_TRANSCRIPT_INDEXES_LOCK = threading.RLock()
_TRANSCRIPT_INDEXES: dict[str, _TranscriptWindowIndex] = {}


def _transcript_index(d: Path) -> _TranscriptWindowIndex:
    key = str(Path(d).resolve())
    with _TRANSCRIPT_INDEXES_LOCK:
        index = _TRANSCRIPT_INDEXES.get(key)
        if index is None:
            index = _TranscriptWindowIndex(Path(d))
            _TRANSCRIPT_INDEXES[key] = index
        return index


def clear_evidence_indexes() -> None:
    """Aislamiento explicito para fixtures; no se usa durante una clase real."""
    with _TRANSCRIPT_INDEXES_LOCK:
        _TRANSCRIPT_INDEXES.clear()


def _numero_finito(valor, defecto: float) -> float:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return float(defecto)
    return numero if math.isfinite(numero) else float(defecto)


def _intervalo_auto_seg(st: dict) -> float | None:
    minutos = _numero_finito(st.get("apuntes_auto", 5), 5.0)
    if minutos <= 0:
        return None
    return min(60.0, minutos) * 60.0


def _proximo_auto_legacy(inicio: float, ultimo_t: float,
                         intervalo: float = AUTO_RESUMEN_SEG) -> float:
    """Deriva el siguiente limite fijo al migrar un estado con solo ``ultimo_t``."""
    inicio = _numero_finito(inicio, time.time())
    ultimo_t = max(inicio, _numero_finito(ultimo_t, inicio))
    intervalo = max(1.0, _numero_finito(intervalo, AUTO_RESUMEN_SEG))
    bloques_cubiertos = math.floor((ultimo_t - inicio) / intervalo)
    return inicio + (bloques_cubiertos + 1) * intervalo


def _cursores_confirmados(
        path: Path, inicio: float) -> tuple[float, float, float, float | None]:
    """Reconstruye cursores desde el journal durable de bloques ya anexados."""
    ultimo_auto = inicio
    ultimo_manual = inicio
    ultimo_final = inicio
    ultimo_deadline: float | None = None
    for registro in est.leer_jsonl(path.parent / RESUMEN_TRAMOS_JSONL):
        disparo = str(registro.get("disparo", "") or "")
        t_fin = max(inicio, _numero_finito(registro.get("t_fin"), inicio))
        if disparo == "auto":
            ultimo_auto = max(ultimo_auto, t_fin)
        if disparo == "manual":
            ultimo_manual = max(ultimo_manual, t_fin)
        if disparo == "fin":
            ultimo_final = max(ultimo_final, t_fin)
        if disparo == "auto":
            deadline = _numero_finito(
                registro.get("auto_deadline_t"), t_fin + MARGEN_TRANSCRIPTOR_SEG)
            ultimo_deadline = max(ultimo_deadline or deadline, deadline)
    return ultimo_auto, ultimo_manual, ultimo_final, ultimo_deadline


def _cargar_estado_escriba(
        path: Path, inicio: float,
        intervalo: float = AUTO_RESUMEN_SEG) -> tuple[float, float, float, float]:
    """Lee cursores independientes y migra el formato legacy sin omitir contenido."""
    datos: dict = {}
    if path.is_file():
        try:
            cargado = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cargado, dict):
                datos = cargado
        except (OSError, json.JSONDecodeError):
            datos = {}
    inicio = _numero_finito(inicio, time.time())
    intervalo = max(1.0, _numero_finito(intervalo, AUTO_RESUMEN_SEG))
    (confirmado_auto, confirmado_manual, confirmado_final,
     deadline_confirmado) = _cursores_confirmados(path, inicio)
    legacy_t = max(inicio, _numero_finito(datos.get("ultimo_t"), inicio))
    formato_nuevo = any(
        clave in datos
        for clave in ("ultimo_auto_t", "ultimo_manual_t", "ultimo_final_t")
    )
    if formato_nuevo:
        ultimo_auto_t = max(
            inicio, _numero_finito(datos.get("ultimo_auto_t"), legacy_t))
        ultimo_manual_t = max(
            inicio, _numero_finito(datos.get("ultimo_manual_t"), legacy_t))
    else:
        # El cursor legacy pudo haber sido movido por un resumen manual. Solo el journal
        # permite atribuir contenido al calendario automatico sin saltarlo en silencio.
        ultimo_auto_t = confirmado_auto
        ultimo_manual_t = legacy_t
    ultimo_auto_t = max(ultimo_auto_t, confirmado_auto)
    ultimo_manual_t = max(ultimo_manual_t, confirmado_manual)
    ultimo_final_t = max(
        inicio,
        _numero_finito(datos.get("ultimo_final_t"), inicio),
        confirmado_final,
    )

    # El cursor manual evita duplicar contenido, pero nunca mueve la grilla automática.
    cursor_cubierto = max(ultimo_auto_t, ultimo_final_t)
    proximo_defecto = _proximo_auto_legacy(
        inicio, cursor_cubierto + MARGEN_TRANSCRIPTOR_SEG, intervalo)
    # Recalcular repara estados schema 4 que pudieron persistir un reanclaje manual.
    # La grilla de pared es siempre un múltiplo del intervalo desde ``inicio``.
    proximo_auto_t = proximo_defecto
    if deadline_confirmado is not None:
        proximo_auto_t = max(proximo_auto_t, deadline_confirmado + intervalo)
    return ultimo_auto_t, ultimo_manual_t, ultimo_final_t, proximo_auto_t


def _cargar_auto_anchor(path: Path, inicio: float, ultimo_auto_t: float) -> float:
    """Recupera solo el cursor automático; ignora reanclajes manuales schema 4."""
    inicio = _numero_finito(inicio, time.time())
    anchor = max(inicio, _numero_finito(ultimo_auto_t, inicio))
    for registro in est.leer_jsonl(path.parent / RESUMEN_TRAMOS_JSONL):
        disparo = str(registro.get("disparo", "") or "")
        if disparo == "auto":
            anchor = max(
                anchor, _numero_finito(registro.get("t_fin"), inicio))
    return anchor


def _guardar_estado_escriba(path: Path, ultimo_auto_t: float,
                             ultimo_manual_t: float, proximo_auto_t: float,
                             ultimo_final_t: float | None = None,
                             auto_anchor_t: float | None = None) -> None:
    if ultimo_final_t is None:
        ultimo_final_t = ultimo_auto_t
    if auto_anchor_t is None:
        auto_anchor_t = ultimo_auto_t
    datos = {
        "schema_version": ESTADO_ESCRIBA_SCHEMA_VERSION,
        # Alias de lectura para versiones anteriores. Nunca representa el cursor manual.
        "ultimo_t": float(ultimo_auto_t),
        "ultimo_auto_t": float(ultimo_auto_t),
        "ultimo_manual_t": float(ultimo_manual_t),
        "ultimo_final_t": float(ultimo_final_t),
        "proximo_auto_t": float(proximo_auto_t),
        "auto_anchor_t": float(auto_anchor_t),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(datos), encoding="utf-8")
    tmp.replace(path)


def _avanzar_proximo_auto(proximo_auto_t: float, ahora: float | None = None,
                          intervalo: float = AUTO_RESUMEN_SEG) -> float:
    """Confirma exactamente una frontera fija, incluso cuando hay varias vencidas."""
    intervalo = max(1.0, _numero_finito(intervalo, AUTO_RESUMEN_SEG))
    defecto = (_numero_finito(ahora, time.time()) if ahora is not None else time.time())
    return _numero_finito(proximo_auto_t, defecto) + intervalo


def planificar_auto_captura_util(
        st: dict, marcas: list[dict], *, ultimo_auto_t: float,
        ahora: float | None = None, intervalo: float = AUTO_RESUMEN_SEG,
        margen: float = MARGEN_TRANSCRIPTOR_SEG) -> dict:
    """Mapea una frontera de captura útil a pared sin contar pausas."""
    ahora = time.time() if ahora is None else float(ahora)
    intervalo = max(1.0, float(intervalo))
    intervalos = est.intervalos_captura_util(st, marcas, ahora)

    def acumulado_hasta(limite: float) -> float:
        return sum(
            max(0.0, min(fin, limite) - ini)
            for ini, fin in intervalos
            if limite > ini
        )

    recorded = sum(max(0.0, fin - ini) for ini, fin in intervalos)
    cursor_recorded = acumulado_hasta(float(ultimo_auto_t))
    fronteras_confirmadas = math.floor(
        max(0.0, cursor_recorded + 1e-6) / intervalo)
    boundary_recorded = (fronteras_confirmadas + 1) * intervalo
    boundary_wall = None
    if recorded + 1e-6 >= boundary_recorded:
        restante = boundary_recorded
        for ini, fin in intervalos:
            duracion = max(0.0, fin - ini)
            if restante <= duracion + 1e-6:
                boundary_wall = ini + min(restante, duracion)
                break
            restante -= duracion
    due_wall = (
        boundary_wall + max(0.0, float(margen))
        if boundary_wall is not None else None
    )
    return {
        "recorded_elapsed_sec": recorded,
        "cursor_recorded_sec": cursor_recorded,
        "boundary_recorded_sec": boundary_recorded,
        "boundary_wall_t": boundary_wall,
        "due_wall_t": due_wall,
        "due": bool(
            boundary_wall is not None
            and est.estado_efectivo(st) == "grabando_activo"
            and ahora >= float(due_wall)),
    }


def _linea_buscar(cuerpo: str) -> bool:
    return bool(re.search(r"^BUSCAR:\s*\S+", cuerpo or "", re.MULTILINE))


def _markdown_abierto(cuerpo: str) -> bool:
    texto = cuerpo or ""
    return texto.count("```") % 2 == 1 or texto.count("**") % 2 == 1


def _termina_cortado(cuerpo: str) -> bool:
    lineas = [x.strip() for x in (cuerpo or "").splitlines() if x.strip()]
    if not lineas:
        return True
    ultima = lineas[-1]
    if ultima.startswith(("BUSCAR:", "#")):
        return False
    if ultima[-1] in ".!?:;)]}»\"'`":
        return False
    if re.match(r"^\s*[-*]\s+", ultima):
        return True
    return bool(re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{3,}$", ultima))


def motivos_respuesta_incompleta(cuerpo: str | None, finish_reason: str | None,
                                 transcript: str, ocr_texto: str) -> list[str]:
    motivos: list[str] = []
    texto = (cuerpo or "").strip()
    if not texto:
        return ["respuesta_vacia"]
    if finish_reason == "length":
        motivos.append("finish_reason=length")
    if not _linea_buscar(texto):
        motivos.append("falta_BUSCAR")
    if _markdown_abierto(texto):
        motivos.append("markdown_abierto")
    if _termina_cortado(texto):
        motivos.append("final_sin_cierre")
    material = len((transcript or "").strip()) + len((ocr_texto or "").strip())
    if material >= 1800 and len(texto) < max(350, int(material * 0.06)):
        motivos.append("respuesta_demasiado_corta")
    return motivos


def _continuar_respuesta(mensajes_base: list[dict], parcial: str,
                         transcript: str, ocr_texto: str) -> tuple[str, list[str]]:
    mensajes = list(mensajes_base) + [
        {"role": "assistant", "content": parcial},
        {"role": "user", "content": (
            "Continúa exactamente desde donde se cortó, sin repetir lo anterior, "
            "cerrando secciones Markdown y terminando con BUSCAR: <3-5 palabras clave "
            "en inglés del concepto central>."
        )},
    ]
    detalle = preguntar_detalle(mensajes, max_tokens=1800, etiqueta="escriba-continuacion")
    cont = (detalle.get("texto") or "").strip()
    unido = (parcial.rstrip() + "\n" + cont).strip() if cont else parcial.strip()
    motivos = motivos_respuesta_incompleta(
        unido, detalle.get("finish_reason"), transcript, ocr_texto)
    return unido, motivos


def _quitar_buscar_y_enriquecer(cuerpo: str) -> str:
    m = re.search(r"^BUSCAR:\s*(.+)$", cuerpo, re.MULTILINE)
    if not m:
        return cuerpo.strip()
    consulta = m.group(1).strip()[:80]
    cuerpo = cuerpo[:m.start()].rstrip()
    if os.environ.get("CLASE_ESCRIBA_WEB", "0") != "1":
        return cuerpo.strip()
    try:
        from . import web
        cuerpo += "\n" + web.seccion_md(consulta)
    except Exception:
        pass
    return cuerpo.strip()


def _transcript_crudo_preservado(texto: str) -> str:
    lineas = ["### Transcripción cruda preservada", ""]
    for bloque in re.split(r"(?<=[.!?])\s+", texto.strip()):
        if bloque:
            lineas.append("> " + bloque.strip())
    return "\n".join(lineas).strip()


def _limpiar_md_contexto(txt: str, max_chars: int = 1200) -> str:
    txt = RE_IMG_MD_LOCAL.sub("", str(txt or ""))
    txt = re.sub(r"^BUSCAR:.*$", "", txt, flags=re.MULTILINE)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars].rsplit("\n", 1)[0].rstrip()
    return txt


def _estado_pedagogico_de_cuerpo(
        cuerpo: str, max_chars: int = MAX_CONTEXTO_PEDAGOGICO_CHARS) -> str:
    """Extrae panorama + contexto suficientes para la siguiente pausa de estudio."""
    texto = str(cuerpo or "")
    titulos = (
        "Panorama acumulado de la clase",
        "Contexto acumulado para continuar",
    )
    secciones = []
    limite_seccion = max(800, (max_chars - 120) // len(titulos))
    for titulo in titulos:
        match = re.search(
            rf"^#{{3,5}}\s+{re.escape(titulo)}\s*\n"
            r"(?P<body>.*?)(?=\n#{3,5}\s|\Z)",
            texto, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
        if not match:
            continue
        cuerpo_seccion = re.sub(r"\n{3,}", "\n\n", match.group("body")).strip()
        if len(cuerpo_seccion) > limite_seccion:
            cuerpo_seccion = cuerpo_seccion[:limite_seccion].rsplit("\n", 1)[0].rstrip()
        if cuerpo_seccion:
            secciones.append(f"### {titulo}\n{cuerpo_seccion}")
    if secciones:
        return "\n\n".join(secciones)[:max_chars].rstrip()

    # Compatibilidad con resúmenes producidos por el contrato anterior.
    legacy = re.search(
        r"^#{3,5}\s+Hilo de la clase hasta ahora\s*\n"
        r"(?P<body>.*?)(?=\n#{3,5}\s|\Z)",
        texto, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not legacy:
        return ""
    estado = re.sub(r"\n{3,}", "\n\n", legacy.group("body")).strip()
    return ("### Contexto acumulado para continuar\n" + estado)[:max_chars].rstrip()


def resumenes_acumulados_md(
        d: Path, hasta: float | None = None, parte: int | None = None,
        max_chars: int = MAX_CONTEXTO_PEDAGOGICO_CHARS) -> str:
    """Devuelve un estado pedagógico reemplazable, no el historial concatenado."""
    registros = est.leer_jsonl(d / RESUMEN_TRAMOS_JSONL)
    if hasta is not None:
        registros = [r for r in registros if float(r.get("t_fin", 0) or 0) <= hasta]
    if parte is not None:
        registros = [r for r in registros if _registro_en_parte(r, parte)]
    if not registros:
        return ""
    for registro in reversed(registros):
        estado = str(registro.get("estado_pedagogico", "") or "").strip()
        if not estado:
            estado = _estado_pedagogico_de_cuerpo(
                str(registro.get("cuerpo", "") or ""), max_chars=max_chars)
        if estado:
            return estado[:max_chars].rstrip()
    lineas = [
        "ESTADO PEDAGÓGICO PREVIO (compactar y reemplazar, no repetir literal):",
        "",
    ]
    for r in registros[-8:]:
        cuerpo = _limpiar_md_contexto(
            str(r.get("cuerpo", "") or ""), max_chars=700)
        if not cuerpo:
            continue
        capturas = ", ".join(str(x) for x in (r.get("imagenes") or [])[:4])
        partes = r.get("partes") or [r.get("parte", "")]
        meta = f"- {r.get('rango', '')} - partes {','.join(map(str, partes))}"
        if capturas:
            meta += f" - capturas: {capturas}"
        lineas.append(meta)
        lineas.append(cuerpo)
        lineas.append("")
    compacto = "\n".join(lineas).strip()
    if len(compacto) > max_chars:
        compacto = compacto[-max_chars:].split("\n", 1)[-1].strip()
        compacto = "ESTADO PEDAGÓGICO PREVIO RECIENTE:\n" + compacto
    return compacto


def _fmt_duracion(segundos: float) -> str:
    total = max(0, int(round(float(segundos))))
    horas, resto = divmod(total, 3600)
    minutos, segundos = divmod(resto, 60)
    return f"{horas:02d}:{minutos:02d}:{segundos:02d}"


def _leer_estado_resumen_opcional(d: Path, instante: float) -> dict:
    """Lee el reloj de clase o aporta un origen neutro para fixtures/migraciones.

    Una clase real siempre persiste ``session_state.json`` antes de iniciar al
    escriba. El fallback mantiene compatibles herramientas de reparación y
    fixtures mínimos que procesan evidencia histórica sin reconstruir toda la
    sesión; no oculta un estado real corrupto, pues solo cubre archivo ausente.
    """
    try:
        return est.leer_estado(d)
    except FileNotFoundError:
        return {
            "inicio": 0.0,
            "resumen_clock": "pared",
            "resumen_t": _numero_finito(instante, 0.0),
        }


def _tiempo_relativo_clase(d: Path, instante: float) -> float:
    st = _leer_estado_resumen_opcional(d, instante)
    inicio = _numero_finito(st.get("inicio"), instante)
    instante = max(inicio, _numero_finito(instante, inicio))
    if st.get("resumen_clock") == "captura_util":
        return sum(
            max(0.0, min(fin, instante) - ini)
            for ini, fin in est.intervalos_captura_util(
                st, est.leer_marcas(d), instante)
            if instante > ini
        )
    return max(0.0, instante - inicio)


def _rango_clase(d: Path, desde: float, hasta: float) -> str:
    return (
        f"{_fmt_duracion(_tiempo_relativo_clase(d, desde))}–"
        f"{_fmt_duracion(_tiempo_relativo_clase(d, hasta))}"
    )


def _recortar_evidencia_nueva(texto: str, limite: int) -> str:
    texto = str(texto or "")
    if len(texto) <= limite:
        return texto
    cabeza = int(limite * 0.68)
    cola = limite - cabeza - 64
    return (
        texto[:cabeza].rsplit(" ", 1)[0].rstrip()
        + "\n[… tramo nuevo recortado por presupuesto …]\n"
        + texto[-cola:].lstrip()
    )


def _quitar_ruido_vista(cuerpo: str, max_chars: int | None = 2200) -> str:
    txt = RE_IMG_MD_LOCAL.sub("", str(cuerpo or ""))
    txt = re.sub(r"^BUSCAR:.*$", "", txt, flags=re.MULTILINE)
    txt = re.split(r"\n#{3,5}[^\n]*Para profundizar \(web\)", txt, maxsplit=1)[0]
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    if max_chars is not None and len(txt) > max_chars:
        txt = txt[:max_chars].rsplit("\n", 1)[0].rstrip() + "\n\n..."
    return txt


def _apuntes_tramo_vista(cuerpo: str, max_chars: int = 1800) -> str:
    txt = _quitar_ruido_vista(cuerpo, max_chars=max_chars + 1400)
    m = re.search(
        r"^#{3,5}[^\n]*Apuntes del tramo[^\n]*\n(?P<body>.*?)(?=\n#{3,5}\s|\Z)",
        txt,
        flags=re.MULTILINE | re.DOTALL,
    )
    if m:
        txt = m.group("body").strip()
    txt = re.sub(r"^#{3,5}\s+.*$", "", txt, flags=re.MULTILINE)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."
    return txt


def _compactar_tramo_vista(cuerpo: str, max_chars: int = 520) -> str:
    txt = _apuntes_tramo_vista(cuerpo, max_chars=max_chars + 420)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."
    return txt


def _decorar_ficha_estudio(cuerpo: str, max_chars: int | None) -> str:
    """Convierte la última ficha pedagógica en una pausa de lectura escaneable.

    Los iconos y títulos los controla la aplicación, no el LLM. De ese modo el
    Markdown seguro del panel produce HTML consistente sin aceptar clases ni SVG
    arbitrarios provenientes de una respuesta externa.
    """
    limit = None if max_chars is None else max(1000, max_chars)
    txt = _quitar_ruido_vista(cuerpo, max_chars=limit)
    reemplazos = (
        (r"Panorama acumulado de la clase", "🧭 Panorama hasta ahora"),
        (r"Desarrollo detallado del tramo actual", "🎯 Lo nuevo en este tramo"),
        (r"Contexto acumulado para continuar", "🧩 Contexto para continuar"),
        (r"Conceptos y procedimientos clave", "🧠 Conceptos clave"),
        (r"Código o procedimiento", "🧪 Procedimiento o código"),
        (r"Comprueba tu comprensión", "✅ Comprueba tu comprensión"),
        (r"Dudas o correcciones", "⚠️ Dudas y correcciones"),
        (r"Enlaces y referencias", "🔗 Enlaces y referencias"),
        (r"Fuera de guion", "🗂️ Fuera de guion"),
        (r"Apuntes del tramo", "🎯 Lo nuevo en este tramo"),
    )
    for patron, titulo in reemplazos:
        txt = re.sub(
            rf"^#{{2,5}}\s*(?:[^\w\n]+\s*)?{patron}\s*$",
            f"## {titulo}", txt, flags=re.MULTILINE | re.IGNORECASE,
        )
    return txt.strip()


def _transcript_pendiente_md(d: Path, desde: float, parte: int,
                             max_chars: int = 2200) -> str:
    del parte, max_chars
    # Esta vista solo necesita saber si existe trabajo nuevo; releer todo el
    # transcript cada 5 s era costoso y no aporta texto visible.
    try:
        st = est.leer_estado(d)
        transcribed_until = float(st.get("stt_transcribed_until_wall") or 0.0)
    except (OSError, TypeError, ValueError):
        transcribed_until = 0.0
    if transcribed_until <= float(desde or 0.0) + 0.5:
        return ""
    return (
        "## ⏳ Tramo en curso\n\n"
        "> Hay contenido nuevo en curso. Se integrará en la próxima pausa de estudio.\n"
    )


def vista_leccion_vivo_resumen(
        d: Path, parte: int, max_chars: int | None = None) -> str:
    """Vista del panel: una ficha acumulada, no una lista de panoramas repetidos."""
    registros = [
        r for r in est.leer_jsonl(d / RESUMEN_TRAMOS_JSONL)
        if _registro_en_parte(r, parte)
    ]
    registros.sort(key=lambda r: float(r.get("t_fin", 0) or 0))
    if not registros:
        lec = d / "leccion_en_vivo.md"
        txt = lec.read_text(encoding="utf-8") if lec.is_file() else ""
        return _quitar_ruido_vista(txt, max_chars=max_chars)

    ultimo = registros[-1]
    body_limit = None if max_chars is None else max(1000, max_chars - 900)
    cuerpo_ultimo = _decorar_ficha_estudio(
        str(ultimo.get("cuerpo", "") or ""), max_chars=body_limit)
    lineas = [
        "# Pausa de estudio",
        "",
        f"> Actualizado hasta **{ultimo.get('rango', '')}**. Aquí tienes el panorama "
        "de la clase, lo nuevo y el contexto necesario para retomarla.",
        "",
        cuerpo_ultimo,
    ]
    pendiente = _transcript_pendiente_md(
        d, float(ultimo.get("t_fin", 0) or 0), int(parte))
    if pendiente:
        lineas.extend(["", pendiente])
    salida = "\n".join(lineas).strip()
    if max_chars is not None and len(salida) > max_chars:
        # Conserva siempre panorama y tramo actual; nunca recortes por la cola,
        # porque eso eliminaba precisamente la introducción acumulada.
        salida = salida[:max_chars].rsplit("\n", 1)[0].rstrip() + "\n\n…"
    return salida


def _chunks_pendientes_transcript(d: Path) -> list[Path]:
    completos = audio_manifest.completed_transcript_chunks(
        d, verify_files=False)
    pendientes = [
        d / "audio" / str(row.get("path") or f"{chunk_id}.wav")
        for chunk_id, row in audio_manifest.latest_by_chunk(d).items()
        if row.get("status") == "capturing"
    ]
    for row in audio_manifest.capture_records(d):
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id and chunk_id not in completos:
            pendientes.append(
                d / "audio" / str(row.get("path") or f"{chunk_id}.wav"))
    return sorted(set(pendientes))


def _esperar_transcriptor_fin(d: Path) -> dict:
    """Espera la barrera común y siempre declara si terminó o alcanzó timeout."""
    try:
        timeout = float(os.environ.get("CLASE_ESCRIBA_FIN_WAIT", "75"))
    except ValueError:
        timeout = 75.0
    inicio_espera = time.time()
    limite = inicio_espera + max(0.0, timeout)
    ultimo_estado = None
    while True:
        reporte = audio_manifest.completion_barrier_report(d)
        ahora = time.time()
        if reporte.get("ready"):
            return {
                **reporte,
                "timed_out": False,
                "waited_seconds": round(max(0.0, ahora - inicio_espera), 3),
            }
        estado_actual = (
            reporte.get("status"), reporte.get("pending_stt_count"),
            reporte.get("unfinished_capture_count"),
        )
        if estado_actual != ultimo_estado:
            print(
                "[escriba] esperando barrera final: "
                f"status={reporte.get('status')} · "
                f"STT={reporte.get('pending_stt_count', 0)} · "
                f"captura={reporte.get('unfinished_capture_count', 0)}"
            )
            ultimo_estado = estado_actual
        if ahora >= limite:
            return {
                **reporte,
                "timed_out": True,
                "waited_seconds": round(max(0.0, ahora - inicio_espera), 3),
            }
        time.sleep(min(3.0, max(0.0, limite - ahora)))


def _bloque_parcial(cuerpo: str | None, texto: str, motivos: list[str]) -> str:
    parcial = (cuerpo or "").strip()
    detalle = ", ".join(motivos) if motivos else "respuesta incompleta"
    piezas = [
        "### ⚠️ Resumen parcial del tramo",
        f"> Resumen parcial; transcript conservado. Motivo: {detalle}.",
    ]
    if parcial:
        piezas.extend(["", parcial])
    piezas.extend(["", _transcript_crudo_preservado(texto)])
    return "\n".join(piezas).strip()


def generar_resumen_en_vivo(texto: str, ocr_junto: str, rango: str,
                            contexto_acumulado: str = "") -> dict:
    texto_tramo = texto
    contexto = str(contexto_acumulado or "")[:MAX_CONTEXTO_PEDAGOGICO_CHARS]
    transcript_prompt = _recortar_evidencia_nueva(
        texto_tramo, MAX_TRANSCRIPT_RESUMEN_CHARS)
    ocr_prompt = _recortar_evidencia_nueva(
        ocr_junto, MAX_OCR_RESUMEN_CHARS)
    piezas = []
    if contexto.strip():
        piezas.extend([
            "ESTADO PEDAGÓGICO ANTERIOR (actualízalo; no lo repitas entero):",
            contexto.strip(), "",
        ])
    piezas.extend([
        f"TRANSCRIPCIÓN NUEVA ({rango}):",
        transcript_prompt or "(sin transcript nuevo)", "",
        "TEXTO OCR NUEVO DE PANTALLA:",
        ocr_prompt or "(sin capturas nuevas)",
    ])
    prompt_usuario = "\n".join(piezas)
    mensajes = [
        {"role": "system", "content": PROMPT_SISTEMA},
        {"role": "user", "content": prompt_usuario},
    ]
    detalle = preguntar_detalle(
        mensajes, max_tokens=MAX_TOKENS_ESCRIBA, etiqueta="escriba")
    cuerpo = (detalle.get("texto") or "").strip()
    motivos = motivos_respuesta_incompleta(
        cuerpo, detalle.get("finish_reason"), texto_tramo, ocr_junto)
    if cuerpo and motivos:
        cuerpo, motivos = _continuar_respuesta(mensajes, cuerpo, texto_tramo, ocr_junto)
    if not cuerpo:
        return {
            "estado": "parcial",
            "msg": "Resumen parcial; transcript conservado",
            "cuerpo": _bloque_parcial(None, texto_tramo, ["api_sin_respuesta"]),
            "motivos": ["api_sin_respuesta"],
            "estado_pedagogico": "",
        }
    if motivos:
        return {
            "estado": "parcial",
            "msg": "Resumen parcial; transcript conservado",
            "cuerpo": _bloque_parcial(cuerpo, texto_tramo, motivos),
            "motivos": motivos,
            "estado_pedagogico": _estado_pedagogico_de_cuerpo(cuerpo),
        }
    return {
        "estado": "generado",
        "msg": "Resumen generado.",
        "cuerpo": _quitar_buscar_y_enriquecer(cuerpo),
        "motivos": [],
        "estado_pedagogico": _estado_pedagogico_de_cuerpo(cuerpo),
    }


def extraer_links(*textos: str) -> list[str]:
    vistos, out = set(), []
    for t in textos:
        for url in RE_LINK.findall(t or ""):
            url = url.rstrip(".,;")
            if url not in vistos:
                vistos.add(url)
                out.append(url)
    return out


def construir_bloque(rango: str, parte: int, imagenes: list[str],
                     cuerpo: str, links_extra: list[str]) -> str:
    """Ensambla el bloque md final. Puro y testeable (sin red)."""
    lineas = [f"\n---\n\n## ⏱️ {rango} · Parte {parte}\n"]
    for img in imagenes:
        lineas.append(f"![captura](img/{img})\n")
    lineas.append(cuerpo.strip() + "\n")
    extras = [u for u in links_extra if u not in cuerpo]
    if extras:
        lineas.append("\n### 🔗 Enlaces detectados automáticamente\n")
        lineas.extend(f"- <{u}>\n" for u in extras)
    return "".join(lineas)


def encabezado_leccion(st: dict) -> str:
    fecha = time.strftime("%Y-%m-%d", time.localtime(st.get("inicio", time.time())))
    return (f"# 📚 {st.get('nombre', 'Clase')}\n\n"
            f"> Fecha: {fecha} · Generado en vivo por clase-viva\n"
            f"> Estado: EN CONSTRUCCIÓN durante la clase; la versión final ordenada "
            f"se genera con `python -m src.post_procesar --todo`\n")


def _registro_en_parte(registro: dict, parte: int) -> bool:
    """Los registros legacy sin ``parte`` siguen siendo visibles."""
    partes = registro.get("partes")
    if isinstance(partes, list) and partes:
        try:
            return int(parte) in {int(valor) for valor in partes}
        except (TypeError, ValueError):
            pass
    if registro.get("parte") in (None, ""):
        return True
    try:
        return int(registro["parte"]) == int(parte)
    except (TypeError, ValueError):
        return True


def _parte_registro(registro: dict, defecto: int) -> int:
    try:
        return int(registro.get("parte", defecto) or defecto)
    except (TypeError, ValueError):
        return int(defecto)


def _texto_segmentos(segs: list[dict], parte: int, separar_partes: bool) -> str:
    def texto_segmento(segmento: dict) -> str:
        texto = str(segmento.get("texto", "") or "")
        if segmento.get("source_role") == "student_local_intervention":
            return f"[INTERVENCIÓN DEL ESTUDIANTE] {texto}"
        return texto

    if not separar_partes:
        return " ".join(texto_segmento(s) for s in segs)
    piezas: list[str] = []
    parte_anterior: int | None = None
    for segmento in segs:
        parte_segmento = _parte_registro(segmento, parte)
        if parte_segmento != parte_anterior:
            piezas.append(f"\n[PARTE {parte_segmento}]\n")
            parte_anterior = parte_segmento
        piezas.append(texto_segmento(segmento))
    return " ".join(piezas).strip()


def _segmentos_pedagogicos(
        d: Path, desde: float | None = None, hasta: float | None = None,
        *, include_start: bool = False,
        inicio_clase: float | None = None) -> list[dict]:
    """Combina vistas sin convertir la voz local en transcript docente."""
    index = _transcript_index(d)
    teacher_rows = (
        index.all() if desde is None or hasta is None
        else index.between(desde, hasta, include_start=include_start)
    )
    student_rows = student_notes.local_intervention_segments(d)
    if desde is not None and hasta is not None:
        student_rows = [
            row for row in student_rows
            if _tiempo_en_ventana(
                d, row.get("t"), desde, hasta,
                inicio_clase=inicio_clase)
        ]
    rows = [
        *teacher_rows,
        *student_rows,
    ]
    return sorted(rows, key=lambda row: (
        _numero_finito(row.get("t_rel", row.get("t")), 0.0),
        str(row.get("student_note_id", row.get("chunk", ""))),
    ))


def _normalizar_ocr(texto: str) -> str:
    return re.sub(r"\W+", " ", str(texto or "").casefold()).strip()


def _calidad_captura(captura: dict) -> tuple[int, int, float, str]:
    texto = str(captura.get("_ocr", "") or "")
    codigo = int(bool(texto and ocr.parece_codigo(texto)))
    return (
        codigo,
        min(len(_normalizar_ocr(texto)), 2000),
        _numero_finito(captura.get("t"), 0.0),
        str(captura.get("archivo", "")),
    )


def _seleccionar_capturas_resumen(
        d: Path, capturas: list[dict], maximo: int = MAX_CAPTURAS_RESUMEN) -> list[dict]:
    """OCR-first y muestreo estratificado: calidad local con cobertura temporal."""
    maximo = max(0, int(maximo))
    if not capturas or maximo == 0:
        return []

    unicas: list[dict] = []
    archivos_vistos: set[str] = set()
    for original in sorted(
            capturas,
            key=lambda c: (_numero_finito(c.get("t"), 0.0), str(c.get("archivo", "")))):
        archivo = str(original.get("archivo", "") or "")
        if not archivo or archivo in archivos_vistos:
            continue
        archivos_vistos.add(archivo)
        captura = dict(original)
        if "ocr" in captura:
            captura["_ocr"] = str(captura.get("ocr", "") or "")
        else:
            try:
                captura["_ocr"] = ocr.texto_de_imagen(d / "img" / archivo) or ""
            except Exception as exc:
                config.log_error("escriba-ocr", f"{type(exc).__name__}: {archivo}")
                captura["_ocr"] = ""
        unicas.append(captura)

    # ``img_log`` ya evita heartbeats/JPEG repetidos. No se fusionan archivos distintos
    # solo por OCR: dos diagramas pueden compartir etiquetas y seguir siendo evidencia.
    deduplicadas = unicas

    if len(deduplicadas) <= maximo:
        return deduplicadas
    if maximo == 1:
        return [max(deduplicadas, key=_calidad_captura)]

    # Primero y ultimo fijan los extremos; cada cupo interior elige la captura con
    # mas informacion de su franja, sin permitir que un solo momento monopolice todo.
    indices = {0, len(deduplicadas) - 1}
    interiores = deduplicadas[1:-1]
    cupos = maximo - 2
    for cupo in range(cupos):
        ini = math.floor(cupo * len(interiores) / cupos)
        fin = math.floor((cupo + 1) * len(interiores) / cupos)
        grupo = list(enumerate(interiores[ini:fin], start=ini + 1))
        if grupo:
            indice, _ = max(grupo, key=lambda par: _calidad_captura(par[1]))
            indices.add(indice)
    if len(indices) < maximo:
        restantes = (
            (i, captura) for i, captura in enumerate(deduplicadas) if i not in indices)
        for indice, _captura in sorted(
                restantes, key=lambda par: _calidad_captura(par[1]), reverse=True):
            indices.add(indice)
            if len(indices) == maximo:
                break
    return [deduplicadas[i] for i in sorted(indices)]


def _tiempo_en_ventana(d: Path, valor, desde: float, hasta: float,
                       inicio_clase: float | None = None) -> bool:
    t = _numero_finito(valor, desde - 1)
    inicio = inicio_clase
    if inicio is None:
        inicio = desde
        try:
            inicio = _numero_finito(est.leer_estado(d).get("inicio"), desde)
        except Exception:
            pass
    limite_inicial = math.isclose(desde, inicio, abs_tol=1e-6)
    return (t >= desde if limite_inicial else t > desde) and t <= hasta


def _filas_visuales_ventana(
        d: Path, desde: float, hasta: float, *,
        include_start: bool) -> list[dict] | None:
    """Acota timeline sin perder la slide activa antes del corte.

    Los journals modernos usan epoch. Si una clase legacy mezcla tiempos
    relativos con una ventana epoch, se devuelve ``None`` para pedir al
    postproceso su reconstruccion integral compatible.
    """
    captures = incremental_jsonl.time_index_for(d / "img_log.jsonl")
    timeline = incremental_jsonl.time_index_for(d / "visual_timeline.jsonl")
    bounds = [value for value in (captures.bounds(), timeline.bounds()) if value]
    if desde >= 100_000_000 and any(end < 100_000_000 for _start, end in bounds):
        return None

    prior_capture = captures.before(
        desde, limit=1, include_end=not include_start)
    # Una muestra corta basta para conocer el ultimo heartbeat/fin y estimar la
    # cadencia; la definicion visual se aporta por separado en prior_capture.
    prior_events = timeline.before(
        desde, limit=32, include_end=not include_start)
    current_captures = captures.between(
        desde, hasta, include_start=include_start)
    current_events = timeline.between(
        desde, hasta, include_start=include_start)
    return [*prior_capture, *prior_events, *current_captures, *current_events]


def _capturas_timeline_resumen(d: Path, desde: float, hasta: float,
                               segs: list[dict], fallback: list[dict], *,
                               filas_visuales: list[dict] | None = None) -> list[dict]:
    """Reusa timeline OCR-first para incluir una slide activa antes del rango."""
    try:
        from . import post_procesar

        st = est.leer_estado(d) if (d / "session_state.json").is_file() else {}
        origen = {
            "origen": st.get("origen", "vivo"),
            "inicio": _numero_finito(st.get("inicio"), 0.0),
        }
        slides = post_procesar.construir_timeline_slides(
            d, desde, hasta, segs, origen, filas_visuales=filas_visuales)
        if not slides:
            return fallback
        return [
            {
                **slide,
                "t": _numero_finito(
                    slide.get("t_abs", slide.get("t_rel")), desde),
                "_ocr": str(slide.get("ocr", "") or ""),
            }
            for slide in slides
        ]
    except Exception as exc:
        config.log_error("escriba-visual", f"{type(exc).__name__}: {exc}")
        return fallback


def _fila_audio_solapa(row: dict, desde: float, hasta: float) -> bool:
    inicio = _numero_finito(
        row.get("pcm_wall_start", row.get("wall_start")), desde - 1)
    fin = _numero_finito(
        row.get("pcm_wall_end", row.get("wall_end")),
        inicio + _numero_finito(row.get("duration"), 0.0),
    )
    return fin >= desde and inicio <= hasta


def _ventana_auto_resuelta_sin_texto(
        d: Path, desde: float, hasta: float, deadline: float,
        ahora: float) -> bool:
    """Distingue silencio confirmado de STT atrasado antes de mover deadline."""
    latest = audio_manifest.latest_by_chunk(d)
    pendientes_captura = [
        row for row in latest.values()
        if row.get("status") == "capturing"
        and _fila_audio_solapa(row, desde, hasta)
    ]
    if pendientes_captura:
        return False
    finales = [
        row for row in audio_manifest.capture_records(d)
        if _fila_audio_solapa(row, desde, hasta)
    ]
    if finales:
        completos = audio_manifest.completed_transcript_chunks(
            d, verify_files=False)
        return all(str(row.get("chunk_id") or "") in completos for row in finales)
    terminales_sin_wav = [
        row for row in latest.values()
        if row.get("status") in {"discarded_silence", "capture_failed"}
        and _fila_audio_solapa(row, desde, hasta)
    ]
    if terminales_sin_wav:
        return True
    return ahora >= deadline + AUTO_EMPTY_GRACE_SEG


def evidencia_tramo_completa(d: Path, desde: float, hasta: float,
                             ahora: float) -> bool:
    """Confirma que el tramo ya tiene toda su evidencia antes del margen fijo.

    El resumen resultante es el mismo que esperando el techo: ``hasta`` es la
    frontera de captura util (funcion de las marcas, no del instante de
    disparo), y aqui solo se comprueba que ninguna fuente pueda todavia aportar
    texto a esa ventana. Ante cualquier duda devuelve False y manda al techo.
    """
    # Orden deliberado de barato a caro: el reloj no cuesta E/S y descarta la
    # mayoria de los polls antes de tocar el manifest de una clase larga.
    if ahora < hasta + VISUAL_ASENTAMIENTO_SEG:
        return False
    try:
        # La voz del estudiante entra al mismo resumen y es idempotente: un
        # borrador abierto o una extraccion pendiente aun puede aportar texto.
        if local_interventions.pending_report(d).get("blocking"):
            return False
        if student_notes.pending_audio_notes(d, included_only=True):
            return False
        if student_notes.unresolved_local_interventions(d):
            return False
        latest = audio_manifest.latest_by_chunk(d)
        if any(
                row.get("status") == "capturing"
                and _fila_audio_solapa(row, desde, hasta)
                for row in latest.values()):
            return False
        finales = [
            row for row in audio_manifest.capture_records(d)
            if _fila_audio_solapa(row, desde, hasta)
        ]
        # Sin audio final que solape no hay cobertura que confirmar: la ventana
        # vacia la resuelve el camino normal con su propia gracia.
        if not finales:
            return False
        completos = audio_manifest.completed_transcript_chunks(
            d, verify_files=False)
        if any(str(row.get("chunk_id") or "") not in completos
               for row in finales):
            return False
    except Exception as exc:
        config.log_error("escriba-cobertura", f"{type(exc).__name__}: {exc}")
        return False
    return True


def _registrar_ventana_auto_vacia(
        d: Path, parte: int, desde: float, hasta: float,
        deadline: float) -> None:
    if _resumen_confirmado(d, "auto", parte, desde, hasta):
        return
    rango = _rango_clase(d, desde, hasta)
    resumen_id = _resumen_id("auto", parte, desde, hasta)
    cuerpo = (
        "### Sin evidencia transcribible en esta ventana\n\n"
        "No hubo transcript nuevo confirmado. La frontera automática se conserva "
        "en el historial sin consumir una llamada al modelo."
    )
    registro = {
        "resumen_id": resumen_id,
        "t_ini": desde,
        "t_fin": hasta,
        "auto_deadline_t": deadline,
        "rango": rango,
        "parte": int(parte),
        "partes": [int(parte)],
        "disparo": "auto",
        "estado": "sin_transcript",
        "msg": "Ventana automática sin transcript confirmado.",
        "motivos": ["ventana_vacia_confirmada"],
        "imagenes": [],
        "segmentos": 0,
        "capturas": 0,
        "capturas_disponibles": 0,
        "transcript_chars": 0,
        "ocr_chars": 0,
        "links_extra": [],
        "cuerpo": cuerpo,
        "sin_llamada_llm": True,
    }
    est.anexar_jsonl(d / RESUMEN_TRAMOS_JSONL, registro, durable=True)
    bloque = construir_bloque(rango, parte, [], cuerpo, [])
    _anexar_bloque_idempotente(d, resumen_id, bloque)


def _resumen_id(disparo: str, parte: int, desde: float, hasta: float) -> str:
    return f"{disparo}:p{int(parte)}:{float(desde):.6f}:{float(hasta):.6f}"


def _anexar_bloque_idempotente(d: Path, resumen_id: str, bloque: str) -> None:
    leccion = d / "leccion_en_vivo.md"
    marca_ini = f"<!-- resumen-inicio:{resumen_id} -->"
    marca_fin = f"<!-- resumen-fin:{resumen_id} -->"
    existente = leccion.read_text(encoding="utf-8") if leccion.is_file() else ""
    if marca_fin in existente:
        return
    if marca_ini in existente:
        # Un append interrumpido solo puede estar al final: el Escriba es instancia unica.
        existente = existente.split(marca_ini, 1)[0].rstrip() + "\n"
        tmp = leccion.with_name(leccion.name + ".tmp")
        tmp.write_text(existente, encoding="utf-8")
        tmp.replace(leccion)
    with open(leccion, "a", encoding="utf-8") as archivo:
        archivo.write(f"{marca_ini}\n{bloque.rstrip()}\n{marca_fin}\n")


def _restaurar_bloque_confirmado(d: Path, registro: dict) -> None:
    resumen_id = str(registro.get("resumen_id", "") or "")
    cuerpo = str(registro.get("cuerpo", "") or "")
    rango = str(registro.get("rango", "") or "")
    if not resumen_id or not cuerpo or not rango:
        return
    bloque = construir_bloque(
        rango,
        _parte_registro(registro, 1),
        [str(x) for x in (registro.get("imagenes") or []) if str(x)],
        cuerpo,
        [str(x) for x in (registro.get("links_extra") or []) if str(x)],
    )
    _anexar_bloque_idempotente(d, resumen_id, bloque)


def _reparar_bloques_confirmados(d: Path) -> None:
    for registro in est.leer_jsonl(d / RESUMEN_TRAMOS_JSONL):
        _restaurar_bloque_confirmado(d, registro)


def _resumen_confirmado(
        d: Path, disparo: str, parte: int, desde: float, hasta: float) -> dict | None:
    esperado = _resumen_id(disparo, parte, desde, hasta)
    for registro in est.leer_jsonl(d / RESUMEN_TRAMOS_JSONL):
        if registro.get("resumen_id") == esperado:
            _restaurar_bloque_confirmado(d, registro)
            return registro
        if (
            str(registro.get("disparo", "") or "") == disparo
            and _registro_en_parte(registro, parte)
            and math.isclose(_numero_finito(registro.get("t_ini"), -1), desde, abs_tol=1e-6)
            and math.isclose(_numero_finito(registro.get("t_fin"), -1), hasta, abs_tol=1e-6)
        ):
            _restaurar_bloque_confirmado(d, registro)
            return registro
    return None


def procesar_bloque(d: Path, desde: float, hasta: float, parte: int,
                    forzar: bool = False, disparo: str | None = None) -> bool:
    """Genera y anexa un bloque. Devuelve True si había contenido.
    forzar=True (clic del usuario): genera aunque haya poco material, y si no hay NADA
    deja una nota visible en la lección (feedback, no silencio)."""
    disparo = disparo or ("manual" if forzar else "auto")
    if disparo not in DISPAROS_RESUMEN:
        raise ValueError(f"disparo de resumen invalido: {disparo}")
    procesar_bloque.ultimo_estado = {"estado": "", "msg": ""}
    confirmado = _resumen_confirmado(d, disparo, parte, desde, hasta)
    if confirmado is not None:
        procesar_bloque.ultimo_estado = {
            "estado": confirmado.get("estado") or "generado",
            "msg": confirmado.get("msg") or "Resumen generado.",
            "motivos": confirmado.get("motivos") or [],
        }
        return True

    try:
        inicio_clase = _numero_finito(est.leer_estado(d).get("inicio"), desde)
    except Exception:
        inicio_clase = desde
    include_start = math.isclose(desde, inicio_clase, abs_tol=1e-6)
    segs_intervalo = _segmentos_pedagogicos(
        d, desde, hasta, include_start=include_start,
        inicio_clase=inicio_clase)
    caps_raw = incremental_jsonl.time_index_for(
        d / "img_log.jsonl").between(
            desde, hasta, include_start=include_start)
    filas_visuales = _filas_visuales_ventana(
        d, desde, hasta, include_start=include_start)
    caps_intervalo = _capturas_timeline_resumen(
        d, desde, hasta, segs_intervalo, caps_raw,
        filas_visuales=filas_visuales)
    if disparo in {"auto", "fin"}:
        # Un deadline puede atravesar un cambio de parte. Consumir el cursor
        # automatico exige conservar todas las partes incluidas en la ventana.
        segs = segs_intervalo
        caps_disponibles = caps_intervalo
    else:
        segs = [s for s in segs_intervalo if _registro_en_parte(s, parte)]
        caps_disponibles = [
            c for c in caps_intervalo if _registro_en_parte(c, parte)]
    partes_contenido = sorted({_parte_registro(s, parte) for s in segs})
    texto = _texto_segmentos(segs, parte, len(partes_contenido) > 1)
    cierre_corto = disparo == "fin" and bool(texto.strip()) and len(texto) < 120
    if len(texto) < 120 and not cierre_corto:
        procesar_bloque.ultimo_estado = {
            "estado": "sin_transcript",
            "msg": "Aún no hay transcripción suficiente; la solicitud sigue pendiente.",
            "motivos": ["transcript_insuficiente"],
        }
        return False  # silencio / pausa del profesor: no gastar API

    caps = _seleccionar_capturas_resumen(d, caps_disponibles)
    ocr_textos = []
    for c in caps:
        t = str(c.get("_ocr", "") or "")
        if t:
            marca = " (PARECE CÓDIGO)" if ocr.parece_codigo(t) else ""
            marca_parte = (
                f" parte {_parte_registro(c, parte)}"
                if len(partes_contenido) > 1 else ""
            )
            ocr_textos.append(
                f"--- pantalla {_fmt_duracion(_tiempo_relativo_clase(d, c['t']))}"
                f"{marca_parte}{marca} ---\n{t[:1500]}")
    ocr_junto = "\n".join(ocr_textos)

    rango = _rango_clase(d, desde, hasta)
    contexto_acumulado = resumenes_acumulados_md(
        d, hasta=desde, parte=parte)
    if cierre_corto:
        # El cierre no debe perder la ultima frase solo por no alcanzar el umbral
        # economico usado por los resumenes periodicos.
        resultado = {
            "estado": "generado",
            "msg": "Último tramo preservado sin llamada al modelo.",
            "cuerpo": _transcript_crudo_preservado(texto),
            "motivos": ["cierre_corto_preservado"],
            "estado_pedagogico": contexto_acumulado,
        }
    else:
        resultado = generar_resumen_en_vivo(
            texto, ocr_junto, rango, contexto_acumulado)
    cuerpo = resultado["cuerpo"]
    procesar_bloque.ultimo_estado = {
        "estado": resultado["estado"],
        "msg": resultado["msg"],
        "motivos": resultado.get("motivos", []),
    }

    links_extra = extraer_links(texto, ocr_junto)
    resumen_id = _resumen_id(disparo, parte, desde, hasta)
    bloque = construir_bloque(
        rango, parte, [c["archivo"] for c in caps], cuerpo, links_extra)
    registro = {
        "resumen_id": resumen_id,
        "t_ini": desde,
        "t_fin": hasta,
        "auto_deadline_t": (
            hasta + MARGEN_TRANSCRIPTOR_SEG if disparo == "auto" else None),
        "rango": rango,
        "parte": parte,
        "partes": partes_contenido or [int(parte)],
        "disparo": disparo,
        "estado": resultado["estado"],
        "msg": resultado["msg"],
        "motivos": resultado.get("motivos", []),
        "imagenes": [c["archivo"] for c in caps],
        "segmentos": len(segs),
        "capturas": len(caps),
        "capturas_disponibles": len(caps_disponibles),
        "transcript_chars": len(texto),
        "ocr_chars": len(ocr_junto),
        "links_extra": links_extra,
        "cuerpo": cuerpo,
        "estado_pedagogico": resultado.get("estado_pedagogico", ""),
    }
    # El journal se confirma primero. Si el append Markdown se interrumpe, el siguiente
    # arranque reconstruye el bloque y elimina cualquier cola parcial marcada.
    est.anexar_jsonl(d / RESUMEN_TRAMOS_JSONL, registro, durable=True)
    _anexar_bloque_idempotente(d, resumen_id, bloque)
    sufijo = " PARCIAL" if resultado["estado"] == "parcial" else ""
    print(f"[escriba] bloque {rango} anexado{sufijo} "
          f"({len(segs)} segmentos, {len(caps)}/{len(caps_disponibles)} capturas)")
    return True


def modo_test() -> int:
    """Prueba el prompt con texto sintético (1 llamada real, ~$0.0001)."""
    cuerpo = preguntar(
        [{"role": "system", "content": PROMPT_SISTEMA},
         {"role": "user", "content": "TRANSCRIPCIÓN (17:00–17:03):\nEl perceptrón usa una "
          "función escalón, por eso no funciona con descenso por gradiente, necesitamos "
          "funciones diferenciables como la sigmoide.\n\nTEXTO OCR DE PANTALLA:\n"
          "def sigmoid(x):\n  return 1/(1+np.exp(-x))"}],
        max_tokens=600, etiqueta="escriba-test")
    if cuerpo is None:
        print("TEST_FALLO: la API no respondió")
        return 1
    print(cuerpo)
    print("\nTEST_OK: bloque generado por DeepSeek arriba")
    return 0


def main() -> int:
    config.stdout_utf8()
    if "--test" in sys.argv:
        return modo_test()

    if not config.instancia_unica("escriba"):
        print("[escriba] ya hay otro vivo (candado). Saliendo.")
        return 0
    d = est.clase_activa()
    if d is None:
        print("[escriba] no hay clase activa. Saliendo.")
        return 1

    estado_propio = d / "escriba_estado.json"
    st_inicial = est.leer_estado(d)
    inicio = _numero_finito(st_inicial.get("inicio"), time.time())
    intervalo_inicial = _intervalo_auto_seg(st_inicial) or AUTO_RESUMEN_SEG
    ultimo_auto, ultimo_manual, ultimo_final, proximo_auto = _cargar_estado_escriba(
        estado_propio, inicio, intervalo=intervalo_inicial)
    auto_anchor = _cargar_auto_anchor(estado_propio, inicio, ultimo_auto)
    _guardar_estado_escriba(
        estado_propio, ultimo_auto, ultimo_manual, proximo_auto,
        ultimo_final_t=ultimo_final, auto_anchor_t=auto_anchor)

    leccion = d / "leccion_en_vivo.md"
    if not leccion.is_file():
        leccion.write_text(encabezado_leccion(st_inicial), encoding="utf-8")
    _reparar_bloques_confirmados(d)
    print(
        f"[escriba] bajo demanda (📌) + auto cada "
        f"{intervalo_inicial / 60:g} min de captura útil -> {leccion}"
    )

    auto_fallos = manual_fallos = 0
    auto_retry_after = manual_retry_after = 0.0

    while True:
        st = est.leer_estado(d)
        efectivo = est.estado_efectivo(st)
        ahora = time.time()
        intervalo_auto = _intervalo_auto_seg(st)
        # margen de 45s para que el transcriptor alcance a los chunks recientes
        corte = ahora - MARGEN_TRANSCRIPTOR_SEG
        if efectivo == "finalizado" or config.latido_perdido():
            if efectivo == "finalizado":
                espera = _esperar_transcriptor_fin(d)
                est.actualizar_estado(d, cierre_stt=espera)
                if not espera.get("ready"):
                    estado_barrera = str(espera.get("status") or "incompleta")
                    pendientes = int(espera.get("pending_stt_count") or 0)
                    msg = (
                        "Cierre pendiente: timeout de barrera final "
                        f"(status={estado_barrera}, STT pendientes={pendientes})."
                    )
                    est.actualizar_estado(
                        d, resumen_estado="pendiente", resumen_msg=msg,
                        resumen_t=time.time())
                    print(f"[escriba] {msg} No se avanzan cursores.")
                    return 0
                # El commit se publica antes que su vista textual. La barrera confirma
                # commits; reconstruir aqui hace que el cierre lea exactamente esa fuente.
                audio_manifest.materialize_transcript(d, verify_files=True)
                ahora = time.time()
            hasta_fin = ahora + 1
            desde_fin = max(ultimo_auto, ultimo_manual, ultimo_final)
            bloque_final = procesar_bloque(
                d, desde_fin, hasta_fin, st["parte"], disparo="fin")
            if bloque_final:
                ultimo_final = hasta_fin
                _guardar_estado_escriba(
                    estado_propio, ultimo_auto, ultimo_manual, proximo_auto,
                    ultimo_final_t=ultimo_final, auto_anchor_t=auto_anchor)
            print("[escriba] fin de clase (o panel cerrado). Cerrando.")
            return 0
        pedido = bool(st.get("resumir"))
        modo_captura_util = st.get("resumen_clock") == "captura_util"
        plan_auto = None
        if modo_captura_util and intervalo_auto is not None:
            plan_auto = planificar_auto_captura_util(
                st, est.leer_marcas(d), ultimo_auto_t=auto_anchor,
                ahora=ahora, intervalo=intervalo_auto)
            if plan_auto["due_wall_t"] is not None:
                proximo_auto = float(plan_auto["due_wall_t"])
            auto_debido = bool(
                plan_auto["due"] and ahora >= auto_retry_after)
            if (not auto_debido
                    and plan_auto["boundary_wall_t"] is not None
                    and ahora >= auto_retry_after
                    and est.estado_efectivo(st) == "grabando_activo"):
                # Misma ventana que usaria el disparo por techo; solo cambia
                # el instante en que se confirma que ya esta completa.
                desde_cobertura = max(ultimo_auto, ultimo_manual, ultimo_final)
                hasta_cobertura = max(
                    float(plan_auto["boundary_wall_t"]), desde_cobertura + 1)
                if evidencia_tramo_completa(
                        d, desde_cobertura, hasta_cobertura, ahora):
                    auto_debido = True
                    print(
                        "[escriba] evidencia del tramo completa antes del "
                        f"margen de {MARGEN_TRANSCRIPTOR_SEG:g}s; "
                        "resumen adelantado.")
        elif intervalo_auto is not None:
            auto_debido = (
                efectivo == "grabando_activo"
                and ahora >= proximo_auto
                and ahora >= auto_retry_after
            )
        else:
            auto_debido = False
        pedido_debido = (
            pedido
            and efectivo in {
                "grabando_activo", "pausado_manual", "auto_pausado",
                "audio_requires_attention",
            }
            and ahora >= manual_retry_after
        )
        if pedido_debido or auto_debido:
            disparo = "auto" if auto_debido else "manual"
            desde_manual = max(ultimo_auto, ultimo_manual, ultimo_final)
            hasta_manual = max(corte, desde_manual + 1)
            manual_satisfecho_por_auto = False
            if auto_debido:
                desde = max(ultimo_auto, ultimo_manual, ultimo_final)
                if modo_captura_util:
                    hasta = max(float(plan_auto["boundary_wall_t"]), desde + 1)
                else:
                    hasta = max(
                        proximo_auto - MARGEN_TRANSCRIPTOR_SEG, desde + 1)
                manual_satisfecho_por_auto = bool(
                    pedido_debido
                    and desde <= desde_manual
                    and hasta >= hasta_manual
                )
                forzar = manual_satisfecho_por_auto
            else:
                desde = desde_manual
                hasta = hasta_manual
                forzar = True
            msg_generando = (
                f"Generando resumen automático de {intervalo_auto / 60:g} min..."
                if disparo == "auto" else "generando resumen..."
            )
            est.actualizar_estado(
                d, resumen_estado="generando", resumen_msg=msg_generando,
                resumen_t=time.time())
            bloque_generado = False
            try:
                bloque_generado = procesar_bloque(
                    d, desde, hasta, st["parte"], forzar=forzar,
                    disparo=disparo)
            except Exception as e:
                config.log_error("escriba", f"{type(e).__name__}: {e}")
                if disparo == "auto":
                    auto_fallos += 1
                    auto_retry_after = ahora + min(60.0, 5.0 * (2 ** min(
                        auto_fallos - 1, 4)))
                else:
                    manual_fallos += 1
                    manual_retry_after = ahora + min(60.0, 5.0 * (2 ** min(
                        manual_fallos - 1, 4)))
                est.actualizar_estado(
                    d, resumen_estado="error",
                    resumen_msg=("No se pudo generar el resumen; se conservará "
                                 "el transcript crudo."),
                    resumen_t=time.time())
            else:
                info_bloque = getattr(procesar_bloque, "ultimo_estado", {}) or {}
                if bloque_generado:
                    if disparo == "auto":
                        auto_fallos = 0
                        auto_retry_after = 0.0
                    else:
                        manual_fallos = 0
                        manual_retry_after = 0.0
                    estado_resumen = info_bloque.get("estado") or "generado"
                    msg_resumen = info_bloque.get("msg") or "Resumen generado."
                    if estado_resumen == "parcial":
                        msg_resumen = "Resumen parcial; transcript conservado"
                    elif disparo == "auto":
                        msg_resumen = (
                            f"Resumen automático de {intervalo_auto / 60:g} min generado."
                        )
                    est.actualizar_estado(
                        d, resumen_estado=estado_resumen,
                        resumen_msg=msg_resumen, resumen_t=time.time())
                    if auto_debido:
                        ultimo_auto = hasta
                        auto_anchor = hasta
                        proximo_auto = _avanzar_proximo_auto(
                            proximo_auto, ahora, intervalo=intervalo_auto)
                        if manual_satisfecho_por_auto:
                            ultimo_manual = max(ultimo_manual, hasta_manual)
                    else:
                        ultimo_manual = hasta
                        # El botón manual solo adelanta contenido. La grilla automática
                        # permanece anclada a 00:00, 05:00, 10:00... de captura útil.
                    _guardar_estado_escriba(
                        estado_propio, ultimo_auto, ultimo_manual, proximo_auto,
                        ultimo_final_t=ultimo_final, auto_anchor_t=auto_anchor)
                else:
                    ventana_vacia = bool(
                        auto_debido
                        and _ventana_auto_resuelta_sin_texto(
                            d, desde, hasta, proximo_auto, ahora)
                    )
                    if ventana_vacia:
                        _registrar_ventana_auto_vacia(
                            d, st["parte"], desde, hasta, proximo_auto)
                        ultimo_auto = hasta
                        auto_anchor = hasta
                        proximo_auto = _avanzar_proximo_auto(
                            proximo_auto, ahora, intervalo=intervalo_auto)
                        auto_fallos = 0
                        auto_retry_after = 0.0
                        _guardar_estado_escriba(
                            estado_propio, ultimo_auto, ultimo_manual, proximo_auto,
                            ultimo_final_t=ultimo_final, auto_anchor_t=auto_anchor)
                        est.actualizar_estado(
                            d, resumen_estado="sin_transcript",
                            resumen_msg=("Frontera automática registrada sin "
                                         "transcript nuevo; calendario conservado."),
                            resumen_t=time.time())
                    else:
                        est.actualizar_estado(
                            d, resumen_estado="sin_transcript",
                            resumen_msg=("No hay suficiente transcripción nueva para resumir. "
                                         "La solicitud sigue pendiente mientras Whisper termina."),
                            resumen_t=time.time())
            pedido_atendido = pedido_debido and bloque_generado and (
                not auto_debido or manual_satisfecho_por_auto)
            if pedido_atendido:
                est.actualizar_estado(
                    d, resumir=False, resumen_solicitado_t=0.0)
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
