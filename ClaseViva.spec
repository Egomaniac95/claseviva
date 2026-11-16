# Windows x64, modo directorio: no se extrae un runtime por cada worker.
from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata,
)

root = Path(SPECPATH)
datas = [(str(root / ".env.example"), ".")]
datas += [(str(root / "assets" / name), "assets")
          for name in ("claseviva.png", "claseviva.ico", "source_picker.js")]
datas += [(str(root / "assets" / "silero_vad" / name), "assets/silero_vad")
          for name in ("silero_vad.onnx", "LICENSE", "README.md")]
hiddenimports = collect_submodules("src") + collect_submodules("winrt")
binaries = collect_dynamic_libs("winrt")
for package in ("rapidocr_onnxruntime", "fake_useragent", "latex2mathml", "pypdfium2"):
    datas += collect_data_files(package)
for package in ("ddgs", "openai", "pypdfium2", "rapidocr-onnxruntime"):
    datas += copy_metadata(package)

a = Analysis(
    [str(root / "lanzador.py")], pathex=[str(root)],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    excludes=["pytest", "hypothesis", "ruff", "torch", "transformers",
              "faster_whisper", "whisper", "ctranslate2"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="ClaseViva",
    debug=False, strip=False, upx=False, console=False,
    icon=str(root / "assets" / "claseviva.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ClaseViva")
