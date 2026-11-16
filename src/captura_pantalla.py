"""Captura la pantalla cuando CAMBIA (nueva diapositiva, código, pizarra) usando dhash.

- LIVIANO: guarda WebP calidad 85, con el ancho máximo configurado.
- MULTI-MONITOR: captura el monitor indicado en el estado de la sesión (comando /monitor
  del copiloto o elegido al iniciar), con fallback a CLASE_MONITOR del .env.
  CLASE_REGION (recorte fijo) tiene prioridad sobre todo.
- Respeta pausa manual Y auto-pausa por silencio (break: no almacena nada).
- Test aislado:  python -m src.captura_pantalla --test
"""
import sys
import time
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

import mss
from PIL import Image

from . import config, imagen, ocr, visual_idle
from . import estado as est
from .image_optimization import encode_webp
from .visual_runtime import (
    VisualGate,
    VisualRuntimeMetrics,
    build_visual_probe,
)

AUDIO_SIGNAL_MAX_AGE_SEG = max(
    10.0,
    float(config.AUDIO_CHUNK_SEG) * 2.0 + float(config.CAPTURA_INTERVALO) * 2.0,
)


class VisualOcrCache:
    """Cache acotada por huella visual; evita OCR repetido en cada refresco."""

    def __init__(self, max_entries: int = 64):
        self.max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[tuple, str] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.captures_avoided = 0
        self.last_hit = False

    def obtener(self, img: Image.Image, coarse_hash: int,
                detail_hash: int) -> str:
        key = (img.width, img.height, int(coarse_hash), int(detail_hash))
        if key in self._entries:
            self.hits += 1
            self.last_hit = True
            texto = self._entries.pop(key)
            self._entries[key] = texto
            return texto
        self.misses += 1
        self.last_hit = False
        texto = ocr.texto_de_pil(img)[:max(1, config.CAPTURA_OCR_MAX_CHARS)]
        self._entries[key] = texto
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return texto

    def registrar_evitada(self) -> None:
        self.captures_avoided += 1

    def metricas(self) -> dict:
        return {
            "ocr_cache_entries": len(self._entries),
            "ocr_cache_hits": self.hits,
            "ocr_cache_misses": self.misses,
            "captures_avoided": self.captures_avoided,
        }


def monitores_disponibles() -> list[dict]:
    with mss.MSS() as sct:
        return sct.monitors[1:]  # [0] es el virtual combinado


def monitores_info() -> list[dict]:
    """[{'idx':1,'etiqueta':'Pantalla 1 · 3440x1440 · principal'}, ...] con nombre amigable
    (principal/secundaria) para identificar laptop vs externo sin adivinar por número."""
    mons = monitores_disponibles()
    detalles = {}
    try:
        from screeninfo import get_monitors
        for m in get_monitors():
            detalles[(m.x, m.y, m.width, m.height)] = m
    except Exception:
        pass
    salida = []
    for i, m in enumerate(mons, 1):
        det = detalles.get((m["left"], m["top"], m["width"], m["height"]))
        rol = "principal" if (det.is_primary if det else (m["left"] == 0 and m["top"] == 0)) \
            else "secundaria"
        nombre = (det.name.replace("\\\\.\\", "") + " · ") if det and det.name else ""
        salida.append({"idx": i, "etiqueta": f"Pantalla {i} · {nombre}"
                                             f"{m['width']}x{m['height']} · {rol}"})
    return salida


def _etapa(metricas, nombre: str):
    return metricas.stage(nombre) if metricas is not None else nullcontext()


def capturar_ahora(sct=None, monitor: int | None = None,
                   region: list | None = None, *, metricas=None) -> Image.Image:
    """PIL.Image de la región/monitor configurado. Importable por el copiloto.
    Prioridad: `region` de la CLASE (selector ✂️ del panel, coords absolutas)
    > CLASE_REGION del .env > monitor completo."""
    propio = sct is None
    if propio:
        sct = mss.mss()
    try:
        zona = None
        if region and len(region) == 4 and region[2] > 20 and region[3] > 20:
            zona = {"left": int(region[0]), "top": int(region[1]),
                    "width": int(region[2]), "height": int(region[3])}
        if zona is None:
            zona = config.region_captura()
        if zona is None:
            idx = monitor or config.MONITOR
            idx = idx if 0 < idx < len(sct.monitors) else 1
            zona = sct.monitors[idx]
        with _etapa(metricas, "acquisition"):
            shot = sct.grab(zona)
        with _etapa(metricas, "copy_conversion"):
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    finally:
        if propio:
            sct.close()


def preparar_evidencia(img: Image.Image) -> Image.Image:
    """Normaliza una sola vez la imagen que compartirán OCR y WebP."""
    if img.width > config.CAPTURA_ANCHO_MAX:
        alto = round(img.height * config.CAPTURA_ANCHO_MAX / img.width)
        return img.resize((config.CAPTURA_ANCHO_MAX, alto), Image.LANCZOS)
    return img


def codificar_jpeg(img: Image.Image) -> bytes:
    salida = BytesIO()
    img.save(salida, format="JPEG", quality=config.CAPTURA_CALIDAD,
             optimize=True, progressive=True)
    return salida.getvalue()


def codificar_webp(img: Image.Image) -> bytes:
    return encode_webp(img, quality=config.CAPTURA_CALIDAD)


def persistir_jpeg_atomico(contenido: bytes, destino: Path) -> None:
    tmp = destino.with_suffix(".part")
    with tmp.open("wb") as archivo:
        archivo.write(contenido)
        archivo.flush()
    tmp.replace(destino)


def guardar_liviana(img: Image.Image, destino) -> None:
    """Guarda WebP 85; JPEG solo para consumidores legacy explícitos."""
    canonica = preparar_evidencia(img)
    path = Path(destino)
    contenido = codificar_webp(canonica) if path.suffix.lower() == ".webp" else codificar_jpeg(canonica)
    persistir_jpeg_atomico(contenido, path)


def decision_idle_con_audio(
        visual_diag: dict, st: dict, ahora: float | None = None) -> dict:
    """Decide una omisión visual sin confundir estabilidad con silencio docente."""
    if not bool(visual_diag.get("idle")):
        audio_crudo = str(st.get("audio_ultimo_estado") or "").strip().lower()
        modo_estudio = bool(
            st.get("auto_pausa") or st.get("audio_requires_attention")
            or audio_crudo in {"silencio", "captura_rota"})
        return {
            "omitir": False,
            "motivo": "silent_visual_study" if modo_estudio else "visual_activa",
            "audio_estado": audio_crudo or "desconocido",
            "silencio_chunks": max(0, int(st.get("audio_silencio_chunks") or 0)),
            "preserve_canonical": True,
            "extend_dwell": True,
            "provenance": (
                "visual_sin_narracion_confirmada"
                if modo_estudio else "visual_con_audio_no_determinado"),
        }

    if st.get("audio_requires_attention"):
        return {
            "omitir": False,
            "motivo": "audio_requires_attention_conservador",
            "audio_estado": "requiere_atencion",
            "silencio_chunks": max(0, int(st.get("audio_silencio_chunks") or 0)),
            "audio_signal_fresh": False,
            "audio_age_seconds": None,
        }

    ahora = time.time() if ahora is None else float(ahora)
    try:
        audio_ultimo_t = float(st.get("audio_ultimo_t"))
        audio_edad = max(0.0, ahora - audio_ultimo_t)
        audio_fresco = audio_edad <= AUDIO_SIGNAL_MAX_AGE_SEG
    except (TypeError, ValueError):
        audio_edad = None
        audio_fresco = False

    if st.get("auto_pausa"):
        return {
            "omitir": True,
            "motivo": "auto_pausa",
            "audio_estado": "silencio_sostenido",
            "silencio_chunks": max(0, int(st.get("audio_silencio_chunks") or 0)),
            "audio_signal_fresh": audio_fresco,
            "audio_age_seconds": audio_edad,
        }

    audio_estado = (
        str(st.get("audio_ultimo_estado") or "").strip().lower()
        if audio_fresco else "desconocido"
    )
    if audio_fresco and audio_estado not in {"activo", "silencio", "captura_rota"}:
        rms = st.get("audio_ultimo_rms")
        try:
            audio_estado = "activo" if float(rms) >= config.SILENCIO_RMS else "silencio"
        except (TypeError, ValueError):
            audio_estado = "desconocido"
    silencio_chunks = (
        max(0, int(st.get("audio_silencio_chunks") or 0)) if audio_fresco else 0)
    umbral = visual_idle.umbral_silencio(config.SILENCIO_CHUNKS, True)
    if audio_estado == "activo":
        motivo = "audio_activo"
        omitir = False
    elif audio_estado == "silencio" and silencio_chunks >= umbral:
        motivo = "silencio_sostenido"
        omitir = True
    elif audio_estado == "silencio":
        motivo = "silencio_no_confirmado_conservador"
        omitir = False
    elif audio_estado == "captura_rota":
        motivo = "captura_audio_rota_conservador"
        omitir = False
    else:
        motivo = "audio_desconocido_conservador"
        omitir = False
    return {
        "omitir": omitir,
        "motivo": motivo,
        "audio_estado": audio_estado,
        "silencio_chunks": silencio_chunks,
        "umbral_silencio_chunks": int(umbral),
        "audio_signal_fresh": audio_fresco,
        "audio_age_seconds": audio_edad,
    }


@dataclass(frozen=True)
class VisualObservationOutcome:
    saved: bool = False
    heartbeat: bool = False
    ocr_ran: bool = False
    discarded: bool = False
    reason: str = ""
    visual_diag: dict | None = None
    joint_decision: dict | None = None
    filename: str | None = None


class VisualEvidencePipeline:
    """Propietario síncrono de evidencia: un frame en vuelo y ninguna cola."""

    def __init__(
            self, img_dir: Path, img_log_path: Path, timeline_path: Path, *,
            ocr_fn: Callable[[Image.Image], str] | None = None,
            save_jpeg: bool = True, gate=None, metrics=None) -> None:
        self.img_dir = Path(img_dir)
        self.img_log_path = Path(img_log_path)
        self.timeline_path = Path(timeline_path)
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self._uses_default_ocr = ocr_fn is None
        self.ocr_fn = ocr_fn or ocr.texto_de_pil
        self.save_jpeg = bool(save_jpeg)
        self.metrics = metrics or VisualRuntimeMetrics(
            max_samples=config.CAPTURA_METRICAS_MUESTRAS)
        self.gate = gate or VisualGate(
            coarse_threshold=config.UMBRAL_DHASH,
            detail_threshold=config.UMBRAL_DHASH_DETALLE,
            pixel_delta=config.CAPTURA_PIXEL_DELTA,
            change_ratio=config.CAPTURA_CAMBIO_RATIO,
            major_ratio=config.CAPTURA_CAMBIO_MAYOR_RATIO,
            mean_delta=config.CAPTURA_CAMBIO_MEDIA,
            ocr_min_interval_s=config.CAPTURA_OCR_INTERVALO_MIN,
            ocr_refresh_s=config.CAPTURA_OCR_REFRESH_SEG,
            subtle_interval_s=config.CAPTURA_CAMBIO_SUTIL_INTERVALO,
            heartbeat_interval_s=config.CAPTURA_HEARTBEAT_SEG,
        )
        self.last_file: str | None = None
        self.last_coarse_hash: int | None = None
        self.last_detail_hash: int | None = None
        self.last_ocr = ""
        self.last_saved_at: float | None = None
        self.current_part: int | None = None
        self.visibility_closed = False
        self.closed = False
        self._sequence = 0
        self._issued_names: set[str] = set()

    def _new_filename(self, epoch: float, parte: int) -> str:
        base = f"cap_{int(epoch):012d}_p{int(parte)}"
        nombre = f"{base}.webp"
        while nombre in self._issued_names or (self.img_dir / nombre).exists():
            self._sequence += 1
            nombre = f"{base}_{self._sequence:03d}.webp"
        self._issued_names.add(nombre)
        return nombre

    def _append(self, path: Path, payload: dict) -> None:
        with self.metrics.stage("persistence"):
            est.anexar_jsonl(path, payload)

    def _commit_allowed(self, validator: Callable[[], bool] | None) -> bool:
        if validator is None:
            return True
        with self.metrics.stage("state_revalidate"):
            try:
                return bool(validator())
            except Exception as exc:
                config.log_error(
                    "pantalla", f"revalidacion_visual: {type(exc).__name__}: {exc}")
                return False

    def _close_visibility(self, *, epoch: float, parte: int, motivo: str) -> bool:
        if self.visibility_closed or self.last_file is None:
            return False
        with self.metrics.stage("persistence"):
            registrar_fin_visual(
                self.timeline_path,
                epoch=int(epoch),
                parte=int(parte),
                ultimo_hash=self.last_coarse_hash,
                ultimo_archivo=self.last_file,
                motivo=str(motivo),
            )
        self.visibility_closed = True
        self.metrics.increment("visual_intervals_closed")
        return True

    def _reset_part(self, parte: int) -> None:
        self.last_file = None
        self.last_coarse_hash = None
        self.last_detail_hash = None
        self.last_ocr = ""
        self.last_saved_at = None
        self.current_part = int(parte)
        self.visibility_closed = False
        self.gate.reset()

    def process(
            self, img: Image.Image, *, epoch: float, parte: int, state: dict,
            commit_validator: Callable[[], bool] | None = None,
            ) -> VisualObservationOutcome:
        if self.closed:
            return VisualObservationOutcome(
                discarded=True, reason="pipeline_closed")
        epoch = float(epoch)
        parte = int(parte)
        if self.current_part is not None and parte != self.current_part:
            self._close_visibility(
                epoch=epoch, parte=self.current_part, motivo="cambio_parte")
            self._reset_part(parte)
        elif self.current_part is None:
            self._reset_part(parte)

        with self.metrics.frame():
            self.metrics.increment("frames_observed")
            with self.metrics.stage("probe"):
                probe = build_visual_probe(
                    img, width=config.CAPTURA_PROBE_ANCHO)
            with self.metrics.stage("visual_idle"):
                visual_diag = (
                    visual_idle.analizar_miniatura(probe.thumbnail)
                    if config.VISUAL_IDLE_SKIP_CAPTURA
                    else {"idle": False, "razones": ["idle_skip_disabled"]}
                )
                joint = decision_idle_con_audio(visual_diag, state, ahora=epoch)
            if visual_diag.get("idle") and joint.get("omitir"):
                self.metrics.increment("frames_idle_omitted")
                self._close_visibility(
                    epoch=epoch, parte=parte,
                    motivo=f"visual_idle_{joint.get('motivo', 'omitida')}")
                self.gate.last_observed_probe = probe
                return VisualObservationOutcome(
                    reason=str(joint.get("motivo") or "visual_idle"),
                    visual_diag=visual_diag, joint_decision=joint)

            if self.visibility_closed:
                self.visibility_closed = False
                self.gate.resume()
            with self.metrics.stage("hash_compare"):
                decision = self.gate.decide(probe, now=epoch)
            if decision.candidate:
                self.metrics.increment("candidate_frames")
            else:
                self.metrics.increment("stable_frames")

            canonical: Image.Image | None = None
            if decision.candidate or decision.run_ocr:
                with self.metrics.stage("resize"):
                    canonical = preparar_evidencia(img)

            texto_ocr = ""
            ocr_ran = False
            if decision.run_ocr:
                ocr_ran = True
                self.metrics.increment("ocr_runs")
                try:
                    with self.metrics.stage("ocr"):
                        texto_ocr = str(self.ocr_fn(canonical or img) or "")[
                            :max(1, config.CAPTURA_OCR_MAX_CHARS)]
                except Exception as exc:
                    self.metrics.increment("ocr_errors")
                    config.log_error(
                        "ocr", f"pipeline_visual: {type(exc).__name__}: {exc}")
                    texto_ocr = ""
                if self._uses_default_ocr:
                    motor = ocr.estado_motor()
                    self.metrics.set_gauge(
                        "ocr_engine", str(motor.get("engine") or "unknown"))
                    self.metrics.set_gauge(
                        "ocr_provider", str(motor.get("provider") or "unknown"))
                    if motor.get("language"):
                        self.metrics.set_gauge(
                            "ocr_language", str(motor["language"]))
                    self.metrics.set_gauge(
                        "ocr_operational", bool(motor.get("operational")))

            semantic_change = bool(
                ocr_ran and self.last_ocr.strip() and texto_ocr.strip()
                and ocr.cambio_evidencia(self.last_ocr, texto_ocr))
            should_save = bool(
                self.last_file is None or decision.candidate or semantic_change)
            if not should_save:
                heartbeat = bool(decision.emit_heartbeat and self.last_file)
                if heartbeat and not self._commit_allowed(commit_validator):
                    self.metrics.increment("frames_discarded")
                    return VisualObservationOutcome(
                        ocr_ran=ocr_ran, discarded=True,
                        reason="estado_cambio_antes_heartbeat",
                        visual_diag=visual_diag, joint_decision=joint)
                if heartbeat:
                    self._append(self.timeline_path, {
                        "t": int(epoch),
                        "archivo": self.last_file,
                        "ref_archivo": self.last_file,
                        "parte": parte,
                        "dhash": f"{int(self.last_coarse_hash or 0):016x}",
                        "dhash_observado": f"{probe.coarse_hash:016x}",
                        "dhash_detalle_observado": f"{probe.detail_hash:064x}",
                        "evento": "heartbeat",
                        "motivo": decision.reason,
                        "audio_estado": str(joint.get("audio_estado") or "desconocido"),
                        "provenance": str(
                            joint.get("provenance") or "visual_con_audio_no_determinado"),
                    })
                    self.metrics.increment("heartbeats")
                self.gate.commit(
                    decision, probe, now=epoch, heartbeat=heartbeat,
                    ocr_ran=ocr_ran, semantic_confirmed=ocr_ran)
                if ocr_ran and texto_ocr.strip():
                    self.last_ocr = texto_ocr
                return VisualObservationOutcome(
                    heartbeat=heartbeat, ocr_ran=ocr_ran,
                    reason=decision.reason, visual_diag=visual_diag,
                    joint_decision=joint, filename=self.last_file)

            if canonical is None:
                with self.metrics.stage("resize"):
                    canonical = preparar_evidencia(img)
            contenido = b""
            if self.save_jpeg:
                with self.metrics.stage("compression"):
                    contenido = codificar_webp(canonical)
            # La revalidación ocurre después de OCR/compresión y justo antes del commit.
            if not self._commit_allowed(commit_validator):
                self.metrics.increment("frames_discarded")
                self.gate.commit(
                    decision, probe, now=epoch, ocr_ran=ocr_ran)
                return VisualObservationOutcome(
                    ocr_ran=ocr_ran, discarded=True,
                    reason="estado_cambio_antes_commit",
                    visual_diag=visual_diag, joint_decision=joint)

            nombre = self._new_filename(epoch, parte)
            if self.save_jpeg:
                with self.metrics.stage("persistence"):
                    persistir_jpeg_atomico(contenido, self.img_dir / nombre)
            delta = decision.delta
            payload = {
                "t": int(epoch),
                "t_preciso": epoch,
                "archivo": nombre,
                "parte": parte,
                "dhash": f"{probe.coarse_hash:016x}",
                "dhash_detalle": f"{probe.detail_hash:064x}",
                "ocr": texto_ocr,
                "ocr_estado": (
                    "live" if ocr_ran and texto_ocr.strip()
                    else "unavailable" if ocr_ran else "deferred"),
                "evento": "captura",
                "motivo": (
                    "ocr_cambio," + decision.reason
                    if semantic_change else decision.reason),
                "audio_estado": str(joint.get("audio_estado") or "desconocido"),
                "provenance": str(
                    joint.get("provenance") or "visual_con_audio_no_determinado"),
                "visual_signal": {
                    "kind": decision.kind,
                    "changed_ratio": round(delta.changed_ratio, 6) if delta else None,
                    "mean_delta": round(delta.mean_delta, 4) if delta else None,
                    "max_delta": delta.max_delta if delta else None,
                    "color_changed_ratio": (
                        round(delta.color_changed_ratio, 6) if delta else None),
                    "color_mean_delta": (
                        round(delta.color_mean_delta, 4) if delta else None),
                    "color_max_delta": delta.color_max_delta if delta else None,
                    "coarse_distance": delta.coarse_distance if delta else None,
                    "detail_distance": delta.detail_distance if delta else None,
                },
            }
            self._append(self.img_log_path, payload)
            self.last_file = nombre
            self.last_coarse_hash = probe.coarse_hash
            self.last_detail_hash = probe.detail_hash
            self.last_ocr = texto_ocr if ocr_ran else ""
            self.last_saved_at = epoch
            self.visibility_closed = False
            self.metrics.increment("frames_saved")
            self.gate.commit(
                decision, probe, now=epoch, saved=True, ocr_ran=ocr_ran)
            return VisualObservationOutcome(
                saved=True, ocr_ran=ocr_ran, reason=decision.reason,
                visual_diag=visual_diag, joint_decision=joint, filename=nombre)

    def close(self, *, epoch: float, parte: int, motivo: str) -> bool:
        if self.closed:
            return False
        self._close_visibility(
            epoch=float(epoch), parte=int(parte), motivo=str(motivo))
        self.closed = True
        self.metrics.increment("closed")
        self.metrics.close()
        return True

    def suspend(self, *, epoch: float, parte: int, motivo: str) -> bool:
        """Cierra solo el intervalo visible; permite reanudar el mismo pipeline."""
        if self.closed:
            return False
        return self._close_visibility(
            epoch=float(epoch), parte=int(parte), motivo=str(motivo))

    def snapshot(self) -> dict:
        payload = self.metrics.snapshot()
        contadores = payload.get("counters", {})
        payload.update({
            "frames_observed": int(contadores.get("frames_observed", 0)),
            "frames_saved": int(contadores.get("frames_saved", 0)),
            "frames_discarded": int(contadores.get("frames_discarded", 0)),
            "ocr_runs": int(contadores.get("ocr_runs", 0)),
            "heartbeats": int(contadores.get("heartbeats", 0)),
            "closed": bool(self.closed),
        })
        return payload


def registrar_observacion_visual(
        img: Image.Image, img_dir: Path, log_path: Path, *, epoch: int,
        parte: int, ultimo_hash: int | None, ultimo_archivo: str | None,
        ultimo_ocr: str = "", ultimo_guardado_t: int | None = None,
        umbral: int | None = None,
        timeline_path: Path | None = None,
        cache_ocr: VisualOcrCache | None = None) -> tuple[int, str, str, int, bool]:
    """Registra evidencia por dHash y OCR antes de emitir un heartbeat."""
    h = imagen.dhash(img)
    h_detalle_actual = imagen.dhash(img, tam=16)
    umbral = config.UMBRAL_DHASH if umbral is None else int(umbral)
    ocr_actual = (
        cache_ocr.obtener(img, h, h_detalle_actual)
        if cache_ocr is not None else
        ocr.texto_de_pil(img)[:max(1, config.CAPTURA_OCR_MAX_CHARS)]
    )
    distancia = imagen.distancia(h, ultimo_hash) if ultimo_hash is not None else None
    cambio_hash = ultimo_hash is None or (distancia is not None and distancia > umbral)
    h_detalle = h_detalle_actual
    distancia_detalle = None
    if (distancia is not None and distancia <= umbral and ultimo_archivo
            and (img_dir / ultimo_archivo).is_file()):
        try:
            with Image.open(img_dir / ultimo_archivo) as previa:
                distancia_detalle = imagen.distancia(
                    h_detalle, imagen.dhash(previa, tam=16))
        except Exception:
            distancia_detalle = None
    cambio_detalle = bool(
        distancia_detalle is not None
        and distancia_detalle > config.UMBRAL_DHASH_DETALLE)
    cambio_ocr = bool(
        ultimo_ocr.strip() and ocr_actual.strip()
        and ocr.cambio_evidencia(ultimo_ocr, ocr_actual))
    ocr_recuperado = bool(
        ultimo_hash is not None and not ultimo_ocr.strip() and ocr_actual.strip())
    hash_ambiguo_sin_ocr = bool(
        distancia is not None and 0 < distancia <= umbral
        and (not ultimo_ocr.strip() or not ocr_actual.strip()))
    checkpoint_sin_ocr = bool(
        ultimo_hash is not None and not ocr_actual.strip()
        and ultimo_guardado_t is not None
        and int(epoch) - int(ultimo_guardado_t)
        >= max(1.0, config.CAPTURA_CHECKPOINT_SIN_OCR_SEG))
    cambio = (
        cambio_hash or cambio_detalle or cambio_ocr or ocr_recuperado
        or hash_ambiguo_sin_ocr or checkpoint_sin_ocr
    )
    if cambio or not ultimo_archivo:
        nombre = f"cap_{int(epoch):012d}_p{int(parte)}.webp"
        guardar_liviana(img, img_dir / nombre)
        motivos = []
        if ultimo_hash is None:
            motivos.append("inicio")
        if cambio_hash and ultimo_hash is not None:
            motivos.append("dhash")
        if cambio_detalle:
            motivos.append("dhash_detalle")
        if cambio_ocr:
            motivos.append("ocr_cambio")
        if ocr_recuperado:
            motivos.append("ocr_recuperado")
        if hash_ambiguo_sin_ocr:
            motivos.append("dhash_ambiguo_sin_ocr")
        if checkpoint_sin_ocr:
            motivos.append("checkpoint_sin_ocr")
        est.anexar_jsonl(log_path, {
            "t": int(epoch),
            "archivo": nombre,
            "parte": int(parte),
            "dhash": f"{h:016x}",
            "dhash_detalle": f"{h_detalle:064x}" if h_detalle is not None else "",
            "ocr": ocr_actual,
            "evento": "captura",
            "motivo": ",".join(motivos) or "captura",
        })
        return h, nombre, ocr_actual, int(epoch), True

    timeline_path = timeline_path or log_path.with_name("visual_timeline.jsonl")
    if cache_ocr is not None:
        cache_ocr.registrar_evitada()
    est.anexar_jsonl(timeline_path, {
        "t": int(epoch),
        "archivo": ultimo_archivo,
        "ref_archivo": ultimo_archivo,
        "parte": int(parte),
        "dhash": f"{ultimo_hash:016x}",
        "dhash_observado": f"{h:016x}",
        "dhash_detalle_observado": (
            f"{h_detalle:064x}" if h_detalle is not None else ""),
        "evento": "heartbeat",
        "ocr_cache_hit": bool(cache_ocr.last_hit) if cache_ocr is not None else False,
    })
    return ultimo_hash, ultimo_archivo, ultimo_ocr, int(ultimo_guardado_t or epoch), False


def registrar_fin_visual(timeline_path: Path, *, epoch: int, parte: int,
                         ultimo_hash: int | None, ultimo_archivo: str | None,
                         motivo: str) -> None:
    """Cierra la visibilidad de la última captura sin crear otro archivo."""
    if ultimo_hash is None or not ultimo_archivo:
        return
    est.anexar_jsonl(timeline_path, {
        "t": int(epoch),
        "archivo": ultimo_archivo,
        "ref_archivo": ultimo_archivo,
        "parte": int(parte),
        "dhash": f"{ultimo_hash:016x}",
        "evento": "fin",
        "motivo": motivo,
    })


def aplicar_decision_idle(
        d: Path, timeline_path: Path, visual_diag: dict, st: dict, *,
        parte: int, ultimo_hash: int | None, ultimo_archivo: str | None,
        cerrada_por_idle: bool, epoch: int | None = None,
        ahora: float | None = None) -> tuple[dict, bool]:
    """Publica la decisión conjunta y cierra el intervalo solo si se omite."""
    decision = decision_idle_con_audio(visual_diag, st, ahora=ahora)
    if not visual_diag.get("idle"):
        return decision, False
    diagnostico = {**visual_diag, "decision_conjunta": decision}
    est.actualizar_estado(d, visual_idle=diagnostico)
    if decision["omitir"] and not cerrada_por_idle:
        registrar_fin_visual(
            timeline_path,
            epoch=int(time.time() if epoch is None else epoch),
            parte=int(parte),
            ultimo_hash=ultimo_hash,
            ultimo_archivo=ultimo_archivo,
            motivo=f"visual_idle_{decision['motivo']}",
        )
    return decision, bool(decision["omitir"])


def modo_test() -> int:
    config.ensure_dirs()
    mons = monitores_disponibles()
    print("[test] monitores: " + ", ".join(
        f"{i}={m['width']}x{m['height']}@({m['left']},{m['top']})"
        for i, m in enumerate(mons, 1)))
    img = capturar_ahora()
    destino = config.LOGS_DIR / "test_captura.webp"
    guardar_liviana(img, destino)
    kb = destino.stat().st_size // 1024
    print(f"TEST_OK archivo={destino} original={img.size[0]}x{img.size[1]} "
          f"peso={kb}KB dhash={imagen.dhash(img):016x}")
    return 0


def main() -> int:
    config.stdout_utf8()
    if "--test" in sys.argv:
        return modo_test()

    if not config.instancia_unica("captura_pantalla"):
        print("[pantalla] ya hay otro vivo (candado). Saliendo.")
        return 0
    d = est.clase_activa()
    if d is None:
        print("[pantalla] no hay clase activa. Saliendo.")
        return 1
    img_dir = d / "img"
    log_path = d / "img_log.jsonl"
    timeline_path = d / "visual_timeline.jsonl"
    pipeline = VisualEvidencePipeline(img_dir, log_path, timeline_path)
    ultima_telemetria = time.monotonic()
    ultima_firma_idle = None
    ultima_parte = 1
    print(f"[pantalla] vigilando cambios cada {config.CAPTURA_INTERVALO}s "
          f"(fast path sin cola; OCR mínimo {config.CAPTURA_OCR_INTERVALO_MIN:g}s) "
          f"-> {img_dir}")

    try:
        with mss.mss() as sct:
            while True:
                iteracion_inicio = time.monotonic()
                with pipeline.metrics.stage("state_read"):
                    st = est.leer_estado(d)
                    latido_perdido = config.latido_perdido()
                ultima_parte = int(st.get("parte", ultima_parte))
                if st["estado"] == "fin" or latido_perdido:
                    pipeline.close(
                        epoch=time.time(), parte=ultima_parte,
                        motivo="latido_perdido" if latido_perdido else "fin_clase")
                    print("[pantalla] fin de clase (o panel cerrado). Cerrando.")
                    return 0
                if not est.capturando_visual(st):
                    pipeline.metrics.increment("paused_polls")
                    pipeline.suspend(
                        epoch=time.time(), parte=ultima_parte,
                        motivo="pausa_captura")
                    time.sleep(1)
                    continue
                try:
                    img = capturar_ahora(
                        sct, monitor=st.get("monitor"), region=st.get("region"),
                        metricas=pipeline.metrics)

                    def commit_permitido() -> bool:
                        actual = est.leer_estado(d)
                        return bool(
                            actual.get("estado") != "fin"
                            and int(actual.get("parte", ultima_parte)) == ultima_parte
                            and est.capturando_visual(actual)
                        )

                    resultado = pipeline.process(
                        img, epoch=time.time(), parte=ultima_parte, state=st,
                        commit_validator=commit_permitido)
                    diagnostico = resultado.visual_diag or {}
                    firma_idle = (
                        bool(diagnostico.get("idle")),
                        tuple(diagnostico.get("razones") or ()),
                        str((resultado.joint_decision or {}).get("motivo") or ""),
                    )
                    publicar_idle = bool(diagnostico.get("idle"))
                    limpiar_idle = (
                        ultima_firma_idle is not None
                        and ultima_firma_idle[0] and not firma_idle[0])
                    if (publicar_idle or limpiar_idle) and firma_idle != ultima_firma_idle:
                        est.actualizar_estado(d, visual_idle={
                            **diagnostico,
                            "decision_conjunta": resultado.joint_decision or {},
                        })
                        ultima_firma_idle = firma_idle

                    ahora_mono = time.monotonic()
                    if ahora_mono - ultima_telemetria >= config.CAPTURA_TELEMETRIA_SEG:
                        health = pipeline.snapshot()
                        health.update({
                            "mode": resultado.reason,
                            "last_observed_at": time.time(),
                            "queue_contract": "none_one_frame_in_flight",
                        })
                        est.actualizar_estado(d, visual_capture_health=health)
                        ultima_telemetria = ahora_mono
                except Exception as e:
                    pipeline.metrics.increment("errors")
                    config.log_error("pantalla", f"{type(e).__name__}: {e}")
                    time.sleep(3)
                transcurrido = time.monotonic() - iteracion_inicio
                demora = config.CAPTURA_INTERVALO - transcurrido
                if demora < 0:
                    pipeline.metrics.record_overrun(-demora)
                time.sleep(max(0.0, demora))
    finally:
        pipeline.close(
            epoch=time.time(), parte=ultima_parte, motivo="shutdown_pipeline")
        ocr.cerrar_motor()


if __name__ == "__main__":
    sys.exit(main())
