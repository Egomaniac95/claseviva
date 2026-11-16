# Auditoría de ClaseViva — 16 de octubre de 2026

> Estado posterior: Silero ya está integrado, las diez clases están publicadas y
> purgadas, y el entorno y el portable están reparados y verificados. Véase
> [la entrega final](ENTREGA_20261016.md). Este documento conserva los hallazgos y
> mediciones de la auditoría previa a ese cierre.

> Estado posterior: Silero ya está integrado, las diez clases están publicadas y
> purgadas, y el entorno y el portable están reparados y verificados. Véase
> [la entrega final](ENTREGA_20261016.md). Este documento conserva los hallazgos y
> mediciones de la auditoría previa a ese cierre.

La estrategia actual encaja con el uso descrito: transcripción remota en cadena
aprovechando cuotas gratuitas, evidencia local recuperable y redacción enriquecida
con DeepSeek. La prioridad es prolongar esas cuotas, conservar el contenido académico
y retirar auxiliares solo después de verificar la publicación. Cambiar a una API de
pago más barata por minuto no mejora un gasto STT que actualmente es cero.

Se revisaron captura y recuperación, intentos STT, consolidación, caché LLM,
compactación, registros de consumo y diez carpetas de clase. Las verificaciones son
locales y con proveedores simulados: no se enviaron audios a APIs ni se consumió cuota.
No es una evaluación humana de exactitud de todas las transcripciones ni una auditoría
exhaustiva de seguridad de las dependencias.

## Hallazgos y correcciones aplicadas

| Prioridad | Hallazgo | Resultado |
| --- | --- | --- |
| Alta | Al reintentar un bloque aceptado por un proveedor alternativo, podía volver a probarse el principal cuando vencía su espera. | Primero se recupera el trabajo pendiente. Mientras siga pendiente no se sube el mismo audio a otro proveedor. Se conserva AssemblyAI → Deepgram → Speechmatics → Groq para nuevos trabajos. |
| Alta | Un evento antiguo de carga o consulta podía recuperarse aunque el mismo intento ya hubiera terminado en fallo. | La recuperación reduce los eventos por identificador de intento y excluye intentos fallidos. Un reinicio durante la consulta conserva el identificador del trabajo. Una respuesta asíncrona completada antes del commit local se vuelve a consultar sin subir audio. |
| Media | El agotamiento de créditos HTTP 402 podía repetirse en cada bloque. Los 429 sin cabecera esperaban poco y las fechas HTTP no se interpretaban. | 402 suspende ese proveedor al menos una hora; 429/498 sin cabecera esperan 60 s. Se interpreta `Retry-After` numérico o fecha y se mantiene la recuperación de las esperas persistidas. |
| Media | La clave de caché de consolidación no distinguía roles, límites de mensajes ni presupuesto de salida. | La identidad incluye mensajes completos, modelo, versión de prompt, endpoint y parámetros. Escritura atómica para evitar entradas parciales. Una caché antigua puede fallar una vez al migrar al contrato nuevo. |
| Media | La limpieza retiraba también el detalle de uso STT. | Las publicaciones nuevas incorporan un resumen por bloque y proveedor. Los reintentos de limpieza de publicaciones antiguas conservan ese resumen en el recibo externo, sin modificar el Markdown aceptado. |
| Media | El contador de bytes retirados no descontaba un HTML generado durante la limpieza. | Los próximos recibos incluyen tamaño posterior y ahorro neto, separados de los bytes eliminados. |

El tratamiento de HTTP 402 coincide con el error de saldo insuficiente documentado
por [Deepgram](https://developers.deepgram.com/docs/errors). La duración de espera de
una hora es una decisión local conservadora; no representa el momento de renovación
de la cuota del proveedor.

La corrección anterior de pausa/apagado y el recorte digital se documentan en
[AUDITORIA_OPTIMIZACION.md](AUDITORIA_OPTIMIZACION.md). Se mantienen los prompts,
presupuestos de redacción y modelos existentes. La caché evita repetir exactamente
una solicitud; no sustituye partes de la lección por resúmenes más cortos.

Si el proceso se interrumpe después de registrar el éxito de una API síncrona y antes
del commit local, no hay recuperación por identificador implementada: el WAV queda
pendiente para resolución explícita. No se vuelve a consumir cuota automáticamente.

## Limpieza realizada y datos conservados

Las clases IA 03 y IA 06 pasaron las verificaciones de commits, continuidad, notas,
antigüedad de publicación e imágenes. Se retiraron 273 WAV, capturas no publicadas
y archivos intermedios. Se verificaron los SHA-256 de los Markdown antes y después,
y las 412 imágenes conservadas. Los hashes de las demás lecciones tampoco cambiaron.

| Clase de Inteligencia Artificial | Tamaño anterior | Tamaño final | Ahorro neto | Imágenes conservadas |
| --- | ---: | ---: | ---: | ---: |
| `CLASE_03_clase-2` | 1.075.213.140 B | 85.020.059 B | 990.193.081 B | 234 |
| `CLASE_06_ultima-clase` | 718.345.829 B | 31.008.478 B | 687.337.351 B | 178 |
| Total | 1.793.558.969 B | 116.028.537 B | **1.677.530.432 B** | 412 |

Se borraron 1.743.780.916 bytes auxiliares y se generaron 66.250.484 bytes de HTML
autocontenido; por eso el ahorro neto es **1,68 GB decimales**, no 1,74 GB. Es adicional
a la limpieza de Big Data 04 realizada en la intervención anterior. No se regeneró
el texto de las lecciones ni se alteró su contenido académico.

Cada carpeta compactada conserva `leccion_final.md`, `leccion_final.html` e `img/`.
La duplicación de imágenes dentro del HTML permite compartirlo como un solo archivo.
El Markdown con imágenes externas mantiene la continuidad y edición del curso.

Dos clases conservan sus auxiliares por motivos concretos:

- **IA 04, `CLASE_04_clase-3`:** 1.464.824.516 B y 226 WAV; no existe lección final.
  Corresponde consolidarla antes de decidir qué evidencia retirar.
- **IA 05, `CLASE_05_clase-semana-4`:** 2.105.445.567 B y 310 WAV. Los commits están
  completos y válidos, pero dos WAV con señal útil no coinciden con texto cercano y
  la continuidad señala intervalos de 351,113 s y 12.836,666 s sin justificar bajo
  las reglas actuales. No se ha demostrado pérdida de esas horas de audio: es una
  discrepancia entre evidencia y relojes que requiere contrastar pausas y tiempos.
  No se amplió la tolerancia para forzar su eliminación.

Git, las claves, los registros de consumo, los recibos y el entorno de dependencias
se conservan. No son archivos de clase descartables. El runtime `.venv` apunta a
Python 3.12, ausente en este equipo; eliminarlo no repara el inicio desde fuente.
También se retiraron 6.183.042 B de temporales de prueba y cachés Python regenerables,
con rutas verificadas dentro del proyecto y sin enlaces a otros directorios. Recibo:
`history/audit-generated-cleanup-20261016.json`.

## Medición del ahorro y del gasto

La muestra local contiene **40 WAV distribuidos a intervalos regulares entre los
archivos disponibles antes de esta limpieza**, con 1.200 s de audio. Se aplicó el
mismo orden de decodificación, compensación de velocidad, normalización y recorte
del flujo de transcripción. No se contaron como ahorro nuevo los bloques que ya
descartaba el filtro de silencio existente.

| Métrica | Resultado |
| --- | ---: |
| Audio antes del recorte | 1.200,000 s |
| Audio después | 1.126,896 s |
| Tiempo retirado | 73,104 s |
| Reducción en esta muestra | **6,092 %** |
| Errores de procesamiento / llamadas remotas | 0 / 0 |

Esta medición demuestra menor duración enviada para esta muestra, no una mejora de
exactitud ni un ahorro monetario garantizado. El filtro retira pausas digitales de
al menos dos segundos, con márgenes de 300 ms; no distingue palabras del ruido.
Con una distribución igual, una cuota basada solo en segundos alcanzaría alrededor
de 6,5 % más de grabación original. Los límites por solicitudes y mínimos de duración
pueden reducir ese beneficio.

El registro local de DeepSeek suma **434 llamadas**, 3.032.109 tokens de entrada,
1.167.428 de salida y **US$0,920394812 estimados históricamente**. Los apuntes periódicos
(`escriba` y sus continuaciones/reintentos) representan aproximadamente 50,1 % de esa
estimación. No es una factura, saldo disponible ni proyección mensual; no se
recalcularon llamadas antiguas con tarifas nuevas.

Con esos datos no se justifica reducir el desarrollo académico para ahorrar unos
centavos. Se conserva la frecuencia actual de apuntes. Un intervalo configurable
de 8–10 minutos podría estudiarse después, a cambio de mayor demora para consultarlos;
su ahorro real dependerá de tokens y reintentos, no solo del número de llamadas.

El nuevo resumen STT distingue solicitudes completadas, fallidas y sin resolver,
duración enviada y silencio retirado cuando está medido. Solo suma duración de
solicitudes completadas conocidas; intentos fallidos también podrían consumir cuota.
Los registros antiguos sin duración original quedan expresamente como desconocidos.
No se calcula un saldo gratuito a partir de esos contadores.

## Proyectos y estrategias contrastados en línea

Las siguientes son fuentes primarias consultadas el 16/10/2026. Las conclusiones de
adopción son recomendaciones para esta app, no resultados de ejecución de esos
proyectos en esta laptop.

| Proyecto | Estrategia publicada | Aplicación a ClaseViva |
| --- | --- | --- |
| [Meetily](https://github.com/Zackriya-Solutions/meetily) | Transcripción local Whisper/Parakeet y redacción con Ollama o proveedores remotos; soporte de Windows y macOS. | Referencia para separar captura, transcripción y redacción. Migrar todo a local exige medir carga del equipo y calidad en español; no supone ahorro frente al STT gratuito actual. |
| [Anarlog, antes Hyprnote](https://github.com/fastrepl/anarlog) | Datos locales, exportación Markdown y configuración separada de transcripción e inteligencia. | Mantener portabilidad del resultado y elección de proveedores. ClaseViva ya tiene parte de estas ventajas; no hace falta sustituirla. |
| [Silero VAD](https://github.com/snakers4/silero-vad) | Detección de actividad vocal, soporte 8/16 kHz, ejecución mediante ONNX y licencia MIT. | Candidato principal para quitar pausas con ruido de fondo. Integración ONNX aislada aprovechando la decodificación existente; requiere evaluar voz tenue antes de activarse. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Integra Silero VAD; su configuración conservadora elimina silencios de más de dos segundos. | Referencia para segmentación y márgenes. No es necesario instalar su motor de transcripción para evaluar VAD. |
| [WhisperX](https://github.com/m-bain/whisperX) | Combina detección de voz, alineación temporal por palabra y diarización. | Útil si distinguir hablantes o afinar tiempos por palabra pasa a ser un requisito. Añade modelos y procesamiento que hoy no son necesarios para redactar la lección. |

Groq documenta un mínimo facturable de diez segundos por solicitud y entrada óptima
mono a 16 kHz. Por ello, una implementación VAD debería reunir los fragmentos de voz
dentro del bloque y conservar un mapa al reloj original, evitando una petición por
cada frase corta. El mínimo facturable no prueba cómo se descuenta la cuota concreta
de esta cuenta. [Documentación de audio de Groq](https://console.groq.com/docs/speech-to-text).

Las cuotas y créditos deben contrastarse con los paneles de las cuentas existentes.
Las ofertas de alta actuales de [AssemblyAI](https://www.assemblyai.com/pricing/) y
[Speechmatics](https://www.speechmatics.com/pricing) no indican el saldo del usuario;
Groq también aplica límites por segundos y solicitudes según la cuenta.
[Límites oficiales de Groq](https://console.groq.com/docs/rate-limits).

## Mejoras siguientes, por utilidad

1. **Evaluar Silero ONNX en paralelo al flujo actual, inicialmente sin recortar.**
   Medir 20–30 minutos de varias clases con español, términos técnicos en inglés,
   preguntas de estudiantes y voz baja. Comparar marcas con una referencia revisada
   manualmente. Empezar con pausas de dos segundos y márgenes de 300–500 ms, conservar
   el mapa temporal y volver al audio original ante fallo del detector. No activarlo
   por defecto mientras suprima palabras de la referencia. El ahorro adicional no
   está medido aún.
2. **Mejorar vocabulario por curso.** Aprovechar títulos y OCR legible para preparar
   términos técnicos; corregir nombres solo cuando la evidencia lo respalde y
   señalar discrepancias en cifras o fórmulas. Groq permite un prompt acotado de
   vocabulario. No añadir automáticamente servicios de enriquecimiento facturables
   de otros proveedores. [Parámetros de Groq](https://console.groq.com/docs/speech-to-text).
3. **Resolver IA 05 mediante sus relojes y commits originales.** Evitar retranscribir
   la clase completa o borrar sus WAV para resolver una inconsistencia temporal.
   El diagnóstico preservado identifica los dos archivos y dos límites de captura.
4. **Reparar el entorno de ejecución y actualizar el portable antes de distribuir.**
   Esta auditoría modifica el código fuente. El ejecutable distribuido todavía no
   incorpora estos cambios; no se reconstruyó durante esta intervención.

## Verificación y evidencia reproducible

Pasaron **319 pruebas de regresión** de cuotas, trabajos pendientes, timestamps,
silencio, manifiestos, recuperación de pausas, consolidación, notas, paquetes, caché,
costos y cierre. Se estabilizó una prueba de cierre fijando su reloj simulado: antes
comparaba 1 s exacto con una espera reducida unos microsegundos por el tiempo real
del propio test. No se cambió la lógica de cierre para acomodar esa prueba.

Las pruebas se ejecutaron en `.venv-qa` con Python 3.14; no equivalen a validar el
portable de Python 3.12 ni a una clase real completa con APIs. Los cambios nuevos de
la auditoría no añaden dependencias al runtime. `git diff --check` pasó.

- Inventario y muestra anterior a limpieza: `history/audit-efficiency-20261016.json`.
- Inventario posterior: `history/audit-efficiency-after-20261016.json`.
- Diagnóstico retenido: `history/audit-IA05-retained-20261016.json`.
- Recibo IA 03: `history/cleanup-CLASE_03_clase-2-1792208263714466900.json`.
- Recibo IA 06: `history/cleanup-CLASE_06_ultima-clase-1792208391907905000.json`.

Para repetir el inventario sin APIs:

```powershell
.\.venv-qa\Scripts\python.exe -m scripts.auditar_eficiencia --audio-samples 40 --output history/eficiencia-nueva.json
```

Una nueva muestra puede cambiar porque ya se retiraron los WAV de las clases
compactadas. La medición original queda conservada para no confundir ambas muestras.
