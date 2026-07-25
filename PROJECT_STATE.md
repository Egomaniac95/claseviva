# Estado de entrega de ClaseViva

Actualizado: 2026-07-24

Versión Git: tag anotado `version-final`, un único commit raíz

Estado: **lista para auditoría de ingeniería y prueba local combinada**

## Contratos vigentes

- Un único propietario de audio; WAV PCM canónicos de 30 segundos y último chunk corto.
- Publicación `sealed` solo después de `fsync`, rename y SHA-256; manifest append-only,
  sequence estable, backpressure acotado y recuperación idempotente.
- STT exclusivamente remoto y secuencial: AssemblyAI → Deepgram → Speechmatics → Groq.
  Se filtra silencio, no existe hedging ni Whisper local y un commit válido nunca se cobra
  de nuevo. El agotamiento conserva WAV y estado pendiente.
- La transcripción no se muestra en vivo, pero alimenta resúmenes, memoria y lección.
- Resúmenes automáticos en 00:00, 05:00, 10:00… de captura útil. El resumen manual cubre
  lo pendiente sin mover la próxima frontera.
- Las notas del estudiante conservan original y procedencia y se publican separadas de la
  voz docente; pueden incluirse, excluirse o descartarse.
- La lección integra transcript, OCR, visuales y material oficial opt-in. Las fórmulas se
  renderizan offline como MathML y conservan LaTeX legible.
- Consolidar publica primero la lección y solo compacta a `leccion_final.md + img/` cuando
  audio, commits, notas y referencias son íntegros. Ante duda conserva la evidencia.
- Continuar una clase compactada parte de la lección y sus imágenes; Inicio puede mover
  curso o clase a la Papelera con vista previa, confirmación y fingerprint.

## Rendimiento

El runtime ya no carga Whisper/CUDA. Manifest STT, costos, transcript e imágenes se leen
incrementalmente; el transcriptor conserva un solo heartbeat y el panel evita renderizar
contenido sin cambios. Un benchmark sintético de 1.000 WAV redujo quince polls idle de
1.351,915 ms a 0,398 ms por ventana de 30 s; sirve como comparación del algoritmo, no como
umbral universal de hardware.

En la auditoría real del 2026-07-24, los procesos de ClaseViva sumaron aproximadamente
1,09 % de CPU y 0 % de GPU. El salto térmico 71 → 94 °C coincidió con el renderer y
`VideoDecode` de Chrome; la RTX se mantuvo cerca de 20 W y no apareció un proceso extraño.
La causa residual más probable es boost/política térmica del CPU bajo reproducción, no un
loop de ClaseViva. Antes de intervenir metal líquido faltan datos de potencia CPU, clocks,
RPM y throttling, además de un A/B Chrome solo frente a Chrome + ClaseViva.

## Validación reproducible

Ejecutado en Windows con Python 3.12.10, sin red ni evidencia real:

- Ruff: PASS, sin cache local.
- pytest: **638 passed** en 48,21 s.
- `tests/test_smoke.py`: `SMOKE_OK`.
- `init.ps1`: PASS; detectó runtime y claves sin mostrar valores.
- `pip check`: sin dependencias rotas.

Los gates usan `%TEMP%` y no dejan `.pytest_cache`, `.ruff_cache`, `.hypothesis`, bytecode
ni resultados dentro del repositorio. Las pruebas funcionales permanecen para la revisión
del ingeniero; se retiraron skills, gobernanza, runners A/B, diagnósticos de cursos y
artefactos históricos.

## Distribución y datos locales

`ClaseViva.vbs` es el lanzador normal. `.venv` permanece en esta máquina porque el VBS la
necesita, pero no forma parte de Git ni del archivo de entrega. `requirements.txt` permite
reconstruir el runtime y `requirements-dev.txt` instala únicamente QA.

No pertenecen a Git: `.env`, `contenido/**`, `logs/**`, `backups/**`, `.venv`, caches,
`clase_actual.txt` ni `memoria_cursos.md`. La memoria ligera local se conserva para el
usuario y el runtime la crea si falta. El MCP `codebase_memory`, su configuración de
proyecto, ejecutable e índice fueron retirados; ClaseViva no dependía de él.

## Validaciones manuales pendientes

- Sesión larga con el mismo video comparando Chrome solo y Chrome + ClaseViva.
- Hot-plug de monitor, cambio de micrófono, pausa/reanudación, Fin y cierre real.
- A/B de proveedores con WER/CER, costo, latencia, privacidad y retención acordada.
- Registrar CPU package power/PPT, clocks efectivos, RPM y flags de throttling antes de
  decidir mantenimiento interno del sistema de refrigeración.

Rollback del código: conservar el ZIP de entrega asociado al tag `version-final`. Los
datos reales no se alteraron durante esta preparación.
