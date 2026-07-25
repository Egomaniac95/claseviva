"""Compactacion segura de una clase.

Dry-run por defecto. En modo aplicar solo mueve descartes a _compact_backup_<ts>/;
no borra archivos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

from . import estado as est
from .deepseek_client import costo_acumulado
from .post_procesar import (
    analizar_cobertura_audio,
    analizar_cobertura_temporal,
    extraer_referencias_img,
)

COMPACT_VERSION = "compactar_clase_v1"


def _rel(d: Path, p: Path) -> str:
    try:
        return str(p.relative_to(d)).replace("\\", "/")
    except ValueError:
        return str(p)


def _bytes_path(p: Path) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def _docs_estudio(d: Path, conservar_partes: bool = True) -> list[Path]:
    docs = [d / "leccion_final.md", d / "leccion_final_eng.md", d / "qa.md"]
    if conservar_partes:
        docs.extend(sorted(d.glob("leccion_parte*.md")))
    return [p for p in docs if p.is_file()]


def imagenes_referenciadas_finales(d: Path, conservar_partes: bool = True) -> set[str]:
    refs: set[str] = set()
    for md in _docs_estudio(d, conservar_partes):
        refs.update(extraer_referencias_img(md.read_text(encoding="utf-8")))
    return refs


def _lineas_jsonl(p: Path) -> int:
    if not p.is_file():
        return 0
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


def _estado_origen(d: Path) -> tuple[dict, dict]:
    st = est.leer_estado(d) if (d / "session_state.json").is_file() else {}
    origen = {"origen": st.get("origen", "vivo"), "inicio": st.get("inicio", 0.0)}
    return st, origen


def _candidatos_mover(d: Path, refs_img: set[str],
                      conservar_partes: bool = True) -> list[Path]:
    candidatos: list[Path] = []
    for wav in sorted((d / "audio").glob("*.wav")) if (d / "audio").is_dir() else []:
        candidatos.append(wav)
    for nombre in (
            "transcript.jsonl", "img_log.jsonl", "visual_timeline.jsonl",
            "audio_manifest.jsonl", "leccion_en_vivo.md"):
        p = d / nombre
        if p.is_file():
            candidatos.append(p)
    journal = d / "transcript_chunks"
    if journal.is_dir():
        candidatos.append(journal)
    if not conservar_partes:
        candidatos.extend(sorted(d.glob("leccion_parte*.md")))
    for patron in ("ESCRIBA_ESTADO*", "*.tmp"):
        candidatos.extend(sorted(p for p in d.glob(patron) if p.is_file()))
    debug = d / "_debug"
    if debug.is_dir():
        candidatos.append(debug)
    img_dir = d / "img"
    if img_dir.is_dir():
        for img in sorted(p for p in img_dir.iterdir() if p.is_file()):
            if img.name not in refs_img:
                candidatos.append(img)
    vistos: set[str] = set()
    out: list[Path] = []
    for p in candidatos:
        r = _rel(d, p)
        if r not in vistos and p.exists():
            vistos.add(r)
            out.append(p)
    return out


def plan_compactacion(d: Path, conservar_partes: bool = True,
                      forzar: bool = False, timestamp: str | None = None) -> dict:
    d = Path(d)
    ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
    final = d / "leccion_final.md"
    if not final.is_file():
        return {
            "ok": False,
            "clase": str(d),
            "motivo": "falta leccion_final.md; no se puede compactar",
            "dry_run_default": True,
        }

    st, origen = _estado_origen(d)
    segs = est.leer_jsonl(d / "transcript.jsonl")
    capturas = est.leer_jsonl(d / "img_log.jsonl")
    cobertura = analizar_cobertura_audio(d, segs, capturas, origen) if segs or (d / "audio").is_dir() else {}
    temporal = analizar_cobertura_temporal(d, segs, origen) if segs or (d / "audio").is_dir() else {}
    final_txt = final.read_text(encoding="utf-8")
    refs_img = imagenes_referenciadas_finales(d, conservar_partes)
    img_dir = d / "img"
    faltantes = sorted(ref for ref in refs_img if not (img_dir / ref).is_file())
    candidatos = _candidatos_mover(d, refs_img, conservar_partes)
    riesgos = []
    if "Reporte de cobertura temporal" not in final_txt:
        riesgos.append("leccion_final.md no contiene Reporte de cobertura temporal")
    if faltantes:
        riesgos.append(f"imagenes referenciadas faltantes: {', '.join(faltantes[:10])}")
    if cobertura.get("wav_sin_transcript_total"):
        riesgos.append("hay WAV existentes sin transcript")
    if cobertura.get("transcript_wav_faltante_total"):
        riesgos.append("hay transcript que referencia WAV inexistentes")
    if (temporal.get("continuidad_audio") or {}).get("status") == "warning":
        riesgos.append("audio con déficit o gaps; no compactar evidencia antes de auditar")
    manifest = {
        "version": COMPACT_VERSION,
        "fecha_plan": time.strftime("%Y-%m-%d %H:%M:%S"),
        "clase": str(d),
        "clase_id": st.get("clase_id", d.name),
        "curso": st.get("curso", est.curso_de(d) if d.parent.name == "clases" else ""),
        "wav_originales": len(list((d / "audio").glob("*.wav"))) if (d / "audio").is_dir() else 0,
        "transcript_segmentos_originales": len(segs),
        "img_log_originales": len(capturas),
        "imagenes_originales": len(list(img_dir.iterdir())) if img_dir.is_dir() else 0,
        "imagenes_conservadas": sorted(refs_img),
        "imagenes_faltantes": faltantes,
        "cobertura_audio": cobertura,
        "cobertura_temporal": temporal,
        "coste_api_usd_estimado_global": round(costo_acumulado(), 6),
        "hashes": {
            p.name: {"sha256": _sha256_file(p), "bytes": p.stat().st_size}
            for p in _docs_estudio(d, conservar_partes)
        },
    }
    return {
        "ok": True,
        "clase": str(d),
        "timestamp": ts,
        "dry_run_default": True,
        "backup_dir": str(d / f"_compact_backup_{ts}"),
        "conservar": sorted(_rel(d, p) for p in _docs_estudio(d, conservar_partes))
                    + sorted(f"img/{ref}" for ref in refs_img if (img_dir / ref).is_file())
                    + [x for x in ("session_state.json", "marcas.jsonl", "clase_manifest.json")
                       if (d / x).exists()],
        "mover": [
            {"ruta": _rel(d, p), "bytes": _bytes_path(p),
             "tipo": "directorio" if p.is_dir() else "archivo"}
            for p in candidatos
        ],
        "bytes_ahorrables": sum(_bytes_path(p) for p in candidatos),
        "imagenes_referenciadas": sorted(refs_img),
        "imagenes_faltantes": faltantes,
        "riesgos": riesgos,
        "puede_aplicar": forzar or not riesgos,
        "manifest": manifest,
    }


def aplicar_compactacion(plan: dict, backup: bool = True) -> dict:
    if not plan.get("ok"):
        raise RuntimeError(plan.get("motivo", "plan invalido"))
    if not backup:
        raise RuntimeError("solo se permite compactar moviendo a backup; usa --backup")
    if not plan.get("puede_aplicar"):
        raise RuntimeError("hay riesgos en el plan; revisa dry-run o usa --forzar")
    d = Path(plan["clase"])
    backup_dir = Path(plan["backup_dir"])
    backup_dir.mkdir(parents=True, exist_ok=False)
    movidos = []
    for item in plan["mover"]:
        origen = d / item["ruta"]
        if not origen.exists():
            continue
        destino = backup_dir / item["ruta"]
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(origen), str(destino))
        movidos.append(item["ruta"])
    manifest = {**plan["manifest"], "fecha_compactacion": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backup_dir": str(backup_dir), "movidos": movidos}
    (d / "clase_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**plan, "aplicado": True, "movidos": movidos}


def _imprimir_plan(plan: dict) -> None:
    print("== Compactacion de clase ==")
    print(f"clase: {plan.get('clase')}")
    if not plan.get("ok"):
        print(f"NO compactable: {plan.get('motivo')}")
        return
    print("modo: DRY-RUN por defecto")
    print(f"backup: {plan['backup_dir']}")
    print(f"conservar: {len(plan['conservar'])} rutas")
    print(f"mover: {len(plan['mover'])} rutas")
    print(f"bytes ahorrables: {plan['bytes_ahorrables']}")
    print(f"imagenes referenciadas: {len(plan['imagenes_referenciadas'])}")
    if plan["imagenes_faltantes"]:
        print("imagenes faltantes: " + ", ".join(plan["imagenes_faltantes"][:10]))
    if plan["riesgos"]:
        print("riesgos:")
        for r in plan["riesgos"]:
            print(f"- {r}")
    else:
        print("riesgos: ninguno")
    for item in plan["mover"][:30]:
        print(f"- mover {item['ruta']} ({item['bytes']} bytes)")
    if len(plan["mover"]) > 30:
        print(f"... {len(plan['mover']) - 30} rutas mas")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compacta una clase con dry-run seguro")
    ap.add_argument("--clase", required=True, help="ruta de la carpeta de clase")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="no mueve nada (default)")
    ap.add_argument("--aplicar", action="store_true",
                    help="ejecuta compactacion; requiere --backup")
    ap.add_argument("--backup", action="store_true",
                    help="mueve descartes a _compact_backup_<ts>/")
    ap.add_argument("--forzar", action="store_true",
                    help="permite aplicar aunque haya riesgos reportados")
    ap.add_argument("--sin-partes", action="store_true",
                    help="no conservar leccion_parte*.md")
    ap.add_argument("--json", action="store_true", help="imprime JSON")
    args = ap.parse_args(argv)

    plan = plan_compactacion(Path(args.clase), conservar_partes=not args.sin_partes,
                             forzar=args.forzar)
    if args.aplicar:
        if not args.backup:
            print("Para aplicar se requiere --backup. No se borra nada.", file=sys.stderr)
            return 2
        plan = aplicar_compactacion(plan, backup=True)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        _imprimir_plan(plan)
        if not args.aplicar:
            print("no se movio nada; usa --aplicar --backup para ejecutar el plan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
