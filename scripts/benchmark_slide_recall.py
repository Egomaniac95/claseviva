"""Compara capturas/seleccion final contra paginas de un deck ya renderizado.

El script no descarga ni modifica evidencia. El PDF debe renderizarse fuera del
script a PNG/JPEG; ``--shown-pages`` identifica las paginas realmente visibles
en el video y evita penalizar notas que nunca aparecieron.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path

from PIL import Image


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _natural_path_key(path: Path) -> tuple[tuple[int, int | str], ...]:
    """Ordena nombres con numeros por su valor: page_2 antes de page_10."""
    return tuple(
        (1, int(token)) if token.isdigit() else (0, token.casefold())
        for token in re.split(r"(\d+)", path.name)
        if token
    )


def parse_pages(spec: str, total: int) -> list[int]:
    if not spec.strip():
        return list(range(1, total + 1))
    pages = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start, end = (int(x) for x in item.split("-", 1))
            pages.update(range(min(start, end), max(start, end) + 1))
        else:
            pages.add(int(item))
    invalid = sorted(p for p in pages if p < 1 or p > total)
    if invalid:
        raise ValueError(f"paginas fuera de rango 1..{total}: {invalid}")
    return sorted(pages)


def _dhash(image: Image.Image, size: int = 12) -> int:
    gray = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    get_pixels = getattr(gray, "get_flattened_data", gray.getdata)
    pixels = list(get_pixels())
    value = 0
    for y in range(size):
        row = y * (size + 1)
        for x in range(size):
            value = (value << 1) | int(pixels[row + x] > pixels[row + x + 1])
    return value


def _crop_variants(image: Image.Image) -> list[Image.Image]:
    width, height = image.size
    boxes = [
        (0, 0, width, height),
        (int(width * 0.05), int(height * 0.05), int(width * 0.95), int(height * 0.95)),
        (int(width * 0.10), 0, int(width * 0.90), height),
        (0, 0, int(width * 0.85), height),
        (int(width * 0.15), 0, width, height),
    ]
    return [image.crop(box) for box in boxes if box[2] > box[0] and box[3] > box[1]]


def image_signature(path: Path) -> list[int]:
    with Image.open(path) as image:
        image.load()
        return [_dhash(crop) for crop in _crop_variants(image.convert("RGB"))]


def visual_similarity(a: list[int], b: list[int], bits: int = 144) -> float:
    distance = min((x ^ y).bit_count() for x in a for y in b)
    return max(0.0, 1.0 - distance / bits)


def _tokens(text: str) -> set[str]:
    normal = "".join(
        char for char in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(char)
    )
    stop = {"the", "and", "for", "with", "from", "this", "that", "una", "para", "con"}
    return {
        token for token in re.findall(r"[a-z0-9]{3,}", normal)
        if token not in stop
    }


def ocr_similarity(a: str, b: str) -> tuple[float, int]:
    ta, tb = _tokens(a), _tokens(b)
    common = len(ta & tb)
    return common / max(1, len(ta | tb)), common


def _lesson_refs(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8")
    refs = set(re.findall(r"(?:img/|src=[\"']img/)([^\s)\"'>]+)", text, re.IGNORECASE))
    return {Path(ref).name for ref in refs}


class _OcrExtractor:
    """OCR instrumentado: disponibilidad de import no equivale a inferencia operativa."""

    def __init__(self, enabled: bool):
        self.requested = bool(enabled)
        self.successful_calls = 0
        self.failed_calls = 0
        self._engine = None
        self._cache: dict[Path, str] = {}
        if not self.requested:
            return
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        except Exception:
            self._engine = None

    @property
    def operational(self) -> bool:
        return self._engine is not None and self.successful_calls > 0

    def __call__(self, path: Path) -> str:
        path = Path(path)
        if path in self._cache:
            return self._cache[path]
        if self._engine is None:
            return ""
        try:
            result, _ = self._engine(str(path))
            text = "\n".join(str(item[1]) for item in (result or []))
            self.successful_calls += 1
        except Exception:
            self.failed_calls += 1
            text = ""
        self._cache[path] = text
        return text


def _ocr_engine(enabled: bool) -> _OcrExtractor:
    return _OcrExtractor(enabled)


def _best_matches(deck: list[dict], candidates: list[dict],
                  visual_threshold: float, ocr_threshold: float) -> list[dict]:
    pares: list[list[dict]] = []
    elegibles: list[list[int]] = []
    for page in deck:
        fila = []
        indices_elegibles = []
        for candidate_idx, candidate in enumerate(candidates):
            visual = visual_similarity(page["signature"], candidate["signature"])
            ocr_score, common = ocr_similarity(page["ocr"], candidate["ocr"])
            score = max(visual, ocr_score)
            current = {
                "page": page["page"],
                "deck_file": page["path"].name,
                "candidate": candidate["path"].name,
                "visual_similarity": round(visual, 4),
                "ocr_similarity": round(ocr_score, 4),
                "ocr_common_tokens": common,
                "score": round(score, 4),
                "matched": (
                    visual >= visual_threshold
                    or (ocr_score >= ocr_threshold and common >= 3)
                ),
            }
            fila.append(current)
            if current["matched"]:
                indices_elegibles.append(candidate_idx)
        indices_elegibles.sort(
            key=lambda idx: (-float(fila[idx]["score"]),
                             str(fila[idx]["candidate"])))
        pares.append(fila)
        elegibles.append(indices_elegibles)

    # Matching bipartito de cardinalidad maxima: una captura no puede acreditar
    # dos paginas distintas del deck. El orden por score solo desempata dentro
    # de una asignacion valida 1:1.
    candidato_duenio: dict[int, int] = {}
    asignacion: dict[int, int] = {}

    def aumentar(page_idx: int, vistos: set[int]) -> bool:
        for candidate_idx in elegibles[page_idx]:
            if candidate_idx in vistos:
                continue
            vistos.add(candidate_idx)
            duenio = candidato_duenio.get(candidate_idx)
            if duenio is None or aumentar(duenio, vistos):
                candidato_duenio[candidate_idx] = page_idx
                asignacion[page_idx] = candidate_idx
                return True
        return False

    orden_paginas = sorted(
        range(len(deck)),
        key=lambda idx: (len(elegibles[idx]) if elegibles[idx] else 10**9,
                         int(deck[idx]["page"])),
    )
    for page_idx in orden_paginas:
        aumentar(page_idx, set())

    matches = []
    for page_idx, page in enumerate(deck):
        candidate_idx = asignacion.get(page_idx)
        if candidate_idx is not None:
            matches.append(dict(pares[page_idx][candidate_idx], matched=True))
            continue
        if pares[page_idx]:
            best = max(
                pares[page_idx],
                key=lambda item: (float(item["score"]), str(item["candidate"])),
            )
            matches.append(dict(
                best, matched=False,
                assignment_conflict=bool(elegibles[page_idx])))
        else:
            matches.append({
                "page": page["page"], "deck_file": page["path"].name,
                "candidate": "", "score": 0.0, "matched": False,
            })
    return matches


def evaluar(clase: Path, deck_dir: Path, shown_pages: str = "", use_ocr: bool = True,
            visual_threshold: float = 0.82, ocr_threshold: float = 0.22) -> dict:
    clase = Path(clase)
    deck_paths = sorted(
        (
            p for p in Path(deck_dir).iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        ),
        key=_natural_path_key,
    )
    pages = parse_pages(shown_pages, len(deck_paths))
    shown_paths = [(page, deck_paths[page - 1]) for page in pages]
    image_dir = clase / "img"
    capture_paths = sorted(
        p for p in image_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ) if image_dir.is_dir() else []
    refs = _lesson_refs(clase / "leccion_final.md")
    selected_paths = [p for p in capture_paths if p.name in refs]
    extract_ocr = _ocr_engine(use_ocr)

    def records(paths, numbered=False):
        out = []
        for index, item in enumerate(paths, 1):
            page, path = item if numbered else (None, item)
            out.append({
                "page": page if numbered else index,
                "path": path,
                "signature": image_signature(path),
                "ocr": extract_ocr(path),
            })
        return out

    deck = records(shown_paths, numbered=True)
    captured = records(capture_paths)
    selected = records(selected_paths)
    capture_matches = _best_matches(
        deck, captured, visual_threshold, ocr_threshold)
    selected_matches = _best_matches(
        deck, selected, visual_threshold, ocr_threshold)
    matched_selected = [m for m in selected_matches if m["matched"]]
    pages_by_candidate = {}
    for match in matched_selected:
        pages_by_candidate.setdefault(match["candidate"], []).append(match["page"])
    best_page_for_selected = {}
    for candidate in selected:
        best = None
        for page in deck:
            visual = visual_similarity(page["signature"], candidate["signature"])
            ocr_score, common = ocr_similarity(page["ocr"], candidate["ocr"])
            matched = visual >= visual_threshold or (
                ocr_score >= ocr_threshold and common >= 3)
            current = (max(visual, ocr_score), page["page"], matched)
            if best is None or current[0] > best[0]:
                best = current
        if best and best[2]:
            best_page_for_selected[candidate["path"].name] = best[1]
    informative_selected = set(best_page_for_selected)
    page_counts = {}
    for page in best_page_for_selected.values():
        page_counts[page] = page_counts.get(page, 0) + 1
    duplicates = sum(max(0, count - 1) for count in page_counts.values())
    shown_total = len(deck)
    return {
        "class": str(clase),
        "deck_dir": str(deck_dir),
        "ground_truth": "shown_pages" if shown_pages.strip() else "all_deck_pages_assumed",
        "shown_pages": pages,
        "shown_pages_total": shown_total,
        "captured_images": len(captured),
        "selected_images": len(selected),
        "capture_recall": round(
            sum(1 for m in capture_matches if m["matched"]) / max(1, shown_total), 4),
        "selected_recall": round(len(matched_selected) / max(1, shown_total), 4),
        "selected_precision": round(
            len(informative_selected) / max(1, len(selected)), 4),
        "selected_duplicate_rate": round(duplicates / max(1, len(selected)), 4),
        "missing_from_capture": [m["page"] for m in capture_matches if not m["matched"]],
        "missing_from_lesson": [m["page"] for m in selected_matches if not m["matched"]],
        "capture_matches": capture_matches,
        "selected_matches": selected_matches,
        "thresholds": {
            "visual": visual_threshold,
            "ocr": ocr_threshold,
            "ocr_requested": bool(use_ocr),
            "ocr_enabled": extract_ocr.operational,
            "ocr_successful_calls": extract_ocr.successful_calls,
            "ocr_failed_calls": extract_ocr.failed_calls,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clase", required=True)
    parser.add_argument("--deck-dir", required=True,
                        help="PNG/JPEG del deck, ordenados por nombre")
    parser.add_argument("--shown-pages", default="",
                        help="paginas realmente mostradas: 1,2,5-8")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    result = evaluar(
        Path(args.clase), Path(args.deck_dir), args.shown_pages,
        use_ocr=not args.no_ocr)
    raw = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(raw + "\n", encoding="utf-8")
    print(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
