# Auditoría de búsqueda web — 27/10/2026

La configuración efectiva de `.env` utiliza `https://api.deepseek.com` para
conversación y no configura herramientas Search o Fetch. La revisión del código,
scripts y configuración no encontró llamadas a `/v1/tools/search` ni a
`/v1/tools/fetch` de DeepSeek. Las claves no se incluyen en este informe.

Antes del cambio, la casilla Web del copiloto llamaba a `web.bloque_contexto`,
que obtenía títulos, enlaces y resúmenes mediante `ddgs`. La redacción del
copiloto usaba después una petición normal `chat.completions.create`; no se
enviaban herramientas de búsqueda. Las rutas opcionales del escriba y del
postprocesado emplean `web.buscar` y `web.extraer_url`: búsqueda externa y
descarga HTTP directa de páginas, respectivamente. Ninguna era DeepSeek Search
o DeepSeek Fetch. El enriquecimiento del escriba está desactivado por defecto.

Ahora, la casilla Web y `/web` descargan un feed público de Bing mediante HTTP,
sin enviar una solicitud Search o Fetch a DeepSeek. Se limitan los datos leídos,
se filtran enlaces que no son HTTP(S) y resultados con escasas coincidencias con la
consulta; si no hay resultados, el prompt advierte que no se inventen fuentes.
La respuesta sigue usando el chat de DeepSeek y, por tanto, consume sus tokens.
El buscador externo puede bloquear peticiones, devolver resultados irrelevantes
o no encontrar fuentes; el filtro reduce la exposición a resultados ajenos pero
no garantiza relevancia. En ese caso se responde sin fuentes web verificadas.

**Solicitudes de Search de DeepSeek evitadas frente al flujo anterior: cero por
clase**, porque la aplicación ya hacía cero. Fetch de DeepSeek también era cero
y sigue en cero. No hay un número fijo de consultas del copiloto por clase ni
registros que permitan atribuir llamadas de herramientas externas a DeepSeek.
En un escenario hipotético que antes hiciera una Search facturada por consulta,
el flujo actual evitaría una por consulta, pero ese no era el comportamiento
real observado. La [documentación oficial de DeepSeek](https://api-docs.deepseek.com/guides/search/)
indica US$0,75 por 1.000 solicitudes exitosas para cada herramienta.

La prueba de integración del botón simula HTTP y chat, comprueba el destino del
fetch y que la solicitud al modelo no contenga herramientas. Las pruebas también
cubren fallos, respuestas inválidas y resultados ajenos. La suite completa pasa
con 825 pruebas, incluyendo recuperación secuencial de proveedores y Silero.
Tras el último ajuste del filtro pasaron nuevamente las cuatro pruebas focales.

El portable actualizado es `dist/ClaseViva-Portable-20261027.zip`. Su diagnóstico
offline pasó después de la compilación y sus 485 archivos se verificaron por SHA-256
contra el contenido comprimido. SHA-256 del ZIP:
`f809fabfa07cfd52d28a46a49ecf05705f1d9b01b11c7b27febb2fc05a562604`.
