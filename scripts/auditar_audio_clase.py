"""Audita WAV, transcript y backlog STT de una clase ClaseViva.

No modifica evidencia. Escribe un reporte opcional en _debug para reproducir fallos de
captura como "Chrome suena pero el WAV queda casi mudo".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import audio_manifest, estado, transcript_quality
from src.captura_audio import (
    PCM_CASI_CERO_PEAK,
    PCM_CASI_CERO_RMS,
    metricas_wav,
)


def _leer_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_raw": line, "_error": "json_decode"})
    return out


def _epoch_chunk(nombre: str) -> int | None:
    m = re.search(r"chunk_(\d+)_", nombre)
    return int(m.group(1)) if m else None


def _celda_md(valor) -> str:
    texto = "" if valor is None else str(valor)
    return texto.replace("|", "\\|").replace("\n", " ").strip()


def auditar(clase: Path, transcript_oficial: Path | None = None) -> dict:
    audio_dir = clase / "audio"
    wavs = sorted(audio_dir.glob("chunk_*.wav")) if audio_dir.is_dir() else []
    transcript = _leer_jsonl(clase / "transcript.jsonl")
    stt = audio_manifest.transcript_status_report(clase)
    chunks_transcritos = set(stt["completed_chunks"])
    filas = []
    utiles = mudos = pendientes = 0
    for wav in wavs:
        met = metricas_wav(wav)
        casi_mudo = (
            met["rms"] <= PCM_CASI_CERO_RMS
            and int(met["peak"]) <= PCM_CASI_CERO_PEAK
        )
        transcrito = wav.stem in chunks_transcritos
        if met["useful_audio"]:
            utiles += 1
        if casi_mudo:
            mudos += 1
        if not transcrito:
            pendientes += 1
        filas.append({
            "archivo": wav.name,
            "epoch": _epoch_chunk(wav.name),
            "duration_sec": round(float(met["duration_sec"]), 2),
            "rms": round(float(met["rms"]), 2),
            "peak": int(met["peak"]),
            "useful_audio": bool(met["useful_audio"]),
            "casi_mudo": casi_mudo,
            "transcrito": transcrito,
        })
    continuidad = audio_manifest.continuity_report(clase)
    filtered_transcript, replay = transcript_quality.filter_replayed_segments(transcript)
    official = None
    if transcript_oficial is not None and transcript_oficial.is_file():
        official_text = transcript_oficial.read_text(encoding="utf-8-sig")
        official = transcript_quality.compare_with_official_text(
            official_text, transcript)
        unique = transcript_quality.compare_with_official_text(
            official_text, filtered_transcript)
        official["official_sha256"] = hashlib.sha256(
            transcript_oficial.read_bytes()).hexdigest()
        official["derived_unique_words"] = unique["captured_words"]
        official["derived_unique_wer"] = unique["wer"]
        official["path"] = str(transcript_oficial.resolve())
    return {
        "clase": str(clase),
        "wav_total": len(wavs),
        "wav_utiles": utiles,
        "wav_casi_mudos": mudos,
        "wav_pendientes": pendientes,
        "transcript_segmentos": len(transcript),
        "chunks_transcritos": len(chunks_transcritos),
        "commits_vacios": stt["empty_completed_count"],
        "commits_invalidos": stt["invalid_count"],
        "estado_stt": stt,
        "capture_failures_terminales": continuidad.get(
            "terminal_capture_failures", 0),
        "capture_failures_recuperados": continuidad.get(
            "recovered_capture_failures", 0),
        "capturas_inconclusas": continuidad.get("unfinished_capture_count", 0),
        "continuidad": continuidad,
        "replay": replay,
        "transcript_oficial": official,
        "filas": filas,
    }


def reporte_md(res: dict) -> str:
    lineas = [
        "# Auditoria de audio",
        "",
        f"- Clase: `{res['clase']}`",
        f"- WAV total: {res['wav_total']}",
        f"- WAV utiles: {res['wav_utiles']}",
        f"- WAV casi mudos: {res['wav_casi_mudos']}",
        f"- WAV pendientes o inválidos de STT: {res['wav_pendientes']}",
        f"- Segmentos transcript: {res['transcript_segmentos']}",
        f"- Chunks con commit STT completo: {res['chunks_transcritos']}",
        f"- Commits STT completos vacíos: {res.get('commits_vacios', 0)}",
        f"- Commits STT inválidos: {res.get('commits_invalidos', 0)}",
        f"- Continuidad: {res['continuidad']['status']} "
        f"({res['continuidad']['source']})",
        f"- Déficit PCM: {res['continuidad']['deficit_seconds']:.3f} s",
        f"- Gaps entre WAV: total {res['continuidad']['wall_gap_total_seconds']:.3f} s; "
        f"p95 {res['continuidad']['wall_gap_p95_seconds']:.3f} s; "
        f"máximo {res['continuidad']['wall_gap_max_seconds']:.3f} s",
        f"- Discontinuidades declaradas: "
        f"WASAPI={res['continuidad'].get('data_discontinuity_packets', 0)}; "
        f"terminales={res['continuidad'].get('terminal_discontinuities', 0)}",
        f"- Fallos de captura: "
        f"terminales={res['continuidad'].get('terminal_capture_failures', 0)}; "
        f"recuperados={res['continuidad'].get('recovered_capture_failures', 0)}",
        f"- Capturas inconclusas: "
        f"total={res['continuidad'].get('unfinished_capture_count', 0)}; "
        f"terminales={res['continuidad'].get('terminal_unfinished_capture_count', 0)}; "
        f"activas={res['continuidad'].get('active_unfinished_capture_count', 0)}",
        f"- Replays derivados detectados: "
        f"{res.get('replay', {}).get('replayed_runs', 0)} secuencia(s), "
        f"{res.get('replay', {}).get('replayed_segments', 0)} segmento(s); "
        "el transcript canónico no se modifica",
        "",
        "| WAV | dur_s | rms | peak | util | casi_mudo | transcrito |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    for f in res["filas"]:
        lineas.append(
            f"| {f['archivo']} | {f['duration_sec']} | {f['rms']} | {f['peak']} | "
            f"{'si' if f['useful_audio'] else 'no'} | "
            f"{'si' if f['casi_mudo'] else 'no'} | "
            f"{'si' if f['transcrito'] else 'no'} |"
        )
    fallos_terminales = res["continuidad"].get(
        "terminal_capture_failure_details", [])
    if fallos_terminales:
        lineas.extend([
            "",
            "## Fallos terminales de captura",
            "",
            "| chunk | secuencia | motivo | error |",
            "|---|---:|---|---|",
        ])
        for fallo in fallos_terminales:
            lineas.append(
                f"| {_celda_md(fallo.get('chunk_id'))} | "
                f"{_celda_md(fallo.get('sequence'))} | "
                f"{_celda_md(fallo.get('reason'))} | "
                f"{_celda_md(fallo.get('error_type'))} |"
            )
    inconclusas = res["continuidad"].get("unfinished_capture_details", [])
    if inconclusas:
        lineas.extend([
            "",
            "## Capturas inconclusas",
            "",
            "| chunk | secuencia | inicio | estado |",
            "|---|---:|---:|---|",
        ])
        for captura in inconclusas:
            estado_captura = "activa" if captura.get("active_session") else "terminal"
            lineas.append(
                f"| {_celda_md(captura.get('chunk_id'))} | "
                f"{_celda_md(captura.get('sequence'))} | "
                f"{_celda_md(captura.get('wall_start'))} | {estado_captura} |"
            )
    official = res.get("transcript_oficial")
    if official:
        lineas.extend([
            "", "## Contraste con transcript oficial", "",
            f"- Fuente: `{official.get('path')}`",
            f"- SHA-256: `{official.get('official_sha256')}`",
            f"- Palabras oficial/captura: {official.get('official_words')} / "
            f"{official.get('captured_words')}.",
            f"- WER normalizado: {float(official.get('wer', 0)) * 100:.2f}%.",
            f"- Vista derivada sin replay: {official.get('derived_unique_words')} palabras; "
            f"WER {float(official.get('derived_unique_wer', 0)) * 100:.2f}%.",
            "- El filtro de replay no reescribe transcript ni commits STT.",
        ])
    reasons = []
    continuity = res["continuidad"]
    if res.get("wav_casi_mudos"):
        reasons.append(f"WAV casi mudos: {res['wav_casi_mudos']}")
    if res.get("wav_pendientes"):
        reasons.append(f"WAV sin commit STT: {res['wav_pendientes']}")
    if res.get("commits_vacios"):
        reasons.append(f"commits STT vacíos: {res['commits_vacios']}")
    if res.get("commits_invalidos"):
        reasons.append(f"commits STT inválidos: {res['commits_invalidos']}")
    if continuity.get("terminal_capture_failures"):
        reasons.append(
            f"fallos terminales de captura: {continuity['terminal_capture_failures']}")
    if continuity.get("unfinished_capture_count"):
        reasons.append(f"capturas inconclusas: {continuity['unfinished_capture_count']}")
    if continuity.get("data_discontinuity_packets") or continuity.get(
            "terminal_discontinuities"):
        reasons.append("discontinuidad PCM declarada")
    if float(continuity.get("deficit_seconds", 0) or 0) > 0:
        reasons.append(
            f"déficit PCM: {float(continuity['deficit_seconds']):.3f} s")
    if float(continuity.get("wall_gap_max_seconds", 0) or 0) > 0.2:
        reasons.append(
            "gap de reloj máximo: "
            f"{float(continuity['wall_gap_max_seconds']):.3f} s")
    if reasons:
        lineas.extend([
            "",
            "## Veredicto",
            "",
            "La evidencia requiere revisión por estos hallazgos concretos:",
            *[f"- {reason}." for reason in reasons],
        ])
    return "\n".join(lineas) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Audita audio/transcript de una clase")
    ap.add_argument("--clase", default="", help="Ruta de CLASE_XX; por defecto clase activa")
    ap.add_argument("--out", default="", help="Reporte Markdown")
    ap.add_argument("--json", default="", help="Reporte JSON")
    ap.add_argument(
        "--transcript-oficial", default="",
        help="TXT oficial opcional para WER, hash y contraste de cobertura")
    args = ap.parse_args()

    clase = Path(args.clase) if args.clase else estado.clase_activa()
    if clase is None or not clase.is_dir():
        raise SystemExit("No se encontro clase para auditar")
    official = Path(args.transcript_oficial) if args.transcript_oficial else None
    res = auditar(clase, official)
    md = reporte_md(res)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
    if args.json:
        out_json = Path(args.json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
