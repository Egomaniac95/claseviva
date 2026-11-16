"""Convierte a WebP 85 las carpetas img y actualiza Markdown y HTML ligeros.

Vista previa por defecto. Ejemplo:
python -m scripts.optimizar_imagenes --raiz contenido --aplicar --reporte history/imagenes.json
Se admiten varias --raiz, incluidas bibliotecas fuera del proyecto.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from src import image_optimization as images
from src import study_package


def image_directories(roots: list[Path]) -> list[Path]:
    found = set()
    for raw in roots:
        root = raw.resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"La raíz no es una carpeta: {root}")
        for current, directories, _ in os.walk(root, followlinks=False):
            path = Path(current)
            for name in list(directories):
                child = path / name
                if child.is_symlink() or child.is_junction():
                    directories.remove(name)
            if path.name.casefold() == "img":
                found.add(path)
                directories.clear()
    # Si ambas raíces contienen una clase, solo se procesa una vez.
    return sorted(found, key=lambda path: str(path).casefold())


def _files(root: Path) -> list[Path]:
    paths = []
    for path in root.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise RuntimeError(f"No se optimizan carpetas con enlaces: {path}")
        if path.is_file() and path.suffix.casefold() in images.IMAGE_SUFFIXES:
            paths.append(path)
    return sorted(paths)


def _preflight(class_dir: Path) -> None:
    final = class_dir / "leccion_final.md"
    if final.is_file():
        validation = study_package.validate_candidate(class_dir)
        if not validation["ok"]:
            raise RuntimeError(f"La lección debe validar antes de optimizar: {class_dir}; "
                               f"{validation['missing_images']} {validation['hash_errors']}")
    state = class_dir / "session_state.json"
    if state.is_file():
        status = json.loads(state.read_text(encoding="utf-8-sig")).get("estado")
        if status != "fin":
            raise RuntimeError(f"Finaliza la captura antes de optimizar: {class_dir}")


def lighten_existing_html(html: Path, inventory: list[dict]) -> bool:
    """Recupera enlaces a img/ incluso si solo se conservó el HTML de la clase."""
    names = {}
    for record in inventory:
        for key in ("before_sha256", "after_sha256"):
            if record.get(key):
                names.setdefault(record[key], record["name"])
    original = html.read_bytes().decode("utf-8")

    def replace(match):
        data = base64.b64decode(re.sub(r"\s+", "", match.group(3)), validate=True)
        name = names.get(hashlib.sha256(data).hexdigest())
        if name is None:
            return match.group(0)
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not (html.parent / "img" / path).is_file():
            raise RuntimeError("Imagen HTML fuera de la carpeta img")
        return match.group(1) + match.group(2) + "img/" + quote(name, safe="/") + match.group(2)

    page = re.sub(r'''(\b(?:src|href)\s*=\s*)(["'])data:image/[\w.+-]+;base64,([A-Za-z0-9+/=\s]+)\2''',
                  replace, original)
    if page == original:
        return False
    page = page.replace("Documento autocontenido · lectura sin conexión",
                        "Lectura sin conexión · conserva este HTML junto a la carpeta img")
    images.write_bytes_atomic(html, page.encode("utf-8"))
    return True


def replace_references(text: str, mapping: dict[str, str]) -> str:
    """Cambia únicamente referencias de recursos, sin reescribir la explicación."""
    pattern = r"(?P<prefix>(?:\./)?img/|\.\./|\[\[IMG:)(?P<name>[\w./-]+)"
    return re.sub(pattern, lambda m: m['prefix'] + mapping.get(m['name'], m['name']), text)


def optimize_directory(image_dir: Path, *, workers: int = 4) -> dict:
    """Transacción por clase: restaura imágenes, Markdown y HTML ante un fallo."""
    from src.lesson_export import export_html

    image_dir = image_dir.resolve(strict=True)
    class_dir = image_dir.parent
    _preflight(class_dir)
    files = _files(image_dir)
    # No sobrescribir un WebP existente ni mezclar dos originales de igual stem.
    destinations = set()
    for source in files:
        if source.suffix.lower() == ".webp":
            continue
        target = source.with_suffix(".webp")
        if target in destinations or target.exists():
            raise RuntimeError(f"Colisión de nombres al convertir a WebP: {target}")
        destinations.add(target)
    final = class_dir / "leccion_final.md"
    html = class_dir / "leccion_final.html"
    report = {"class": str(class_dir), "images": [], "html_created": False,
              "html_before_bytes": html.stat().st_size if html.is_file() else 0}
    # El temporal es hermano de la clase, nunca una ruta calculada fuera de su raíz.
    with tempfile.TemporaryDirectory(prefix=".image-optimization-", dir=class_dir.parent) as raw:
        stage = Path(raw).resolve()
        if not stage.is_relative_to(class_dir.parent.resolve()):
            raise RuntimeError("Respaldo temporal fuera de la biblioteca")
        backups: dict[Path, Path | None] = {}
        mapping: dict[str, str] = {}
        originals: list[Path] = []

        def backup(path: Path) -> None:
            if path in backups:
                return
            if path.exists():
                target = stage / path.relative_to(class_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                backups[path] = target
            else:
                backups[path] = None

        try:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
                # Acota la cantidad de resultados y píxeles simultáneos en memoria.
                for start in range(0, len(files), 16):
                    for path, result, record in pool.map(images.prepare_image, files[start:start + 16]):
                        old_name = path.relative_to(image_dir).as_posix()
                        record["source_name"] = old_name
                        record["name"] = old_name
                        report["images"].append(record)
                        if not record["optimized"]:
                            continue
                        if study_package.sha256_file(path) != record["before_sha256"]:
                            raise RuntimeError(f"La imagen cambió durante la optimización: {path}")
                        backup(path)
                        target = path.with_suffix(".webp")
                        backup(target)
                        images.write_bytes_atomic(target, result)
                        new_name = target.relative_to(image_dir).as_posix()
                        record["name"] = new_name
                        mapping[old_name] = new_name
                        originals.append(path)
            for document in class_dir.rglob("*.md"):
                text = document.read_bytes().decode("utf-8")
                rewritten = replace_references(text, mapping)
                if rewritten != text:
                    backup(document)
                    images.write_bytes_atomic(document, rewritten.encode("utf-8"))
            if final.is_file():
                backup(final)
                text = final.read_bytes().decode("utf-8")
                metadata = study_package.parse_metadata(text)
                inventory = study_package.image_inventory(class_dir, text)
                if metadata.get("images") != inventory:
                    metadata["images"] = inventory
                    # Reemplaza solo el comentario; conserva el texto de estudio exacto.
                    text = study_package._META_RE.sub(
                        lambda _: study_package.metadata_comment(metadata), text, count=1)
                    images.write_bytes_atomic(final, text.encode("utf-8"))
                if not study_package.validate_candidate(class_dir)["ok"]:
                    raise RuntimeError("El inventario no validó después de comprimir")
                backup(html)
                export_html(class_dir)
                report["html_created"] = backups[html] is None
            elif html.is_file():
                backup(html)
                lighten_existing_html(html, report["images"])
                text = html.read_bytes().decode("utf-8")
                images.write_bytes_atomic(html, replace_references(text, mapping).encode("utf-8"))
            for record in report["images"]:
                path = image_dir / record["name"]
                expected = record.get("after_sha256", record["before_sha256"])
                if study_package.sha256_file(path) != expected:
                    raise RuntimeError(f"La imagen escrita no coincide: {path}")
            # Retira originales solo cuando los recursos nuevos y las referencias validan.
            for original in originals:
                original.unlink()
            if final.is_file() and not study_package.validate_candidate(class_dir)["ok"]:
                raise RuntimeError("La lección no validó después de retirar los JPEG")
        except BaseException:
            for path, source in reversed(list(backups.items())):
                if source is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(source, path)
            raise
    report["html_after_bytes"] = html.stat().st_size if html.is_file() else 0
    report["image_before_bytes"] = sum(r["before_bytes"] for r in report["images"])
    report["image_after_bytes"] = sum(r.get("after_bytes", r["before_bytes"]) for r in report["images"])
    report["images_optimized"] = sum(r["optimized"] for r in report["images"])
    report["images_verified"] = sum(r.get("dimensions_verified", False) for r in report["images"])
    report["quality"] = images.WEBP_QUALITY
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raiz", action="append", required=True, type=Path)
    parser.add_argument("--aplicar", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--reporte", type=Path)
    args = parser.parse_args()
    directories = image_directories(args.raiz)
    for directory in directories:
        _preflight(directory.parent)
        _files(directory)
    report = {"applied": args.aplicar, "complete": False,
              "roots": [str(p.resolve()) for p in args.raiz],
              "classes": []}
    for directory in directories:
        if args.aplicar:
            item = optimize_directory(directory, workers=args.workers)
        else:
            files = _files(directory)
            item = {"class": str(directory.parent), "image_count": len(files),
                    "image_before_bytes": sum(p.stat().st_size for p in files)}
        report["classes"].append(item)
        if args.reporte:
            args.reporte.parent.mkdir(parents=True, exist_ok=True)
            study_package.write_text_atomic(args.reporte, json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in item.items() if k != "images"}, ensure_ascii=True), flush=True)
    report["complete"] = True
    if args.reporte:
        study_package.write_text_atomic(args.reporte, json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
