@echo off
rem Solo configura la copia local. Nunca sobrescribir un .env existente.
if not exist "%~dp0.env" copy /-Y "%~dp0.env.example" "%~dp0.env" >nul
if not exist "%~dp0.env" (
  echo No se pudo crear .env. Descomprime la carpeta completa en Documentos.
  pause
  exit /b 1
)
start "" notepad.exe "%~dp0.env"
