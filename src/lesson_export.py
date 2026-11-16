"""Publicación offline ligera; imágenes incrustadas solo al descargar una copia."""
import base64
import html
import re
from pathlib import Path
from urllib.parse import quote, unquote


def render_html(class_dir: Path, *, self_contained: bool = False) -> str:
    from . import study_package
    from .panel import _pagina_leccion_html

    d = Path(class_dir).resolve()
    validation = study_package.validate_candidate(d)
    if not validation["ok"]:
        raise RuntimeError("No se puede exportar: lección o imágenes inválidas")
    text = (d / "leccion_final.md").read_text(encoding="utf-8")
    page = _pagina_leccion_html(text, standalone=True)
    images = {}
    used = set()
    for name in validation["references"]:
        path = (d / "img" / name).resolve()
        if not path.is_relative_to(d / "img"):
            raise RuntimeError("Imagen fuera de la carpeta de clase")
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}.get(path.suffix.lower())
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}:
            raise RuntimeError("Formato de imagen no permitido en HTML autocontenido")
        images[name] = (
            f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
            if self_contained else "img/" + quote(name, safe="/"))

    def replace(match):
        name = unquote(html.unescape(match.group(2)))
        if name not in images:
            raise RuntimeError("Referencia visual no inventariada")
        used.add(name)
        return f'{match.group(1)}="{images[name]}"'

    page = re.sub(r'(src|href)="/img/([^"?]+)"', replace, page)
    page = page.replace('<a href="/leccion_final.md">Ver Markdown</a>',
                        '<span>Documento autocontenido · lectura sin conexión</span>')
    if re.search(r'(?:src|href)="/img/', page):
        raise RuntimeError("Quedaron imágenes dependientes del servidor")
    if used != set(images):
        raise RuntimeError("El render HTML omitió imágenes referenciadas")
    if not self_contained:
        page = page.replace(
            "Documento autocontenido · lectura sin conexión",
            "Lectura sin conexión · conserva este HTML junto a la carpeta img")
    return page


def export_html(class_dir: Path) -> Path:
    """Guarda el HTML con enlaces relativos a img/, sin duplicar sus bytes."""
    from . import study_package

    d = Path(class_dir).resolve()
    page = render_html(d)
    target = d / "leccion_final.html"
    study_package.write_text_atomic(target, page)
    return target
