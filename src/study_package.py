"""Paquete ligero y reanudable de una clase cerrada.

La carpeta cerrada conserva únicamente ``leccion_final.md`` y ``img/``. La
metadata necesaria para continuar vive en un comentario HTML versionado dentro
del Markdown; no se necesita el audio, transcript ni los manifests históricos.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path


SCHEMA_VERSION = 1
KIND = "clase-viva-study-package"
MARKER = "CLASEVIVA_STUDY_PACKAGE_V1"
ADDITIONAL_LESSONS_DIR = Path("img") / ".additional_lessons"
_META_RE = re.compile(
    rf"<!--\s*{MARKER}\s*(?P<payload>\{{.*?\}})\s*-->",
    re.DOTALL,
)
_LOCAL_IMG_RE = re.compile(
    r"""(?:src\s*=\s*["'](?:\./)?img/(?P<html>[\w.\-/]+)"""
    r"""|!\[[^\]]*\]\((?:\./)?img/(?P<markdown>[\w.\-/]+)"""
    r"""|\[\[IMG:(?P<marker>[\w.\-/]+))""",
    re.IGNORECASE,
)


def format_hms(seconds: float | int) -> str:
    total = max(0, int(round(float(seconds or 0))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def strip_metadata(markdown: str) -> str:
    return _META_RE.sub("", str(markdown or ""), count=1).lstrip()


def parse_metadata(markdown: str) -> dict | None:
    match = _META_RE.search(str(markdown or ""))
    if not match:
        return None
    try:
        payload = json.loads(match.group("payload"))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != KIND:
        return None
    try:
        schema = int(payload.get("schema_version", 0))
    except (TypeError, ValueError):
        return None
    if schema != SCHEMA_VERSION:
        return None
    parts = payload.get("recording_parts")
    if not isinstance(parts, list) or not parts:
        return None
    return payload


def read_metadata(class_dir: Path | str) -> dict | None:
    final = Path(class_dir) / "leccion_final.md"
    if not final.is_file():
        return None
    return parse_metadata(final.read_text(encoding="utf-8"))


def metadata_comment(payload: dict) -> str:
    normalized = dict(payload)
    normalized["kind"] = KIND
    normalized["schema_version"] = SCHEMA_VERSION
    raw = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"<!-- {MARKER}\n{raw}\n-->"


def recording_map_markdown(parts: list[dict]) -> str:
    valid = []
    for raw in parts:
        try:
            number = int(raw.get("part", 0))
            start = float(raw.get("useful_start_sec", 0.0))
            end = float(raw.get("useful_end_sec", start))
        except (AttributeError, TypeError, ValueError):
            continue
        if number > 0 and end >= start:
            valid.append((number, start, end))
    valid.sort(key=lambda item: item[0])
    if not valid:
        return ""
    lines = [
        "## ⏱️ Bloques de grabación",
        "",
        (
            f"> Esta lección integra {len(valid)} "
            f"{'bloque' if len(valid) == 1 else 'bloques'} de grabación. "
            "Los límites se muestran para ubicar la procedencia temporal; "
            "el contenido de estudio está organizado por temas."
        ),
        "",
        "| Bloque | Tiempo útil cubierto |",
        "|---|---:|",
    ]
    lines.extend(
        f"| Parte {number} | {format_hms(start)} → {format_hms(end)} |"
        for number, start, end in valid
    )
    return "\n".join(lines)


def local_image_references(markdown: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for match in _LOCAL_IMG_RE.finditer(str(markdown or "")):
        raw = next(
            value for value in match.group("html", "markdown", "marker")
            if value is not None
        )
        raw = raw.replace("\\", "/").split("?", 1)[0]
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            continue
        normalized = path.as_posix().lstrip("/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            refs.append(normalized)
    return refs


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_inventory(class_dir: Path | str, markdown: str) -> list[dict]:
    root = Path(class_dir) / "img"
    inventory = []
    for relative in local_image_references(markdown):
        path = root / Path(relative)
        if not path.is_file():
            continue
        inventory.append({
            "name": relative,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return inventory


def additional_lesson_inventory(class_dir: Path | str) -> list[dict]:
    class_path = Path(class_dir)
    sources = []
    legacy = class_path / "lecciones_detectadas"
    packaged = class_path / ADDITIONAL_LESSONS_DIR
    if legacy.is_dir():
        sources.extend(path for path in legacy.glob("*.md") if path.is_file())
    if packaged.is_dir():
        sources.extend(path for path in packaged.glob("*.md") if path.is_file())
    records = []
    seen: set[str] = set()
    for path in sorted(sources, key=lambda item: item.name.casefold()):
        sha = sha256_file(path)
        package_name = (
            path.name if path.parent == packaged
            else f"{sha[:12]}_{path.name}"
        )
        if package_name in seen:
            continue
        seen.add(package_name)
        records.append({
            "name": path.name,
            "package_name": package_name,
            "bytes": path.stat().st_size,
            "sha256": sha,
        })
    return records


def _packaged_lesson_image_references(markdown: str) -> set[str]:
    references = set(local_image_references(markdown))
    relative_pattern = re.compile(
        r"""(?:src\s*=\s*["']|\]\()(?:\./)?\.\./"""
        r"""(?P<path>[\w.\-/]+)""",
        re.IGNORECASE,
    )
    for match in relative_pattern.finditer(str(markdown or "")):
        path = Path(match.group("path").split("?", 1)[0])
        if not path.is_absolute() and ".." not in path.parts:
            references.add(path.as_posix())
    return references


def relocate_additional_lessons(class_dir: Path | str) -> dict:
    class_path = Path(class_dir)
    legacy = class_path / "lecciones_detectadas"
    packaged = class_path / ADDITIONAL_LESSONS_DIR
    image_refs: set[str] = set()
    moved = 0
    if legacy.is_dir():
        packaged.mkdir(parents=True, exist_ok=True)
        for source in sorted(legacy.glob("*.md")):
            text = source.read_text(encoding="utf-8")
            image_refs |= set(local_image_references(text))
            sha = sha256_file(source)
            target = packaged / f"{sha[:12]}_{source.name}"
            rewritten = (
                text
                .replace('src="img/', 'src="../')
                .replace("src='img/", "src='../")
                .replace("](img/", "](../")
            )
            if target.exists():
                if sha256_file(target) != hashlib.sha256(
                        rewritten.encode("utf-8")).hexdigest():
                    raise RuntimeError(
                        f"colisión al empaquetar lección adicional: {target.name}")
                source.unlink()
            else:
                write_text_atomic(target, rewritten)
                source.unlink()
            moved += 1
        try:
            legacy.rmdir()
        except OSError:
            pass
    if packaged.is_dir():
        for lesson in packaged.glob("*.md"):
            image_refs |= _packaged_lesson_image_references(
                lesson.read_text(encoding="utf-8"))
    return {"moved": moved, "image_references": sorted(image_refs)}


def validate_candidate(class_dir: Path | str, markdown: str | None = None) -> dict:
    class_path = Path(class_dir)
    final = class_path / "leccion_final.md"
    text = (
        str(markdown)
        if markdown is not None
        else final.read_text(encoding="utf-8") if final.is_file() else ""
    )
    metadata = parse_metadata(text)
    references = local_image_references(text)
    missing = [
        relative for relative in references
        if not (class_path / "img" / Path(relative)).is_file()
    ]
    hash_errors: list[str] = []
    metadata_images = metadata.get("images") if isinstance(metadata, dict) else None
    if isinstance(metadata_images, list) and metadata_images:
        declared = {
            str(item.get("name")): item
            for item in metadata_images
            if isinstance(item, dict) and str(item.get("name", ""))
        }
        if set(declared) != set(references):
            hash_errors.append("el inventario de imágenes no coincide con las referencias")
        for relative in references:
            path = class_path / "img" / Path(relative)
            item = declared.get(relative)
            if item is None or not path.is_file():
                continue
            try:
                expected_bytes = int(item.get("bytes", -1))
            except (TypeError, ValueError):
                expected_bytes = -1
            if expected_bytes != path.stat().st_size:
                hash_errors.append(f"tamaño distinto: {relative}")
            expected_sha = str(item.get("sha256") or "")
            if not expected_sha or expected_sha != sha256_file(path):
                hash_errors.append(f"sha256 distinto: {relative}")
    return {
        "ok": bool(final.is_file() and metadata is not None
                   and not missing and not hash_errors),
        "metadata": metadata,
        "references": references,
        "missing_images": missing,
        "hash_errors": hash_errors,
        "image_hashes_verified": bool(
            isinstance(metadata_images, list) and metadata_images and not hash_errors),
        "has_final": final.is_file(),
    }


def recover_interrupted_cleanup(class_dir: Path | str) -> dict:
    """Restaura staging huérfano antes de intentar una nueva continuación."""
    class_path = Path(class_dir)
    pattern = f".{class_path.name}.compactando-*"
    stages = sorted(
        path for path in class_path.parent.glob(pattern) if path.is_dir())
    restored = 0
    for stage in stages:
        files = [
            path for path in stage.rglob("*")
            if path.is_file() and path.name != "cleanup_manifest.json"
        ]
        conflicts = []
        for source in files:
            target = class_path / source.relative_to(stage)
            if target.exists() and (
                    not target.is_file()
                    or sha256_file(target) != sha256_file(source)):
                conflicts.append(target.relative_to(class_path).as_posix())
        if conflicts:
            raise RuntimeError(
                "limpieza interrumpida con conflictos; no se sobrescribe: "
                + ", ".join(conflicts[:10]))
        for source in files:
            target = class_path / source.relative_to(stage)
            if target.exists():
                source.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            restored += 1
        shutil.rmtree(stage)
    return {"stages": len(stages), "files_restored": restored}


def _atomic_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_text_atomic(path: Path | str, text: str) -> None:
    _atomic_text(Path(path), str(text))


def migrate_legacy_lesson(class_dir: Path | str) -> dict:
    """Añade solo metadata reanudable a una lección legacy ya aprobada.

    No llama a modelos, no reescribe el cuerpo pedagógico y no toca evidencia.
    El único bloque visible nuevo es el mapa de grabaciones derivado de marcas.
    """
    class_path = Path(class_dir)
    final = class_path / "leccion_final.md"
    if not final.is_file():
        raise RuntimeError("falta leccion_final.md para migrar")
    before = final.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()
    existing = parse_metadata(before.decode("utf-8"))
    if existing is not None:
        return {
            "ok": True, "changed": False,
            "before_sha256": before_sha, "after_sha256": before_sha,
            "metadata": existing,
        }

    # Imports diferidos evitan convertir study_package en una dependencia del
    # runtime de captura durante lecturas normales.
    from . import estado as est
    from . import post_procesar

    if not (class_path / "session_state.json").is_file():
        raise RuntimeError("la lección legacy no tiene session_state para derivar bloques")
    state = est.leer_estado(class_path)
    origin = post_procesar._origen_postproceso(class_path, state)
    ranges = est.rangos_partes(class_path)
    blocks = post_procesar._bloques_grabacion(ranges, origin)
    if not blocks:
        raise RuntimeError("no se pudieron derivar bloques de grabación")

    public = before.decode("utf-8")
    recording_map = recording_map_markdown(blocks)
    if recording_map and "## ⏱️ Bloques de grabación" not in public:
        next_section = re.search(r"(?m)^##\s+", public)
        insertion = next_section.start() if next_section else len(public)
        prefix = public[:insertion]
        separator = "" if prefix.endswith(("\n\n", "\r\n\r\n")) else "\n\n"
        public = prefix + separator + recording_map + "\n\n" + public[insertion:]
    payload = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "class": {
            "class_id": str(state.get("clase_id") or class_path.name),
            "name": str(state.get("nombre") or class_path.name),
            "course": str(state.get("curso") or ""),
            "week": int(state.get("semana", 1) or 1),
            "origin": str(state.get("origen") or "vivo"),
            "started_at": float(state.get("inicio", 0) or 0),
        },
        "recording_parts": blocks,
        "useful_duration_sec": float(blocks[-1]["useful_end_sec"]),
        "package_policy": "lesson_and_referenced_assets",
        "continuation_source": "leccion_final.md",
        "images": image_inventory(class_path, public),
        "migration": {
            "kind": "metadata_only_v1",
            "source_lesson_sha256": before_sha,
        },
    }
    missing = [
        name for name in local_image_references(public)
        if not (class_path / "img" / Path(name)).is_file()
    ]
    if missing:
        raise RuntimeError(
            "la lección legacy referencia imágenes ausentes: "
            + ", ".join(missing[:10]))
    migrated = metadata_comment(payload) + "\n\n" + public.lstrip()
    _atomic_text(final, migrated)
    validation = validate_candidate(class_path)
    if not validation["ok"]:
        _atomic_text(final, before.decode("utf-8"))
        raise RuntimeError("la metadata migrada no validó; se restauró la lección")
    after_sha = sha256_file(final)
    return {
        "ok": True, "changed": True,
        "before_sha256": before_sha, "after_sha256": after_sha,
        "metadata": payload,
    }


def _relative_to(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _cleanup_candidates(class_dir: Path, references: set[str]) -> list[Path]:
    candidates = [
        path for path in class_dir.iterdir()
        if path.name not in {"leccion_final.md", "img"}
    ]
    image_root = class_dir / "img"
    if image_root.is_dir():
        for path in image_root.rglob("*"):
            if not path.is_file():
                continue
            relative = _relative_to(path, image_root)
            if relative not in references:
                candidates.append(path)
    return sorted(
        candidates,
        key=lambda path: (len(path.parts), str(path).casefold()),
        reverse=True,
    )


def _prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted(
            (item for item in root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass


def finalize_lightweight_package(
        class_dir: Path | str, *,
        evidence_safe: bool,
        blocking_reasons: list[str] | None = None,
        relocate_student_notes=None) -> dict:
    """Reduce una clase cerrada a la lección y sus recursos publicados.

    Primero mueve descartes a un staging hermano. Si una operación falla, los
    movimientos ya realizados vuelven a su ubicación original. El staging solo
    se elimina cuando el paquete final vuelve a validar.
    """
    class_path = Path(class_dir)
    if not evidence_safe:
        detail = ", ".join(str(reason) for reason in (blocking_reasons or [])
                           if str(reason).strip())
        raise RuntimeError(
            "la cobertura no permite compactar; se conserva toda la evidencia"
            + (f" ({detail})" if detail else ""))
    final = class_path / "leccion_final.md"
    if not final.is_file():
        raise RuntimeError("falta leccion_final.md; no se compacta")

    # ``relocate_student_notes`` se conserva solo en la firma para consumidores
    # internos antiguos. El contrato actual materializa texto e imágenes antes
    # de llegar aquí y elimina originales, audio y manifests con el staging.
    validation = validate_candidate(class_path)
    if not validation["ok"]:
        problems = [*validation["missing_images"], *validation["hash_errors"]]
        detail = ", ".join(problems[:10])
        raise RuntimeError(
            "paquete de estudio inválido"
            + (f"; imágenes referenciadas ausentes o alteradas: {detail}"
               if detail else ""))

    references = set(validation["references"])
    candidates = _cleanup_candidates(class_path, references)
    stage = class_path.parent / (
        f".{class_path.name}.compactando-{time.strftime('%Y%m%d_%H%M%S')}-"
        f"{uuid.uuid4().hex[:8]}")
    stage.mkdir(parents=False, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "class_dir": str(class_path),
        "started_at": time.time(),
        "moves": [],
    }
    try:
        for source in candidates:
            if not source.exists():
                continue
            relative = source.relative_to(class_path)
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved.append((source, target))
            manifest["moves"].append(relative.as_posix())
        _atomic_text(
            stage / "cleanup_manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        _prune_empty_dirs(class_path / "img")
        after = validate_candidate(class_path)
        top_level = sorted(path.name for path in class_path.iterdir())
        if not after["ok"] or top_level != ["img", "leccion_final.md"]:
            raise RuntimeError(
                "la verificación posterior no produjo solo leccion_final.md e img/")
    except Exception:
        for source, target in reversed(moved):
            if not target.exists():
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(source))
        shutil.rmtree(stage, ignore_errors=True)
        raise

    bytes_removed = sum(
        path.stat().st_size
        for path in stage.rglob("*")
        if path.is_file() and path.name != "cleanup_manifest.json"
    )
    moved_count = len(moved)
    shutil.rmtree(stage)
    return {
        "ok": True,
        "kept_top_level": ["img", "leccion_final.md"],
        "images_kept": len(references),
        "paths_removed": moved_count,
        "bytes_removed": bytes_removed,
    }
