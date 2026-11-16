"""Iconos locales de Windows: descubrimiento automático y caché fuera del polling.

Solo registra ejecutables de procesos ya descubiertos. El navegador recibe una
clave opaca, nunca rutas ni un endpoint que permita elegir archivos arbitrarios.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
import ctypes
import hashlib
from pathlib import Path
import re
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from . import config

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="app-icons")
_lock = threading.Lock()
_jobs: OrderedDict[tuple[Path, str], tuple[Future, float]] = OrderedDict()
_LIMIT = 256


def cache_root() -> Path:
    return config.ROOT / ".cache" / "iconos_apps"


def icon_url(process) -> str:
    """Encola una vez por ejecutable/versión; nunca espera a Windows Shell."""
    if sys.platform != "win32":
        return ""
    try:
        raw = process.exe()
        if not raw or raw.startswith(("\\\\", "//")):
            return ""
        executable = Path(raw)
        if not executable.is_absolute():
            return ""
        executable = executable.resolve()
        stat = executable.stat()
        signature = f"icon-v3-light:{str(executable).casefold()}:{stat.st_size}:{stat.st_mtime_ns}"
        key = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        root = cache_root()
        destination = root / f"{key}.png"
        if not destination.is_file():
            identity = (root, key)
            with _lock:
                previous = _jobs.get(identity)
                # Un fallo transitorio se reintenta al actualizar después de un minuto.
                if previous is None or (previous[0].done() and time.monotonic() - previous[1] > 60):
                    if len(_jobs) >= _LIMIT:
                        completed = next((k for k, (future, _) in _jobs.items() if future.done()), None)
                        if completed is None:
                            return ""
                        _jobs.pop(completed)
                    _jobs[identity] = (_pool.submit(_save_icon, executable, destination), time.monotonic())
        return f"/api/iconos-app/{key}.png"
    except (OSError, AttributeError):
        return ""
    except Exception:
        # psutil.AccessDenied/NoSuchProcess: usar el icono genérico sin romper fuentes.
        return ""


def icon_file(key: str, timeout: float = 1.5) -> Path | None:
    """Solo sirve PNG de esta caché. La espera ocurre en la petición de imagen."""
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        return None
    root = cache_root()
    destination = root / f"{key}.png"
    if destination.is_file():
        return destination
    with _lock:
        job = _jobs.get((root, key))
    if job is None:
        return None
    try:
        return job[0].result(timeout=timeout)
    except TimeoutError:
        return None
    except Exception:
        return None


def _save_icon(executable: Path, destination: Path) -> Path | None:
    temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        icon = _packaged_icon(executable)
        if icon is None:
            icon = _windows_icon(executable)
        if icon is None:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            icon.save(temporary, format="PNG")
            temporary.replace(destination)
        finally:
            icon.close()
        return destination
    except Exception:
        return None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _packaged_icon(executable: Path):
    """Apps de Microsoft Store: el logo real puede estar en AppxManifest, no en el EXE."""
    from PIL import Image

    for root in list(executable.parents)[:6]:
        manifest = root / "AppxManifest.xml"
        if not manifest.is_file():
            continue
        try:
            if manifest.stat().st_size > 2_000_000:
                return None
            for application in ET.parse(manifest).getroot().iter():
                if application.tag.rsplit("}", 1)[-1] != "Application":
                    continue
                target = application.attrib.get("Executable", "")
                if not target or (root / target).resolve() != executable:
                    continue
                for visual in application:
                    asset = visual.attrib.get("Square44x44Logo")
                    if not asset:
                        continue
                    base = (root / asset).resolve()
                    if not base.is_relative_to(root.resolve()):
                        continue
                    candidates = [
                        base.with_name(base.stem + ".targetsize-32_altform-lightunplated" + base.suffix),
                        base.with_name(base.stem + ".targetsize-32_altform-unplated" + base.suffix),
                        base.with_name(base.stem + ".targetsize-32" + base.suffix),
                        base, *sorted(base.parent.glob(base.stem + ".scale-*.png")),
                    ]
                    for path in candidates:
                        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                            continue
                        with Image.open(path) as image:
                            result = image.convert("RGBA")
                        result.thumbnail((32, 32), Image.Resampling.LANCZOS)
                        canvas = Image.new("RGBA", (32, 32))
                        canvas.alpha_composite(result, ((32-result.width)//2, (32-result.height)//2))
                        result.close()
                        return canvas
        except (OSError, ET.ParseError, ValueError):
            return None
        return None
    return None


def _windows_icon(executable: Path):
    """Windows Shell entrega el HICON del ejecutable; liberar todos los HANDLE."""
    from ctypes import wintypes
    from PIL import Image, ImageChops

    class FileInfo(ctypes.Structure):
        _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                    ("attributes", wintypes.DWORD), ("displayName", wintypes.WCHAR * 260),
                    ("typeName", wintypes.WCHAR * 80)]

    class BitmapInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                    ("height", wintypes.LONG), ("planes", wintypes.WORD),
                    ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                    ("imageSize", wintypes.DWORD), ("xppm", wintypes.LONG),
                    ("yppm", wintypes.LONG), ("used", wintypes.DWORD),
                    ("important", wintypes.DWORD), ("colors", wintypes.DWORD * 3)]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    ole = ctypes.WinDLL("ole32")
    shell.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.POINTER(FileInfo), wintypes.UINT, wintypes.UINT]
    shell.SHGetFileInfoW.restype = ctypes.c_size_t
    shell.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                   ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT]
    shell.ExtractIconExW.restype = wintypes.UINT
    user.DestroyIcon.argtypes = [wintypes.HICON]
    user.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    user.DrawIconEx.restype = wintypes.BOOL
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BitmapInfo), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi.CreateDIBSection.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2) >= 0
    info = FileInfo()
    size = 32

    def render(background: int):
        dc = gdi.CreateCompatibleDC(None)
        bitmap, old = None, None
        try:
            if not dc:
                raise OSError("CreateCompatibleDC")
            header = BitmapInfo()
            header.size, header.width, header.height = 40, size, -size
            header.planes, header.bits = 1, 32
            pixels = ctypes.c_void_p()
            bitmap = gdi.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(pixels), None, 0)
            if not bitmap or not pixels.value:
                raise OSError("CreateDIBSection")
            old = gdi.SelectObject(dc, bitmap)
            ctypes.memset(pixels, background, size * size * 4)
            if not user.DrawIconEx(dc, 0, 0, info.hIcon, size, size, 0, None, 3):
                raise OSError("DrawIconEx")
            gdi.GdiFlush()
            return Image.frombytes("RGBA", (size, size), ctypes.string_at(pixels, size * size * 4), "raw", "BGRA")
        finally:
            if old:
                gdi.SelectObject(dc, old)
            if bitmap:
                gdi.DeleteObject(bitmap)
            if dc:
                gdi.DeleteDC(dc)

    try:
        handle = wintypes.HICON()
        shell.ExtractIconExW(str(executable), 0, ctypes.byref(handle), None, 1)
        info.hIcon = handle.value
        if not info.hIcon:
            shell.SHGetFileInfoW(str(executable), 0, ctypes.byref(info), ctypes.sizeof(info), 0x100)
            if not info.hIcon:
                return None
        image = render(0)
        if image.getchannel("A").getextrema()[1] == 0:
            # Iconos antiguos sin alfa: recuperar la máscara con dos fondos.
            white = render(255)
            mask = ImageChops.difference(white.convert("RGB"), image.convert("RGB")).convert("L")
            image.putalpha(mask.point(lambda value: 255 - value))
            white.close()
        # DrawIconEx entrega RGB premultiplicado; PNG espera RGBA normal.
        result = Image.frombytes("RGBa", image.size, image.tobytes()).convert("RGBA")
        image.close()
        return result
    finally:
        if info.hIcon:
            user.DestroyIcon(info.hIcon)
        if initialized:
            ole.CoUninitialize()
