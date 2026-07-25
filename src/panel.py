"""PANEL WEB v6.5: interfaz de ClaseViva (http://localhost:8756).

- Estética 2026: Light Aura Mesh con fondo claro/pastel, texto oscuro, cards
  glass claras, logo SVG inline y Markdown rico seguro.
- Servidor MULTI-HILO (threaded=True): una consulta larga a DeepSeek YA NO congela los
  botones ni el refresco (causa raíz de "pausa/fin no respondían").
- TODA acción muestra confirmación (toast) — incluidos errores de red/servidor.
- 📸 La captura de pantalla vive en el COPILOTO: adjunta el screenshot al chat y pregunta.
- Selector de pantalla con nombre amigable (principal/secundaria + resolución), cambiable
  en vivo.
- Vigilante de hijos (modo app): si un proceso propio muere, lo reinicia con límites
  y muestra su estado en la UI; STT permanece exclusivamente remoto.
"""
import contextlib
import html
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from . import audio_sesiones
from . import audio_manifest
from . import captura_audio
from . import config
from . import content_management
from . import copiloto
from . import costos_api
from . import estado as est
from . import local_interventions
from . import math_render
from . import student_notes
from . import stt_backend
from .runtime_shutdown import (
    OwnedProcess,
    PsutilProcessOps,
    ShutdownCoordinator,
    StopPolicy,
    stop_owned_children,
)
SPOOL_FILENAME = "stt-preview.slot"
ACK_FILENAME = "stt-preview.ack.json"
STATE_FILENAME = "stt-preview.state.json"

try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError:
    raise SystemExit("Falta flask: .venv\\Scripts\\python.exe -m pip install flask markdown")


ALLOWED_MD_TAGS = {
    "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "img", "li", "ol", "p", "pre", "strong", "table", "tbody", "td", "th",
    "thead", "tr", "ul", "details", "summary", "small",
}
VOID_MD_TAGS = {"br", "hr", "img"}


def _safe_href(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return url
    if parsed.scheme.lower() == "mailto" and parsed.path:
        return url
    if not parsed.scheme and url.startswith("#"):
        return url
    return ""


def _safe_img_src(url: str) -> str:
    url = str(url or "").strip()
    if re.fullmatch(r"/img/[A-Za-z0-9_.-]+", url):
        return url
    parsed = urlparse(url)
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return url
    return ""


def _host(url: str) -> str:
    return urlparse(url).netloc or "fuente"


def _attrs_html(attrs: list[tuple[str, str]]) -> str:
    return "".join(
        f' {name}="{html.escape(value, quote=True)}"'
        for name, value in attrs
    )


class MarkdownSanitizer(HTMLParser):
    """Sanitiza el HTML generado desde Markdown antes de insertarlo en el panel."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in ALLOWED_MD_TAGS:
            self.out.append(html.escape(self.get_starttag_text() or f"<{tag}>"))
            return

        pairs = {k.lower(): (v or "") for k, v in attrs if k}
        if tag == "a":
            href = _safe_href(pairs.get("href", ""))
            clean: list[tuple[str, str]] = []
            if href:
                clean.extend([
                    ("href", href),
                    ("target", "_blank"),
                    ("rel", "noopener noreferrer"),
                    ("class", "md-link"),
                ])
            title = pairs.get("title", "").strip()
            if title:
                clean.append(("title", title[:180]))
            self.out.append(f"<a{_attrs_html(clean)}>")
            return

        if tag == "img":
            src = _safe_img_src(pairs.get("src", ""))
            alt = pairs.get("alt", "").strip()[:180]
            title = pairs.get("title", "").strip()[:180]
            if not src:
                self.out.append(
                    f'<span class="md-image-blocked">{html.escape(alt or "imagen externa omitida")}</span>'
                )
                return
            local = src.startswith("/img/")
            host = _host(src)
            clean_img = [
                ("src", src),
                ("alt", alt or ("evidencia visual de la clase" if local
                                else f"imagen de {host}")),
                ("loading", "lazy"),
                ("decoding", "async"),
            ]
            if not local:
                clean_img.append(("referrerpolicy", "no-referrer"))
            if title:
                clean_img.append(("title", title))
            if local:
                self.out.append(
                    f'<span class="md-image-card md-image-local">'
                    f'<img{_attrs_html(clean_img)}></span>'
                )
                return
            clean_link = [
                ("href", src),
                ("target", "_blank"),
                ("rel", "noopener noreferrer"),
                ("class", "md-image-link"),
            ]
            self.out.append(
                f'<span class="md-image-card"><a{_attrs_html(clean_link)}>'
                f'<img{_attrs_html(clean_img)}></a>'
                f'<span class="md-source">fuente: '
                f'<a href="{html.escape(src, quote=True)}" target="_blank" '
                f'rel="noopener noreferrer">{html.escape(host)}</a></span></span>'
            )
            return

        clean = []
        if tag == "code":
            cls = pairs.get("class", "")
            if re.match(r"^language-[A-Za-z0-9_+.-]{1,40}$", cls):
                clean.append(("class", cls))
        self.out.append(f"<{tag}{_attrs_html(clean)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ALLOWED_MD_TAGS and tag not in VOID_MD_TAGS:
            self.out.append(f"</{tag}>")
        elif tag not in ALLOWED_MD_TAGS:
            self.out.append(html.escape(f"</{tag}>"))

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data))

    def handle_comment(self, data: str) -> None:
        self.out.append(html.escape(f"<!--{data}-->"))

    def handle_decl(self, decl: str) -> None:
        self.out.append(html.escape(f"<!{decl}>"))

    def html(self) -> str:
        return "".join(self.out)


def sanitize_markdown_html(raw: str) -> str:
    parser = MarkdownSanitizer()
    parser.feed(raw)
    parser.close()
    return parser.html()


try:
    import markdown as md

    def a_html(texto: str) -> str:
        texto = re.sub(
            r'(<img\s+[^>]*?src=["\'])img/([A-Za-z0-9_.-]+)(["\'])',
            r'\1/img/\2\3', str(texto or ""), flags=re.I)
        texto, math_replacements = math_render.protect_math(texto)
        raw = md.markdown(texto, extensions=["tables", "fenced_code", "sane_lists"],
                          output_format="html5")
        return math_render.restore_math(
            sanitize_markdown_html(raw), math_replacements)
except ImportError:
    def a_html(texto: str) -> str:
        return "<pre>" + html.escape(str(texto or "")) + "</pre>"

app = Flask(__name__)
MODO_APP = os.environ.get("CLASE_PANEL_MODO") == "app"
MODO_VISOR_MSG = ("Panel en modo visor. Para crear clases desde la interfaz, inicia "
                  "ClaseViva.vbs/ClaseViva.bat o relanza con CLASE_PANEL_MODO=app.")
MODULOS = ["src.captura_audio", "src.captura_pantalla",
           "src.transcriptor_vivo", "src.escriba"]
HIJOS: list[dict] = []  # {mod, p, intentos}
RUNTIME_INSTANCE_ID = (
    os.environ.get("CLASE_INSTANCE_ID") or uuid.uuid4().hex)
os.environ["CLASE_INSTANCE_ID"] = RUNTIME_INSTANCE_ID
RUNTIME_DIR = (
    Path(tempfile.gettempdir()) / "ClaseViva" / RUNTIME_INSTANCE_ID).resolve()
_PROCESS_OPS = PsutilProcessOps()
_HIJOS_LOCK = threading.RLock()
_SESSION_STOPPING = threading.Event()
_APP_STOPPING = threading.Event()
_SERVER = None
_SHUTDOWN_COORDINATOR: ShutdownCoordinator | None = None
_SHUTDOWN_COORDINATOR_LOCK = threading.Lock()
_LAST_SHUTDOWN_TICKET = None
_APP_CLOSE_FOLLOWUP = threading.Event()
POST_LOG: list[str] = []
POST_LOG_MAX = max(100, int(os.environ.get("CLASE_POST_LOG_LINEAS_UI", "800")))
SALUD: list[str] = []
SALUD_MAX = max(20, int(os.environ.get("CLASE_RUNTIME_HEALTH_LINEAS_UI", "100")))
_POST_CORRIENDO = False
_POST_ERROR = ""
_POST_LOCK = threading.Lock()
_CONTENT_ADMIN_LOCK = threading.Lock()
_MONITORES: list[dict] | None = None
_DISPOSITIVOS: list[dict] | None = None
_SOURCE_CACHE_LOCK = threading.RLock()
_PANEL_CACHE_LOCK = threading.RLock()
_PANEL_CACHE: dict[str, tuple[object, float, object]] = {}


def _panel_cache_clear(prefix: str = "") -> None:
    """Vacía snapshots derivados del panel; nunca toca evidencia persistida."""
    with _PANEL_CACHE_LOCK:
        if not prefix:
            _PANEL_CACHE.clear()
            return
        for key in [key for key in _PANEL_CACHE if key.startswith(prefix)]:
            _PANEL_CACHE.pop(key, None)


def _panel_cached(name: str, signature: object, *, ttl: float,
                  loader) -> object:
    """Deduplica lecturas caras entre polls concurrentes por un TTL acotado."""
    now = time.monotonic()
    with _PANEL_CACHE_LOCK:
        cached = _PANEL_CACHE.get(str(name))
        if cached is not None:
            old_signature, loaded_at, value = cached
            if old_signature == signature and now - loaded_at <= max(0.0, ttl):
                return value
        value = loader()
        _PANEL_CACHE[str(name)] = (signature, now, value)
        if len(_PANEL_CACHE) > 256:
            oldest = min(_PANEL_CACHE, key=lambda key: _PANEL_CACHE[key][1])
            if oldest != str(name):
                _PANEL_CACHE.pop(oldest, None)
        return value


def _path_signature(path: Path) -> tuple[bool, int, int]:
    try:
        stat = path.stat()
        return True, int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return False, 0, 0


def _directory_files_signature(path: Path, pattern: str) -> tuple:
    """Firma barata para invalidar caches si cambia un artefacto físico."""
    if not path.is_dir():
        return ()
    rows = []
    for item in sorted(path.glob(pattern), key=lambda candidate: candidate.name):
        try:
            stat = item.stat()
        except OSError:
            continue
        if item.is_file():
            rows.append((item.name, int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(rows)


def _directory_signature(path: Path) -> tuple:
    """Firma O(1) para polling; los gates finales usan inventario profundo."""
    try:
        stat = path.stat()
        return (True, int(stat.st_mtime_ns)) if path.is_dir() else (False, 0)
    except OSError:
        return (False, 0)


def _panel_jsonl(path: Path) -> list[dict]:
    signature = _path_signature(path)
    return _panel_cached(
        f"jsonl:{path.resolve()}", signature, ttl=3600.0,
        loader=lambda: est.leer_jsonl(path),
    )


def _panel_text(path: Path, *, tail: int = 0) -> str:
    signature = (_path_signature(path), int(tail))

    def load() -> str:
        if not path.is_file():
            return ""
        value = path.read_text(encoding="utf-8")
        return value[-tail:] if tail else value

    return _panel_cached(
        f"text:{path.resolve()}:{tail}", signature, ttl=3600.0, loader=load)


def _last_jsonl_rows(path: Path, *, max_bytes: int = 262_144) -> list[dict]:
    """Lee solo la cola estable de un JSONL creciente."""
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        offset = max(0, size - max(4096, int(max_bytes)))
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read()
        if offset:
            _, _, raw = raw.partition(b"\n")
        rows = []
        for line in raw.splitlines():
            try:
                item = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError, TypeError):
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows
    except OSError:
        return []


def _panel_last_capture(d: Path) -> dict | None:
    log = d / "img_log.jsonl"
    signature = (_path_signature(log), _directory_signature(d / "img"))

    def load():
        for row in reversed(_last_jsonl_rows(log)):
            name = str(row.get("archivo") or "")
            if re.fullmatch(r"[A-Za-z0-9_.-]+", name) and (
                    d / "img" / name).is_file():
                return row
        return None

    return _panel_cached(
        f"last-capture:{d.resolve()}", signature, ttl=3600.0, loader=load)


def _panel_live_lesson(d: Path, parte: int, fallback_text: str) -> str:
    signature = (
        int(parte), _path_signature(d / "resumen_tramos.jsonl"),
        _path_signature(d / "leccion_en_vivo.md"),
        _path_signature(student_notes.root(d) / student_notes.MANIFEST_NAME),
    )

    def load():
        try:
            from . import escriba
            return escriba.vista_leccion_vivo_resumen(d, parte)
        except Exception:
            return leccion_vivo(fallback_text, parte)

    return _panel_cached(
        f"live-lesson:{d.resolve()}:{parte}", signature,
        ttl=3600.0, loader=load)


def _panel_live_lesson_html(d: Path, parte: int, fallback_text: str) -> str:
    """Renderiza Markdown solo cuando cambia la evidencia de la ficha viva."""
    signature = (
        int(parte), _path_signature(d / "resumen_tramos.jsonl"),
        _path_signature(d / "leccion_en_vivo.md"),
        _path_signature(student_notes.root(d) / student_notes.MANIFEST_NAME),
    )
    return _panel_cached(
        f"live-lesson-html:{d.resolve()}:{parte}", signature,
        ttl=3600.0,
        loader=lambda: a_html(_panel_live_lesson(d, parte, fallback_text)),
    )


def _panel_course_html(d: Path) -> str:
    path = est.curso_md(d)
    if not path.is_file():
        return ""
    signature = _path_signature(path)
    return _panel_cached(
        f"course-html:{path.resolve()}", signature, ttl=3600.0,
        loader=lambda: a_html(_panel_text(path)),
    )
_INICIO_LOCK = threading.Lock()  # dos clics rápidos en Empezar NO deben duplicar pipelines

def _post_log_append(linea: str) -> None:
    linea = str(linea or "").strip()
    if not linea:
        return
    POST_LOG.append(linea)
    if len(POST_LOG) > POST_LOG_MAX:
        del POST_LOG[:len(POST_LOG) - POST_LOG_MAX]


def _runtime_health_append(linea: str) -> None:
    """Registra salud del runtime sin contaminar el log de consolidación."""
    linea = str(linea or "").strip()
    if not linea:
        return
    SALUD.append(linea)
    if len(SALUD) > SALUD_MAX:
        del SALUD[:len(SALUD) - SALUD_MAX]


PAGINA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>ClaseViva - panel de clase</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Panel local de ClaseViva para transcripción, capturas, copiloto y consolidación de clases.">
<meta name="theme-color" content="#F8FBFF">
<style>
 :root{color-scheme:dark;--bg:#10131e;--text:#f5f7fb;--mut:#aeb8c8;--mut2:#788497;--panel:rgba(20,27,43,.82);--panel2:rgba(255,255,255,.06);--line:rgba(232,242,248,.15);--line2:rgba(232,242,248,.24);--cyan:#45d4df;--mint:#8dd5bf;--gold:#d8b366;--rose:#e0717a;--green:#55bf89;--shadow:0 28px 80px rgba(1,7,18,.42);--r:14px;--max:1720px;--font:Aptos,"Segoe UI Variable","Segoe UI",system-ui,sans-serif;--display:Bahnschrift,"Aptos Display",Aptos,"Segoe UI",system-ui,sans-serif}
 *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-height:100dvh;color:var(--text);font-family:var(--font);font-variant-numeric:tabular-nums;background:var(--bg);overflow:auto}
 body:before{content:"";position:fixed;inset:0;z-index:-2;background:radial-gradient(circle at 12% 8%,rgba(69,212,223,.23),transparent 28rem),radial-gradient(circle at 82% 14%,rgba(216,179,102,.14),transparent 25rem),radial-gradient(circle at 78% 86%,rgba(141,213,191,.12),transparent 30rem),linear-gradient(150deg,#111520 0%,#0b1019 52%,#15121c 100%)}
 body:after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.32;background:repeating-linear-gradient(90deg,rgba(255,255,255,.028) 0 1px,transparent 1px 84px),repeating-linear-gradient(0deg,rgba(255,255,255,.018) 0 1px,transparent 1px 84px),radial-gradient(circle at 45% 48%,rgba(255,255,255,.035) 0 1px,transparent 1px 5px)}
 .skip{position:absolute;left:16px;top:-48px;background:#effbff;color:#071016;padding:9px 12px;border-radius:8px;z-index:100;text-decoration:none;font-weight:750}.skip:focus{top:12px;outline:2px solid var(--cyan)}
 header{display:grid;grid-template-columns:minmax(150px,220px) minmax(0,1fr) auto;gap:14px;align-items:center;min-height:76px;padding:12px max(18px,calc((100vw - var(--max))/2 + 18px));background:rgba(12,16,27,.84);backdrop-filter:blur(20px);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20;box-shadow:0 10px 34px rgba(2,8,20,.28)}
 .brand{display:flex;flex-direction:column;gap:2px;min-width:0}.logo{font-family:var(--display);font-weight:850;font-size:22px;color:#fff7e9}.brand small{color:var(--mut);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.chips{display:flex;gap:7px;flex-wrap:wrap;align-items:center;min-width:0}.chip{display:inline-flex;gap:6px;align-items:center;background:rgba(255,255,255,.06);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12px;color:var(--mut);white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis}.chip b{color:var(--mint);font-weight:750;overflow:hidden;text-overflow:ellipsis}.chip.wide{max-width:420px}.status b{color:#ff9a9f}.timechip b,.modechip b{color:var(--cyan)}.alerta b{color:var(--gold)}
 main{width:min(var(--max),calc(100vw - 32px));margin:0 auto;padding:16px 0 28px;display:grid;grid-template-columns:minmax(0,1.18fr) minmax(420px,.82fr);grid-template-areas:"start start" "commands commands" "live capture" "live copilot" "lesson copilot" "course post";gap:18px;align-items:start;height:auto;overflow:visible}.stack{display:contents}.primary,.side{display:contents}
 section{background:linear-gradient(155deg,rgba(28,36,54,.88),rgba(14,19,31,.78));border:1px solid var(--line);border-radius:var(--r);padding:18px;overflow:hidden;backdrop-filter:blur(16px);box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.05);min-height:0}#inicio{grid-area:start;padding:0;margin:0}.compact-start .formgrid,.compact-start #imsg{display:none}.compact-start{max-height:62px}.compact-start:hover,.compact-start:focus-within{max-height:none}.compact-start:hover .formgrid,.compact-start:focus-within .formgrid{display:grid}.compact-start:hover #imsg,.compact-start:focus-within #imsg{display:block}.compact-start h3{margin:0}.compact-start:after{content:"Clase cargada; abre el asistente solo al iniciar una nueva sesión.";display:block;color:var(--mut);font-size:12px;margin-top:4px}.compact-start:hover:after,.compact-start:focus-within:after{display:none}
 #controles{grid-area:commands;position:sticky;top:88px;z-index:15;padding:10px;background:rgba(18,24,38,.86);border-color:var(--line2);box-shadow:var(--shadow);backdrop-filter:blur(18px)}#controles .section-head{display:none}.controlgrid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:8px;align-items:end}.controlgrid button,.controlgrid select{width:100%;margin:0}.controlgrid>button:nth-of-type(1),.controlgrid>button:nth-of-type(2){grid-column:span 1}.controlgrid>button:nth-of-type(3){grid-column:span 2}.controlgrid>button:nth-of-type(4){grid-column:span 1}.controlgrid>button:nth-of-type(5){grid-column:span 2}.controlgrid>button:nth-of-type(6){grid-column:span 2}.controlgrid>button:nth-of-type(7){grid-column:span 1}.controlgrid>.field:nth-of-type(1){grid-column:span 1}.controlgrid>.field:nth-of-type(2){grid-column:span 1}.controlgrid>.field:nth-of-type(3){display:none}.controlgrid>.field:nth-of-type(4){grid-column:span 2}.controlgrid>.controlwide{grid-column:auto}.controlgrid>.nota{display:none}
 .primary>section:nth-of-type(2){grid-area:live;min-height:680px;max-height:calc(100dvh - 230px);display:flex;flex-direction:column}.primary>section:nth-of-type(3){grid-area:lesson;min-height:260px}.side>section:nth-of-type(3){grid-area:capture;min-height:420px}.side>section:nth-of-type(4){grid-area:copilot;min-height:560px}.side>section:nth-of-type(5){grid-area:course}.side>#secpost{grid-area:post}
 .section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:14px}h1,h2,h3{font-family:var(--display);color:#fff7e9;letter-spacing:0;text-wrap:balance}h3{margin:0;font-size:20px;line-height:1.12}.panel-kicker,.nota,.empty{color:var(--mut);font-size:12px}.timegrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}.timebox{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px}.timebox span{display:block;color:var(--mut);font-size:11px}.timebox b{display:block;margin-top:4px;color:#fff7e9;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.topic-strip{display:flex;gap:10px;align-items:center;margin-bottom:12px;padding:11px 12px;border:1px solid rgba(69,212,223,.25);border-radius:12px;background:linear-gradient(135deg,rgba(69,212,223,.12),rgba(141,213,191,.06))}.topic-strip span{color:var(--mut);font-size:12px}.topic-strip b{color:#fff7e9;font-size:14px}
 .primary>section:nth-of-type(2)>div:nth-of-type(2){flex:1;min-height:0;display:flex;flex-direction:column;background:rgba(9,13,22,.42)!important;border:1px solid var(--line)!important;border-radius:12px!important;padding:13px!important}#reciente{flex:1;min-height:440px;max-height:none;overflow:auto;white-space:pre-wrap;padding:10px 6px 10px 0;font-size:14px;line-height:1.55;color:#e8eef6}#transdiag{padding-top:8px;border-top:1px solid var(--line);margin-top:8px}
 .capture-frame{min-height:320px;display:flex;align-items:center;justify-content:center;border:1px solid var(--line);border-radius:12px;background:linear-gradient(145deg,rgba(0,0,0,.26),rgba(255,255,255,.035));overflow:hidden}#ultcap{width:100%!important;max-height:42vh!important;object-fit:contain;background:transparent!important;border-radius:10px!important}.side>section:nth-of-type(4){display:flex;flex-direction:column}.side>section:nth-of-type(4) #chat{flex:1;min-height:320px;max-height:520px;overflow:auto;font-size:14px}.q{margin-top:10px;padding:10px 12px;border-radius:13px 13px 4px 13px;background:rgba(216,179,102,.11);border:1px solid rgba(216,179,102,.22);color:#ffe4ad}.a{margin-top:8px;padding:11px 12px;border-radius:13px 13px 13px 4px;background:rgba(255,255,255,.06);border:1px solid var(--line);color:#dfe8f2}.chatbar{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.055)}#preg{width:100%;border:0;background:transparent;padding:10px 8px;min-height:42px}.previa{display:flex;gap:12px;align-items:center;margin-top:10px;padding:10px;border:1px solid rgba(69,212,223,.25);border-radius:12px;background:rgba(69,212,223,.08);color:var(--mut);font-size:12px}.previa img{width:min(320px,52vw);max-height:220px;object-fit:contain;border-radius:10px;border:1px solid var(--line)}.cap{max-width:min(360px,60vw);border-radius:10px;border:1px solid var(--line);display:block;margin:4px 0 8px}
 .summarybar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}#leccion{max-height:260px;overflow:auto;line-height:1.52;color:#e7edf5}#cursohtml{max-height:220px;overflow:auto;color:#dce5ef}.note-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:10px}#postlog{white-space:pre-wrap;max-height:220px;overflow:auto;color:var(--mut);font-size:13px;line-height:1.45}
 button{min-height:40px;padding:9px 13px;border:1px solid rgba(230,250,255,.18);border-radius:10px;background:linear-gradient(180deg,#39afbb,#167986);color:white;font-weight:750;cursor:pointer;white-space:nowrap;transition:transform .18s ease,box-shadow .18s ease,opacity .18s ease,background .18s ease}button:hover{transform:translateY(-1px);box-shadow:0 12px 26px rgba(69,212,223,.22)}button:active{transform:translateY(1px) scale(.99)}button[disabled],select[disabled]{opacity:.66;cursor:wait;transform:none}button.rojo{background:linear-gradient(180deg,#cf6868,#923b42)}button.verde{background:linear-gradient(180deg,#55bf89,#277957)}button.gris{background:rgba(255,255,255,.07);border-color:var(--line2);color:#e6edf6}button.busy{background:linear-gradient(90deg,#1f7f8d,#36b8c5,#1f7f8d);background-size:220% 100%;animation:busybar 1.1s linear infinite}@keyframes busybar{to{background-position:220% 0}}
 input,select{width:100%;min-width:0;min-height:40px;padding:9px 10px;border:1px solid var(--line2);border-radius:10px;background:rgba(255,255,255,.07);color:var(--text);font:inherit;font-size:13px}label{display:block;color:var(--mut);font-size:12px;margin-bottom:4px}option{background:#151b2a}.field{min-width:0}.formgrid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:10px;align-items:end}.span2{grid-column:span 2}.span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}.span6{grid-column:span 6}.span8{grid-column:span 8}.span12{grid-column:1/-1}.message{margin-top:10px;color:var(--gold);font-size:13px}.md{line-height:1.5}.md p{margin:.35rem 0}.md ul,.md ol{margin:.35rem 0 .35rem 1.15rem}.md pre{background:rgba(0,0,0,.28);border:1px solid var(--line);border-radius:10px;padding:10px;overflow:auto}.md code{color:#c8e9ff}
 button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(90px);z-index:90;max-width:min(680px,86vw);padding:12px 18px;border:1px solid rgba(69,212,223,.62);border-radius:12px;background:rgba(13,20,32,.96);box-shadow:var(--shadow);backdrop-filter:blur(18px);transition:transform .25s ease;color:#fff7e9}#toast.ver{transform:translateX(-50%) translateY(0)}#toast.err{border-color:var(--rose);background:rgba(54,19,24,.96)}.region-overlay{position:fixed;inset:0;background:rgba(5,9,16,.9);z-index:70;flex-direction:column;align-items:center;justify-content:center;gap:10px;backdrop-filter:blur(12px)}.region-help{font-size:14px}.region-help a{color:#a9d9ff}.region-help a:first-of-type{color:#ffaaa8}.region-canvas{position:relative;max-width:94vw;max-height:84vh;overflow:hidden}.region-img{max-width:94vw;max-height:84vh;display:block;cursor:crosshair}.region-box{display:none;position:absolute;border:2px solid var(--cyan);background:rgba(69,212,223,.22);pointer-events:none}
 @media(max-width:1180px){header{padding-inline:14px}main{width:min(1120px,calc(100vw - 28px));grid-template-columns:1fr;grid-template-areas:"start" "commands" "live" "capture" "copilot" "lesson" "post" "course"}.primary>section:nth-of-type(2){min-height:620px;max-height:none}.side>section:nth-of-type(4){min-height:520px}#controles{position:static}.controlgrid{grid-template-columns:repeat(6,minmax(0,1fr))}.controlgrid>*{grid-column:auto!important}.controlgrid>.field:nth-of-type(3){display:none!important}#reciente{min-height:420px}}
 @media(max-width:760px){header{grid-template-columns:1fr;gap:10px;min-height:0}.chip{font-size:11.5px;max-width:100%}main{width:min(100vw - 16px,1120px);padding-top:10px}.controlgrid,.formgrid,.timegrid,.chatbar,.note-row{grid-template-columns:1fr}.span2,.span3,.span4,.span5,.span6,.span8{grid-column:1/-1}section{padding:14px}.primary>section:nth-of-type(2){min-height:560px}#reciente{min-height:340px}.capture-frame{min-height:240px}}
 /* v6.2 warm bento overrides */
 :root{--bg:#1d1413;--text:#fff7ea;--mut:#cbb9a7;--mut2:#9d8172;--panel:rgba(58,34,32,.82);--panel2:rgba(255,246,230,.075);--line:rgba(255,238,210,.18);--line2:rgba(255,238,210,.29);--cream:#fff0d2;--orange:#e98a3a;--orange2:#f0b15f;--garnet:#8e2638;--garnet2:#5b1a29;--teal:#36b7aa;--green:#6abf8f;--cyan:#36b7aa;--mint:#9ed8c8;--gold:#f0b15f;--rose:#c94c5e;--shadow:0 28px 78px rgba(43,18,14,.44);--r:16px;--max:1880px;--font:Aptos,"Segoe UI Variable","Segoe UI",system-ui,sans-serif;--display:Bahnschrift,"Aptos Display",Aptos,"Segoe UI",system-ui,sans-serif}
 body{background:var(--bg);color:var(--text);isolation:isolate}
 body:before{background:radial-gradient(circle at 11% 7%,rgba(233,138,58,.23),transparent 25rem),radial-gradient(circle at 87% 15%,rgba(142,38,56,.27),transparent 29rem),radial-gradient(circle at 78% 82%,rgba(54,183,170,.13),transparent 27rem),linear-gradient(150deg,#201313 0%,#2a1719 45%,#1b1510 100%)}
 body:after{opacity:.26;background:repeating-linear-gradient(90deg,rgba(255,240,210,.035) 0 1px,transparent 1px 96px),repeating-linear-gradient(0deg,rgba(255,240,210,.026) 0 1px,transparent 1px 96px),radial-gradient(circle at 48% 50%,rgba(255,240,210,.045) 0 1px,transparent 1px 5px)}
 .ornaments{position:fixed;inset:0;width:100vw;height:100vh;z-index:-1;pointer-events:none;opacity:.62}.ornaments path,.ornaments circle,.ornaments polygon{vector-effect:non-scaling-stroke}.ornaments .warm{stroke:rgba(240,177,95,.28)}.ornaments .wine{stroke:rgba(201,76,94,.23)}.ornaments .teal{stroke:rgba(54,183,170,.24)}.ornaments .fill-warm{fill:rgba(255,240,210,.035)}
 header{min-height:70px;background:linear-gradient(120deg,rgba(70,35,29,.92),rgba(42,24,26,.9) 58%,rgba(32,28,23,.9));border-bottom:1px solid rgba(255,220,178,.18);box-shadow:0 12px 38px rgba(57,20,17,.34)}
 .brand{gap:0}.logo{color:var(--cream);font-size:23px}.brand small{color:#e3c7aa}.chips{gap:6px}.chip{border-radius:9px;padding:5px 8px;background:rgba(255,243,220,.07);border-color:rgba(255,226,189,.17);color:#d8c2ad}.chip b{color:var(--cream)}.status b{color:#ffb06d}.timechip b,.modechip b{color:var(--teal)}.alerta b{color:var(--orange2)}
 main{width:min(var(--max),calc(100vw - 32px));grid-template-columns:minmax(640px,1.42fr) minmax(430px,.72fr);grid-template-areas:"start start" "commands commands" "lesson capture" "lesson copilot" "monitor copilot" "course post";gap:16px;padding-top:14px}
 section,details.panel{background:linear-gradient(150deg,rgba(67,40,36,.86),rgba(32,22,21,.78));border:1px solid var(--line);border-radius:var(--r);padding:16px;overflow:hidden;backdrop-filter:blur(18px);box-shadow:var(--shadow),inset 0 1px 0 rgba(255,244,223,.07);min-height:0}
 .section-head{margin-bottom:12px}.section-head h3,summary .summary-title{display:flex;align-items:center;gap:9px}.ico{width:19px;height:19px;flex:none;stroke:var(--orange2);stroke-width:1.8;fill:none;stroke-linecap:round;stroke-linejoin:round}.ico.teal{stroke:var(--teal)}.ico.wine{stroke:#e17a86}h1,h2,h3{color:var(--cream);letter-spacing:0}h3{font-size:19px}.panel-kicker,.nota,.empty{color:var(--mut);font-size:12px}
 #controles{top:82px;padding:9px 10px;background:linear-gradient(115deg,rgba(80,43,34,.88),rgba(42,28,26,.86));border-color:var(--line2)}.controlgrid{grid-template-columns:repeat(12,minmax(82px,1fr));gap:7px}.controlgrid>*{grid-column:span 1!important}.controlgrid>.field:not(.fallback-device){display:block!important}.controlgrid>.part-field{grid-column:span 3!important}.controlgrid>.action-wide,.controlgrid>.screen-field{grid-column:span 2!important}.controlgrid>.fallback-device{display:none!important}.controlgrid>.fallback-btn,.controlgrid>.region-btn{background:rgba(255,244,222,.06);color:#ead5bd;border-color:rgba(255,232,202,.15);font-weight:650}.controlgrid>.nota{display:none!important}.controlgrid label{font-size:11px;margin-bottom:3px}
 button{min-height:36px;padding:8px 12px;border-color:rgba(255,235,205,.24);border-radius:10px;background:linear-gradient(180deg,#f0a04c,#c85f27);color:#fff8ef;white-space:normal;line-height:1.08;text-wrap:balance}button:hover{box-shadow:0 12px 26px rgba(233,138,58,.22)}button.rojo{background:linear-gradient(180deg,#b33d4d,#711d2d)}button.verde{background:linear-gradient(180deg,#54b59f,#237e74)}button.gris{background:rgba(255,244,222,.07);border-color:rgba(255,232,202,.18);color:#f3dfc6}button.busy{background:linear-gradient(90deg,#9a3a34,#e98a3a,#9a3a34);background-size:220% 100%}
 input,select{min-height:36px;border-color:var(--line2);background:rgba(255,244,222,.08);color:var(--text);border-radius:10px}option{background:#2a1b19}
 .primary>section.panel-lesson{grid-area:lesson;min-height:760px;display:flex;flex-direction:column}.primary>section.panel-live{grid-area:monitor;min-height:330px;max-height:none;display:flex;flex-direction:column}.side>section.panel-capture{grid-area:capture;min-height:460px}.side>section.panel-copilot{grid-area:copilot;min-height:640px;display:flex;flex-direction:column}.side>#secpost{grid-area:post}details.panel-course{grid-area:course;padding:0}
 .summarybar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid rgba(255,236,205,.13)}.summarybar button{min-height:34px;padding-inline:12px}.summarybar select{width:auto;min-width:132px;max-width:150px}.summarybar .inline-note{flex:1 1 230px}
 #leccion{flex:1;min-height:620px;max-height:calc(100dvh - 245px);overflow:auto;color:#f4e9dc;font-size:15.5px;line-height:1.66;padding-right:8px;text-wrap:pretty}.lesson-body h1,.lesson-body h2,.lesson-body h3{line-height:1.18}.lesson-body p,.lesson-body li{max-width:82ch}.lesson-body table{max-width:100%;overflow:auto}
 .monitor-meta{display:flex!important;flex:0 0 auto!important;flex-direction:row!important;gap:8px;align-items:center;flex-wrap:wrap;min-height:auto!important;margin-bottom:9px;background:transparent!important;border:0!important;border-radius:0!important;padding:0!important}.monitor-meta span{display:inline-flex;gap:5px;align-items:center;border:1px solid rgba(255,232,202,.16);border-radius:9px;padding:5px 8px;background:rgba(255,244,222,.055);color:var(--mut);font-size:11.5px}.monitor-meta b{color:var(--cream);font-weight:750}.topic-strip{margin-bottom:9px;border-color:rgba(233,138,58,.25);background:linear-gradient(135deg,rgba(233,138,58,.12),rgba(142,38,56,.08))}
 .last-segment{border-left:3px solid var(--orange);padding:6px 0 8px 10px;margin-bottom:8px;background:linear-gradient(90deg,rgba(233,138,58,.09),transparent)}.last-segment span,.history-head span{color:var(--mut);font-size:11.5px}.last-segment p{margin:3px 0 0;color:#fff2df;line-height:1.42;font-size:14px;white-space:pre-wrap}.history-head{display:flex;justify-content:space-between;gap:10px;align-items:center;border-top:1px solid rgba(255,236,205,.12);padding-top:8px;margin-top:4px}.history-head b{font-size:12.5px;color:#ffe2bb}
 #reciente{flex:none;min-height:150px;max-height:235px;overflow:auto;white-space:pre-wrap;padding:8px 4px 6px 0;font-size:12.5px;line-height:1.48;color:#ead8c8}#transdiag{padding-top:7px;border-top:1px solid rgba(255,236,205,.12);margin-top:6px;color:#bfa997}
 .capture-frame{min-height:360px;aspect-ratio:16/10;border-color:rgba(255,232,202,.16);background:linear-gradient(145deg,rgba(16,10,9,.22),rgba(255,240,210,.045))}#ultcap{max-height:56vh!important;border-radius:12px!important}.side>section.panel-capture #capdate{margin-top:8px;color:#dec5ab}
 .side>section.panel-copilot #chat{flex:1;min-height:360px;max-height:560px;overflow:auto;font-size:14px;padding-right:2px}.q{background:rgba(233,138,58,.12);border-color:rgba(233,138,58,.24);color:#ffe4bf}.a{background:rgba(255,246,230,.065);border-color:rgba(255,232,202,.16);color:#f1e2d0}.chatbar{grid-template-columns:minmax(0,1fr) minmax(84px,auto) minmax(142px,auto);gap:8px;border-color:rgba(255,232,202,.16);background:rgba(255,244,222,.06);border-radius:18px;padding:8px}#preg{min-height:38px;padding:9px 8px}.chatbar button{min-height:38px}.webtoggle{display:inline-flex;align-items:center;gap:6px;width:max-content;margin-top:8px;padding:5px 8px;border:1px solid rgba(255,232,202,.16);border-radius:999px;background:rgba(255,244,222,.055);color:#e2cdb8;font-size:12px}.webtoggle input{width:14px!important;min-width:14px;height:14px;min-height:14px;padding:0;margin:0;accent-color:var(--teal)}.webtoggle svg{width:14px;height:14px;stroke:var(--teal);stroke-width:1.8;fill:none}.previa{align-items:flex-start;border-color:rgba(54,183,170,.24);background:rgba(54,183,170,.08)}.previa img{width:min(440px,70vw);max-height:260px}
 details.panel-course summary{list-style:none;cursor:pointer;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;gap:12px}details.panel-course summary::-webkit-details-marker{display:none}details.panel-course summary:after{content:"abrir";color:var(--orange2);font-size:12px}details.panel-course[open] summary:after{content:"cerrar"}.course-body{padding:0 16px 16px}#cursohtml{max-height:180px;overflow:auto;color:#e8d8c5}.note-row{grid-template-columns:minmax(0,1fr) auto}#postlog{max-height:180px;color:#d3bfa9}
 #toast{border-color:rgba(233,138,58,.62);background:rgba(41,25,22,.96);color:var(--cream)}#toast.err{border-color:#d96978;background:rgba(63,22,28,.96)}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
 @media(max-width:1180px){main{width:min(1160px,calc(100vw - 28px));grid-template-columns:1fr;grid-template-areas:"start" "commands" "lesson" "capture" "copilot" "monitor" "post" "course"}.primary>section.panel-lesson{min-height:620px}.primary>section.panel-live{min-height:300px}.side>section.panel-copilot{min-height:520px}#leccion{min-height:500px;max-height:none}.capture-frame{min-height:340px}#controles{position:static}.controlgrid{grid-template-columns:repeat(6,minmax(92px,1fr))}.controlgrid>.part-field,.controlgrid>.action-wide,.controlgrid>.screen-field{grid-column:span 2!important}}
 @media(max-width:760px){header{grid-template-columns:1fr}.chips{max-height:132px;overflow:auto}main{width:min(100vw - 16px,1160px);gap:12px}.controlgrid,.chatbar,.note-row{grid-template-columns:1fr}.controlgrid>*,.controlgrid>.part-field,.controlgrid>.action-wide,.controlgrid>.screen-field{grid-column:1/-1!important}section,details.panel{padding:13px}details.panel-course{padding:0}.primary>section.panel-lesson{min-height:540px}#leccion{min-height:420px;font-size:14.5px}.primary>section.panel-live{min-height:280px}.monitor-meta span{flex:1 1 130px}.capture-frame{min-height:240px}.side>section.panel-copilot #chat{min-height:280px}.previa img{width:100%;max-height:220px}details.panel-course summary{padding:13px}.course-body{padding:0 13px 13px}}
 /* v6.3 pastel studio polish */
 :root{--bg:#15111d;--text:#fff8f5;--mut:#d7c8d6;--mut2:#ad9db0;--panel:rgba(33,26,43,.82);--panel2:rgba(255,248,244,.075);--line:rgba(249,225,200,.18);--line2:rgba(236,194,255,.29);--cream:#f9e1c8;--lilac:#ecc2ff;--aqua:#2bc9e9;--coral:#ff8a6b;--coral2:#ffb178;--garnet:#8f2944;--garnet2:#5d1830;--teal:#2bc9e9;--green:#6bd1a3;--cyan:#2bc9e9;--mint:#bfeeea;--gold:#f9e1c8;--rose:#d84a65;--shadow:0 30px 86px rgba(10,5,18,.48);--r:16px;--max:1880px}
 body{background:var(--bg);color:var(--text)}
 body:before{background:radial-gradient(circle at 9% 7%,rgba(236,194,255,.25),transparent 26rem),radial-gradient(circle at 62% 0%,rgba(249,225,200,.18),transparent 29rem),radial-gradient(circle at 88% 18%,rgba(43,201,233,.18),transparent 26rem),radial-gradient(circle at 18% 94%,rgba(255,138,107,.12),transparent 29rem),linear-gradient(148deg,#17111f 0%,#211728 46%,#111d24 100%)}
 body:after{opacity:.28;background:repeating-linear-gradient(90deg,rgba(249,225,200,.032) 0 1px,transparent 1px 102px),repeating-linear-gradient(0deg,rgba(236,194,255,.024) 0 1px,transparent 1px 102px),radial-gradient(circle at 48% 50%,rgba(43,201,233,.055) 0 1px,transparent 1px 6px)}
 .ornaments{opacity:.58}.ornaments .warm{stroke:rgba(249,225,200,.30)}.ornaments .wine{stroke:rgba(236,194,255,.28)}.ornaments .teal{stroke:rgba(43,201,233,.27)}.ornaments .fill-warm{fill:rgba(236,194,255,.045)}
 header{background:linear-gradient(122deg,rgba(35,24,48,.94),rgba(34,25,38,.9) 48%,rgba(15,42,51,.88));border-bottom-color:rgba(236,194,255,.2);box-shadow:0 14px 42px rgba(9,4,18,.38)}
 .logo{color:#fff0fb}.brand small{color:#ead7ef}.chip{background:rgba(255,248,244,.075);border-color:rgba(236,194,255,.19);color:#e4d3e4}.chip b{color:#fff0fb}.status b{color:var(--coral2)}.timechip b,.modechip b{color:var(--aqua)}.alerta b{color:var(--cream)}#chipcosto{background:linear-gradient(135deg,rgba(236,194,255,.11),rgba(43,201,233,.075));border-color:rgba(43,201,233,.25)}#costolabel{overflow:hidden;text-overflow:ellipsis}
 main{grid-template-columns:minmax(650px,1.44fr) minmax(430px,.72fr);gap:17px}
 section,details.panel{background:linear-gradient(150deg,rgba(41,31,52,.86),rgba(22,20,31,.8));border-color:var(--line);box-shadow:var(--shadow),inset 0 1px 0 rgba(255,248,244,.075)}
 .section-head{padding:0;margin-bottom:13px}.section-head h3,summary .summary-title{min-width:0}.ico{stroke:var(--coral2)}.ico.teal{stroke:var(--aqua)}.ico.wine{stroke:#f09ab0}h1,h2,h3{color:#fff3f5}
 #inicio.panel-start{padding:18px 20px!important;background:linear-gradient(135deg,rgba(49,35,61,.9),rgba(27,29,42,.84));border-color:rgba(236,194,255,.24)}
 #inicio .section-head{margin-bottom:15px}#inicio h3{padding-left:0}.panel-start .formgrid{gap:12px}.panel-start label{padding-left:2px}.panel-start input,.panel-start select{padding-inline:12px}.compact-start.panel-start{max-height:64px;padding:13px 16px!important}.compact-start:after{color:#dfcce0}
 #controles{background:linear-gradient(115deg,rgba(42,30,51,.9),rgba(22,31,39,.86));border-color:rgba(43,201,233,.22)}.controlgrid{grid-template-columns:repeat(12,minmax(96px,1fr));gap:8px}.controlgrid>.fallback-btn,.controlgrid>.region-btn{background:rgba(255,248,244,.065);border-color:rgba(236,194,255,.18);color:#f1e1ef}.controlgrid>.fallback-btn{font-weight:620}
 button{border-color:rgba(249,225,200,.24);background:linear-gradient(180deg,var(--coral2),#f06f55);color:#fffaf6}button:hover{box-shadow:0 13px 28px rgba(255,138,107,.22)}button.rojo{background:linear-gradient(180deg,#c74462,var(--garnet2))}button.verde{background:linear-gradient(180deg,#30cfe9,#148ca4)}button.gris{background:rgba(255,248,244,.075);border-color:rgba(236,194,255,.2);color:#f2e3f0}button.busy{background:linear-gradient(90deg,#8c5776,#2bc9e9,#ff8a6b);background-size:220% 100%}
 input,select{background:rgba(255,248,244,.082);border-color:rgba(236,194,255,.24);color:var(--text)}option{background:#211728}label{color:#dfcde0}
 .summarybar{border-bottom-color:rgba(236,194,255,.14)}.summarybar select{min-width:124px}.primary>section.panel-lesson{min-height:780px}.primary>section.panel-live{min-height:315px}#leccion{color:#f8edf4;line-height:1.68}.lesson-body.is-empty{display:flex;align-items:center;justify-content:center;min-height:620px}
 .landing-empty{width:min(520px,100%);padding:26px 24px;border:1px solid rgba(236,194,255,.25);border-radius:18px;background:linear-gradient(135deg,rgba(236,194,255,.13),rgba(43,201,233,.08));box-shadow:inset 0 1px 0 rgba(255,248,244,.08);text-align:left}.landing-empty b{display:block;color:#fff3f8;font-size:18px;line-height:1.25;margin-bottom:6px}.landing-empty span{display:block;color:#d9c8d8;font-size:13px;line-height:1.45}
 .monitor-meta span{border-color:rgba(236,194,255,.17);background:rgba(255,248,244,.06)}.monitor-meta b{color:#fff3f5}.topic-strip{border-color:rgba(43,201,233,.24);background:linear-gradient(135deg,rgba(43,201,233,.11),rgba(236,194,255,.09))}.last-segment{border-left-color:var(--aqua);background:linear-gradient(90deg,rgba(43,201,233,.09),transparent)}.last-segment p{color:#fff4f4}.history-head{border-top-color:rgba(236,194,255,.13)}.history-head b{color:#f9e1c8}#reciente{color:#eadde8}#transdiag{border-top-color:rgba(236,194,255,.13);color:#cbbace}
 .capture-frame{border-color:rgba(236,194,255,.18);background:linear-gradient(145deg,rgba(8,14,22,.34),rgba(236,194,255,.05),rgba(43,201,233,.045))}.capture-frame.is-empty:before{content:"";width:min(52%,320px);aspect-ratio:16/10;border:1px dashed rgba(236,194,255,.22);border-radius:16px;background:linear-gradient(135deg,rgba(236,194,255,.07),rgba(43,201,233,.05))}
 .q{background:rgba(255,138,107,.12);border-color:rgba(255,138,107,.24);color:#ffe4da}.a{background:rgba(255,248,244,.065);border-color:rgba(236,194,255,.16);color:#f3e6ef}.chatbar{border-color:rgba(236,194,255,.18);background:linear-gradient(135deg,rgba(255,248,244,.065),rgba(43,201,233,.045));border-radius:18px;padding:8px 9px}#preg{padding-inline:10px}.webtoggle{gap:5px;margin-top:8px;padding:4px 7px;border-color:rgba(43,201,233,.25);background:rgba(43,201,233,.075);color:#e9d9e8;font-size:11.5px}.webtoggle input{width:13px!important;min-width:13px;height:13px;min-height:13px}.webtoggle svg{width:14px;height:14px;stroke:var(--aqua)}.previa{border-color:rgba(43,201,233,.25);background:rgba(43,201,233,.08)}.previa img{width:min(460px,72vw);max-height:280px}
 details.panel-course summary:after{color:var(--aqua)}#cursohtml{color:#eadde7}
 #toast{border-color:rgba(236,194,255,.62);background:rgba(25,18,35,.96);color:#fff3f5}#toast.err{border-color:#e06b7f;background:rgba(50,16,28,.96)}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--aqua)}
 @media(max-width:1180px){main{grid-template-columns:1fr;grid-template-areas:"start" "commands" "lesson" "capture" "copilot" "monitor" "post" "course"}.primary>section.panel-lesson{min-height:620px}#leccion,.lesson-body.is-empty{min-height:500px}.controlgrid{grid-template-columns:repeat(6,minmax(104px,1fr))}}
 @media(max-width:760px){#inicio.panel-start{padding:16px!important}.compact-start.panel-start{padding:13px!important}.formgrid{gap:10px}.landing-empty{padding:20px 18px}.lesson-body.is-empty{min-height:390px}.controlgrid{grid-template-columns:1fr}.summarybar select{width:100%;max-width:none}.chatbar{grid-template-columns:1fr}.webtoggle{width:max-content}.capture-frame.is-empty:before{width:min(70%,280px)}}
 /* v6.4 aura mesh gradient + high relief + rich Markdown */
 :root{--bg:#070a12;--text:#fff9f7;--mut:#d8cada;--mut2:#a797ad;--panel:rgba(16,20,34,.78);--panel2:rgba(255,255,255,.07);--line:rgba(229,241,255,.18);--line2:rgba(236,194,255,.32);--cream:#f9e1c8;--lilac:#ecc2ff;--aqua:#2bc9e9;--blue:#7fa5f2;--violet:#9c35d7;--coral:#ff8a6b;--coral2:#ffbd95;--garnet:#8f2944;--garnet2:#561529;--green:#6bd1a3;--cyan:#2bc9e9;--mint:#bfeeea;--gold:#f9e1c8;--rose:#d84a65;--shadow:0 34px 90px rgba(2,7,19,.56),0 12px 28px rgba(43,201,233,.08);--inner:inset 0 1px 0 rgba(255,255,255,.11),inset 0 -18px 42px rgba(0,0,0,.22);--r:18px;--max:1880px}
 body{background:#070a12;color:var(--text);overflow-x:hidden}
 body:before{background-image:radial-gradient(at 85.32256092005814% 77.82506794038136%,hsla(183.1746031746032,84.75336322869956%,43.72549019607843%,.42) 0%,hsla(183.1746031746032,84.75336322869956%,43.72549019607843%,0) 58%),radial-gradient(at 12.721580297840262% 78.96504379083257%,hsla(220.98591549295776,78.02197802197801%,64.31372549019608%,.34) 0%,hsla(220.98591549295776,78.02197802197801%,64.31372549019608%,0) 62%),radial-gradient(at 36.68424281552986% 27.91164863006117%,hsla(295.9162303664922,84.88888888888889%,44.11764705882353%,.30) 0%,hsla(295.9162303664922,84.88888888888889%,44.11764705882353%,0) 58%),radial-gradient(at 1.8049051328255072% 33.92776648324861%,hsla(183.1746031746032,84.75336322869956%,43.72549019607843%,.28) 0%,hsla(183.1746031746032,84.75336322869956%,43.72549019607843%,0) 56%),radial-gradient(at 49.2405373545942% 74.78248150873944%,hsla(220.98591549295776,78.02197802197801%,64.31372549019608%,.22) 0%,hsla(220.98591549295776,78.02197802197801%,64.31372549019608%,0) 62%),linear-gradient(148deg,rgba(5,7,15,.96) 0%,rgba(16,13,27,.94) 46%,rgba(6,24,30,.92) 100%);filter:saturate(.86) contrast(1.08)}
 body:after{opacity:1;background:linear-gradient(180deg,rgba(2,4,10,.62),rgba(5,7,13,.78)),radial-gradient(circle at 50% 18%,rgba(255,255,255,.052),transparent 36rem),repeating-linear-gradient(90deg,rgba(255,255,255,.025) 0 1px,transparent 1px 112px),repeating-linear-gradient(0deg,rgba(236,194,255,.02) 0 1px,transparent 1px 112px),radial-gradient(circle at 48% 50%,rgba(43,201,233,.05) 0 1px,transparent 1px 6px)}
 .ornaments{opacity:.18;filter:saturate(.7)}
 header{background:linear-gradient(126deg,rgba(9,13,25,.88),rgba(20,16,31,.82) 52%,rgba(6,34,42,.78));border-bottom-color:rgba(236,194,255,.24);box-shadow:0 16px 42px rgba(1,5,16,.48),inset 0 1px 0 rgba(255,255,255,.08)}
 .logo{color:#fff7fb;text-shadow:0 1px 0 rgba(0,0,0,.35)}.brand small{color:#e3d5e6}.chip{border-radius:11px;background:linear-gradient(145deg,rgba(255,255,255,.09),rgba(255,255,255,.045));border-color:rgba(236,194,255,.2);box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 8px 22px rgba(2,8,20,.18);color:#e4d7e6}.chip b{color:#fff8fb}.status b{color:var(--coral2)}.timechip b,.modechip b{color:var(--aqua)}#chipcosto{border-color:rgba(43,201,233,.32);box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 10px 24px rgba(43,201,233,.10)}
 section,details.panel{position:relative;background:linear-gradient(150deg,rgba(23,26,43,.82),rgba(10,14,25,.72));border-color:rgba(235,241,255,.18);box-shadow:var(--shadow),var(--inner);backdrop-filter:blur(24px) saturate(1.12)}
 section:before,details.panel:before{content:"";position:absolute;inset:1px;border-radius:calc(var(--r) - 1px);pointer-events:none;background:linear-gradient(135deg,rgba(255,255,255,.09),transparent 34%,rgba(43,201,233,.045));opacity:.85}
 .section-head,.formgrid,.controlgrid,.summarybar,#leccion,#chat,.capture-frame,.course-body,#postlog,.monitor-meta,.topic-strip,.last-segment,.history-head{position:relative}
 #inicio.panel-start:not(.compact-start){padding:24px 26px!important;border-radius:22px;background:linear-gradient(135deg,rgba(31,25,49,.88),rgba(10,23,32,.82));border-color:rgba(236,194,255,.34);box-shadow:0 36px 92px rgba(3,8,22,.56),0 12px 42px rgba(43,201,233,.10),inset 0 1px 0 rgba(255,255,255,.13),inset 0 -24px 44px rgba(0,0,0,.22)}
 #inicio.panel-start:not(.compact-start) h3{font-size:26px;line-height:1.12}#inicio.panel-start:not(.compact-start) .panel-kicker{font-size:13px;color:#e8d9e9}#inicio .section-head{align-items:center}#inicio .field{min-width:0;overflow:visible}#inicio label{margin-bottom:6px;line-height:1.25}#inicio input,#inicio select{min-height:44px;padding:11px 13px}#inicio button.verde{min-height:44px}.compact-start.panel-start{box-shadow:0 18px 46px rgba(2,7,18,.38),var(--inner)}
 input,select{background:linear-gradient(180deg,rgba(255,255,255,.105),rgba(255,255,255,.055));border-color:rgba(236,194,255,.28);box-shadow:inset 0 1px 0 rgba(255,255,255,.10),inset 0 -12px 20px rgba(0,0,0,.18),0 10px 22px rgba(1,6,18,.18);outline:none}input:hover,select:hover{border-color:rgba(43,201,233,.38)}input:focus,select:focus{border-color:rgba(43,201,233,.65);box-shadow:0 0 0 3px rgba(43,201,233,.16),inset 0 1px 0 rgba(255,255,255,.11)}
 button{border-radius:12px;border-color:rgba(255,235,220,.35);background:linear-gradient(180deg,#ffc49f 0%,#ff8a6b 42%,#d85d46 100%);box-shadow:inset 0 1px 0 rgba(255,255,255,.35),inset 0 -4px 10px rgba(91,21,41,.28),0 15px 28px rgba(255,110,82,.18),0 3px 0 rgba(80,17,32,.32);text-shadow:0 1px 0 rgba(0,0,0,.28);transition:transform .18s ease,box-shadow .18s ease,filter .18s ease,opacity .18s ease}button:hover{transform:translateY(-2px);filter:saturate(1.05) brightness(1.04);box-shadow:inset 0 1px 0 rgba(255,255,255,.38),inset 0 -4px 10px rgba(91,21,41,.25),0 18px 34px rgba(255,110,82,.22),0 3px 0 rgba(80,17,32,.32)}button:active{transform:translateY(1px) scale(.985);box-shadow:inset 0 3px 9px rgba(35,8,20,.34),0 8px 18px rgba(255,110,82,.15)}button.verde{background:linear-gradient(180deg,#74f0ff 0%,#2bc9e9 42%,#0f7f99 100%);box-shadow:inset 0 1px 0 rgba(255,255,255,.36),inset 0 -4px 10px rgba(3,50,69,.34),0 15px 30px rgba(43,201,233,.20),0 3px 0 rgba(4,55,70,.35)}button.rojo{background:linear-gradient(180deg,#e45f7a 0%,#a82f4d 45%,#561529 100%);box-shadow:inset 0 1px 0 rgba(255,255,255,.22),inset 0 -4px 12px rgba(28,3,13,.38),0 15px 30px rgba(168,47,77,.22),0 3px 0 rgba(52,8,23,.42)}button.gris,.controlgrid>.fallback-btn,.controlgrid>.region-btn{background:linear-gradient(180deg,rgba(255,255,255,.105),rgba(255,255,255,.055));border-color:rgba(236,194,255,.24);color:#f4e8f5;box-shadow:inset 0 1px 0 rgba(255,255,255,.13),inset 0 -12px 20px rgba(0,0,0,.18),0 12px 24px rgba(2,8,20,.20)}
 .primary>section.panel-lesson{min-height:790px}.lesson-body{border:1px solid rgba(236,194,255,.18);border-radius:16px;background:linear-gradient(180deg,rgba(255,255,255,.075),rgba(8,12,22,.24));box-shadow:inset 0 1px 0 rgba(255,255,255,.08),inset 0 -20px 44px rgba(0,0,0,.16);padding:22px 24px}#leccion.md,#cursohtml.md,.a.md{line-height:1.74;color:#fff7fb}.md{font-size:15px}.md p{max-width:78ch;margin:.55rem 0}.panel-copilot .a.md p{max-width:66ch}.md h1,.md h2,.md h3,.md h4{margin:1.05rem 0 .45rem;color:#fff3f8;line-height:1.18}.md h1{font-size:1.55rem}.md h2{font-size:1.32rem}.md h3{font-size:1.15rem}.md ul,.md ol{margin:.55rem 0 .75rem 1.25rem;padding-left:.55rem}.md li{margin:.22rem 0}.md blockquote{margin:.8rem 0;padding:.7rem .9rem;border-left:3px solid var(--aqua);background:rgba(43,201,233,.07);border-radius:0 12px 12px 0;color:#e9f8fb}.md pre{margin:.85rem 0;background:linear-gradient(180deg,rgba(3,8,18,.92),rgba(8,12,24,.88));border:1px solid rgba(43,201,233,.22);border-radius:14px;padding:14px 15px;overflow:auto;box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 12px 28px rgba(1,6,18,.26)}.md code{font-family:"Cascadia Mono","Consolas",monospace;color:#d7f7ff}.md :not(pre)>code{padding:.12rem .35rem;border:1px solid rgba(43,201,233,.18);border-radius:7px;background:rgba(43,201,233,.09)}.md table{width:100%;border-collapse:separate;border-spacing:0;margin:.9rem 0;border:1px solid rgba(236,194,255,.22);border-radius:14px;overflow:hidden;background:rgba(255,255,255,.04)}.md th,.md td{padding:10px 12px;border-bottom:1px solid rgba(236,194,255,.14);vertical-align:top}.md th{background:rgba(236,194,255,.11);color:#fff3f8;font-weight:760}.md tr:last-child td{border-bottom:0}.md a{color:#8be9ff;text-decoration:none;border-bottom:1px solid rgba(139,233,255,.36)}.md a:hover{color:#b9f4ff;border-bottom-color:#b9f4ff}.md a.md-link:before{content:"";display:inline-block;width:.52em;height:.52em;margin-right:.34em;border:1px solid currentColor;border-radius:2px;box-shadow:2px -2px 0 -1px currentColor;vertical-align:.04em}.md-image-card{display:block;width:min(100%,720px);margin:.85rem 0;padding:10px;border:1px solid rgba(236,194,255,.22);border-radius:16px;background:rgba(255,255,255,.055);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 14px 30px rgba(2,8,20,.22)}.md-image-card img{display:block;max-width:100%;max-height:420px;object-fit:contain;border-radius:12px;border:1px solid rgba(255,255,255,.14);background:#070a12}.md-source{display:block;margin-top:7px;color:#cfbfd4;font-size:12px}.md-image-blocked,.md-image-error{display:inline-block;margin:.35rem 0;padding:.45rem .6rem;border:1px solid rgba(255,138,107,.26);border-radius:10px;background:rgba(255,138,107,.10);color:#ffe0d8}
 .q,.a{box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 12px 24px rgba(2,8,20,.20)}.a{border-color:rgba(236,194,255,.18)}.chatbar,.previa,.capture-frame,.monitor-meta span,.last-segment{box-shadow:inset 0 1px 0 rgba(255,255,255,.08),inset 0 -14px 28px rgba(0,0,0,.13)}.capture-frame{min-height:350px}.capture-frame img{box-shadow:0 18px 42px rgba(1,6,18,.34)}
 @media(max-width:1180px){main{width:min(1180px,calc(100vw - 28px))}.primary>section.panel-lesson{min-height:640px}.lesson-body{padding:19px 20px}.capture-frame{min-height:330px}}
 @media(max-width:980px){main{width:min(100vw - 18px,1180px)}header{align-items:start}.chips{max-height:144px;overflow:auto}.chip{white-space:normal;line-height:1.25}.formgrid{grid-template-columns:repeat(6,minmax(0,1fr))}.span2,.span3,.span4,.span5,.span6,.span8{grid-column:span 3}.span12{grid-column:1/-1}.chatbar{grid-template-columns:1fr}.md table{display:block;overflow-x:auto}.md p{max-width:100%}}
 @media(max-width:760px){main{width:calc(100vw - 14px);padding-bottom:22px}#inicio.panel-start:not(.compact-start){padding:18px!important}#inicio.panel-start:not(.compact-start) h3{font-size:22px}.formgrid{grid-template-columns:1fr}.span2,.span3,.span4,.span5,.span6,.span8,.span12{grid-column:1/-1}.lesson-body{padding:16px 15px}.md{font-size:14.5px}.md h1{font-size:1.34rem}.md h2{font-size:1.18rem}.md-image-card{padding:8px}.md-image-card img{max-height:300px}button{white-space:normal;min-width:0}.capture-frame{min-height:250px}}
 /* v6.5 LIGHT AURA MESH - overrides the previous dark aura completely */
 :root{color-scheme:light;--bg:#f8fbff;--text:#172033;--mut:#526071;--mut2:#6f7d8f;--panel:rgba(255,255,255,.72);--panel-strong:rgba(255,255,255,.86);--panel2:rgba(255,255,255,.62);--line:rgba(70,55,110,.16);--line2:rgba(70,55,110,.24);--cream:#f9e1c8;--lilac:#ecc2ff;--aqua:#2bc9e9;--blue:#5d8aeb;--violet:#c311d0;--coral:#ff7d64;--coral2:#ffb08d;--garnet:#8f2944;--garnet2:#5d1830;--teal:#2bc9e9;--green:#128668;--cyan:#2bc9e9;--mint:#d9fff8;--gold:#9d623a;--rose:#ba3152;--shadow:0 28px 70px rgba(82,96,113,.18),0 10px 26px rgba(93,138,235,.12);--inner:inset 0 1px 0 rgba(255,255,255,.88),inset 0 -16px 34px rgba(93,138,235,.07);--r:18px;--max:1880px}
 body{background:#f8fbff!important;color:var(--text)!important;overflow-x:hidden}
 body:before{background-image:radial-gradient(at 85.32256092005814% 77.82506794038136%,hsla(183.1746031746032,84.75336322869956%,43.72549019607843%,.62) 0%,hsla(183.1746031746032,84.75336322869956%,43.72549019607843%,0) 58%),radial-gradient(at 12.721580297840262% 78.96504379083257%,hsla(220.98591549295776,78.02197802197801%,64.31372549019608%,.56) 0%,hsla(220.98591549295776,78.02197802197801%,64.31372549019608%,0) 62%),radial-gradient(at 36.68424281552986% 27.91164863006117%,hsla(295.9162303664922,84.88888888888889%,44.11764705882353%,.38) 0%,hsla(295.9162303664922,84.88888888888889%,44.11764705882353%,0) 58%),radial-gradient(at 1.8049051328255072% 33.92776648324861%,hsla(183.1746031746032,84.75336322869956%,43.72549019607843%,.46) 0%,hsla(183.1746031746032,84.75336322869956%,43.72549019607843%,0) 56%),radial-gradient(at 49.2405373545942% 74.78248150873944%,hsla(220.98591549295776,78.02197802197801%,64.31372549019608%,.36) 0%,hsla(220.98591549295776,78.02197802197801%,64.31372549019608%,0) 62%),linear-gradient(148deg,#f8fbff 0%,#fff7f0 45%,#f6ecff 100%)!important;filter:saturate(.96) contrast(1.02)}
 body:after{opacity:1!important;background:linear-gradient(180deg,rgba(255,255,255,.58),rgba(255,255,255,.34)),radial-gradient(circle at 50% 8%,rgba(255,255,255,.62),transparent 34rem),repeating-linear-gradient(90deg,rgba(23,32,51,.035) 0 1px,transparent 1px 112px),repeating-linear-gradient(0deg,rgba(23,32,51,.025) 0 1px,transparent 1px 112px),radial-gradient(circle at 48% 50%,rgba(93,138,235,.08) 0 1px,transparent 1px 6px)!important}
 .ornaments{opacity:.44!important;filter:none}.ornaments path,.ornaments circle,.ornaments polygon,.ornaments line{vector-effect:non-scaling-stroke}.ornaments .warm{stroke:rgba(157,98,58,.28)}.ornaments .wine{stroke:rgba(195,17,208,.22)}.ornaments .teal{stroke:rgba(43,201,233,.35)}.ornaments .blue{stroke:rgba(93,138,235,.28)}.ornaments .const{stroke:rgba(23,32,51,.18);fill:rgba(23,32,51,.18)}.ornaments .fill-warm{fill:rgba(249,225,200,.32)}
 .skip{background:#172033;color:#fff;border-radius:12px}
 header{grid-template-columns:minmax(220px,290px) minmax(0,1fr) auto;min-height:76px;background:rgba(255,255,255,.68)!important;border-bottom:1px solid rgba(70,55,110,.14)!important;box-shadow:0 16px 42px rgba(93,138,235,.13),inset 0 1px 0 rgba(255,255,255,.82)!important;color:var(--text);backdrop-filter:blur(22px) saturate(1.22)}
 .brand{display:flex;flex-direction:row!important;align-items:center;gap:11px;min-width:0}.brand-mark{width:46px;height:46px;flex:none;filter:drop-shadow(0 10px 18px rgba(93,138,235,.22))}.logo-text{display:flex;flex-direction:column;min-width:0}.logo-word{font-family:var(--display);font-weight:850;font-size:24px;line-height:1;color:#172033;letter-spacing:0;text-shadow:none}.brand small{color:#526071!important;font-size:12px;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .chips{gap:7px}.chip{border-radius:12px;background:rgba(255,255,255,.62)!important;border-color:rgba(70,55,110,.15)!important;color:#526071!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.88),0 8px 22px rgba(82,96,113,.10)!important}.chip b{color:#172033!important;font-weight:800}.status b{color:#8f2944!important}.timechip b,.modechip b{color:#135f9d!important}.timechip b{display:inline-block;min-inline-size:8ch;text-align:right;font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}.alerta b{color:#8f2944!important}#chipcosto{background:linear-gradient(135deg,rgba(255,255,255,.78),rgba(43,201,233,.14))!important;border-color:rgba(43,201,233,.34)!important}#costolabel{color:#526071}.header-actions button{min-width:96px}
 main{gap:18px;padding-top:16px}.panel,section,details.panel{position:relative;background:linear-gradient(150deg,rgba(255,255,255,.78),rgba(255,255,255,.62))!important;border:1px solid rgba(70,55,110,.16)!important;color:var(--text)!important;box-shadow:var(--shadow),var(--inner)!important;backdrop-filter:blur(26px) saturate(1.16)}
 section:before,details.panel:before{background:linear-gradient(135deg,rgba(255,255,255,.76),transparent 38%,rgba(43,201,233,.10))!important;opacity:.78}
 h1,h2,h3{color:#172033!important;text-shadow:none}h3{font-weight:820}.panel-kicker,.nota,.empty,label{color:#526071!important}.ico{stroke:#5d8aeb!important}.ico.teal{stroke:#0ea7c6!important}.ico.wine{stroke:#c311d0!important}
 #inicio.panel-start:not(.compact-start){padding:26px 28px!important;border-radius:24px;background:linear-gradient(135deg,rgba(255,255,255,.80),rgba(255,255,255,.58))!important;border-color:rgba(195,17,208,.16)!important;box-shadow:0 34px 82px rgba(93,138,235,.18),0 12px 42px rgba(43,201,233,.12),inset 0 1px 0 rgba(255,255,255,.88),inset 0 -18px 42px rgba(236,194,255,.12)!important}#inicio.panel-start:not(.compact-start) h3{font-size:28px;color:#172033!important}#inicio.panel-start:not(.compact-start) .panel-kicker{color:#526071!important}.compact-start.panel-start{background:rgba(255,255,255,.76)!important;box-shadow:0 18px 46px rgba(93,138,235,.14),var(--inner)!important}.compact-start:after{color:#526071!important}
 input,select{background:rgba(255,255,255,.74)!important;border-color:rgba(70,55,110,.20)!important;color:#172033!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.92),inset 0 -12px 20px rgba(93,138,235,.06),0 10px 22px rgba(82,96,113,.10)!important}option{background:#fff;color:#172033}input::placeholder{color:#7d8897}input:hover,select:hover{border-color:rgba(93,138,235,.38)!important}input:focus,select:focus{border-color:rgba(43,201,233,.70)!important;box-shadow:0 0 0 4px rgba(43,201,233,.18),inset 0 1px 0 rgba(255,255,255,.95)!important}
 button{color:#172033!important;text-shadow:none!important;border-color:rgba(70,55,110,.17)!important;background:linear-gradient(180deg,#ffffff 0%,#f7fbff 42%,#dcecff 100%)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.95),inset 0 -5px 11px rgba(93,138,235,.14),0 16px 28px rgba(93,138,235,.16),0 3px 0 rgba(93,138,235,.18)!important}button:hover{filter:brightness(1.02);box-shadow:inset 0 1px 0 rgba(255,255,255,1),inset 0 -5px 11px rgba(93,138,235,.12),0 20px 34px rgba(93,138,235,.21),0 3px 0 rgba(93,138,235,.18)!important}button:active{box-shadow:inset 0 4px 9px rgba(93,138,235,.18),0 8px 18px rgba(93,138,235,.12)!important}button.verde{background:linear-gradient(180deg,#dfffff 0%,#82eafe 42%,#2bc9e9 100%)!important;color:#08384a!important;border-color:rgba(43,201,233,.40)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.96),inset 0 -5px 12px rgba(14,112,145,.16),0 16px 30px rgba(43,201,233,.24),0 3px 0 rgba(14,112,145,.18)!important}button.rojo{background:linear-gradient(180deg,#ffd0c6 0%,#ff8068 44%,#8f2944 100%)!important;color:#fff7f4!important;border-color:rgba(143,41,68,.28)!important;text-shadow:0 1px 0 rgba(79,14,31,.28)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.62),inset 0 -5px 13px rgba(79,14,31,.25),0 16px 30px rgba(143,41,68,.20),0 3px 0 rgba(93,20,45,.24)!important}button.gris,.controlgrid>.fallback-btn,.controlgrid>.region-btn{background:linear-gradient(180deg,rgba(255,255,255,.86),rgba(246,242,255,.72))!important;color:#29354a!important;border-color:rgba(70,55,110,.18)!important}
 .summarybar{border-bottom-color:rgba(70,55,110,.12)!important}.primary>section.panel-lesson{min-height:790px}.lesson-body{background:rgba(255,255,255,.76)!important;border-color:rgba(70,55,110,.14)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.88),inset 0 -20px 44px rgba(236,194,255,.10)!important;color:#172033!important}#leccion.md,#cursohtml.md,.a.md{line-height:1.76;color:#172033!important}.md{font-size:15.5px}.md p{max-width:78ch;color:#172033}.md h1,.md h2,.md h3,.md h4{color:#172033!important}.md blockquote{border-left-color:#2bc9e9!important;background:rgba(43,201,233,.11)!important;color:#233047!important}.md pre{background:#f3f6ff!important;border-color:rgba(93,138,235,.22)!important;color:#172033!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.88),0 12px 26px rgba(93,138,235,.10)!important}.md code{color:#244e9a!important}.md :not(pre)>code{background:rgba(93,138,235,.10)!important;border-color:rgba(93,138,235,.18)!important;color:#183e85!important}.md table{background:rgba(255,255,255,.76)!important;border-color:rgba(70,55,110,.16)!important}.md th,.md td{border-bottom-color:rgba(70,55,110,.12)!important;color:#172033}.md th{background:rgba(236,194,255,.22)!important;color:#172033!important}.md a{color:#145fb8!important;border-bottom-color:rgba(20,95,184,.30)!important}.md a:hover{color:#0b7c9a!important;border-bottom-color:#0b7c9a!important}.md-image-card{background:rgba(255,255,255,.86)!important;border-color:rgba(70,55,110,.16)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.92),0 18px 34px rgba(93,138,235,.14)!important}.md-image-card img{background:#fff!important;border-color:rgba(70,55,110,.14)!important}.md-source{color:#526071!important}.md-image-blocked,.md-image-error{background:#fff0ec!important;border-color:rgba(143,41,68,.22)!important;color:#8f2944!important}
 .monitor-meta span{background:rgba(255,255,255,.66)!important;border-color:rgba(70,55,110,.14)!important;color:#526071!important}.monitor-meta b{color:#172033!important}.topic-strip{border-color:rgba(43,201,233,.22)!important;background:linear-gradient(135deg,rgba(43,201,233,.14),rgba(236,194,255,.20))!important}.topic-strip span{color:#526071!important}.topic-strip b{color:#172033!important}.last-segment{border-left-color:#2bc9e9!important;background:linear-gradient(90deg,rgba(43,201,233,.12),rgba(255,255,255,0))!important}.last-segment span,.history-head span{color:#526071!important}.last-segment p{color:#172033!important}.history-head{border-top-color:rgba(70,55,110,.12)!important}.history-head b{color:#172033!important}#reciente{color:#28364a!important}#transdiag{border-top-color:rgba(70,55,110,.12)!important;color:#526071!important}
 .capture-frame{background:linear-gradient(145deg,rgba(255,255,255,.68),rgba(246,236,255,.42),rgba(43,201,233,.10))!important;border-color:rgba(70,55,110,.14)!important}.capture-frame.is-empty:before{border-color:rgba(70,55,110,.18)!important;background:linear-gradient(135deg,rgba(255,255,255,.78),rgba(236,194,255,.20),rgba(43,201,233,.12))!important}.capture-frame img{box-shadow:0 18px 42px rgba(82,96,113,.18)!important}.empty{color:#526071!important}
 .q{background:rgba(255,247,240,.82)!important;border-color:rgba(255,125,100,.22)!important;color:#172033!important}.a{background:rgba(255,255,255,.80)!important;border-color:rgba(70,55,110,.14)!important;color:#172033!important}.q,.a{box-shadow:inset 0 1px 0 rgba(255,255,255,.90),0 12px 24px rgba(82,96,113,.10)!important}.chatbar{background:rgba(255,255,255,.72)!important;border-color:rgba(70,55,110,.15)!important;align-items:end}.webtoggle{background:rgba(255,255,255,.64)!important;border-color:rgba(43,201,233,.24)!important;color:#29354a!important}.webtoggle svg{stroke:#0b93ac!important}.webtoggle input{accent-color:#2bc9e9!important}.previa{background:rgba(43,201,233,.10)!important;border-color:rgba(43,201,233,.24)!important;color:#29354a!important}
 #preg{width:100%;min-width:0;min-height:38px;max-height:170px;padding:9px 10px;border:0;background:transparent;color:#172033;font:inherit;font-size:13px;line-height:1.35;resize:none;overflow-y:hidden;white-space:pre-wrap;word-break:break-word}.chatbar.is-expanded #preg{overflow-y:auto}.chatbar.is-expanded{grid-template-columns:minmax(0,1fr) minmax(84px,auto) minmax(142px,auto)}#preg:focus{outline:none}
 details.panel-course summary:after{color:#145fb8!important}#cursohtml{color:#172033!important}.course-body{color:#172033}.note-row input{background:rgba(255,255,255,.72)!important}#postlog{color:#29354a!important}
 .landing-empty{display:grid;grid-template-columns:auto minmax(0,1fr);gap:14px;align-items:center;width:min(620px,100%);padding:24px 24px!important;border:1px solid rgba(70,55,110,.16)!important;border-radius:24px!important;background:linear-gradient(135deg,rgba(255,255,255,.82),rgba(255,255,255,.58))!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.92),0 24px 52px rgba(93,138,235,.15)!important;text-align:left}.landing-empty .empty-logo{width:74px;height:74px;filter:drop-shadow(0 14px 22px rgba(93,138,235,.22))}.landing-empty b{color:#172033!important;font-size:19px!important;margin-bottom:5px}.landing-empty span{color:#526071!important;font-size:13.5px!important}.landing-empty .empty-copy{min-width:0}
 #toast{background:rgba(255,255,255,.92)!important;color:#172033!important;border-color:rgba(43,201,233,.36)!important;box-shadow:0 18px 44px rgba(82,96,113,.18),inset 0 1px 0 rgba(255,255,255,.9)!important}#toast.err{background:#fff5f6!important;border-color:rgba(143,41,68,.38)!important;color:#8f2944!important}
 .region-overlay{background:rgba(248,251,255,.86)!important;color:#172033!important}.region-help a{color:#145fb8!important}.region-help a:first-of-type{color:#8f2944!important}.region-box{border-color:#2bc9e9!important;background:rgba(43,201,233,.18)!important}
 button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid rgba(43,201,233,.72)!important;outline-offset:2px}
 @media(max-width:1180px){.primary>section.panel-lesson{min-height:620px}#leccion,.lesson-body.is-empty{min-height:500px}}
 @media(max-width:980px){header{grid-template-columns:1fr}.brand-mark{width:42px;height:42px}.logo-word{font-size:23px}.chips{max-height:148px;overflow:auto}.chip{white-space:normal}.landing-empty{grid-template-columns:1fr;text-align:left}.landing-empty .empty-logo{width:64px;height:64px}}
 @media(max-width:760px){main{width:calc(100vw - 14px)}#inicio.panel-start:not(.compact-start){padding:18px!important}#inicio.panel-start:not(.compact-start) h3{font-size:23px}.lesson-body{padding:16px 15px}.md{font-size:14.5px}.landing-empty{padding:20px 18px!important}.landing-empty .empty-logo{width:58px;height:58px}}
 /* v6.6 estados operativos reales */
 main{grid-template-areas:"start start" "commands commands" "post post" "lesson capture" "lesson copilot" "monitor copilot" "notes notes" "course course"!important}
 .side>#secpost{grid-area:post!important}
 #controles .section-head{display:flex!important;margin-bottom:10px!important}
 .operational-status{position:relative;display:grid;grid-template-columns:auto auto minmax(0,1fr);gap:9px;align-items:center;margin-bottom:10px;padding:11px 12px;border:1px solid rgba(70,55,110,.14);border-radius:16px;background:linear-gradient(135deg,rgba(255,255,255,.74),rgba(236,194,255,.20),rgba(43,201,233,.12));box-shadow:inset 0 1px 0 rgba(255,255,255,.92),0 14px 28px rgba(93,138,235,.12)}
 .operational-status b{color:#172033;font-size:15px}.operational-status span:last-child{color:#526071;font-size:12.5px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.op-dot{width:11px;height:11px;border-radius:99px;background:#5d8aeb;box-shadow:0 0 0 5px rgba(93,138,235,.13)}
 .operational-status.is-grabando .op-dot{background:#2bc9e9;box-shadow:0 0 0 5px rgba(43,201,233,.18)}.operational-status.is-pausado .op-dot{background:#f0a34a;box-shadow:0 0 0 5px rgba(240,163,74,.16)}.operational-status.is-pendiente .op-dot{background:#8f2944;box-shadow:0 0 0 5px rgba(143,41,68,.13)}.operational-status.is-consolidando .op-dot{background:#c311d0;box-shadow:0 0 0 5px rgba(195,17,208,.14);animation:pulseop 1s ease-in-out infinite}.operational-status.is-consolidada .op-dot{background:#2f9f6a;box-shadow:0 0 0 5px rgba(47,159,106,.15)}.operational-status.is-error .op-dot{background:#b7254b;box-shadow:0 0 0 5px rgba(183,37,75,.16)}
 .viewer-notice{position:relative;margin:2px 0 0;padding:13px 14px;border:1px solid rgba(143,41,68,.20);border-radius:16px;background:linear-gradient(135deg,rgba(255,255,255,.82),rgba(249,225,200,.38));color:#5d1830;font-size:14px;line-height:1.42;box-shadow:inset 0 1px 0 rgba(255,255,255,.90),0 14px 30px rgba(143,41,68,.10)}.viewer-start .formgrid,.viewer-start #imsg,.viewer-start .finish-actions{display:none!important}.content-admin{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding-top:5px;border-top:1px solid var(--line)}.content-admin .nota{margin-right:auto}.compact-start .content-admin{display:none!important}
 .audio-warning{display:flex;gap:8px;align-items:flex-start;margin:-2px 0 10px;padding:10px 12px;border:1px solid rgba(143,41,68,.20);border-radius:14px;background:linear-gradient(135deg,rgba(255,240,236,.86),rgba(249,225,200,.42));color:#7a263b;font-size:13px;line-height:1.35;box-shadow:inset 0 1px 0 rgba(255,255,255,.88),0 12px 24px rgba(143,41,68,.10);white-space:pre-line}.audio-warning:before{content:"!";display:inline-grid;place-items:center;flex:0 0 auto;width:20px;height:20px;border-radius:99px;background:#8f2944;color:#fff;font-weight:850;font-size:13px}
 .summary-status{display:none;flex:1 1 220px;color:#526071;font-size:13px;line-height:1.35}.summary-status.is-active{display:inline-flex;align-items:center;gap:8px;color:#244e9a}.summary-status.is-active:before{content:"";width:8px;height:8px;border-radius:99px;background:#2bc9e9;box-shadow:0 0 0 4px rgba(43,201,233,.14)}.summary-status.is-warning{color:#7a263b;font-weight:720}.summary-status.is-warning:before{background:#8f2944;box-shadow:0 0 0 4px rgba(143,41,68,.12)}
 @keyframes pulseop{50%{transform:scale(1.24);filter:brightness(1.12)}}
 .controlgrid{align-items:stretch!important}.controlgrid button.is-protagonist{grid-column:span 3!important;transform:translateY(-1px);box-shadow:inset 0 1px 0 rgba(255,255,255,.96),inset 0 -6px 14px rgba(14,112,145,.18),0 20px 36px rgba(43,201,233,.28),0 3px 0 rgba(14,112,145,.20)!important}.controlgrid button.soft-action{opacity:.82}
 button[disabled]:not(.busy),select[disabled],input[disabled]{cursor:not-allowed!important;opacity:.48!important;filter:saturate(.65)!important;transform:none!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.70),inset 0 -4px 10px rgba(93,138,235,.07),0 8px 16px rgba(82,96,113,.08)!important}
 #secpost{padding:14px 16px!important;scroll-margin-top:150px}#secpost .section-head{margin-bottom:8px}.post-state{position:relative;margin-bottom:8px;padding:9px 11px;border:1px solid rgba(70,55,110,.13);border-radius:14px;background:rgba(255,255,255,.62);color:#29354a;font-size:13px;box-shadow:inset 0 1px 0 rgba(255,255,255,.86)}#secpost.post-active{border-color:rgba(195,17,208,.22)!important;box-shadow:0 22px 52px rgba(195,17,208,.12),var(--inner)!important}.post-active .post-state{background:linear-gradient(135deg,rgba(236,194,255,.28),rgba(43,201,233,.16));font-weight:720}#postlog{max-height:150px!important;overflow:auto!important;white-space:pre-wrap!important;font-family:"Cascadia Mono","Consolas",monospace;font-size:12.5px;line-height:1.45;color:#29354a!important}.post-compact #postlog{max-height:64px!important;color:#526071!important}
 #inicio.finish-start{padding:14px 16px!important;border-radius:18px!important;background:rgba(255,255,255,.76)!important}#inicio.finish-start .section-head{margin-bottom:8px!important}#inicio.finish-start .formgrid{display:none}#inicio.finish-start.show-start-form .formgrid{display:grid}#inicio.finish-start #imsg{display:block}.finish-actions{position:relative;display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}.finish-actions button{min-height:36px}.compact-start .finish-actions{display:none!important}
 @media(max-width:1180px){main{grid-template-columns:1fr!important;grid-template-areas:"start" "commands" "post" "lesson" "capture" "copilot" "monitor" "notes" "course"!important}#secpost{position:static}.controlgrid button.is-protagonist{grid-column:span 2!important}}
 @media(max-width:760px){.operational-status{grid-template-columns:auto minmax(0,1fr);align-items:start}.operational-status span:last-child{grid-column:1/-1;white-space:normal}.finish-actions{display:grid;grid-template-columns:1fr}.controlgrid button.is-protagonist{grid-column:1/-1!important}}
 .transcript-flow{display:flex;flex:1;min-height:0;flex-direction:column;gap:10px;overflow:auto;padding:4px 2px}#reciente.transcript-canonical{display:flex;flex:none;min-height:0;max-height:none;overflow:visible;white-space:normal;padding:0;flex-direction:column;gap:11px}.transcript-block{display:grid;grid-template-columns:52px minmax(0,1fr);gap:10px;align-items:start}.transcript-time{color:#66758a;font-size:11px;font-weight:760;padding-top:3px}.transcript-block p{margin:0;color:#172033;font-size:14px;line-height:1.58;white-space:normal}.stt-tail{display:none;border-radius:13px;padding:10px 12px;line-height:1.5}.stt-tail.is-visible{display:block}.stt-tail b{display:block;margin-bottom:3px;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase}.stt-tail p{margin:0;white-space:pre-wrap}.stt-stable{border:1px solid rgba(43,201,233,.24);background:rgba(43,201,233,.10);color:#173b4a}.stt-stable b{color:#147f99}.stt-preview{border:1px dashed rgba(195,17,208,.28);background:rgba(236,194,255,.16);color:#633068;font-style:italic}.stt-preview b{color:#8c2094;font-style:normal}.transcript-empty{color:#66758a;padding:18px 4px;font-size:13px}
 details.panel-student-notes{grid-area:notes;padding:0!important;overflow:visible}details.panel-student-notes>summary{list-style:none;cursor:pointer;padding:16px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px}details.panel-student-notes>summary::-webkit-details-marker{display:none}details.panel-student-notes>summary:after{content:"abrir";color:#145fb8;font-size:12px}details.panel-student-notes[open]>summary:after{content:"cerrar"}.student-notes-body{padding:0 18px 18px}.student-note-compose{display:grid;grid-template-columns:minmax(0,1fr) auto auto auto;gap:8px;align-items:end}.student-note-text{width:100%;min-height:84px;resize:vertical;padding:10px;border:1px solid rgba(70,55,110,.20);border-radius:12px;background:rgba(255,255,255,.76);color:#172033;font:inherit}.student-note-files{display:none}.student-note-hint{margin:8px 0 12px;color:#526071;font-size:12px}.student-notes-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}.student-note-card{padding:12px;border:1px solid rgba(70,55,110,.15);border-radius:14px;background:rgba(255,255,255,.70);box-shadow:inset 0 1px 0 rgba(255,255,255,.88)}.student-note-card header{position:static;display:flex;min-height:0;padding:0;background:none;border:0;box-shadow:none;backdrop-filter:none;justify-content:space-between}.student-note-card header b{color:#172033}.student-note-card small{color:#66758a}.student-note-card p{white-space:pre-wrap;color:#29354a;max-height:140px;overflow:auto}.student-note-card textarea{width:100%;min-height:68px;resize:vertical;padding:8px;border:1px solid rgba(70,55,110,.17);border-radius:10px;background:#fff;color:#172033;font:inherit}.student-note-actions{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-top:8px}.student-note-actions label{display:flex;align-items:center;gap:5px;margin:0}.student-note-actions input{width:16px!important;min-width:16px;min-height:16px;box-shadow:none!important}.student-note-actions button{min-height:34px;padding:7px 10px}.student-note-empty{color:#66758a;font-size:13px}@media(max-width:760px){.student-note-compose{grid-template-columns:1fr}.student-note-compose button{width:100%}.student-notes-list{grid-template-columns:1fr}}
.student-note-draft{margin:0 0 12px;padding:12px;border:1px solid rgba(195,17,208,.20);border-radius:14px;background:rgba(236,194,255,.14)}.student-note-draft img,.student-note-draft audio{display:block;max-width:100%;max-height:220px;margin-bottom:8px}.student-note-draft-actions{display:flex;gap:8px;justify-content:flex-end}.student-note-card.is-discarded{opacity:.68}.local-intervention{margin:0 0 12px;padding:12px;border:1px solid rgba(20,95,184,.20);border-radius:14px;background:rgba(219,239,255,.42)}.local-intervention-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}.local-intervention-controls{display:flex;align-items:end;gap:8px;flex-wrap:wrap}.local-intervention-controls label{min-width:240px;flex:1}.local-intervention-controls select{width:100%;min-height:38px}.local-intervention-status{margin-top:8px;color:#41536a;font-size:12px}.local-intervention-status.is-recording{color:#a10d25;font-weight:700}.local-intervention-preview{margin-top:10px}.local-intervention-preview audio{width:100%}@media(max-width:760px){.local-intervention-controls{display:grid;grid-template-columns:1fr}.local-intervention-controls label{min-width:0}.local-intervention-controls button{width:100%}}
 .content-nav{display:inline-flex;gap:6px;margin-left:auto}.content-nav button{min-height:30px!important;padding:5px 9px!important;font-size:12px;background:rgba(255,255,255,.72)!important;color:#244e9a!important;border-color:rgba(70,55,110,.16)!important}.content-nav button:hover{box-shadow:0 8px 18px rgba(93,138,235,.16)!important}.course-content-nav{display:flex;justify-content:flex-end;margin:0 0 6px}#leccion,#cursohtml,#postlog,#chat,.student-note-card p{scrollbar-gutter:stable;overflow-wrap:anywhere;overscroll-behavior:contain}.md pre{max-width:100%;white-space:pre;overflow:auto}.md table{display:block;max-width:100%;overflow-x:auto!important}.md th,.md td{overflow-wrap:anywhere}.lesson-body{overflow-x:auto!important}.operational-status span:last-child{white-space:normal!important;overflow:visible!important;text-overflow:clip!important}
 </style></head><body>
<a class="skip" href="#contenido">Saltar al contenido</a>
<svg class="ornaments" viewBox="0 0 1440 900" preserveAspectRatio="none" aria-hidden="true">
 <path class="warm" d="M-40 182 C 164 128, 264 236, 446 184 S 766 80, 1014 156 S 1290 258, 1490 184" fill="none"/>
 <path class="wine" d="M942 70 C 1010 140, 1118 104, 1176 186 C 1226 258, 1162 338, 1254 406" fill="none"/>
 <path class="teal" d="M-60 708 C 152 642, 300 750, 500 684 S 856 556, 1118 646 S 1320 724, 1500 650" fill="none"/>
 <path class="blue" d="M198 270 C 286 230, 362 254, 420 318 S 540 428, 642 382" fill="none"/>
 <path class="teal" d="M1030 610 C 1110 570, 1196 590, 1266 654 S 1378 748, 1500 704" fill="none"/>
 <line class="const" x1="1010" y1="96" x2="1088" y2="142"/>
 <line class="const" x1="1088" y1="142" x2="1164" y2="112"/>
 <line class="const" x1="1164" y1="112" x2="1228" y2="170"/>
 <circle class="warm" cx="1198" cy="208" r="86" fill="none"/>
 <circle class="teal" cx="210" cy="704" r="58" fill="none"/>
 <circle class="const" cx="1010" cy="96" r="4"/>
 <circle class="const" cx="1088" cy="142" r="3.5"/>
 <circle class="const" cx="1164" cy="112" r="4"/>
 <circle class="const" cx="1228" cy="170" r="3"/>
 <polygon class="fill-warm" points="84,94 168,66 210,142 138,204"/>
</svg>
<header>
 <div class="brand">
  <svg class="brand-mark" viewBox="0 0 64 64" aria-hidden="true">
   <defs><linearGradient id="cvLogoGrad" x1="8" y1="8" x2="56" y2="58" gradientUnits="userSpaceOnUse">
    <stop offset="0" stop-color="#2BC9E9"/><stop offset=".48" stop-color="#ECC2FF"/><stop offset="1" stop-color="#F9E1C8"/>
   </linearGradient></defs>
   <rect x="7" y="7" width="50" height="50" rx="16" fill="url(#cvLogoGrad)"/>
   <path d="M18 39c5-5 13-5 18 0 3-3 7-4 11-3v7c-4-1-8 0-11 3-5-4-13-4-18 0z" fill="rgba(255,255,255,.72)"/>
   <path d="M19 24h8m-8 6h13M38 18v16m6-12v8m-12-5v12" stroke="#172033" stroke-width="2.7" stroke-linecap="round"/>
   <path d="M17 44c7-5 15-5 22 0" stroke="#172033" stroke-width="2.3" stroke-linecap="round" fill="none"/>
  </svg>
  <span class="logo-text"><span class="logo-word">ClaseViva</span><small>estudio en vivo con IA</small></span>
 </div>
  <div class="chips">
   <span class="chip status">Estado <b id="estado">...</b></span>
   <span class="chip timechip">Sesión total <b id="tiempo_pared">-</b></span>
   <span class="chip timechip">Captura útil <b id="duracion">-</b></span>
   <span class="chip timechip">Pausa <b id="tiempo_pausa">-</b></span>
   <span class="chip timechip">Transcrito hasta <b id="transcrito_hasta">-</b></span>
   <span class="chip timechip">Retraso STT <b id="retraso_stt">-</b></span>
   <span class="chip timechip" id="chipvideo" style="display:none">Video equivalente <b id="tiempo_video">-</b></span>
   <span class="chip">Parte <b id="parte">-</b></span>
   <span class="chip">Curso <b id="curso">-</b></span>
   <span class="chip wide">Tema <b id="tema">-</b></span>
   <span class="chip">Audio estado <b id="audioestado">-</b></span>
   <span class="chip">STT <b id="sttestado">-</b></span>
   <span class="chip alerta" id="chiphijos" style="display:none">Procesos <b id="hijos"></b></span>
  <span class="chip" id="chipcosto"><span id="costolabel">Gasto API</span> <b id="costo">Sesión $0 · Curso $0</b></span>
 </div>
 <div class="header-actions"><button id="bcerrar" class="rojo" style="display:none"
   onclick="if(confirm('¿Cerrar la aplicación completa? Si hay clase activa se finalizará.'))cerrarApp()">Cerrar app</button></div>
</header>
<main id="contenido">
 <div class="stack primary">
  <section id="inicio" class="panel panel-start" style="display:none"><div class="section-head"><h3>Inicio de clase</h3><span class="panel-kicker" id="inicio_kicker">curso, pantalla y audio</span></div>
   <div id="finishActions" class="finish-actions" style="display:none">
    <button class="verde" onclick="prepararInicio('nueva')">Iniciar nueva clase</button>
    <button class="gris" onclick="prepararInicio('continuar')">Continuar clase anterior</button>
    <button class="gris" onclick="prepararInicio('')">Cambiar curso/sesión</button>
   </div>
   <div id="viewerNotice" class="viewer-notice" style="display:none">Panel en modo visor. Para crear clases desde la interfaz, inicia ClaseViva.vbs/ClaseViva.bat o relanza con CLASE_PANEL_MODO=app.</div>
   <div class="formgrid">
    <div class="field span2"><label>Curso</label><select id="icurso" onchange="cambioCurso()"></select></div>
    <div class="field span3" id="campo_curso_nuevo" style="display:none"><label>Curso nuevo</label><input id="icursonuevo" placeholder="nombre del curso nuevo"></div>
    <div class="field span3"><label>Sesión</label><select id="itipo" onchange="cambioTipo()">
      <option value="nueva">Lección nueva</option>
      <option value="continuar">Continuación directa</option></select></div>
    <div class="field span4" id="campo_clase" style="display:none"><label>Clase</label><select id="iclase"></select></div>
    <div class="field span3"><label>Tema</label><input id="itema" placeholder="tema de la clase"></div>
    <div class="field span4"><label>Pantalla</label><select id="imon"></select></div>
    <button id="startRefreshSources" class="gris span2" type="button"
      onclick="refreshSources(this)">Actualizar pantallas</button>
    <div class="field span2"><label>Audio</label><select id="ifuente"><option value="chrome">Chrome</option>
      <option value="msedge">Edge</option><option value="sistema">Todo el sistema</option></select></div>
    <div class="field span4"><label>Salida fallback (mezcla sistema)</label><select id="idispositivo"></select></div>
    <div class="field span3"><label>Transcripción</label><select id="isttbackend" onchange="sttBackendChanged()">
      <option value="remote">Remota · failover automático</option></select></div>
     <label class="webtoggle span5" id="sttConsentLabel"><input id="isttconsent" type="checkbox">
      Autorizo enviar WAV sellados a AssemblyAI, Deepgram, Speechmatics o Groq</label>
     <button class="verde span2" onclick="iniciarClase(this)">Empezar</button>
     <div id="contentAdmin" class="content-admin span12">
      <span class="nota">Administrar grabaciones · el movimiento es reversible desde la Papelera.</span>
      <button id="deleteClassButton" class="gris" type="button" onclick="deleteRecordedContent('class',this)">Mover clase a Papelera</button>
      <button id="deleteCourseButton" class="rojo" type="button" onclick="deleteRecordedContent('course',this)">Mover curso a Papelera</button>
     </div>
    </div>
   <div id="imsg" class="message"></div>
  </section>
  <section class="panel panel-live"><div class="section-head"><h3><svg class="ico teal" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h3l2-5 4 10 3-7 2 2h4"/></svg>Estado de evidencia</h3><span class="panel-kicker">audio sellado → STT canónico</span></div>
   <div class="monitor-meta">
    <span>Fuente <b id="monitor_audio">-</b></span>
    <span>Tiempo de pared <b id="ahora_abs">-</b></span>
    <span>Parte <b id="parte_inicio_abs">-</b></span>
    <span>Último <b id="ultimo_segmento_abs">-</b></span>
    <span>Motor <b id="stt_hardware">-</b></span>
   </div>
   <div class="topic-strip"><span>Tema activo</span><b id="tema_focus">-</b></div>
   <p class="nota">La transcripción se conserva para resúmenes y lección final, pero no se renderiza en vivo. Esto evita lectura repetida y trabajo visual sin valor de estudio.</p>
   <div id="transdiag" class="nota"></div>
  </section>
  <section class="panel panel-lesson"><div class="section-head"><h3><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h9l3 3v13H6z"/><path d="M14 4v4h4"/><path d="M9 12h6M9 16h6"/></svg>Resumen y lección en vivo</h3><span class="panel-kicker">DeepSeek bajo demanda</span></div>
   <div class="summarybar">
    <button id="btnResumen" onclick="pedirResumen(this)">Resumir tramo ahora</button>
    <span class="nota inline-note">Auto en 05:00, 10:00, 15:00… de captura útil; este botón resume lo pendiente sin mover esos cortes.</span>
    <span id="resumenStatus" class="summary-status"></span>
    <span class="content-nav" aria-label="Navegar el resumen">
     <button class="gris" type="button" onclick="scrollContent('leccion','start')">Inicio</button>
     <button class="gris" type="button" onclick="scrollContent('leccion','end')">Último tramo</button>
    </span>
   </div>
   <div id="leccion" class="lesson-body md">(sin clase activa)</div>
  </section>
 </div>
 <div class="stack side">
  <section id="controles" class="panel panel-controls"><div class="section-head"><h3><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg>Controles de clase</h3><span class="panel-kicker">estado y acciones</span></div>
   <div id="opstatus" class="operational-status">
    <span class="op-dot"></span><b id="op_title">Sin clase</b><span id="op_hint">Elige curso y sesión.</span>
	   </div>
	   <div id="audioWarning" class="audio-warning" style="display:none"></div>
	   <div id="runtimeHealth" class="nota" style="display:none"></div>
	   <div class="controlgrid">
    <button id="btnPausa" onclick="cmd('pausa',this)">Pausa</button>
    <button id="btnSeguir" onclick="cmd('seguir',this)">Seguir</button>
    <div class="field controlwide part-field"><label>Nombre de parte</label><input id="parteNota" placeholder="opcional, ej. backpropagation"></div>
    <button id="btnParte" class="gris" onclick="cerrarParte(this)">Cerrar parte</button>
    <button id="btnFin" class="rojo" onclick="if(confirm('¿Finalizar la clase?'))cmd('fin',this)">Fin</button>
    <button id="btnConsolidar" class="verde controlwide action-wide" onclick="consolidar(this)">Consolidar lección final</button>
    <button id="btnAbrirFinal" class="verde controlwide action-wide" style="display:none" onclick="window.open('/leccion_final','_blank')">Abrir lección final</button>
    <button id="btnCompactDry" class="gris controlwide action-wide" style="display:none" onclick="compactDry(this)">Compactar clase (dry-run)</button>
    <div class="field"><label>Velocidad</label><select id="selv" onchange="cmd('velocidad '+this.value,this)">
     <option>1</option><option>1.25</option><option>1.5</option><option>1.75</option><option>2</option></select></div>
    <div class="field"><label>Fuente</label><select id="self" onchange="cmd('fuente '+this.value,this)">
     <option>chrome</option><option>msedge</option><option>sistema</option></select></div>
    <div class="field controlwide fallback-device"><label>Salida fallback (mezcla sistema)</label><select id="seld" onchange="cmd('dispositivo '+this.value,this)"></select></div>
    <div class="field controlwide screen-field"><label>Pantalla</label><select id="selm" onchange="cmd('monitor '+this.value,this)"></select></div>
    <button id="refreshSources" class="controlwide gris" type="button"
      onclick="refreshSources(this)">Actualizar pantallas</button>
    <button class="controlwide gris fallback-btn" onclick="cmd('enfoque '+(document.getElementById('enf').textContent=='ON'?'off':'on'),this)">Silenciar otras apps (fallback)</button>
    <button id="btnRegion" class="controlwide gris region-btn" onclick="abrirRegion(this)">Región</button>
    <div class="nota controlwide">Ruta ideal: Chrome/Edge EXCLUSIVO POR PID. Si ves FALLBACK SALIDA SISTEMA, se esta usando la mezcla del dispositivo elegido; "Silenciar otras apps" solo ayuda en esa ruta.</div>
   </div>
  </section>
  <section id="secpost" class="panel" style="display:none"><div class="section-head"><h3><svg class="ico teal" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l4 4L19 6"/><path d="M5 20h14"/></svg>Post-proceso</h3><span class="panel-kicker">consolidación</span></div><div id="legacyWarning" class="audio-warning" style="display:none"></div><div id="evidenciaInfo" class="nota"></div><div id="poststate" class="post-state">Listo para consolidar al finalizar la clase.</div><div id="postlog"></div></section>
  <section class="panel panel-capture"><div class="section-head"><h3><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="12" rx="2"/><path d="M8 21h8M12 17v4"/></svg>Última captura</h3><span class="panel-kicker">cambia con la pantalla</span></div>
   <div class="capture-frame"><img id="ultcap" style="display:none"></div>
   <div id="capdate" class="nota"></div>
   <div id="sincap" class="empty">(aún sin capturas)</div></section>
  <section class="panel panel-copilot"><div class="section-head"><h3><svg class="ico wine" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14v10H8l-3 3z"/><path d="M9 10h6M9 13h4"/></svg>Copiloto</h3><span class="panel-kicker">pregunta, captura o web</span></div><div id="chat"></div>
   <div class="chatbar">
    <textarea id="preg" rows="1" placeholder="Pregunta sobre la clase..." oninput="autoresizePregunta()" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();enviarPregunta(null)}"></textarea>
    <button onclick="enviarPregunta(this)">Enviar</button>
    <button class="gris" title="Congelar captura para la próxima pregunta" onclick="capturarPantalla(this)">Captura + pregunta</button>
   </div>
   <label class="webtoggle" title="Busca en la web (DuckDuckGo) y responde citando fuentes">
    <input type="checkbox" id="webchk"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/></svg><span>Web</span></label>
   <div id="cap_previa" class="previa" style="display:none"></div>
  </section>
  <details id="studentNotes" class="panel panel-student-notes" open><summary><span class="summary-title"><svg class="ico wine" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>Notas del estudiante</span><small class="panel-kicker">texto, archivos, captura y voz</small></summary>
   <div class="student-notes-body">
    <div class="local-intervention" id="localInterventionBox">
     <div class="local-intervention-head"><b>Mi intervención en la sesión</b><small>apagado por defecto</small></div>
     <div class="local-intervention-controls">
      <label>Micrófono<select id="localMicSelect"><option value="">Pulsa Actualizar micrófonos</option></select></label>
      <button id="localMicRefresh" class="gris" onclick="refreshInterventionMics(this)">Actualizar micrófonos</button>
      <button id="localInterventionStart" class="verde" onclick="localInterventionStart(this)">Iniciar mi intervención</button>
      <button id="localInterventionStop" class="rojo" onclick="localInterventionStop()" disabled>Detener</button>
     </div>
     <div id="localInterventionStatus" class="local-intervention-status">No se captura el ambiente ni se observa Meet: solo se graba entre Iniciar y Detener.</div>
     <div id="localInterventionPreview" class="local-intervention-preview" style="display:none">
      <audio id="localInterventionAudio" controls></audio>
      <div class="student-note-draft-actions">
       <button class="verde" onclick="localInterventionSave(this)">Guardar e incluir</button>
       <button class="rojo" onclick="localInterventionDiscard(this)">Descartar</button>
      </div>
     </div>
    </div>
    <div class="student-note-compose" id="studentNotePasteZone">
     <textarea id="studentNoteText" class="student-note-text" placeholder="Escribe o pega tus apuntes. Las imágenes pegadas se conservan como originales separados."></textarea>
     <input id="studentNoteFiles" class="student-note-files" type="file" multiple accept="image/*,.pdf,.doc,.docx,.pptx,.txt,.md,.csv,.json,audio/*">
     <input id="studentNotePhoto" class="student-note-files" type="file" accept="image/*" capture="environment">
     <button class="gris" onclick="document.getElementById('studentNoteFiles').click()">Adjuntar</button>
     <button class="gris" onclick="document.getElementById('studentNotePhoto').click()">Foto</button>
     <button class="gris" onclick="studentNoteScreenshot(this)">Captura de pantalla</button>
     <button id="studentNoteRecord" class="gris" onclick="studentNoteToggleRecord(this)">Grabar voz</button>
     <button class="verde" onclick="studentNoteSaveText(this)">Guardar nota</button>
    </div>
    <div class="student-note-hint">Los originales, hash, procedencia y tiempo se conservan. Toda nota guardada queda incluida por defecto; puedes excluirla o descartarla. La voz usa Groq con confirmación y permanece separada de lo dicho por el profesor.</div>
    <div id="studentNotesList" class="student-notes-list"><div class="student-note-empty">Sin notas todavía.</div></div>
    <div id="studentNoteDraft" class="student-note-draft" style="display:none">
     <div id="studentNoteDraftPreview"></div>
     <div class="student-note-draft-actions">
      <button class="verde" onclick="studentNoteSaveDraft(this)">Guardar</button>
      <button class="rojo" onclick="studentNoteDiscardDraft()">Descartar</button>
     </div>
    </div>
   </div>
  </details>
  <details class="panel panel-course"><summary><span class="summary-title"><svg class="ico teal" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h10a4 4 0 0 1 4 4v10H8a3 3 0 0 0-3 3z"/><path d="M5 5v17"/></svg>Curso hasta hoy</span><small class="panel-kicker">memoria ligera</small></summary>
   <div class="course-body">
   <div class="course-content-nav content-nav" aria-label="Navegar la memoria del curso">
    <button class="gris" type="button" onclick="scrollContent('cursohtml','start')">Inicio</button>
    <button class="gris" type="button" onclick="scrollContent('cursohtml','end')">Final</button>
   </div>
   <div id="cursohtml" class="nota md">(aún sin bitácora)</div>
   <div class="note-row"><input id="cursonota" placeholder="añadir nota al curso..." onkeydown="if(event.key=='Enter')notaCurso(null)">
   <button class="gris" onclick="notaCurso(this)">Añadir</button></div></div></details>
 </div>
</main>
<div id="rov" class="region-overlay" style="display:none">
 <div class="nota region-help">Arrastra un rectángulo sobre el video del profesor -
  <a href="#" onclick="quitarRegion();return false">quitar región actual</a> -
  <a href="#" onclick="cerrarRegion();return false">cancelar</a></div>
 <div class="region-canvas">
  <img id="rimg" class="region-img" onmousedown="rdown(event)" onmousemove="rmove(event)" onmouseup="rup(event)" draggable="false">
  <div id="rbox" class="region-box"></div>
 </div>
</div>
<div id="toast"></div>
<script>
let INFO=null;
let CAPTURA=null;
let SESION_VISUAL=false;
let LAST_STATE=null;
let FAST_REFRESH_TIMER=null;
let REFRESH_TIMER=null;
let REFRESH_IN_FLIGHT=false;
let REFRESH_PENDING=false;
let CLOCK_ANCHOR=null;
const RENDERED_SCROLLABLE={};
const VIEWER_MSG='Panel en modo visor. Para crear clases desde la interfaz, inicia ClaseViva.vbs/ClaseViva.bat o relanza con CLASE_PANEL_MODO=app.';
function esc(s){return (s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function decorateMarkdown(root){
 if(!root)return;
 root.querySelectorAll('a[href]').forEach(a=>{
  const href=a.getAttribute('href')||'';
  if(/^https?:\\/\\//i.test(href)||href.startsWith('mailto:')){
   a.target='_blank';a.rel='noopener noreferrer';
   if(!a.classList.contains('md-image-link'))a.classList.add('md-link');
  }else if(!href.startsWith('#')){a.removeAttribute('href')}
 });
 root.querySelectorAll('img').forEach(img=>{
  const src=img.getAttribute('src')||'';
  if(!/^https?:\\/\\//i.test(src))return;
  img.loading='lazy';img.decoding='async';img.referrerPolicy='no-referrer';
  img.addEventListener('error',()=>{
   const a=document.createElement('a');
   a.href=src;a.target='_blank';a.rel='noopener noreferrer';
   a.className='md-image-error';a.textContent='Imagen no disponible: '+src;
   const card=img.closest('.md-image-card');
   if(card)card.replaceChildren(a);else img.replaceWith(a);
  },{once:true});
 });
}
function scrollContent(id,where){const el=document.getElementById(id);if(!el)return;
 el.scrollTo({top:where==='end'?el.scrollHeight:0,behavior:'smooth'})}
function renderScrollableHtml(root,html,channel,context){
 if(!root)return false;html=String(html||'');context=String(context||'');
 const previous=RENDERED_SCROLLABLE[channel];
 if(previous&&previous.html===html&&previous.context===context)return false;
 const reset=!previous||previous.context!==context;
 const oldTop=root.scrollTop,oldHeight=root.scrollHeight,viewport=root.clientHeight;
 const followEnd=!reset&&oldHeight>viewport+8&&oldHeight-oldTop-viewport<=24;
 root.innerHTML=html;decorateMarkdown(root);
 if(reset)root.scrollTop=0;
 else if(followEnd)root.scrollTop=root.scrollHeight;
 else root.scrollTop=Math.min(oldTop,Math.max(0,root.scrollHeight-root.clientHeight));
 RENDERED_SCROLLABLE[channel]={html,context};return true;
}
function renderFollowingText(root,text,channel){
 if(!root)return;text=String(text||'');const previous=RENDERED_SCROLLABLE[channel];
 if(previous&&previous.html===text)return;
 const oldTop=root.scrollTop,oldHeight=root.scrollHeight,viewport=root.clientHeight;
 const followEnd=!previous||oldHeight<=viewport+8||oldHeight-oldTop-viewport<=24;
 root.textContent=text;root.scrollTop=followEnd?root.scrollHeight:Math.min(
  oldTop,Math.max(0,root.scrollHeight-root.clientHeight));
 RENDERED_SCROLLABLE[channel]={html:text,context:''};
}
function toast(m,err){const t=document.getElementById('toast');t.textContent=m;
 t.className='ver'+(err?' err':'');clearTimeout(t._t);t._t=setTimeout(()=>t.className='',3000)}
function refreshDelay(){
 if(document.hidden)return 15000;
 if(LAST_STATE&&LAST_STATE.post_corriendo)return 1000;
 if(!LAST_STATE||LAST_STATE.estado==='sin clase'||LAST_STATE.estado==='fin')return 5000;
 if(LAST_STATE.estado==='pausado'||LAST_STATE.auto_pausa)return 8000;
 return 5000;
}
function formatElapsed(seconds){
 const total=Math.max(0,Math.floor(Number(seconds)||0));
 const h=String(Math.floor(total/3600)).padStart(2,'0');
 const m=String(Math.floor((total%3600)/60)).padStart(2,'0');
 const s=String(total%60).padStart(2,'0');return h+':'+m+':'+s;
}
function anclarRelojLocal(state){
 CLOCK_ANCHOR={at:performance.now(),estado_efectivo:state.estado_efectivo||'',
  elapsed_wall_sec:Number(state.elapsed_wall_sec)||0,
  recorded_elapsed_sec:Number(state.recorded_elapsed_sec)||0,
  velocidad:Number(state.velocidad)||1};
}
function actualizarRelojLocal(){
 const a=CLOCK_ANCHOR;if(!a||document.hidden)return;
 const finalizado=a.estado_efectivo==='finalizado';
 const activo=a.estado_efectivo==='grabando_activo';
 const delta=finalizado?0:Math.max(0,(performance.now()-a.at)/1000);
 const wall=a.elapsed_wall_sec+delta;
 const recorded=a.recorded_elapsed_sec+(activo?delta:0);
 const values={duracion:recorded,tiempo_pared:wall,
  tiempo_pausa:Math.max(0,wall-recorded),tiempo_video:recorded*a.velocidad,
  ahora_abs:wall};
 for(const[id,value]of Object.entries(values)){
  const el=document.getElementById(id);if(el)el.textContent=formatElapsed(value)}
}
function scheduleRefresh(ms){clearTimeout(REFRESH_TIMER);REFRESH_TIMER=setTimeout(()=>refrescar(),Math.max(0,ms||0))}
function refrescoRapido(ms){clearTimeout(FAST_REFRESH_TIMER);FAST_REFRESH_TIMER=setTimeout(()=>refrescar(true),ms||1200)}
function setBusy(el,on,label){if(!el)return;if(on){el.dataset.prevLabel=el.innerHTML;el.disabled=true;el.classList.add('busy');
 if(el.tagName==='BUTTON'&&label)el.textContent=label}else{el.disabled=false;el.classList.remove('busy');if(el.dataset.prevLabel){el.innerHTML=el.dataset.prevLabel;delete el.dataset.prevLabel}}}
async function runBusy(el,label,fn){setBusy(el,true,label);try{return await fn()}finally{setBusy(el,false);scheduleRefresh(0)}}
async function api(url,body,ms){const ctl=new AbortController();
 const to=setTimeout(()=>ctl.abort(),ms||15000);
 try{const r=await fetch(url,{method:body?'POST':'GET',signal:ctl.signal,cache:'no-store',
  headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):null});
  clearTimeout(to);const txt=await r.text();let data=null;
  if((r.headers.get('content-type')||'').includes('application/json')){
    try{data=txt?JSON.parse(txt):{}}catch(e){throw new Error('JSON invalido: '+e.message)}
  }else{
    data={ok:false,msg:(txt||'').replace(/<[^>]+>/g,' ').replace(/\\s+/g,' ').trim().slice(0,240)}
  }
  if(!r.ok)throw new Error(data.msg||data.respuesta||('HTTP '+r.status));
 return data}
 catch(e){clearTimeout(to);throw e}}
function setBtn(id,opts){
 const b=document.getElementById(id);if(!b)return;
 const show=opts.show!==false;b.style.display=show?'':'none';
 if(opts.label!==undefined)b.textContent=opts.label;
 b.disabled=!!opts.disabled;
 b.classList.toggle('is-protagonist',!!opts.primary);
 b.classList.toggle('soft-action',!!opts.soft);
}
function setControlDisabled(id,disabled){const el=document.getElementById(id);if(el)el.disabled=!!disabled}
function setOperativoClase(key){
 const box=document.getElementById('opstatus');if(!box)return;
 box.className='operational-status is-'+(key||'sin_clase');
}
function prepararInicio(tipo){
 const inicio=document.getElementById('inicio');
 if(!inicio)return;
 inicio.classList.add('show-start-form');
 if(tipo){
  const it=document.getElementById('itipo');
  if(it){it.value=tipo;cambioTipo()}
 }
 if(tipo==='continuar'&&LAST_STATE&&LAST_STATE.clase_id){
  const sel=document.getElementById('iclase');
  if(sel&&[...sel.options].some(o=>o.value===LAST_STATE.clase_id))sel.value=LAST_STATE.clase_id;
 }
 inicio.scrollIntoView({behavior:'smooth',block:'start'});
 const foco=tipo==='continuar'?document.getElementById('iclase'):document.getElementById('itema');
 if(foco)setTimeout(()=>foco.focus(),220);
}
function aplicarEstadoOperativo(s,landing,visorSinClase){
 const key=s.post_estado||s.estado||'sin_clase';
 const fin=s.estado==='fin';
 const sinClase=s.estado==='sin clase';
 const efectivo=s.estado_efectivo||'desconocido';
 const controles=s.controles||{};
 const grabando=efectivo==='grabando_activo';
 const pausado=['pausado_manual','auto_pausado','audio_requires_attention'].includes(efectivo);
  const consolidando=key==='consolidando';
  const cierrePendiente=s.shutdown_blocking!==undefined
   ?!!s.shutdown_blocking
   :['finalizando','pendiente_cierre','pendiente_consolidar'].includes(s.shutdown_state||'');
 const consolidada=fin&&s.leccion_final_actualizada&&!consolidando&&key!=='error';
 const pendiente=fin&&!consolidada&&!consolidando;
 setOperativoClase(key);
 document.getElementById('op_title').textContent=landing?'Listo para empezar':(s.post_titulo||s.estado||'Sin clase');
 document.getElementById('op_hint').textContent=landing?'Selecciona curso y sesión para empezar.':(s.post_mensaje||'');
	 const audioWarn=document.getElementById('audioWarning');
	 if(audioWarn){
	  const partes=[
	   s.audio_action_required?s.audio_state_detail:'',
	   s.audio_warning||'',s.audio_hint||''
	  ].filter(Boolean);
	  audioWarn.textContent=partes.join('\\n');
	  audioWarn.title=s.audio_diag||partes.join(' ');
	  audioWarn.style.display=(!landing&&!visorSinClase&&(s.estado==='grabando'||s.estado==='pausado')&&partes.length)?'':'none';
	 }
	 const runtimeHealth=document.getElementById('runtimeHealth');
	 if(runtimeHealth){
	  runtimeHealth.textContent=s.runtime_health_latest||'';
	  runtimeHealth.style.display=(!landing&&!visorSinClase&&runtimeHealth.textContent)?'':'none';
	 }
	 setBtn('btnPausa',{disabled:landing||visorSinClase||sinClase||!controles.pausar||consolidando});
 setBtn('btnSeguir',{disabled:landing||visorSinClase||sinClase||!controles.seguir||consolidando,
  label:controles.seguir_label||(
   efectivo==='audio_requires_attention'?'Reanudar / probar audio':'Seguir')});
 setBtn('btnParte',{disabled:landing||visorSinClase||sinClase||!controles.cerrar_parte||consolidando});
 setBtn('btnFin',{disabled:landing||visorSinClase||sinClase||!controles.finalizar||consolidando});
  const puedeConsolidar=fin&&!cierrePendiente&&!consolidando&&!consolidada;
 setBtn('btnConsolidar',{
  show:!consolidada,
 disabled:landing||visorSinClase||sinClase||!puedeConsolidar,
  label:consolidando?'Consolidando…':(key==='error'?'Reintentar consolidación':'Consolidar lección final'),
  primary:pendiente||key==='error',
  soft:!fin
 });
 setBtn('btnAbrirFinal',{show:consolidada,disabled:false,label:'Abrir lección final',primary:true});
 setBtn('btnCompactDry',{show:consolidada,disabled:false,label:'Compactar clase (dry-run)',soft:true});
 const bloquearRuntime=landing||visorSinClase||sinClase||fin||consolidando;
 ['selv','self','seld','selm','parteNota'].forEach(id=>setControlDisabled(id,bloquearRuntime));
 setBtn('btnRegion',{disabled:bloquearRuntime});
 const resumenActivo=['pendiente','generando'].includes(s.resumen_estado||'');
 const resumenBtn=document.getElementById('btnResumen');
 if(resumenBtn){
  resumenBtn.disabled=landing||visorSinClase||sinClase||(!grabando&&!pausado)||consolidando||resumenActivo;
  if(!resumenBtn.classList.contains('busy'))resumenBtn.textContent=resumenActivo?'Generando resumen...':'Resumir tramo ahora';
 }
 const resumenStatus=document.getElementById('resumenStatus');
 if(resumenStatus){
 resumenStatus.textContent=(!landing&&!visorSinClase&&(s.resumen_msg||resumenActivo))?(s.resumen_msg||'generando resumen...'):'';
  resumenStatus.classList.toggle('is-active',!landing&&!visorSinClase&&!!resumenStatus.textContent);
  resumenStatus.classList.toggle('is-warning',(s.resumen_estado||'')==='parcial'||(s.resumen_estado||'')==='error');
 }
 if(resumenActivo)refrescoRapido(1200);
 const post=document.getElementById('secpost');
 const mostrarPost=!landing&&!visorSinClase&&s.estado!=='sin clase';
 if(post){
  post.style.display=mostrarPost?'':'none';
  post.classList.toggle('post-active',consolidando);
  post.classList.toggle('post-compact',!consolidando&&!(s.post_log||'').trim());
 }
 const legacy=document.getElementById('legacyWarning');
 if(legacy){
  legacy.textContent=s.contenido_legacy_msg||'';
  legacy.style.display=(!landing&&!visorSinClase&&legacy.textContent)?'':'none';
 }
 const evid=document.getElementById('evidenciaInfo');
 if(evid)evid.textContent=(!landing&&!visorSinClase&&s.evidencia_resumen)?s.evidencia_resumen:'';
 const ps=document.getElementById('poststate');
 if(ps)ps.textContent=landing?'Post-proceso disponible al finalizar la clase.':(s.post_mensaje||'Listo.');
 const pl=document.getElementById('postlog');
 if(pl){
  const postText=(s.post_log||'').trim()||(
   fin?(consolidada?'Lección consolidada.':'Sin líneas de post-proceso todavía.'): 'El log aparecerá al consolidar.'
  );
  renderFollowingText(pl,postText,'postlog');
 }
 if(visorSinClase){
  document.getElementById('op_title').textContent='Panel en modo visor';
  document.getElementById('op_hint').textContent=s.modo_msg||VIEWER_MSG;
 }
}
async function refrescar(force){
 if(REFRESH_IN_FLIGHT){REFRESH_PENDING=true;return}
 clearTimeout(REFRESH_TIMER);REFRESH_IN_FLIGHT=true;
 try{
 const s=await api('/api/estado');
 LAST_STATE=s;
 anclarRelojLocal(s);
 const studentNotes=document.getElementById('studentNotes');
 if(studentNotes)studentNotes.style.display=s.student_notes_enabled===false?'none':'';
 const sinClaseReal=s.estado==='sin clase';
 const claseFinal=s.estado==='fin';
 if(!s.modo_app||(!sinClaseReal&&!claseFinal))SESION_VISUAL=true;
 const landing=(s.modo_app&&sinClaseReal);
 const visorSinClase=(!s.modo_app&&sinClaseReal);
 const campos={estado:s.post_titulo||s.estado+(s.auto_pausa?' silencio':''),parte:s.parte,
   curso:s.curso||'-',tema:s.tema||'-',tema_focus:s.tema||'(sin tema activo)',
   fuente:s.fuente,amodo:s.audio_ruta||s.audio_modo||'-',monitor_audio:s.audio_ruta_detalle||s.fuente_audio||s.audio_modo||s.fuente||'-',
   audioestado:s.audio_state_label||'-',sttestado:s.stt_state_label||'-',
   vel:(s.velocidad=='-'?'-':s.velocidad+'x'),monitor_chip:s.monitor?('P'+s.monitor):'-',
   enf:s.enfoque?'ON':'off',nseg:s.segmentos,costo:s.costo_display||('$'+s.costo),
   duracion:s.duracion||'-',tiempo_pared:s.elapsed_wall||'-',tiempo_pausa:s.paused_elapsed||'-',
   transcrito_hasta:s.stt_transcribed_until||'-',retraso_stt:s.stt_delay||'-',
   tiempo_video:s.video_equivalent_elapsed||'-',inicio_abs:s.inicio_abs||'-',modo_tiempo:s.modo_tiempo||'-',
   ahora_abs:s.ahora_rel||s.ahora_abs||'-',parte_inicio_abs:s.parte_inicio_rel||s.parte_inicio_abs||'-',
   stt_hardware:s.stt_hardware_label||'-',
   ultimo_segmento_abs:s.ultimo_segmento_rel||s.ultimo_segmento_abs||'-'};
 if(landing)Object.assign(campos,{estado:'listo',parte:'-',curso:'-',tema:'-',
   tema_focus:'-',fuente:'-',amodo:'-',monitor_audio:'-',audioestado:'-',sttestado:'-',vel:'-',
   monitor_chip:'-',enf:'-',nseg:'0',costo:s.costo_display||'Sesión $0.000 · Curso $0.000',
    duracion:'-',tiempo_pared:'-',tiempo_pausa:'-',tiempo_video:'-',transcrito_hasta:'-',retraso_stt:'-',
    inicio_abs:'-',modo_tiempo:'-',parte_inicio_abs:'-',ultimo_segmento_abs:'-',stt_hardware:'-'});
  for(const[k,v]of Object.entries(campos)){
   const el=document.getElementById(k);if(el)el.textContent=v}
  const chipVideo=document.getElementById('chipvideo');
  if(chipVideo)chipVideo.style.display=(!landing&&Number(s.velocidad||1)>1)?'':'none';
 const costLabel=document.getElementById('costolabel');
 if(costLabel)costLabel.textContent=s.costo_etiqueta||'Gasto API';
 const costChip=document.getElementById('chipcosto');
 if(costChip)costChip.title=landing?'El contador visible inicia en 0 al crear o continuar un curso.':(s.costo_nota||'');
 const audioChip=document.getElementById('chipaudio');
 if(audioChip)audioChip.title=landing?'':[
  s.audio_state_detail||'',s.audio_ruta_detalle||s.fuente_audio||s.audio_modo||''
 ].filter(Boolean).join(' · ');
 const leccion=document.getElementById('leccion');
 const uc=document.getElementById('ultcap');
 const capFrame=document.querySelector('.capture-frame');
 if(landing){
  leccion.classList.add('is-empty');
  leccion.innerHTML='<div class="landing-empty"><svg class="empty-logo" viewBox="0 0 80 80" aria-hidden="true"><defs><linearGradient id="cvEmptyGrad" x1="8" y1="8" x2="72" y2="72" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#2BC9E9"/><stop offset=".48" stop-color="#ECC2FF"/><stop offset="1" stop-color="#F9E1C8"/></linearGradient></defs><rect x="9" y="9" width="62" height="62" rx="20" fill="url(#cvEmptyGrad)"/><path d="M22 50c7-7 18-7 25 0 4-4 9-5 14-4v9c-5-1-10 0-14 4-7-5-18-5-25 0z" fill="rgba(255,255,255,.72)"/><path d="M24 30h10m-10 8h16M48 23v21m8-15v10m-17-6v16" stroke="#172033" stroke-width="3.2" stroke-linecap="round"/><path d="M23 57c9-6 20-6 29 0" stroke="#172033" stroke-width="2.7" stroke-linecap="round" fill="none"/></svg><span class="empty-copy"><b>Selecciona curso y sesión para empezar</b><span>El panel queda limpio hasta abrir una clase.</span></span></div>';
  document.getElementById('transdiag').textContent='Evidencia lista';
  uc.style.display='none';uc.removeAttribute('src');
  if(capFrame)capFrame.classList.add('is-empty');
  document.getElementById('sincap').style.display='';
  document.getElementById('sincap').textContent='Selecciona curso y sesión para empezar';
  document.getElementById('capdate').textContent='';
  document.getElementById('cursohtml').innerHTML='(sin sesión seleccionada)';
 }else{
  leccion.classList.remove('is-empty');
  const lessonContext=[s.curso||'',s.clase_id||'',s.parte||''].join('/');
  renderScrollableHtml(
   leccion,s.leccion_html||'(sin clase activa)','lesson',lessonContext);
  document.getElementById('transdiag').textContent=s.trans_diag||'';
  if(s.ultima_captura){if(!uc.src.endsWith(s.ultima_captura))uc.src='/img/'+s.ultima_captura;
   uc.style.display='';document.getElementById('sincap').style.display='none';
   document.getElementById('capdate').textContent=s.ultima_captura_abs||''}
  else{uc.style.display='none';document.getElementById('sincap').style.display='';
   document.getElementById('sincap').textContent='(aún sin capturas)';
   document.getElementById('capdate').textContent=''}
  if(capFrame)capFrame.classList.toggle('is-empty',!s.ultima_captura);
  if(s.curso_html!==undefined){
   const cursoEl=document.getElementById('cursohtml');
   renderScrollableHtml(
    cursoEl,s.curso_html||'(aún sin bitácora)','course',s.curso||'');
  }
 }
 document.getElementById('selv').value=s.velocidad;document.getElementById('self').value=s.fuente;
  const selm=document.getElementById('selm');
  if(s.monitores)fillSourceSelect('selm',s.monitores,'idx','etiqueta',s.monitor);
  if(s.monitor&&selm.value!=s.monitor)selm.value=s.monitor;
  const seld=document.getElementById('seld');
  if(s.dispositivos)fillSourceSelect('seld',s.dispositivos,'id','etiqueta',s.dispositivo);
 if(seld&&s.dispositivo!==undefined&&seld.value!=s.dispositivo)seld.value=s.dispositivo;
 document.getElementById('bcerrar').style.display=s.modo_app?'':'none';
 const ch=document.getElementById('chiphijos');
 const hijosEsperados=(s.hijos_esperados_activos===undefined?s.hijos_esperados:s.hijos_esperados_activos);
 if(s.modo_app&&hijosEsperados>0&&s.hijos<hijosEsperados){
   ch.style.display='';document.getElementById('hijos').textContent=s.hijos+'/'+hijosEsperados+' procesos';}
 else ch.style.display='none';
 const inicio=document.getElementById('inicio');
 inicio.style.display=(s.modo_app||visorSinClase)?'':'none';
 const finVisible=s.estado==='fin'&&!landing;
 inicio.classList.toggle('viewer-start',visorSinClase);
 inicio.classList.toggle('compact-start',!landing&&!finVisible&&!visorSinClase);
 inicio.classList.toggle('finish-start',finVisible);
 if(!finVisible||visorSinClase)inicio.classList.remove('show-start-form');
 const finishActions=document.getElementById('finishActions');
 if(finishActions)finishActions.style.display=(finVisible&&!visorSinClase)?'':'none';
 const viewerNotice=document.getElementById('viewerNotice');
 if(viewerNotice){viewerNotice.textContent=s.modo_msg||VIEWER_MSG;viewerNotice.style.display=visorSinClase?'':'none'}
 const inicioKicker=document.getElementById('inicio_kicker');
 if(inicioKicker)inicioKicker.textContent=landing?'curso, pantalla y audio':(finVisible?'nueva clase, continuidad o cambio de sesión':'sesión cargada');
 if(inicioKicker&&visorSinClase)inicioKicker.textContent='modo visor';
 const controles=document.getElementById('controles');
 if(controles)controles.style.display=(landing||visorSinClase)?'none':'';
 aplicarEstadoOperativo(s,landing,visorSinClase);
 if(!sinClaseReal){loadStudentNotes(false);loadLocalInterventions(false)}
  if(s.modo_app&&!INFO)cargarInfo();
 }catch(e){}finally{
  REFRESH_IN_FLIGHT=false;
  const pending=REFRESH_PENDING;REFRESH_PENDING=false;
  scheduleRefresh(pending?0:refreshDelay());
 }}
function fillSourceSelect(id,items,key,label,preferred){
 const select=document.getElementById(id);if(!select)return;
 const wanted=String(preferred!==undefined&&preferred!==null?preferred:select.value||'');
 const signature=JSON.stringify((items||[]).map(item=>[String(item[key]),String(item[label]||'')]));
 if(select.dataset.sourceSignature!==signature){
  select.replaceChildren();for(const item of (items||[])){
   const option=document.createElement('option');option.value=String(item[key]);
   option.textContent=String(item[label]||item[key]);select.appendChild(option)}
  select.dataset.sourceSignature=signature;
 }
 if(wanted&&[...select.options].some(option=>option.value===wanted))select.value=wanted;
}
async function refreshSources(button){return runBusy(button,'Actualizando...',async()=>{try{
 const data=await api('/api/recargar_fuentes',{});
 const selectedMonitor=(LAST_STATE&&LAST_STATE.monitor)||document.getElementById('selm').value;
 const selectedDevice=(LAST_STATE&&LAST_STATE.dispositivo)||document.getElementById('seld').value;
 fillSourceSelect('imon',data.monitores,'idx','etiqueta',document.getElementById('imon').value);
 fillSourceSelect('selm',data.monitores,'idx','etiqueta',selectedMonitor);
 fillSourceSelect('idispositivo',data.dispositivos,'id','etiqueta',document.getElementById('idispositivo').value);
 fillSourceSelect('seld',data.dispositivos,'id','etiqueta',selectedDevice);
 if(INFO){INFO.monitores=data.monitores;INFO.dispositivos=data.dispositivos}
 toast(data.msg||'Fuentes actualizadas');
 }catch(e){toast('No se pudieron actualizar las fuentes: '+e.message,true)}})}
async function cargarInfo(force){try{if(INFO&&!force)return;INFO=await api('/api/inicio_info');
 if((INFO.recovered_deletions||[]).length)toast('Restaurada(s) desde Papelera: '+INFO.recovered_deletions.join(', '));
 const c=document.getElementById('icurso');
 c.replaceChildren();for(const name of INFO.cursos){const option=document.createElement('option');option.value=name;option.textContent=name;c.appendChild(option)}
 const nuevo=document.createElement('option');nuevo.value='__nuevo__';nuevo.textContent='curso nuevo...';c.appendChild(nuevo);
  fillSourceSelect('imon',INFO.monitores,'idx','etiqueta');
  fillSourceSelect('idispositivo',INFO.dispositivos,'id','etiqueta');
 const backend=document.getElementById('isttbackend');if(backend&&INFO.stt_backend)backend.value=INFO.stt_backend;
 sttBackendChanged();cambioCurso()}catch(e){toast('no pude cargar cursos: '+e,true)}}
function cambioCurso(){const v=document.getElementById('icurso').value;
 document.getElementById('campo_curso_nuevo').style.display=(v=='__nuevo__')?'':'none';cambioTipo();updateContentAdmin()}
function cambioTipo(){const t=document.getElementById('itipo').value;
 const sel=document.getElementById('iclase');const curso=document.getElementById('icurso').value;
 const lista=(INFO&&INFO.clases[curso])||[];
 document.getElementById('campo_clase').style.display=(t=='continuar')?'':'none';
 sel.replaceChildren();for(const name of lista){const option=document.createElement('option');option.value=name;option.textContent=name;sel.appendChild(option)}
 if(t=='continuar'&&lista.length)sel.value=lista[lista.length-1];sel.onchange=updateContentAdmin;updateContentAdmin()}
function updateContentAdmin(){const course=document.getElementById('icurso').value;
 const classId=document.getElementById('iclase').value;const known=!!(INFO&&(INFO.deletable_courses||[]).includes(course));
 const courseButton=document.getElementById('deleteCourseButton');const classButton=document.getElementById('deleteClassButton');
 if(courseButton)courseButton.disabled=!known;if(classButton)classButton.disabled=!known||!classId}
function humanBytes(value){let size=Math.max(0,Number(value)||0);const units=['B','KB','MB','GB','TB'];let i=0;
 while(size>=1024&&i<units.length-1){size/=1024;i++}return (i?size.toFixed(size>=10?1:2):String(Math.round(size)))+' '+units[i]}
async function deleteRecordedContent(kind,button){return runBusy(button,'Preparando...',async()=>{try{
 const course=document.getElementById('icurso').value;const classId=kind==='class'?document.getElementById('iclase').value:'';
 const preview=await api('/api/contenido/vista_previa_eliminar',{kind:kind,course:course,class_id:classId});
 const subject=kind==='course'?('el curso '+preview.confirmation):('la clase '+preview.confirmation);
 const detail=preview.file_count+' archivo(s), '+humanBytes(preview.byte_count)+(kind==='course'?', '+preview.class_count+' clase(s)':'');
 const typed=prompt('Se moverá '+subject+' a la Papelera ('+detail+').\\n\\nEscribe exactamente «'+preview.confirmation+'» para confirmar:','');
 if(typed===null){toast('Operación cancelada');return}if(typed!==preview.confirmation){toast('El nombre no coincide; no se movió nada.',true);return}
 const result=await api('/api/contenido/eliminar',{kind:kind,course:course,class_id:classId,
  confirmation:typed,fingerprint:preview.fingerprint},120000);
 toast(result.msg,!result.ok);INFO=null;await cargarInfo(true);refrescar(true)
 }catch(e){toast('No se pudo mover a la Papelera: '+e.message,true)}})}
function sttBackendChanged(){const select=document.getElementById('isttbackend');
  const label=document.getElementById('sttConsentLabel');if(label)label.style.display=''}
async function iniciarClase(el){return runBusy(el,'Empezando...',async()=>{try{
 const s=await api('/api/iniciar',{curso:document.getElementById('icurso').value,
  curso_nuevo:document.getElementById('icursonuevo').value,
  tipo:document.getElementById('itipo').value,clase:document.getElementById('iclase').value,
  tema:document.getElementById('itema').value,monitor:document.getElementById('imon').value,
  fuente:document.getElementById('ifuente').value,
  dispositivo:document.getElementById('idispositivo').value,
  stt_backend:document.getElementById('isttbackend').value,
  stt_remote_consent:document.getElementById('isttconsent').checked});
 if(s.ok!==false)SESION_VISUAL=true;
 toast(s.msg,!s.ok);INFO=null;refrescar()}catch(e){toast('error iniciando: '+e,true)}})}
async function cmd(c,el){return runBusy(el,'Aplicando...',async()=>{try{
 if(!(await localInterventionBeforeCommand(c)))return;
 const s=await api('/api/comando',{cmd:c});
 toast(s.msg,!s.ok);refrescar()}catch(e){toast('sin respuesta del panel: '+e,true)}})}
async function pedirResumen(el){return runBusy(el,'Solicitando...',async()=>{try{
 const s=await api('/api/comando',{cmd:'resumir'});
 if(s.ok){toast('Resumen solicitado');refrescoRapido(900)}
 else toast(s.msg||'No se pudo solicitar el resumen',true);
 refrescar()}catch(e){toast('sin respuesta del panel: '+e,true)}})}
async function cerrarParte(el){const i=document.getElementById('parteNota');const nota=i.value.trim();await cmd('parte '+nota,el);if(nota)i.value=''}
async function consolidar(el){return runBusy(el,'Consolidando...',async()=>{try{
 let s=await api('/api/consolidar',{});
 if(s.requires_confirmation){
  if(!confirm(s.msg)){toast('Consolidación cancelada: falta evidencia de audio.',true);return}
  s=await api('/api/consolidar',{confirmar_sin_evidencia:true});
 }
 toast(s.msg,!s.ok);refrescar()}catch(e){toast('error: '+e,true)}})}
async function compactDry(el){return runBusy(el,'Calculando...',async()=>{try{
 const s=await api('/api/compactar_dry_run',{});
 const pl=document.getElementById('postlog');
 if(pl&&s.resumen)pl.textContent=s.resumen;
 toast(s.msg,!s.ok);refrescar()}catch(e){toast('error dry-run: '+e,true)}})}
async function cerrarApp(){
 document.body.innerHTML='<h2 style="padding:40px">Cerrando ClaseViva… esperando audio, STT y procesos propios.</h2>';
 try{await api('/api/cerrar_app',{})}catch(e){}
 for(let i=0;i<180;i++){
  try{
   const r=await fetch('/api/estado',{cache:'no-store'});
   if(r.ok){const s=await r.json();if(s.shutdown_state==='finalizada'||s.shutdown_blocking===false)break}
  }catch(e){break}
  await new Promise(resolve=>setTimeout(resolve,500));
 }
 document.body.innerHTML='<h2 style="padding:40px">Aplicación cerrada. Puedes cerrar esta pestaña.</h2>'
}
function autoresizePregunta(){
 const i=document.getElementById('preg');if(!i)return;
 const bar=i.closest('.chatbar');
 i.style.height='auto';
 const base=38,max=170,h=Math.min(max,Math.max(base,i.scrollHeight));
 i.style.height=h+'px';
 if(bar)bar.classList.toggle('is-expanded',!!i.value&&(i.scrollHeight>base+12||i.value.length>90));
}
function resetPregunta(){
 const i=document.getElementById('preg');if(!i)return;
 i.value='';i.style.height='';
 const bar=i.closest('.chatbar');if(bar)bar.classList.remove('is-expanded');
}
async function enviarPregunta(el){
 const i=document.getElementById('preg');const q=i?i.value:'';
 await preguntar(q,el);resetPregunta();
}
async function preguntar(q,el){q=(q||'').trim();if(!q&&!CAPTURA)return;return runBusy(el,'Pensando...',async()=>{const ch=document.getElementById('chat');
 const cap=CAPTURA;CAPTURA=null;const preview=document.getElementById('cap_previa');
 preview.replaceChildren();preview.style.display='none';
 ch.innerHTML+='<div class="q">'+(cap?'<img class="cap" src="/img/'+cap+'">':'')+
  esc(q||'Explica la captura')+'</div><div class="a md">pensando...</div>';
 ch.scrollTop=ch.scrollHeight;
 try{const s=await api('/api/pregunta',{q:q,captura:cap||'',
   web:document.getElementById('webchk').checked},120000);
  if(s.ok===false)throw new Error(s.respuesta||s.msg||'error del copiloto');
  ch.lastElementChild.innerHTML=s.respuesta_html||esc(s.respuesta);
  decorateMarkdown(ch.lastElementChild)}
 catch(e){ch.lastElementChild.textContent='error: '+e}
 ch.scrollTop=ch.scrollHeight})}
async function descartarCapturaPrevia(silent){
 const nombre=CAPTURA;if(!nombre)return;
 try{await api('/api/captura_previa/descartar',{captura:nombre});CAPTURA=null;
  const p=document.getElementById('cap_previa');p.replaceChildren();p.style.display='none';
  if(!silent)toast('Captura descartada')
 }catch(e){if(!silent)toast('No se pudo descartar: '+e.message,true)}
}
async function capturarPantalla(el){return runBusy(el,'Capturando...',async()=>{try{
 if(CAPTURA)await descartarCapturaPrevia(true);
 const s=await api('/api/captura_previa',{},30000);
 if(s.ok===false)throw new Error(s.msg||'no pude capturar');
 CAPTURA=s.captura;const p=document.getElementById('cap_previa');
 p.replaceChildren();const img=document.createElement('img');img.src='/img/'+CAPTURA;
 const label=document.createElement('span');label.textContent='captura adjunta a la próxima pregunta';
 const discard=document.createElement('button');discard.className='rojo';discard.textContent='Descartar';
 discard.onclick=()=>descartarCapturaPrevia(false);p.append(img,label,discard);
 p.style.display='flex';toast('captura congelada')}
 catch(e){toast('no pude capturar: '+e,true)}})}
async function notaCurso(el){const i=document.getElementById('cursonota');const n=i.value.trim();
 if(!n)return;return runBusy(el,'Añadiendo...',async()=>{try{const s=await api('/api/curso_nota',{nota:n});toast(s.msg,!s.ok);i.value='';refrescar()}
 catch(e){toast('error: '+e,true)}})}
let STUDENT_NOTES_SIGNATURE='';
let STUDENT_NOTES_LAST_FETCH=0;
let STUDENT_NOTE_RECORDER=null;
let STUDENT_NOTE_STREAM=null;
let STUDENT_NOTE_CHUNKS=[];
let STUDENT_NOTE_DRAFT=null;
let LOCAL_INTERVENTION={draftId:null,status:'idle',recorder:null,stream:null,
 sequence:0,upload:Promise.resolve(),stopPromise:null,stopResolve:null};
let LOCAL_INTERVENTION_LAST_FETCH=0;
function localInterventionStatus(message,recording){const node=document.getElementById('localInterventionStatus');
 if(!node)return;node.textContent=message;node.classList.toggle('is-recording',!!recording)}
function localInterventionButtons(recording){const start=document.getElementById('localInterventionStart');
 const stop=document.getElementById('localInterventionStop'),refresh=document.getElementById('localMicRefresh');
 const select=document.getElementById('localMicSelect');if(start)start.disabled=!!recording;
 if(stop)stop.disabled=!recording;if(refresh)refresh.disabled=!!recording;if(select)select.disabled=!!recording}
async function refreshInterventionMics(button){return runBusy(button,'Buscando...',async()=>{
 let permissionStream=null;try{
  permissionStream=await navigator.mediaDevices.getUserMedia({audio:true});
  const devices=(await navigator.mediaDevices.enumerateDevices()).filter(item=>item.kind==='audioinput');
  const select=document.getElementById('localMicSelect'),wanted=select.value;select.replaceChildren();
  for(const [index,item]of devices.entries()){const option=document.createElement('option');
   option.value=item.deviceId;option.textContent=item.label||('Micrófono '+(index+1));select.appendChild(option)}
  if(wanted&&devices.some(item=>item.deviceId===wanted))select.value=wanted;
  localInterventionStatus(devices.length?'Elige el micrófono y pulsa Iniciar.':'No se detectaron micrófonos.',false)
 }catch(e){toast('No se pudieron enumerar micrófonos: '+e.message,true)
 }finally{if(permissionStream)permissionStream.getTracks().forEach(track=>track.stop())}})}
async function localInterventionStart(button){if(LOCAL_INTERVENTION.draftId){
 toast('Guarda o descarta la intervención pendiente.',true);return}
 const select=document.getElementById('localMicSelect');if(!select.value){await refreshInterventionMics(button)}
 const selected=select.value;if(!selected){toast('Selecciona un micrófono.',true);return}
 let createdDraftId=null;
 try{
  const stream=await navigator.mediaDevices.getUserMedia({audio:{deviceId:{exact:selected},
   echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
  const track=stream.getAudioTracks()[0],settings=track.getSettings?track.getSettings():{};
  const label=track.label||select.options[select.selectedIndex]?.textContent||'Micrófono seleccionado';
  let mime='audio/webm;codecs=opus';if(!window.MediaRecorder||
   (MediaRecorder.isTypeSupported&&!MediaRecorder.isTypeSupported(mime)))mime='audio/webm';
  const created=await studentNoteRequest('/api/local_interventions',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({mime:mime,device_id:settings.deviceId||selected,
   device_label:label,class_seconds:Number((LAST_STATE&&LAST_STATE.recorded_elapsed_sec)||0)})});
  createdDraftId=created.draft.draft_id;
  const recorder=new MediaRecorder(stream,mime?{mimeType:mime}:undefined);
  LOCAL_INTERVENTION={draftId:created.draft.draft_id,status:'recording',recorder:recorder,
   stream:stream,sequence:0,upload:Promise.resolve(),stopPromise:null,stopResolve:null};
  recorder.ondataavailable=event=>{if(!event.data||!event.data.size)return;
   const sequence=LOCAL_INTERVENTION.sequence++;LOCAL_INTERVENTION.upload=LOCAL_INTERVENTION.upload.then(async()=>{
    const response=await fetch('/api/local_interventions/'+encodeURIComponent(LOCAL_INTERVENTION.draftId)+
     '/chunks/'+sequence,{method:'POST',headers:{'Content-Type':event.data.type||mime},body:event.data});
    const data=await response.json().catch(()=>({ok:false,msg:'respuesta inválida'}));
    if(!response.ok||data.ok===false)throw new Error(data.msg||('HTTP '+response.status))})};
  recorder.onstop=async()=>{try{await LOCAL_INTERVENTION.upload;
    const sealed=await studentNoteRequest('/api/local_interventions/'+encodeURIComponent(LOCAL_INTERVENTION.draftId)+'/seal',{method:'POST'});
    LOCAL_INTERVENTION.status='sealed';const preview=document.getElementById('localInterventionPreview');
    const audio=document.getElementById('localInterventionAudio');audio.src='/api/local_interventions/'+
     encodeURIComponent(LOCAL_INTERVENTION.draftId)+'/preview?sha='+encodeURIComponent(sealed.draft.sha256||'');
    preview.style.display='';localInterventionStatus('Intervención detenida. Revísala y decide Guardar o Descartar.',false)
   }catch(e){LOCAL_INTERVENTION.status='error';localInterventionStatus('No se pudo sellar: '+e.message,false);toast(e.message,true)
   }finally{if(LOCAL_INTERVENTION.stream)LOCAL_INTERVENTION.stream.getTracks().forEach(item=>item.stop());
    LOCAL_INTERVENTION.stream=null;localInterventionButtons(false);
    if(LOCAL_INTERVENTION.stopResolve){LOCAL_INTERVENTION.stopResolve();LOCAL_INTERVENTION.stopResolve=null}}};
  track.onended=()=>{if(LOCAL_INTERVENTION.status==='recording')localInterventionStop()};
  recorder.start(2000);localInterventionButtons(true);
  localInterventionStatus('Grabando solo tu intervención desde '+label+'…',true)
 }catch(e){if(LOCAL_INTERVENTION.stream)LOCAL_INTERVENTION.stream.getTracks().forEach(track=>track.stop());
  if(createdDraftId){try{await studentNoteRequest('/api/local_interventions/'+
   encodeURIComponent(createdDraftId)+'/discard',{method:'POST'})}catch(_cleanupError){}}
  LOCAL_INTERVENTION={draftId:null,status:'idle',recorder:null,stream:null,sequence:0,
   upload:Promise.resolve(),stopPromise:null,stopResolve:null};localInterventionButtons(false);
  toast('Micrófono no disponible: '+e.message,true)}}
async function localInterventionStop(){if(LOCAL_INTERVENTION.status!=='recording'||!LOCAL_INTERVENTION.recorder)return;
 if(LOCAL_INTERVENTION.stopPromise)return LOCAL_INTERVENTION.stopPromise;
 LOCAL_INTERVENTION.status='stopping';LOCAL_INTERVENTION.stopPromise=new Promise(resolve=>{LOCAL_INTERVENTION.stopResolve=resolve});
 LOCAL_INTERVENTION.recorder.stop();localInterventionStatus('Deteniendo y verificando fragmentos…',false);
 return LOCAL_INTERVENTION.stopPromise}
function localInterventionReset(){const audio=document.getElementById('localInterventionAudio');if(audio){audio.pause();audio.removeAttribute('src')}
 const preview=document.getElementById('localInterventionPreview');if(preview)preview.style.display='none';
 LOCAL_INTERVENTION={draftId:null,status:'idle',recorder:null,stream:null,sequence:0,
  upload:Promise.resolve(),stopPromise:null,stopResolve:null};localInterventionButtons(false);
 localInterventionStatus('No se captura el ambiente: solo se graba entre Iniciar y Detener.',false)}
async function localInterventionSave(button){if(!LOCAL_INTERVENTION.draftId||LOCAL_INTERVENTION.status!=='sealed')return;
 return runBusy(button,'Guardando...',async()=>{try{const data=await studentNoteRequest('/api/local_interventions/'+
  encodeURIComponent(LOCAL_INTERVENTION.draftId)+'/commit',{method:'POST'});localInterventionReset();
  STUDENT_NOTES_SIGNATURE='';await loadStudentNotes(true);toast(data.msg||'Intervención incluida')
 }catch(e){toast('No se pudo guardar: '+e.message,true)}})}
async function localInterventionDiscard(button){if(!LOCAL_INTERVENTION.draftId)return;
 if(LOCAL_INTERVENTION.status==='recording'||LOCAL_INTERVENTION.status==='stopping')await localInterventionStop();
 return runBusy(button,'Descartando...',async()=>{try{await studentNoteRequest('/api/local_interventions/'+
  encodeURIComponent(LOCAL_INTERVENTION.draftId)+'/discard',{method:'POST'});localInterventionReset();
  toast('Intervención descartada; no se publicará')
 }catch(e){toast('No se pudo descartar: '+e.message,true)}})}
async function loadLocalInterventions(force){const now=Date.now();if(!force&&now-LOCAL_INTERVENTION_LAST_FETCH<5000)return;
 LOCAL_INTERVENTION_LAST_FETCH=now;if(LOCAL_INTERVENTION.status==='recording'||LOCAL_INTERVENTION.status==='stopping')return;
 try{const data=await studentNoteRequest('/api/local_interventions',{cache:'no-store'}),draft=(data.drafts||[])[0];
  if(!draft){if(!LOCAL_INTERVENTION.draftId)localInterventionReset();return}
  LOCAL_INTERVENTION.draftId=draft.draft_id;LOCAL_INTERVENTION.status=draft.status;
  const preview=document.getElementById('localInterventionPreview');preview.style.display='';
  if(draft.status==='sealed'){document.getElementById('localInterventionAudio').src='/api/local_interventions/'+
   encodeURIComponent(draft.draft_id)+'/preview?sha='+encodeURIComponent(draft.sha256||'');
   localInterventionStatus('Borrador recuperado. Guarda o descarta antes de finalizar.',false)
  }else{localInterventionStatus('Grabación interrumpida al recargar. Descártala y vuelve a iniciar.',false)}
 }catch(e){}}
async function localInterventionBeforeCommand(command){const action=String(command||'').split(' ')[0];
 if(!['pausa','fin'].includes(action))return true;
 if(LOCAL_INTERVENTION.status==='recording'||LOCAL_INTERVENTION.status==='stopping')await localInterventionStop();
 if(action==='fin'&&LOCAL_INTERVENTION.draftId){toast('Antes de Fin, guarda o descarta tu intervención pendiente.',true);return false}
 return true}
window.addEventListener('pagehide',()=>{const id=LOCAL_INTERVENTION.draftId;
 if(LOCAL_INTERVENTION.stream)LOCAL_INTERVENTION.stream.getTracks().forEach(track=>track.stop());
 if(id)navigator.sendBeacon('/api/local_interventions/'+encodeURIComponent(id)+'/discard',new Blob([],{type:'application/octet-stream'}))});
function studentNoteTime(seconds){seconds=Math.max(0,Math.round(Number(seconds)||0));
 return String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0')}
async function studentNoteRequest(path,options){
 const response=await fetch(path,options||{cache:'no-store'});
 const data=await response.json().catch(()=>({ok:false,msg:'respuesta inválida'}));
 if(!response.ok||data.ok===false)throw new Error(data.msg||('HTTP '+response.status));
 return data;
}
function studentNoteCard(note){
 const card=document.createElement('article');card.className='student-note-card';
 const head=document.createElement('header');const title=document.createElement('b');
 const source=note.source||{},when=note.class_time||{};
 title.textContent=(source.original_name||source.kind||'nota')+' · '+studentNoteTime(when.seconds);
 const rev=document.createElement('small');rev.textContent='rev. '+(note.revision||1);
 head.append(title,rev);card.appendChild(head);
 const extracted=document.createElement('p');const extraction=note.extraction||{};
 extracted.textContent=extraction.text||(
  extraction.status==='pending'?'Extracción pendiente.':
  extraction.status==='failed'?('Extracción fallida: '+(extraction.error||'')):
  'Original conservado sin texto extraído.');card.appendChild(extracted);
 const edit=document.createElement('textarea');edit.value=(note.correction||{}).text||'';
 edit.placeholder='Corrección o versión editable del estudiante';card.appendChild(edit);
 const actions=document.createElement('div');actions.className='student-note-actions';
 const includeLabel=document.createElement('label');const include=document.createElement('input');
 include.type='checkbox';include.checked=(note.inclusion||{}).state==='included';
 includeLabel.append(include,document.createTextNode('Incluir en lección'));
 const save=document.createElement('button');save.className='gris';save.textContent='Guardar cambios';
 save.onclick=()=>studentNoteUpdate(note.note_id,edit,include,save);
 const original=document.createElement('a');original.href='/api/student_notes/'+note.note_id+'/original';
 original.target='_blank';original.rel='noopener';original.textContent='Abrir original';
  const discard=document.createElement('button');discard.className='rojo';discard.textContent='Descartar';
  discard.onclick=()=>studentNoteDiscard(note.note_id,discard);
  actions.append(includeLabel,save,discard,original);
 if(source.kind==='microphone'){
  const transcribe=document.createElement('button');transcribe.className='gris';
  transcribe.textContent='Transcribir voz';transcribe.onclick=()=>studentNoteTranscribe(note.note_id,transcribe);
  actions.appendChild(transcribe);
 }
 card.appendChild(actions);return card;
}
async function loadStudentNotes(force){
 const now=Date.now();if(!force&&now-STUDENT_NOTES_LAST_FETCH<5000)return;
 STUDENT_NOTES_LAST_FETCH=now;
 try{const data=await studentNoteRequest('/api/student_notes',{cache:'no-store'});
  const signature=JSON.stringify((data.notes||[]).map(n=>[n.note_id,n.revision]));
  if(!force&&signature===STUDENT_NOTES_SIGNATURE)return;
  STUDENT_NOTES_SIGNATURE=signature;const root=document.getElementById('studentNotesList');
  root.replaceChildren();if(!(data.notes||[]).length){const empty=document.createElement('div');
   empty.className='student-note-empty';empty.textContent='Sin notas todavía.';root.appendChild(empty)}
  else for(const note of data.notes)root.appendChild(studentNoteCard(note));
 }catch(e){}
}
async function studentNoteUpload(blob,name,mime,kind,action,button){
 const form=new FormData();form.append('file',blob,name);form.append('source_kind',kind);
 form.append('user_action',action);form.append('mime',mime||blob.type||'application/octet-stream');
 return runBusy(button,'Guardando...',async()=>{try{const data=await studentNoteRequest(
  '/api/student_notes',{method:'POST',body:form});toast(data.msg||'Nota guardada');
   STUDENT_NOTES_SIGNATURE='';await loadStudentNotes(true);return true
  }catch(e){toast(e.message,true);return false}})
}
async function studentNoteSaveText(button){
 const input=document.getElementById('studentNoteText'),text=input.value.trim();if(!text)return;
 const form=new FormData();form.append('text',text);
 return runBusy(button,'Guardando...',async()=>{try{const data=await studentNoteRequest(
  '/api/student_notes',{method:'POST',body:form});input.value='';toast(data.msg||'Nota guardada');
  STUDENT_NOTES_SIGNATURE='';await loadStudentNotes(true)}catch(e){toast(e.message,true)}})
}
async function studentNoteUpdate(id,edit,include,button){
 return runBusy(button,'Guardando...',async()=>{try{const data=await studentNoteRequest(
  '/api/student_notes/'+encodeURIComponent(id),{method:'PATCH',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({correction:edit.value,inclusion:include.checked?'included':'excluded',
    reason:include.checked?'':'student_excluded'})});
  toast(data.msg||'Nota actualizada');STUDENT_NOTES_SIGNATURE='';await loadStudentNotes(true)
 }catch(e){toast(e.message,true)}})
}
async function studentNoteDiscard(id,button){
 if(!confirm('¿Descartar esta nota? El original se conserva para recuperación y auditoría.'))return;
 return runBusy(button,'Descartando...',async()=>{try{const data=await studentNoteRequest(
  '/api/student_notes/'+encodeURIComponent(id),{method:'PATCH',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({discarded:true,reason:'student_discarded'})});
  toast(data.msg||'Nota descartada');STUDENT_NOTES_SIGNATURE='';await loadStudentNotes(true)
 }catch(e){toast(e.message,true)}})
}
async function studentNoteTranscribe(id,button){
 const backend='remote',remote=true;
 if(!confirm('La nota de voz se enviará a la cadena STT remota. ¿Continuar?'))return;
 return runBusy(button,'Transcribiendo...',async()=>{try{const data=await studentNoteRequest(
  '/api/student_notes/'+encodeURIComponent(id)+'/transcribe',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({stt_backend:backend,remote_consent:remote})});
  toast(data.msg||'Voz transcrita');STUDENT_NOTES_SIGNATURE='';await loadStudentNotes(true)
 }catch(e){toast(e.message,true)}})
}
function studentNoteDiscardDraft(){
 if(STUDENT_NOTE_DRAFT&&STUDENT_NOTE_DRAFT.url)URL.revokeObjectURL(STUDENT_NOTE_DRAFT.url);
 STUDENT_NOTE_DRAFT=null;const root=document.getElementById('studentNoteDraft');
 document.getElementById('studentNoteDraftPreview').replaceChildren();root.style.display='none';
}
function studentNoteSetDraft(blob,name,mime,kind,action){
 studentNoteDiscardDraft();const url=URL.createObjectURL(blob);
 STUDENT_NOTE_DRAFT={blob,name,mime,kind,action,url};
 const preview=document.getElementById('studentNoteDraftPreview');
 const media=(mime||'').startsWith('audio/')?document.createElement('audio'):document.createElement('img');
 media.src=url;if(media.tagName==='AUDIO'){media.controls=true}else{media.alt=name}
 const label=document.createElement('p');label.textContent=name+' · revisar antes de guardar';
 preview.append(media,label);document.getElementById('studentNoteDraft').style.display='';
}
async function studentNoteSaveDraft(button){
 const draft=STUDENT_NOTE_DRAFT;if(!draft)return;
 const saved=await studentNoteUpload(draft.blob,draft.name,draft.mime,draft.kind,draft.action,button);
 if(saved)studentNoteDiscardDraft();
}
async function studentNoteScreenshot(button){
 try{const stream=await navigator.mediaDevices.getDisplayMedia({video:true,audio:false});
  const video=document.createElement('video');video.srcObject=stream;await video.play();
  const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
  canvas.getContext('2d').drawImage(video,0,0);stream.getTracks().forEach(track=>track.stop());
  const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
   if(blob)studentNoteSetDraft(blob,'captura.png','image/png','screenshot','capture')
 }catch(e){if(e.name!=='NotAllowedError')toast('No se pudo capturar: '+e.message,true)}
}
async function studentNoteToggleRecord(button){
 if(STUDENT_NOTE_RECORDER&&STUDENT_NOTE_RECORDER.state==='recording'){
  STUDENT_NOTE_RECORDER.stop();button.textContent='Grabar voz';return;
 }
 try{STUDENT_NOTE_STREAM=await navigator.mediaDevices.getUserMedia({audio:true});
  STUDENT_NOTE_CHUNKS=[];STUDENT_NOTE_RECORDER=new MediaRecorder(STUDENT_NOTE_STREAM);
  STUDENT_NOTE_RECORDER.ondataavailable=event=>{if(event.data.size)STUDENT_NOTE_CHUNKS.push(event.data)};
  STUDENT_NOTE_RECORDER.onstop=async()=>{const mime=STUDENT_NOTE_RECORDER.mimeType||'audio/webm';
   const blob=new Blob(STUDENT_NOTE_CHUNKS,{type:mime});STUDENT_NOTE_STREAM.getTracks().forEach(t=>t.stop());
   const ext=mime.includes('ogg')?'.ogg':mime.includes('mp4')?'.m4a':'.webm';
    STUDENT_NOTE_STREAM=null;studentNoteSetDraft(blob,'nota-voz'+ext,mime,'microphone','record')};
  STUDENT_NOTE_RECORDER.start(500);button.textContent='Detener grabación';toast('Grabando nota de voz…')
 }catch(e){toast('Micrófono no disponible: '+e.message,true)}
}
const studentFileInput=document.getElementById('studentNoteFiles');
studentFileInput.addEventListener('change',async event=>{for(const file of event.target.files){
 const kind=file.type.startsWith('image/')?'file_image':file.type.startsWith('audio/')?'microphone':'document';
 await studentNoteUpload(file,file.name,file.type,kind,'select',null)}event.target.value=''});
document.getElementById('studentNotePhoto').addEventListener('change',async event=>{
 const file=event.target.files&&event.target.files[0];if(file)await studentNoteUpload(
  file,file.name||'foto-apuntes.jpg',file.type,'photo','capture',null);event.target.value=''});
document.getElementById('studentNotePasteZone').addEventListener('paste',async event=>{
 const files=[...(event.clipboardData&&event.clipboardData.files||[])];
 for(const file of files){if(file.type.startsWith('image/')){
  event.preventDefault();await studentNoteUpload(file,file.name||'imagen-pegada.png',file.type,
   'paste_image','paste',null)}}});
let RS=null;
async function abrirRegion(el){return runBusy(el,'Abriendo...',async()=>{try{const s=await api('/api/region_shot',{},30000);
 document.getElementById('rimg').src='/img/'+s.captura;
 document.getElementById('rov').style.display='flex';toast('arrastra sobre el video del profesor')}
 catch(e){toast('no pude capturar: '+e,true)}})}
function rdown(e){const r=document.getElementById('rimg').getBoundingClientRect();
 RS={x:e.clientX-r.left,y:e.clientY-r.top};const b=document.getElementById('rbox');
 b.style.display='block';b.style.left=RS.x+'px';b.style.top=RS.y+'px';b.style.width='0';b.style.height='0'}
function rmove(e){if(!RS)return;const r=document.getElementById('rimg').getBoundingClientRect();
 const x=Math.min(Math.max(e.clientX-r.left,0),r.width),y=Math.min(Math.max(e.clientY-r.top,0),r.height);
 const b=document.getElementById('rbox');b.style.left=Math.min(RS.x,x)+'px';b.style.top=Math.min(RS.y,y)+'px';
 b.style.width=Math.abs(x-RS.x)+'px';b.style.height=Math.abs(y-RS.y)+'px'}
async function rup(e){if(!RS)return;const im=document.getElementById('rimg');
 const r=im.getBoundingClientRect();
 const x2=Math.min(Math.max(e.clientX-r.left,0),r.width),y2=Math.min(Math.max(e.clientY-r.top,0),r.height);
 const k=im.naturalWidth/r.width;
 const sel={x:Math.round(Math.min(RS.x,x2)*k),y:Math.round(Math.min(RS.y,y2)*k),
  w:Math.round(Math.abs(x2-RS.x)*k),h:Math.round(Math.abs(y2-RS.y)*k)};RS=null;
 if(sel.w<40||sel.h<40){toast('selección muy pequeña; intenta de nuevo',true);return}
 try{const s=await api('/api/region',sel);toast(s.msg,!s.ok);cerrarRegion()}catch(e){toast(''+e,true)}}
function cerrarRegion(){document.getElementById('rov').style.display='none';
 document.getElementById('rbox').style.display='none';RS=null}
async function quitarRegion(){try{const s=await api('/api/region',{quitar:1});toast(s.msg,!s.ok);
 cerrarRegion()}catch(e){toast(''+e,true)}}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refrescar(true)});
setInterval(actualizarRelojLocal,1000);
refrescar(true);
</script></body></html>"""


def _clase() -> Path | None:
    return est.clase_activa()


RE_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
RE_IMG_HTML = re.compile(r"<img[^>]*>")


def leccion_vivo(texto: str, parte) -> str:
    """Vista EN VIVO: solo bloques de la parte actual y SIN imágenes."""
    bloques = texto.split("\n---\n")
    propios = [b for b in bloques if f"· Parte {parte}" in b]
    if propios:
        texto = "\n---\n".join(propios)
    return RE_IMG_MD.sub("", RE_IMG_HTML.sub("", texto))


def _monitores(refresh: bool = False) -> list[dict]:
    global _MONITORES
    with _SOURCE_CACHE_LOCK:
        if refresh:
            _MONITORES = None
        if _MONITORES is None:
            try:
                from .captura_pantalla import monitores_info
                _MONITORES = monitores_info()
            except Exception:
                _MONITORES = [{"idx": 1, "etiqueta": "Pantalla 1"}]
        return list(_MONITORES)


def _dispositivos(refresh: bool = False) -> list[dict]:
    global _DISPOSITIVOS
    with _SOURCE_CACHE_LOCK:
        if refresh:
            _DISPOSITIVOS = None
        if _DISPOSITIVOS is None:
            try:
                _DISPOSITIVOS = captura_audio.dispositivos_info()
            except Exception:
                _DISPOSITIVOS = []
        return list(_DISPOSITIVOS)


def _fmt_abs(epoch: float | None) -> str:
    if not epoch:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _fmt_rel(segundos: float | int | None) -> str:
    try:
        total = max(0, int(round(float(segundos or 0))))
    except (TypeError, ValueError):
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _audio_ruta_ui(modo: str, fuente: str) -> tuple[str, str]:
    modo = str(modo or "")
    fuente = str(fuente or "sistema")
    if fuente == "sistema":
        return "TODO EL SISTEMA", "captura la mezcla completa del sistema"
    if ":proceso" in modo:
        return "EXCLUSIVO POR PID", f"{modo} - captura solo el arbol de procesos elegido"
    if ":fallback" in modo or ":dispositivo" in modo:
        return "FALLBACK SALIDA SISTEMA", f"{modo} - captura la mezcla del dispositivo seleccionado"
    if "sin_sesion" in modo:
        return "SIN SESION DE AUDIO", f"{modo} - no se encontro stream activo del navegador"
    return (modo or fuente or "-"), modo or fuente or "-"


def _entero_no_negativo(valor, defecto: int = 0) -> int:
    try:
        return max(0, int(valor))
    except (TypeError, ValueError):
        return max(0, int(defecto))


def _numero_no_negativo(valor, defecto: float = 0.0) -> float:
    try:
        return max(0.0, float(valor))
    except (TypeError, ValueError):
        return max(0.0, float(defecto))


def clasificar_estado_audio(
        st: dict, *, valid_wav_count: int | None = None) -> dict:
    """Clasifica fase de captura y evidencia WAV sin confundirlas."""
    health = st.get("audio_route_health")
    if not isinstance(health, dict):
        health = {}
    wavs = _entero_no_negativo(
        st.get("audio_valid_wav_count")
        if valid_wav_count is None else valid_wav_count)
    efectivo = est.estado_efectivo(st)
    intento = _entero_no_negativo(health.get("attempt"))
    ultimo_resultado = str(health.get("last_result") or "")
    ultimo_estado = str(st.get("audio_ultimo_estado") or "")
    capture_phase = str(st.get("audio_capture_phase") or "")
    warning = str(st.get("audio_warning") or "")
    sin_valido = _numero_no_negativo(health.get("seconds_without_valid_audio"))
    timeout = max(1.0, _numero_no_negativo(
        health.get("no_valid_audio_timeout"), 60.0))

    if efectivo == "finalizado":
        phase = "closed"
    elif efectivo == "audio_requires_attention":
        phase = "requires_attention"
    elif efectivo == "pausado_manual":
        phase = "manual_paused"
    elif efectivo == "auto_pausado":
        phase = "auto_paused"
    elif "fallback" in capture_phase or (
            capture_phase in {"trying_device", "reading_device"}
            and str(st.get("fuente") or "sistema") != "sistema"):
        phase = "fallback"
    elif ultimo_resultado in {"failed", "flat", "partial"}:
        phase = "fallback"
    elif warning and ultimo_resultado == "silence":
        phase = "video_paused"
    elif ultimo_resultado == "valid" or ultimo_estado == "activo":
        phase = "active"
    elif ultimo_resultado == "silence" or ultimo_estado == "silencio":
        phase = "silent"
    else:
        phase = "initializing"

    if wavs:
        evidence = "valid_wav"
    elif phase in {"closed", "requires_attention"} and intento:
        evidence = "no_valid_wav"
    elif intento >= 2 and sin_valido >= min(30.0, timeout / 2.0):
        evidence = "no_valid_wav"
    else:
        evidence = "awaiting_first_wav"

    labels = {
        "initializing": "Audio inicializando",
        "active": "Audio activo",
        "silent": "Audio silencioso normal",
        "video_paused": "Video posiblemente pausado",
        "auto_paused": "Audio en auto-pausa",
        "manual_paused": "Audio pausado manualmente",
        "fallback": "Audio intentando fallback",
        "requires_attention": "Audio requiere atención",
        "closed": "Audio cerrado",
        "no_valid_wav": "Audio sin WAV válido",
    }
    code = (
        "no_valid_wav"
        if evidence == "no_valid_wav"
        and phase not in {"closed", "requires_attention", "fallback"}
        else phase)
    detalle_partes = [labels[phase]]
    if evidence == "awaiting_first_wav":
        detalle_partes.append("esperando el primer WAV sellado")
    elif evidence == "no_valid_wav":
        detalle_partes.append("todavía no existe ningún WAV sealed/recovered")
    else:
        detalle_partes.append(f"{wavs} WAV válido(s)")
    if health.get("route"):
        detalle_partes.append(f"ruta={health['route']}")
    return {
        "code": code,
        "phase": phase,
        "evidence": evidence,
        "label": labels[code],
        "detail": " · ".join(detalle_partes),
        "valid_wav_count": wavs,
        "has_valid_wav": bool(wavs),
        "healthy": bool(wavs and phase in {"active", "silent"}),
        "action_required": bool(
            phase == "requires_attention"
            or (evidence == "no_valid_wav" and phase not in {"closed", "auto_paused"})),
    }


def clasificar_estado_stt(st: dict, report: dict) -> dict:
    """Combina trabajo transaccional y ciclo de vida sin inventar pendientes."""
    lifecycle = st.get("stt_lifecycle")
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    supervisor = st.get("stt_supervisor")
    if not isinstance(supervisor, dict):
        supervisor = {}
    lifecycle_status = str(lifecycle.get("status") or "")
    supervisor_status = str(supervisor.get("status") or "")
    if supervisor.get("restarted"):
        process_code = "restarting"
    elif supervisor_status == "lock_conflict":
        process_code = "lock_conflict"
    elif supervisor_status == "idle_no_pending":
        process_code = "idle_no_pending"
    elif supervisor_status in {
            "heartbeat_lost", "python_exception", "native_crash",
            "unknown_exit", "lock_stale"} and not supervisor.get("restarted"):
        process_code = "error"
    elif lifecycle_status == "pending_remote_stt" or supervisor_status == "pending_remote_stt":
        process_code = "pending_remote_stt"
    elif lifecycle_status == "python_exception":
        process_code = "error"
    elif lifecycle_status == "stopped_no_pending" or supervisor_status in {
            "normal_exit", "stopped_no_pending"}:
        process_code = "stopped_normal"
    else:
        process_code = "running"

    if "available_audio_count" in report:
        wavs = _entero_no_negativo(report.get("available_audio_count"))
    else:
        wavs = _entero_no_negativo(
            st.get("audio_valid_wav_count"), report.get("captured_count", 0))
    actionable = _entero_no_negativo(
        report.get("actionable_count"),
        _entero_no_negativo(report.get("pending_count"))
        + _entero_no_negativo(report.get("invalid_count")))
    ultimo_chunk = str(st.get("stt_ultimo_chunk") or "")
    ultimo_segmentos = st.get("stt_ultimo_segmentos")
    if ultimo_chunk and ultimo_segmentos is not None:
        last_commit = (
            "empty" if _entero_no_negativo(ultimo_segmentos) == 0 else "segments")
    elif (_entero_no_negativo(report.get("completed_count")) > 0
          and _entero_no_negativo(report.get("empty_completed_count"))
          == _entero_no_negativo(report.get("completed_count"))):
        last_commit = "empty"
    else:
        last_commit = "none"

    if process_code in {"lock_conflict", "restarting", "error", "pending_remote_stt"}:
        code = process_code
    elif wavs == 0:
        health = st.get("audio_route_health")
        intentos_audio = _entero_no_negativo(
            health.get("attempt") if isinstance(health, dict) else 0)
        if est.estado_efectivo(st) in {
                "audio_requires_attention", "finalizado"} or intentos_audio > 0:
            code = "no_valid_audio"
        else:
            code = "waiting_first_wav"
    elif (lifecycle_status == "processing"
          and str(lifecycle.get("phase") or "") == "transcribe"
          and actionable):
        code = "processing"
    elif actionable:
        code = "waiting_chunk"
    elif process_code == "stopped_normal":
        code = "stopped_normal"
    elif process_code == "idle_no_pending":
        code = "idle_no_pending"
    elif ultimo_chunk and last_commit == "empty":
        code = "complete_empty"
    elif ultimo_chunk and last_commit == "segments":
        code = "complete_segments"
    elif _entero_no_negativo(report.get("completed_count")):
        code = "no_pending"
    else:
        code = "waiting_chunk"

    labels = {
        "no_valid_audio": "STT sin audio válido",
        "waiting_first_wav": "STT esperando primer WAV",
        "waiting_chunk": "STT esperando chunk",
        "processing": "STT procesando",
        "complete_empty": "Commit STT completo vacío",
        "complete_segments": "Commit STT con segmentos",
        "no_pending": "STT sin pendientes",
        "idle_no_pending": "STT al día, esperando audio",
        "stopped_normal": "STT detenido normalmente",
        "lock_conflict": "STT bloqueado por lock",
        "restarting": "STT reiniciando",
        "error": "STT con error",
        "pending_remote_stt": "STT remoto pendiente",
    }
    if code == "no_valid_audio":
        detail = "0 audio válido → no hay trabajo para el transcriptor."
    elif code == "complete_empty":
        detail = "El último WAV terminó con commit completo y cero segmentos."
    elif code == "complete_segments":
        detail = "El último WAV terminó con segmentos transcritos."
    elif code == "stopped_normal":
        detail = "El transcriptor terminó normalmente y no tiene trabajo accionable."
    elif code == "idle_no_pending":
        detail = "El transcriptor está vivo y no tiene WAV pendientes."
    else:
        detail = str(supervisor.get("message") or lifecycle.get("phase") or labels[code])
    return {
        "code": code,
        "label": labels[code],
        "detail": detail,
        "process_code": process_code,
        "last_commit_status": last_commit,
        "has_work": bool(actionable),
        "actionable_count": actionable,
        "valid_wav_count": wavs,
        "process_expected": not (
            process_code == "stopped_normal" and not actionable),
    }


def _stt_hardware_ui(st: dict) -> dict:
    hardware = st.get("stt_hardware")
    hardware = hardware if isinstance(hardware, dict) else {}
    device = str(st.get("stt_device") or hardware.get("device") or "")
    backend = str(st.get("stt_backend") or hardware.get("backend")
                  or hardware.get("backend_candidate") or "")
    readiness = str(st.get("stt_readiness") or hardware.get("readiness") or "")
    fallback = str(st.get("stt_fallback_reason")
                   or hardware.get("fallback_reason") or "")
    detected = hardware.get("detected_nvidia_gpus")
    detected = detected if isinstance(detected, list) else []
    detected_first = detected[0] if detected and isinstance(detected[0], dict) else {}
    name = str(hardware.get("selected_device_name") or st.get("stt_gpu")
               or detected_first.get("name") or "")
    total = _entero_no_negativo(hardware.get("vram_total_mb"))
    free = _entero_no_negativo(hardware.get("vram_free_mb"))
    if not total and detected_first:
        total = _entero_no_negativo(detected_first.get("memory_total_mb"))
        free = _entero_no_negativo(detected_first.get("memory_free_mb"))
    if device == "remote" or backend.startswith("remote"):
        effective = str(st.get("stt_provider") or "cadena")
        label = f"STT remoto · {effective} · GPU local libre"
    elif device == "cuda":
        label = f"CUDA NVIDIA · {name or 'GPU'}"
        if total:
            label += f" · {free}/{total} MiB libres"
    elif device == "cpu" or backend:
        label = "CPU"
        if name:
            label += f" · NVIDIA detectada no usada: {name}"
            if total:
                label += f" · {free}/{total} MiB libres"
    else:
        label = "detectando hardware"
    if readiness:
        label += f" · {readiness}"
    if fallback:
        label += f" · fallback: {fallback}"
    return {
        "label": label,
        "device": device,
        "backend": backend,
        "readiness": readiness,
        "fallback_reason": fallback,
        "gpu_name": name,
        "vram_total_mb": total,
        "vram_free_mb": free,
        "probe": hardware,
    }


def _inicio_parte(d: Path, st: dict) -> float:
    parte = int(st.get("parte", 1))
    candidatos = [float(st.get("inicio", time.time()))]
    for m in est.leer_marcas(d):
        if m.get("tipo") == "fin_parte" and int(m.get("parte", 0)) == parte - 1:
            candidatos.append(float(m.get("t", candidatos[0])))
        if m.get("tipo") == "reanudar_sesion" and int(m.get("parte", 0)) == parte:
            candidatos.append(float(m.get("t", candidatos[0])))
    return max(candidatos)


def _rango_clase(d: Path, st: dict, ahora: float,
                 marcas: list[dict] | None = None) -> tuple[float, float]:
    inicio = float(st.get("inicio", ahora))
    try:
        marcas = marcas if marcas is not None else est.leer_marcas(d)
    except Exception:
        marcas = []
    fines = [float(m.get("t", inicio)) for m in marcas if m.get("tipo") == "fin"]
    fin = fines[-1] if st.get("estado") == "fin" and fines else ahora
    return inicio, max(inicio, fin)


def _intervalos_almacenando(d: Path, st: dict, ahora: float,
                            marcas: list[dict] | None = None) -> list[tuple[float, float]]:
    """Compatibilidad: delega la historia temporal a la fuente canonica."""
    try:
        marcas = marcas if marcas is not None else est.leer_marcas(d)
    except Exception:
        marcas = []
    return est.intervalos_captura_util(st, marcas, ahora)


def _segundos_almacenando(d: Path, st: dict, ahora: float,
                          desde: float | None = None,
                          marcas: list[dict] | None = None) -> float:
    total = 0.0
    for ini, fin in _intervalos_almacenando(d, st, ahora, marcas=marcas):
        if desde is not None:
            ini = max(ini, desde)
        if fin > ini:
            total += fin - ini
    return max(0.0, total)


def _ruta_relativa_clase(d: Path) -> str:
    try:
        return d.relative_to(config.ROOT).as_posix()
    except ValueError:
        return d.as_posix()


def _uso_api_logs() -> list[dict]:
    return est.leer_jsonl(config.LOGS_DIR / "uso_api.jsonl")


def _costo_fila_uso(u: dict) -> float:
    try:
        return float(u.get("costo_usd", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _t_fila_uso(u: dict) -> float | None:
    try:
        return float(u.get("t", 0))
    except (TypeError, ValueError):
        return None


def _uso_tiene_contexto(u: dict) -> bool:
    return any(u.get(k) for k in ("curso", "clase_id", "clase_path"))


def _uso_en_rangos(u: dict, rangos: list[tuple[float, float]]) -> bool:
    t = _t_fila_uso(u)
    return t is not None and any(ini <= t <= fin for ini, fin in rangos)


def _uso_es_de_clase(u: dict, d: Path, st: dict,
                     rango: tuple[float, float]) -> bool:
    curso = str(st.get("curso") or est.curso_de(d))
    clase_id = str(st.get("clase_id") or d.name)
    clase_path = _ruta_relativa_clase(d)
    log_curso = str(u.get("curso") or "")
    log_clase = str(u.get("clase_id") or "")
    log_path = str(u.get("clase_path") or "")
    if _uso_tiene_contexto(u):
        if log_curso and log_curso != curso:
            return False
        if log_path:
            return log_path == clase_path
        if log_clase:
            return log_clase == clase_id
        return False
    return _uso_en_rangos(u, [rango])


def _uso_es_de_curso(u: dict, curso: str,
                     clases: list[tuple[Path, dict, tuple[float, float]]]) -> bool:
    log_curso = str(u.get("curso") or "")
    if log_curso:
        return log_curso == curso
    log_path = str(u.get("clase_path") or "")
    if log_path:
        return any(log_path == _ruta_relativa_clase(d) for d, _st, _r in clases)
    log_clase = str(u.get("clase_id") or "")
    if log_clase:
        return any(log_clase == str(st.get("clase_id") or d.name)
                   for d, st, _r in clases
                   if str(st.get("curso") or est.curso_de(d)) == curso)
    return _uso_en_rangos(u, [r for _d, _st, r in clases])


def _rangos_curso(curso: str, ahora: float) -> list[tuple[float, float]]:
    clases = config.dir_curso(curso) / "clases"
    if not clases.is_dir():
        return []
    rangos: list[tuple[float, float]] = []
    for d in clases.iterdir():
        if not (d / "session_state.json").is_file():
            continue
        try:
            rangos.append(_rango_clase(d, est.leer_estado(d), ahora))
        except Exception:
            continue
    return rangos


def _costo_en_rangos(rangos: list[tuple[float, float]]) -> float:
    if not rangos:
        return 0.0
    total = 0.0
    usos = est.leer_jsonl(config.LOGS_DIR / "uso_api.jsonl")
    for u in usos:
        try:
            t = float(u.get("t", 0))
            costo = float(u.get("costo_usd", 0) or 0)
        except (TypeError, ValueError):
            continue
        if any(ini <= t <= fin for ini, fin in rangos):
            total += costo
    return total


def _clases_curso_con_rango(curso: str, ahora: float) -> list[tuple[Path, dict, tuple[float, float]]]:
    clases_dir = config.dir_curso(curso) / "clases"
    if not clases_dir.is_dir():
        return []
    out: list[tuple[Path, dict, tuple[float, float]]] = []
    for d in sorted(clases_dir.iterdir()):
        if not (d / "session_state.json").is_file():
            continue
        try:
            st = est.leer_estado(d)
            out.append((d, st, _rango_clase(d, st, ahora)))
        except Exception:
            continue
    return out


def _costos_curso(curso: str, ahora: float, clase_actual: Path | None = None) -> dict:
    return costos_api.resumen_curso(curso, ahora, clase_actual)


def _leccion_final_info(d: Path) -> dict:
    final = d / "leccion_final.md"
    if not final.is_file():
        return {"existe": False, "actualizada": False, "mtime": 0.0, "nuevas": []}
    try:
        mtime = final.stat().st_mtime
    except OSError:
        return {"existe": False, "actualizada": False, "mtime": 0.0, "nuevas": []}
    fuentes = [
        d / "transcript.jsonl",
        d / "leccion_en_vivo.md",
        d / "qa.md",
        d / "marcas.jsonl",
        d / "img_log.jsonl",
    ]
    nuevas = []
    for p in fuentes:
        try:
            if p.is_file() and p.stat().st_mtime > mtime + 2:
                nuevas.append(p.name)
        except OSError:
            continue
    return {"existe": True, "actualizada": not nuevas, "mtime": mtime, "nuevas": nuevas}


def _estado_operativo(st: dict, final_info: dict) -> dict:
    efectivo = est.estado_efectivo(st)
    if _POST_CORRIENDO:
        return {
            "clave": "consolidando",
            "titulo": "Consolidando…",
            "detalle": "El post-proceso está generando la lección final.",
        }
    if _POST_ERROR:
        return {
            "clave": "error",
            "titulo": "Error al consolidar",
            "detalle": _POST_ERROR,
        }
    if efectivo == "finalizado":
        if final_info.get("actualizada"):
            return {
                "clave": "consolidada",
                "titulo": "Lección consolidada",
                "detalle": "La lección final está lista y actualizada.",
            }
        return {
            "clave": "pendiente",
            "titulo": "Finalizada — pendiente de consolidar",
            "detalle": "Consolida para crear o actualizar leccion_final.md.",
        }
    if efectivo == "pausado_manual":
        return {
            "clave": "pausado",
            "titulo": "Pausado",
            "detalle": "La clase está pausada; puedes seguir o finalizar.",
        }
    if efectivo == "audio_requires_attention":
        return {
            "clave": "audio_atencion",
            "titulo": "Audio requiere atención",
            "detalle": "La evidencia visual continúa; revisa la salida y reanuda para probar audio.",
        }
    if efectivo == "auto_pausado":
        return {
            "clave": "auto_pausado",
            "titulo": "Auto-pausa por silencio",
            "detalle": "El tiempo de pared continúa; puedes reanudar o pausar manualmente.",
        }
    if efectivo == "grabando_activo":
        return {
            "clave": "grabando",
            "titulo": "Grabando",
            "detalle": "Audio sellado, STT canónico y capturas alimentan la clase.",
        }
    return {"clave": "sin_clase", "titulo": "Sin clase", "detalle": "Elige curso y sesión."}


def _contenido_legacy_info() -> dict:
    try:
        from .auditar_contenido import auditar_contenido
        rep = auditar_contenido()
        legacy = rep.get("carpetas_legacy", [])
        return {
            "legacy": legacy,
            "msg": ("contenido/ tiene raíces legacy: " + ", ".join(legacy)
                    if legacy else ""),
        }
    except Exception as e:
        return {"legacy": [], "msg": f"no se pudo auditar contenido/: {type(e).__name__}"}


def _evidencia_clase(d: Path | None) -> dict:
    if d is None:
        return {"resumen": "", "bytes": 0, "wav": 0, "imagenes": 0}
    audio = []
    for record in audio_manifest.capture_records(d):
        ruta = d / "audio" / Path(str(
            record.get("path") or f"{record.get('chunk_id')}.wav")).name
        try:
            if ruta.is_file() and ruta.stat().st_size > 44:
                audio.append(ruta)
        except OSError:
            continue
    imgs = list((d / "img").glob("*")) if (d / "img").is_dir() else []
    transcript = d / "transcript.jsonl"
    img_log = d / "img_log.jsonl"
    debug = d / "_debug"
    bytes_audio = sum(p.stat().st_size for p in audio if p.is_file())
    bytes_img = sum(p.stat().st_size for p in imgs if p.is_file())
    bytes_trans = transcript.stat().st_size if transcript.is_file() else 0
    bytes_img_log = img_log.stat().st_size if img_log.is_file() else 0
    bytes_debug = sum(p.stat().st_size for p in debug.rglob("*") if p.is_file()) \
        if debug.is_dir() else 0
    total = bytes_audio + bytes_img + bytes_trans + bytes_img_log + bytes_debug
    imgs_count = len([p for p in imgs if p.is_file()])
    resumen = (f"Evidencia conservada: {len(audio)} WAV, {imgs_count} imágenes, "
               f"transcript {bytes_trans} B, debug {bytes_debug} B.")
    return {"resumen": resumen, "bytes": total, "wav": len(audio),
            "imagenes": imgs_count}


def _panel_legacy_info() -> dict:
    root_signature = _path_signature(config.CONTENIDO_DIR)
    return _panel_cached(
        f"legacy:{config.CONTENIDO_DIR.resolve()}", root_signature, ttl=60.0,
        loader=_contenido_legacy_info,
    )


def _panel_evidencia(d: Path | None) -> dict:
    if d is None:
        return _evidencia_clase(None)
    # Este contador es informativo, no una barrera. La verificación completa se
    # ejecuta al cerrar/consolidar; durante LIVE basta una muestra por minuto.
    return _panel_cached(
        f"evidence:{d.resolve()}", str(d.resolve()), ttl=60.0,
        loader=lambda: _evidencia_clase(d),
    )


def _panel_costos_curso(curso: str, ahora: float,
                        clase_actual: Path | None) -> dict:
    current = str(clase_actual.resolve()) if clase_actual is not None else ""
    return _panel_cached(
        f"course-cost:{config.CONTENIDO_DIR.resolve()}:{curso}:{current}",
        # Los cargos de STT llegan como maximo al cerrar cada WAV de 30 s; un
        # refresco de cinco segundos repetia recorridos sin informacion nueva.
        (str(curso), current), ttl=30.0,
        loader=lambda: _costos_curso(curso, ahora, clase_actual=clase_actual),
    )


def _panel_stt_status(d: Path, *, verify_files: bool) -> dict:
    signature = (
        bool(verify_files), _path_signature(audio_manifest.manifest_path(d)),
        (
            _directory_files_signature(d / "transcript_chunks", "*.json*"),
            _directory_files_signature(d / "audio", "*.wav"),
        ) if verify_files else (
            _directory_signature(d / "transcript_chunks"),
            _directory_signature(d / "audio"),
        ),
    )
    return _panel_cached(
        f"stt-status:{d.resolve()}:{int(verify_files)}", signature, ttl=60.0,
        loader=lambda: audio_manifest.transcript_status_report(
            d, verify_files=verify_files),
    )


def _panel_completion_barrier(d: Path, stt_status: dict) -> dict:
    signature = (
        _path_signature(audio_manifest.manifest_path(d)),
        _directory_signature(d / "transcript_chunks"),
        _directory_signature(d / "audio"),
    )
    return _panel_cached(
        f"completion-barrier:{d.resolve()}", signature, ttl=60.0,
        loader=lambda: audio_manifest.completion_barrier_report(
            d, transcript_report=stt_status),
    )


def _consolidation_gate(d: Path, st: dict, *,
                        stt_status: dict | None = None,
                        fresh: bool = False) -> dict:
    """Deriva la disponibilidad de consolidación sin mutar evidencia real."""
    if str(st.get("estado") or "") != "fin":
        return {
            "ready": False,
            "shutdown_blocking": True,
            "status": "class_not_finished",
            "warnings": [],
            "reasons": ["class_not_finished"],
        }
    shutdown_state = str(st.get("shutdown_state") or "")
    if shutdown_state in {"finalizando", "pendiente_cierre"}:
        return {
            "ready": False,
            "shutdown_blocking": True,
            "status": "waiting_shutdown",
            "warnings": [],
            "reasons": [shutdown_state],
        }
    if fresh:
        barrier = audio_manifest.completion_barrier_report(
            d, transcript_report=stt_status)
    else:
        if stt_status is None:
            stt_status = _panel_stt_status(d, verify_files=True)
        barrier = _panel_completion_barrier(d, stt_status)
    return {
        "ready": bool(barrier.get("ready")),
        "shutdown_blocking": not bool(barrier.get("ready")),
        "status": str(barrier.get("status") or ""),
        "warnings": list(barrier.get("warnings") or []),
        "reasons": list(
            barrier.get("blocking_reasons")
            if barrier.get("blocking_reasons") is not None
            else barrier.get("reasons") or []),
        "barrier": barrier,
    }


def _limpiar_runtime_stt() -> int:
    """Elimina solo IPC efímero de esta instancia, nunca evidencia de la clase."""
    removed = 0
    if not RUNTIME_DIR.is_dir():
        return removed
    allowed = {SPOOL_FILENAME, ACK_FILENAME, STATE_FILENAME}
    try:
        candidates = list(RUNTIME_DIR.iterdir())
    except OSError:
        return removed
    for path in candidates:
        if not path.is_file():
            continue
        if path.name not in allowed and not (
                path.name.startswith(".stt-preview.")
                and path.name.endswith(".tmp")):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    try:
        RUNTIME_DIR.rmdir()
    except OSError:
        pass
    return removed


def _preparar_runtime_stt() -> Path:
    _limpiar_runtime_stt()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR


def _entorno_hijo(mod: str, *, force_cpu: bool = False) -> dict:
    entorno = {
        **os.environ,
        "CLASE_APP": "1",
        "CLASE_INSTANCE_ID": RUNTIME_INSTANCE_ID,
        "CLASE_RUNTIME_DIR": str(RUNTIME_DIR),
    }
    entorno.pop("CLASE_STT_CPU", None)
    return entorno


def clasificar_salida_hijo(
        mod: str, *, returncode: int | None, lifecycle: dict | None = None,
        pending_count: int = 0, ahora: float | None = None,
        heartbeat_timeout: float = 30.0) -> dict:
    """Separa causa de salida, política de reinicio y fallback CPU."""
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    status_explicit = str(lifecycle.get("status") or "")
    ahora = time.time() if ahora is None else float(ahora)
    heartbeat = float(lifecycle.get("heartbeat") or 0.0)
    base = {
        "returncode": returncode,
        "restart": False,
        "force_cpu": False,
        "pending_count": int(pending_count),
        "lock_owner": lifecycle.get("lock_owner", ""),
        "last_heartbeat": heartbeat,
        "last_commit": lifecycle.get("last_commit", ""),
    }
    if returncode is None:
        estados_idle_stt = {"running", "waiting_audio", "paused", "idle_no_pending"}
        if (mod == "src.transcriptor_vivo"
                and int(pending_count) <= 0
                and status_explicit in estados_idle_stt):
            return {
                **base,
                "status": "idle_no_pending",
                "message": "STT al día; esperando nuevo audio.",
            }
        if heartbeat and ahora - heartbeat > max(1.0, float(heartbeat_timeout)):
            return {
                **base,
                "status": "heartbeat_lost",
                "message": "STT sin heartbeat; proceso aún vivo, requiere diagnóstico.",
            }
        return {**base, "status": "running", "message": "STT ejecutándose."}
    if mod != "src.transcriptor_vivo":
        if returncode == 0:
            return {**base, "status": "normal_exit", "message": "Proceso detenido normalmente."}
        return {
            **base, "status": "python_exception", "restart": True,
            "message": f"Proceso terminó con error (returncode={returncode}); reiniciando.",
        }
    if returncode == 0:
        status = (
            "pending_remote_stt"
            if status_explicit == "pending_remote_stt" else
            "stopped_no_pending"
            if status_explicit == "stopped_no_pending" else
            "normal_exit" if not pending_count else "unknown_exit")
        return {
            **base,
            "status": status,
            "restart": status == "unknown_exit",
            "message": (
                "STT pendiente conservado para reintento remoto."
                if status == "pending_remote_stt" else
                "STT detenido normalmente, sin audio pendiente."
                if status == "stopped_no_pending" else
                "STT terminó normalmente."
                if status == "normal_exit" else
                "STT terminó sin causa explícita y aún hay audio pendiente."),
        }
    if returncode == 3 or status_explicit == "lock_conflict":
        return {
            **base, "status": "lock_conflict",
            "message": "STT bloqueado por un propietario de lock existente.",
        }
    if status_explicit == "requested_stop" or returncode in {-15, -9}:
        return {**base, "status": "killed", "message": "STT detenido por solicitud."}
    native = returncode < 0 or returncode >= 0xC0000000
    if native:
        return {
            **base,
            "status": "native_crash",
            "restart": True,
            "force_cpu": False,
            "message": "STT remoto tuvo un crash nativo; reiniciando sin cambiar backend.",
        }
    return {
        **base,
        "status": "python_exception" if status_explicit == "python_exception"
        or returncode == 1 else "unknown_exit",
        "restart": True,
        "message": f"STT terminó con error Python (returncode={returncode}); reiniciando.",
    }


def _spawn(mod: str, intentos: int = 0, *, force_cpu: bool = False) -> dict:
    entorno = _entorno_hijo(mod, force_cpu=force_cpu)
    py_venv = config.ROOT / ".venv" / "Scripts" / "python.exe"
    py = str(py_venv) if py_venv.is_file() else \
        sys.executable.replace("pythonw.exe", "python.exe")
    lg = open(config.LOGS_DIR / f"{mod.split('.')[-1]}.log", "a", encoding="utf-8")
    try:
        p = subprocess.Popen([py, "-m", mod], cwd=str(config.ROOT), stdout=lg,
                             stderr=subprocess.STDOUT, env=entorno,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        lg.close()
    snapshot = _PROCESS_OPS.snapshot(p.pid)
    owned = (
        OwnedProcess(
            module=mod,
            instance_id=RUNTIME_INSTANCE_ID,
            root=snapshot,
            class_dir=str(_clase() or ""),
        )
        if snapshot is not None else None
    )
    return {
        "mod": mod, "p": p, "intentos": intentos,
        "force_cpu": bool(force_cpu), "reported_status": "",
        "requested_stop": False, "owned": owned,
    }


def _lanzar_hijos() -> None:
    config.ensure_dirs()
    _preparar_runtime_stt()
    _SESSION_STOPPING.clear()
    with _HIJOS_LOCK:
        if any(h["p"].poll() is None for h in HIJOS):
            return
        HIJOS.clear()
        for mod in MODULOS:
            HIJOS.append(_spawn(mod))


def _cerrar_hijos_coordinado() -> list[dict]:
    """Espera productores/consumidores y escala solo fingerprints propios."""
    _SESSION_STOPPING.set()
    with _HIJOS_LOCK:
        registros = list(HIJOS)
        for registro in registros:
            registro["requested_stop"] = True
    policy = StopPolicy(
        cooperative_timeout=float(
            os.environ.get("CLASE_SHUTDOWN_COOPERATIVE_SEC", "30")),
        terminate_timeout=float(
            os.environ.get("CLASE_SHUTDOWN_TERMINATE_SEC", "5")),
        kill_timeout=float(os.environ.get("CLASE_SHUTDOWN_KILL_SEC", "2")),
    )
    owned = [registro.get("owned") for registro in registros
             if registro.get("owned") is not None]
    resultados = [
        {
            "module": resultado.module,
            "pid": resultado.pid,
            "status": resultado.status,
            "detail": resultado.detail,
            "remaining_pids": list(resultado.remaining_pids),
        }
        for resultado in stop_owned_children(
            owned, _PROCESS_OPS, policy, instance_id=RUNTIME_INSTANCE_ID)
    ]
    for registro in registros:
        if registro.get("owned") is not None:
            continue
        proceso = registro["p"]
        try:
            proceso.wait(timeout=policy.cooperative_timeout)
            status = "cooperative"
        except subprocess.TimeoutExpired:
            # Sin create_time verificable no es seguro escalar por PID.
            status = "unverified_alive"
        resultados.append({
            "module": registro["mod"],
            "pid": getattr(proceso, "pid", 0),
            "status": status,
            "detail": "fingerprint no disponible" if status != "cooperative" else "",
        })
    with _HIJOS_LOCK:
        HIJOS[:] = [
            registro for registro in registros
            if registro["p"].poll() is None
        ]
        vivos = {getattr(registro["p"], "pid", 0) for registro in HIJOS}
    for resultado in resultados:
        resultado["alive"] = (
            resultado["pid"] in vivos
            or bool(resultado.get("remaining_pids"))
        )
    return resultados


def _cerrar_hijos(timeout: float = 90) -> None:
    """Alias legacy; el timeout duro vive ahora en la política coordinada."""
    del timeout
    _cerrar_hijos_coordinado()


def _shutdown_server() -> None:
    """Detiene el servidor después de que la solicitud HTTP inicial respondió."""
    _APP_STOPPING.set()
    server = _SERVER
    if server is not None:
        server.shutdown()


def _shutdown_runner(reason: str, close_app: bool) -> dict:
    d = _clase()
    if d is not None:
        st = est.leer_estado(d)
        if st.get("estado") != "fin":
            est.transicionar_captura(d, "fin")
        elif st.get("shutdown_state") not in {"finalizando", "finalizada"}:
            est.actualizar_estado(
                d,
                shutdown_state="finalizando",
                shutdown_requested_at=time.time(),
                audio_capture_phase="finalizing",
            )
    audio_sesiones.restaurar_enfoques()
    resultados = _cerrar_hijos_coordinado()
    pendientes = [resultado for resultado in resultados if resultado.get("alive")]
    runtime_ipc_removed = _limpiar_runtime_stt() if not pendientes else 0
    locks_limpiados = []
    for nombre, modulo in (
            ("captura_audio", "src.captura_audio"),
            ("captura_pantalla", "src.captura_pantalla"),
            ("transcriptor", "src.transcriptor_vivo"),
            ("escriba", "src.escriba")):
        if config.limpiar_instancia_obsoleta(
                nombre, modulo=modulo, class_dir=d):
            locks_limpiados.append(nombre)
    barrera = (
        audio_manifest.completion_barrier_report(d)
        if d is not None else {"ready": True, "capture_session_closed": True,
                               "reasons": []}
    )
    integridad_pendiente = not bool(barrera.get("ready"))
    status = (
        "pending" if pendientes else
        "complete_with_pending" if integridad_pendiente else
        "complete"
    )
    if d is not None:
        est.actualizar_estado(
            d,
            shutdown_state=(
                "pendiente_cierre" if pendientes else
                "pendiente_consolidar" if integridad_pendiente else
                "finalizada"),
            shutdown_completed_at=time.time() if not pendientes else 0.0,
            shutdown_reason=str(reason),
            shutdown_children=resultados,
            audio_capture_phase="closing" if pendientes else "closed",
        )
    report = {
        "status": status,
        "reason": str(reason),
        "close_app": bool(close_app),
        "instance_id": RUNTIME_INSTANCE_ID,
        "children": resultados,
        "pending_count": len(pendientes),
        "capture_session_closed": bool(barrera.get("capture_session_closed")),
        "completion_ready": bool(barrera.get("ready")),
        "completion_reasons": list(barrera.get("reasons") or []),
        "completion_warnings": list(barrera.get("warnings") or []),
        "stale_locks_removed": locks_limpiados,
        "runtime_ipc_removed": runtime_ipc_removed,
    }
    if close_app:
        _shutdown_server()
    return report


def _request_runtime_shutdown(reason: str, close_app: bool = False):
    global _SHUTDOWN_COORDINATOR, _LAST_SHUTDOWN_TICKET
    with _SHUTDOWN_COORDINATOR_LOCK:
        if _SHUTDOWN_COORDINATOR is None:
            _SHUTDOWN_COORDINATOR = ShutdownCoordinator(_shutdown_runner)
        coordinator = _SHUTDOWN_COORDINATOR
        ticket = coordinator.request(str(reason), bool(close_app))
        _LAST_SHUTDOWN_TICKET = ticket
    if close_app and not ticket.accepted and not _APP_CLOSE_FOLLOWUP.is_set():
        _APP_CLOSE_FOLLOWUP.set()

        def completar_cierre_app():
            global _LAST_SHUTDOWN_TICKET
            ticket.completed.wait()
            siguiente = coordinator.request(f"{reason}_after_active", True)
            _LAST_SHUTDOWN_TICKET = siguiente
            siguiente.completed.wait()
            _APP_CLOSE_FOLLOWUP.clear()

        threading.Thread(
            target=completar_cierre_app,
            name="clase-viva-app-close-followup",
            daemon=False,
        ).start()
    return ticket


def _handle_shutdown_signal(signum, _frame) -> None:
    _request_runtime_shutdown(f"signal_{signum}", True)


def _on_tray_exit() -> None:
    """Punto único para una bandeja presente o futura; no termina por nombre."""
    _request_runtime_shutdown("tray_exit", True)


def _install_shutdown_handlers() -> None:
    for nombre in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, nombre, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _handle_shutdown_signal)
        except (OSError, ValueError):
            # Solo el hilo principal puede instalar handlers.
            continue


def _evaluar_hijo_una_vez(h: dict, d: Path) -> tuple[dict, dict]:
    returncode = h["p"].poll()
    st = est.leer_estado(d)
    lifecycle = st.get("stt_lifecycle")
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    if h.get("requested_stop"):
        lifecycle = {**lifecycle, "status": "requested_stop"}
    pending_count = 0
    if h.get("mod") == "src.transcriptor_vivo":
        # El supervisor despierta cada ocho segundos, pero el inventario STT solo
        # se reconstruye cuando cambia manifest/commits. En idle reutiliza el
        # mismo snapshot barato que el endpoint del panel.
        reporte_stt = _panel_stt_status(d, verify_files=False)
        pending_count = int(
            reporte_stt.get(
                "actionable_count", reporte_stt.get("unresolved_count", 0))
            or 0)
    resultado = clasificar_salida_hijo(
        h["mod"], returncode=returncode, lifecycle=lifecycle,
        pending_count=pending_count)
    if resultado["status"] == "lock_conflict":
        lock = config.diagnosticar_instancia(
            "transcriptor", modulo="src.transcriptor_vivo", class_dir=d)
        resultado.update({
            "lock_owner": lock.get("owner_id", ""),
            "last_heartbeat": lock.get("heartbeat", 0.0),
            "lock_status": lock.get("status", ""),
        })
        if not lock.get("owner_valid"):
            resultado.update({
                "status": "lock_stale",
                "restart": True,
                "message": "Lock STT obsoleto recuperado; reiniciando una vez.",
            })
    puede_reiniciar = bool(
        resultado.get("restart")
        and not h.get("requested_stop")
        and int(h.get("intentos") or 0) < 3)
    if puede_reiniciar:
        nuevo = _spawn(
            h["mod"], int(h.get("intentos") or 0) + 1,
            force_cpu=bool(resultado.get("force_cpu")),
        )
        resultado["restarted"] = True
        return nuevo, resultado
    resultado["restarted"] = False
    return h, resultado


def _vigilante() -> None:
    """Modo app: latido + resucitar hijos caídos mientras la clase esté activa."""
    while not _APP_STOPPING.is_set():
        try:
            config.LATIDO.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        d = _clase()
        activa = d is not None and est.leer_estado(d)["estado"] in ("grabando", "pausado")
        if activa:
            for i, h in enumerate(list(HIJOS)):
                nuevo, resultado = _evaluar_hijo_una_vez(h, d)
                HIJOS[i] = nuevo
                status = str(resultado.get("status") or "")
                if status != h.get("reported_status"):
                    aviso = str(resultado.get("message") or status)
                    _runtime_health_append(aviso)
                    nuevo["reported_status"] = status
                    if h.get("mod") == "src.transcriptor_vivo":
                        try:
                            est.actualizar_estado(d, stt_supervisor={
                                **resultado,
                                "attempt": int(nuevo.get("intentos") or 0),
                                "mode": "remote",
                            })
                        except Exception:
                            pass
        if _APP_STOPPING.wait(8):
            return


@app.get("/")
def inicio():
    return PAGINA


def _pagina_leccion_html(markdown_texto: str) -> str:
    titulo_match = re.search(r"(?m)^#\s+(.+)$", str(markdown_texto or ""))
    titulo = titulo_match.group(1).strip() if titulo_match else "Lección final"
    cuerpo = a_html(markdown_texto)
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(titulo)}</title>
<style>
:root{{--ink:#172033;--muted:#667085;--paper:#fbfcff;--card:#fff;--teal:#0f9f98;
--violet:#7765d8;--coral:#f47f72;--line:#dbe4ef;--soft:#edf8f7}}
*{{box-sizing:border-box}} body{{margin:0;color:var(--ink);background:
radial-gradient(circle at 8% 2%,#dff8f4 0,transparent 26rem),
radial-gradient(circle at 92% 8%,#eee9ff 0,transparent 28rem),var(--paper);
font:17px/1.72 Inter,"Segoe UI",system-ui,sans-serif}}
.top{{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;
align-items:center;gap:1rem;padding:.8rem max(1rem,calc((100vw - 1180px)/2));
background:rgba(251,252,255,.92);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}}
.brand{{font-weight:800;color:var(--teal);letter-spacing:.02em}} .top a{{color:var(--violet);
font-weight:700;text-decoration:none;padding:.45rem .75rem;border:1px solid #d8d2fa;border-radius:999px}}
main{{max-width:1180px;margin:0 auto;padding:2.4rem 1.2rem 6rem}} article{{background:rgba(255,255,255,.78);
border:1px solid rgba(219,228,239,.9);border-radius:28px;padding:clamp(1.2rem,4vw,4rem);
box-shadow:0 24px 70px rgba(58,70,96,.10)}}
h1{{font-size:clamp(2.1rem,5vw,4rem);line-height:1.08;margin:.1em 0 .7em}}
h2{{margin:2.2em 0 .7em;padding:.45em .65em;border-left:6px solid var(--teal);
background:linear-gradient(90deg,#e7f8f6,transparent);border-radius:0 14px 14px 0;font-size:1.75rem}}
h3{{margin:1.8em 0 .6em;color:#463d91;font-size:1.35rem}} h4{{margin:1.5em 0 .45em;color:#245f65}}
p,li{{max-width:82ch}} blockquote{{margin:1.2rem 0;padding:1rem 1.2rem;border-left:5px solid var(--coral);
background:#fff4f1;border-radius:0 14px 14px 0;color:#493c3a}} code{{background:#eef2f7;padding:.12em .32em;border-radius:5px}}
pre{{overflow:auto;padding:1rem;background:#182033;color:#edf4ff;border-radius:14px}} a{{color:#385ac8}}
table{{width:100%;border-collapse:separate;border-spacing:0;margin:1.35rem 0;border:1px solid var(--line);
border-radius:18px;overflow:hidden;background:var(--card)}} th,td{{padding:1rem;vertical-align:top;border-bottom:1px solid var(--line)}}
th{{background:#eef7fb;text-align:left}} tr:last-child td{{border-bottom:0}}
table:has(.md-image-local) td:first-child{{width:61%;padding:.65rem;background:#eef4f8}}
table:has(.md-image-local) td:last-child{{width:39%;padding:1.25rem 1.35rem;background:#fff}}
.math-inline{{font-size:1.04em;vertical-align:-.18em}}.math-block{{display:block;max-width:100%;
overflow-x:auto;margin:1.15rem auto;padding:.85rem 1rem;background:#f5f8fc;border:1px solid var(--line);
border-radius:14px;text-align:center}}
.md-image-card{{display:block}} .md-image-local img{{display:block;width:100%;height:auto;max-height:74vh;
object-fit:contain;border-radius:12px;background:#edf1f6}} details{{margin:1.2rem 0;border:1px solid #d7dff0;
border-radius:16px;background:#fafbff;padding:.2rem 1rem 1rem}} summary{{cursor:pointer;font-weight:800;
padding:1rem;color:#4b4291}} small{{color:var(--muted)}} hr{{border:0;border-top:1px solid var(--line);margin:2.4rem 0}}
@media(max-width:760px){{body{{font-size:16px}}article{{padding:1rem;border-radius:18px}}
table:has(.md-image-local),table:has(.md-image-local) tbody,table:has(.md-image-local) tr,
table:has(.md-image-local) td{{display:block;width:100%!important}}table:has(.md-image-local) th{{display:none}}
h2{{font-size:1.45rem}}}}
@media print{{.top{{display:none}}body{{background:#fff}}article{{box-shadow:none;border:0;padding:0}}
details{{display:block}}details>summary{{display:none}}}}
</style></head><body><header class="top"><span class="brand">ClaseViva · modo estudio</span>
<a href="/leccion_final.md">Ver Markdown</a></header><main><article>{cuerpo}</article></main></body></html>"""


@app.get("/leccion_final")
def ver_leccion_final():
    d = _clase()
    if d is None or not (d / "leccion_final.md").is_file():
        return "Lección final no disponible.", 404
    pagina = _pagina_leccion_html(
        (d / "leccion_final.md").read_text(encoding="utf-8"))
    return pagina, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/leccion_final.md")
def ver_leccion_final_markdown():
    d = _clase()
    if d is None or not (d / "leccion_final.md").is_file():
        return "Lección final no disponible.", 404
    return send_from_directory(d, "leccion_final.md",
                               mimetype="text/markdown; charset=utf-8",
                               as_attachment=False)


def _cursos_inicio() -> list[str]:
    cursos = []
    for c in config.cursos_existentes():
        curso_dir = config.dir_curso(c)
        es_curso = (
            (curso_dir / "clases").is_dir()
            or (curso_dir / "resumenes").is_dir()
            or (curso_dir / "CURSO.md").is_file()
            or (curso_dir / "TAREAS.md").is_file()
        )
        # contenido/clases y contenido/resumenes son carpetas legadas, no cursos.
        if c in {"clases", "resumenes"} and not es_curso:
            continue
        cursos.append(c)
    if config.CURSO_DEFAULT not in cursos:
        cursos.insert(0, config.CURSO_DEFAULT)
    return cursos


def _contenido_admin_no_disponible(plan: content_management.DeletionPlan) -> str:
    """Devuelve la causa operativa que impide mover evidencia a Papelera."""
    if _POST_CORRIENDO:
        return "hay una consolidación en curso"
    with _SHUTDOWN_COORDINATOR_LOCK:
        ticket = _LAST_SHUTDOWN_TICKET
    if ticket is not None and not ticket.completed.is_set():
        return "el cierre coordinado todavía está en curso"
    with _HIJOS_LOCK:
        if any(registro["p"].poll() is None for registro in HIJOS):
            return "hay procesos de captura o transcripción activos"
    if student_notes.transcription_in_progress():
        return "hay una nota de voz transcribiéndose"
    for name, module in (
            ("captura_audio", "src.captura_audio"),
            ("captura_pantalla", "src.captura_pantalla"),
            ("transcriptor", "src.transcriptor_vivo"),
            ("escriba", "src.escriba")):
        diagnosis = config.diagnosticar_instancia(name, modulo=module)
        if diagnosis.get("owner_valid"):
            return f"el proceso {name} todavía conserva un propietario válido"
    active = _clase()
    if active is not None:
        target = Path(plan.target).resolve(strict=False)
        current = active.resolve(strict=False)
        targeted = current == target
        if plan.kind == "course":
            try:
                current.relative_to(target)
                targeted = True
            except ValueError:
                pass
        if targeted and est.leer_estado(active).get("estado") != "fin":
            return "la clase seleccionada aún no ha finalizado"
    return ""


def _contenido_plan_from_request() -> content_management.DeletionPlan:
    data = request.get_json(silent=True) or {}
    return content_management.build_deletion_plan(
        config.CONTENIDO_DIR,
        kind=str(data.get("kind") or ""),
        course=str(data.get("course") or ""),
        class_id=str(data.get("class_id") or ""),
    )


@app.get("/api/inicio_info")
def api_inicio_info():
    recovered_deletions = (
        content_management.recover_interrupted_deletions(config.CONTENIDO_DIR)
        if MODO_APP else []
    )
    cursos = _cursos_inicio()
    deletable_courses = config.cursos_existentes()
    clases = {}
    for c in cursos:
        cl = config.dir_curso(c) / "clases"
        clases[c] = sorted(
            p.name for p in cl.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ) if cl.is_dir() else []
    return jsonify({"cursos": cursos, "deletable_courses": deletable_courses,
                    "recovered_deletions": recovered_deletions,
                    "clases": clases, "monitores": _monitores(),
                    "dispositivos": _dispositivos(),
                    "stt_backend": stt_backend.configured_backend()})


@app.post("/api/contenido/vista_previa_eliminar")
def api_contenido_vista_previa_eliminar():
    if not MODO_APP:
        return jsonify({"ok": False, "msg": MODO_VISOR_MSG}), 403
    try:
        plan = _contenido_plan_from_request()
        payload = plan.public()
        payload.update({
            "ok": True,
            "reversible": True,
            "destination": "Papelera del sistema",
        })
        return jsonify(payload)
    except (content_management.ContentSelectionError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400


@app.post("/api/contenido/eliminar")
def api_contenido_eliminar():
    if not MODO_APP:
        return jsonify({"ok": False, "msg": MODO_VISOR_MSG}), 403
    if not _INICIO_LOCK.acquire(blocking=False):
        return jsonify({
            "ok": False,
            "msg": "Inicio o administración de contenido ya está en curso",
        }), 409
    try:
        if not _CONTENT_ADMIN_LOCK.acquire(blocking=False):
            return jsonify({
                "ok": False, "msg": "otra eliminación ya está en curso",
            }), 409
        try:
            data = request.get_json(silent=True) or {}
            plan = _contenido_plan_from_request()
            if reason := _contenido_admin_no_disponible(plan):
                return jsonify({"ok": False, "msg": reason}), 409
            completed = content_management.execute_deletion(
                config.CONTENIDO_DIR,
                kind=plan.kind,
                course=plan.course,
                class_id=plan.class_id,
                confirmation=str(data.get("confirmation") or ""),
                expected_fingerprint=str(data.get("fingerprint") or ""),
            )
            pointer_cleared = content_management.clear_pointer_if_targeted(
                config.PUNTERO_CLASE, config.ROOT, completed.target)
            _panel_cache_clear()
            label = "curso" if completed.kind == "course" else "clase"
            return jsonify({
                "ok": True,
                "kind": completed.kind,
                "course": completed.course,
                "class_id": completed.class_id,
                "pointer_cleared": pointer_cleared,
                "msg": (
                    f"{label.capitalize()} {completed.confirmation} movido a la "
                    "Papelera; puede restaurarse desde Windows."
                ),
            })
        finally:
            _CONTENT_ADMIN_LOCK.release()
    except content_management.ContentSelectionError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400
    except content_management.StaleDeletionPreview as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 409
    except (OSError, RuntimeError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500
    finally:
        _INICIO_LOCK.release()


@app.post("/api/recargar_fuentes")
def api_recargar_fuentes():
    """Reenumera monitores/salidas solo cuando el usuario lo solicita."""
    monitores = _monitores(refresh=True)
    dispositivos = _dispositivos(refresh=True)
    return jsonify({
        "ok": True,
        "monitores": monitores,
        "dispositivos": dispositivos,
        "msg": (
            f"Fuentes actualizadas: {len(monitores)} pantalla(s) y "
            f"{len(dispositivos)} salida(s)."
        ),
    })


@app.post("/api/iniciar")
def api_iniciar():
    if not MODO_APP:
        return jsonify({"ok": False, "msg": MODO_VISOR_MSG})
    if not _INICIO_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "msg": "inicio ya en curso…"})
    try:
        return _iniciar_interno()
    finally:
        _INICIO_LOCK.release()


def _iniciar_interno():
    dd = request.get_json(silent=True) or {}
    activa = _clase()
    if activa is not None and est.leer_estado(activa)["estado"] != "fin" and HIJOS:
        return jsonify({"ok": False, "msg": "ya hay una clase corriendo"})
    # ``groq`` se conserva como alias de lectura para sesiones anteriores.
    selected_backend = str(dd.get("stt_backend") or "remote").casefold()
    remote_consent = bool(dd.get("stt_remote_consent", False))
    stt_check = stt_backend.preflight(
        backend=selected_backend, remote_consent=remote_consent)
    if not stt_check["ok"]:
        return jsonify({
            "ok": False, "reason": "stt_preflight_failed",
            "msg": stt_check["message"],
        }), 409
    curso = dd.get("curso_nuevo", "").strip() or dd.get("curso", config.CURSO_DEFAULT)
    if curso == "__nuevo__":
        curso = config.CURSO_DEFAULT
    if dd.get("tipo") == "continuar" and dd.get("clase"):
        d = config.dir_curso(est._slug(curso)) / "clases" / dd["clase"]
        if not (d / "session_state.json").is_file():
            if not (d / "leccion_final.md").is_file():
                return jsonify({
                    "ok": False,
                    "msg": f"clase no encontrada: {dd['clase']}",
                })
            try:
                est.hidratar_clase_compactada(d)
            except Exception as exc:
                return jsonify({
                    "ok": False,
                    "msg": (
                        "La lección existe, pero no puede continuarse desde su "
                        f"paquete ligero: {type(exc).__name__}: {exc}"),
                })
        st = est.reanudar(d)
        msg = f"continuando como parte {st['parte']}"
    else:
        d = est.crear_clase(dd.get("tema", "").strip() or "clase", curso=curso)
        msg = f"clase creada: {d.name}"
    cambios = {}
    if str(dd.get("monitor", "")).isdigit():
        cambios["monitor"] = int(dd["monitor"])
    fuente = dd.get("fuente", "sistema")
    cambios["fuente"] = fuente
    cambios["dispositivo"] = dd.get("dispositivo", "")
    cambios["audio_modo"] = ""
    cambios["audio_warning"] = ""
    cambios["audio_diag"] = ""
    cambios["stt_backend_requested"] = selected_backend
    cambios["stt_remote_consent"] = bool(remote_consent)
    cambios["stt_provider"] = stt_check.get("provider", "")
    cambios["stt_model"] = stt_check.get("model", "")
    if fuente != "sistema":
        cambios["enfoque"] = False
        msg += " · audio exclusivo por proceso (fallback: enfoque)"
    est.actualizar_estado(d, **cambios)
    _lanzar_hijos()
    return jsonify({"ok": True, "msg": msg + " · grabando ●"})


@app.get("/api/estado")
def api_estado():
    d = _clase()
    legacy_info = _panel_legacy_info()
    evidencia = _panel_evidencia(d)
    base = {"modo_app": MODO_APP,
            "modo_panel": "app" if MODO_APP else "visor",
            "modo_msg": "" if MODO_APP else MODO_VISOR_MSG,
            "contenido_legacy": legacy_info["legacy"],
            "contenido_legacy_msg": legacy_info["msg"],
            "evidencia_resumen": evidencia["resumen"],
            "evidencia_bytes": evidencia["bytes"],
            "post_log": "\n".join(POST_LOG),
            "runtime_health": list(SALUD[-20:]),
            "runtime_health_latest": SALUD[-1] if SALUD else "",
            "stt_runtime_health": list(SALUD[-20:]),
            "stt_runtime_health_latest": SALUD[-1] if SALUD else "",
            "post_corriendo": _POST_CORRIENDO,
            "post_error": _POST_ERROR,
            "hijos": sum(1 for h in HIJOS if h["p"].poll() is None),
            "hijos_esperados": len(MODULOS) if HIJOS else 0,
            "monitores": _monitores(),
            "dispositivos": _dispositivos(),
            "student_notes_enabled": config.student_notes_enabled()}
    if d is None:
        return jsonify({**base, "estado": "sin clase", "parte": "-", "curso": "-", "tema": "-", "fuente": "-",
                        "audio_modo": "-", "dispositivo": "",
                        "velocidad": "-", "enfoque": False, "auto_pausa": False,
                        "monitor": 1, "segmentos": 0, "costo": "0.000",
                        "costo_display": "Sesión $0.000 · Curso $0.000",
                        "costo_sesion": "0.000",
                        "costo_clase": "0.000", "costo_curso": "0.000",
                        "costos_lecciones": [],
                        "costo_etiqueta": "Gasto API",
                        "costo_nota": ("Sin curso activo: los contadores de sesión y curso "
                                       "inician en cero."),
                        "leccion_html": "", "ticker": "", "trans_diag": "",
                        "fuente_audio": "-",
                        "ahora_rel": "-", "parte_inicio_rel": "-",
                        "ultimo_segmento_rel": "-",
                         "audio_warning": "", "audio_diag": "", "audio_hint": "",
                         "wav_persistence": {},
                         "stt_pending_count": 0, "stt_invalid_count": 0,
                         "stt_empty_completed_count": 0,
                         "audio_state": "closed", "audio_state_label": "Audio cerrado",
                         "audio_state_detail": "Sin clase activa.",
                         "audio_valid_wav_count": 0, "audio_healthy": False,
                         "stt_state": "no_valid_audio",
                         "stt_state_label": "STT sin audio válido",
                         "stt_state_detail": "0 audio válido → no hay trabajo STT remoto.",
                         "stt_has_work": False, "stt_process_expected": False,
                         "stt_transcribed_until_sec": 0.0,
                         "stt_transcribed_until": "-",
                         "stt_delay_sec": 0.0, "stt_delay": "-",
                         "stt_hardware_label": "-", "stt_hardware": {},
                         "stt_readiness": "", "stt_backend": "",
                         "hijos_esperados_activos": 0,
                         "resumen_estado": "", "resumen_msg": "",
                        "clase_id": "",
                        "leccion_final_existe": False,
                        "leccion_final_actualizada": False,
                        "leccion_final_nuevas": [],
                        "consolidation_ready": False,
                        "consolidation_status": "class_not_finished",
                        "consolidation_warnings": [],
                        "consolidation_barrier_reasons": ["class_not_finished"],
                        "shutdown_blocking": True,
                        "post_estado": "sin_clase",
                        "post_titulo": "Sin clase",
                        "post_mensaje": "Selecciona curso y sesión para empezar.",
                        "estado_efectivo": "sin_clase",
                        "controles": {
                            "pausar": False, "seguir": False,
                            "reintentar_audio": False, "cerrar_parte": False,
                            "finalizar": False, "seguir_label": "Seguir",
                        },
                        "elapsed_wall_sec": 0.0,
                        "recorded_elapsed_sec": 0.0,
                        "paused_elapsed_sec": 0.0,
                        "video_equivalent_elapsed_sec": 0.0,
                        "elapsed_wall": "-", "recorded_elapsed": "-",
                        "paused_elapsed": "-", "video_equivalent_elapsed": "-",
                        "duracion": "-", "duracion_parte": "-", "inicio_abs": "-",
                        "ahora_abs": _fmt_abs(time.time()), "parte_inicio_abs": "-",
                        "ultimo_segmento_abs": "-", "ultima_captura_abs": "-",
                        "modo_tiempo": "-"})
    st = est.leer_estado(d)
    # GET /api/estado es observacional: defaults y estados de UI legados se
    # normalizan en memoria para que un poll o una prueba nunca escriban evidencia.
    if st.get("estado") == "fin" and st.get("resumir"):
        st = {**st, "resumir": False, "resumen_estado": "", "resumen_msg": ""}
    ahora = time.time()
    curso_actual = st.get("curso", est.curso_de(d))
    final_info = _leccion_final_info(d)
    operativo = _estado_operativo(st, final_info)
    marcas = est.leer_marcas(d)
    metricas_tiempo = est.metricas_tiempo(st, marcas, ahora)
    controles = est.capacidades_estado(st)
    t_inicio, _t_fin = _rango_clase(d, st, ahora, marcas=marcas)
    t_parte = _inicio_parte(d, st)
    duracion_activa = _segundos_almacenando(d, st, ahora, marcas=marcas)
    duracion_parte_activa = _segundos_almacenando(
        d, st, ahora, desde=t_parte, marcas=marcas)
    costos = _panel_costos_curso(curso_actual, ahora, clase_actual=d)
    costo_clase = costos["clase_actual_usd"]
    costo_curso = costos["total_usd"]
    vel = st.get("velocidad", 1.0)
    modo_tiempo = f"video {vel}x" if isinstance(vel, (int, float)) and float(vel) > 1 else "en vivo"
    # LIVE usa metadatos y nombres para no rehashear todos los WAV en cada poll.
    # Fin y consolidación vuelven a validar los bytes físicos obligatoriamente.
    stt_estado = _panel_stt_status(
        d, verify_files=str(st.get("estado") or "") == "fin")
    segmentos_total = int(stt_estado.get("segment_count") or 0)
    consolidation_gate = _consolidation_gate(
        d, st, stt_status=stt_estado)
    if operativo["clave"] == "pendiente":
        if consolidation_gate["ready"] and consolidation_gate["warnings"]:
            operativo = {
                **operativo,
                "detalle": (
                    "Cierre completo con advertencias de cobertura; puedes "
                    "consolidar y la lección conservará esas limitaciones."),
            }
        elif consolidation_gate["shutdown_blocking"]:
            operativo = {
                "clave": "pendiente",
                "titulo": "Finalizando evidencia",
                "detalle": (
                    "Esperando el cierre verificable de captura, WAV y STT "
                    "antes de consolidar."),
            }
    audio_runtime = clasificar_estado_audio(
        st, valid_wav_count=stt_estado["available_audio_count"])
    stt_runtime = clasificar_estado_stt(st, stt_estado)
    stt_supervisor = st.get("stt_supervisor")
    if not isinstance(stt_supervisor, dict):
        stt_supervisor = {}
    stt_lifecycle = st.get("stt_lifecycle")
    if not isinstance(stt_lifecycle, dict):
        stt_lifecycle = {}
    stt_hardware_ui = _stt_hardware_ui(st)
    pendientes = stt_estado["unresolved_chunks"]
    stt_until_wall = _numero_no_negativo(st.get("stt_transcribed_until_wall"))
    ultimo_t = stt_until_wall
    audio_until_wall = _numero_no_negativo(st.get("audio_last_valid_wall_end"))
    stt_until_sec = max(0.0, stt_until_wall - t_inicio) if stt_until_wall else 0.0
    stt_delay_sec = (
        max(0.0, audio_until_wall - max(stt_until_wall, t_inicio))
        if audio_until_wall else 0.0)
    diag = (f"{stt_runtime['label']} · {stt_runtime['detail']} · "
            f"{segmentos_total} segmentos canónicos"
            f" · wav pendientes {len(pendientes)}")
    if stt_estado["invalid_count"]:
        diag += f" · STT inválidos {stt_estado['invalid_count']}"
    if stt_estado["empty_completed_count"]:
        diag += f" · STT vacíos completos {stt_estado['empty_completed_count']}"
    wav_persistence = st.get("wav_persistence")
    if not isinstance(wav_persistence, dict):
        wav_persistence = {}
    if wav_persistence:
        profundidad = int(wav_persistence.get("queue_depth") or 0)
        maximo = int(
            wav_persistence.get("queue_depth_max")
            or wav_persistence.get("queue_capacity")
            or 0)
        pendientes_wav = int(wav_persistence.get("pending_jobs") or 0)
        diag += f" · WAV cola {profundidad}/{maximo} · pendientes {pendientes_wav}"
    if ultimo_t:
        diag += f" · último segmento {_fmt_abs(ultimo_t)}"
    if st.get("audio_modo"):
        diag += f" · {st['audio_modo']}"
    stt_modelo = st.get("stt_model")
    if stt_modelo:
        stt_partes = [str(stt_modelo)]
        if st.get("stt_device"):
            stt_partes.append(str(st.get("stt_device")))
        if st.get("stt_gpu"):
            stt_partes.append(str(st.get("stt_gpu")))
        if st.get("stt_compute"):
            stt_partes.append(str(st.get("stt_compute")))
        diag += " - STT " + " / ".join(stt_partes)
    if st.get("stt_backlog_wav") is not None:
        diag += f" · STT cola {st.get('stt_backlog_wav')} wav"
    if st.get("stt_ultimo_transcribe_sec") is not None:
        diag += (f" · último STT {st.get('stt_ultimo_transcribe_sec')}s"
                 f"/{st.get('stt_ultimo_audio_reloj_sec', '?')}s")
    if st.get("stt_commit_latency_sec") is not None:
        diag += f" · latencia commit {st.get('stt_commit_latency_sec')}s"
    if st.get("stt_real_time_factor") is not None:
        diag += f" · RTF {st.get('stt_real_time_factor')}"
    diag += f" · hardware {stt_hardware_ui['label']}"
    fuente_audio = st.get("audio_modo") or st.get("fuente", "sistema")
    audio_ruta, audio_ruta_detalle = _audio_ruta_ui(
        st.get("audio_modo", ""), st.get("fuente", "sistema"))
    audio_warning = st.get("audio_warning", "")
    audio_diag = st.get("audio_diag", "")
    audio_hint = ""
    if audio_warning and st.get("estado") in {"grabando", "pausado"} and st.get("fuente") in audio_sesiones.NAVEGADORES:
        audio_hint = captura_audio.mensaje_video_pausado(st.get("fuente", "chrome"))
    if audio_warning:
        diag += f" · aviso audio: {audio_warning}"
    if st.get("audio_ultimo_rms") is not None:
        diag += (f" · audio rms={st.get('audio_ultimo_rms')}"
                 f" peak={st.get('audio_ultimo_peak')}"
                 f" pico={st.get('audio_ultimo_pico_sesion')}")
    resumen_estado = str(st.get("resumen_estado", "") or "")
    resumen_msg = str(st.get("resumen_msg", "") or "")
    if resumen_estado == "listo":
        resumen_estado = "generado"
    if st.get("resumir") and resumen_estado not in {"generando", "pendiente"}:
        resumen_estado = "pendiente"
    if resumen_estado in {"generando", "pendiente"} and not resumen_msg:
        resumen_msg = "generando resumen..."
    ultima_obj = _panel_last_capture(d)
    ultima = ultima_obj["archivo"] if ultima_obj else ""
    lec = d / "leccion_en_vivo.md"
    texto = _panel_text(lec)
    vista_leccion_html = _panel_live_lesson_html(d, st["parte"], texto)
    return jsonify({
        **base,
        "estado": st["estado"], "auto_pausa": st.get("auto_pausa", False),
        "shutdown_state": str(st.get("shutdown_state") or ""),
        "shutdown_blocking": consolidation_gate["shutdown_blocking"],
        "consolidation_ready": consolidation_gate["ready"],
        "consolidation_status": consolidation_gate["status"],
        "consolidation_warnings": consolidation_gate["warnings"],
        "consolidation_barrier_reasons": consolidation_gate["reasons"],
        "estado_efectivo": controles["estado_efectivo"],
        "controles": controles,
        "clase_id": st.get("clase_id", d.name),
        "leccion_final_existe": final_info["existe"],
        "leccion_final_actualizada": final_info["actualizada"],
        "leccion_final_nuevas": final_info["nuevas"],
        "post_estado": operativo["clave"],
        "post_titulo": operativo["titulo"],
        "post_mensaje": operativo["detalle"],
        "parte": st["parte"], "curso": curso_actual,
        "tema": st.get("nombre", d.name), "fuente": st.get("fuente", "sistema"),
        "audio_modo": st.get("audio_modo", "") or "-",
        "dispositivo": st.get("dispositivo", ""),
        "velocidad": vel, "enfoque": st.get("enfoque", False),
        "monitor": st.get("monitor") or config.MONITOR,
        "segmentos": segmentos_total,
        "costo": f"{costo_clase:.3f}",
        "costo_display": f"Sesión ${costo_clase:.3f} · Curso ${costo_curso:.3f}",
        "costo_sesion": f"{costo_clase:.3f}",
        "costo_clase": f"{costo_clase:.3f}",
        "costo_curso": f"{costo_curso:.3f}",
        "costos_lecciones": costos["lecciones"],
        "costo_etiqueta": "Gasto API",
        "costo_nota": ("Sesión: gasto de esta clase. Curso: acumulado de sus clases. "
                       "Los logs nuevos se atribuyen por curso/clase; los antiguos usan "
                       "fallback por rango temporal."),
        "leccion_html": vista_leccion_html,
        "fuente_audio": fuente_audio,
        "audio_ruta": audio_ruta,
        "audio_ruta_detalle": audio_ruta_detalle,
        "audio_ultimo_rms": st.get("audio_ultimo_rms"),
        "audio_ultimo_peak": st.get("audio_ultimo_peak"),
        "audio_ultimo_pico_sesion": st.get("audio_ultimo_pico_sesion"),
        "audio_warning": audio_warning,
        "audio_diag": audio_diag,
        "audio_hint": audio_hint,
        "audio_state": audio_runtime["code"],
        "audio_phase": audio_runtime["phase"],
        "audio_evidence": audio_runtime["evidence"],
        "audio_state_label": audio_runtime["label"],
        "audio_state_detail": audio_runtime["detail"],
        "audio_valid_wav_count": audio_runtime["valid_wav_count"],
        "audio_healthy": audio_runtime["healthy"],
        "audio_action_required": audio_runtime["action_required"],
        "wav_persistence": wav_persistence,
        "stt_state_source": stt_estado["source"],
        "stt_pending_count": stt_estado["unresolved_count"],
        "stt_actionable_count": stt_estado["actionable_count"],
        "stt_missing_audio_count": stt_estado["missing_audio_count"],
        "stt_invalid_count": stt_estado["invalid_count"],
        "stt_empty_completed_count": stt_estado["empty_completed_count"],
        "stt_state": stt_runtime["code"],
        "stt_state_label": stt_runtime["label"],
        "stt_state_detail": stt_runtime["detail"],
        "stt_process_state": stt_runtime["process_code"],
        "stt_last_commit_status": stt_runtime["last_commit_status"],
        "stt_has_work": stt_runtime["has_work"],
        "stt_process_expected": stt_runtime["process_expected"],
        "stt_transcribed_until_sec": stt_until_sec,
        "stt_transcribed_until": (
            config.hhmmss_rel(stt_until_sec) if stt_until_wall else "-"),
        "stt_delay_sec": stt_delay_sec,
        "stt_delay": (
            config.hhmmss_rel(stt_delay_sec) if audio_until_wall else "-"),
        "stt_hardware_label": stt_hardware_ui["label"],
        "stt_hardware": stt_hardware_ui["probe"],
        "stt_backend_requested": str(
            st.get("stt_backend_requested") or "remote"),
        "stt_readiness": stt_hardware_ui["readiness"],
        "stt_backend": stt_hardware_ui["backend"],
        "stt_fallback_reason": stt_hardware_ui["fallback_reason"],
        "stt_returncode": stt_supervisor.get("returncode"),
        "stt_exit_cause": str(
            stt_supervisor.get("status") or stt_lifecycle.get("status") or ""),
        "stt_attempt": _entero_no_negativo(stt_supervisor.get("attempt")),
        "stt_mode": str(stt_supervisor.get("mode") or ""),
        "stt_lock_owner": str(stt_supervisor.get("lock_owner") or ""),
        "stt_last_heartbeat": _numero_no_negativo(
            stt_supervisor.get("last_heartbeat")
            or stt_lifecycle.get("heartbeat")),
        "stt_last_commit": str(
            stt_lifecycle.get("last_commit") or st.get("stt_ultimo_chunk") or ""),
        "hijos_esperados_activos": max(
            0, int(base["hijos_esperados"])
            - (0 if stt_runtime["process_expected"] else 1)),
        "resumen_estado": resumen_estado,
        "resumen_msg": resumen_msg,
        "trans_diag": diag,
        "ultima_captura": ultima,
        "ultima_captura_abs": _fmt_abs(ultima_obj.get("t") if ultima_obj else None),
        "apuntes_auto": st.get("apuntes_auto", 0),
        "curso_html": _panel_course_html(d),
        "duracion": config.hhmmss_rel(duracion_activa),
        "duracion_parte": config.hhmmss_rel(duracion_parte_activa),
        **metricas_tiempo,
        "elapsed_wall": config.hhmmss_rel(metricas_tiempo["elapsed_wall_sec"]),
        "recorded_elapsed": config.hhmmss_rel(
            metricas_tiempo["recorded_elapsed_sec"]),
        "paused_elapsed": config.hhmmss_rel(
            metricas_tiempo["paused_elapsed_sec"]),
        "video_equivalent_elapsed": config.hhmmss_rel(
            metricas_tiempo["video_equivalent_elapsed_sec"]),
        "inicio_abs": _fmt_abs(t_inicio),
        "ahora_abs": _fmt_abs(ahora),
        "parte_inicio_abs": _fmt_abs(t_parte),
        "ultimo_segmento_abs": _fmt_abs(ultimo_t),
        "ahora_rel": _fmt_rel(duracion_activa),
        "parte_inicio_rel": _fmt_rel(t_parte - t_inicio),
        "ultimo_segmento_rel": _fmt_rel(float(ultimo_t or t_inicio) - t_inicio),
        "modo_tiempo": modo_tiempo,
    })


@app.post("/api/comando")
def api_comando():
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"})
    cmd = (request.get_json(silent=True) or {}).get("cmd", "").strip()
    st = est.leer_estado(d)
    palabra, _, arg = cmd.partition(" ")
    arg = arg.strip()
    estado_actual = st.get("estado")
    if estado_actual == "fin":
        if palabra in {"pausa", "seguir", "parte", "velocidad", "fuente", "dispositivo",
                       "enfoque", "monitor", "apuntes", "resumir"}:
            return jsonify({"ok": False, "msg": "La clase ya finalizó. Consolida la lección final o inicia/continúa una clase."})
    if palabra == "parte" and est.estado_efectivo(st) != "grabando_activo":
        return jsonify({"ok": False, "msg": "Cerrar parte requiere captura activa."})
    ok, msg = True, "ok"
    transicion = None
    if palabra == "pausa":
        transicion = est.transicionar_captura(d, "pausa")
        msg = transicion["msg"]
    elif palabra == "seguir":
        transicion = est.transicionar_captura(d, "seguir")
        msg = transicion["msg"]
    elif palabra == "parte":
        est.marcar(d, "fin_parte", nota=arg)
        est.actualizar_estado(d, parte=st["parte"] + 1)
        msg = f"🧩 parte {st['parte']} cerrada; ahora parte {st['parte'] + 1}"
    elif palabra == "fin":
        pending_voice = local_interventions.pending_report(d)
        if pending_voice["blocking"]:
            return jsonify({
                "ok": False,
                "msg": (
                    "Guarda o descarta la intervención de micrófono pendiente "
                    "antes de finalizar la clase."
                ),
            }), 409
        audio_sesiones.restaurar_enfoques()
        transicion = est.transicionar_captura(d, "fin")
        if MODO_APP and transicion["changed"]:
            _request_runtime_shutdown("fin_clase", False)
        msg = ("🏁 finalizando captura; la consolidación se habilitará al drenar todo"
               if transicion["changed"] else transicion["msg"])
    elif palabra == "velocidad":
        try:
            v = float(arg)
            assert v in (1.0, 1.25, 1.5, 1.75, 2.0)
            est.actualizar_estado(d, velocidad=v)
            msg = (f"✅ velocidad {v}x aplicada — recuerda poner el reproductor a {v}x"
                   if v > 1 else "✅ velocidad normal (1x)")
        except (ValueError, AssertionError):
            ok, msg = False, "velocidad inválida"
    elif palabra == "fuente":
        if arg in ("chrome", "msedge", "sistema"):
            cambios_fuente = {
                "fuente": arg,
                "audio_modo": "",
                "audio_warning": "",
                "audio_diag": "",
            }
            if arg != st.get("fuente", "sistema"):
                audio_sesiones.restaurar_enfoques()
                cambios_fuente["enfoque"] = False
            est.actualizar_estado(d, **cambios_fuente)
            msg = f"✅ fuente de audio: {arg}"
        else:
            ok, msg = False, "fuente inválida"
    elif palabra == "dispositivo":
        ids = {x["id"]: x["etiqueta"] for x in _dispositivos()}
        if arg in ids:
            est.actualizar_estado(d, dispositivo=arg, audio_modo="", audio_warning="", audio_diag="")
            msg = f"🎧 dispositivo fallback: {ids[arg]}"
        else:
            ok, msg = False, "dispositivo inválido"
    elif palabra == "enfoque":
        activar = arg == "on"
        n = (audio_sesiones.enfoque(st.get("fuente", "sistema"), True)
             if activar else audio_sesiones.restaurar_enfoques())
        est.actualizar_estado(d, enfoque=activar)
        msg = f"🎯 enfoque {'ON' if activar else 'OFF'} ({n} apps {'silenciadas' if activar else 'restauradas'})"
    elif palabra == "monitor":
        if arg.isdigit():
            selected = int(arg)
            ets = {int(m["idx"]): m["etiqueta"] for m in _monitores()}
            if selected not in ets:
                return jsonify({
                    "ok": False,
                    "msg": "pantalla no disponible; pulsa Actualizar pantallas",
                })
            changes = {"monitor": selected}
            if selected != int(st.get("monitor") or config.MONITOR):
                # La región usa coordenadas de la pantalla anterior.
                changes["region"] = None
            est.actualizar_estado(d, **changes)
            msg = f"🖥️ capturando: {ets.get(int(arg), 'pantalla ' + arg)}"
        else:
            ok, msg = False, "monitor inválido"
    elif palabra == "apuntes":
        try:
            m = float(arg)
            assert 0 <= m <= 60
            est.actualizar_estado(d, apuntes_auto=m)
            msg = ("✍️ apuntes SOLO manuales (📌)" if m == 0
                   else f"✍️ apuntes automáticos cada {m:g} min")
        except (ValueError, AssertionError):
            ok, msg = False, "intervalo inválido"
    elif palabra == "resumir":
        if st["estado"] not in {"grabando", "pausado"}:
            ok, msg = False, "solo durante la grabación"
        elif st.get("resumir"):
            est.actualizar_estado(
                d, resumen_estado=st.get("resumen_estado") or "pendiente",
                resumen_msg=st.get("resumen_msg") or "generando resumen...",
                resumen_t=time.time())
            msg = "Resumen solicitado"
        else:
            solicitado_t = time.time()
            est.actualizar_estado(
                d, resumir=True, resumen_estado="pendiente",
                resumen_msg="generando resumen...", resumen_t=solicitado_t,
                resumen_solicitado_t=solicitado_t)
            msg = "Resumen solicitado"
    else:
        ok, msg = False, f"comando desconocido: {palabra}"
    respuesta = {"ok": ok, "msg": msg}
    if transicion is not None:
        respuesta.update({
            "changed": transicion["changed"],
            "estado_efectivo": transicion["effective_state"],
        })
    return jsonify(respuesta)


class _EscritorLog:
    def write(self, s):
        for ln in s.splitlines():
            if ln.strip():
                _post_log_append(ln.strip())

    def flush(self):
        pass


def _iniciar_worker_post(target) -> None:
    threading.Thread(target=target, daemon=True).start()


@app.post("/api/consolidar")
def api_consolidar():
    global _POST_CORRIENDO, _POST_ERROR
    payload = request.get_json(silent=True) or {}
    paquete_ligero = (
        payload.get("limpiar") is not False
        and payload.get("conservar_evidencia") is not True
    )
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase"})
    estado_actual = est.leer_estado(d)
    if estado_actual["estado"] != "fin":
        return jsonify({"ok": False, "msg": "Primero finaliza la clase."})
    pending_voice = local_interventions.pending_report(d)
    if pending_voice["blocking"]:
        return jsonify({
            "ok": False,
            "reason": "local_intervention_pending",
            "msg": (
                "Hay una intervención de micrófono sin decidir. "
                "Guárdala o descártala antes de consolidar."
            ),
        }), 409
    notes_ready = student_notes.package_readiness_report(d)
    if not notes_ready["ok"]:
        reasons = sorted({
            str(item.get("reason") or "student_note_not_ready")
            for item in notes_ready["issues"]
        })
        return jsonify({
            "ok": False,
            "reason": "student_notes_not_ready",
            "note_issues": notes_ready["issues"],
            "msg": (
                "Hay notas del estudiante sin decisión o sin texto extraído. "
                "Inclúyelas, transcribe/corrige su contenido, exclúyelas o "
                f"descártalas antes de consolidar ({', '.join(reasons)})."
            ),
        }), 409
    pending_local_stt = student_notes.pending_audio_notes(
        d, source_kinds={"local_intervention"}, included_only=True)
    if pending_local_stt:
        return jsonify({
            "ok": False,
            "reason": "local_intervention_stt_pending",
            "msg": (
                "La intervención guardada aún se está transcribiendo con el "
                "mismo STT remoto. Espera a que termine antes de consolidar."
            ),
        }), 409
    unresolved_local = student_notes.unresolved_local_interventions(d)
    if unresolved_local:
        return jsonify({
            "ok": False,
            "reason": "local_intervention_stt_failed",
            "msg": (
                "Una intervención propia no tiene texto utilizable. Corrige su texto "
                "en Notas del estudiante o descártala antes de consolidar."
            ),
        }), 409
    reporte_stt = audio_manifest.transcript_status_report(d, verify_files=True)
    consolidation_gate = _consolidation_gate(
        d, estado_actual, stt_status=reporte_stt, fresh=True)
    if not consolidation_gate["ready"]:
        razones = consolidation_gate["reasons"]
        detalle = ", ".join(razones) if razones else "barrera no disponible"
        return jsonify({
            "ok": False,
            "reason": "shutdown_not_complete",
            "barrier_status": consolidation_gate["status"],
            "barrier_reasons": razones,
            "msg": (
                "Aún se están drenando captura, WAV o STT; "
                f"espera el cierre completo ({detalle})."),
        })
    audio_validos = int(reporte_stt.get("available_audio_count") or 0)
    segmentos_vista = len(est.leer_jsonl(d / "transcript.jsonl"))
    segmentos_transcript = max(
        segmentos_vista, int(reporte_stt.get("segment_count") or 0))
    sin_evidencia_audio = audio_validos == 0 and segmentos_transcript == 0
    if sin_evidencia_audio and payload.get("confirmar_sin_evidencia") is not True:
        return jsonify({
            "ok": False,
            "requires_confirmation": True,
            "confirmation_required": True,
            "reason": "zero_audio_and_transcript",
            "audio_valid_wav_count": audio_validos,
            "transcript_segment_count": segmentos_transcript,
            "msg": (
                "La clase tiene 0 WAV válidos y 0 segmentos de transcript. "
                "El STT remoto no tuvo trabajo. ¿Consolidar de todos modos usando solo "
                "la evidencia visual y dejando esta limitación explícita?"),
        })
    with _POST_LOCK:
        reservado = not _POST_CORRIENDO
        if reservado:
            _POST_CORRIENDO = True
            _POST_ERROR = ""
    if not reservado:
        return jsonify({"ok": False, "msg": "La consolidación ya está en curso. Mira Post-proceso."})

    def correr():
        global _POST_CORRIENDO, _POST_ERROR
        try:
            POST_LOG.clear()
            _post_log_append(
                "=== Consolidando leccion final"
                + (" como paquete ligero reanudable ==="
                   if paquete_ligero else " conservando evidencia por solicitud ==="))
            if sin_evidencia_audio:
                _post_log_append(
                    "ADVERTENCIA confirmada: 0 WAV validos y 0 segmentos de transcript; "
                    "la salida no se considera validacion de audio/STT.")
            from . import post_procesar
            with contextlib.redirect_stdout(_EscritorLog()):
                post_procesar.consolidar_todo(
                    d,
                    forzar=True,
                    limpiar_al_final=paquete_ligero,
                    paquete_ligero=paquete_ligero,
                )
            _post_log_append("OK Leccion consolidada: leccion_final.md regenerada.")
            if paquete_ligero:
                _post_log_append(
                    "OK Clase cerrada como leccion_final.md + imágenes referenciadas.")
            else:
                _post_log_append("OK Evidencia conservada por solicitud explícita.")
        except Exception as e:
            _POST_ERROR = f"{type(e).__name__}: {e}"
            _post_log_append(f"ERROR al consolidar: {_POST_ERROR}")
        finally:
            from . import ocr
            ocr.cerrar_motor()
            with _POST_LOCK:
                _POST_CORRIENDO = False

    try:
        _iniciar_worker_post(correr)
    except Exception as e:
        with _POST_LOCK:
            _POST_ERROR = f"{type(e).__name__}: {e}"
            _POST_CORRIENDO = False
        _post_log_append(f"ERROR al iniciar consolidación: {_POST_ERROR}")
        return jsonify({
            "ok": False,
            "msg": f"No se pudo iniciar la consolidación: {_POST_ERROR}",
        })
    return jsonify({
        "ok": True,
        "msg": "Consolidando… sigue el avance en Post-proceso.",
        "accepted_without_audio": sin_evidencia_audio,
        "audio_valid_wav_count": audio_validos,
        "transcript_segment_count": segmentos_transcript,
        "consolidation_status": consolidation_gate["status"],
        "consolidation_warnings": consolidation_gate["warnings"],
    })


@app.post("/api/compactar_dry_run")
def api_compactar_dry_run():
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase"})
    try:
        from .compactar_clase import plan_compactacion
        plan = plan_compactacion(d)
        if not plan.get("ok"):
            return jsonify({"ok": False, "msg": plan.get("motivo", "no compactable"),
                            "plan": plan})
        lineas = [
            "== Compactar clase (dry-run) ==",
            f"Backup propuesto: {plan['backup_dir']}",
            f"Rutas a conservar: {len(plan['conservar'])}",
            f"Rutas que se moverían: {len(plan['mover'])}",
            f"Bytes ahorrables: {plan['bytes_ahorrables']}",
            f"Imágenes referenciadas: {len(plan['imagenes_referenciadas'])}",
        ]
        if plan.get("riesgos"):
            lineas.append("Riesgos:")
            lineas.extend(f"- {r}" for r in plan["riesgos"])
        else:
            lineas.append("Riesgos: ninguno")
        return jsonify({"ok": True, "msg": "Dry-run calculado; no se movió nada.",
                        "plan": plan, "resumen": "\n".join(lineas)})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"{type(e).__name__}: {e}"})


@app.post("/api/cerrar_app")
def api_cerrar_app():
    ticket = _request_runtime_shutdown("api_cerrar_app", True)
    return jsonify({
        "ok": True,
        "accepted": bool(ticket.accepted),
        "msg": (
            "Cierre coordinado iniciado; esperando productores y consumidores."
            if ticket.accepted else
            "El cierre coordinado ya está en curso."
        ),
    })


@app.post("/api/pregunta")
def api_pregunta():
    d = _clase()
    if d is None:
        r = "sin clase activa"
        return jsonify({"ok": False, "respuesta": r, "respuesta_html": a_html(r)})
    dd = request.get_json(silent=True) or {}
    q = str(dd.get("q") or "").strip()
    captura = Path(str(dd.get("captura") or "")).name
    if not q and not captura:
        r = "escribe una pregunta o adjunta una captura"
        return jsonify({"ok": False, "respuesta": r, "respuesta_html": a_html(r)})
    usar_web = bool(dd.get("web"))
    try:
        if captura:
            r = copiloto.responder_con_captura(d, q, captura, usar_web=usar_web)
        else:
            r = copiloto.responder(d, q, usar_web=usar_web)
            copiloto.registrar_qa(d, q, r)
        return jsonify({"ok": True, "respuesta": r, "respuesta_html": a_html(r)})
    except Exception as e:
        config.log_error("panel-pregunta", f"{type(e).__name__}: {e}")
        r = f"⚠️ error interno del copiloto: {type(e).__name__}. Revisa logs/errores.log"
        return jsonify({"ok": False, "respuesta": r, "respuesta_html": a_html(r)})


@app.post("/api/captura_previa")
def api_captura_previa():
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa", "captura": ""})
    nombre = copiloto.capturar_para_pregunta(d)
    return jsonify({"ok": True, "captura": nombre})


@app.post("/api/captura_previa/descartar")
def api_descartar_captura_previa():
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    nombre = str((request.get_json(silent=True) or {}).get("captura") or "")
    try:
        copiloto.descartar_captura_previa(d, nombre)
        return jsonify({"ok": True, "msg": "captura descartada"})
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 404
    except RuntimeError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 409
    except (ValueError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400


@app.post("/api/region_shot")
def api_region_shot():
    """Captura el MONITOR COMPLETO a resolución nativa (sin recorte ni reescala) para
    que el usuario arrastre el rectángulo del video del profesor sobre píxeles reales."""
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa", "captura": ""})
    from .captura_pantalla import capturar_ahora
    st = est.leer_estado(d)
    img = capturar_ahora(monitor=st.get("monitor"))  # SIN region: monitor completo
    nombre = f"region_{int(time.time())}.jpg"
    img.save(d / "img" / nombre, format="JPEG", quality=80)
    return jsonify({"ok": True, "captura": nombre})


@app.post("/api/region")
def api_region():
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"})
    dd = request.get_json(silent=True) or {}
    if dd.get("quitar"):
        est.actualizar_estado(d, region=None)
        return jsonify({"ok": True, "msg": "✂️ región quitada: se captura la pantalla completa"})
    try:
        x, y, w, h = int(dd["x"]), int(dd["y"]), int(dd["w"]), int(dd["h"])
        st = est.leer_estado(d)
        from .captura_pantalla import monitores_disponibles
        mons = monitores_disponibles()
        idx = (st.get("monitor") or config.MONITOR) - 1
        m = mons[idx if 0 <= idx < len(mons) else 0]
        region = [m["left"] + x, m["top"] + y, w, h]
        est.actualizar_estado(d, region=region)
        return jsonify({"ok": True, "msg": f"✂️ región {w}x{h} aplicada: todas las capturas "
                                           f"salen recortadas al video del profesor"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"región inválida: {e}"})


@app.post("/api/curso_nota")
def api_curso_nota():
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"})
    nota = str((request.get_json(silent=True) or {}).get("nota") or "").strip()
    if not nota:
        return jsonify({"ok": False, "msg": "nota vacía"})
    p = est.curso_md(d)
    with est._bloqueo_estado(d):
        if not p.is_file():
            p.write_text("# 📒 Bitácora del curso\n", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n- 📝 [{time.strftime('%Y-%m-%d %H:%M')}] {nota}\n")
    return jsonify({"ok": True, "msg": "📒 nota añadida a la bitácora del curso"})


def _student_note_seconds(d: Path, supplied=None) -> float:
    try:
        if supplied is not None and str(supplied).strip():
            return max(0.0, float(supplied))
    except (TypeError, ValueError):
        pass
    st = est.leer_estado(d)
    try:
        return max(0.0, time.time() - float(st.get("inicio") or time.time()))
    except (TypeError, ValueError):
        return 0.0


def _student_notes_disabled_response():
    if config.student_notes_enabled():
        return None
    return jsonify({
        "ok": False,
        "msg": "Notas del estudiante deshabilitadas por CLASE_STUDENT_NOTES=0",
    }), 503


@app.get("/api/local_interventions")
def api_local_interventions_list():
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa", "drafts": []}), 404
    return jsonify({"ok": True, "drafts": local_interventions.list_drafts(d)})


@app.post("/api/local_interventions")
def api_local_interventions_create():
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    if local_interventions.pending_report(d)["blocking"]:
        return jsonify({
            "ok": False,
            "msg": "Ya existe una intervención pendiente; guárdala o descártala.",
        }), 409
    payload = request.get_json(silent=True) or {}
    try:
        draft = local_interventions.create_draft(
            d,
            mime=str(payload.get("mime") or "audio/webm"),
            device_id=str(payload.get("device_id") or ""),
            device_label=str(payload.get("device_label") or "Micrófono seleccionado"),
            class_seconds=_student_note_seconds(d, payload.get("class_seconds")),
        )
        return jsonify({"ok": True, "draft": draft})
    except (ValueError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400


@app.post("/api/local_interventions/<draft_id>/chunks/<int:sequence>")
def api_local_interventions_chunk(draft_id, sequence):
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    try:
        report = local_interventions.append_chunk(
            d, draft_id, sequence, request.get_data(cache=False))
        return jsonify({"ok": True, **report})
    except KeyError:
        return jsonify({"ok": False, "msg": "borrador no encontrado"}), 404
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 409


@app.post("/api/local_interventions/<draft_id>/seal")
def api_local_interventions_seal(draft_id):
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    try:
        return jsonify({
            "ok": True,
            "draft": local_interventions.seal_draft(d, draft_id),
        })
    except KeyError:
        return jsonify({"ok": False, "msg": "borrador no encontrado"}), 404
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 409


@app.get("/api/local_interventions/<draft_id>/preview")
def api_local_interventions_preview(draft_id):
    d = _clase()
    if d is None:
        return ("", 404)
    try:
        path = local_interventions.preview_path(d, draft_id)
        return send_from_directory(path.parent, path.name, as_attachment=False)
    except (KeyError, RuntimeError, OSError, ValueError):
        return ("", 404)


@app.post("/api/local_interventions/<draft_id>/commit")
def api_local_interventions_commit(draft_id):
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    try:
        note = local_interventions.commit_draft(d, draft_id)
        return jsonify({
            "ok": True, "note": note,
            "msg": "Intervención guardada e incluida como aporte del estudiante.",
        })
    except KeyError:
        return jsonify({"ok": False, "msg": "borrador no encontrado"}), 404
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 409


@app.post("/api/local_interventions/<draft_id>/discard")
def api_local_interventions_discard(draft_id):
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    return jsonify(local_interventions.discard_draft(d, draft_id))


@app.get("/api/student_notes")
def api_student_notes_list():
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa", "notes": []}), 404
    return jsonify({"ok": True, "notes": student_notes.list_notes(d)})


@app.post("/api/student_notes")
def api_student_notes_capture():
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    created = []
    try:
        seconds = _student_note_seconds(d, request.form.get("class_seconds"))
        text = str(request.form.get("text") or "").strip()
        if text:
            created.append(student_notes.capture_text(
                d, text, class_seconds=seconds))
        source_kind = str(request.form.get("source_kind") or "document")
        user_action = str(request.form.get("user_action") or "select")
        requested_mime = str(request.form.get("mime") or "")
        for uploaded in request.files.getlist("file"):
            created.append(student_notes.capture_file(
                d, uploaded.read(), original_name=uploaded.filename or "archivo",
                mime=requested_mime or uploaded.mimetype or "application/octet-stream",
                source_kind=source_kind, user_action=user_action,
                class_seconds=seconds,
            ))
        if not created:
            return jsonify({"ok": False, "msg": "nota vacía"}), 400
        return jsonify({
            "ok": True, "notes": created,
            "msg": f"{len(created)} aporte(s) del estudiante guardado(s)",
        })
    except (ValueError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400


@app.patch("/api/student_notes/<note_id>")
def api_student_notes_update(note_id):
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    data = request.get_json(silent=True) or {}
    try:
        note = None
        if data.get("discarded") is True:
            note = student_notes.discard_note(
                d, note_id, reason=str(data.get("reason") or "student_discarded"))
        elif "correction" in data:
            note = student_notes.correct_note(d, note_id, data.get("correction", ""))
        if data.get("discarded") is not True and "inclusion" in data:
            note = student_notes.set_inclusion(
                d, note_id, str(data.get("inclusion") or "pending"),
                target="lesson", reason=str(data.get("reason") or ""))
        if note is None:
            return jsonify({"ok": False, "msg": "sin cambios"}), 400
        return jsonify({"ok": True, "note": note, "msg": "nota actualizada"})
    except KeyError:
        return jsonify({"ok": False, "msg": "nota no encontrada"}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400


@app.post("/api/student_notes/<note_id>/transcribe")
def api_student_notes_transcribe(note_id):
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return jsonify({"ok": False, "msg": "sin clase activa"}), 404
    state = est.leer_estado(d)
    if state.get("estado") != "fin":
        return jsonify({
            "ok": False,
            "msg": "Finaliza la clase antes de transcribir la nota de voz; el STT docente tiene prioridad.",
        }), 409
    try:
        data = request.get_json(silent=True) or {}
        backend_name = str(
            data.get("stt_backend")
            or state.get("stt_backend_requested") or "remote").casefold()
        remote_consent = bool(data.get("remote_consent", False))
        note = student_notes.transcribe_audio_note(
            d, note_id, teacher_capture_active=False,
            backend_name=backend_name, remote_consent=remote_consent)
        return jsonify({"ok": True, "note": note, "msg": "nota de voz transcrita"})
    except KeyError:
        return jsonify({"ok": False, "msg": "nota no encontrada"}), 404
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 409


@app.get("/api/student_notes/<note_id>/original")
def api_student_notes_original(note_id):
    if disabled := _student_notes_disabled_response():
        return disabled
    d = _clase()
    if d is None:
        return ("", 404)
    try:
        note = next(
            item for item in student_notes.list_notes(d, include_discarded=True)
            if str(item.get("note_id")) == str(note_id))
        relative = Path(str(note.get("original", {}).get("path", "")))
        allowed = {
            Path(student_notes.ROOT_NAME).parts,
            student_notes.PACKAGED_ROOT.parts,
        }
        if not relative.parts or not any(
                relative.parts[:len(prefix)] == prefix for prefix in allowed):
            return ("", 404)
        return send_from_directory(d, relative.as_posix(), as_attachment=False)
    except (StopIteration, OSError):
        return ("", 404)


@app.post("/api/pantalla")
def api_pantalla():
    d = _clase()
    if d is None:
        return jsonify({"respuesta": "sin clase activa", "respuesta_html": "sin clase activa",
                        "captura": ""})
    q = (request.get_json(silent=True) or {}).get("q", "").strip()
    r, nombre = copiloto.pantalla_con_captura(d, q)
    return jsonify({"respuesta": r, "respuesta_html": a_html(r), "captura": nombre})


@app.get("/img/<path:nombre>")
def api_img(nombre):
    d = _clase()
    if d is None:
        return ("", 404)
    return send_from_directory(d / "img", nombre)


def _vigilar_fin_modo_hijo() -> None:
    while not _APP_STOPPING.wait(3):
        d = _clase()
        if d is not None:
            try:
                if est.leer_estado(d)["estado"] == "fin":
                    _request_runtime_shutdown("fin_modo_hijo", True)
                    return
            except Exception:
                pass


def _create_server(host: str = "127.0.0.1", port: int = 8756):
    from werkzeug.serving import make_server
    return make_server(host, int(port), app, threaded=True)


def main() -> int:
    global _SERVER
    config.stdout_utf8()
    config.ensure_dirs()
    _install_shutdown_handlers()
    if MODO_APP:
        threading.Thread(target=_vigilante, daemon=True).start()
    else:
        threading.Thread(target=_vigilar_fin_modo_hijo, daemon=True).start()
    print("[panel] http://localhost:8756" + (" (modo app)" if MODO_APP else ""))
    exit_code = 0
    try:
        _SERVER = _create_server("127.0.0.1", 8756)
        _SERVER.serve_forever()
    except KeyboardInterrupt:
        _request_runtime_shutdown("keyboard_interrupt", True)
    except BaseException as error:
        exit_code = 1
        _runtime_health_append(
            f"Servidor terminó con {type(error).__name__}; iniciando cierre coordinado.")
        _request_runtime_shutdown("server_exception", True)
    finally:
        ticket = _LAST_SHUTDOWN_TICKET
        if ticket is None:
            ticket = _request_runtime_shutdown("server_finally", True)
        ticket.completed.wait(timeout=120)
        server = _SERVER
        if server is not None:
            try:
                server.server_close()
            finally:
                _SERVER = None
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
