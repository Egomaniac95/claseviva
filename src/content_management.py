"""Administración reversible de cursos y clases desde el panel local.

Las rutas se resuelven exclusivamente como hijos directos de ``contenido``. Una
eliminación de clase se empaqueta antes de enviarse a la Papelera para conservar juntos
la clase, su resumen y las versiones previas de la memoria del curso.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from typing import Callable


STAGE_PREFIX = ".claseviva-delete-"
MANIFEST_NAME = "deletion_manifest.json"


class ContentSelectionError(ValueError):
    """La selección no identifica un curso o una clase confinados."""


class StaleDeletionPreview(RuntimeError):
    """La evidencia cambió entre la vista previa y la confirmación."""


@dataclass(frozen=True)
class DeletionPlan:
    content_root: str
    kind: str
    course: str
    class_id: str
    target: str
    confirmation: str
    file_count: int
    directory_count: int
    byte_count: int
    class_count: int
    sidecars: tuple[str, ...]
    documents_updated: tuple[str, ...]
    fingerprint: str

    def public(self) -> dict:
        data = asdict(self)
        data["sidecars"] = list(self.sidecars)
        data["documents_updated"] = list(self.documents_updated)
        return data


def _component(raw: str, label: str) -> str:
    value = str(raw or "").strip()
    if (not value or value in {".", ".."} or Path(value).name != value
            or "/" in value or "\\" in value or "\x00" in value):
        raise ContentSelectionError(f"{label} inválido")
    return value


def _direct_child(parent: Path, name: str, label: str) -> Path:
    parent = parent.resolve(strict=True)
    candidate = parent / _component(name, label)
    if not candidate.is_dir() or candidate.is_symlink():
        raise ContentSelectionError(f"{label} no encontrado")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != parent:
        raise ContentSelectionError(f"{label} fuera de la ruta permitida")
    return resolved


def _inventory(paths: list[Path], root: Path) -> tuple[int, int, int, str]:
    files = 0
    directories = 0
    total_bytes = 0
    digest = hashlib.sha256()
    root = root.resolve(strict=True)
    for source in sorted(paths, key=lambda item: str(item).casefold()):
        if not source.exists():
            continue
        candidates = [source]
        if source.is_dir():
            candidates.extend(sorted(source.rglob("*"), key=lambda item: str(item).casefold()))
        for path in candidates:
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise ContentSelectionError("la selección contiene una ruta no confinada") from exc
            stat = path.lstat()
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ContentSelectionError(
                    f"la selección contiene un enlace no admitido: {relative}")
            if path.is_dir():
                directories += 1
                kind = "d"
                size = 0
            elif path.is_file():
                files += 1
                kind = "f"
                size = int(stat.st_size)
                total_bytes += size
            else:
                raise ContentSelectionError(
                    f"la selección contiene un tipo de archivo no admitido: {relative}")
            digest.update(
                f"{kind}\0{relative}\0{size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
    return files, directories, total_bytes, digest.hexdigest()


def _class_count(course_dir: Path) -> int:
    classes = course_dir / "clases"
    if not classes.is_dir():
        return 0
    return sum(
        1 for path in classes.iterdir()
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
    )


def build_deletion_plan(
        content_root: Path | str,
        *,
        kind: str,
        course: str,
        class_id: str = "",
) -> DeletionPlan:
    """Crea una vista previa read-only y un fingerprint contra carreras."""
    root = Path(content_root).resolve(strict=True)
    kind = str(kind or "").strip().casefold()
    if kind not in {"course", "class"}:
        raise ContentSelectionError("tipo de eliminación inválido")
    course_dir = _direct_child(root, course, "curso")
    sidecars: list[Path] = []
    documents: list[Path] = []
    if kind == "course":
        target = course_dir
        class_name = ""
        selected = [target]
        class_count = _class_count(course_dir)
        confirmation = course_dir.name
    else:
        class_name = _component(class_id, "clase")
        classes_dir = course_dir / "clases"
        if not classes_dir.is_dir() or classes_dir.is_symlink():
            raise ContentSelectionError("carpeta de clases no encontrada")
        target = _direct_child(classes_dir, class_name, "clase")
        summary = course_dir / "resumenes" / f"resumen_{class_name}.md"
        if summary.is_file() and not summary.is_symlink():
            sidecars.append(summary.resolve(strict=True))
        for name in ("CURSO.md", "TAREAS.md"):
            document = course_dir / name
            if document.is_file() and not document.is_symlink():
                documents.append(document.resolve(strict=True))
        selected = [target, *sidecars]
        class_count = 1
        confirmation = class_name
    files, directories, byte_count, inventory_hash = _inventory(selected, root)
    fingerprint = hashlib.sha256(
        json.dumps({
            "kind": kind,
            "course": course_dir.name,
            "class_id": class_name,
            "inventory": inventory_hash,
            "documents": [
                [str(path.relative_to(root).as_posix()), path.stat().st_size,
                 path.stat().st_mtime_ns]
                for path in documents
            ],
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return DeletionPlan(
        content_root=str(root),
        kind=kind,
        course=course_dir.name,
        class_id=class_name,
        target=str(target),
        confirmation=confirmation,
        file_count=files,
        directory_count=directories,
        byte_count=byte_count,
        class_count=class_count,
        sidecars=tuple(str(path) for path in sidecars),
        documents_updated=tuple(str(path) for path in documents),
        fingerprint=fingerprint,
    )


def _strip_class_block(text: str, class_id: str) -> str:
    pattern = re.compile(
        rf"(?ms)^##[ \t]+{re.escape(class_id)}"
        r"(?:[ \t]+\([^\n]*\))?[ \t]*\n.*?(?=^##[ \t]+|\Z)",
    )
    cleaned = pattern.sub("", str(text or ""))
    return re.sub(r"\n{3,}", "\n\n", cleaned).rstrip() + "\n"


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_remove_stage(stage: Path, course_dir: Path) -> None:
    try:
        resolved = stage.resolve(strict=False)
        parent = course_dir.resolve(strict=True)
    except OSError:
        return
    if resolved.parent == parent and resolved.name.startswith(STAGE_PREFIX):
        shutil.rmtree(resolved, ignore_errors=True)


def _rollback_stage(stage: Path, manifest: dict) -> bool:
    complete = True
    for row in reversed(list(manifest.get("documents") or [])):
        backup = stage / str(row["backup"])
        destination = Path(str(row["source"]))
        if backup.is_file():
            _write_atomic(destination, backup.read_bytes())
    for row in reversed(list(manifest.get("moves") or [])):
        staged = stage / str(row["staged"])
        source = Path(str(row["source"]))
        if staged.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, source)
        elif staged.exists():
            complete = False
    return complete


def recover_interrupted_deletions(content_root: Path | str) -> list[str]:
    """Revierte bundles locales que nunca alcanzaron la Papelera."""
    root = Path(content_root)
    if not root.is_dir():
        return []
    recovered: list[str] = []
    for course_dir in root.iterdir():
        if not course_dir.is_dir() or course_dir.is_symlink():
            continue
        for stage in course_dir.glob(f"{STAGE_PREFIX}*"):
            manifest_path = stage / MANIFEST_NAME
            if not manifest_path.is_file():
                if not any(stage.iterdir()):
                    stage.rmdir()
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if _rollback_stage(stage, manifest):
                    recovered.append(str(manifest.get("class_id") or stage.name))
                    _safe_remove_stage(stage, course_dir)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                # No se destruye un bundle que no pudo recuperarse por completo.
                continue
    return recovered


def _execute_class_plan(
        plan: DeletionPlan,
        send_to_trash: Callable[[str], None],
) -> None:
    root = Path(plan.content_root)
    course_dir = root / plan.course
    stage = course_dir / (
        f"{STAGE_PREFIX}{plan.class_id}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    stage.mkdir()
    moves = [{
        "source": plan.target,
        "staged": f"clase/{plan.class_id}",
    }]
    for path in plan.sidecars:
        source = Path(path)
        moves.append({
            "source": str(source),
            "staged": f"sidecars/{source.name}",
        })
    documents = []
    for path in plan.documents_updated:
        source = Path(path)
        documents.append({
            "source": str(source),
            "backup": f"documentos_originales/{source.name}",
        })
    manifest = {
        "schema_version": 1,
        "kind": "class",
        "course": plan.course,
        "class_id": plan.class_id,
        "fingerprint": plan.fingerprint,
        "created_at": time.time(),
        "moves": moves,
        "documents": documents,
    }
    (stage / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for row in documents:
            source = Path(row["source"])
            backup = stage / row["backup"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(source.read_bytes())
        for row in moves:
            source = Path(row["source"])
            staged = stage / row["staged"]
            staged.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, staged)
        for row in documents:
            source = Path(row["source"])
            cleaned = _strip_class_block(
                source.read_text(encoding="utf-8"), plan.class_id)
            _write_atomic(source, cleaned.encode("utf-8"))
        send_to_trash(str(stage))
        if stage.exists():
            raise RuntimeError("la Papelera no confirmó el movimiento de la clase")
    except Exception:
        # Algunas APIs nativas pueden completar el movimiento y aun así perder la
        # respuesta. La ausencia del bundle prueba que ya salió del curso.
        if not stage.exists():
            return
        if _rollback_stage(stage, manifest):
            _safe_remove_stage(stage, course_dir)
        raise


def execute_deletion(
        content_root: Path | str,
        *,
        kind: str,
        course: str,
        class_id: str = "",
        confirmation: str,
        expected_fingerprint: str,
        send_to_trash: Callable[[str], None] | None = None,
) -> DeletionPlan:
    """Revalida y mueve a Papelera; nunca borra permanentemente."""
    recover_interrupted_deletions(content_root)
    plan = build_deletion_plan(
        content_root, kind=kind, course=course, class_id=class_id)
    if confirmation != plan.confirmation:
        raise ContentSelectionError(
            f"la confirmación debe ser exactamente: {plan.confirmation}")
    if not expected_fingerprint or expected_fingerprint != plan.fingerprint:
        raise StaleDeletionPreview(
            "el contenido cambió desde la vista previa; revísala y confirma otra vez")
    if send_to_trash is None:
        try:
            from send2trash import send2trash as send_to_trash
        except ImportError as exc:
            raise RuntimeError(
                "Falta Send2Trash; instala requirements.txt en .venv para usar la Papelera") from exc
    if plan.kind == "course":
        try:
            send_to_trash(plan.target)
        except Exception:
            if Path(plan.target).exists():
                raise
        if Path(plan.target).exists():
            raise RuntimeError("la Papelera no confirmó el movimiento del curso")
    else:
        _execute_class_plan(plan, send_to_trash)
    return plan


def clear_pointer_if_targeted(
        pointer: Path | str,
        project_root: Path | str,
        deleted_target: Path | str,
) -> bool:
    """Retira solo el puntero que apuntaba dentro del target ya movido."""
    pointer = Path(pointer)
    if not pointer.is_file():
        return False
    raw = pointer.read_text(encoding="utf-8-sig").strip()
    if not raw:
        return False
    current = Path(raw)
    if not current.is_absolute():
        current = Path(project_root) / current
    current = current.resolve(strict=False)
    target = Path(deleted_target).resolve(strict=False)
    try:
        current.relative_to(target)
    except ValueError:
        return False
    pointer.unlink()
    return True
