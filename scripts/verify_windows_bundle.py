"""Gate offline del directorio portable; no arranca grabación ni usa APIs/clases."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(bundle: Path) -> dict:
    bundle = bundle.resolve()
    forbidden = {".env", "contenido", "clase_actual.txt", "memoria_cursos.md",
                 "logs", ".git", ".venv", "tests", "__pycache__"}
    files = sorted(path for path in bundle.rglob("*") if path.is_file())
    for path in files:
        if forbidden.intersection(path.relative_to(bundle).parts):
            raise RuntimeError("Datos privados o artefactos de desarrollo en distribución")
    if not (bundle / "ClaseViva.exe").is_file():
        raise RuntimeError("Falta ClaseViva.exe")
    required = [".env.example", "Configurar claves.cmd", "LEEME.md",
                "_internal/assets/silero_vad/silero_vad.onnx",
                "_internal/assets/silero_vad/LICENSE"]
    if any(not (bundle / name).is_file() for name in required):
        raise RuntimeError("Faltan configuración, instrucciones o recursos Silero del portable")
    if not (bundle / "THIRD_PARTY").is_dir():
        raise RuntimeError("Faltan los avisos de licencia del portable")
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("CLASE", "DEEPSEEK", "ASSEMBLYAI", "DEEPGRAM", "SPEECHMATICS", "GROQ", "PYTHON")))}
    env.update({"CLASEVIVA_NO_DIALOG": "1", "CLASEVIVA_NO_BROWSER": "1"})
    env["PATH"] = os.pathsep.join([
        str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"),
        os.environ.get("SystemRoot", r"C:\Windows"),
    ])
    with tempfile.TemporaryDirectory(prefix="ClaseViva-offline-check-") as tmp:
        root = Path(tmp)
        env["CLASEVIVA_DATA_DIR"] = str(root / "datos á con espacios")
        report = root / "result.json"
        completed = subprocess.run(
            [str(bundle / "ClaseViva.exe"), "--self-test", "--report", str(report)],
            cwd=tmp, env=env, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"ok": False}
        if completed.returncode != 0 or not result.get("ok") or not result.get("frozen"):
            raise RuntimeError(f"Diagnóstico de binario falló: código={completed.returncode}, checks={result}")
        worker_checks = {}
        for module in ("captura_audio", "captura_pantalla", "transcriptor_vivo", "escriba"):
            worker = subprocess.run(
                [str(bundle / "ClaseViva.exe"), "--worker", f"src.{module}"],
                cwd=tmp, env=env, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            log = Path(env["CLASEVIVA_DATA_DIR"]) / "logs" / f"{module}.log"
            message = log.read_text(encoding="utf-8") if log.exists() else ""
            ok = worker.returncode == 1 and "no hay clase activa" in message and "Traceback" not in message
            worker_checks[module] = {"ok": ok, "exit_code": worker.returncode,
                                     "reason": "no_active_class" if ok else "unexpected"}
            if not ok:
                raise RuntimeError(f"El worker {module} no pudo iniciar/cerrar sin una clase")
    manifest = {
        "schema_version": 1, "offline_self_test": result, "offline_workers": worker_checks,
        "files": [{"path": p.relative_to(bundle).as_posix(), "bytes": p.stat().st_size,
                   "sha256": file_hash(p)} for p in files],
    }
    target = bundle.parent / "bundle-verification.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "files": len(files), "bytes": sum(p.stat().st_size for p in files),
            "report": str(target)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    print(json.dumps(verify(parser.parse_args().bundle), ensure_ascii=False))
