@echo off
rem ClaseViva - alternativa al .vbs (puede parpadear una ventana un instante)
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Falta .venv. Crealo con Python 3.12, instala requirements.txt y despues ejecuta init.ps1. Ver README.md.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "lanzador.py"
