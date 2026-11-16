"""Reintenta la limpieza de una lección publicada sin regenerar ni usar APIs.

Vista previa por defecto. Ejecutar desde la raíz: python -m
scripts.compactar_publicada --clase contenido/.../clases/CLASE_... --aplicar
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from src import config, consolidation_recovery, estado, local_interventions, post_procesar
from src import student_notes, study_package, stt_usage


def compactar(class_dir: Path, *, aplicar: bool = False,
              recuperacion_confirmada_sha256: str = "") -> dict:
    d = class_dir.resolve(strict=True)
    content = config.CONTENIDO_DIR.resolve(strict=True)
    if not d.is_relative_to(content) or d.parent.name != "clases":
        raise RuntimeError("la clase debe estar dentro de contenido/<curso>/clases/")
    if any(path.is_symlink() or path.is_junction() for path in [d, *d.rglob("*")]):
        raise RuntimeError("no se compactan clases con enlaces de archivos o directorios")
    state = estado.leer_estado(d)
    if state.get("estado") != "fin":
        raise RuntimeError("la clase debe estar finalizada")
    final = d / "leccion_final.md"
    validation = study_package.validate_candidate(d)
    if not validation["ok"] or not validation["image_hashes_verified"]:
        raise RuntimeError("la lección y su inventario de imágenes deben validar por hash")
    if not student_notes.package_readiness_report(d)["ok"] or local_interventions.pending_report(d)["blocking"]:
        raise RuntimeError("hay notas o intervenciones sin resolver")
    sources = [d / "transcript.jsonl", d / "img_log.jsonl", d / "marcas.jsonl"]
    sources += [p for root in (d / "transcript_chunks", d / "student_notes")
                if root.is_dir() for p in root.rglob("*") if p.is_file()]
    if any(path.is_file() and path.stat().st_mtime_ns > final.stat().st_mtime_ns
           for path in sources):
        raise RuntimeError("hay evidencia posterior a la publicación; consolida primero")
    text = final.read_text(encoding="utf-8")
    for note in student_notes.list_notes(d):
        if note.get("inclusion", {}).get("state") != "included":
            continue
        sha = str(note.get("original", {}).get("sha256") or "")
        if not sha or sha not in text:
            raise RuntimeError("una nota incluida no tiene su procedencia publicada")
    barrier = post_procesar.verificar_barrera_final(d)
    coverage = post_procesar.analizar_cobertura_audio(
        d, estado.leer_jsonl(d / "transcript.jsonl"),
        estado.leer_jsonl(d / "img_log.jsonl"),
        post_procesar._origen_postproceso(d, state), manifest_verificado=True)
    source_archived = validation["metadata"].get("source_transcript") is not None
    chunks_safe = post_procesar._wav_limpieza_segura_por_chunks(coverage) and (
        source_archived or coverage["continuidad_audio"].get("status") != "warning")
    safe = bool(barrier["ready"] and (coverage.get("limpieza_segura") or chunks_safe))
    waived_reasons = []
    if recuperacion_confirmada_sha256:
        # Explicit, one-publication recovery decision. Never bypass pending STT,
        # new evidence, notes or asset integrity; ordinary consolidation has no
        # automatic exception based on age or a historical class name.
        if recuperacion_confirmada_sha256 != study_package.sha256_file(final):
            raise RuntimeError("la publicación no coincide con la recuperación confirmada")
        if not barrier["ready"]:
            raise RuntimeError("la recuperación no permite omitir commits pendientes")
        waived_reasons = post_procesar._razones_compactacion_retenida(barrier, coverage, sin_audio=False)
        safe = True
    if not safe:
        raise RuntimeError("la cobertura aún impide limpiar: " + ", ".join(
            post_procesar._razones_compactacion_retenida(barrier, coverage, sin_audio=False)))
    lesson_hash = study_package.sha256_file(final)
    report = {
        "class": str(d), "applied": False, "lesson_sha256": lesson_hash,
        "wav_verified": coverage["wav_total"], "pending": coverage["wav_sin_transcript_total"],
        "invalid": coverage["stt_invalidos_total"], "images_kept": len(validation["references"]),
        "continuity": {k: v for k, v in coverage["continuidad_audio"].items()
                       if k != "boundaries"},
        "images": validation["metadata"]["images"],
        "stt_usage": stt_usage.summarize(d),
        "historical_recovery": {
            "accepted_lesson_sha256": recuperacion_confirmada_sha256 if waived_reasons else "",
            "coverage_reasons_accepted": waived_reasons,
        },
        "before_bytes": sum(p.stat().st_size for p in d.rglob("*") if p.is_file()),
    }
    if aplicar:
        receipt = config.ROOT / "history" / f"cleanup-{d.name}-{time.time_ns()}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        study_package.write_text_atomic(receipt, json.dumps(report, ensure_ascii=False, indent=2))
        result = study_package.finalize_lightweight_package(d, evidence_safe=True)
        if study_package.sha256_file(final) != lesson_hash:
            raise RuntimeError("el hash de la lección cambió durante la limpieza")
        report.update(result, applied=True, receipt=str(receipt))
        report["after_bytes"] = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
        # Exporting a self-contained HTML may add bytes even while auxiliaries
        # are removed. Report actual space saved separately from deleted bytes.
        report["net_bytes_saved"] = report["before_bytes"] - report["after_bytes"]
        study_package.write_text_atomic(receipt, json.dumps(report, ensure_ascii=False, indent=2))
        consolidation_recovery.write_status(
            d, running=False, status="complete", stage="complete", error="",
            cleanup_status="complete", cleanup_message="",
            lesson_sha256=lesson_hash,
            html_sha256=study_package.sha256_file(d / "leccion_final.html"))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clase", required=True, type=Path)
    parser.add_argument("--aplicar", action="store_true")
    parser.add_argument("--recuperacion-confirmada-sha256", default="",
                        help="aceptación explícita de cobertura histórica para este Markdown exacto")
    args = parser.parse_args()
    report = compactar(args.clase, aplicar=args.aplicar,
                       recuperacion_confirmada_sha256=args.recuperacion_confirmada_sha256)
    print(json.dumps({k: v for k, v in report.items() if k not in {"images", "continuity"}},
                     ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
