# ClaseViva para Windows

Edición: **2026.10.27**.
Incluye Silero VAD local en CPU, recuperación del mapa temporal enviado, cuotas STT
secuenciales, control de adquisición frente a Pausa/Fin y exportación HTML completa.
La casilla Web consulta un feed público mediante HTTP y no usa Search ni Fetch de
DeepSeek. Si no obtiene fuentes pertinentes, avisa; el chat continúa consumiendo
tokens de DeepSeek. La disponibilidad de la búsqueda externa puede variar.

## Abrir y configurar

1. Descomprime **toda** la carpeta `ClaseViva` en una ubicación escribible, por ejemplo
   Documentos. No ejecutes desde dentro del ZIP ni copies solo el EXE.
2. Abre `Configurar claves.cmd`: completa la clave DeepSeek y las claves STT que tengas.
   El archivo `.env` queda junto a `ClaseViva.exe`; nunca compartas una copia con tus claves.
3. Doble clic en `ClaseViva.exe`. Se abre `http://localhost:8756` en tu navegador habitual.
   No se requiere Python, CUDA, Whisper local ni administrador. Hay que tener Internet
   para las APIs y saldo/créditos/cuota suficientes; las cuotas gratuitas no se garantizan.
4. Elige curso, clase, pantalla y fuente de audio. Autoriza el envío remoto y pulsa Inicio.
   Al terminar usa Fin, consolida y cierra desde **Cerrar app**. Cerrar solo la pestaña
   no detiene la grabación ni el servidor.

STT secuencial: AssemblyAI → Deepgram → Speechmatics → Groq. Se omiten claves faltantes;
un proveedor sin cuota puede fallar y dar paso al siguiente. Los WAV pendientes nunca se
eliminan por un fallo de red. Los resúmenes usan captura útil, no hora del día. No hay
transcript en vivo ni descarga/inferencia de modelos Whisper.

Silero VAD y su licencia se incluyen en `_internal/assets/silero_vad/`.
Detecta pausas antes de subir audio, con márgenes conservadores y recuperación del
reloj original. No consume cuota ni necesita descargar modelos al abrir la app.
Si el detector falla, continúa con el filtro digital de silencio. Su detección es
probabilística; conserva intervalos ambiguos. `CLASE_STT_VAD=digital` selecciona
solo el filtro anterior para trabajos nuevos.

Las lecciones, notas y memoria quedan en esta misma carpeta. La compactación solo elimina
evidencia cuando pasa los gates de integridad. Para mover el programa con tus cursos,
cierra la app y copia la carpeta completa. Para compartir el programa, usa el ZIP de
distribución limpio, no tu copia con `.env`, `contenido`, `logs` o datos personales.
Conserva `THIRD_PARTY`: contiene versiones y avisos de licencia de las dependencias.

Cada consolidación genera `leccion_final.html` con las imágenes incrustadas: puedes
copiar solo ese archivo para estudiar sin conexión. También puedes descargarlo desde
el visor de la lección. Conserva `leccion_final.md` e `img/` dentro de la aplicación:
son el paquete reanudable necesario para continuar una clase. HTML es una exportación,
no un reemplazo de ese estado. El HTML ocupa más que las imágenes originales por base64.

## Compatibilidad y límites

Objetivo: Windows 10/11 **x64**, navegador actualizado y captura WASAPI. La captura
exclusiva por proceso depende de la versión de Windows/controlador; el panel muestra
si está usando mezcla del sistema. En esa ruta pueden grabarse notificaciones ajenas.
El audio de tu micrófono solo se incorpora mediante los controles explícitos de la app.

PDF e imágenes oficiales funcionan sin Office; PPTX requiere PowerPoint Desktop o su
exportación previa a PDF. OCR nativo usa idiomas instalados de Windows y conserva
fallback OCR CPU incluido. No se promete compatibilidad con Windows ARM/32 bits.

El ejecutable no está firmado: SmartScreen o Control de aplicaciones puede bloquear
un binario nuevo. No desactives protecciones para instalarlo. Una entrega pública requiere
firma de código/reputación del distribuidor y prueba en equipos limpios.

Si el puerto está ocupado por otra copia, ciérrala desde su panel. Si no puede escribir,
mueve la carpeta fuera de Archivos de programa. Tras editar claves, reinicia la app.
El arranque no imprime claves; `logs/lanzador.log` registra fallos de inicio sin sus valores.

## Reconstruir desde código (solo ingeniería)

En Windows x64 con Python 3.12 y el código fuente:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows.ps1 -BuildPython C:\ruta\python.exe
```

El script crea un entorno de compilación independiente, instala versiones fijadas en
`requirements-windows.lock` y `requirements-build.txt`, genera un directorio portable y
ejecuta diagnóstico offline con claves vacías, PATH sin Python y datos temporales.
No modifica el `.venv` del usuario ni incorpora `.env` o cursos. La salida predeterminada
es `../ClaseViva-Windows`; si ya existe se exige otra ruta para evitar sobrescribir datos.
`bundle-verification.json` conserva hashes y resultado. El entorno temporal se elimina
automáticamente cuando la compilación y el diagnóstico terminan correctamente.
Añade `-KeepWork` para conservarlo al depurar; los fallos también lo conservan para
diagnóstico. Estos entornos no pertenecen al ZIP.

La validación local no sustituye pruebas en otro Windows limpio: doble apertura, pantalla/
audio reales, pausa, Fin, cierre y recuperación de pendientes sin duplicar commits.
Una distribución comercial/pública requiere además revisar las obligaciones de las
dependencias; los avisos incluidos no sustituyen esa revisión.
Linux queda fuera del alcance del producto.
