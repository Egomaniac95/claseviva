"""Convierte el PNG creado con IA a recursos pequeños PNG/ICO; no genera con APIs."""
import argparse
from pathlib import Path

from PIL import Image


def export_icon(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGBA")
        if image.width != image.height:
            raise ValueError("El icono original debe ser cuadrado")
        icon = image.resize((256, 256), Image.Resampling.LANCZOS)
        icon.save(destination / "claseviva.png", optimize=True)
        icon.save(destination / "claseviva.ico", format="ICO",
                  sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    export_icon(args.source, args.destination)
