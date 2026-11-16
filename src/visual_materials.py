"""Material visual oficial para el postproceso de ClaseViva.

El módulo es deliberadamente derivado y opt-in: nunca modifica capturas, logs ni
manifests canónicos. Renderiza una copia limpia del material suministrado, empareja
apariciones por rasgos locales/OCR y registra su procedencia en un manifest separado.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from .image_optimization import encode_webp

SCHEMA_VERSION = 1
MANIFEST_NAME = "visual_material_manifest.json"
CONFIG_NAME = "materiales_oficiales.json"
LOCAL_OPT_IN_DIR = "contenido_descargado"
DECK_SUFFIXES = {".pdf", ".pptx"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
_TOKEN_STOP = {
    "about", "after", "antes", "como", "clase", "course", "desde", "esta",
    "este", "imagen", "para", "slide", "sobre", "that", "the", "this", "with",
}
PDF_RENDERER_REQUIREMENT = "pypdfium2==5.11.0"
PDF_RENDERER_INSTALL_WINDOWS = (
    r".\.venv\Scripts\python.exe -m pip install -r requirements.txt"
)
PPTX_RENDERER_HELP_WINDOWS = (
    "instala Microsoft PowerPoint Desktop (Office 16+) y vuelve a abrir ClaseViva; "
    "el archivo se abre read-only y las imágenes se derivan en un temporal"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", path.name)]


def _source_hash(path: Path) -> str:
    if path.is_file():
        return _sha256(path)
    digest = hashlib.sha256()
    images = sorted(
        (item for item in path.iterdir()
         if item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES),
        key=_natural_key,
    )
    for image in images:
        digest.update(image.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(image).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def resolver_materiales_oficiales(
        clase_dir: Path, explicit: Iterable[str | Path] | None = None) -> list[Path]:
    """Resuelve opt-in por CLI, entorno, contrato o carpeta local dedicada."""
    raw: list[tuple[str | Path, Path | None]] = [
        (item, None) for item in (explicit or []) if str(item).strip()
    ]
    env = os.environ.get("CLASE_POST_MATERIALES", "").strip()
    if env:
        raw.extend((item, None) for item in env.split(os.pathsep) if item.strip())

    config_path = clase_dir / CONFIG_NAME
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            items = data.get("materiales", data.get("materials", [])) \
                if isinstance(data, dict) else data
            for item in items if isinstance(items, list) else []:
                value = item.get("path") if isinstance(item, dict) else item
                if value:
                    raw.append((value, config_path.parent))
        except (OSError, ValueError, TypeError):
            # El contrato es opcional: un archivo inválido no rompe una clase legacy.
            pass

    # Colocar material dentro de esta carpeta de la clase es una acción explícita del
    # usuario. Solo se activa como fallback cuando CLI/env/config no seleccionaron otra
    # fuente; los TXT (incluido el transcript oficial) nunca se tratan como visuales.
    local_opt_in = clase_dir / LOCAL_OPT_IN_DIR
    if not raw and local_opt_in.is_dir():
        decks = sorted(
            (item for item in local_opt_in.iterdir()
             if item.is_file() and item.suffix.casefold() in DECK_SUFFIXES),
            key=_natural_key,
        )
        raw.extend((item, None) for item in decks)
        if any(
            item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES
            for item in local_opt_in.iterdir()
        ):
            raw.append((local_opt_in, None))

    result: list[Path] = []
    seen: set[str] = set()
    for value, base in raw:
        path = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if not path.is_absolute() and base is not None:
            path = base / path
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def _crop_outer_white(image: Image.Image) -> Image.Image:
    """Quita márgenes blancos grandes sin recortar slides legítimamente blancas."""
    rgb = image.convert("RGB")
    background = Image.new("RGB", rgb.size, "white")
    diff = ImageChops.difference(rgb, background).convert("L")
    # Ignora pequeñas variaciones de compresión/antialiasing del papel PDF.
    diff = diff.point(lambda value: 255 if value > 14 else 0)
    bbox = diff.getbbox()
    if not bbox:
        return rgb
    left, top, right, bottom = bbox
    content_w = right - left
    content_h = bottom - top
    if content_w < rgb.width * 0.55 or content_h < rgb.height * 0.45:
        return rgb
    margin_x = max(2, int(content_w * 0.006))
    margin_y = max(2, int(content_h * 0.006))
    bbox = (
        max(0, left - margin_x), max(0, top - margin_y),
        min(rgb.width, right + margin_x), min(rgb.height, bottom + margin_y),
    )
    return rgb.crop(bbox)


def _save_clean_page(image: Image.Image, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    image = ImageOps.exif_transpose(image).convert("RGB")
    if max(image.size) > 1800:
        image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
    tmp = target.with_name(target.stem + ".tmp" + target.suffix)
    tmp.write_bytes(encode_webp(image, prefer_lossless=True))
    os.replace(tmp, target)
    return _sha256(target)


def preflight_pdf_renderer():
    """Valida el renderer PDF opt-in antes de leer o derivar sus páginas."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - mensaje probado en frontera funcional
        raise RuntimeError(
            f"falta {PDF_RENDERER_REQUIREMENT} para procesar PDFs oficiales. "
            "En Windows instálalo únicamente en el entorno del proyecto con: "
            f"{PDF_RENDERER_INSTALL_WINDOWS}"
        ) from exc
    if not hasattr(pdfium, "PdfDocument"):
        raise RuntimeError(
            f"{PDF_RENDERER_REQUIREMENT} está incompleto o es incompatible; "
            "reinstala el entorno del proyecto con: "
            f"{PDF_RENDERER_INSTALL_WINDOWS}"
        )
    return pdfium


def _pdf_pages(path: Path):
    pdfium = preflight_pdf_renderer()
    document = pdfium.PdfDocument(str(path))
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                bitmap = page.render(scale=1.6)
                image = _crop_outer_white(bitmap.to_pil())
                text = ""
                try:
                    text_page = page.get_textpage()
                    try:
                        text = str(text_page.get_text_range() or "")
                    finally:
                        text_page.close()
                except Exception:
                    text = ""
                yield index + 1, image, text
            finally:
                page.close()
    finally:
        document.close()


def _image_pages(path: Path):
    if path.is_dir():
        sources = sorted(
            (item for item in path.iterdir()
             if item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES),
            key=_natural_key,
        )
    else:
        sources = [path]
    for index, source in enumerate(sources, 1):
        with Image.open(source) as image:
            yield index, image.convert("RGB"), ""


def preflight_pptx_renderer() -> dict:
    """Comprueba PowerPoint COM sin abrir el deck ni modificar Windows."""
    if os.name != "nt":
        raise RuntimeError(
            "el renderer PPTX validado actualmente requiere Windows y PowerPoint Desktop; "
            "convierte el deck a PDF o a un directorio de imágenes en este sistema")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft Office" / "root" / "Office16" / "POWERPNT.EXE",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft Office" / "root" / "Office16" / "POWERPNT.EXE",
    ]
    executable = next((path for path in candidates if path.is_file()), None)
    if powershell is None or executable is None:
        raise RuntimeError(
            "no está disponible el renderer PPTX de PowerPoint. En Windows, "
            f"{PPTX_RENDERER_HELP_WINDOWS}. Alternativa reproducible: exporta el PPTX "
            "a PDF y pásalo con --material")
    return {
        "renderer": "microsoft-powerpoint-com",
        "powershell": str(Path(powershell).resolve()),
        "executable": str(executable.resolve()),
    }


def _render_pptx(source: Path, output_dir: Path) -> dict:
    preflight = preflight_pptx_renderer()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = (
        "$ErrorActionPreference='Stop';"
        "$app=$null;$deck=$null;"
        "try {"
        "$app=New-Object -ComObject PowerPoint.Application;"
        "$deck=$app.Presentations.Open($env:CLASE_PPTX_INPUT,-1,0,0);"
        "$count=$deck.Slides.Count;"
        "$version=$app.Version;"
        "$deck.Export($env:CLASE_PPTX_OUTPUT,'PNG',1600,900);"
        "Write-Output ($count.ToString()+'|'+$version);"
        "} finally {"
        "if($deck){$deck.Close();[Runtime.InteropServices.Marshal]::FinalReleaseComObject($deck)|Out-Null};"
        "if($app){$app.Quit();[Runtime.InteropServices.Marshal]::FinalReleaseComObject($app)|Out-Null};"
        "[GC]::Collect();[GC]::WaitForPendingFinalizers();"
        "}"
    )
    environment = dict(os.environ)
    environment.update({
        "CLASE_PPTX_INPUT": str(source),
        "CLASE_PPTX_OUTPUT": str(output_dir),
    })
    completed = subprocess.run(
        [preflight["powershell"], "-NoLogo", "-NoProfile", "-NonInteractive",
         "-Command", command],
        capture_output=True, text=True, timeout=180, check=False, env=environment,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "sin detalle").strip()
        raise RuntimeError(
            "PowerPoint no pudo exportar el PPTX read-only: " + detail[:600])
    images = sorted(
        (path for path in output_dir.iterdir()
         if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES),
        key=_natural_key,
    )
    if not images:
        raise RuntimeError("PowerPoint terminó sin producir imágenes de slides")
    version = ""
    declared = len(images)
    match = re.search(r"(?m)^(\d+)\|([^\r\n]+)$", completed.stdout or "")
    if match:
        declared = int(match.group(1))
        version = match.group(2).strip()
    if declared != len(images):
        raise RuntimeError(
            f"PowerPoint declaró {declared} slides pero exportó {len(images)} imágenes")
    return {
        **preflight,
        "renderer": "microsoft-powerpoint-com",
        "renderer_version": version,
        "pages": len(images),
    }


def _pptx_slide_texts(path: Path) -> dict[int, str]:
    texts: dict[int, str] = {}
    with zipfile.ZipFile(path) as archive:
        names = [
            name for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        ]
        names.sort(key=lambda name: [
            int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", name)
        ])
        for name in names:
            number_match = re.search(r"slide(\d+)\.xml$", name)
            if number_match is None:
                continue
            try:
                xml = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                continue
            values = [
                str(node.text or "").strip() for node in xml.iter()
                if node.tag.endswith("}t") and str(node.text or "").strip()
            ]
            texts[int(number_match.group(1))] = " ".join(values)
    return texts


def _pptx_pages(path: Path):
    texts = _pptx_slide_texts(path)
    with tempfile.TemporaryDirectory(prefix="clase-viva-pptx-") as temp:
        output = Path(temp)
        renderer = _render_pptx(path, output)
        images = sorted(
            (item for item in output.iterdir()
             if item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES),
            key=_natural_key,
        )
        for index, source in enumerate(images, 1):
            with Image.open(source) as image:
                yield index, image.convert("RGB"), texts.get(index, ""), renderer


def preparar_materiales_oficiales(
        materials: Iterable[str | Path], img_dir: Path, *,
        manifest_path: Path | None = None,
        reset_appearances: bool = False) -> tuple[list[dict], dict]:
    """Materializa copias limpias y un manifest derivado, sin tocar los originales."""
    img_dir = Path(img_dir)
    pages: list[dict] = []
    report = {
        "materiales_solicitados": 0,
        "materiales_validos": 0,
        "paginas_preparadas": 0,
        "errores": [],
    }
    previous_appearances: dict[str, list[dict]] = {}
    if manifest_path is not None and Path(manifest_path).is_file() \
            and not reset_appearances:
        try:
            previous = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            previous_appearances = {
                str(page.get("material_id", "")): list(page.get("apariciones", []))
                for page in previous.get("paginas", []) if isinstance(page, dict)
            }
        except (OSError, ValueError, TypeError, AttributeError):
            previous_appearances = {}
    for raw in materials:
        report["materiales_solicitados"] += 1
        source = Path(raw).expanduser()
        try:
            source = source.resolve()
        except OSError:
            source = source.absolute()
        if not source.exists():
            report["errores"].append(f"no existe: {source}")
            continue
        suffix = source.suffix.casefold() if source.is_file() else ""
        kind = "pdf" if suffix == ".pdf" else "pptx" if suffix == ".pptx" \
            else "imagenes"
        if kind == "imagenes" and source.is_file() \
                and source.suffix.casefold() not in IMAGE_SUFFIXES:
            report["errores"].append(f"formato no soportado: {source}")
            continue
        if kind == "imagenes" and source.is_dir() and not any(
                item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES
                for item in source.iterdir()):
            report["errores"].append(f"directorio sin imágenes: {source}")
            continue
        try:
            if kind == "pdf":
                preflight_pdf_renderer()
            source_sha = _source_hash(source)
            iterator = (
                _pdf_pages(source) if kind == "pdf"
                else _pptx_pages(source) if kind == "pptx"
                else _image_pages(source)
            )
            material_pages = []
            for item in iterator:
                page_number, image, text = item[:3]
                renderer = item[3] if len(item) > 3 else {}
                filename = (
                    f"material_oficial_{source_sha[:12]}_p{page_number:04d}.webp"
                )
                image_sha = _save_clean_page(image, img_dir / filename)
                material_id = f"{source_sha}:p{page_number:04d}"
                material_pages.append({
                    "material_id": material_id,
                    "tipo_fuente": kind,
                    "fuente": str(source),
                    "fuente_nombre": source.name,
                    "fuente_sha256": source_sha,
                    "pagina": page_number,
                    "archivo": filename,
                    "archivo_sha256": image_sha,
                    "texto": text.strip(),
                    "renderizador": str(renderer.get("renderer", "pypdfium2"
                        if kind == "pdf" else "pillow")),
                    "renderizador_version": str(
                        renderer.get("renderer_version", "")),
                    "apariciones": previous_appearances.get(material_id, []),
                })
            pages.extend(material_pages)
            report["materiales_validos"] += 1
        except Exception as exc:
            report["errores"].append(f"{source}: {type(exc).__name__}: {exc}")

    report["paginas_preparadas"] = len(pages)
    if manifest_path is not None and pages:
        _atomic_json(Path(manifest_path), {
            "schema_version": SCHEMA_VERSION,
            "tipo": "material_visual_derivado",
            "paginas": pages,
            "reporte_preparacion": report,
        })
    return pages, report


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9áéíóúüñ]+", " ", text.casefold())
    return {token for token in normalized.split()
            if len(token) >= 4 and token not in _TOKEN_STOP}


def _text_score(capture_text: str, page_text: str) -> tuple[float, int]:
    evidence = _text_evidence(capture_text, page_text)
    return evidence["overlap_ratio"], evidence["common"]


def _text_evidence(capture_text: str, page_text: str) -> dict:
    left, right = _tokens(capture_text), _tokens(page_text)
    if not left or not right:
        return {
            "overlap_ratio": 0.0,
            "common": 0,
            "capture_tokens": len(left),
            "page_tokens": len(right),
            "capture_precision": 0.0,
            "page_coverage": 0.0,
        }
    common = len(left & right)
    return {
        "overlap_ratio": common / max(1, min(len(left), len(right))),
        "common": common,
        "capture_tokens": len(left),
        "page_tokens": len(right),
        "capture_precision": common / len(left),
        "page_coverage": common / len(right),
    }


def _looks_like_external_ui(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    presentation_library = (
        any(term in normalized for term in (
            "presentaciones recientes", "recent presentations",
        ))
        and any(term in normalized for term in (
            "propietario", "owner",
        ))
    )
    drive_library = (
        any(term in normalized for term in ("mi unidad", "my drive"))
        and any(term in normalized for term in ("propietario", "owner"))
        and any(term in normalized for term in (
            "última modificación", "ultima modificacion", "last modified",
        ))
    )
    if presentation_library or drive_library:
        return True
    strong = (
        "http://", "https://", ".com/", "all bookmarks", "progress tab",
        "course catalog", "shop drivers support", "find training",
        "self paced courses", "instructor-led workshops", "products solutions",
        "unirse con google meet", "proponer otro horario", "responder a todos",
        "agregar estudiantes", "invitados", "profesores estudiantes",
    )
    return sum(term in normalized for term in strong) >= 2


def _fuzzy_search_engine_token(token: str) -> bool:
    """Tolera deformaciones OCR breves de nombres de buscadores conocidos."""
    compact = re.sub(r"[^a-z0-9]", "", str(token or "").casefold())
    if compact in {"cegle", "ceogle", "geogle", "gogle", "gooqle"}:
        return True
    if not 4 <= len(compact) <= 10:
        return False
    return any(
        difflib.SequenceMatcher(None, compact, engine).ratio() >= 0.64
        for engine in ("google", "bing", "duckduckgo")
    )


def _has_search_engine_signal(text: str) -> bool:
    raw_tokens = re.findall(r"[A-Za-z0-9]+", str(text or ""))
    if any(_fuzzy_search_engine_token(token) for token in raw_tokens):
        return True
    # OCR como ``CE)gle`` puede partir la marca en dos tokens cortos.
    for index in range(max(0, len(raw_tokens) - 1)):
        joined = raw_tokens[index] + raw_tokens[index + 1]
        if _fuzzy_search_engine_token(joined):
            return True
    normalized = " ".join(str(text or "").casefold().split())
    return any(engine in normalized for engine in (
        "google", "geogle", "gogle", "ceogle", "cegle", "bing", "duckduckgo",
    ))


def _extract_browsing_query(text: str) -> str:
    """Recupera la consulta visible sin copiar resultados, anuncios o paneles laterales."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return ""
    search_engine = _has_search_engine_signal(raw)
    marker = re.search(
        r"\b(?:resultados(?:\s+de\s+b[uú]squeda|\s+patrocinados)?|"
        r"search results|sponsored results|visi[oó]n general(?: creada por ia)?|"
        r"ai overview|modo ia|images|im[aá]genes|noticias|news)\b",
        raw,
        flags=re.I,
    )
    # En capturas recortadas o tras un cambio sutil el OCR puede perder el logo del
    # buscador, aunque conserve la consulta seguida por un panel inequívoco de
    # resultados. Solo se recupera en el prefijo corto anterior a ese panel.
    marker_without_engine = bool(
        marker
        and 2 <= marker.start() <= 120
        and re.search(
            r"resultados(?:\s+de\s+b[uú]squeda|\s+patrocinados)|"
            r"search results|sponsored results|"
            r"visi[oó]n general(?: creada por ia)?|ai overview",
            marker.group(0),
            flags=re.I,
        )
    )
    if not search_engine and not marker_without_engine:
        return ""
    leading = re.findall(r"[A-Za-z0-9]+", raw[:80])[:4]
    leading_engine = any(
        _fuzzy_search_engine_token(token) for token in leading)
    leading_engine = leading_engine or any(
        _fuzzy_search_engine_token(leading[index] + leading[index + 1])
        for index in range(max(0, len(leading) - 1))
    )
    if not leading_engine and not marker_without_engine:
        return ""
    prefix = raw[:marker.start()] if marker else raw[:180]
    tokens = re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9_.+-]*", prefix)
    while tokens and (
            _fuzzy_search_engine_token(tokens[0])
            or len(tokens[0]) <= 3
            or tokens[0].casefold() in {
                "search", "buscar", "todo", "all", "web",
            }):
        tokens.pop(0)
    navigation = {
        "all", "herramientas", "images", "imagenes", "libros", "news",
        "noticias", "shopping", "todo", "videos", "web",
    }

    def navigation_token(token: str, next_token: str) -> bool:
        normalized = "".join(
            char for char in unicodedata.normalize("NFKD", token.casefold())
            if not unicodedata.combining(char)
        ).translate(str.maketrans({"0": "o", "1": "i"}))
        following = next_token.casefold()
        if normalized in navigation:
            return True
        if 4 <= len(normalized) <= 12 and any(
            difflib.SequenceMatcher(None, normalized, expected).ratio() >= 0.62
            for expected in navigation
        ):
            return True
        return bool(
            following == "ia"
            and (
                normalized.endswith("do")
                or difflib.SequenceMatcher(
                    None, normalized, "modo").ratio() >= 0.55
            )
        )

    for index, token in enumerate(tokens):
        next_token = tokens[index + 1].casefold() if index + 1 < len(tokens) else ""
        if navigation_token(token, next_token):
            tokens = tokens[:index]
            break
    if not tokens:
        return ""
    if len(tokens) > 8:
        return ""
    query = " ".join(tokens).strip(" -:|")
    if len(query) < 3:
        return ""
    return query[:160]


def _looks_like_browsing_reference(text: str) -> bool:
    """Reconoce navegación web de apoyo sin confundirla con una lámina docente."""
    normalized = " ".join(str(text or "").casefold().split())
    application_chrome = sum(term in normalized for term in (
        ".ipynb", "archivo", "editar", "insertar", "entorno de ejecución",
        "entorno de ejecucion", "comandos", "ejecutar todas", "copiar en drive",
        "+ código", "+ codigo",
    ))
    if (
        application_chrome >= 4
        and any(term in normalized for term in (
            ".ipynb", "entorno de ejecución", "entorno de ejecucion",
            "ejecutar todas", "copiar en drive",
        ))
    ):
        return False
    direct = (
        "resultados de búsqueda", "resultados de busqueda", "búsquedas relacionadas",
        "busquedas relacionadas", "search results", "people also ask",
        "resultados patrocinados", "sponsored results",
    )
    if any(term in normalized for term in direct):
        return True
    search_engine = _has_search_engine_signal(text)
    search_navigation = sum(term in normalized for term in (
        "modo ia", "imágenes", "imagenes", "noticias", "videos cortos",
        "shopping", "web images", "all news videos",
        "visión general creada por ia", "vision general creada por ia",
        "ai overview", "mostrar todo", "show all", "sitios", "sources",
    ))
    if search_engine and search_navigation >= 2:
        return True
    wikipedia_article = "wikipedia" in normalized and any(
        term in normalized for term in (
            "article", "artículo", "articulo", "contents", "read edit",
            "referencias", "references", "encyclopedia",
        )
    )
    article_navigation = sum(term in normalized for term in (
        "contents hide", "contents (top)", "article talk", "read edit",
        "edit history", "appearance text", "references notes citations",
        "bibliography external links", "from wikipedia", "free encyclopedia",
    ))
    answer_panel = sum(term in normalized for term in (
        "haz una pregunta", "ask a follow-up", "sitios", "sources",
        "mostrar todo", "show all",
    ))
    return bool(
        wikipedia_article
        or article_navigation >= 2
        or answer_panel >= 2
    )


_MEDIA_UI_TOKENS = {
    "youtube", "vimeo", "suscribirse", "suscribete", "subscribe", "subscribers",
    "suscriptores", "compartir", "share", "guardar", "save", "views", "vistas",
    "crear", "create", "recomendado", "recommended", "playlist", "video",
}


def _looks_like_media_reference(text: str) -> bool:
    """Detecta una página o reproducción audiovisual usada como material docente."""
    normalized = " ".join(str(text or "").casefold().split())
    platform = any(term in normalized for term in (
        "youtube", "youtu.be", "vimeo", "loom", "dailymotion",
    ))
    controls = sum(term in normalized for term in (
        "suscrib", "subscribe", "compartir", "share", "guardar", "save",
        "vistas", "views", "reproducir", "play", "pausa", "pause",
    ))
    content = sum(term in normalized for term in (
        "demo", "demostraci", "lecture", "conferencia", "webinar", "tutorial",
        "documentary", "documental", "experiment", "experimento",
    ))
    return bool(platform and (controls >= 2 or (content >= 1 and controls >= 1)))


def _media_phase(slide: dict) -> str:
    text = _browsing_evidence_text(slide)
    normalized = " ".join(text.casefold().split())
    descriptive = any(term in normalized for term in (
        "this is a demo", "esta es una demostración", "esta es una demostracion",
        "it was developed", "fue desarrollado", "description", "descripción",
        "descripcion",
    ))
    dense_page = len(_tokens(text)) >= 34 and sum(
        term in normalized for term in (
            "suscrib", "subscribe", "vistas", "views", "compartir", "guardar",
        )
    ) >= 2
    return "ficha" if descriptive or dense_page else "reproduccion"


def _media_title(text: str) -> str:
    """Extrae una identidad corta del área principal del reproductor."""
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    compact = re.sub(
        r"^\W*[A-Za-z]?\s*(?:youtube|vimeo|loom)[\"']?\s+",
        "",
        compact,
        flags=re.I,
    )
    match = re.search(
        r"(?P<title>[A-Za-zÀ-ÿ0-9][^|]{3,120}?\b(?:19|20)\d{2})\b",
        compact,
    )
    if not match:
        return ""
    title = match.group("title").strip(" -–—:|·")
    # Evita que controles o consultas breves formen parte del título.
    title = re.sub(
        r"^(?:[A-Za-zÀ-ÿ0-9_.+-]+\s+){0,3}(?:x|q)\s+", "",
        title,
        flags=re.I,
    ).strip()
    return title[:140] if len(_tokens(title)) >= 3 else ""


def _media_topic_tokens(slide: dict) -> set[str]:
    title = str(slide.get("media_title", "") or "")
    source = title or _browsing_evidence_text(slide)
    return {
        token for token in _tokens(source)
        if token not in _MEDIA_UI_TOKENS
    }


def _same_media_topic(previous: dict, current: dict) -> bool:
    left = _media_topic_tokens(previous)
    right = _media_topic_tokens(current)
    common = left & right
    containment = len(common) / max(1, min(len(left), len(right)))
    years_left = set(re.findall(
        r"\b(?:19|20)\d{2}\b", _browsing_evidence_text(previous)))
    years_right = set(re.findall(
        r"\b(?:19|20)\d{2}\b", _browsing_evidence_text(current)))
    return bool(
        len(common) >= 3 and containment >= 0.30
        or (
            years_left & years_right
            and len(common) >= 2
            and containment >= 0.20
        )
    )


def _in_media_session(slide: dict, direct_references: list[dict]) -> bool:
    """Propaga una reproducción a frames cuyo OCR perdió el nombre de la plataforma."""
    current_time = float(
        slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0)
    return any(
        abs(current_time - float(
            seed.get("t_rel", seed.get("t_inicio_rel", 0)) or 0)) <= 240
        and _same_media_topic(seed, slide)
        for seed in direct_references
    )


def _looks_technical_content(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    patterns = (
        r"\b(import|def|class|return|print|pip install|conda|jupyter)\b",
        r"\bfrom\s+[a-z_][a-z0-9_.]*\s+import\b",
        r"\b(tensorflow|keras|pytorch|torch|numpy|pandas|matplotlib)\b",
        r"\b(model\.fit|optimizer|activation|epoch|batch_size|learning_rate)\b",
        r"\b(loss function|gradient descent|backpropagation|matrix|tensor)\b",
        r"\b[a-z_][a-z0-9_]*\s*(?:==|<=|>=|=)\s*(?:[-+]?\d|[a-z_])",
        r"\[(?:\s*[-+]?(?:\d+(?:\.\d+)?|[a-z_]\w*)\s*,){1,}",
        r"\b(?:mse|mae|relu|softmax|sigmoid|argmax)\s*\(",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _technical_atoms(text: str) -> set[str]:
    """Identidades compactas de fórmulas; ignora relojes y números de interfaz."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    atoms = re.findall(
        r"\b[a-z_][a-z0-9_.]{0,30}\s*"
        r"(?:==|<=|>=|=|→|->)\s*[-+]?\d+(?:[.,]\d+)?\b",
        normalized,
    )
    return {re.sub(r"\s+", "", atom).replace(",", ".") for atom in atoms}


def _replaces_technical_value(
        previous_atoms: set[str], current_atoms: set[str]) -> bool:
    """Distingue una asignación actualizada de átomos adicionales recuperados por OCR."""
    def values_by_left(atoms: set[str]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for atom in atoms:
            match = re.match(r"^(.+?)(==|<=|>=|=|→|->)(.+)$", atom)
            if not match:
                continue
            key = f"{match.group(1)}{match.group(2)}"
            result.setdefault(key, set()).add(match.group(3))
        return result

    previous = values_by_left(previous_atoms)
    current = values_by_left(current_atoms)
    return any(
        previous[key] != current[key]
        and not previous[key].issubset(current[key])
        for key in previous.keys() & current.keys()
    )


@lru_cache(maxsize=256)
def _cv_features(path: Path):
    try:
        import cv2
    except ImportError:
        return None
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    height, width = image.shape[:2]
    scale = min(1.0, 1100.0 / max(height, width))
    if scale < 1.0:
        image = cv2.resize(
            image, (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    detector = cv2.SIFT_create(nfeatures=1200, contrastThreshold=0.02)
    keypoints, descriptors = detector.detectAndCompute(image, None)
    if descriptors is None or len(keypoints) < 8:
        return None
    points = [kp.pt for kp in keypoints]
    return points, descriptors, (height * scale, width * scale)


def _visual_score(left, right) -> tuple[int, int, float, float]:
    if left is None or right is None:
        return 0, 0, 0.0, 0.0
    try:
        import cv2
        import numpy as np
        cv2.setRNGSeed(0)
        left_points, left_desc, left_shape = left
        right_points, right_desc, right_shape = right
        matcher = cv2.FlannBasedMatcher(
            {"algorithm": 1, "trees": 4}, {"checks": 36})
        matches = matcher.knnMatch(left_desc, right_desc, k=2)
        good = [pair[0] for pair in matches
                if len(pair) == 2 and pair[0].distance < 0.70 * pair[1].distance]
        if len(good) < 8:
            return len(good), 0, 0.0, 0.0
        source = np.float32([left_points[item.queryIdx] for item in good]).reshape(-1, 1, 2)
        target = np.float32([right_points[item.trainIdx] for item in good]).reshape(-1, 1, 2)
        _, mask = cv2.findHomography(source, target, cv2.RANSAC, 5.0)
        inliers = int(mask.ravel().sum()) if mask is not None else 0
        if mask is None or inliers < 4:
            return len(good), inliers, 0.0, 0.0
        selected = mask.ravel().astype(bool)

        def coverage(points, shape) -> float:
            chosen = points.reshape(-1, 2)[selected]
            if len(chosen) < 4:
                return 0.0
            span = np.maximum(chosen.max(axis=0) - chosen.min(axis=0), 0)
            height, width = shape
            return float((span[0] * span[1]) / max(1.0, width * height))

        capture_coverage = coverage(source, left_shape)
        page_coverage = coverage(target, right_shape)
        return len(good), inliers, capture_coverage, page_coverage
    except Exception:
        return 0, 0, 0.0, 0.0


def _build_feature_index(page_features: dict):
    try:
        import cv2
        import numpy as np
        cv2.setRNGSeed(0)
        descriptors = []
        page_ids = []
        for page_id, feature in page_features.items():
            if feature is None:
                continue
            desc = feature[1]
            descriptors.append(desc)
            page_ids.extend([page_id] * len(desc))
        if not descriptors:
            return None
        combined = np.vstack(descriptors).astype("float32", copy=False)
        matcher = cv2.FlannBasedMatcher(
            {"algorithm": 1, "trees": 5}, {"checks": 48})
        matcher.add([combined])
        matcher.train()
        return matcher, page_ids
    except Exception:
        return None


def _candidate_page_ids(capture_features, feature_index, limit: int = 12) -> list[str]:
    if capture_features is None or feature_index is None:
        return []
    try:
        matcher, page_ids = feature_index
        matches = matcher.knnMatch(capture_features[1], k=2)
        counts: dict[str, int] = {}
        for pair in matches:
            if len(pair) != 2 or pair[0].distance >= 0.74 * pair[1].distance:
                continue
            page_id = str(page_ids[pair[0].trainIdx])
            counts[page_id] = counts.get(page_id, 0) + 1
        return [page_id for page_id, _ in sorted(
            counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]
    except Exception:
        return []


def _best_match(capture: Path, ocr: str, pages: list[dict], page_features: dict,
                feature_index=None):
    capture_features = _cv_features(capture)
    candidate_ids = set(_candidate_page_ids(capture_features, feature_index))
    text_ranked = sorted(
        ((_text_score(ocr, str(page.get("texto", "")))[0],
          str(page["material_id"])) for page in pages),
        reverse=True,
    )
    candidate_ids.update(page_id for ratio, page_id in text_ranked[:3] if ratio >= 0.35)
    candidate_pages = [
        page for page in pages if str(page["material_id"]) in candidate_ids
    ] if candidate_ids else pages
    candidates = []
    for page in candidate_pages:
        page_id = str(page["material_id"])
        good, inliers, capture_coverage, page_coverage = _visual_score(
            capture_features, page_features.get(page_id))
        text_evidence = _text_evidence(ocr, str(page.get("texto", "")))
        text_ratio = float(text_evidence["overlap_ratio"])
        common = int(text_evidence["common"])
        score = (inliers * 2.0 + min(good, 120) * 0.10
                 + min(capture_coverage, 0.5) * 24.0
                 + min(page_coverage, 0.8) * 18.0 + text_ratio * 16.0)
        candidates.append((
            score, inliers, good, capture_coverage, page_coverage,
            text_ratio, common, text_evidence, page,
        ))
    if not candidates:
        return None, len(candidate_pages)
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    (score, inliers, good, capture_coverage, page_coverage,
     text_ratio, common, text_evidence, page) = candidates[0]
    second_score = candidates[1][0] if len(candidates) > 1 else 0.0
    score_margin = score - second_score
    visual_available = capture_features is not None and any(
        feature is not None for feature in page_features.values())
    visual_base = (
        inliers >= 20 and good >= 30
        and capture_coverage >= 0.075 and page_coverage >= 0.10
    )
    capture_tokens = _tokens(ocr)
    page_tokens = _tokens(str(page.get("texto", "")))
    capture_text_precision = float(text_evidence["capture_precision"])
    page_text_coverage = float(text_evidence["page_coverage"])
    text_contradiction = (
        len(capture_tokens) >= 6 and len(page_tokens) >= 1
        and (
            common == 0
            or (
                len(page_tokens) >= 3
                and common <= 2
                and page_text_coverage <= 0.50
                and capture_text_precision < 0.10
            )
        )
    )
    spatial_identity = (
        visual_base
        and capture_coverage >= 0.30
        and page_coverage >= 0.30
        and score_margin >= 18.0
    )
    semantic_identity = (
        visual_base
        and common >= 4
        and page_text_coverage >= 0.65
        and capture_text_precision >= 0.18
        and score_margin >= 8.0
    )
    short_exact_identity = (
        visual_base
        and 1 <= len(page_tokens) <= 3
        and capture_tokens == page_tokens
        and score_margin >= 18.0
    )
    external_ui_conflict = (
        _looks_like_external_ui(ocr)
        and not semantic_identity
        and not spatial_identity
    )
    visual_ok = (
        not text_contradiction
        and not external_ui_conflict
        and (spatial_identity or semantic_identity or short_exact_identity)
    )
    # Con backend visual disponible, OCR solo refuerza una geometría incipiente;
    # no puede sustituirla y emparejar por vocabulario genérico de la plantilla.
    text_ok = (
        not text_contradiction
        and not external_ui_conflict
        and common >= 6
        and page_text_coverage >= 0.72
        and capture_text_precision >= 0.20
        and score_margin >= 8.0
        and (not visual_available or (inliers >= 8 and good >= 14))
    )
    if not visual_ok and not text_ok:
        return None, len(candidate_pages)
    if spatial_identity:
        gate = "geometria_distribuida"
    elif semantic_identity or short_exact_identity:
        gate = "geometria_mas_ocr"
    else:
        gate = "ocr_mas_geometria"
    return {
        "page": page,
        "score": round(score, 3),
        "inliers": inliers,
        "good_matches": good,
        "capture_coverage": round(capture_coverage, 4),
        "page_coverage": round(page_coverage, 4),
        "text_ratio": round(text_ratio, 4),
        "capture_text_precision": round(capture_text_precision, 4),
        "page_text_coverage": round(page_text_coverage, 4),
        "score_margin": round(score_margin, 3),
        "candidatos_evaluados": len(candidate_pages),
        "metodo": "sift" if visual_ok else "ocr",
        "gate": gate,
    }, len(candidate_pages)


def _appearance_items(slide: dict, fallback_name: str) -> list[dict]:
    result: list[dict] = []
    existing = slide.get("evidencias_aparicion") or []
    for item in existing:
        if isinstance(item, dict):
            name = str(item.get("archivo", "")).strip()
            if name:
                result.append(dict(item, archivo=name))
        elif str(item).strip():
            result.append({"archivo": str(item).strip()})
    if not result and fallback_name:
        result.append({
            "archivo": fallback_name,
            "t_rel": slide.get("t_rel"),
            "t_abs": slide.get("t_abs"),
        })
    seen: set[tuple[str, object]] = set()
    unique = []
    for item in result:
        key = (str(item.get("archivo", "")), item.get("t_rel"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _merge_text(left: str, right: str) -> str:
    lines = []
    seen = set()
    for line in (str(left or "") + "\n" + str(right or "")).splitlines():
        normalized = " ".join(line.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            lines.append(line.strip())
    return "\n".join(lines)


def _merge_official_state(base: dict, other: dict) -> None:
    appearances = _appearance_items(base, str(base.get("archivo_evidencia", "")))
    appearances.extend(_appearance_items(other, str(other.get("archivo_evidencia", ""))))
    base["evidencias_aparicion"] = _appearance_items(
        {"evidencias_aparicion": appearances}, "")
    base["intervalos_visuales"] = list(base.get("intervalos_visuales") or []) + list(
        other.get("intervalos_visuales") or [])
    base["t_inicio_rel"] = min(
        float(base.get("t_inicio_rel", base.get("t_rel", 0)) or 0),
        float(other.get("t_inicio_rel", other.get("t_rel", 0)) or 0),
    )
    base["t_fin_rel"] = max(
        float(base.get("t_fin_rel", base.get("t_rel", 0)) or 0),
        float(other.get("t_fin_rel", other.get("t_rel", 0)) or 0),
    )
    base["grupo_total"] = int(base.get("grupo_total", 1)) + int(
        other.get("grupo_total", 1))
    base["capturas_colapsadas"] = int(base.get("capturas_colapsadas", 1)) + int(
        other.get("capturas_colapsadas", 1))
    base["duplicados_material"] = int(base.get("duplicados_material", 0)) + 1 + int(
        other.get("duplicados_material", 0))
    base["transcript_ventana"] = _merge_text(
        str(base.get("transcript_ventana", "")),
        str(other.get("transcript_ventana", "")),
    )
    if len(str(other.get("ocr", ""))) > len(str(base.get("ocr", ""))):
        base["ocr"] = other.get("ocr", "")
    base["score"] = max(float(base.get("score", 0) or 0),
                        float(other.get("score", 0) or 0))
    base["duracion_visual_seg"] = round(sum(
        max(0.0, float(item.get("fin", 0) or 0) - float(item.get("inicio", 0) or 0))
        for item in base["intervalos_visuales"]
    ), 2)
    base["visual_estable"] = bool(
        base.get("visual_estable") or other.get("visual_estable"))


def _dhash(path: Path) -> int | None:
    try:
        with Image.open(path) as image:
            gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(
                gray.get_flattened_data() if hasattr(gray, "get_flattened_data")
                else gray.getdata()
            )
        value = 0
        for row in range(8):
            for col in range(8):
                value = (value << 1) | int(
                    pixels[row * 9 + col] > pixels[row * 9 + col + 1])
        return value
    except OSError:
        return None


@lru_cache(maxsize=4096)
def _structural_features_cached(
        path_text: str, size: int, mtime_ns: int) -> tuple[bytes, int]:
    del size, mtime_ns  # forman parte de la clave e invalidan el cache en memoria
    with Image.open(path_text) as source:
        gray = source.convert("L")
        edge_source = gray.resize((160, 90), Image.Resampling.LANCZOS)
        hash_source = gray.resize((9, 8), Image.Resampling.LANCZOS)
    edges = edge_source.filter(ImageFilter.FIND_EDGES).tobytes()
    pixels = list(
        hash_source.get_flattened_data()
        if hasattr(hash_source, "get_flattened_data") else hash_source.getdata()
    )
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | int(
                pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return edges, value


def _structural_features(path: Path) -> tuple[bytes, int]:
    stat = path.stat()
    return _structural_features_cached(
        str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns))


def _structural_delta(previous: Path, current: Path) -> dict:
    """Mide cambio de estructura por celdas, no solo diferencia global de píxel."""
    try:
        left_bytes, left_hash = _structural_features(previous)
        right_bytes, right_hash = _structural_features(current)
        left_edges = Image.frombytes("L", (160, 90), left_bytes)
        right_edges = Image.frombytes("L", (160, 90), right_bytes)
        difference = ImageChops.difference(left_edges, right_edges)
        edge_delta = float(ImageStat.Stat(difference).mean[0])
        cell_deltas = []
        for row in range(3):
            for col in range(4):
                box = (col * 40, row * 30, (col + 1) * 40, (row + 1) * 30)
                cell_deltas.append(float(
                    ImageStat.Stat(difference.crop(box)).mean[0]))
        active_cells = sum(value >= 14.0 for value in cell_deltas)
        hash_distance = (left_hash ^ right_hash).bit_count()
        return {
            "edge_delta": round(edge_delta, 4),
            "active_cells": active_cells,
            "dhash_distance": hash_distance,
        }
    except OSError:
        return {
            "edge_delta": 0.0,
            "active_cells": 0,
            "dhash_distance": None,
        }


def _pedagogical_delta(
        img_dir: Path, previous: dict, current: dict) -> dict:
    previous_tokens = _tokens(str(previous.get("ocr", "")))
    current_tokens = _tokens(str(current.get("ocr", "")))
    common = len(previous_tokens & current_tokens)
    union = len(previous_tokens | current_tokens)
    added = current_tokens - previous_tokens
    transcript_tokens = _tokens(str(current.get("transcript_ventana", "")))
    corroborated_added = added & transcript_tokens
    ocr_similarity = common / max(1, union)
    ocr_novelty = len(added) / max(1, len(current_tokens))
    structural = _structural_delta(
        Path(img_dir) / str(previous.get("archivo", "")),
        Path(img_dir) / str(current.get("archivo", "")),
    )
    previous_atoms = _technical_atoms(str(previous.get("ocr", "")))
    current_atoms = _technical_atoms(str(current.get("ocr", "")))
    new_atoms = current_atoms - previous_atoms
    replaced_assignment = _replaces_technical_value(
        previous_atoms, current_atoms)
    formula_change_supported = bool(
        new_atoms
        and (
            replaced_assignment
            or bool(current.get("visual_estable"))
            or len(corroborated_added) >= 2
            or (
                structural["edge_delta"] >= 6.0
                and structural["active_cells"] >= 2
                and structural["dhash_distance"] is not None
                and structural["dhash_distance"] >= 4
            )
        )
    )
    formula_progress = bool(
        previous_atoms and current_atoms and formula_change_supported
        and common >= 3 and ocr_similarity >= 0.45
        and structural["dhash_distance"] is not None
        and structural["dhash_distance"] <= 18
        and structural["active_cells"] <= 6
    )
    same_ocr = (
        common >= 4
        and ocr_similarity >= 0.70
        and not (
            len(added) >= 2
            and ocr_novelty >= 0.08
            and len(corroborated_added) >= 2
        )
    )
    sparse_ocr = min(len(previous_tokens), len(current_tokens)) < 4
    same_sparse_visual = (
        sparse_ocr
        and structural["dhash_distance"] is not None
        and structural["dhash_distance"] <= 8
        and structural["active_cells"] <= 5
    )
    text_progress = (
        len(added) >= 2
        and ocr_novelty >= 0.08
        and len(corroborated_added) >= 2
        and structural["edge_delta"] >= 4.0
    )
    # Un cambio grande de píxeles por sí solo también describe vídeo, cursor o
    # presentador. Solo cuenta como build espacial cuando el nuevo estado fue
    # estable; el progreso textual corroborado sigue pudiendo conservar un
    # cambio breve de código, fórmula o lista.
    spatial_progress = (
        structural["edge_delta"] >= 16.0
        and structural["active_cells"] >= 6
        and bool(current.get("visual_estable"))
    )
    return {
        **structural,
        "ocr_similarity": round(ocr_similarity, 4),
        "ocr_tokens_nuevos": len(added),
        "ocr_tokens_nuevos_corroborados": len(corroborated_added),
        "ocr_novedad_ratio": round(ocr_novelty, 4),
        "formulas_nuevas": len(new_atoms),
        "valor_formula_reemplazado": replaced_assignment,
        "cambio_formula_confirmado": formula_change_supported,
        "mismo_estado": bool((same_ocr or same_sparse_visual) and not formula_progress),
        "progreso": bool(text_progress or spatial_progress or formula_progress),
    }


def _same_semantic_state(previous: dict, current: dict) -> bool:
    """Detecta la misma lámina aunque cambien rostro, cursor u OCR menor."""
    previous_tokens = _tokens(str(previous.get("ocr", "")))
    current_tokens = _tokens(str(current.get("ocr", "")))
    if not previous_tokens or not current_tokens:
        return False
    common = previous_tokens & current_tokens
    similarity = len(common) / max(1, len(previous_tokens | current_tokens))
    added = current_tokens - previous_tokens
    transcript_tokens = _tokens(str(current.get("transcript_ventana", "")))
    previous_atoms = _technical_atoms(str(previous.get("ocr", "")))
    current_atoms = _technical_atoms(str(current.get("ocr", "")))
    new_atoms = current_atoms - previous_atoms
    transcript_atoms = _technical_atoms(str(current.get("transcript_ventana", "")))
    if new_atoms and (
            _replaces_technical_value(previous_atoms, current_atoms)
            or bool(current.get("visual_estable"))
            or bool(new_atoms & transcript_atoms)):
        return False
    pedagogical_addition = (
        len(added) >= 2
        and len(added & transcript_tokens) >= 2
        and len(added) / max(1, len(current_tokens)) >= 0.08
    )
    return len(common) >= 4 and similarity >= 0.66 and not pedagogical_addition


def _prefer_current_representative(previous: dict, current: dict) -> bool:
    """Dentro de una identidad elige el frame con texto más completo/legible."""
    previous_text = str(previous.get("ocr", "") or "").strip()
    current_text = str(current.get("ocr", "") or "").strip()
    previous_tokens = _tokens(previous_text)
    current_tokens = _tokens(current_text)
    if current_text and not previous_text:
        return True
    if len(current_tokens) >= len(previous_tokens) + 4:
        return True
    if (
        len(current_text) >= max(80, int(len(previous_text) * 1.18))
        and len(current_tokens) >= len(previous_tokens)
    ):
        return True
    previous_time = float(
        previous.get("t_rel", previous.get("t_inicio_rel", 0)) or 0)
    current_time = float(
        current.get("t_rel", current.get("t_inicio_rel", 0)) or 0)
    return bool(
        previous_text
        and current_text
        and len(current_tokens) >= len(previous_tokens)
        and len(current_text) >= int(len(previous_text) * 0.98)
        and current_time > previous_time
    )


def _ocr_corroborated_by_transcript(slide: dict) -> bool:
    """Exige señal compartida antes de elevar una captura genérica al cuerpo."""
    ocr_tokens = _tokens(str(slide.get("ocr", "") or ""))
    transcript_tokens = _tokens(str(slide.get("transcript_ventana", "") or ""))
    common = ocr_tokens & transcript_tokens
    return len(common) >= min(3, max(2, len(ocr_tokens) // 6))


def _same_gallery_state(img_dir: Path, previous: dict, current: dict) -> bool:
    """Colapsa jitter OCR/movimiento sin borrar pantallas pedagógicamente distintas."""
    if (
        previous.get("media_reference")
        and current.get("media_reference")
        and previous.get("media_phase") != current.get("media_phase")
    ):
        return False
    if previous.get("browsing_reference") and current.get("browsing_reference"):
        if _browsing_phase(previous) != _browsing_phase(current):
            return False
        if not _same_browsing_topic(previous, current):
            return False
    previous_text = " ".join(str(previous.get("ocr", "") or "").casefold().split())
    current_text = " ".join(str(current.get("ocr", "") or "").casefold().split())
    previous_tokens = _tokens(previous_text)
    current_tokens = _tokens(current_text)
    previous_atoms = _technical_atoms(previous_text)
    current_atoms = _technical_atoms(current_text)
    common = previous_tokens & current_tokens
    containment = len(common) / max(1, min(len(previous_tokens), len(current_tokens)))
    text_ratio = (
        difflib.SequenceMatcher(None, previous_text, current_text).ratio()
        if previous_text and current_text else 0.0
    )
    structural = _structural_delta(
        Path(img_dir) / str(previous.get("archivo", "")),
        Path(img_dir) / str(current.get("archivo", "")),
    )
    new_atoms = current_atoms - previous_atoms
    transcript_atoms = _technical_atoms(str(current.get("transcript_ventana", "")))
    confirmed_formula_change = bool(
        new_atoms
        and (
            _replaces_technical_value(previous_atoms, current_atoms)
            or bool(current.get("visual_estable"))
            or bool(new_atoms & transcript_atoms)
            or (
                structural.get("dhash_distance") is not None
                and int(structural["dhash_distance"]) >= 4
                and int(structural.get("active_cells", 0)) >= 2
                and float(structural.get("edge_delta", 0.0)) >= 6.0
            )
        )
    )
    if confirmed_formula_change:
        return False
    if len(common) >= 4 and (text_ratio >= 0.72 or containment >= 0.80):
        return True
    return bool(
        structural.get("dhash_distance") is not None
        and int(structural["dhash_distance"]) <= 4
        and int(structural.get("active_cells", 0)) <= 4
        and (len(common) >= 2 or min(len(previous_tokens), len(current_tokens)) < 4)
    )


_BROWSING_TOPIC_STOP = _TOKEN_STOP | {
    "a", "al", "all", "and", "article", "articulo", "busqueda", "con",
    "creo", "de", "del", "el", "en", "es", "google", "geogle", "imagenes",
    "la", "las", "los", "modo", "noticias", "of", "pagina", "por", "que",
    "read", "resultados", "se", "search", "son", "the", "todo", "un", "una",
    "utiliza", "videos", "wikipedia", "web", "y",
}


def _browsing_topic_tokens(slide: dict) -> set[str]:
    # El OCR principal evita que barras, menús y resultados laterales contaminen el
    # tema. El OCR completo solo actúa como respaldo cuando no hubo recorte útil.
    topic_text = str(slide.get("browsing_query", "") or "").strip()
    if not topic_text:
        topic_text = str(slide.get("browsing_parent_query", "") or "").strip()
    if not topic_text:
        topic_text = str(slide.get("ocr", "") or "").strip()
    if not topic_text:
        topic_text = str(slide.get("ocr_completo", "") or "")
    return {
        token for token in _tokens(topic_text)
        if token not in _BROWSING_TOPIC_STOP
    }


def _browsing_evidence_text(slide: dict) -> str:
    return "\n".join(filter(None, (
        str(slide.get("ocr", "") or "").strip(),
        str(slide.get("ocr_completo", "") or "").strip(),
    )))


def _effective_browsing_query(slide: dict) -> str:
    return str(
        slide.get("browsing_query")
        or slide.get("browsing_parent_query")
        or ""
    ).strip()


def _browsing_phase(slide: dict) -> str:
    """Distingue la consulta escrita del recurso que se abrió después."""
    if str(slide.get("browsing_query", "") or "").strip():
        return "consulta"
    return "recurso"


def _same_browsing_topic(previous: dict, current: dict) -> bool:
    previous_query_tokens = {
        token for token in _tokens(
            _effective_browsing_query(previous))
        if token not in _BROWSING_TOPIC_STOP
    }
    current_query_tokens = {
        token for token in _tokens(
            _effective_browsing_query(current))
        if token not in _BROWSING_TOPIC_STOP
    }
    if previous_query_tokens and current_query_tokens:
        common = previous_query_tokens & current_query_tokens
        containment = len(common) / max(
            1, min(len(previous_query_tokens), len(current_query_tokens)))
        required_common = (
            1
            if min(len(previous_query_tokens), len(current_query_tokens)) == 1
            else 2
        )
        return len(common) >= required_common and containment >= 0.50

    left = _browsing_topic_tokens(previous)
    right = _browsing_topic_tokens(current)
    common = left & right
    if previous_query_tokens or current_query_tokens:
        query = previous_query_tokens or current_query_tokens
        other = right if previous_query_tokens else left
        common = query & other
        query_coverage = len(common) / max(1, len(query))
        required_common = 1 if len(query) == 1 else 2
        return len(common) >= required_common and query_coverage >= 0.50
    containment = len(common) / max(1, min(len(left), len(right)))
    return len(common) >= 3 and containment >= 0.30


def _strong_code_or_formula(text: str) -> bool:
    """Protege notebooks y fórmulas ante la inferencia temporal de navegación."""
    normalized = " ".join(str(text or "").casefold().split())
    if _technical_atoms(normalized):
        return True
    return bool(re.search(
        r"\b(?:import|from\s+[a-z_][a-z0-9_.]*\s+import|def|class|return|"
        r"print|pip\s+install|conda|model\.fit|optimizer\.step|loss\.backward)\b|"
        r"\b[a-z_][a-z0-9_]*\s*(?:==|<=|>=|=|->)\s*(?:[-+]?\d|\[|\()",
        normalized,
    ))


def _assign_browsing_parent_queries(direct_references: list[dict]) -> None:
    """Vincula recursos abiertos con la consulta anterior sin mezclar consultas nuevas."""
    ordered = sorted(
        direct_references,
        key=lambda item: float(
            item.get("t_rel", item.get("t_inicio_rel", 0)) or 0),
    )
    active_query = ""
    active_time = 0.0
    for slide in ordered:
        current_time = float(
            slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0)
        query = str(slide.get("browsing_query", "") or "").strip()
        if query:
            active_query = query
            active_time = current_time
            continue
        if active_query and 0 <= current_time - active_time <= 150:
            slide["browsing_parent_query"] = active_query
    # Algunos cambios de pantalla producen primero el panel de respuesta y unos
    # segundos después un frame donde el buscador vuelve a hacer legible la caja de
    # consulta. Ese recurso anterior pertenece a la misma sesión, no a otra búsqueda.
    for index, slide in enumerate(ordered):
        query = str(slide.get("browsing_query", "") or "").strip()
        if not query:
            continue
        query_time = float(
            slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0)
        for previous in reversed(ordered[:index]):
            previous_time = float(
                previous.get("t_rel", previous.get("t_inicio_rel", 0)) or 0)
            if query_time - previous_time > 30:
                break
            if (
                not str(previous.get("browsing_query", "") or "").strip()
                and not str(previous.get("browsing_parent_query", "") or "").strip()
            ):
                previous["browsing_parent_query"] = query


def _browsing_session_seed(
        slide: dict, direct_references: list[dict]) -> dict | None:
    """Devuelve la referencia web que permite propagar una sesión con alta confianza."""
    if slide.get("material_oficial") or slide.get("visual_estable"):
        return None
    text = str(slide.get("ocr", "") or "")
    if len(_tokens(text)) < 18 or _strong_code_or_formula(text):
        return None
    normalized = " ".join(_browsing_evidence_text(slide).casefold().split())
    application_chrome = sum(term in normalized for term in (
        "archivo", "editar", "insertar", "entorno de ejecución",
        "entorno de ejecucion", "herramientas", "comandos", "ejecutar todas",
        "copiar en drive",
    ))
    if application_chrome >= 4:
        return None
    weak_ui = bool(
        re.search(r"https?://|\bwww\.|\.[a-z]{2,}/", normalized)
        or any(term in normalized for term in (
            "haz una pregunta", "ask a follow-up", "mostrar todo", "show all",
            "sitios", "sources", "article talk", "read edit", "contents hide",
            "traducir esta página", "traducir esta pagina",
        ))
    )
    if (
        not weak_ui
        and str(slide.get("visual_tipo", "") or "") == "slide_textual"
    ):
        return None
    current_time = float(
        slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0)
    candidates = [
        seed for seed in direct_references
        if 0 <= current_time - float(
            seed.get("t_rel", seed.get("t_inicio_rel", 0)) or 0) <= 90
        and _same_browsing_topic(seed, slide)
    ]
    return candidates[-1] if candidates else None


def _prefer_current_browsing(previous: dict, current: dict) -> bool:
    previous_text = str(previous.get("ocr", "") or "").casefold()
    current_text = str(current.get("ocr", "") or "").casefold()
    previous_search = any(term in previous_text for term in (
        "google", "geogle", "bing", "search results", "resultados de búsqueda"))
    current_article = any(term in current_text for term in (
        "wikipedia", "article", "artículo", "articulo", "references", "referencias"))
    if previous_search and current_article:
        return True
    if len(_browsing_topic_tokens(current)) >= len(_browsing_topic_tokens(previous)) + 2:
        return True
    return _prefer_current_representative(previous, current)


def _collapse_media_representatives(img_dir: Path, slides: list[dict]) -> None:
    """Conserva ficha y estados pedagógicos de cada recurso audiovisual.

    No se muestrea por un máximo de imágenes. Para cada identidad se conserva la ficha
    descriptiva más completa y un estado observable de la reproducción. Estados
    adicionales solo se publican cuando su OCR técnico aporta progreso verificable.
    """
    del img_dir  # reservado para futuros gates espaciales compatibles
    ordered = sorted(
        (slide for slide in slides if slide.get("media_reference")),
        key=lambda slide: float(
            slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0),
    )
    groups: list[list[dict]] = []
    for slide in ordered:
        current_time = float(
            slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0)
        group = next((
            candidate for candidate in reversed(groups[-12:])
            if (
                0 <= current_time - float(
                    candidate[-1].get(
                        "t_rel", candidate[-1].get("t_inicio_rel", 0)) or 0
                ) <= 900
                and any(
                    _same_media_topic(member, slide)
                    for member in candidate[-4:]
                )
            )
        ), None)
        if group is None:
            groups.append([slide])
        else:
            group.append(slide)

    for group in groups:
        title = next((
            candidate
            for slide in group
            if (candidate := _media_title(_browsing_evidence_text(slide)))
        ), "")
        for slide in group:
            if title:
                slide["media_title"] = title
            slide["visual_role"] = "sin_valor"
            slide["duplicado_pedagogico"] = True
            slide["cambio_pedagogico"] = {
                "mismo_estado": True,
                "progreso": False,
                "criterio": "recurso_audiovisual_sin_estado_publicable",
            }

        fichas = [
            slide for slide in group if slide.get("media_phase") == "ficha"
        ]
        ficha = max(
            fichas,
            key=lambda slide: (
                len(_tokens(_browsing_evidence_text(slide))),
                len(_browsing_evidence_text(slide)),
                float(slide.get("t_rel", 0) or 0),
            ),
            default=None,
        )
        if ficha is not None:
            ficha["visual_role"] = "principal"
            ficha["duplicado_pedagogico"] = False
            ficha["representante"] = "ficha_audiovisual_mas_informativa"
            ficha["cambio_pedagogico"] = {
                "mismo_estado": False,
                "progreso": True,
                "criterio": "identidad_y_descripcion_del_recurso",
            }

        playback = [
            slide for slide in group if slide.get("media_phase") == "reproduccion"
        ]
        extra_progress: list[dict] = []
        previous_atoms: set[str] = set()
        for slide in playback:
            atoms = _technical_atoms(str(slide.get("ocr", "") or ""))
            new_atoms = atoms - previous_atoms
            if new_atoms and (
                    bool(slide.get("visual_estable"))
                    or bool(new_atoms & _technical_atoms(
                        str(slide.get("transcript_ventana", "") or "")))):
                extra_progress.append(slide)
                previous_atoms |= atoms

        remaining = [slide for slide in playback if slide not in extra_progress]
        playback_rep = None
        if remaining:
            max_score = max(
                float(slide.get("visual_score", 0) or 0) for slide in remaining)
            informative = [
                slide for slide in remaining
                if float(slide.get("visual_score", 0) or 0) >= max_score - 2.0
            ]
            playback_rep = max(
                informative,
                key=lambda slide: float(
                    slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0),
            )

        sequence = list(extra_progress)
        if playback_rep is not None and playback_rep not in sequence:
            sequence.append(playback_rep)
        for slide in sequence:
            slide["visual_role"] = "secuencia" if ficha is not None else "principal"
            slide["duplicado_pedagogico"] = False
            slide["secuencia_progresiva"] = ficha is not None
            slide["representante"] = (
                "estado_audiovisual_con_progreso"
                if slide in extra_progress
                else "estado_audiovisual_observable"
            )
            slide["cambio_pedagogico"] = {
                "mismo_estado": False,
                "progreso": True,
                "criterio": (
                    "cambio_tecnico_verificable"
                    if slide in extra_progress
                    else "estado_visible_de_la_demostracion"
                ),
            }


def _collapse_browsing_representatives(slides: list[dict]) -> None:
    """Deja consulta y recurso por tema; no aplica un techo numérico global."""
    representatives: list[dict] = []
    ordered = sorted(
        (slide for slide in slides
         if slide.get("visual_role") == "galeria" and slide.get("browsing_reference")),
        key=lambda slide: float(
            slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0),
    )
    for slide in ordered:
        current_time = float(
            slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0)
        recent = [
            state for state in representatives[-16:]
            if 0 <= current_time - float(
                state.get("t_rel", state.get("t_inicio_rel", 0)) or 0) <= 300
        ]
        same = next((
            state for state in reversed(recent)
            if (
                _browsing_phase(state) == _browsing_phase(slide)
                and _same_browsing_topic(state, slide)
            )
        ), None)
        if same is None:
            representatives.append(slide)
            continue
        slide["visual_role"] = "sin_valor"
        slide["duplicado_pedagogico"] = True
        slide["cambio_pedagogico"] = {
            "mismo_estado": True,
            "progreso": False,
            "criterio": "misma_navegacion_tematica",
        }
        same.setdefault(
            "representante",
            (
                "consulta_web_mas_informativa"
                if _browsing_phase(same) == "consulta"
                else "referencia_web_mas_informativa"
            ),
        )
        if _prefer_current_browsing(same, slide):
            same["visual_role"] = "sin_valor"
            same["duplicado_pedagogico"] = True
            slide["visual_role"] = "galeria"
            slide["duplicado_pedagogico"] = False
            slide["representante"] = (
                "consulta_web_mas_informativa"
                if _browsing_phase(slide) == "consulta"
                else "referencia_web_mas_informativa"
            )
            representatives[representatives.index(same)] = slide


def _collapse_gallery_representatives(img_dir: Path, slides: list[dict]) -> None:
    """Deja un representante por estado reciente; no impone un total máximo."""
    representatives: list[dict] = []
    ordered = sorted(
        (slide for slide in slides if slide.get("visual_role") == "galeria"),
        key=lambda slide: float(
            slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0),
    )
    for slide in ordered:
        current_time = float(
            slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0)
        recent = [
            state for state in representatives[-24:]
            if 0 <= current_time - float(
                state.get("t_rel", state.get("t_inicio_rel", 0)) or 0) <= 45
        ]
        same = next((
            state for state in reversed(recent)
            if _same_gallery_state(img_dir, state, slide)
        ), None)
        if same is None:
            representatives.append(slide)
            continue
        slide["visual_role"] = "sin_valor"
        slide["duplicado_pedagogico"] = True
        slide["cambio_pedagogico"] = {
            "mismo_estado": True,
            "progreso": False,
            "criterio": "representante_galeria_ocr_estructura",
        }
        if _prefer_current_representative(same, slide):
            same["visual_role"] = "sin_valor"
            same["duplicado_pedagogico"] = True
            slide["visual_role"] = "galeria"
            slide["duplicado_pedagogico"] = False
            slide["representante"] = "captura_mas_completa_o_final"
            representatives[representatives.index(same)] = slide


def clasificar_roles_visuales(
        img_dir: Path, slides: list[dict], *,
        evidence_img_dir: Path | None = None) -> list[dict]:
    """Asigna roles por identidad/valor; nunca aplica un límite numérico global."""
    result = [dict(slide) for slide in slides]
    evidence_img_dir = (
        Path(evidence_img_dir) if evidence_img_dir is not None else Path(img_dir)
    )
    previous_official: dict | None = None
    previous_hash: int | None = None
    pedagogical_states: list[dict] = []
    direct_browsing: list[dict] = []
    direct_media: list[dict] = []
    for slide in result:
        if slide.get("material_oficial"):
            continue
        evidence_text = _browsing_evidence_text(slide)
        if _looks_like_browsing_reference(evidence_text):
            slide["browsing_reference"] = True
            slide["browsing_reference_origin"] = "directa"
            query = _extract_browsing_query(evidence_text)
            if query:
                slide["browsing_query"] = query
            slide["browsing_phase"] = _browsing_phase(slide)
            direct_browsing.append(slide)
        if _looks_like_media_reference(evidence_text):
            slide["media_reference"] = True
            slide["media_reference_origin"] = "directa"
            slide["media_phase"] = _media_phase(slide)
            title = _media_title(evidence_text)
            if title:
                slide["media_title"] = title
            direct_media.append(slide)
    _assign_browsing_parent_queries(direct_browsing)
    for slide in result:
        if (
            slide.get("material_oficial")
            or slide.get("media_reference")
            or not _in_media_session(slide, direct_media)
        ):
            continue
        slide["media_reference"] = True
        slide["media_reference_origin"] = "sesion_tematico_temporal"
        slide["media_phase"] = "reproduccion"
    for slide in result:
        official = slide.get("material_oficial")
        if official:
            meaningful = _tokens(
                f"{official.get('texto', '')} {slide.get('ocr', '')}")
            technical = str(slide.get("visual_tipo", "")) in {
                "diagrama_o_grafico_probable", "codigo_formula_tabla",
            }
            if len(meaningful) <= 1 and not technical:
                role = "sin_valor"
            elif len(meaningful) <= 3 and not technical:
                role = "galeria"
            else:
                role = "principal"
            current_hash = _dhash(Path(img_dir) / str(slide.get("archivo", "")))
            if role == "principal" and previous_official is not None:
                same_source = (
                    official.get("fuente_sha256") == previous_official.get("fuente_sha256")
                )
                consecutive = int(official.get("pagina", 0) or 0) == int(
                    previous_official.get("pagina", 0) or 0) + 1
                distance = ((current_hash ^ previous_hash).bit_count()
                            if current_hash is not None and previous_hash is not None else 65)
                if same_source and consecutive and distance <= 22:
                    role = "secuencia"
                    slide["secuencia_progresiva"] = True
            slide["visual_role"] = role
            previous_official = official if role == "principal" else None
            previous_hash = current_hash if role == "principal" else None
            continue

        previous_official = None
        previous_hash = None
        ocr = str(slide.get("ocr", "") or "").strip()
        visual_type = str(slide.get("visual_tipo", "") or "")
        corroborated = _ocr_corroborated_by_transcript(slide)
        explicit_technical = (
            _looks_technical_content(ocr)
            or "patron_tecnico" in str(slide.get("motivo", ""))
        )
        technical = visual_type == "codigo_formula_tabla" or explicit_technical
        formula_identity = bool(_technical_atoms(ocr))
        stable = bool(slide.get("visual_estable"))
        rich_ocr = len(_tokens(ocr)) >= 18
        browsing_reference = bool(slide.get("browsing_reference"))
        media_reference = bool(slide.get("media_reference"))
        browsing_seed = (
            None
            if browsing_reference
            else _browsing_session_seed(slide, direct_browsing)
        )
        if browsing_seed is not None:
            browsing_reference = True
            slide["browsing_reference"] = True
            slide["browsing_reference_origin"] = "sesion_tematico_temporal"
            slide["browsing_phase"] = "recurso"
            parent_query = _effective_browsing_query(browsing_seed)
            if parent_query:
                slide["browsing_parent_query"] = parent_query
        if slide.get("no_slide") or slide.get("evidencia_secundaria"):
            role = "sin_valor"
        elif media_reference:
            # La ficha y los estados internos se resuelven por identidad después
            # del loop; ``youtube`` no puede convertirlos prematuramente en ruido.
            role = "galeria"
        elif browsing_reference:
            role = "galeria"
        elif (
            visual_type in {"ruido_visual", "pantalla_plana_o_idle"}
            or _looks_like_external_ui(ocr)
        ):
            role = "sin_valor"
        else:
            meaningful = (
                (
                    (technical or visual_type == "diagrama_o_grafico_probable")
                    and (stable or corroborated or rich_ocr or formula_identity)
                )
                or (
                    not slide.get("sin_transcript_cercano")
                    and (stable or corroborated or len(_tokens(ocr)) >= 12)
                )
            )
            if not meaningful:
                role = "sin_valor"
            else:
                current_time = float(
                    slide.get("t_rel", slide.get("t_inicio_rel", 0)) or 0)
                recent_states = [
                    state for state in pedagogical_states[-16:]
                    if 0 <= current_time - float(
                        state.get("t_rel", state.get("t_inicio_rel", 0)) or 0
                    ) <= 300
                ]
                same_state = next((
                    state for state in reversed(recent_states)
                    if _same_semantic_state(state, slide)
                ), None)
                same_delta = None
                if same_state is None and recent_states and min(
                        len(_tokens(str(recent_states[-1].get("ocr", "")))),
                        len(_tokens(ocr))) < 4:
                    sparse_delta = _pedagogical_delta(
                        evidence_img_dir, recent_states[-1], slide)
                    if sparse_delta["mismo_estado"]:
                        same_state, same_delta = recent_states[-1], sparse_delta
                if same_state is not None:
                    role = "sin_valor"
                    slide["duplicado_pedagogico"] = True
                    slide["cambio_pedagogico"] = same_delta or {
                        "mismo_estado": True,
                        "progreso": False,
                        "criterio": "ocr_semantico_estable",
                    }
                    if _prefer_current_representative(same_state, slide):
                        role = str(same_state.get("visual_role", "galeria"))
                        same_state["visual_role"] = "sin_valor"
                        same_state["duplicado_pedagogico"] = True
                        slide["duplicado_pedagogico"] = False
                        slide["representante"] = "captura_mas_completa_o_final"
                        pedagogical_states[
                            pedagogical_states.index(same_state)] = slide
                elif not recent_states:
                    role = (
                        "principal"
                        if ((technical and (stable or corroborated or formula_identity))
                            or (stable and corroborated))
                        else "galeria"
                    )
                    pedagogical_states.append(slide)
                else:
                    previous = recent_states[-1]
                    delta = _pedagogical_delta(evidence_img_dir, previous, slide)
                    slide["cambio_pedagogico"] = delta
                    previous_tokens = _tokens(str(previous.get("ocr", "")))
                    current_tokens = _tokens(ocr)
                    common = len(previous_tokens & current_tokens)
                    similarity = common / max(1, len(previous_tokens | current_tokens))
                    hash_distance = delta.get("dhash_distance")
                    same_composition = (
                        hash_distance is not None
                        and hash_distance <= 18
                        and int(delta.get("active_cells", 0)) <= 6
                    )
                    real_build = (
                        delta["progreso"]
                        and same_composition
                        and common >= 3
                        and similarity >= 0.28
                    )
                    if real_build:
                        role = "secuencia"
                        slide["secuencia_progresiva"] = True
                    elif technical:
                        role = (
                            "principal"
                            if (
                                formula_identity
                                or (
                                    corroborated and rich_ocr
                                    and not explicit_technical
                                )
                            )
                            else "galeria"
                        )
                    else:
                        role = (
                            "principal"
                            if stable and corroborated
                            else "galeria"
                        )
                    pedagogical_states.append(slide)
        slide["visual_role"] = role
    _collapse_media_representatives(evidence_img_dir, result)
    _collapse_browsing_representatives(result)
    _collapse_gallery_representatives(evidence_img_dir, result)
    return result


def _update_manifest(manifest_path: Path, slides: list[dict]) -> None:
    if not manifest_path.is_file():
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    pages = data.get("paginas", []) if isinstance(data, dict) else []
    by_id = {str(page.get("material_id", "")): page
             for page in pages if isinstance(page, dict)}
    for slide in slides:
        official = slide.get("material_oficial") or {}
        page = by_id.get(str(official.get("material_id", "")))
        if page is None:
            continue
        appearances = []
        for item in _appearance_items(
                slide, str(slide.get("archivo_evidencia", ""))):
            appearances.append({
                "archivo": item.get("archivo"),
                "t_rel": item.get("t_rel"),
                "t_abs": item.get("t_abs"),
                "procedencia": "captura_de_aparicion",
                "estado_inclusion": "evidencia",
            })
        appearances = list(page.get("apariciones", [])) + appearances
        unique_appearances = []
        seen: set[tuple[str, object]] = set()
        for appearance in appearances:
            key = (str(appearance.get("archivo", "")), appearance.get("t_rel"))
            if key not in seen:
                seen.add(key)
                unique_appearances.append(appearance)
        page["apariciones"] = sorted(
            unique_appearances,
            key=lambda item: float(item.get("t_rel", 0) or 0),
        )
        page["visual_role"] = slide.get("visual_role", "principal")
        page["archivo_leccion"] = slide.get("archivo")
    _atomic_json(manifest_path, data)


def _reject_temporal_official_outliers(
        material_img_dir: Path, slides: list[dict], pages: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Conserva la captura ante un salto lejano y ambiguo dentro de un deck.

    Un salto real a un apéndice se conserva cuando su página es visualmente única. El
    veto solo aplica si el deck vuelve pronto a la vecindad anterior y la página lejana
    es casi duplicada de una página de esa vecindad.
    """
    material_img_dir = Path(material_img_dir)
    ordered = sorted(
        (dict(slide) for slide in slides),
        key=lambda item: float(item.get("t_rel", 0) or 0),
    )
    page_by_key = {
        (str(page.get("fuente_sha256", "")), int(page.get("pagina", 0) or 0)): page
        for page in pages
    }
    source_sizes: dict[str, int] = {}
    page_hashes: dict[tuple[str, int], int | None] = {}
    for (source, number), page in page_by_key.items():
        source_sizes[source] = max(source_sizes.get(source, 0), number)
        page_hashes[(source, number)] = _dhash(
            material_img_dir / str(page.get("archivo", "")))

    matched_indices = [
        index for index, slide in enumerate(ordered)
        if slide.get("material_oficial")
    ]
    rejected: list[dict] = []
    for position, index in enumerate(matched_indices):
        slide = ordered[index]
        official = slide.get("material_oficial") or {}
        source = str(official.get("fuente_sha256", ""))
        current_page = int(official.get("pagina", 0) or 0)
        current_time = float(slide.get("t_rel", 0) or 0)

        previous = next((
            ordered[matched_indices[candidate]]
            for candidate in range(position - 1, -1, -1)
            if str(ordered[matched_indices[candidate]].get(
                "material_oficial", {}).get("fuente_sha256", "")) == source
            and int(ordered[matched_indices[candidate]].get(
                "material_oficial", {}).get("pagina", 0) or 0) != current_page
        ), None)
        following = next((
            ordered[matched_indices[candidate]]
            for candidate in range(position + 1, len(matched_indices))
            if str(ordered[matched_indices[candidate]].get(
                "material_oficial", {}).get("fuente_sha256", "")) == source
            and int(ordered[matched_indices[candidate]].get(
                "material_oficial", {}).get("pagina", 0) or 0) != current_page
        ), None)
        if previous is None or following is None:
            continue
        previous_page = int(previous["material_oficial"].get("pagina", 0) or 0)
        following_page = int(following["material_oficial"].get("pagina", 0) or 0)
        previous_time = float(previous.get("t_rel", 0) or 0)
        following_time = float(following.get("t_rel", 0) or 0)
        max_jump = max(8, round(source_sizes.get(source, 0) * 0.15))
        local_low = min(previous_page, following_page)
        local_high = max(previous_page, following_page)
        locally_coherent = (
            following_page >= previous_page - 2
            and local_high - local_low <= max_jump * 2
            and 0 <= current_time - previous_time <= 120
            and 0 <= following_time - current_time <= 120
        )
        outside = (
            current_page < local_low - max_jump
            or current_page > local_high + max_jump
        )
        if not locally_coherent or not outside:
            continue

        current_key = (source, current_page)
        current_hash = page_hashes.get(current_key)
        current_text = str(page_by_key.get(current_key, {}).get("texto", ""))
        near_duplicate_page = None
        for local_page in range(max(1, local_low - 2), local_high + 3):
            local_key = (source, local_page)
            local_hash = page_hashes.get(local_key)
            if current_hash is None or local_hash is None:
                continue
            text_evidence = _text_evidence(
                current_text, str(page_by_key.get(local_key, {}).get("texto", "")))
            if (current_hash ^ local_hash).bit_count() <= 10 \
                    and int(text_evidence.get("common", 0)) >= 1:
                near_duplicate_page = local_page
                break
        if near_duplicate_page is None:
            continue

        evidence_file = str(slide.get("archivo_evidencia", "") or "")
        rejection = {
            "reason": "salto_temporal_ambiguo_entre_paginas_casi_duplicadas",
            "pagina_rechazada": current_page,
            "pagina_local_similar": near_duplicate_page,
            "pagina_anterior": previous_page,
            "pagina_siguiente": following_page,
            "t_rel": current_time,
            "archivo_evidencia": evidence_file,
            "material_match": dict(slide.get("material_match") or {}),
        }
        slide["material_match_rejected"] = rejection
        slide["archivo"] = evidence_file or str(slide.get("archivo", ""))
        slide["motivo"] = (
            f"{slide.get('motivo', 'slide')}; material_oficial_rechazado_ambiguo"
        )
        slide.pop("material_oficial", None)
        slide.pop("material_match", None)
        slide.pop("archivo_evidencia", None)
        slide.pop("evidencias_aparicion", None)
        ordered[index] = slide
        rejected.append(rejection)
    return ordered, rejected


def integrar_material_oficial(
        img_dir: Path, slides: list[dict], pages: list[dict], *,
        manifest_path: Path | None = None,
        material_img_dir: Path | None = None) -> tuple[list[dict], dict]:
    """Empareja estados, colapsa por página y conserva cada aparición original."""
    report = {
        "habilitado": bool(pages),
        "estados_entrada": len(slides),
        "estados_emparejados": 0,
        "paginas_oficiales_usadas": 0,
        "duplicados_colapsados": 0,
        "estados_salida": len(slides),
        "roles": {},
        "comparaciones_locales": 0,
        "matches_rechazados_secuencia": 0,
        "detalle_matches_rechazados": [],
    }
    if not pages:
        return slides, report

    img_dir = Path(img_dir)
    material_img_dir = Path(material_img_dir) if material_img_dir is not None else img_dir
    page_features = {
        str(page["material_id"]): _cv_features(
            material_img_dir / str(page["archivo"]))
        for page in pages
    }
    feature_index = _build_feature_index(page_features)
    matched_states: list[dict] = []
    for original in slides:
        slide = dict(original)
        capture_name = str(slide.get("archivo", ""))
        match, comparisons = _best_match(
            img_dir / capture_name, str(slide.get("ocr", "")), pages, page_features,
            feature_index=feature_index)
        report["comparaciones_locales"] += int(comparisons)
        if match is None:
            matched_states.append(slide)
            continue
        page = match["page"]
        slide["archivo_evidencia"] = capture_name
        slide["evidencias_aparicion"] = _appearance_items(slide, capture_name)
        slide["archivo"] = page["archivo"]
        slide["material_oficial"] = {
            key: page.get(key) for key in (
                "material_id", "tipo_fuente", "fuente", "fuente_nombre",
                "fuente_sha256", "pagina", "archivo_sha256", "texto",
            )
        }
        slide["material_match"] = {key: match[key] for key in (
            "score", "inliers", "good_matches", "capture_coverage", "page_coverage",
            "text_ratio", "capture_text_precision", "page_text_coverage",
            "score_margin", "candidatos_evaluados", "metodo", "gate",
        )}
        slide["motivo"] = (
            f"{slide.get('motivo', 'slide')}; material_oficial_pagina_{page['pagina']}"
        )
        matched_states.append(slide)

    matched_states, rejected = _reject_temporal_official_outliers(
        material_img_dir, matched_states, pages)
    report["matches_rechazados_secuencia"] = len(rejected)
    report["detalle_matches_rechazados"] = rejected
    enriched: list[dict] = []
    grouped: dict[str, dict] = {}
    for slide in matched_states:
        page = slide.get("material_oficial") or {}
        if not page:
            enriched.append(slide)
            continue
        report["estados_emparejados"] += 1
        identity = str(page["material_id"])
        if identity in grouped:
            _merge_official_state(grouped[identity], slide)
            report["duplicados_colapsados"] += 1
        else:
            grouped[identity] = slide
            enriched.append(slide)

    enriched.sort(key=lambda item: float(item.get("t_rel", 0) or 0))
    enriched = clasificar_roles_visuales(
        material_img_dir, enriched, evidence_img_dir=img_dir)
    used = {
        str(slide.get("material_oficial", {}).get("material_id", ""))
        for slide in enriched if slide.get("material_oficial")
    }
    roles: dict[str, int] = {}
    for slide in enriched:
        role = str(slide.get("visual_role", "sin_clasificar"))
        roles[role] = roles.get(role, 0) + 1
    report["paginas_oficiales_usadas"] = len(used)
    report["estados_salida"] = len(enriched)
    report["roles"] = roles
    if manifest_path is not None:
        _update_manifest(Path(manifest_path), enriched)
    return enriched, report
