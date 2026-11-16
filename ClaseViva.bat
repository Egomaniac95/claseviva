@echo off
rem ClaseViva - inicio con consola para diagnosticar errores del .vbs
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Falta .venv. Crealo con Python 3.12, instala requirements.txt y despues ejecuta init.ps1. Ver README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "lanzador.py"
set "clasevivaExit=%errorlevel%"
if not "%clasevivaExit%"=="0" (
  echo.
  echo ClaseViva no pudo iniciar. Revisa el error anterior y README.md.
  pause
)
exit /b %clasevivaExit%
