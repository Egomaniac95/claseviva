# WebP 85 y limpieza del proyecto — 12 de noviembre de 2026

Se aplicó WebP 85 a las clases existentes y al guardado de nuevas capturas. Se revisó la ocupación de todo el proyecto y se retiraron dependencias y artefactos que ya no necesita la aplicación.

## Ocupación del proyecto

| Componente | Antes | Después | Reducción |
|---|---:|---:|---:|
| Entorno Python | 569.15 MB | 414.84 MB | 154.32 MB |
| Portable antiguo y ZIP | 514.52 MB | 0.00 MB | 514.52 MB |
| Clases de contenido | 155.21 MB | 93.53 MB | 61.67 MB |
| **Proyecto completo** | **1268.90 MB** | **533.79 MB** | **735.11 MB** |

La fila del proyecto incluye los componentes anteriores; no se suman entre sí. Unidades decimales: 1 MB = 1.000.000 bytes. Las cifras incluyen el código, `.git`, clases, entorno, logs y recibos de auditoría; las pequeñas escrituras de este informe pueden variar unos KB.

## Imágenes de ambas ubicaciones

| Ubicación | Imágenes | Antes | WebP 85 | Reducción |
|---|---:|---:|---:|---:|
| ClaseViva / contenido | 1360 | 149.62 MB | 87.94 MB | 61.68 MB |
| Infraestructura | 1343 | 239.29 MB | 147.01 MB | 92.27 MB |

Las 2.703 imágenes bajaron un **39.59 %** frente a los JPEG ya optimizados: **153.95 MB** adicionales. WebP 85 es compresión con pérdida autorizada; se conserva la resolución y se verifican orientación, perfil de color y transparencia. No se afirma igualdad de los píxeles RGB.

Cada cambio de extensión se refleja en los Markdown, HTML e inventarios. Los 12 cuerpos de lección coinciden con los originales tras normalizar únicamente las rutas de imagen. Las 13 carpetas conservan el mismo número de imágenes y todas las referencias resuelven. Los JPEG anteriores se retiraron después de comprobar el paquete; el proceso restaura una clase si falla antes de validarla. No quedaron copias de respaldo duplicando la biblioteca.

## Nuevas clases

Captura continua, capturas del copiloto, selector de región, páginas oficiales y notas visuales publicadas usan WebP 85. La captura en vivo usa el encoder rápido; la conversión de biblioteca usa mayor esfuerzo. Los materiales de texto/diagramas pueden usar WebP sin pérdida cuando produce un archivo menor. Las imágenes WebP existentes no se recomprimen para evitar pérdidas acumuladas.

El HTML guardado sigue usando rutas relativas a `img/`. Debe copiarse junto a esa carpeta. La descarga autocontenida del panel continúa funcionando y se genera en memoria. Se fijó explícitamente el MIME WebP porque el registro de Windows no lo identificaba.

## Paquetes retirados

Se retiraron estos 12 paquetes que estaban presentes al empezar, incluidos los de desarrollo que no necesita la ejecución diaria. Los archivos de requisitos de desarrollo se conservan para reinstalarlos cuando haga falta.

| Paquete | Versión retirada | Tamaño de archivos registrado |
|---|---|---:|
| cffi | 2.1.1 | 1.10 MB |
| greenlet | 3.5.6 | 1.13 MB |
| iniconfig | 2.3.1 | 0.04 MB |
| mozjpeg-lossless-optimization | 1.3.2 | 0.27 MB |
| playwright | 1.64.1 | 113.16 MB |
| pluggy | 1.7.0 | 0.17 MB |
| pycparser | 3.12 | 0.55 MB |
| pyee | 14.0.0 | 0.09 MB |
| Pygments | 2.21.0 | 8.65 MB |
| pytest | 9.2.0 | 3.39 MB |
| pytest-timeout | 2.4.0 | 0.07 MB |
| ruff | 0.17.4 | 27.44 MB |

Quedan **62 dependencias de ejecución y pip**. Se comprobó la dependencia transitiva completa, incluyendo extras solicitados por DDGS, y coincide exactamente con `requirements-windows.lock`. `pip check` terminó correctamente. Se conservan OpenCV, ONNX, NumPy, RapidOCR y AV porque participan en OCR, identificación de diapositivas, VAD y decodificación de notas de voz. DDGS requiere HTTPX con Brotli, HTTP/2 y SOCKS; estos componentes también se conservan.

Hypothesis y sortedcontainers se instalaron temporalmente para ejecutar toda la suite y se retiraron al terminar. Las herramientas de desarrollo pueden reinstalarse mediante:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## Artefactos retirados

- Portable `dist/ClaseViva-build-20261027/` y ZIP de la misma versión: 514,52 MB. El inicio habitual por VBS/BAT usa el código fuente y `.venv`; se validó ese entorno después de la limpieza.
- Backend de jpegoptim sustituido por WebP y cachés de pruebas/lint/bytecode del proyecto.
- Variantes experimentales de JXL, AVIF y WebP que ya no se usan. Solo se conserva una muestra original y su WebP 85 para comparación.
- El script de build ahora elimina su entorno temporal después de una compilación verificada; `-KeepWork` permite conservarlo para diagnóstico. No se generó otro portable durante esta limpieza.

## Validación

- 885 pruebas aprobadas: 884 en la suite y una prueba de tiempos de audio repetida aisladamente después de un timeout bajo carga. La repetición conservó el límite original de la prueba y pasó.
- Smoke de importaciones y lógica: `SMOKE_OK`.
- Diagnóstico offline de la aplicación sin herramientas de desarrollo instaladas: correcto, incluidos OCR real, Silero VAD, PDF y WebP.
- Los 2.703 archivos WebP se abrieron y se contrastaron dimensiones y SHA-256 con el recibo de conversión.
- Los 12 cuerpos de lección conservan la explicación; los 13 HTML tienen todas sus imágenes locales disponibles.
- Microsoft Edge abrió 39 imágenes representativas de las 13 carpetas con sus dimensiones originales.
- Revisión de sintaxis/importaciones correcta; script de build validado por el parser de PowerShell.

## Evidencia

- `webp-migration-20261112.json`: recibo de cada imagen y resultado por carpeta.
- `disk-audit-before-webp-20261112.json` y `disk-audit-after-webp-20261112.json`: tamaños completos por componente.
- `dependency-audit-before-20261112.json` y `dependency-audit-after-20261112.json`: paquetes y cierre de dependencias.
- `project-cleanup-20261112.json`: rutas y bytes de artefactos retirados.
- `webp-browser-qa-20261112.json` y `webp-tests-20261112.xml`: navegador y suite.
- `../logs/diagnostico-webp-20261112.json` y `../logs/smoke-webp-20261112.txt`: diagnóstico y smoke.
