"""Calidad derivada del transcript sin reescribir la evidencia canónica.

Detecta replays continuos (por ejemplo, cuando se vuelve a reproducir el cierre de un
video) y permite contrastar una vista STT con un transcript oficial. Ninguna función de
este módulo escribe ``transcript.jsonl`` ni los commits transaccionales.
"""
from __future__ import annotations

import hashlib
import re
import statistics
import unicodedata
from difflib import SequenceMatcher


def tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", normalized)


def _time(row: dict, key: str = "t") -> float:
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _end_time(row: dict) -> float:
    start = _time(row)
    end = _time(row, "fin")
    if end >= start:
        return end
    local_start = _time(row, "ini")
    if end >= local_start:
        return start + (end - local_start)
    return start


def _similarity(left: list[str], right: list[str]) -> float:
    if min(len(left), len(right)) < 5:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def detect_replayed_segments(
        segments: list[dict], *, min_separation_seconds: float = 60.0,
        anchor_similarity: float = 0.68, min_anchors: int = 3,
        min_matched_words: int = 30, max_anchor_gap: int = 4) -> dict:
    """Encuentra rangos posteriores repetidos con varios anclajes consecutivos.

    Una frase aislada repetida por énfasis no basta. Se exige el mismo desplazamiento
    entre índices, al menos tres segmentos ancla, separación temporal y masa textual.
    Los huecos pequeños entre anclas permiten variaciones legítimas del STT dentro de
    una reproducción real.
    """
    rows = list(segments or [])
    token_rows = [tokens(str(row.get("texto", ""))) for row in rows]
    by_offset: dict[int, list[tuple[int, int, float, int]]] = {}
    for later in range(len(rows)):
        if len(token_rows[later]) < 5:
            continue
        best_by_offset: dict[int, tuple[int, int, float, int]] = {}
        for earlier in range(later):
            separation = _time(rows[later]) - _time(rows[earlier])
            if separation < min_separation_seconds:
                continue
            similarity = _similarity(token_rows[earlier], token_rows[later])
            if similarity < anchor_similarity:
                continue
            offset = later - earlier
            candidate = (
                later, earlier, similarity,
                min(len(token_rows[earlier]), len(token_rows[later])),
            )
            previous = best_by_offset.get(offset)
            if previous is None or candidate[2] > previous[2]:
                best_by_offset[offset] = candidate
        for offset, candidate in best_by_offset.items():
            by_offset.setdefault(offset, []).append(candidate)

    candidates: list[dict] = []
    for offset, anchors in by_offset.items():
        anchors.sort()
        groups: list[list[tuple[int, int, float, int]]] = []
        for anchor in anchors:
            if not groups or anchor[0] - groups[-1][-1][0] > max_anchor_gap:
                groups.append([anchor])
            else:
                groups[-1].append(anchor)
        for group in groups:
            matched_words = sum(anchor[3] for anchor in group)
            if len(group) < min_anchors or matched_words < min_matched_words:
                continue
            later_start, later_end = group[0][0], group[-1][0]
            earlier_start = later_start - offset
            earlier_end = later_end - offset
            separation = _time(rows[later_start]) - _time(rows[earlier_start])
            candidates.append({
                "later_start_index": later_start,
                "later_end_index": later_end,
                "earlier_start_index": earlier_start,
                "earlier_end_index": earlier_end,
                "anchor_count": len(group),
                "matched_words": matched_words,
                "similarity_median": round(
                    statistics.median(anchor[2] for anchor in group), 4),
                "separation_seconds": round(separation, 3),
                "later_start": _time(rows[later_start]),
                "later_end": _end_time(rows[later_end]),
                "earlier_start": _time(rows[earlier_start]),
                "earlier_end": _end_time(rows[earlier_end]),
            })

    # Prefiere el candidato con más evidencia cuando dos offsets cubren el mismo final.
    candidates.sort(key=lambda item: (
        -item["anchor_count"], -item["matched_words"],
        item["later_start_index"], item["later_end_index"],
    ))
    accepted: list[dict] = []
    occupied: set[int] = set()
    for candidate in candidates:
        covered = set(range(
            candidate["later_start_index"], candidate["later_end_index"] + 1))
        if covered & occupied:
            continue
        occupied.update(covered)
        accepted.append(candidate)
    accepted.sort(key=lambda item: item["later_start_index"])
    return {
        "schema_version": 1,
        "segments_total": len(rows),
        "replayed_segments": len(occupied),
        "replayed_runs": len(accepted),
        "replayed_indices": sorted(occupied),
        "runs": accepted,
        "canonical_transcript_unchanged": True,
    }


def filter_replayed_segments(segments: list[dict], **kwargs) -> tuple[list[dict], dict]:
    report = detect_replayed_segments(segments, **kwargs)
    excluded = set(report["replayed_indices"])
    return [row for index, row in enumerate(segments) if index not in excluded], report


def _wer(reference: list[str], hypothesis: list[str]) -> tuple[float, int]:
    previous = list(range(len(hypothesis) + 1))
    for row_index, reference_word in enumerate(reference, 1):
        current = [row_index]
        for column_index, hypothesis_word in enumerate(hypothesis, 1):
            current.append(min(
                current[-1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + (reference_word != hypothesis_word),
            ))
        previous = current
    distance = previous[-1]
    return distance / max(1, len(reference)), distance


def compare_with_official_text(official_text: str, segments: list[dict]) -> dict:
    raw = str(official_text or "")
    reference = tokens(raw)
    hypothesis = tokens(" ".join(
        str(row.get("texto", "")) for row in segments or []
    ))
    error, distance = _wer(reference, hypothesis)
    return {
        "schema_version": 1,
        "official_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "official_words": len(reference),
        "captured_words": len(hypothesis),
        "captured_to_official_ratio": round(
            len(hypothesis) / max(1, len(reference)), 6),
        "wer": round(error, 6),
        "edit_distance": distance,
    }
