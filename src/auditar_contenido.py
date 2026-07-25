"""Auditoria segura de contenido/ y migracion legacy dry-run por defecto."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import config

LEGACY_ROOTS = ("clases", "resumenes")
DOCS_AUDIT = (
    "README.md", "PROJECT_STATE.md", "memoria_cursos.md",
)


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _str(p: Path) -> str:
    return str(p)


def _backup_fuera_de_contenido(backup_dir: Path, contenido_dir: Path) -> bool:
    try:
        backup_dir.resolve().relative_to(contenido_dir.resolve())
        return False
    except ValueError:
        return True


def plan_migracion_legacy(contenido_dir: Path | None = None,
                          backup_root: Path | None = None,
                          timestamp: str | None = None) -> dict:
    contenido_dir = Path(contenido_dir or config.CONTENIDO_DIR)
    backup_root = Path(backup_root or (config.ROOT / "backups"))
    ts = timestamp or _ts()
    backup_dir = backup_root / f"contenido_legacy_{ts}"
    movimientos = []
    for nombre in LEGACY_ROOTS:
        origen = contenido_dir / nombre
        if origen.exists():
            movimientos.append({
                "origen": _str(origen),
                "destino": _str(backup_dir / nombre),
                "tipo": "directorio" if origen.is_dir() else "archivo",
                "bytes": _bytes_path(origen),
            })
    return {
        "contenido_dir": _str(contenido_dir),
        "backup_dir": _str(backup_dir),
        "backup_fuera_de_contenido": _backup_fuera_de_contenido(backup_dir, contenido_dir),
        "movimientos": movimientos,
        "dry_run_default": True,
    }


def auditar_contenido(contenido_dir: Path | None = None,
                      backup_root: Path | None = None) -> dict:
    contenido_dir = Path(contenido_dir or config.CONTENIDO_DIR)
    cursos_validos = []
    carpetas_legacy = []
    carpetas_sistema = []
    if contenido_dir.is_dir():
        for p in sorted(contenido_dir.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_dir():
                continue
            if p.name.lower() in LEGACY_ROOTS:
                carpetas_legacy.append(p.name)
            elif config.es_dir_curso_valido(p):
                cursos_validos.append(p.name)
            else:
                carpetas_sistema.append(p.name)
    legacy_clases = contenido_dir / "clases"
    legacy_resumenes = contenido_dir / "resumenes"
    clases_fuera = sorted(p.name for p in legacy_clases.iterdir()
                          if p.is_dir()) if legacy_clases.is_dir() else []
    resumenes_fuera = sorted(p.name for p in legacy_resumenes.iterdir()
                             if p.is_file() or p.is_dir()) if legacy_resumenes.is_dir() else []
    return {
        "contenido_dir": _str(contenido_dir),
        "cursos_validos": cursos_validos,
        "carpetas_legacy": carpetas_legacy,
        "carpetas_sistema_ignoradas": carpetas_sistema,
        "clases_fuera_de_curso": clases_fuera,
        "resumenes_fuera_de_curso": resumenes_fuera,
        "propuesta_migracion": plan_migracion_legacy(contenido_dir, backup_root),
    }


def _bytes_path(p: Path) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def migrar_legacy(contenido_dir: Path | None = None,
                  backup_root: Path | None = None,
                  aplicar: bool = False,
                  timestamp: str | None = None) -> dict:
    plan = plan_migracion_legacy(contenido_dir, backup_root, timestamp)
    plan["aplicado"] = False
    if not aplicar:
        return plan
    if not plan["backup_fuera_de_contenido"]:
        raise RuntimeError("backup_dir queda dentro de contenido/; se cancela migracion")
    backup_dir = Path(plan["backup_dir"])
    backup_dir.mkdir(parents=True, exist_ok=False)
    movidos = []
    for mov in plan["movimientos"]:
        origen = Path(mov["origen"])
        destino = Path(mov["destino"])
        if not origen.exists():
            continue
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(origen), str(destino))
        movidos.append({**mov, "movido": True})
    manifest = {
        **plan,
        "aplicado": True,
        "movidos": movidos,
        "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def auditar_docs(root: Path | None = None) -> dict:
    root = Path(root or config.ROOT)
    docs = []
    for nombre in DOCS_AUDIT:
        p = root / nombre
        if not p.is_file():
            docs.append({"archivo": nombre, "existe": False})
            continue
        txt = p.read_text(encoding="utf-8")
        lineas = txt.splitlines()
        recomendacion = ""
        if len(lineas) > 300:
            recomendacion = "condensar el documento manteniendo estado o contratos vigentes"
        docs.append({
            "archivo": nombre,
            "existe": True,
            "bytes": p.stat().st_size,
            "lineas": len(lineas),
            "recomendacion": recomendacion,
        })
    return {
        "root": _str(root),
        "dry_run_default": True,
        "docs": docs,
        "aplicado": False,
    }


def _imprimir_reporte(rep: dict) -> None:
    print("== Auditoria contenido/ ==")
    print(f"contenido: {rep['contenido_dir']}")
    print(f"cursos validos: {', '.join(rep['cursos_validos']) or '(ninguno)'}")
    print(f"legacy root: {', '.join(rep['carpetas_legacy']) or '(ninguno)'}")
    print(f"sistema ignorado: {', '.join(rep['carpetas_sistema_ignoradas']) or '(ninguno)'}")
    print(f"clases fuera de curso: {len(rep['clases_fuera_de_curso'])}")
    print(f"resumenes fuera de curso: {len(rep['resumenes_fuera_de_curso'])}")
    plan = rep["propuesta_migracion"]
    print("\n== Propuesta migracion legacy (dry-run) ==")
    print(f"backup: {plan['backup_dir']}")
    print(f"backup fuera de contenido/: {plan['backup_fuera_de_contenido']}")
    if not plan["movimientos"]:
        print("sin movimientos propuestos")
    for mov in plan["movimientos"]:
        print(f"- mover {mov['origen']} -> {mov['destino']} ({mov['bytes']} bytes)")


def _imprimir_docs(rep: dict) -> None:
    print("== Auditoria docs (dry-run) ==")
    print(f"root: {rep['root']}")
    for doc in rep["docs"]:
        if not doc.get("existe"):
            print(f"- {doc['archivo']}: no existe")
            continue
        rec = doc.get("recomendacion") or "sin accion"
        print(f"- {doc['archivo']}: {doc['lineas']} lineas, {doc['bytes']} bytes -> {rec}")
    print("no se modifico ningun documento")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Audita contenido/ sin borrar nada")
    ap.add_argument("--migrar-legacy", action="store_true",
                    help="muestra o ejecuta plan para mover contenido/clases y contenido/resumenes")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="no mueve nada (default)")
    ap.add_argument("--aplicar", action="store_true",
                    help="mueve legacy a backups/contenido_legacy_<ts>/")
    ap.add_argument("--docs", action="store_true",
                    help="audita tamaño de docs/progress sin modificar")
    ap.add_argument("--json", action="store_true", help="imprime JSON")
    args = ap.parse_args(argv)

    if args.docs:
        rep = auditar_docs()
    elif args.migrar_legacy and args.aplicar:
        rep = migrar_legacy(aplicar=True)
    elif args.migrar_legacy:
        rep = migrar_legacy(aplicar=False)
    else:
        rep = auditar_contenido()

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif args.docs:
        _imprimir_docs(rep)
    elif args.migrar_legacy:
        print("== Migracion legacy contenido/ ==")
        print(f"modo: {'APLICAR' if args.aplicar else 'DRY-RUN'}")
        print(f"backup: {rep['backup_dir']}")
        if not rep["movimientos"]:
            print("sin movimientos propuestos")
        for mov in rep["movimientos"]:
            print(f"- mover {mov['origen']} -> {mov['destino']} ({mov['bytes']} bytes)")
        if not args.aplicar:
            print("no se movio nada; usa --aplicar para ejecutar el plan")
    else:
        _imprimir_reporte(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
