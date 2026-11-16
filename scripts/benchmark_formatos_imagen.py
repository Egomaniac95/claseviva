"""Compara codecs sobre copias; nunca reemplaza imágenes de las bibliotecas.

Necesita cjxl/djxl, jpegoptim y avifenc en --tools (binarios oficiales).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import time

import numpy as np
from PIL import Image

from scripts.optimizar_imagenes import image_directories


def run(command: list[str]) -> bytes:
    result = subprocess.run(command, capture_output=True, timeout=180,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1800:])
    return result.stdout


def select_samples(roots: list[Path]) -> list[Path]:
    selected = []
    for directory in image_directories(roots):
        files = sorted(directory.rglob("*.jpg"), key=lambda p: (p.stat().st_size, str(p)))
        for index in sorted({min(len(files) - 1, int(len(files) * q)) for q in (.25, .50, .90)}):
            if files:
                selected.append(files[index])
    return selected


def benchmark(path: Path, tools: Path, previews: Path | None = None) -> dict:
    original = path.read_bytes()
    with Image.open(BytesIO(original)) as source:
        source = source.convert("RGB")
    pixels = np.asarray(source)
    records = {}

    def record(name: str, data: bytes, seconds: float, *, reconstructed: bytes | None = None):
        if reconstructed is not None:
            decoded = np.asarray(Image.open(BytesIO(reconstructed)).convert("RGB"))
        else:
            decoded = np.asarray(Image.open(BytesIO(data)).convert("RGB"))
        mse = float(np.mean((pixels.astype(np.float64) - decoded.astype(np.float64)) ** 2))
        records[name] = {
            "bytes": len(data), "seconds": round(seconds, 4),
            "pixels_equal": bool(np.array_equal(pixels, decoded)),
            "psnr_db": round(10 * np.log10(255 ** 2 / mse), 3) if mse else None,
        }

    with tempfile.TemporaryDirectory(prefix="codec-sample-", dir=tools) as raw:
        work = Path(raw)
        png = work / "source.png"
        source.save(png)
        t = time.perf_counter()
        jpeg = run([str(tools / "jpegoptim.exe"), "--stdout", "--strip-none", "--all-progressive", str(path)])
        record("jpegoptim_lossless", jpeg, time.perf_counter() - t)
        for name, options in [
            ("webp_lossless", {"lossless": True, "quality": 80, "method": 4, "exact": True}),
            ("webp_q95", {"quality": 95, "method": 6}),
            ("webp_q85", {"quality": 85, "method": 6}),
            ("avif_q90_444", {"quality": 90, "speed": 6, "subsampling": "4:4:4"}),
            ("avif_q80_444", {"quality": 80, "speed": 6, "subsampling": "4:4:4"}),
            ("avif_q70_444", {"quality": 70, "speed": 6, "subsampling": "4:4:4"}),
        ]:
            output = BytesIO()
            t = time.perf_counter()
            source.save(output, format="AVIF" if name.startswith("avif") else "WEBP", **options)
            data = output.getvalue()
            record(name, data, time.perf_counter() - t)
            if previews is not None:
                (previews / (name + (".avif" if name.startswith("avif") else ".webp"))).write_bytes(data)
        target = work / "lossless.avif"
        t = time.perf_counter()
        run([str(tools / "avifenc.exe"), "--lossless", "--speed", "6", "--jobs", "2", str(png), str(target)])
        record("avif_lossless", target.read_bytes(), time.perf_counter() - t)
        for effort in (9, 10):
            target = work / f"lossless-e{effort}.jxl"
            recovered = work / f"reconstructed-e{effort}.jpg"
            t = time.perf_counter()
            run([str(tools / "cjxl.exe"), str(path), str(target), "--lossless_jpeg=1",
                 f"--effort={effort}", "--brotli_effort=11", "--num_threads=2"])
            seconds = time.perf_counter() - t
            run([str(tools / "djxl.exe"), str(target), str(recovered), "--num_threads=2"])
            data = target.read_bytes()
            reconstructed = recovered.read_bytes()
            record(f"jxl_jpeg_reconstruction_e{effort}", data, seconds, reconstructed=reconstructed)
            records[f"jxl_jpeg_reconstruction_e{effort}"]["original_jpeg_recovered_exactly"] = reconstructed == original
            if previews is not None and effort == 10:
                (previews / "lossless.jxl").write_bytes(data)
        if previews is not None:
            (previews / "original.jpg").write_bytes(original)
    assert path.read_bytes() == original
    return {"path": str(path), "source_bytes": len(original), "dimensions": list(source.size),
            "source_sha256": hashlib.sha256(original).hexdigest(), "variants": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raiz", action="append", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    tools = args.tools.resolve(strict=True)
    samples = select_samples(args.raiz)
    if not samples:
        parser.error("No se encontraron imágenes JPEG en las raíces indicadas")
    preview_source = (next((p for p in samples if "SEMANA_4" in str(p)), samples[0])
                      if args.preview else None)
    if args.preview:
        args.preview.mkdir(parents=True, exist_ok=True)
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(benchmark, p, tools, args.preview if p == preview_source else None): p for p in samples}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({"done": len(results), "total": len(samples), "file": str(futures[future])}), flush=True)
    results.sort(key=lambda item: item["path"])
    source_total = sum(r["source_bytes"] for r in results)
    summary = {}
    for name in results[0]["variants"]:
        variants = [r["variants"][name] for r in results]
        total = sum(v["bytes"] for v in variants)
        summary[name] = {
            "sample_bytes": total, "change_pct": round(100 * (total / source_total - 1), 2),
            "pixels_equal_all": all(v["pixels_equal"] for v in variants),
            "median_encode_seconds": round(float(np.median([v["seconds"] for v in variants])), 4),
            "mean_psnr_db": round(float(np.mean([v["psnr_db"] for v in variants if v["psnr_db"] is not None])), 3)
                if any(v["psnr_db"] is not None for v in variants) else None,
        }
        if name.startswith("jxl"):
            summary[name]["original_jpeg_recovered_all"] = all(v["original_jpeg_recovered_exactly"] for v in variants)
    report = {"sample_count": len(results), "sampling": "25th, 50th and 90th file-size quantile in each img directory",
              "settings": {"webp_lossless": "lossless=True, quality=80 (effort), method=4, exact=True",
                           "avif_lossless": "avifenc --lossless --speed 6 --jobs 2",
                           "jxl": "JPEG coefficient transcoding, Brotli effort 11, encoder effort 9 and 10",
                           "jpegoptim": "--stdout --strip-none --all-progressive"},
              "source": "JPEG already optimized with MozJPEG; additional savings use this baseline",
              "source_sample_bytes": source_total, "summary": summary, "samples": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}, indent=2), flush=True)


if __name__ == "__main__":
    main()
