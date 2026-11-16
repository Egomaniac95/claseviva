"""Caché automática por equipo/ejecutable, apps Store y endpoint confinado."""
from collections import OrderedDict
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from src import app_icons, panel


@pytest.fixture
def icons(monkeypatch, tmp_path):
    monkeypatch.setattr(app_icons.config, "ROOT", tmp_path)
    monkeypatch.setattr(app_icons, "_jobs", OrderedDict())
    monkeypatch.setattr(app_icons, "_packaged_icon", lambda _: None)
    executable = tmp_path / "programa-nuevo.exe"
    executable.write_bytes(b"synthetic executable")
    calls = []

    def extract(path):
        calls.append(path)
        return Image.new("RGBA", (32, 32), (20, 170, 100, 255))

    monkeypatch.setattr(app_icons, "_windows_icon", extract)
    return SimpleNamespace(executable=executable, process=SimpleNamespace(exe=lambda: str(executable)),
                           calls=calls, extract=extract)


def key_of(url):
    return url.rsplit("/", 1)[-1].removesuffix(".png")


@pytest.mark.skipif(sys.platform != "win32", reason="Runtime Windows")
def test_new_program_is_discovered_once_and_cache_survives_restart(icons, monkeypatch):
    url = app_icons.icon_url(icons.process)
    assert url.startswith("/api/iconos-app/") and "programa-nuevo" not in url
    path = app_icons.icon_file(key_of(url), timeout=5)
    assert path and path.is_file() and icons.calls == [icons.executable]
    for _ in range(10):
        assert app_icons.icon_url(icons.process) == url
    assert len(icons.calls) == 1
    # Un nuevo proceso o reinicio reutiliza el PNG, sin un registro manual.
    app_icons._jobs.clear()
    monkeypatch.setattr(app_icons, "_windows_icon", lambda _: pytest.fail("extracción repetida"))
    assert app_icons.icon_url(icons.process) == url
    assert app_icons.icon_file(key_of(url)) == path


@pytest.mark.skipif(sys.platform != "win32", reason="Runtime Windows")
def test_icon_extraction_does_not_block_inventory_and_requests_deduplicate(icons, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def extract(path):
        entered.set()
        assert release.wait(5)
        return icons.extract(path)

    monkeypatch.setattr(app_icons, "_windows_icon", extract)
    try:
        url = app_icons.icon_url(icons.process)
        assert entered.wait(3) and not release.is_set()
        for _ in range(10):
            assert app_icons.icon_url(icons.process) == url
        assert app_icons.icon_file(key_of(url), timeout=0) is None
    finally:
        release.set()
    assert app_icons.icon_file(key_of(url), timeout=5)
    assert icons.calls == [icons.executable]


@pytest.mark.skipif(sys.platform != "win32", reason="Runtime Windows")
def test_updated_program_and_another_computer_build_their_own_cache(icons, monkeypatch, tmp_path):
    first = app_icons.icon_url(icons.process)
    assert app_icons.icon_file(key_of(first), timeout=5)
    icons.executable.write_bytes(b"a different version of the executable")
    updated = app_icons.icon_url(icons.process)
    assert updated != first
    assert app_icons.icon_file(key_of(updated), timeout=5)
    another_computer = tmp_path / "otro-equipo"
    monkeypatch.setattr(app_icons.config, "ROOT", another_computer)
    assert app_icons.icon_url(icons.process) == updated
    new_file = app_icons.icon_file(key_of(updated), timeout=5)
    assert new_file and new_file.is_relative_to(another_computer)
    assert len(icons.calls) == 3


@pytest.mark.skipif(sys.platform != "win32", reason="Runtime Windows")
def test_missing_icon_does_not_create_empty_files_or_repeat_failures(icons, monkeypatch):
    def fail(path):
        icons.calls.append(path)
        raise OSError("synthetic unavailable icon")

    monkeypatch.setattr(app_icons, "_windows_icon", fail)
    url = app_icons.icon_url(icons.process)
    assert app_icons.icon_file(key_of(url), timeout=5) is None
    for _ in range(5):
        assert app_icons.icon_url(icons.process) == url
    assert len(icons.calls) == 1
    assert not list(app_icons.cache_root().glob("*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Runtime Windows")
def test_icon_endpoint_serves_only_png_cache_and_supports_browser_cache(icons):
    url = app_icons.icon_url(icons.process)
    assert app_icons.icon_file(key_of(url), timeout=5)
    client = panel.app.test_client()
    response = client.get(url)
    assert response.status_code == 200 and response.mimetype == "image/png"
    assert response.data.startswith(b"\x89PNG")
    assert "immutable" in response.headers["Cache-Control"]
    assert client.get(url, headers={"If-None-Match": response.headers["ETag"]}).status_code == 304
    assert client.get("/api/iconos-app/not-a-key.png").status_code == 404
    assert client.get("/api/iconos-app/" + "0"*64 + ".png").status_code == 404
    assert app_icons.icon_file("../../.env") is None


def test_store_manifest_uses_application_logo_and_qualified_asset(tmp_path):
    executable = tmp_path / "StoreApp.exe"
    executable.touch()
    assets = tmp_path / "Assets"
    assets.mkdir()
    with Image.new("RGBA", (32, 32), (0, 180, 100, 255)) as image:
        image.save(assets / "Logo.targetsize-32_altform-unplated.png")
    with Image.new("RGBA", (32, 32), (10, 20, 30, 255)) as image:
        image.save(assets / "Logo.targetsize-32_altform-lightunplated.png")
    (tmp_path / "AppxManifest.xml").write_text(
        '<Package xmlns="urn:package" xmlns:uap="urn:uap"><Applications>'
        '<Application Executable="OtraApp.exe"><uap:VisualElements Square44x44Logo="Assets/Other.png"/></Application>'
        '<Application Executable="StoreApp.exe"><uap:VisualElements Square44x44Logo="Assets/Logo.png"/></Application>'
        '</Applications></Package>', encoding="utf-8")
    icon = app_icons._packaged_icon(executable.resolve())
    assert icon and icon.size == (32, 32) and icon.getpixel((16, 16)) == (10, 20, 30, 255)
    icon.close()


def test_manifest_cannot_read_assets_outside_package(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    executable = package / "App.exe"
    executable.touch()
    (package / "AppxManifest.xml").write_text(
        '<Package><Application Executable="App.exe"><VisualElements Square44x44Logo="../outside.png"/></Application></Package>',
        encoding="utf-8")
    assert app_icons._packaged_icon(executable.resolve()) is None


def test_inaccessible_and_network_processes_use_generic_icon():
    assert app_icons.icon_url(SimpleNamespace(exe=lambda: "\\\\server\\app.exe")) == ""
    assert app_icons.icon_url(SimpleNamespace()) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows icons")
def test_native_windows_extraction_has_visible_pixels():
    icon = app_icons._windows_icon(Path(sys.executable))
    assert icon and icon.size == (32, 32) and icon.getchannel("A").getbbox()
    icon.close()
