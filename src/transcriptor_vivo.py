"""Materializa transcript/commits desde WAV sellados mediante STT remoto.

- La cadena AssemblyAI → Deepgram → Speechmatics → Groq es el único backend.
- Reanudable: al arrancar lee transcript.jsonl y no re-procesa chunks ya hechos.
- Cada línea: {"t": epoch_absoluto, "ini": s, "fin": s, "texto": str, "chunk": str, "parte": n}
- Las pruebas de proveedores usan clientes simulados y nunca consumen cuota.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import audio_manifest
from . import config
from . import estado as est
from . import student_notes
from . import stt_backend
from .compat_av import cargar_wav16k, estirar_wsola
from .incremental_jsonl import AppendOnlyJsonlReader

PATRON_CLASE_NUM = re.compile(r"^(?:el\s+)?clase\s+\d+\.?$", re.IGNORECASE)
EXIT_LOCK_CONFLICT = 3
_LAST_LIFECYCLE_FINGERPRINT = None
_LAST_LIFECYCLE_WRITE = 0.0


def _publicar_lifecycle(d: Path, status: str, **campos) -> None:
    global _LAST_LIFECYCLE_FINGERPRINT, _LAST_LIFECYCLE_WRITE
    fingerprint = (
        str(d), str(status), tuple(sorted(
            (str(key), repr(value)) for key, value in campos.items()
            if key != "heartbeat")))
    now_mono = time.monotonic()
    if (fingerprint == _LAST_LIFECYCLE_FINGERPRINT
            and now_mono - _LAST_LIFECYCLE_WRITE < 15.0):
        return
    lifecycle = {
        "status": str(status),
        "heartbeat": time.time(),
        **{k: v for k, v in campos.items() if v is not None},
    }
    try:
        est.actualizar_estado(d, stt_lifecycle=lifecycle)
        _LAST_LIFECYCLE_FINGERPRINT = fingerprint
        _LAST_LIFECYCLE_WRITE = now_mono
    except Exception:
        pass


def debe_cerrar_transcriptor(*, estado_sesion: str, sin_procesar: int,
                             captura_cerrada: bool,
                             latido_perdido: bool) -> bool:
    """El fin normal espera al productor; un proceso huérfano sí puede salir."""
    if int(sin_procesar) > 0:
        return False
    if latido_perdido:
        return True
    return str(estado_sesion) == "fin" and bool(captura_cerrada)


def debe_estacionar_pendientes_remotos(*, estado_sesion: str,
                                       captura_cerrada: bool,
                                       sin_procesar: int,
                                       listos: int) -> bool:
    """Fin determinista: conserva cola no lista sin mantener un proceso girando."""
    return bool(
        str(estado_sesion) == "fin" and captura_cerrada
        and int(sin_procesar) > 0 and int(listos) == 0)


def registros_sellados_para_stt(d: Path) -> list[dict]:
    """Expone solo eventos publicados; nunca infiere huérfanos con manifest activo."""
    rows = audio_manifest.capture_records(d)
    if audio_manifest.manifest_path(d).is_file():
        rows = [row for row in rows if not row.get("inferred_with_manifest")]
    return rows


@dataclass(frozen=True)
class SttQueueSnapshot:
    records: list[dict]
    metadata_by_path: dict[str, dict]
    all_wav: list[Path]
    unprocessed: list[Path]
    ready: list[Path]
    retry_state: dict[str, dict]
    failed_counts: dict[str, int]


class SttQueueIndex:
    """Cola durable: reconstruccion inicial y lecturas append-only posteriores.

    El polling de coordinacion sigue siendo deliberado para observar Pausa/Fin y
    deadlines de retry, pero cuando no cambia nada solo hace ``stat`` sobre dos
    JSONL. Un reemplazo, truncado o reinicio fuerza una reconstruccion segura.
    """

    def __init__(self, class_dir: Path, processed: set[str] | None = None, *,
                 base_delay: float = 30.0, max_delay: float = 300.0):
        self.class_dir = Path(class_dir)
        self.audio_dir = self.class_dir / "audio"
        self.manifest_path = audio_manifest.manifest_path(self.class_dir)
        self.manifest_reader = AppendOnlyJsonlReader(self.manifest_path)
        self.attempt_reader = AppendOnlyJsonlReader(
            audio_manifest.transcription_attempts_path(self.class_dir))
        self.processed = set(processed or ())
        self.base_delay = max(0.0, float(base_delay))
        self.max_delay = max(0.0, float(max_delay))
        self._processed_revision = 0
        self._manifest_records_version = -1
        self._manifest_records: list[dict] = []
        self._legacy_signature = None
        self._legacy_records: list[dict] = []
        self._cached_key = None
        self._cached: SttQueueSnapshot | None = None
        self._next_retry_at: float | None = None
        self.rebuilds = 0
        self.cache_hits = 0

    def mark_processed(self, chunk_id: str) -> None:
        chunk_id = str(chunk_id or "")
        if chunk_id and chunk_id not in self.processed:
            self.processed.add(chunk_id)
            self._processed_revision += 1

    def _legacy_dir_signature(self):
        try:
            stat_result = self.audio_dir.stat()
            return int(stat_result.st_mtime_ns), int(stat_result.st_size)
        except OSError:
            return None

    def _source_rows(self) -> tuple[list[dict], list[dict], tuple]:
        if self.manifest_path.is_file():
            events = self.manifest_reader.refresh()
            if self.manifest_reader.version != self._manifest_records_version:
                self._manifest_records = audio_manifest.capture_records_from_events(events)
                self._manifest_records_version = self.manifest_reader.version
            return events, self._manifest_records, (
                "manifest", self.manifest_reader.version)

        # Compatibilidad de solo lectura para clases legacy sin manifest. La
        # enumeracion WAV se repite solo cuando cambia el directorio.
        self.manifest_reader.refresh()
        signature = self._legacy_dir_signature()
        if signature != self._legacy_signature:
            self._legacy_records = audio_manifest.infer_legacy(self.class_dir)
            self._legacy_signature = signature
        return [], self._legacy_records, ("legacy", signature)

    @staticmethod
    def _safe_wav_path(audio_dir: Path, row: dict) -> Path | None:
        chunk_id = str(row.get("chunk_id") or "")
        raw = str(row.get("path") or (f"{chunk_id}.wav" if chunk_id else ""))
        if (not raw or Path(raw).name != raw or not raw.startswith("chunk_")
                or Path(raw).suffix.casefold() != ".wav"):
            return None
        path = audio_dir / raw
        return path if path.is_file() else None

    def snapshot(self, now: float | None = None) -> SttQueueSnapshot:
        now = time.time() if now is None else float(now)
        events, records, source_key = self._source_rows()
        attempts = self.attempt_reader.refresh()
        key = (source_key, self.attempt_reader.version, self._processed_revision)
        retry_due = self._next_retry_at is not None and now >= self._next_retry_at
        if self._cached is not None and key == self._cached_key and not retry_due:
            self.cache_hits += 1
            return self._cached

        metadata: dict[str, dict] = {}
        all_wav: list[Path] = []
        seen_paths: set[str] = set()
        for row in records:
            path = self._safe_wav_path(self.audio_dir, row)
            if path is None or path.name in seen_paths:
                continue
            seen_paths.add(path.name)
            metadata[path.name] = row
            all_wav.append(path)

        retry_state = audio_manifest.transcription_retry_state_from_rows(
            events, attempts, now=now, base_delay=self.base_delay,
            max_delay=self.max_delay)
        unprocessed = [p for p in all_wav if p.stem not in self.processed]
        ready = [
            p for p in unprocessed
            if p.stem not in retry_state or retry_state[p.stem].get("ready")
        ]
        waiting_deadlines = [
            float(info.get("retry_after"))
            for info in retry_state.values()
            if info.get("retryable") and not info.get("ready")
            and info.get("retry_after") is not None
        ]
        self._next_retry_at = min(waiting_deadlines, default=None)
        snapshot = SttQueueSnapshot(
            records=records,
            metadata_by_path=metadata,
            all_wav=all_wav,
            unprocessed=unprocessed,
            ready=ready,
            retry_state=retry_state,
            failed_counts={
                chunk_id: int(info.get("failures", 0))
                for chunk_id, info in retry_state.items()
            },
        )
        self._cached_key = key
        self._cached = snapshot
        self.rebuilds += 1
        return snapshot

    def attempt_history(self) -> list[dict]:
        return self.attempt_reader.refresh()

    def metrics(self) -> dict:
        return {
            "rebuilds": self.rebuilds,
            "cache_hits": self.cache_hits,
            "manifest": self.manifest_reader.metrics(),
            "attempts": self.attempt_reader.metrics(),
        }


def metricas_commit_stt(row_manifest: dict, *, committed_at: float,
                        transcribe_seconds: float, audio_seconds: float,
                        backlog: int) -> dict:
    """Telemetría final; no mezcla subtítulo provisional con commit durable."""
    try:
        source_wall_end = float(row_manifest.get(
            "pcm_wall_end", row_manifest.get("wall_end")) or 0.0)
    except (TypeError, ValueError):
        source_wall_end = 0.0
    audio_seconds = max(0.0, float(audio_seconds))
    transcribe_seconds = max(0.0, float(transcribe_seconds))
    return {
        "committed_at": float(committed_at),
        "source_wall_end": source_wall_end,
        "commit_latency_seconds": round(
            max(0.0, float(committed_at) - source_wall_end), 3)
        if source_wall_end else None,
        "transcribe_seconds": round(transcribe_seconds, 3),
        "audio_seconds": round(audio_seconds, 3),
        "real_time_factor": round(transcribe_seconds / audio_seconds, 4)
        if audio_seconds else None,
        "backlog_wav": max(0, int(backlog)),
        "provisional": False,
    }






def velocidad_desde_chunk(ruta: Path) -> float:
    partes = ruta.stem.split("_")  # chunk_<epoch>_p<parte>[_v<vel*10>]
    try:
        return int(partes[3][1:]) / 10 if len(partes) > 3 else 1.0
    except (ValueError, IndexError):
        return 1.0


def modelo_para_velocidad(velocidad: float) -> str:
    """La velocidad cambia el derivado WSOLA, no el orden de proveedores."""
    return "remote-failover"


def contexto_whisper(nombre: str) -> str:
    """Prompt STT mínimo y no genérico.

    Un prompt como "Clase sobre Clase 1" puede sesgar al STT en x2 y
    producir bucles "Clase 3, Clase 4...". Solo se usa si aporta vocabulario.
    """
    nombre = " ".join(str(nombre or "").strip().split())
    if not nombre:
        return ""
    solo_generico = re.sub(r"\b(clase|sesion|sesión|semana|parte|curso)\b", " ",
                           nombre, flags=re.IGNORECASE)
    solo_generico = re.sub(r"[\d\W_]+", " ", solo_generico).strip()
    if len(solo_generico) < 4:
        return ""
    return f"Tema: {nombre}."


def contexto_para_chunk(contexto: str, velocidad: float) -> str:
    if not contexto:
        return ""
    if float(velocidad or 1.0) >= 1.5 and os.environ.get("CLASE_STT_PROMPT_X2") != "1":
        return ""
    return contexto


def _normalizar_texto(texto: str) -> str:
    return " ".join(re.findall(r"[\wáéíóúüñ]+", (texto or "").lower(), re.UNICODE))


def _tokens(texto: str) -> list[str]:
    return re.findall(r"[\wáéíóúüñ]+", (texto or "").lower(), re.UNICODE)


def texto_clase_num(texto: str) -> bool:
    return bool(PATRON_CLASE_NUM.match((texto or "").strip()))


def texto_repetitivo(texto: str) -> bool:
    """Detecta alucinaciones compactas: "Clase N", "sí sí sí" o n-gramas en bucle."""
    texto = (texto or "").strip()
    if not texto:
        return False
    if texto_clase_num(texto):
        return True
    toks = _tokens(texto)
    if len(toks) < 10:
        return False
    unicos = set(toks)
    if len(toks) >= 18 and len(unicos) <= 3:
        return True
    for n in range(1, 5):
        if len(toks) < n * 5:
            continue
        conteo: dict[tuple[str, ...], int] = {}
        for i in range(0, len(toks) - n + 1):
            ng = tuple(toks[i:i + n])
            conteo[ng] = conteo.get(ng, 0) + 1
        repetido = max(conteo.values(), default=0)
        cobertura = (repetido * n) / max(1, len(toks))
        if repetido >= 5 and cobertura >= 0.45:
            return True
    return False


def segmento_debe_descartarse(texto: str, avg_logprob=None, no_speech_prob=None,
                              compression_ratio=None) -> bool:
    """Filtro conservador: no escribe segmentos que Whisper marcó como no habla y malos."""
    try:
        no_speech = float(no_speech_prob)
    except (TypeError, ValueError):
        no_speech = None
    try:
        logprob = float(avg_logprob)
    except (TypeError, ValueError):
        logprob = None
    try:
        comp = float(compression_ratio)
    except (TypeError, ValueError):
        comp = None

    if no_speech is not None and logprob is not None and no_speech >= 0.80 and logprob <= -1.0:
        return True
    if comp is not None and comp >= 3.0 and len(texto or "") > 40:
        return True
    # No descartar "Clase 3" de forma aislada aquí; se filtra solo cuando el chunk
    # completo muestra patrón de bucle.
    return texto_repetitivo(texto) and not texto_clase_num(texto)


def diagnosticar_repeticion_lineas(lineas: list[dict] | list[str],
                                   incluir_repeticion_interna: bool = True) -> dict:
    textos = []
    for item in lineas:
        if isinstance(item, dict):
            textos.append(str(item.get("texto") or item.get("text") or "").strip())
        else:
            textos.append(str(item).strip())
    textos = [t for t in textos if t]
    total = len(textos)
    clase_n = sum(1 for t in textos if texto_clase_num(t))
    repetitivos = sum(1 for t in textos if texto_repetitivo(t))
    cortos = [_normalizar_texto(t) for t in textos if len(t) <= 40]
    frec = {}
    for t in cortos:
        if t:
            frec[t] = frec.get(t, 0) + 1
    max_corto = max(frec.values(), default=0)
    sospechoso = False
    razones = []
    if clase_n >= 3 or (total and clase_n / total >= 0.25):
        sospechoso = True
        razones.append("clase_n_loop")
    if incluir_repeticion_interna and repetitivos >= 2 and total and repetitivos / total >= 0.20:
        sospechoso = True
        razones.append("frases_repetitivas")
    if max_corto >= 3:
        sospechoso = True
        razones.append("frase_corta_repetida")
    return {
        "sospechoso": sospechoso,
        "razones": razones,
        "segmentos": total,
        "clase_n": clase_n,
        "repetitivos": repetitivos,
        "max_frase_corta": max_corto,
    }


def filtrar_lineas_repetitivas(lineas: list[dict]) -> list[dict]:
    diag = diagnosticar_repeticion_lineas(lineas)
    if not diag["sospechoso"]:
        return lineas
    vistas: dict[str, int] = {}
    limpias: list[dict] = []
    for linea in lineas:
        texto = str(linea.get("texto") or "").strip()
        norm = _normalizar_texto(texto)
        vistas[norm] = vistas.get(norm, 0) + 1
        if texto_clase_num(texto):
            continue
        if texto_repetitivo(texto):
            continue
        if len(texto) <= 40 and vistas[norm] > 2:
            continue
        limpias.append(linea)
    return limpias


def transcribir_con_reintento(transcribir_base, transcribir_retry) -> tuple[list[dict], dict]:
    """Filtra una respuesta sospechosa sin realizar una segunda llamada facturable."""
    lineas = transcribir_base()
    diag = diagnosticar_repeticion_lineas(lineas)
    if not diag["sospechoso"]:
        diag["retry"] = False
        diag["filtrado"] = False
        return lineas, diag

    filtradas = filtrar_lineas_repetitivas(lineas)
    diag_filtrado = diagnosticar_repeticion_lineas(filtradas)
    diag_filtrado["retry"] = False
    diag_filtrado["filtrado"] = True
    diag_filtrado["razones_originales"] = diag.get("razones", [])
    return filtradas, diag_filtrado


def _float_env(nombre: str, default: float) -> float:
    try:
        return float(os.environ.get(nombre, str(default)))
    except (TypeError, ValueError):
        return default


def normalizar_audio_whisper(
        datos: np.ndarray,
        target_rms: float | None = None,
        max_gain: float | None = None,
        floor_rms: float | None = None,
        floor_peak: float | None = None) -> tuple[np.ndarray, dict]:
    """Amplifica voz real de baja amplitud antes de Whisper sin levantar PCM plano."""
    target_rms = target_rms if target_rms is not None else _float_env(
        "CLASE_STT_TARGET_RMS", 0.035)
    max_gain = max_gain if max_gain is not None else _float_env(
        "CLASE_STT_MAX_GAIN", 32.0)
    floor_rms = floor_rms if floor_rms is not None else _float_env(
        "CLASE_STT_GAIN_FLOOR_RMS", 0.00002)
    floor_peak = floor_peak if floor_peak is not None else _float_env(
        "CLASE_STT_GAIN_FLOOR_PEAK", 0.00012)

    x = np.asarray(datos, dtype=np.float32)
    diag = {
        "applied": False,
        "gain": 1.0,
        "rms_before": 0.0,
        "rms_after": 0.0,
        "peak_before": 0.0,
        "peak_after": 0.0,
        "reason": "",
    }
    if x.size == 0:
        diag["reason"] = "empty"
        return x, diag
    if not np.all(np.isfinite(x)):
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)

    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    peak = float(np.max(np.abs(x)))
    diag.update({
        "rms_before": round(rms, 7),
        "rms_after": round(rms, 7),
        "peak_before": round(peak, 7),
        "peak_after": round(peak, 7),
    })
    if rms < max(0.0, floor_rms) or peak < max(0.0, floor_peak):
        diag["reason"] = "near_silence"
        return x, diag
    if target_rms <= 0 or max_gain <= 1.0 or rms >= target_rms:
        diag["reason"] = "already_ok"
        return x, diag

    clip_gain = 0.98 / peak if peak > 0 else 1.0
    gain = min(float(max_gain), target_rms / rms, clip_gain)
    if gain <= 1.05:
        diag["reason"] = "gain_too_small"
        return x, diag

    y = np.clip(x * gain, -0.98, 0.98).astype(np.float32, copy=False)
    rms_after = float(np.sqrt(np.mean(np.square(y, dtype=np.float64))))
    peak_after = float(np.max(np.abs(y))) if y.size else 0.0
    diag.update({
        "applied": True,
        "gain": round(float(gain), 2),
        "rms_after": round(rms_after, 7),
        "peak_after": round(peak_after, 7),
        "reason": "low_volume",
    })
    return y, diag


def _ventanas_audio(datos: np.ndarray, sr: int = 16000) -> list[tuple[float, np.ndarray]]:
    dur = len(datos) / sr if len(datos) else 0.0
    ventana = max(12.0, float(os.environ.get("CLASE_STT_WINDOW_SEG", "28")))
    solape = min(4.0, max(0.0, float(os.environ.get("CLASE_STT_OVERLAP_SEG", "1.5"))))
    if dur <= ventana + solape:
        return [(0.0, datos)]
    paso = max(5.0, ventana - solape)
    out: list[tuple[float, np.ndarray]] = []
    inicio = 0.0
    while inicio < dur:
        i0 = int(inicio * sr)
        i1 = min(len(datos), int((inicio + ventana) * sr))
        if i1 <= i0:
            break
        out.append((inicio, datos[i0:i1]))
        if i1 >= len(datos):
            break
        inicio += paso
    return out


def _transcribir_audio(modelo, datos: np.ndarray, epoch: float, parte: int, vel: float,
                       contexto: str, kw: dict, opciones: dict | None = None) -> list[dict]:
    opciones = opciones or {}
    lineas: list[dict] = []
    ultimo_fin_estirado = -1.0
    solape_tol = max(0.2, float(os.environ.get("CLASE_STT_OVERLAP_SEG", "1.5")) * 0.65)
    ventanas = (
        [(0.0, datos)] if getattr(modelo, "whole_chunk", False)
        else _ventanas_audio(datos)
    )
    for offset, frag in ventanas:
        transcribe_kw = {
            "vad_filter": opciones.get("vad_filter", True),
            "beam_size": opciones.get("beam_size", 5),
            "initial_prompt": contexto or None,
            "condition_on_previous_text": opciones.get("condition_on_previous_text", False),
            "temperature": opciones.get("temperature", 0),
        }
        if opciones.get("strict"):
            transcribe_kw.update({
                "compression_ratio_threshold": float(os.environ.get(
                    "CLASE_STT_STRICT_COMPRESSION", "1.8")),
                "log_prob_threshold": float(os.environ.get(
                    "CLASE_STT_STRICT_LOGPROB", "-0.5")),
                "no_speech_threshold": float(os.environ.get(
                    "CLASE_STT_STRICT_NO_SPEECH", "0.35")),
            })
        transcribe_kw.update(kw)
        segments, info = modelo.transcribe(
            frag,
            **transcribe_kw)
        idioma = kw.get("language") or (
            info.language if info.language_probability >= 0.5 else "es")
        for s in segments:
            ini_est = offset + float(s.start)
            fin_est = offset + float(s.end)
            texto = s.text.strip()
            if ini_est < ultimo_fin_estirado - solape_tol:
                if fin_est <= ultimo_fin_estirado + 0.05:
                    continue
                # Un segmento puede empezar en el solape y terminar con palabras
                # nuevas. No descartarlo completo: recortar reloj y prefijo ya
                # comprometido cuando Whisper conserva la misma frase.
                if lineas:
                    texto = _quitar_prefijo_repetido(
                        texto, str(lineas[-1].get("texto") or ""), min_tokens=2)
                ini_est = max(ini_est, ultimo_fin_estirado)
            if texto and not segmento_debe_descartarse(
                    texto,
                    avg_logprob=getattr(s, "avg_logprob", None),
                    no_speech_prob=getattr(s, "no_speech_prob", None),
                    compression_ratio=getattr(s, "compression_ratio", None)):
                lineas.append({
                    "t": epoch + ini_est / vel,
                    "ini": round(ini_est / vel, 2),
                    "fin": round(fin_est / vel, 2),
                    "texto": texto,
                    "chunk": "",
                    "parte": parte,
                    "idioma": idioma,
                })
                ultimo_fin_estirado = max(ultimo_fin_estirado, fin_est)
    return lineas


def _partes_ruta_chunk(ruta: Path) -> tuple[int, int, float]:
    partes = ruta.stem.split("_")
    return int(partes[1]), int(partes[2][1:]), velocidad_desde_chunk(ruta)


def _quitar_prefijo_repetido(texto: str, texto_anterior: str,
                              min_tokens: int = 3) -> str:
    """Quita el mayor sufijo/prefijo comun en una frontera de audio."""
    spans_actual = list(re.finditer(r"[\wáéíóúüñ]+", texto or "", re.IGNORECASE))
    tokens_actual = [m.group(0).lower() for m in spans_actual]
    tokens_anterior = _tokens(texto_anterior)
    maximo = min(len(tokens_actual), len(tokens_anterior), 24)
    solape = 0
    for n in range(maximo, min_tokens - 1, -1):
        if tokens_anterior[-n:] == tokens_actual[:n]:
            solape = n
            break
    if not solape:
        return (texto or "").strip()
    if solape >= len(spans_actual):
        return ""
    return texto[spans_actual[solape - 1].end():].lstrip(" ,.;:-").strip()


def deduplicar_frontera(lineas: list[dict], anteriores: list[dict],
                        inicio_actual: float, ventana_seg: float = 8.0) -> list[dict]:
    """Elimina texto ya comprometido por el chunk anterior, solo en la frontera."""
    cercanas = [
        ln for ln in anteriores
        if float(ln.get("t") or 0.0) >= float(inicio_actual) - ventana_seg
        and float(ln.get("t") or 0.0) <= float(inicio_actual) + 0.25
        and str(ln.get("texto") or "").strip()
    ]
    if not cercanas:
        return lineas
    out: list[dict] = []
    for linea in lineas:
        actual = dict(linea)
        texto = str(actual.get("texto") or "").strip()
        if not texto:
            continue
        for previa in reversed(cercanas):
            texto = _quitar_prefijo_repetido(
                texto, str(previa.get("texto") or ""))
            if not texto:
                break
        if texto:
            actual["texto"] = texto
            out.append(actual)
    return out


def leer_contexto_frontera(path: Path, modelo, *, max_bytes: int = 262_144,
                           max_lines: int = 160) -> list[dict]:
    """Lee solo la cola necesaria para deduplicar; remoto no usa solape."""
    if not getattr(modelo, "cross_chunk_overlap", True) or not path.is_file():
        return []
    try:
        size = path.stat().st_size
        offset = max(0, size - max(4096, int(max_bytes)))
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read()
        if offset:
            _, _, raw = raw.partition(b"\n")
        rows = []
        for line in raw.splitlines():
            try:
                item = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError, TypeError):
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows[-max(1, int(max_lines)):]
    except OSError:
        return []


def _recortar_contexto_previo(lineas: list[dict], solape_reloj: float,
                               inicio_actual: float) -> list[dict]:
    """Convierte offsets del audio concatenado a offsets del chunk actual."""
    if solape_reloj <= 0:
        return lineas
    out = []
    for linea in lineas:
        fin = float(linea.get("fin") or 0.0) - solape_reloj
        if fin <= -0.15:
            continue
        actual = dict(linea)
        ini = max(0.0, float(actual.get("ini") or 0.0) - solape_reloj)
        fin = max(ini, fin)
        actual["ini"] = round(ini, 2)
        actual["fin"] = round(fin, 2)
        actual["t"] = float(inicio_actual) + ini
        actual["solape_previo_seg"] = round(solape_reloj, 2)
        out.append(actual)
    return out


_TAIL_SOLAPE_SEG = 5.0
_ultimo_tail_solape: dict | None = None


def _firma_wav(ruta: Path) -> tuple[str, int, int] | None:
    try:
        stat = ruta.stat()
    except OSError:
        return None
    return (str(ruta), int(stat.st_mtime_ns), int(stat.st_size))


def _guardar_tail_solape(ruta: Path, vel: float, datos: np.ndarray) -> None:
    """Retiene el final del derivado post-WSOLA del chunk recién procesado.

    Copia solo los últimos segundos: el bucle secuencial reutiliza ese tail como
    solape del chunk siguiente sin recargar ni re-estirar el WAV anterior. La
    firma (ruta, mtime, size) invalida el tail si el archivo cambiara en disco.
    """
    global _ultimo_tail_solape
    firma = _firma_wav(ruta)
    if firma is None:
        return
    muestras = max(1, round(_TAIL_SOLAPE_SEG * 16000))
    _ultimo_tail_solape = {
        "firma": firma,
        "vel": float(vel),
        "total_len": len(datos),
        "tail": np.array(datos[-muestras:], copy=True),
    }


def _tail_solape_cacheado(ruta: Path, vel: float,
                          muestras: int) -> np.ndarray | None:
    """Tail bit-idéntico al recompute, o None para caer a la ruta actual."""
    cache = _ultimo_tail_solape
    if (cache is None or cache["firma"] != _firma_wav(ruta)
            or abs(cache["vel"] - float(vel)) > 1e-9):
        return None
    tail = cache["tail"]
    if muestras <= 0:
        return tail[:0]
    if len(tail) >= muestras:
        return tail[-muestras:]
    if cache["total_len"] == len(tail):
        # El chunk anterior entero era más corto que la ventana pedida: el
        # recompute usaría exactamente estas muestras (min(len, ventana)).
        return tail
    return None


def transcribir_chunk(modelo, ruta: Path, contexto: str = "",
                      inicio_abs: float | None = None,
                      anterior: Path | None = None,
                      lineas_anteriores: list[dict] | None = None,
                      permitir_solape_anterior: bool = True) -> list[dict]:
    """Devuelve las líneas jsonl de un chunk (t absoluto = epoch del nombre + offset).

    - `contexto`: initial_prompt con el tema de la clase; ancla el vocabulario técnico y
      reduce errores (p. ej. cadera/ilion/piriforme en vez de 'cadena/hílio'). Clave a 2x.
    - Si el chunk se grabó viendo una GRABACIÓN acelerada (sufijo _v15/_v20), el audio se
      ESTIRA con WSOLA para devolverle el tempo natural antes de Whisper.
    """
    epoch_nombre, parte, vel = _partes_ruta_chunk(ruta)
    epoch = float(epoch_nombre if inicio_abs is None else inicio_abs)
    datos = cargar_wav16k(ruta)
    # El gate usa el PCM derivado directamente del WAV canónico, antes de que
    # normalización o WSOLA puedan elevar ruido y provocar una petición facturable.
    useful, silence_diag = stt_backend.wav_has_useful_signal(datos)
    if not useful:
        modelo.last_provider = "silence_gate"
        modelo.last_model = "pcm-energy-v1"
        modelo.last_request_id = ""
        modelo.last_submitted_audio_seconds = 0.0
        modelo.last_silence_diagnostics = silence_diag
        return []
    if vel > 1.01:
        datos = estirar_wsola(datos, vel, sr=16000)
    # Referencia al derivado post-WSOLA sin solape: es lo que un recompute del
    # siguiente chunk obtendría, y se retiene recién después de consumir el
    # tail del anterior (slot único).
    datos_estirados = datos
    solape_estirado = 0.0
    permitir_solape_anterior = bool(
        permitir_solape_anterior
        and getattr(modelo, "cross_chunk_overlap", True))
    if permitir_solape_anterior and anterior is not None and anterior.is_file():
        try:
            _epoch_ant, parte_ant, vel_ant = _partes_ruta_chunk(anterior)
            # Un cambio de parte es una marca editorial, no una discontinuidad
            # fisica. Una velocidad distinta si cambia la escala temporal.
            compatible = abs(vel_ant - vel) <= 0.01
            if compatible:
                segundos = max(0.0, float(os.environ.get(
                    "CLASE_STT_CROSS_CHUNK_OVERLAP_SEG", "1.5")))
                muestras = round(segundos * 16000)
                tail = _tail_solape_cacheado(anterior, vel_ant, muestras)
                if tail is None:
                    datos_ant = cargar_wav16k(anterior)
                    if vel_ant > 1.01:
                        datos_ant = estirar_wsola(datos_ant, vel_ant, sr=16000)
                    n = min(len(datos_ant), muestras)
                    tail = datos_ant[-n:] if n else datos_ant[:0]
                n = len(tail)
                if n:
                    datos = np.concatenate((tail, datos))
                    solape_estirado = n / 16000.0
        except (OSError, ValueError, IndexError):
            solape_estirado = 0.0
    _guardar_tail_solape(ruta, vel, datos_estirados)
    datos, diag_gain = normalizar_audio_whisper(datos)
    if diag_gain.get("applied"):
        print(f"[transcriptor] normalizado {ruta.name}: "
              f"gain={diag_gain['gain']}x rms={diag_gain['rms_before']}"
              f"->{diag_gain['rms_after']}", flush=True)
    # Idioma: es|en forzado, o AUTO por chunk (clases mixtas; evita 'español inventado'
    # cuando el profesor pone un video en inglés). Con prob baja se asume español.
    kw = {"language": config.IDIOMA} if config.IDIOMA in ("es", "en") else {}
    contexto_chunk = contexto_para_chunk(contexto, vel)

    base_epoch = epoch - solape_estirado / vel

    def base():
        return _transcribir_audio(
            modelo, datos, base_epoch, parte, vel, contexto_chunk, kw)

    def retry():
        return _transcribir_audio(
            modelo, datos, base_epoch, parte, vel, "", kw,
            {"vad_filter": True, "condition_on_previous_text": False, "strict": True})

    lineas, diag = transcribir_con_reintento(base, retry)
    if diag.get("retry"):
        print(f"[transcriptor] repetición sospechosa filtrada en {ruta.name}: "
              f"{','.join(diag.get('razones_originales') or diag.get('razones') or [])}")
    lineas = _recortar_contexto_previo(
        lineas, solape_estirado / vel, epoch)
    lineas = deduplicar_frontera(
        lineas, lineas_anteriores or [], epoch)
    for linea in lineas:
        linea["chunk"] = ruta.name
    return lineas


def duracion_wav_seg(ruta: Path) -> float:
    try:
        with wave.open(str(ruta), "rb") as wf:
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return wf.getnframes() / rate
    except Exception:
        return 0.0


def chunks_contiguos(previous: dict, current: dict,
                     max_wall_gap: float = 0.2) -> bool:
    """Decide si el WAV previo es contexto fisico valido para el actual."""
    if not previous or not current:
        return False
    discontinuity = sum(
        max(0, int(float(row.get(key) or 0)))
        for row in (previous, current)
        for key in ("gap_before_frames", "gap_frames", "timeout_padding_frames")
    )
    if discontinuity:
        return False
    previous_stream = str(previous.get("stream_id") or "")
    current_stream = str(current.get("stream_id") or "")
    if previous_stream and current_stream:
        if previous_stream != current_stream:
            return False
        if previous.get("end_frame") is not None and current.get("start_frame") is not None:
            return int(current["start_frame"]) == int(previous["end_frame"])
    previous_end = float(
        previous.get("pcm_wall_end")
        or previous.get("wall_end")
        or (float(previous.get("wall_start") or 0) + float(previous.get("duration") or 0))
    )
    current_start = float(
        current.get("pcm_wall_start") or current.get("wall_start") or 0)
    return bool(previous_end and current_start
                and abs(current_start - previous_end) <= max(0.0, max_wall_gap))


class AudioCambioDuranteStt(RuntimeError):
    """El WAV o su estado final cambió mientras Whisper lo procesaba."""


def snapshot_audio_input(d: Path, ruta: Path, row: dict) -> str:
    """Fija el hash real que debe seguir vigente al publicar el commit."""
    if row.get("status") and row.get("status") not in audio_manifest.FINAL_CAPTURE_STATES:
        raise AudioCambioDuranteStt(f"chunk no sellado: {ruta.name}")
    actual = audio_manifest.sha256_file(ruta)
    if not actual:
        raise AudioCambioDuranteStt(f"WAV no legible: {ruta.name}")
    esperado = str(row.get("sha256") or "")
    if esperado and actual != esperado:
        raise AudioCambioDuranteStt(f"hash manifest no coincide: {ruta.name}")
    return actual


def validar_audio_input(d: Path, ruta: Path, input_hash: str,
                        row_inicial: dict) -> dict:
    """Revalida bytes y transición manifest inmediatamente antes del commit."""
    actual = audio_manifest.sha256_file(ruta)
    if not actual or actual != input_hash:
        raise AudioCambioDuranteStt(f"WAV cambió durante STT: {ruta.name}")
    latest = audio_manifest.latest_by_chunk(d).get(ruta.stem)
    if latest:
        if latest.get("status") not in audio_manifest.FINAL_CAPTURE_STATES:
            raise AudioCambioDuranteStt(f"chunk dejó de estar sellado: {ruta.name}")
        manifest_hash = str(latest.get("sha256") or "")
        if manifest_hash and manifest_hash != input_hash:
            raise AudioCambioDuranteStt(f"manifest cambió durante STT: {ruta.name}")
        return latest
    if (row_inicial.get("status") in audio_manifest.FINAL_CAPTURE_STATES
            and not row_inicial.get("inferred_legacy")
            and not row_inicial.get("inferred_with_manifest")):
        # Desaparecer un evento final solo es posible por reemplazo externo del manifest.
        raise AudioCambioDuranteStt(f"evento final desapareció: {ruta.name}")
    return row_inicial




def _main() -> int:
    config.stdout_utf8()
    if "--test" in sys.argv:
        print("El test interactivo de Whisper local fue retirado; usa fixtures remotos sin red.")
        return 2

    d = est.clase_activa()
    if d is None:
        print("[transcriptor] no hay clase activa. Saliendo.")
        return 1
    if not config.instancia_unica(
            "transcriptor", modulo=__name__, class_dir=d):
        # El proceso perdedor no debe sobrescribir el lifecycle del owner real.
        print("[transcriptor] ya hay otro vivo (candado). Saliendo.")
        return EXIT_LOCK_CONFLICT
    config.iniciar_heartbeat_instancia("transcriptor", class_dir=d)
    trans_path = d / "transcript.jsonl"

    procesados = audio_manifest.completed_transcript_chunks(d)
    retry_base = float(os.environ.get("CLASE_STT_RETRY_BASE_SEG", "30"))
    retry_max = float(os.environ.get("CLASE_STT_RETRY_MAX_SEG", "300"))
    cola_stt = SttQueueIndex(
        d, procesados, base_delay=retry_base, max_delay=retry_max)
    # Un commit publicado antes de un corte de energia puede existir aunque la
    # vista legacy no alcanzara a materializarse.
    manifest_activo = audio_manifest.manifest_path(d).is_file()
    if manifest_activo or (d / audio_manifest.JOURNAL_DIR).is_dir():
        audio_manifest.materialize_transcript(d, verify_files=False)
    st0 = est.leer_estado(d)
    modelo_nom = "remote-failover"
    backend_requested = str(
        st0.get("stt_backend_requested")
        or stt_backend.configured_backend()).casefold()
    remote_consent = bool(st0.get("stt_remote_consent", False))
    print(
        f"[transcriptor] backend={backend_requested}; modelo={modelo_nom}; "
        f"velocidad={st0.get('velocidad', 1)}x")
    _publicar_lifecycle(
        d, "starting_backend", phase="remote_preflight", device="remote",
        backend="remote-chain", readiness="loading",
        gpu_failure_attributed=False, model=modelo_nom,
    )
    modelo, info = stt_backend.create_backend(
        backend=backend_requested, remote_consent=remote_consent)
    try:
        est.actualizar_estado(
            d,
            stt_model=info.get("model", modelo_nom),
            stt_provider=info.get("provider", "remote-chain"),
            stt_device=info.get("device", ""),
            stt_gpu=info.get("gpu", ""),
            stt_compute=info.get("compute_type", ""),
            stt_backend=info.get("backend", ""),
            stt_hardware=info,
            stt_readiness=info.get("readiness", ""),
            stt_fallback_reason=info.get("fallback_reason", ""),
            stt_model_load_sec=info.get("model_load_seconds"),
            stt_warmup_sec=info.get("warmup_seconds"),
            stt_ready_total_sec=info.get("total_ready_seconds"),
        )
    except Exception:
        pass
    _publicar_lifecycle(
        d, "running", device=info.get("device", ""),
        backend=info.get("backend", ""),
        readiness=info.get("readiness", ""),
        fallback_reason=info.get("fallback_reason", ""),
        compute_type=info.get("compute_type", ""),
        model=info.get("model", modelo_nom),
        pending_count=0, gpu_failure_attributed=False,
    )
    # initial_prompt solo si aporta vocabulario técnico. Se desactiva por defecto en x2.
    contexto = contexto_whisper(st0.get("nombre", ""))
    # Estado legacy explícito: no existe canal PCM ni transcript provisional.
    try:
        est.actualizar_estado(d, stt_provisional={
            "requested": False, "enabled": False, "status": "feature_removed",
            "canonical_authority": True,
        })
    except Exception:
        pass

    while True:
        st = est.leer_estado(d)
        cola = cola_stt.snapshot()
        metadata_por_path = cola.metadata_by_path
        todos_wav = cola.all_wav
        fallidos = dict(cola.failed_counts)
        sin_procesar = cola.unprocessed
        pendientes = cola.ready
        if not pendientes:
            # Prioridad: WAV canónico > intervención durable > preview efímero.
            # Así una vista provisional continua no puede dejar sin transcribir
            # una intervención que el estudiante ya decidió incluir.
            local_pending = student_notes.pending_audio_notes(
                d, included_only=True)
            if local_pending:
                note = local_pending[0]
                _publicar_lifecycle(
                    d, "processing_local_intervention",
                    phase="student_voice", pending_count=len(local_pending),
                    device=info.get("device", ""),
                    backend=info.get("backend", ""),
                    gpu_failure_attributed=False,
                )
                try:
                    student_notes.transcribe_audio_note(
                        d, str(note.get("note_id")),
                        teacher_capture_active=True,
                        model=modelo,
                        model_name=str(info.get("model") or modelo_nom),
                    )
                    print("[transcriptor] nota de voz del estudiante transcrita sin "
                          "alterar el transcript docente.")
                except Exception as error:
                    config.log_error(
                        "stt_intervencion_local",
                        f"{type(error).__name__}: {error}")
                continue
            latido_perdido = config.latido_perdido()
            captura_cerrada = False
            if st["estado"] == "fin":
                captura_cerrada = bool(
                    audio_manifest.completion_barrier_report(d).get(
                        "capture_session_closed"))
            if debe_estacionar_pendientes_remotos(
                    estado_sesion=st["estado"], captura_cerrada=captura_cerrada,
                    sin_procesar=len(sin_procesar), listos=len(pendientes)):
                # No mantener un proceso en bucle tras Fin cuando toda la cola
                # está en cooldown o conserva un job remoto recuperable.
                _publicar_lifecycle(
                    d, "pending_remote_stt", phase="remote_retry_pending",
                    pending_count=len(sin_procesar), device="remote",
                    backend="remote-chain", gpu_failure_attributed=False)
                est.actualizar_estado(
                    d, stt_readiness="pending_remote_stt",
                    stt_backlog_wav=len(sin_procesar))
                print("[transcriptor] Fin con STT remoto pendiente; WAV conservados para reintento.")
                return 0
            if debe_cerrar_transcriptor(
                    estado_sesion=st["estado"],
                    sin_procesar=len(sin_procesar),
                    captura_cerrada=captura_cerrada,
                    latido_perdido=latido_perdido):
                _publicar_lifecycle(
                    d, "stopped_no_pending", pending_count=0,
                    last_commit=st.get("stt_ultimo_chunk", ""))
                print("[transcriptor] fin de clase (o panel cerrado) y sin pendientes. Cerrando.")
                return 0
            if st["estado"] == "fin" and not captura_cerrada:
                _publicar_lifecycle(
                    d, "waiting_capture_close", pending_count=0,
                    last_commit=st.get("stt_ultimo_chunk", ""),
                    device=info.get("device", ""),
                )
                time.sleep(1)
                continue
            _publicar_lifecycle(
                d, "waiting_audio", pending_count=len(sin_procesar),
                last_commit=st.get("stt_ultimo_chunk", ""),
                device=info.get("device", ""),
            )
            time.sleep(2)
            continue
        _publicar_lifecycle(
            d, "processing", pending_count=len(pendientes),
            last_commit=st.get("stt_ultimo_chunk", ""),
            device=info.get("device", ""),
            phase="transcribe",
            gpu_failure_attributed=info.get("device") == "cuda",
        )
        posiciones_wav = {path.name: index for index, path in enumerate(todos_wav)}
        for idx, ruta in enumerate(pendientes):
            input_hash = ""
            try:
                row_manifest = metadata_por_path.get(ruta.name, {})
                input_hash = snapshot_audio_input(d, ruta, row_manifest)
                posicion = posiciones_wav[ruta.name]
                anterior = todos_wav[posicion - 1] if posicion > 0 else None
                if anterior is not None and anterior.stem not in procesados:
                    anterior = None
                row_anterior = (
                    metadata_por_path.get(anterior.name, {}) if anterior is not None else {})
                permitir_solape = bool(
                    anterior is not None
                    and chunks_contiguos(row_anterior, row_manifest)
                )
                def record_provider_attempt(**event):
                    status = str(event.pop("status", event.get("state", "started")))
                    audio_manifest.record_transcription_attempt(
                        d, ruta.stem, status, path=ruta.name, **event)

                modelo.set_attempt_context(
                    chunk_id=ruta.stem,
                    audio_sha256=input_hash,
                    sequence=row_manifest.get("sequence"),
                    history=cola_stt.attempt_history(),
                    recorder=record_provider_attempt,
                )
                t0 = time.time()
                lineas_previas = leer_contexto_frontera(
                    trans_path, modelo)
                lineas = transcribir_chunk(
                    modelo,
                    ruta,
                    contexto=contexto,
                    inicio_abs=(float(
                        row_manifest.get("pcm_wall_start", row_manifest.get("wall_start")))
                        if row_manifest.get("pcm_wall_start") is not None
                        or row_manifest.get("wall_start") is not None else None),
                    anterior=anterior,
                    lineas_anteriores=lineas_previas,
                    permitir_solape_anterior=permitir_solape,
                )
                transcribe_sec = time.time() - t0
                row_manifest = validar_audio_input(
                    d, ruta, input_hash, row_manifest)
                audio_reloj = duracion_wav_seg(ruta)
                backlog = max(0, len(pendientes) - idx - 1)
                commit_metrics = metricas_commit_stt(
                    row_manifest,
                    committed_at=time.time(),
                    transcribe_seconds=transcribe_sec,
                    audio_seconds=audio_reloj,
                    backlog=backlog,
                )
                winner_provider = str(
                    getattr(modelo, "last_provider", "") or "remote-chain")
                winner_model = str(
                    getattr(modelo, "last_model", "") or "remote-failover")
                winner_request = str(
                    getattr(modelo, "last_request_id", "") or "")
                audio_manifest.commit_transcript_chunk(
                    d,
                    ruta.stem,
                    lineas,
                    {
                        "sequence": row_manifest.get("sequence"),
                        "wall_start": row_manifest.get(
                            "pcm_wall_start", row_manifest.get("wall_start")),
                        "path": ruta.name,
                        "input_sha256": input_hash,
                        "segments": len(lineas),
                        "stt_provider": winner_provider,
                        "stt_model": winner_model,
                        "stt_backend": "remote-chain",
                        "request_id": winner_request,
                        "silence_gate": bool(winner_provider == "silence_gate"),
                        "stt_submitted_audio_seconds": round(float(
                            getattr(modelo, "last_submitted_audio_seconds", 0.0)
                            or 0.0), 3),
                        **commit_metrics,
                    },
                )
                if not audio_manifest.read_transcript_commit(d, ruta.stem):
                    raise AudioCambioDuranteStt(
                        f"commit inválido tras publicación: {ruta.name}")
                procesados.add(ruta.stem)
                cola_stt.mark_processed(ruta.stem)
                fallidos.pop(ruta.stem, None)
                audio_manifest.record_transcription_attempt(
                    d, ruta.stem, "complete",
                    path=ruta.name,
                    input_sha256=input_hash,
                    segments=len(lineas),
                    transcribe_seconds=round(transcribe_sec, 3),
                    provider=winner_provider,
                    model=winner_model,
                    request_id=winner_request,
                )
                vel_chunk = velocidad_desde_chunk(ruta)
                try:
                    wall_end = row_manifest.get(
                        "pcm_wall_end", row_manifest.get("wall_end"))
                    previo_wall = est.leer_estado(d).get(
                        "stt_transcribed_until_wall")
                    transcrito_hasta = max(
                        [float(valor) for valor in (previo_wall, wall_end)
                         if valor is not None],
                        default=0.0,
                    )
                    est.actualizar_estado(
                        d,
                        stt_backlog_wav=backlog,
                        stt_provider=winner_provider,
                        stt_model=winner_model,
                        stt_ultimo_chunk=ruta.name,
                        stt_ultimo_segmentos=len(lineas),
                        stt_ultimo_transcribe_sec=round(transcribe_sec, 2),
                        stt_ultimo_audio_reloj_sec=round(audio_reloj, 2),
                        stt_ultimo_audio_natural_sec=round(audio_reloj * vel_chunk, 2),
                        stt_transcribed_until_wall=transcrito_hasta,
                        stt_commit_latency_sec=commit_metrics[
                            "commit_latency_seconds"],
                        stt_real_time_factor=commit_metrics["real_time_factor"],
                        stt_commit_metrics=commit_metrics,
                    )
                except Exception:
                    pass
                _publicar_lifecycle(
                    d, "processing", pending_count=backlog,
                    last_commit=ruta.name, device=info.get("device", ""),
                    phase="between_chunks", gpu_failure_attributed=False,
                )
                print(f"[transcriptor] {ruta.name}: {len(lineas)} segmentos en "
                      f"{transcribe_sec:.1f}s; backlog={backlog}", flush=True)
            except Exception as e:
                # Si el commit quedo publicado pero fallo la materializacion, no
                # volver a facturar STT: reconstruir la vista en el reinicio.
                if audio_manifest.read_transcript_commit(d, ruta.stem):
                    procesados.add(ruta.stem)
                    cola_stt.mark_processed(ruta.stem)
                    audio_manifest.materialize_transcript(d)
                    audio_manifest.record_transcription_attempt(
                        d, ruta.stem, "complete",
                        path=ruta.name,
                        input_sha256=input_hash,
                        recovered_after_view_error=True,
                    )
                    fallidos.pop(ruta.stem, None)
                    continue
                fallidos[ruta.stem] = fallidos.get(ruta.stem, 0) + 1
                base_delay = float(os.environ.get("CLASE_STT_RETRY_BASE_SEG", "30"))
                max_delay = float(os.environ.get("CLASE_STT_RETRY_MAX_SEG", "300"))
                retry_delay = min(
                    max_delay,
                    base_delay * (2 ** min(fallidos[ruta.stem] - 1, 20)),
                )
                pending_status = "failed"
                if isinstance(e, stt_backend.RemoteSttError):
                    retry_delay = max(
                        retry_delay, float(e.retry_after_seconds or 0.0))
                    if isinstance(e, stt_backend.RemoteJobPending):
                        pending_status = "pending_remote_job"
                    elif isinstance(e, stt_backend.FailoverExhausted):
                        pending_status = "pending_remote_stt"
                    if not e.retryable:
                        # Una clave/autorización inválida no se arregla repitiendo
                        # de inmediato. El WAV permanece pendiente y recuperable.
                        retry_delay = max(retry_delay, max_delay)
                audio_manifest.record_transcription_attempt(
                    d, ruta.stem, pending_status,
                    attempt=fallidos[ruta.stem],
                    path=ruta.name,
                    input_sha256=input_hash or row_manifest.get("sha256"),
                    # No persistir mensajes de SDK/HTTP: pueden incluir URLs o
                    # cabeceras. Clase y código normalizados bastan para operar.
                    error_type=type(e).__name__,
                    error_class=(
                        e.error_class
                        if isinstance(e, stt_backend.RemoteSttError)
                        else "unexpected_runtime_error"),
                    retryable=(
                        e.retryable
                        if isinstance(e, stt_backend.RemoteSttError) else True),
                    retry_after=time.time() + retry_delay,
                    provider=(
                        e.provider if isinstance(e, stt_backend.RemoteSttError)
                        else "remote-chain"),
                    model=(
                        e.model if isinstance(e, stt_backend.RemoteSttError)
                        else "remote-failover"),
                    request_id=(
                        e.request_id if isinstance(e, stt_backend.RemoteSttError)
                        else ""),
                    status_code=(
                        e.status_code
                        if isinstance(e, stt_backend.RemoteSttError) else None),
                )
                config.log_error("transcriptor", f"{ruta.name}: {type(e).__name__} "
                                                 f"(intento {fallidos[ruta.stem]}; "
                                                 f"retry en {retry_delay:.0f}s)")
                kind = (
                    "fallo remoto no transitorio"
                    if isinstance(e, stt_backend.RemoteSttError)
                    and not e.retryable else "fallo reintentable")
                print(f"[transcriptor] {ruta.name}: {kind}; "
                      f"nuevo intento en {retry_delay:.0f}s", flush=True)


def main() -> int:
    try:
        return _main()
    except Exception as error:
        try:
            d = est.clase_activa()
            if d is not None:
                _publicar_lifecycle(
                    d, "python_exception", error_type=type(error).__name__)
        except Exception:
            pass
        raise
    finally:
        config.liberar_instancia("transcriptor")


if __name__ == "__main__":
    sys.exit(main())
