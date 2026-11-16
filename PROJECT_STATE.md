# Estado de entrega de ClaseViva

## Limpieza automática y fidelidad del material — 2026-11-16

- Corregido el cierre que retenía auxiliares por advertencias históricas de captura
  aunque todos los WAV estuvieran procesados. Las publicaciones nuevas integran y
  verifican el transcript completo antes de limpiar, con advertencias visibles.
- Watchdog conserva PCM parcial útil. Escriba procesa evidencia larga en bloques
  completos. Post conserva cifras y negaciones, recibe contexto vecino y busca
  tareas en todo el material. HTML incluye las tareas publicadas.
- Estado de publicación y limpieza durable en el panel. Respuestas LLM cortadas
  se rechazan, con causa de terminación y duración en el registro de uso.
- Aplicadas las brechas confirmadas por la revisión de MUSE: prompt activo
  exhaustivo, bloques de código protegidos y alerta de compresión al 35 % con un
  reintento. Eliminadas las leyendas genéricas y preservada señal tenue no nula.
- Semana 3 compactada con autorización, 383 imágenes y 30,86 MiB después de
  retirar las plantillas repetidas y regenerar el HTML. Sus fuentes originales
  fueron eliminadas, por lo que no es posible reauditar todo el audio.
- QA general final, 952 pruebas aprobadas y smoke correcto. Una prueba de propiedades
  excede cinco segundos y pasa sin temporizador. Lint E4/E7/E9/F correcto, lint
  amplio pendiente. Rendimiento y captura real de Windows pendientes de medir.
- [Auditoría y límites](history/AUDITORIA_LOGICA_20261116.md).

## WebP 85 y entorno de ejecución depurado — 2026-11-12

- Por autorización del usuario se migraron los 2.703 JPEG de las 13 carpetas a
  WebP 85, conservando resolución, EXIF, transparencia y perfiles de color.
  Imágenes: 388.907.162 → 234.956.622 bytes, otros 153.950.540 bytes menos.
  Se actualizaron nombres, referencias Markdown/HTML e inventarios SHA-256.
  Los 12 cuerpos de lección conservan la misma explicación, salvo rutas de imagen.
- Captura en vivo, copiloto, recorte de región, materiales oficiales y notas
  publicadas usan WebP. Materiales simples pueden usar WebP sin pérdida si pesa
  menos que la variante 85. Los WebP existentes no vuelven a recomprimirse.
- MIME `image/webp` explícito: Windows no lo registraba para la extensión.
  La exportación autocontenida y el servidor no dependen de ese registro.
- Retirados el portable/ZIP obsoletos de octubre, herramientas JPEG sustituidas,
  cachés de desarrollo y variantes experimentales. Se quitaron 12 paquetes del
  entorno original: Playwright y sus dependencias exclusivas, MozJPEG/CFFI y
  herramientas de pruebas/lint. Quedan 62 dependencias de ejecución y pip,
  exactamente las versiones de `requirements-windows.lock`.
- Proyecto: aproximadamente 1.268,9 → 533,8 MB. `.venv`: 569,15 → 414,84 MB.
  Se conserva OpenCV/ONNX para OCR y emparejamiento visual, AV para notas de voz
  y las dependencias HTTP que DDGS declara mediante extras.
- Verificación: 885 pruebas aprobadas (884 en conjunto y una de tiempos de audio
  repetida aisladamente, con su límite original, tras un timeout bajo carga),
  smoke y diagnóstico nativo correctos, 2.703 hashes/dimensiones comprobados,
  13 HTML válidos y 39 WebP de todas las carpetas abiertos en Edge.
- El build elimina su entorno temporal al finalizar correctamente; `-KeepWork`
  permite conservarlo para diagnóstico. Las herramientas de pruebas se reinstalan
  desde `requirements-dev.txt` cuando se necesitan.
- [Informe](history/WEBP_LIMPIEZA_PROYECTO_20261112.md), recibo por imagen en
  `history/webp-migration-20261112.json` y auditorías de disco/dependencias en
  `history/*audit*-20261112.json`.

## Comparativa de codecs y segunda pasada sin pérdida — 2026-11-12

- Revisados seis repositorios y comparadas 39 imágenes de las 13 carpetas.
  Sobre los JPEG ya optimizados: jpegoptim ahorró 1,64 %; JPEG XL reversible,
  18,93 %; WebP calidad 85, 42,16 % con pérdida; AVIF calidad 70 en 4:4:4,
  45,15 % con pérdida. WebP y AVIF sin pérdida aumentaron el tamaño en las
  configuraciones probadas. No se convirtió la biblioteca a formatos con pérdida.
- JPEG XL recuperó los 39 JPEG originales byte por byte. En la prueba local,
  Edge 157.0.4341.46 abrió JPEG, PNG, WebP y AVIF; no abrió JPEG XL.
- Aplicada la mejora compatible a los 2.703 JPEG: otros 6.489.730 bytes menos,
  conservando píxeles, dimensiones, EXIF e ICC. Reducción acumulada desde el estado
  original: 301.455.619 bytes. Imágenes + HTML actuales: 152.588.172 bytes en
  `contenido/` y 242.032.523 bytes en Infraestructura.
- `scripts.optimizar_imagenes` admite el backend opcional `--jpegoptim`, elige
  el menor JPEG entre original/MozJPEG/jpegoptim y verifica sus píxeles. El binario
  oficial 1.5.6 y su licencia están en `.cache/image-codecs/jpegoptim-1.5.6/`.
  Se retiraron los 137,7 MB de herramientas temporales de investigación.
- Verificación final: 2.703 hashes de píxeles coinciden con la primera pasada,
  hashes de archivos escritos comprobados, 12 cuerpos de lección idénticos,
  13 HTML con todos sus enlaces locales resueltos y 37 pruebas focales aprobadas.
  [Comparativa](history/COMPARATIVA_CODECS_20261112.md), resultados por muestra en
  `history/image-codec-benchmark-20261112.json` y recibo de la pasada adicional en
  `history/image-optimization-extra-20261112.json`.

## Imágenes y HTML ligeros — 2026-11-12

- Optimizadas las 2.703 imágenes de las 13 carpetas `img` de `contenido/` y
  `D:/05_Maestria_UNSA/3.SEMESTRE_III/7.INFRAESTRUCTURA`. JPEG conserva todos los
  píxeles, dimensiones, EXIF e ICC; no cambian los nombres ni las referencias.
- Imágenes + HTML en `contenido/`: 415.646.262 → 155.004.655 bytes. En
  Infraestructura: 280.430.052 → 246.105.770 bytes, incluidos cinco HTML nuevos.
  Reducción conjunta: 294.965.889 bytes. Los HTML ya no duplican imágenes Base64.
- Las 12 lecciones conservan exactamente su cuerpo y la metadata ajena a las
  imágenes. Solo cambian tamaños y SHA-256 del inventario. Se aligeró además la
  clase que únicamente conservaba HTML e imágenes.
- Exportación guardada con rutas relativas a `img/`; descarga autocontenida en
  memoria. Las capturas y páginas oficiales nuevas usan JPEG optimizado y
  progresivo, manteniendo calidad y resolución configuradas.
- Herramienta reutilizable: `python -m scripts.optimizar_imagenes --raiz contenido
  --aplicar --reporte history/imagenes.json`; admite varias raíces y restaura la
  clase ante fallos. Respaldos temporales retirados después de validar.
- Validación: 2.703 comparaciones de píxeles y hashes, 12 cuerpos idénticos,
  13 HTML con todos sus enlaces locales resueltos y 154 pruebas aprobadas.
  [Informe](history/OPTIMIZACION_IMAGENES_20261112.md) y recibo por imagen en
  `history/image-optimization-20261112.json`.

## Limpieza autorizada de Semana_2 — 2026-11-08

- Se verificaron 246 WAV procesados, cero pendientes y cero commits inválidos.
  La lección y sus 339 imágenes validaron por SHA-256. El usuario autorizó la
  limpieza tras conocer la advertencia de continuidad de 0,619 s.
- `visualizacion-de-datos/CLASE_01_semana-2` conserva únicamente
  `leccion_final.md`, `leccion_final.html` e `img/` con 339 imágenes. El Markdown
  mantiene el SHA-256 `b040e27ddeaf19627126d3362f870e718f7985d68fb44fccd285296ed983e9c4`.
  El resumen del curso también se conservó.
- Eliminados los WAV, transcripciones y auxiliares de trabajo, 1.335 imágenes
  sin referencia y el respaldo temporal `recovery_20261107_semana2_8b6c547d`.
  Sus 134 enlaces NTFS fueron comprobados contra los WAV originales antes de
  eliminarlos. Espacio liberado estimado, sin duplicar enlaces: 1.396.794.582 bytes.
- Recibo e inventario de integridad en
  `history/cleanup-CLASE_01_semana-2-1794152187882128100.json`.

## Fin sin espera por APIs pendientes — 2026-11-07

- El cierre distingue captura terminada de transcripción pendiente. Si el STT
  está estacionado o su proceso terminó, Escriba sale sin esperar el timeout de
  75 s, sin publicar un resumen incompleto y sin avanzar los cursores.
- El panel muestra **Lista para consolidar**, cuenta los audios pendientes y
  habilita su recuperación. Una lección anterior no oculta esos pendientes.
  La captura sin cerrar sigue bloqueando la consolidación.
- Corregido el primer arranque del servicio auxiliar: crea `logs/consolidacion`
  antes de escribir su estado. Un fallo de escritura o arranque del worker libera
  el indicador de ejecución, muestra un error y permite volver a intentar.
- Se detectó que el clic actual había fallado antes de arrancar el worker; se
  actualizó solo el auxiliar y se retomó la solicitud. Verificado el inicio real
  de la transcripción de los siete WAV pendientes, sin reiniciar el panel.
- Validación: 295 pruebas aprobadas de recuperación, calendario de resúmenes,
  API del panel, cierre de procesos, ciclo STT, concurrencia y publicación.

## Recuperación de APIs sin interrumpir captura — 2026-11-07

- Clase activa `visualizacion-de-datos/CLASE_01_semana-2`, parte 3: se conservaron
  los procesos capturadores y se verificó que seguían grabando durante la reparación.
- Parte 2: 134 WAV válidos, 67 min y 709.601.896 bytes protegidos mediante enlaces
  NTFS en `backups/recovery_20261107_semana2_8b6c547d/`. Recibo con SHA-256 incluido.
  No hay WAV ausentes; quedan siete bloques síncronos ambiguos para el clic manual.
- **Consolidar** ahora recupera primero STT pendiente. Los commits válidos evitan
  retranscribir; los jobs conocidos se recuperan antes de otro envío. Un envío
  síncrono desconocido solo se abandona/reenvía por reintento manual auditado.
  El intento manual vuelve a comprobar red/autorización, respetando cuotas y 429.
- DeepSeek sin respuesta es error durante la consolidación, nunca un resumen de
  éxito mediante fallback. No se compacta ante fallo; los avances se retoman en
  otro clic. Las tareas se consultan antes de reemplazar la lección publicada.
- `consolidation_recovery.py` integra el flujo en panel/CLI. `recovery_service.py`
  y el cliente compatible habilitan el panel ya abierto recargando solo la página,
  sin reiniciar la aplicación ni sus capturadores. No procesan mientras se graba.
- Los resúmenes de una continuación se anclan a la última reanudación, evitando
  ventanas vacías en las horas entre sesiones. La consolidación usa el transcript
  canónico y no depende de que aquellas fichas en vivo se hayan generado.
- Validación: 420 pruebas aprobadas; navegador aislado con dos reintentos y error
  visible, cero llamadas STT/LLM. Panel real compatible verificado en parte 3 activa.
  Auditoría local: `logs/recovery-20261107-delivery.json`.

## Inicio, copiloto y fuentes de audio — 2026-11-07

- Restaurado `.venv` con Python 3.12.10 y `requirements-windows.lock`. VBS valida
  el intérprete; BAT conserva la consola para mostrar errores de inicio.
- El copiloto bloquea envíos simultáneos por Enter/clic. El servidor conserva
  respuestas por identificador de consulta para evitar repetir llamadas y `qa.md`.
- Audio: selección de aplicaciones abiertas por PID e instante de creación,
  árbol de procesos y sesiones en todas las salidas activas. Comet detectado y
  apertura de process-loopback verificada localmente, sin guardar audio.
- Fuentes cerradas requieren actualizar y volver a seleccionar. Se conservan
  los alias anteriores de Chrome/Edge y la alternativa Todo el sistema.
- Inventario filtrado: aplicaciones con sesión de salida y programas de audio
  conocidos abiertos; omite Task Manager/utilidades y atribuye WebView a su dueño.
  Nombres e iconos reconocibles; estado solo de la fuente elegida y controles alineados.
- Pruebas: `test_copiloto_submission.py`, `test_audio_app_sources.py` y regresiones
  de refresco, captura continua, admisión y runtime de audio.
- Cierre de fuentes/UI: 100 pruebas aprobadas; navegador verificado a 1742,
  1100, 900 y 390 px sin controles fuera del panel ni errores de JavaScript.
  Comprobados el icono de Edge, el estado de la fuente elegida y su conservación
  al actualizar. Evidencia local: `logs/qa-audio-sources-20261107/`.
- Iconos reales de Windows: EXE y manifiestos de apps Store, caché automática en
  `.cache/iconos_apps`, extracción en dos hilos y clave por ejecutable/versión.
  El selector muestra imágenes locales y conserva selección, teclado y fallback.
  Suite ampliada: 124 pruebas aprobadas; lista e iconos comprobados en navegador.
  Medición local de cuatro programas: 36,11 ms hasta tener sus PNG; 1,863 ms por
  consulta de los cuatro en caché. Recibo: `logs/icon-performance-20261107.json`.

## Búsqueda Web — 2026-10-27

- El copiloto usa descarga HTTP del feed de Bing, sin Search ni Fetch de DeepSeek.
  La cadena STT no se modificó. La versión anterior ya usaba cero llamadas a estas
  herramientas de DeepSeek; el ahorro adicional comprobado es cero por clase.
- Suite: 825 pruebas aprobadas y cuatro pruebas focales tras el ajuste final.
  Portable actual: `dist/ClaseViva-Portable-20261027.zip`; diagnóstico offline y
  hashes de sus 485 archivos verificados. [Informe](AUDITORIA_WEB_20261027.md).

## Cierre de biblioteca y Silero VAD — 2026-10-16

- Diez finales Markdown + HTML y 2.313 imágenes verificadas. IA 04 consolidada;
  IA 05 compactada con recuperación histórica confirmada, ligada al hash exacto.
  Cero WAV y estados de sesión pendientes dentro de las clases. Ahorro neto en
  carpetas de clase: 3.399.851.946 B. Inventario `history/library-final-20261016.json`.
- Silero VAD 6.2.3 incluido y activo, CPU/ONNX, sin Torch. Pausas >=2 s, umbral
  conservador 0,20 y margen 400 ms. Fallback digital ante error o voz no reconocida.
  Mapa y hash de audio persistidos antes del envío; recuperación sin recalcular VAD.
- Admisión de captura serializada con Pausa/Fin. Cobertura usa reloj PCM del manifiesto.
  Corregido render de rutas con guion bajo y enlace histórico truncado en IA 02.
- `.venv` reparado con Python local 3.12.10 en `.runtime/python312/tools`.
  Las notas inferiores que indican entorno roto o IA 04/05 pendientes son históricas.
- Portable reconstruido: `dist/ClaseViva-Portable.zip`, 154.417.578 B. Sus 441 archivos
  verifican por tamaño/hash y el diagnóstico offline pasa también tras extraer el ZIP.
  Silero CPU incluido. Recibo: `history/portable-20261016-delivery.json`.
  Se retiraron `.venv-qa`, temporales de compilación/extracción, cachés y logs antiguos.
  Las referencias inferiores a un portable pendiente o externo son históricas.
- Portable reconstruido: `dist/ClaseViva-Portable.zip`, 154.417.578 B. Sus 441 archivos
  verifican por tamaño/hash y el diagnóstico offline pasa también tras extraer el ZIP.
  Silero CPU incluido. Recibo: `history/portable-20261016-delivery.json`.
  Se retiraron `.venv-qa`, temporales de compilación/extracción, cachés y logs antiguos.
  Las referencias inferiores a un portable pendiente o externo son históricas.
- Suite completa: 820 passed; ajustes finales de render/portable: 31 passed. Ruff PASS.
  Medición Silero: 20 min → 1.149,760 s, 4,19 % menos; digital solo 1,61 % en esa muestra.
- Detalles y límites actuales: [ENTREGA_20261016.md](ENTREGA_20261016.md).

## Auditoría de eficiencia y cuotas gratuitas — 2026-10-16

- Se conserva la cadena y los modelos STT. Un trabajo remoto pendiente se recupera
  antes de intentar otros proveedores; eventos antiguos de intentos fallidos no
  resucitan trabajos. Éxito remoto asíncrono sin commit local recupera el mismo job.
  402 suspende el proveedor al menos una hora; Retry-After admite
  fecha HTTP y 429 sin cabecera espera 60 s.
- Caché LLM distingue mensajes/roles, presupuesto y endpoint, con escritura atómica.
  Nuevas publicaciones conservan métricas STT compactas por bloque sin inventar saldos.
- IA 03 y 06 compactadas: 273 WAV retirados, 412 imágenes conservadas, Markdown con
  hashes intactos. Ahorro neto adicional **1.677.530.432 B**, ya descontado el HTML
  autocontenido generado. Recibos e inventarios en `history/`.
- IA 04 sigue sin final; IA 05 conserva evidencia por discrepancias de cobertura y
  tiempos. La autorización actual de limpieza sustituye la restricción histórica de
  solo lectura para IA 06 que aparece más abajo.
- Muestra offline: 40 WAV / 20 minutos; recorte digital de 73,104 s (6,092 %).
  Ledger LLM: 434 llamadas, US$0,920394812 estimados históricos, no factura ni saldo.
- 319 regresiones seleccionadas pasan con proveedores simulados. Se estabilizó el
  reloj del test de cierre que antes comparaba microsegundos reales con 1 s exacto.
- Comparativa y límites: [AUDITORIA_APP_20261016.md](AUDITORIA_APP_20261016.md).
  Silero ONNX queda propuesto para evaluación, no activado. `.venv` sigue apuntando
  a Python 3.12 ausente; QA usa `.venv-qa` con 3.14. Portable aún sin reconstruir.

## Pausas, limpieza y recorte de silencios — 2026-10-16

- Corregida la retención de auxiliares tras pausar y apagar: solo se descuentan
  adquisiciones sin PCM iniciadas dentro de una pausa cerrada y reconocidas al
  reanudar. La auditoría original mantiene el fallo; voz pendiente sigue bloqueando.
- Última clase (`CLASE_04_semana-4`): 542 commits válidos, cero pendientes,
  877 imágenes conservadas y 3.593.840.772 bytes retirados. Markdown intacto.
- STT recorta pausas digitales largas con márgenes y mapa al reloj original;
  conserva el failover y los trabajos pendientes. No es un detector de palabras.
- Panel informa si la lección se publicó pero la limpieza quedó retenida.
- Reintento offline: `python -m scripts.compactar_publicada --clase <ruta>`;
  vista previa por defecto, `--aplicar` para limpiar con recibo.
- 328 pruebas específicas pasan. Entorno `.venv-qa` con Python 3.14: el Python
  3.12 apuntado por `.venv` ya no está instalado. No se reconstruyó el portable.
- Evidencia y límites: `AUDITORIA_OPTIMIZACION.md` y recibo
  `history/cleanup-CLASE_04_semana-4-1792207462592222400.json`.

## Corrección de limpieza, exportación y copiloto — 2026-09-26

- Spark (`iii-i-infraestructura-para-big-data/CLASE_03_spark`): 277 commits válidos,
  ninguno pendiente; un bloque silencioso en adquisición al pausar producía una
  falsa discontinuidad temporal. Reconocimiento acotado con prueba de adquisición,
  duración y finalización; no se relajan los gates de pérdida PCM ni de voz.
- Limpieza transaccional realizada sin regenerar texto ni llamadas API:
  1.817.916.869 bytes retirados, 179 imágenes conservadas, Markdown con hash intacto.
  Inventario y recibo: `history/spark-cleanup-20260926.json`.
- Paquete de estudio: `leccion_final.md`, `leccion_final.html` e `img/`.
  HTML autocontenido de 45.755.792 bytes, 179 imágenes incrustadas verificadas contra
  originales. Se comparte solo HTML; conservar Markdown e img para continuar clase.
- Copiloto: pregunta visualmente arriba, respuesta desde su inicio, ampliable,
  sin controles superpuestos y sin mezclar respuestas concurrentes. Seis de ocho
  consultas recientes alcanzaron 1.800 tokens: presupuesto acotado a 3.200 con
  thinking desactivado y aviso explícito si finish_reason=length; sin auto-continuar.
- Pendiente: comprobar interacción y próxima consolidación en sesión real. Las
  pruebas automatizadas no llaman a proveedores. No se modificó la lección aceptada.

## Continuación Spark y compactación — 2026-09-28

- La consolidación nocturna de la clase 3 conservó 115 WAV aunque los 115 tenían
  commits STT válidos y no existía trabajo pendiente. Ocho commits estaban vacíos;
  cinco archivos de RMS bajo quedaron sin segmento cercano. El gate confundía una
  respuesta STT terminal sin texto con audio aún no procesado.
- El gate de paquete ligero ahora acepta esos WAV únicamente cuando cada resultado
  sin texto está cubierto por un commit vacío válido, y bloquea WAV pendientes,
  commits ausentes/alterados y continuidad PCM advertida. No reenvía audio.
- Inventario SHA-256 y recibo de esta ejecución: `history/spark-cleanup-20260928.json`.
  Retirados 776.395.490 bytes; quedaron 235 imágenes. Markdown preservado con SHA-256
  `8a9e41a886b5249b82938fb01c8e36a1d4a9f4bca563d8094e5e5ef69409c2bd`.
- HTML autocontenido verificado: 235/235 referencias incrustadas por hash. La clase
  deja solo `leccion_final.md`, `leccion_final.html` e `img/`.
- Cobertura que se conserva en la lección: 8 commits completados sin texto y un
  intervalo de 7:50 con 139 capturas sin explicación oral cercana. Los WAV fueron
  retirados conforme al paquete ligero; si se necesita recuperar ese audio, no hay
  copia dentro de la clase.

Los registros de distribución siguientes son históricos; la entrega se actualiza
en el mismo `../ClaseViva-Portable.zip`, según `history/ENTREGA_PORTABLE.md`.

## Distribución única — 2026-09-28

La entrega vigente es `../ClaseViva-Portable.zip`, reconstruida desde esta fuente con
la corrección de compactación. Sustituye los directorios y ZIP RC1/intermedios de
septiembre. El inventario y checksum de la entrega se conservan en `history/`.
`HISTORIAL_CAMBIOS.md` documenta la evolución; los seis checkpoints esenciales de las
auditorías anteriores están en `history/checkpoints-20260914-20260924.zip`. Git y los
cambios sin commit, el entorno local, tests, cursos y claves permanecen conservados.

ZIP nuevo verificado tras extraer: 438 archivos; 152.422.489 bytes comprimidos.
SHA-256 `7f1bd16f7ba0ab34fbbb68b2877a263c8b31c4fe4230a8d0a257dd0896dbc9c4`.
El borrado de las cinco entregas/auditorías externas antiguas fue rechazado por la
herramienta antes de ejecutarse: siguen en disco, al igual que temporales de build.
No se reintentó por otra vía. Limpieza pendiente; distribución actual sí terminada.

Los apartados siguientes registran hitos anteriores; sus rutas externas antiguas
ya no son instrucciones de distribución. La recompilación sustituye la limitación
de RC1 indicada en el informe de la corrección focal.

## Corrección focal de compactación — 2026-09-24

- Fuente actual corregida; el portable RC1 del 14/09 no se ha recompilado con esta
  corrección. Usar `ClaseViva.vbs` en este working tree para cargarla.
- Caso: Big Data, `CLASE_02_clase-2`, capturada el 19/09 y consolidada el 24/09.
  No falló por consolidar días después: el gate confundía una pausa manual con
  831,753 s de audio perdido. Tras reanudar, la solicitud llegó 1,004 s después
  (sondeo de pausa de 1 s) y el PCM comenzó 65 ms después; superó la tolerancia
  antigua de 750 ms aplicada al intervalo completo.
- `audio_manifest.continuity_report` reconoce ese arranque solo si hay pausa
  cerrada, evento `capturing` dentro de 1,25 s, apertura PCM dentro de 750 ms,
  stream distinto y contador de frames iniciado en cero. Registra el retraso;
  no amplía la tolerancia general ni oculta huecos físicos, fallos o discontinuidades.
- 9 regresiones sintéticas nuevas; focales 71 passed. Harness completo: **760
  passed en 72,49 s**, Ruff PASS y SMOKE_OK. Sin nuevas dependencias ni APIs.
- Auditoría local: 321 WAV verificados, 321 commits completos (16 sin segmentos),
  0 pendientes/inválidos, 0 WAV útiles sin transcript cercano, continuidad física
  corregida `ok`. Se recalcularon también las métricas de los 321 WAV como contraste.
  Los avisos de periodos sin voz siguen siendo independientes de la integridad PCM.
- Reparación sin regeneración: 2.226.902.824 → 5.535.235 bytes; 3.616 → 39 archivos.
  Solo `leccion_final.md` + `img/` (38 imágenes), hashes y referencias válidos.
  SHA-256 de la lección antes/después:
  `25a581ecdace71ba7b927171ed3f07ac6d2523e39127f6b6db625834e41bad1d`.
  No se tocaron otros cursos. Evidencia auxiliar descartada tras validar; no existe
  respaldo WAV. Se preservaron inventario/hash y registros forenses pequeños fuera
  del curso en `../ClaseViva-cleanup-audit-20260924/`.
- Patch focal verificable: `focal-fix.patch` en esa carpeta, reverse-check PASS,
  SHA-256 `8f9d5a3f7f6ec0ad646e9528f371a4e150570b6a43ec45e5bfe992206d18251d`.
  Sin commit. Rollback del código mediante ese patch tras revisar nuevos cambios;
  el paquete ligero sigue siendo reanudable, pero no recupera WAV ya eliminados.
- Pendiente: próxima prueba real de pausa/reanudación y consolidación diferida;
  arranques mayores que las cotas continúan reteniendo evidencia por seguridad.

## Entrega portable anterior

Actualizado: 2026-09-14. Base Git `9ba31d4`; cambios actuales sin commit.
Estado: **portable Windows validado localmente; prueba en otro equipo pendiente**.
No se regeneraron lecciones, no se modificó `.env` y no hubo inferencia STT/DeepSeek
facturable. Linux queda explícitamente fuera del alcance.

## Contratos conservados

- Un propietario del stream; WAV canónicos de 30 s y último chunk corto; publicación
  sealed tras fsync/rename/hash; sequence, backpressure y recuperación idempotente.
- STT interno, remoto y secuencial: AssemblyAI → Deepgram → Speechmatics → Groq.
  Silencio filtrado; commits válidos no se retranscriben. Fallos conservan pendientes.
  No Whisper/CUDA local, hedging ni transcript en vivo. Consentimiento por clase.
- Resúmenes cada cinco minutos útiles; la acción manual no mueve las fronteras.
  Contexto incremental y límites económicos existentes sin cambios.
- Notas e intervenciones del estudiante por acción explícita, separadas de la voz docente.
- Panorama, glosario, desarrollo exhaustivo, imágenes y continuación desde paquete ligero.
  Consolidar publica primero y solo compacta si audio, commits, notas y hashes validan.
  No se relajaron gates de eliminación ni se borró evidencia real.

## Cambios implementados

### Windows portable

- `src/app_paths.py`: recursos en `_internal`, datos y `.env` junto al EXE. Fuente/VBS
  mantiene el layout anterior. Override absoluto `CLASEVIVA_DATA_DIR` para pruebas aisladas.
- `src/desktop_app.py` y `lanzador.py`: entrada panel/worker, freeze_support, mutex de
  apertura y healthcheck que identifica app + carpeta. Doble apertura no duplica workers.
  El comando congelado conserva módulo/PID/create_time para locks y cierre coordinado.
- `.env` se crea solo si falta y nunca reemplaza claves. Admite BOM y valores entre comillas.
  `Configurar claves.cmd` permite editarlo sin terminal. Sin administrador ni firewall abierto.
- `ClaseViva.spec`, `requirements-windows.lock` (62 paquetes), `requirements-build.txt`:
  compilación desde entorno Windows/Python 3.12 x64 limpio, dependencias fijadas.
  Modo directorio: evita extraer un runtime por cada worker. No copia el .venv del usuario.
- `scripts/build_windows.ps1` rechaza salidas existentes. El gate
  `verify_windows_bundle.py` verifica imports, PDF, OCR, icono, renderizado rico y los
  cuatro workers sin clase activa, con claves vacías y PATH sin Python. Produce hashes.
- OCR: se reprodujo en fuente la incompatibilidad de cargar PyWinRT 3.2.1 antes de
  ONNX Runtime 1.27.0 (DLL initialization error 1114). El backend Windows reserva ORT
  primero al solicitar OCR: sin cargar modelos/sesiones ni GPU al abrir el panel.

### Interfaz y lectura

- Icono generado con IA mediante la skill imagegen, paleta aqua/azul/lila del producto.
  `assets/claseviva.png` y un ICO multirresolución (16–256 px), integrados en panel y EXE.
  Procedencia y conversión reproducible en `assets/README.md`.
- Actualización manual de pantallas/salidas/procesos Chrome y Edge, nombres e iconos;
  diferencia presencia de proceso y señal de audio. No cambia fuente ni añade polling.
- Fallback activo y saludable ya no aparece como intento fallido. La mezcla del sistema
  advierte que puede incluir otras apps. Foco de teclado y movimiento reducido.
- El reloj vacío no avanza antes de abrir clase; sesión total incluye pausas, captura
  útil no. Formato HH:MM:SS e interpolación local sin aumentar consultas al servidor.
- La prueba visual detectó dos defectos previos del visor, no de las extensiones del EXE:
  mostraba el comentario técnico del paquete y bloqueaba imágenes Markdown `img/...`.
  Ahora oculta metadata solo al renderizar y normaliza rutas relativas estrictas.
  Markdown original, hashes y bloqueos de traversal/script permanecen intactos.

### Guías visuales y costos

- `post_procesar.py`: OCR propio y señales complementarias para distinguir ML, código,
  fórmulas, calidad/veracidad y escala de datos. «Muestra» no demuestra píxeles/etiquetas.
  Fórmulas, agendas y comparaciones prevalecen sobre menciones genéricas de datos.
- Gate de títulos OCR ruidosos y corroboración textual; evita afirmar que frames
  retirados por compactación siguen disponibles. No reduce presupuesto de imágenes ni
  extensión del desarrollo, ni cambia los prompts/ventanas LLM aprobados.
- Ejemplo/default usa `deepseek-flash`. El alias local `deepseek-v4-flash` se conserva
  y sigue admitido según el [catálogo oficial](https://api-docs.deepseek.com/).
- `provider_pricing.py`: estimación versionada por modelo devuelto, tokens caché y franja
  UTC según [tarifa oficial consultada](https://api-docs.deepseek.com/quick_start/pricing/).
  Contabilidad incremental; modelo desconocido queda «no estimable», no cero ficticio.
  Sin desglose de caché se registra cota superior. Panel indica estimación parcial.
  No recalcula históricos; no equivale a factura ni saldo/créditos STT.

## Evidencia y validaciones

- Harness final: **751 passed en 50,96 s**, Ruff PASS, SMOKE_OK, pip check PASS.
  Tests con temporales y APIs simuladas. Build instalado desde cero también pasó
  pip check. No se instaló PyInstaller en el entorno de desarrollo.
- Gate EXE offline PASS: librerías nativas, render PDF, OCR CPU incluido, icono,
  encabezados/tablas/código/MathML/imágenes y metadata invisible. Cada worker inicia y
  sale con «no hay clase activa», sin capturar ni llamar APIs.
- Arranque EXE aislado: 2,14 s en esta laptop; 60,6 MiB RSS al arranque y cero hijos.
  Segunda apertura exitosa sin segundo servidor. No es benchmark térmico ni de grabación.
- UI inspeccionada: Inicio, refresco manual, iconos, reloj sin clase, visor largo y
  fichas imagen izquierda/texto derecha. Se conserva el diseño aprobado.
- Copia temporal de Hadoop: hidratación a estado fin, sin iniciar grabación; visor
  entrega 374.893 B HTML, 101 tablas, 4 MathML y **148 imágenes** cuyos SHA-256 se
  verificaron por HTTP local. Markdown servido idéntico al original.
- A/B determinista de clasificación visual contra HEAD, sin LLM ni reescritura:
  IA 05: 3.300 registros, 297 reclasificados (0,758 s); IA 06: 1.377 registros,
  85 reclasificados (0,251 s). No es evaluación humana de cada caption ni regeneración
  completa. Fixtures cubren calidad, zettabytes, ML real, agenda, fórmula y ruido OCR.

Última lección aceptada: `iii-i-infraestructura-para-big-data/CLASE_01_clase-1-hadoop`.
Solo `leccion_final.md + img/`: 15.739.616 B, 2:18:30 útiles, un bloque y diez tramos.
148 imágenes (96 cuerpo/52 galería), cero referencias inexistentes, huérfanas, repetidas,
hashes duplicados o corruptas; metadata reanudable validada. SHA-256 de la lección:
`b0a999055eef96eef11f98992204fd1086f3457e48b2b0f695217874d4d2a694`.

Las clases IA 05 y 06 conservan 310/115 WAV y 3.303/1.379 imágenes, respectivamente.
Lecciones SHA-256: `48dad3654bc2e7dedd26bb6ca2b8736127538007d9fe34189f5084ea11b57f7a`
y `58ac6c55c7bee757647ac01183740b0cacf9b2f978d40b992a2be524aeaf67af`.
Se usan como contraste de solo lectura, no como autorización para compactarlas.

`contenido` íntegro: 11.333 archivos, 5.669.890.425 B. Fingerprint igual al anterior:
`b9d24fd8cf6afe4f7077b6e4f8bfe77752c54b57dd296e78a7fca7cdccc86dc7`.
Algoritmo: rutas relativas a contenido con barra inicial inversa, tamaño y ticks mtime UTC,
separados por |, orden PowerShell Sort-Object, líneas LF sin LF final, SHA-256 UTF-8.
Es fingerprint de metadata, no hash de todos los bytes de los cursos.

## Entrega, límites y siguiente prueba

Portable limpio en `../ClaseViva-Windows-20260914-rc1/ClaseViva`; ZIP
`../ClaseViva-Windows-20260914-rc1.zip`.
Incluye runtime, assets, plantilla vacía, configuración y guía de uso. Excluye claves,
cursos, logs, tests, Git y evidencia externa. `bundle-verification.json` contiene hashes.
438 archivos, 354.009.006 B descomprimidos. `THIRD_PARTY` conserva 99 avisos disponibles
en los wheels y en Python, con inventario de versiones; no incorpora código de tests.
Uso y construcción: `WINDOWS_PORTABLE.md`.

Validado en Windows 11 x64 de esta laptop. Pendiente Windows limpio sin Python y otra
tarjeta/controlador: grabar 5–10 minutos, pausa/reanudar, cambio de fuentes, Fin, consolidar,
continuación, fallo de red y cierre sin huérfanos. Windows 10 x64 es objetivo, no validación
realizada. PPTX requiere PowerPoint o exportar a PDF; PDF e imágenes no requieren Office.
EXE sin firma: SmartScreen/SAC puede bloquearlo. No desactivar seguridad; distribución
pública requiere firma y validación en equipos limpios. No se garantizan cuotas STT gratuitas
ni equivalencia editorial exacta con futuras versiones de API.

No se promete una mejora térmica por empaquetar. OCR sigue en CPU y STT remoto;
Chrome/Edge/VideoDecode y perfiles de energía necesitan A/B de hardware controlado para
atribuir temperatura. No se alteraron esas configuraciones.

Harness mínimo: se conserva run_dev_checks; solo se añaden lock/build/gate portable y
regresiones de contratos observados. No se creó ni reescribió AGENTS/skills de gobernanza.
La skill imagegen solo influyó en el recurso gráfico. Sin nueva skill necesaria.

Rollback: cerrar la app y volver a una copia fuente del commit base con su .venv;
conservar .env y contenido. No aplicar un ZIP limpio encima de datos de usuario.
No hay migración irreversible de cursos; metadata se oculta solo en pantalla. Git y los
cambios sin commit quedan disponibles para revisión, sin resets ni borrado de historial.
