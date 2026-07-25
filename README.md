# ClaseViva

ClaseViva captura audio y evidencia visual de clases, webinars y videos, crea pausas de
estudio periódicas y consolida una lección Markdown autocontenida.

## Inicio habitual

Haz doble clic en `ClaseViva.vbs`. Ese archivo usa
`.venv\Scripts\pythonw.exe`, ejecuta `lanzador.py` y abre el panel local en
`http://127.0.0.1:8756`.

`init.ps1` no inicia la aplicación ni instala paquetes: es un preflight de solo lectura.
`autorizar_red.ps1` es opcional y normalmente no se necesita porque el panel solo escucha
en localhost.

## Instalación o reparación

Requiere Windows y Python 3.12. La instalación se mantiene dentro de `.venv`:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
powershell -NoProfile -ExecutionPolicy Bypass -File .\init.ps1
```

Completa las claves necesarias en `.env`; nunca lo compartas ni lo incorpores a Git.
`requirements.txt` contiene el runtime. `requirements-dev.txt` solo añade pytest, Ruff y
herramientas de QA para quien vaya a revisar el código:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_dev_checks.ps1
```

## Flujo y datos

Antes de grabar selecciona fuente de audio y pantalla y autoriza el STT remoto. El
failover es secuencial: AssemblyAI Universal-3.5 Pro → Deepgram Nova-3 multilingüe →
Speechmatics Melia-1 multilingüe → Groq `whisper-large-v3`. Solo se construyen adaptadores
cuya clave exista en `.env`; no hay Whisper local ni solicitudes paralelas.

La transcripción es interna: alimenta resúmenes y la lección, pero no se renderiza en
vivo. Captura, WAV, hashes, manifest, commits y recuperación permanecen locales. El
silencio comprobado localmente no se envía. Un commit válido evita retranscribir o volver
a cobrar un chunk. Si se agota la cadena, se conserva el WAV pendiente.

```text
contenido/<curso>/
  CURSO.md
  TAREAS.md
  resumenes/
  clases/<clase>/
```

Al **Consolidar**, la lección se publica primero. La compactación solo elimina evidencia
si no hay audio o commits pendientes, las notas están decididas y cada referencia cumple
tamaño y SHA-256. Si un gate falla, la lección sigue disponible y la evidencia se conserva
con un motivo recuperable. El paquete final reanudable queda en:

```text
leccion_final.md
img/
```

**Continuar clase anterior** reconstruye el estado mínimo desde ese paquete. Desde Inicio
también se puede mover una clase o curso a la Papelera mediante vista previa, nombre exacto
y revalidación del fingerprint.

## Notas del estudiante

Texto, documentos, imágenes, capturas, fotografías y voz se guardan por acción explícita.
Se pueden excluir o descartar y la lección las distingue de lo dicho por el profesor. La
voz se transcribe después de **Fin** y requiere confirmación antes de subirla al STT remoto.

## Módulos clave

- `lanzador.py`: inicia el panel y supervisa exclusivamente procesos propios.
- `src/panel.py`: UI/API; coordina Inicio, Pausa, Fin y Consolidar.
- `src/captura_audio.py`: único propietario del stream; sella WAV canónicos.
- `src/audio_manifest.py`: orden, hashes, estado append-only y recuperación.
- `src/transcriptor_vivo.py` y `src/stt_backend.py`: cola incremental y failover remoto.
- `src/captura_pantalla.py`: evidencia visual y selección de monitor.
- `src/escriba.py`: pausa de estudio incremental cada cinco minutos útiles.
- `src/student_notes.py`: originales, procedencia, edición e inclusión de notas.
- `src/post_procesar.py`: integra transcript, visuales y notas en la lección.
- `src/study_package.py`: valida y compacta transaccionalmente.
- `src/content_management.py`: confinamiento y traslado reversible a la Papelera.

## Auditoría de la versión

`PROJECT_STATE.md` resume contratos, validaciones y riesgos abiertos. Las pruebas usan
directorios temporales fuera del repositorio. `.env`, `contenido/**`, `logs/**`,
`backups/**` y la evidencia real no pertenecen al historial Git.
