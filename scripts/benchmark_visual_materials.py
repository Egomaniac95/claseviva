"""Benchmark read-only del emparejador productivo de material visual oficial."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import estado as est  # noqa: E402
from src import post_procesar as post  # noqa: E402
from src import visual_materials  # noqa: E402


SCHEMA_VERSION = 1


class BenchmarkError(RuntimeError):
    """Entrada inválida, mutación detectada o workload sin evidencia útil."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot_visual(class_dir: Path) -> dict[str, dict]:
    names = (
        "session_state.json", "marcas.jsonl", "audio_manifest.jsonl",
        "transcript.jsonl", "img_log.jsonl", "visual_timeline.jsonl",
        "leccion_en_vivo.md", "leccion_final.md",
    )
    paths = [class_dir / name for name in names]
    img_dir = class_dir / "img"
    if img_dir.is_dir():
        paths.extend(sorted(path for path in img_dir.iterdir() if path.is_file()))
    snapshot = {}
    for path in paths:
        if path.is_file():
            relative = path.relative_to(class_dir).as_posix()
            snapshot[relative] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    return snapshot


def _fingerprint(snapshot: dict[str, dict]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_pages(spec: str, total: int) -> list[int]:
    if not spec.strip():
        return []
    pages: set[int] = set()
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        if "-" in item:
            start, end = (int(value) for value in item.split("-", 1))
            pages.update(range(min(start, end), max(start, end) + 1))
        else:
            pages.add(int(item))
    invalid = sorted(page for page in pages if page < 1 or page > total)
    if invalid:
        raise BenchmarkError(f"páginas fuera de rango 1..{total}: {invalid}")
    return sorted(pages)


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _git_info() -> dict:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL)
        return {"head": head, "dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"head": "unknown", "dirty": None}


def run_benchmark(class_dir: Path, materials: list[Path],
                  expected_pages: str = "") -> dict:
    class_dir = class_dir.resolve()
    materials = [path.resolve() for path in materials]
    if not (class_dir / "session_state.json").is_file():
        raise BenchmarkError(f"clase inválida: {class_dir}")
    if not (class_dir / "img_log.jsonl").is_file():
        raise BenchmarkError("falta img_log.jsonl")
    if not materials:
        raise BenchmarkError("se requiere al menos un --material")
    missing = [str(path) for path in materials if not path.exists()]
    if missing:
        raise BenchmarkError(f"material inexistente: {missing}")

    before = _snapshot_visual(class_dir)
    before_fingerprint = _fingerprint(before)
    process = None
    try:
        import psutil
        process = psutil.Process(os.getpid())
    except (ImportError, OSError):
        pass
    rss_before = process.memory_info().rss if process is not None else None
    wall_start = time.perf_counter()
    cpu_start = time.process_time()

    state = est.leer_estado(class_dir)
    origin = {
        "origen": state.get("origen", "vivo"),
        "inicio": state.get("inicio", 0.0),
    }
    segments = est.leer_jsonl(class_dir / "transcript.jsonl")
    ranges = est.rangos_partes(class_dir)
    if not ranges:
        raise BenchmarkError("la clase no contiene rangos/partes auditables")

    part_reports = []
    detected_pages: set[int] = set()
    page_appearances: dict[int, int] = {}
    with tempfile.TemporaryDirectory(prefix="clase-viva-material-benchmark-") as temp:
        material_dir = Path(temp)
        pages, preparation = visual_materials.preparar_materiales_oficiales(
            materials, material_dir)
        if not pages:
            raise BenchmarkError(
                "no se prepararon páginas: " + "; ".join(preparation.get("errores", [])))
        expected = parse_pages(expected_pages, len(pages))
        for part, start, end in ranges:
            timeline = post.construir_timeline_slides(
                class_dir, start, end, segments, origin)
            result, match_report = visual_materials.integrar_material_oficial(
                class_dir / "img", timeline, pages,
                material_img_dir=material_dir)
            roles: dict[str, int] = {}
            for slide in result:
                role = str(slide.get("visual_role", "legacy"))
                roles[role] = roles.get(role, 0) + 1
                official = slide.get("material_oficial") or {}
                if official:
                    page = int(official.get("pagina", 0) or 0)
                    if page:
                        detected_pages.add(page)
                        page_appearances[page] = page_appearances.get(page, 0) + len(
                            slide.get("evidencias_aparicion") or [])
            part_reports.append({
                "parte": part,
                "timeline_states": len(timeline),
                "output_identities": len(result),
                "roles": roles,
                **match_report,
            })

    cpu_seconds = time.process_time() - cpu_start
    wall_seconds = time.perf_counter() - wall_start
    rss_after = process.memory_info().rss if process is not None else None
    after = _snapshot_visual(class_dir)
    after_fingerprint = _fingerprint(after)
    if before != after:
        changed = sorted(set(before) ^ set(after) | {
            name for name in set(before) & set(after) if before[name] != after[name]
        })
        raise BenchmarkError(f"el benchmark modificó evidencia: {changed}")

    total_pages = max((int(report.get("paginas_oficiales_usadas", 0))
                       for report in part_reports), default=0)
    missing_expected = sorted(set(expected) - detected_pages)
    recall = (len(set(expected) & detected_pages) / len(expected)) if expected else None
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "visual-materials-read-only",
        "class_dir": str(class_dir),
        "materials": [str(path) for path in materials],
        "material_source_sha256": sorted({
            str(page.get("fuente_sha256", "")) for page in pages
            if page.get("fuente_sha256")
        }),
        "input_fingerprint_before": before_fingerprint,
        "input_fingerprint_after": after_fingerprint,
        "read_only_verified": before_fingerprint == after_fingerprint,
        "files_fingerprinted": len(before),
        "captures_logged": len(est.leer_jsonl(class_dir / "img_log.jsonl")),
        "official_pages_available": len(pages),
        "official_pages_detected": sorted(detected_pages),
        "official_pages_detected_total": len(detected_pages),
        "official_pages_used_max_per_part": total_pages,
        "appearances_by_page": {
            str(page): page_appearances[page] for page in sorted(page_appearances)
        },
        "expected_pages": expected,
        "missing_expected_pages": missing_expected,
        "expected_page_recall": round(recall, 6) if recall is not None else None,
        "parts": part_reports,
        "performance": {
            "wall_seconds": round(wall_seconds, 6),
            "process_cpu_seconds": round(cpu_seconds, 6),
            "rss_before_bytes": rss_before,
            "rss_after_bytes": rss_after,
            "rss_delta_bytes": ((rss_after - rss_before)
                                if rss_before is not None and rss_after is not None else None),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pillow": _version("Pillow"),
            "opencv": _version("opencv-python"),
            "pypdfium2": _version("pypdfium2"),
        },
        "implementation": {
            "git": _git_info(),
            "visual_materials_sha256": _sha256(ROOT / "src" / "visual_materials.py"),
            "post_procesar_sha256": _sha256(ROOT / "src" / "post_procesar.py"),
            "harness_sha256": _sha256(Path(__file__).resolve()),
        },
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark read-only de capturas contra material oficial")
    parser.add_argument("--clase", required=True, type=Path)
    parser.add_argument("--material", required=True, action="append", type=Path)
    parser.add_argument("--expected-pages", default="",
                        help="páginas realmente mostradas, por ejemplo 1,2,5-8")
    parser.add_argument("--out", type=Path, help="JSON de salida fuera de la clase")
    args = parser.parse_args(argv)
    try:
        class_dir = args.clase.resolve()
        if args.out is not None and args.out.resolve().is_relative_to(class_dir):
            raise BenchmarkError("--out debe quedar fuera de la evidencia de clase")
        result = run_benchmark(class_dir, args.material, args.expected_pages)
        if args.out is not None:
            _write_json(args.out.resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (BenchmarkError, OSError, ValueError) as exc:
        print(f"[benchmark-material] ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
