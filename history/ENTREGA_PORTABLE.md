# Entrega portable actual — 2026.10.27

Archivo actual: [ClaseViva-Portable-20261027.zip](../dist/ClaseViva-Portable-20261027.zip).
155.484.775 bytes, 485 archivos, SHA-256
`f809fabfa07cfd52d28a46a49ecf05705f1d9b01b11c7b27febb2fc05a562604`.
El diagnóstico offline pasó y se verificaron por SHA-256 los archivos del ZIP contra
el bundle. Incluye el cambio Web: [auditoría](../AUDITORIA_WEB_20261027.md).

## Registro histórico — 2026.10.16

Archivo para compartir: [ClaseViva-Portable.zip](../dist/ClaseViva-Portable.zip).

- Tamaño: 154.417.578 bytes; 441 archivos y 356.509.699 bytes descomprimidos.
- SHA-256: `862e5a8a8938d77c14f19d521154740d3e73aab2e47a7e31b466c1c16a15e269`.
- Inventario: [portable-20261016-manifest.json](portable-20261016-manifest.json).
- Validación: [portable-20261016-delivery.json](portable-20261016-delivery.json).

Reconstruido con Python 3.12 x64 y dependencias fijadas. Incluye Silero VAD 6.2.3,
licencia MIT, ONNX en CPU y las correcciones de captura, cobertura, recuperación y
render. La cadena STT secuencial se conserva. Tras extraer el ZIP se comprobaron los
441 tamaños y hashes y se repitió el diagnóstico offline: inferencia real de Silero,
render con imágenes de nombres que contienen guion bajo, PDF/OCR e inicio y salida
de los cuatro workers sin clase activa. Todo pasó, sin llamadas STT.

La suite fuente pasó con 820 pruebas; los últimos ajustes de render y diagnóstico
pasaron además sus 31 pruebas focales. Incluye plantilla vacía de claves, instrucciones
y avisos de terceros. No contiene claves reales ni cursos. Se retiraron los temporales
de esta compilación y extracción: [recibo](build-temp-cleanup-20261016.json).

Uso: extraer la carpeta completa, abrir `Configurar claves.cmd`, guardar las claves
y abrir `ClaseViva.exe`. En el proyecto fuente puede seguir usándose `ClaseViva.vbs`,
cuyo entorno Python fue reparado. [Informe actual](../ENTREGA_20261016.md).
Los artefactos antiguos externos al workspace no se modificaron en esta tarea.
La validación no incluye una grabación de varias horas en otro equipo. El ejecutable
sigue sin firma digital; su destino es Windows x64.

## Registro histórico — 2026.09.28

Lo siguiente describe la entrega anterior; no es la ruta actual para compartir.

Archivo para compartir: `../../ClaseViva-Portable.zip`.

- Tamaño: 152.422.489 bytes (145,36 MiB).
- SHA-256: `7f1bd16f7ba0ab34fbbb68b2877a263c8b31c4fe4230a8d0a257dd0896dbc9c4`.
- Descomprimido: 438 archivos, 354.104.752 bytes.
- Inventario por archivo: `portable-20260928-manifest.json`.
- Construcción desde entorno Windows/Python 3.12 x64 independiente con lock de
  dependencias; pip check y gate offline aprobados.
- ZIP extraído en carpeta temporal; los 438 tamaños/hashes coinciden exactamente
  con el bundle y se repitió allí el gate offline: aprobado.
- El código congelado `src.audio_manifest.continuity_report` incluye el nuevo
  reconocimiento corroborado de pausa/arranque; comprobado dentro del EXE.
- Runtime: importaciones nativas, PDF/OCR, imágenes, Markdown/MathML y cierre de los
  cuatro workers sin clase activa. Suite fuente: 770 passed, Ruff y smoke.
- Incluye exportación HTML de lección con imágenes incrustadas y corrección del
  copiloto (respuesta completa ampliable y aviso al alcanzar el límite de salida).
- Plantilla de claves vacía, instrucciones de uso y avisos de terceros incluidos.
  No contiene `.env` real, cursos, logs, Git, venv ni tests. Sin llamadas STT/DeepSeek.

El borrado de las entregas externas fue bloqueado por la herramienta antes de ejecutar
el comando. Siguen en disco: `ClaseViva-Windows-20260914`,
`ClaseViva-Windows-20260914-rc1`, `ClaseViva-Windows-20260914-rc1.zip`,
`ClaseViva-source-audit-20260914.zip` y `ClaseViva-cleanup-audit-20260924`.
Estas cinco rutas se inventariaron sin cursos/configuraciones privadas activos; son
las candidatas autorizadas a retirar, no la carpeta fuente `clase-viva` ni el ZIP nuevo.
También quedan temporales de build/prueba en `%TEMP%/ClaseViva`; no son entregas.
Checkpoints preservados en el archivo de historial con hashes verificados.
No se borró Git ni sus commits,
ni se modificaron cursos, claves, contabilidad o entorno de la instalación fuente.

Uso: extraer la carpeta completa, abrir `Configurar claves.cmd`, guardar las claves y
abrir `ClaseViva.exe`. Requiere Internet y cuota/saldo de los proveedores configurados.

Pendiente: prueba de captura real en otro Windows limpio/controladores distintos y
firma digital para distribución pública. El ejecutable no está firmado; no desactivar
protecciones de Windows si lo bloquean. No garantiza cuotas gratuitas ni reduce por
sí mismo el consumo térmico. Windows x64; no Linux, ARM ni 32 bits.
