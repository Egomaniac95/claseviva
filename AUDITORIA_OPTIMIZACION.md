# Revisión de consolidación y audio — 2026-10-16

> Estado posterior: la biblioteca completa, Silero activo y el portable actualizado
> están documentados en [la entrega final](ENTREGA_20261016.md). Los pendientes
> descritos aquí corresponden a la revisión inicial y ya fueron resueltos allí.

> Estado posterior: la biblioteca completa, Silero activo y el portable actualizado
> están documentados en [la entrega final](ENTREGA_20261016.md). Los pendientes
> descritos aquí corresponden a la revisión inicial y ya fueron resueltos allí.

Continuación: [auditoría de eficiencia, cuotas y proyectos comparables](AUDITORIA_APP_20261016.md).
Allí se registra la estabilización posterior de la prueba de cierre mencionada aquí.

La cadena STT, los commits por chunk, la recuperación tras apagar el equipo y la
redacción enriquecida de la lección se conservan. No se han cambiado los prompts
ni el contenido de la lección aceptada.

## Causa de la retención de archivos

La clase `CLASE_04_semana-4` tiene 542 WAV con commits válidos, siete de ellos
completados sin texto. El Markdown y sus imágenes validan por tamaño y SHA-256.
La continuidad retenía la limpieza por dos condiciones relacionadas con la pausa:

- Una adquisición iniciada después de pausar quedó abandonada al apagar el equipo.
- El registro del último silencio terminó 1 ms antes de la marca de pausa; la
  solicitud más duración difiere de esa marca en unos 16 ms. El reloj PCM lleva
  un desfase acumulado de unos ocho segundos respecto de la adquisición.

Ahora se reconoce la interrupción sin PCM únicamente si la adquisición comenzó
dentro de una pausa cerrada y su fallo se registró al reanudar, dentro de 1,25 s.
La auditoría original conserva el fallo. La tolerancia de 750 ms existente también
se aplica a los límites de finalización del silencio. No se amplía la tolerancia
general de huecos PCM ni se permiten audios pendientes o commits inválidos.

El panel deja de anunciar que la carpeta quedó limpia cuando la compactación
conservó evidencia: muestra una advertencia para revisar el motivo.

## Ahorro en transcripción

`src/audio_silence.py` recorta únicamente pausas digitales de dos segundos o más,
con señal máxima de dos pasos PCM16 y 300 ms de margen en ambos extremos.
El WAV original y su hash permanecen intactos hasta la compactación final.

El audio enviado conserva un mapa de tiempos: los segmentos remotos vuelven al
reloj original antes de la alineación visual. Cada intento registra la versión de
transformación y la duración efectivamente enviada. Un trabajo anterior sin esa
versión conserva su audio original al recuperarse; uno nuevo conserva su versión
incluso si luego se desactiva el recorte en configuración.

Este filtro no es un detector semántico de voz. No elimina música, ruido ni voz
tenue. El ahorro depende de las pausas digitales presentes y de la facturación
del proveedor. Las pruebas utilizan proveedores simulados, sin gastar cuota.

## Entorno de validación

El `.venv` existente apunta a Python 3.12, actualmente ausente en este equipo.
Las pruebas se ejecutaron con Python 3.14 y dependencias en `.venv-qa`, separado
del runtime. Esto no reconstruye ni actualiza un ejecutable portable ya distribuido.

Pasaron 328 pruebas específicas de STT, recorte, commits, recuperación, paquetes,
consolidación, costos, notas y publicación. `git diff --check` pasó. Una prueba
adicional de `test_runtime_shutdown_h12.py` exige esperas exactamente de 1/2/3 s;
falló porque recibe 0,9999986 s en el entorno actual. Ese módulo no fue modificado.

## Limpieza aplicada a la última clase

Se ejecutó `scripts.compactar_publicada` primero en vista previa y después con
`--aplicar`. Esta herramienta revalida localmente antes de limpiar y rechaza
evidencia posterior al Markdown. No regenera texto ni llama a APIs.

- 542 WAV con commits verificados; cero pendientes y cero inválidos.
- 877 imágenes publicadas conservadas y verificadas por hash.
- 3.593.840.772 bytes eliminados en 3.506 rutas auxiliares.
- Solo quedan `leccion_final.md`, `leccion_final.html` e `img/`.
- Markdown antes/después:
  `10115e8e9e3ccfe2a8dfef215bf9d082a4c544fd5789ac1abfcb40e3b12c379a`.
- Recibo: `history/cleanup-CLASE_04_semana-4-1792207462592222400.json`.

La nota del estudiante incluida ya tenía su texto y huella publicados en el
Markdown; se comprobó antes de eliminar sus originales. La continuación desde
el paquete compacto está cubierta por las pruebas de recuperación existentes.
