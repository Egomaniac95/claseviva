"""Conserva avisos de redistribución disponibles en los wheels usados para el EXE."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import sys


def notice_file(path: str) -> bool:
    candidate = Path(path)
    if (candidate.is_absolute() or ".." in candidate.parts or "__pycache__" in candidate.parts
            or candidate.suffix.casefold() not in {"", ".txt", ".md", ".rst", ".html", ".license"}):
        return False
    return (any(part.casefold() in {"licenses", "licences"} for part in candidate.parts)
            or bool(re.match(r"(?:licen[sc]e|copying|copyright|notice|third.?party.?notices)",
                             candidate.name, re.I)))


def collect(bundle: Path, lock: Path) -> dict:
    target = bundle / "THIRD_PARTY"
    target.mkdir(parents=True, exist_ok=True)
    names = [line.split("==", 1)[0] for line in lock.read_text(encoding="utf-8").splitlines()
             if "==" in line and not line.lstrip().startswith("#")]
    names.append("pyinstaller")  # licencia del bootloader y excepción de distribución
    packages = []
    for name in names:
        dist = metadata.distribution(name)
        folder = target / f"{name}-{dist.version}"
        copied = []
        for item in dist.files or []:
            if not notice_file(str(item)):
                continue
            source = Path(dist.locate_file(item))
            if not source.is_file():
                continue
            # No copiar jerarquías de desarrollo de un wheel al portable.
            destination = folder / f"{len(copied):03d}-{Path(item).name}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            copied.append({"source": str(item).replace("\\", "/"), "file": destination.name})
        packages.append({"package": name, "version": dist.version,
                         "license": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
                         "project_urls": dist.metadata.get_all("Project-URL") or [],
                         "notices": copied})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Falta la licencia del runtime Python del build")
    shutil.copyfile(python_license, target / "PYTHON-LICENSE.txt")
    report = {"python": sys.version.split()[0], "packages": packages,
              "scope": "Avisos disponibles en los wheels; no sustituye revisión de distribución pública."}
    (target / "packages.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"packages": len(packages), "notice_files": sum(len(p["notices"]) for p in packages) + 1}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("lock", type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.bundle, args.lock)))
