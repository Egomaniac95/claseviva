# ClaseViva

La opción **Web** del copiloto descarga resultados mediante HTTP desde el feed público
de Bing y los entrega como contexto al modelo. Filtra resultados con escasas coincidencias
con la consulta cuando el proveedor devuelve contenido ajeno. No utiliza los endpoints Search ni
Fetch de DeepSeek; si la descarga falla, informa que no hay fuentes disponibles.
El enriquecimiento opcional de apuntes usa búsquedas externas y descarga directa de
páginas, también sin las herramientas de DeepSeek. Las respuestas del modelo siguen
consumiendo tokens según la tarifa configurada.

ClaseViva captura audio y evidencia visual de clases, webinars y videos, crea pausas de
estudio periódicas y consolida una lección Markdown autocontenida.

## Inicio habitual

La distribución portable para Windows se abre con `ClaseViva.exe`, sin instalar Python.
Descomprime la carpeta completa y usa `Configurar claves.cmd` para editar `.env`.
Consulta [WINDOWS_PORTABLE.md](WINDOWS_PORTABLE.md) para uso, reconstrucción y límites.
El paquete limpio no contiene cursos ni claves. Linux queda fuera del alcance.

Si trabajas desde el código fuente, el inicio anterior se conserva:

Haz doble clic en `ClaseViva.vbs`. Ese archivo usa
`.venv\Scripts\pythonw.exe`, ejecuta `lanzador.py` y abre el panel local en
`http://127.0.0.1:8756`.
Si falla el inicio, `ClaseViva.bat` muestra el error en una consola y la mantiene
abierta para poder leerlo. El VBS comprueba que el Python del entorno pueda ejecutarse.

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
En este equipo el entorno se restauró el 07/11/2026 con `py -3.12` (Python 3.12.10)
y las versiones de `requirements-windows.lock`. `ClaseViva.vbs` usa esa `.venv`;
conserva también la instalación de Python 3.12 con la que se creó el entorno.
El portable incluye su propio intérprete y se distribuye por separado.
`requirements.txt` contiene el runtime. El entorno de uso diario conserva solo esas
dependencias; las herramientas de pruebas se instalan cuando se necesitan.
`requirements-dev.txt` solo añade pytest, Ruff y
herramientas de QA para quien vaya a revisar el código:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_dev_checks.ps1
```

El build portable usa `requirements-windows.lock` (runtime exacto) y
`requirements-build.txt` (compilación aislada); no copia tu `.venv` ni requiere
instalar PyInstaller en ella. `.env.example` usa `deepseek-flash`; los aliases previos
siguen admitidos. El costo visible es una estimación DeepSeek versionada por modelo,
caché y horario, no la factura ni el saldo/crédito STT. El historial no se recalcula.

## Flujo y datos

Antes de grabar selecciona fuente de audio y pantalla y autoriza el STT remoto.
El selector de audio enumera programas con una sesión de salida de sonido y
navegadores o aplicaciones de llamadas abiertas, aunque aún estén en silencio.
Incluye Comet, Zoom y otros programas; excluye las utilidades del sistema y agrupa
los motores WebView con la aplicación que los utiliza. Muestra nombres como
**Microsoft Edge** y un único estado para la fuente seleccionada.
Los iconos reales se obtienen automáticamente de Windows, incluidos los recursos
de aplicaciones de Microsoft Store, y se guardan en `.cache/iconos_apps`.
La extracción ocurre en segundo plano al descubrir un programa nuevo; la caché
se reutiliza tras reiniciar y se renueva cuando cambia el ejecutable. Cada equipo
la crea a partir de sus propias aplicaciones, sin copiar ni registrar iconos a mano.
Pulsa **Actualizar fuentes** si
abres o cierras una aplicación. La selección queda ligada al PID y a su instante
de creación; captura su árbol de procesos. Si se cierra, selecciona de nuevo la
aplicación al abrirla. Los alias antiguos de Chrome y Edge siguen admitidos.

El copiloto acepta una consulta a la vez por clase: Enter repetido o un clic
adicional mientras responde no duplica las burbujas ni la llamada al modelo.
Los reintentos HTTP con el mismo identificador reutilizan la respuesta guardada
en memoria y no vuelven a escribir el par en `qa.md`.

El failover es secuencial: AssemblyAI Universal-3.5 Pro → Deepgram Nova-3 multilingüe →
Speechmatics Melia-1 multilingüe → Groq `whisper-large-v3`. Solo se construyen adaptadores
cuya clave exista en `.env`; no hay Whisper local ni solicitudes paralelas.

Las cuotas gratuitas se aprovechan en ese orden. Si un proveedor alternativo ya
aceptó un bloque, se recupera ese trabajo antes de intentar otro envío. Los límites
429 respetan `Retry-After` (también en formato fecha; 60 s si falta), y un 402 por
créditos agotados suspende ese proveedor al menos una hora. El siguiente proveedor
sigue atendiendo los bloques nuevos. Esto no activa pagos ni cambia modelos.

Silero VAD 6.2.3 se ejecuta localmente en CPU, con modelo incluido y sin PyTorch.
En trabajos nuevos solo recorta pausas de al menos dos segundos que combinan una
probabilidad de voz inferior a 0,20 con muestras exactamente cero, dejando 400 ms
de margen a cada lado. Si falla o no detecta voz con confianza, usa el filtro de
silencio digital, que también exige ceros. La captura y el STT conservan la señal
no nula aunque tenga volumen muy bajo. Esto puede aumentar el audio enviado y su
costo si hay ruido de fondo; el reconocimiento de todas las palabras sigue
dependiendo de la calidad de la señal y del proveedor.

El mapa de recortes y el hash del audio enviado se guardan antes de la solicitud.
Al reanudar se recupera ese mismo mapa, incluso si cambian modelo o configuración.
Los tiempos vuelven al reloj original para mantener la relación con las capturas.
`CLASE_STT_VAD=digital` selecciona el filtro digital y `CLASE_STT_TRIM_SILENCE=0`
desactiva todo recorte para trabajos nuevos. Los pendientes conservan su transformación.

La transcripción es interna: alimenta resúmenes y la lección, pero no se renderiza en
vivo. Captura, WAV, hashes, manifest, commits y recuperación permanecen locales. El
silencio comprobado localmente no se envía. Un commit válido evita retranscribir o volver
a cobrar un chunk. Si se agota la cadena, se conserva el WAV pendiente.

El acompañamiento funciona por lotes: audio de 30 segundos y apuntes automáticos
cada cinco minutos. A esos intervalos se añade la espera del STT y del modelo.
El trabajador STT es secuencial; un fallo remoto puede demorar los bloques siguientes.

```text
contenido/<curso>/
  CURSO.md
  TAREAS.md
  resumenes/
  clases/<clase>/
```

Al **Consolidar**, se genera el desarrollo detallado y enriquecido de la clase. Las
publicaciones nuevas incorporan el registro completo de los segmentos transcritos en
un apéndice desplegable del Markdown y del HTML. El registro conserva también las
repeticiones, cifras y correcciones, y se verifica antes de retirar los archivos de trabajo.
La fuente queda dentro de esos archivos finales, con sus segmentos y marcas de tiempo,
sin necesitar un `transcript.jsonl` separado después de compactar. El desarrollo pide
conservar razonamientos, ejemplos, preguntas y código sustentado en la evidencia.
Las ampliaciones técnicas se distinguen del contenido registrado. Las leyendas de las
imágenes explican su contenido concreto y no añaden plantillas genéricas de repaso.

Cada tramo que queda por debajo del 35 % de la extensión del texto reconocido recibe
un único reintento de desarrollo. Si sigue breve, publica un aviso y el registro del
tramo. Es una alerta de compresión por longitud, no una medida de cobertura semántica.
El registro completo se conserva siempre, independientemente de ese umbral.

La compactación automática completa el cierre cuando todo el audio guardado está
procesado, las notas están decididas y el texto fuente y las imágenes publicados validan.
Las advertencias históricas de captura quedan documentadas en la lección y permiten
limpiar cuando esos requisitos se cumplen. Audio pendiente, commits inválidos, evidencia
nueva o una publicación alterada retienen los archivos y dejan un motivo recuperable.

Si una API o el firewall impiden procesar la clase, el WAV sellado permanece local.
**Fin** cierra los capturadores y los archivos. Cuando el STT queda estacionado
por un fallo remoto, el cierre termina y el panel muestra **Lista para consolidar**
con la cantidad de audios pendientes; no espera indefinidamente a la API.
Después de **Fin**, **Consolidar** recupera primero todos los audios pendientes y
después genera la lección y su resumen. Un fallo de STT o DeepSeek se muestra como
error y habilita **Reintentar consolidación**. Cada nuevo clic retoma los commits y
respuestas válidos; no vuelve a transcribir los bloques ya completados ni compacta
la evidencia durante un fallo. Los trabajos remotos con identificador se recuperan;
un envío síncrono ambiguo solo se vuelve a enviar por ese intento manual explícito.

Para aplicar esta recuperación a un panel antiguo que ya está grabando, el puente
local `python -m src.recovery_service` permite recargar únicamente la página.
No inicia ni detiene capturadores, exige **Fin** antes de procesar y se retira al
cerrar el panel o detectar el panel actualizado. La versión nueva integra la
recuperación directamente en **Consolidar**.

El paquete final reanudable queda en:

```text
leccion_final.md
leccion_final.html
img/
```

El HTML guardado es ligero: carga las imágenes desde `img/`, con rutas relativas,
sin duplicarlas en Base64. Funciona sin conexión al conservar ambos juntos.
**Descargar HTML con imágenes** sigue entregando una copia autocontenida, generada
solo para esa descarga, sin aumentar el tamaño del paquete guardado.
Las capturas nuevas, el copiloto y las imágenes publicadas usan **WebP calidad 85**.
La conversión conserva la resolución, orientación, transparencia y perfil de color;
usa compresión con pérdida para reducir considerablemente el peso. Los materiales
oficiales de pocos colores pueden usar WebP sin pérdida cuando pesa aún menos.
Para convertir una biblioteca existente y actualizar sus enlaces:

```powershell
python -m scripts.optimizar_imagenes --raiz contenido --aplicar --reporte history/imagenes.json
```

Sin `--aplicar` muestra el inventario. Acepta varias `--raiz`, incluidas carpetas
de otros cursos. Solo sustituye imágenes cuando el WebP pesa menos, actualiza
referencias de Markdown/HTML y hashes del inventario, y restaura la clase si algo
falla. El texto explicativo se conserva. No procesa capturas en curso ni vuelve a
comprimir WebP existentes. No necesita MozJPEG, jpegoptim ni herramientas externas.
Las pruebas comparativas históricas están disponibles mediante
`python -m scripts.benchmark_formatos_imagen --help`; usan copias de muestra.

Las publicaciones nuevas conservan en el Markdown un
resumen compacto del uso STT por proveedor y bloque, incluso tras retirar los WAV.
Son duraciones de solicitudes completadas, no saldo disponible ni factura.

**Continuar clase anterior** reconstruye el estado mínimo desde ese paquete. Desde Inicio
también se puede mover una clase o curso a la Papelera mediante vista previa, nombre exacto
y revalidación del fingerprint.

Si una lección ya publicada retuvo archivos por cobertura, se puede reintentar
solo la limpieza sin pagar otra consolidación:

```powershell
python -m scripts.compactar_publicada --clase contenido/<curso>/clases/<clase>
```

Muestra la vista previa; añade `--aplicar` para limpiar. Revalida commits, imágenes,
pausas, notas y antigüedad de la publicación, y guarda un recibo en `history/`.
Una recuperación histórica confirmada puede aceptarse explícitamente con
`--recuperacion-confirmada-sha256 <hash-del-Markdown>`; la excepción queda ligada
a esa publicación y no omite commits pendientes, evidencia nueva ni imágenes inválidas.
La consolidación normal mantiene todas sus verificaciones.

La auditoría offline de espacio, costos registrados y recorte de silencios se ejecuta
con `python -m scripts.auditar_eficiencia --audio-samples 40 --output history/eficiencia.json`.
No envía audios ni consulta saldos. Sin `--audio-samples`, solo inventaría archivos y
registros. Consulta [la auditoría del 16/10/2026](AUDITORIA_APP_20261016.md) para las
mediciones, cambios y comparación con proyectos similares.
El cierre de toda la biblioteca y la implementación posterior de Silero se describen
en [la entrega actual](ENTREGA_20261016.md).

## Notas del estudiante

Texto, documentos, imágenes, capturas, fotografías y voz se guardan por acción explícita.
Se pueden excluir o descartar y la lección las distingue de lo dicho por el profesor. La
voz se transcribe después de **Fin** y requiere confirmación antes de subirla al STT remoto.

## Módulos clave

- `lanzador.py`, `src/desktop_app.py`: entrada fuente/EXE, apertura única y diagnóstico offline.
- `src/app_paths.py`: separa recursos de datos y conserva identidad de workers congelados.
- `src/panel.py`: UI/API; coordina Inicio, Pausa, Fin y Consolidar.
- `src/captura_audio.py`: único propietario del stream; sella WAV canónicos.
- `src/audio_manifest.py`: orden, hashes, estado append-only y recuperación.
- `src/transcriptor_vivo.py` y `src/stt_backend.py`: cola incremental y failover remoto.
- `src/captura_pantalla.py`: evidencia visual y selección de monitor.
- `src/escriba.py`: pausa de estudio incremental cada cinco minutos útiles.
- `src/student_notes.py`: originales, procedencia, edición e inclusión de notas.
- `src/post_procesar.py`: integra transcript, visuales y notas en la lección.
- `src/provider_pricing.py`, `src/costos_api.py`: tarifas versionadas y contabilidad incremental.
- `src/study_package.py`: valida y compacta transaccionalmente.
- `src/content_management.py`: confinamiento y traslado reversible a la Papelera.

## Auditoría de la versión

`PROJECT_STATE.md` resume contratos, validaciones y riesgos abiertos. Las pruebas usan
directorios temporales fuera del repositorio. `.env`, `contenido/**`, `logs/**`,
`backups/**` y la evidencia real no pertenecen al historial Git.
