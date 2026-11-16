import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from src import app_paths, config, desktop_app, panel
from src.runtime_shutdown import ProcessSnapshot, _command_matches


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "portable á" / "ClaseViva.exe"))
    resources = tmp_path / "portable á" / "_internal"
    resources.mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)
    monkeypatch.delenv("CLASEVIVA_DATA_DIR", raising=False)
    return resources


def test_frozen_paths_do_not_write_internal(frozen):
    assert app_paths.resource_root() == frozen
    assert app_paths.data_root() == frozen.parent
    assert app_paths.worker_command("src.escriba") == [sys.executable, "--worker", "src.escriba"]
    snapshot = ProcessSnapshot(77, 100, tuple(app_paths.worker_command("src.escriba")))
    assert _command_matches(snapshot, "src.escriba")
    assert not _command_matches(snapshot, "src.captura_audio")


def test_source_layout_and_worker_remain_compatible(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("CLASEVIVA_DATA_DIR", raising=False)
    assert app_paths.data_root() == Path(__file__).resolve().parents[1]
    assert app_paths.worker_command("src.escriba")[1:] == ["-m", "src.escriba"]


def test_data_override_absolute_only(monkeypatch, tmp_path):
    monkeypatch.setenv("CLASEVIVA_DATA_DIR", str(tmp_path))
    assert app_paths.data_root() == tmp_path
    monkeypatch.setenv("CLASEVIVA_DATA_DIR", "relative")
    with pytest.raises(ValueError):
        app_paths.data_root()


def test_template_created_once_never_overwrites_secret(frozen):
    (frozen / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    root = app_paths.prepare_portable_config()
    assert (root / ".env").read_text() == "DEEPSEEK_API_KEY=\n"
    (root / ".env").write_text("DEEPSEEK_API_KEY=synthetic-test\n")
    app_paths.prepare_portable_config()
    assert "synthetic-test" in (root / ".env").read_text()
    assert not (frozen / ".env").exists()


def test_missing_bundle_template_does_not_leave_empty_env(frozen):
    with pytest.raises(FileNotFoundError):
        app_paths.prepare_portable_config()
    assert not (frozen.parent / ".env").exists()


def test_env_bom_quotes_and_process_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("SYNTHETIC_NEW_KEY", raising=False)
    monkeypatch.setenv("SYNTHETIC_PRESERVED", "process-value")
    (tmp_path / ".env").write_text(
        '\ufeffSYNTHETIC_NEW_KEY="test-value#not-comment"\n'
        'SYNTHETIC_PRESERVED=file-value\n', encoding="utf-8")
    config._cargar_env()
    import os
    assert os.environ["SYNTHETIC_NEW_KEY"] == "test-value#not-comment"
    assert os.environ["SYNTHETIC_PRESERVED"] == "process-value"


def test_worker_dispatch_no_browser_no_capture_imports(monkeypatch):
    imports = []
    monkeypatch.setattr(desktop_app.importlib, "import_module", lambda name: (
        imports.append(name) or SimpleNamespace(main=lambda: 7)))
    monkeypatch.setattr(sys, "argv", ["ClaseViva.exe", "--worker", "src.escriba"])
    monkeypatch.setattr(desktop_app, "open_browser", lambda: pytest.fail("Worker abrió navegador"))
    assert desktop_app.run_worker("src.escriba") == 7
    assert imports == ["src.escriba"]
    assert sys.argv == ["src.escriba"]
    with pytest.raises(ValueError):
        desktop_app.run_worker("os")
    assert imports == ["src.escriba"]


def test_healthz_is_observational_and_assets_are_allowlisted(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(panel, "_clase", lambda: pytest.fail("healthz consultó curso"))
    client = panel.app.test_client()
    data = client.get("/api/healthz").get_json()
    assert data == {"app": "ClaseViva", "version": 1, "consolidation_recovery": 1,
                    "data_root_id": app_paths.data_root_id(tmp_path)}
    assert list(tmp_path.iterdir()) == []
    assert client.get("/assets/.env").status_code == 404
    assert client.get("/assets/../.env").status_code == 404
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/assets/claseviva.png").status_code == 200


def test_wrong_application_or_data_root_not_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("CLASEVIVA_DATA_DIR", str(tmp_path))
    assert not desktop_app.same_panel(None)
    assert not desktop_app.same_panel({"version": 1, "data_root_id": "another"})
    assert not desktop_app.same_panel({"app": "Other", "version": 1,
                                       "data_root_id": app_paths.data_root_id()})
    assert desktop_app.same_panel({"app": "ClaseViva", "version": 1,
                                   "data_root_id": app_paths.data_root_id()})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_double_launch_guard_released_without_polling(monkeypatch):
    monkeypatch.setattr(desktop_app, "PORT", 65431)
    with desktop_app.LaunchGuard() as first:
        assert first.acquired
        with desktop_app.LaunchGuard() as second:
            assert not second.acquired
    with desktop_app.LaunchGuard() as after:
        assert after.acquired


def test_self_test_exit_and_ok_agree(monkeypatch, tmp_path):
    monkeypatch.setattr(app_paths, "prepare_portable_config", lambda: tmp_path)
    monkeypatch.setattr(desktop_app, "self_test", lambda: {"ok": False})
    report = tmp_path / "diagnostic.json"
    assert desktop_app.cli(["--self-test", "--report", str(report)]) == 1
    assert json.loads(report.read_text())["ok"] is False
    monkeypatch.setattr(desktop_app, "self_test", lambda: {"ok": True})
    assert desktop_app.cli(["--self-test", "--report", str(report)]) == 0
    assert json.loads(report.read_text())["ok"] is True


def test_windows_ocr_reserves_onnx_before_winrt_without_loading_models(monkeypatch):
    import builtins
    from src import ocr

    original_import = builtins.__import__
    calls = []

    def import_probe(name, *args, **kwargs):
        if name == "onnxruntime":
            calls.append(name)
            return SimpleNamespace()
        if name.startswith("winrt."):
            calls.append(name)
            raise RuntimeError("synthetic-winrt-boundary")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(ocr.sys, "platform", "win32")
    monkeypatch.setattr(builtins, "__import__", import_probe)
    with pytest.raises(RuntimeError, match="synthetic-winrt-boundary"):
        ocr._WindowsOcrBackend()
    assert calls == ["onnxruntime", "winrt.windows.globalization"]


def test_portable_render_gate_rejects_plain_text_fallback(monkeypatch):
    assert desktop_app.lesson_render_check()["ok"] is True
    monkeypatch.setattr(panel, "a_html", lambda text: "<pre>" + text + "</pre>")
    result = desktop_app.lesson_render_check()
    assert result["ok"] is False
    assert result["heading"] is False
    assert result["metadata_hidden"] is False


def test_distribution_notices_filter_does_not_copy_source_or_parent_paths():
    from scripts.collect_windows_notices import notice_file
    assert notice_file('onnxruntime/LICENSE')
    assert notice_file('package.dist-info/licenses/vendor/COPYING')
    assert notice_file('cv2/LICENSE-3RD-PARTY.txt')
    assert not notice_file('../LICENSE')
    assert not notice_file('package/runtime.py')
    assert not notice_file('.env')
    assert not notice_file('packaging/licenses/__pycache__/_spdx.cpython-312.pyc')
    assert not notice_file('packaging/licenses/_spdx.py')
