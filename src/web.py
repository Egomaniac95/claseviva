"""Búsqueda web gratuita (DuckDuckGo vía `ddgs`, sin API key) para enriquecer
copiloto, apuntes y lección final. DeepSeek no navega: NOSOTROS buscamos y le
pasamos títulos+resúmenes+URLs para que responda citando fuentes.

Degrada con gracia: si la librería falla o no hay red, devuelve [] y todo sigue.
"""
from . import config
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import re

_disponible = None


def disponible() -> bool:
    global _disponible
    if _disponible is None:
        try:
            from ddgs import DDGS  # noqa: F401
            _disponible = True
        except Exception:
            try:
                from duckduckgo_search import DDGS  # noqa: F401
                _disponible = True
            except Exception as e:
                config.log_error("web", f"busqueda no disponible: {e}")
                _disponible = False
    return _disponible


def _motor():
    try:
        from ddgs import DDGS
    except Exception:
        from duckduckgo_search import DDGS
    return DDGS


def buscar(consulta: str, n: int = 4) -> list[dict]:
    """[{titulo, url, resumen}] o [] si no se puede."""
    if not consulta.strip() or not disponible():
        return []
    try:
        DDGS = _motor()
        with DDGS() as ddg:
            crudos = list(ddg.text(consulta, max_results=n))
        out = []
        for r in crudos[:n]:
            out.append({"titulo": r.get("title", ""), "url": r.get("href", r.get("url", "")),
                        "resumen": (r.get("body", "") or "")[:300]})
        return out
    except Exception as e:
        config.log_error("web", f"buscar({consulta!r}): {type(e).__name__}: {e}")
        return []


class _ExtractorHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.en_title = False
        self.en_script = False
        self.title = ""
        self.partes: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.en_title = tag == "title"
        if tag in {"script", "style", "noscript", "svg"}:
            self.en_script = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.en_title = False
        if tag in {"script", "style", "noscript", "svg"}:
            self.en_script = False

    def handle_data(self, data):
        txt = " ".join(data.split())
        if not txt:
            return
        if self.en_title:
            self.title = (self.title + " " + txt).strip()
        elif not self.en_script and len(txt) > 35:
            self.partes.append(txt)


def _sentencias(texto: str) -> list[str]:
    texto = re.sub(r"\s+", " ", texto).strip()
    trozos = re.split(r"(?<=[.!?])\s+", texto)
    out = []
    for t in trozos:
        t = t.strip()
        if 55 <= len(t) <= 240 and t not in out:
            out.append(t)
        if len(out) >= 6:
            break
    return out


def extraer_contenido_html(html: str, url: str) -> dict:
    """Resumen breve desde HTML ya descargado. No copia textos largos."""
    p = _ExtractorHTML()
    p.feed(html[:250000])
    fuente = urlparse(url).netloc.replace("www.", "") or "web"
    ideas = _sentencias(" ".join(p.partes[:80]))
    return {
        "ok": bool(ideas),
        "titulo": p.title[:140] or fuente,
        "fuente": fuente,
        "ideas": ideas[:6],
        "complementa": ("Aporta contexto externo para contrastar o ampliar lo explicado "
                        "en clase; revisar junto con la transcripción."),
        "url": url,
    }


def extraer_url(url: str, timeout: float = 6.0) -> dict:
    """Extrae título e ideas clave de una página. Si falla, devuelve ok=False."""
    try:
        req = Request(url, headers={"User-Agent": "ClaseViva/1.0 (+study notes)"})
        with urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("content-type", "")
            if "text/html" not in ctype and "application/xhtml" not in ctype:
                return {"ok": False, "url": url, "error": f"contenido no HTML: {ctype}"}
            html = r.read(250000).decode("utf-8", errors="ignore")
        return extraer_contenido_html(html, url)
    except Exception as e:
        config.log_error("web", f"extraer_url({url!r}): {type(e).__name__}: {e}")
        return {"ok": False, "url": url, "error": type(e).__name__}


def bloque_contexto(consulta: str, n: int = 4) -> str:
    """Texto listo para inyectar en un prompt (con URLs para citar)."""
    rs = buscar(consulta, n)
    if not rs:
        return ""
    lineas = [f"=== RESULTADOS WEB para «{consulta}» (cita las URLs que uses) ==="]
    for r in rs:
        lineas.append(f"- {r['titulo']} — {r['resumen']} [{r['url']}]")
    return "\n".join(lineas) + "\n\n"


def seccion_md(consulta: str, n: int = 3) -> str:
    """Sección Markdown con enlaces reales y extracción breve si la página permite lectura."""
    rs = buscar(consulta, n)
    if not rs:
        return ""
    lineas = ["\n#### 🌐 Para profundizar (web)\n"]
    for r in rs:
        info = extraer_url(r["url"]) if r.get("url") else {"ok": False}
        titulo = (info.get("titulo") if info.get("ok") else r.get("titulo")) or "Recurso web"
        url = r.get("url", "")
        if info.get("ok"):
            lineas.append(f"- **{titulo}** — {info['fuente']}")
            lineas.append("  - Ideas clave:")
            for idea in info["ideas"][:6]:
                lineas.append(f"    - {idea[:220]}")
            lineas.append(f"  - Cómo complementa la clase: {info['complementa']}")
            lineas.append(f"  - URL: <{url}>")
        else:
            resumen = (r.get("resumen") or "").strip()
            extra = f" Resumen del buscador: {resumen[:180]}" if resumen else ""
            lineas.append(f"- [{titulo}]({url}) — no se pudo extraer contenido.{extra}")
    return "\n".join(lineas) + "\n"
