# Historial conservado de ClaseViva

Este proyecto es la fuente de trabajo. La entrega para otra computadora es únicamente
`../ClaseViva-Portable.zip`; las carpetas antiguas de build no son instalaciones necesarias.
Git y todos los cambios actuales se conservan, sin reset, reescritura ni borrado de commits.

## Hitos

- 2026-09-26: Spark clase 3 reproduce otro cruce de pausa con bloque silencioso aún
  en vuelo. La continuidad contextual usa solicitudes y finalización de adquisición
  para corroborarlo; no se amplían tolerancias generales de pérdidas PCM. Se añade
  HTML autocontenido por consolidación y descarga desde el visor. El copiloto coloca
  la pregunta arriba, mantiene identidad de cada respuesta y muestra su inicio;
  lectura ampliable y controles que no se superponen. Presupuesto visible de 3.200
  tokens, thinking desactivado y aviso de truncamiento, sin auto-consultas adicionales.
  Spark compactada sin cambiar su Markdown: 1,82 GB liberados, 179 imágenes intactas.
  El portable único se reconstruye y valida con 438 archivos; 770 pruebas, Ruff,
  smoke y gate offline aprobados. El 28/09 se corrigió el gate de paquete ligero
  para reconocer commits STT vacíos pero válidos; en Spark clase 3 se retiraron
  776 MB y se mantuvieron 235 imágenes, tras inventario SHA-256. Suite completa:
  770 pruebas, Ruff y smoke; sin llamadas facturables.
- Base Git de esta iteración: `9ba31d4`. El historial anterior disponible sigue en `.git`.
- 2026-09-14: Windows portable con runtime independiente, configuración de claves,
  icono generado con IA, verificación nativa/offline, correcciones del visor y precios
  estimados por modelo. 751 pruebas aprobadas. Sin STT local ni cambio de cursos.
- 2026-09-24: corrección focal de compactación tras pausa/reanudación. La adquisición
  diferida por el sondeo de pausa no se confunde con una pérdida física si existe
  evidencia temporal y stream nuevo. 760 pruebas aprobadas. Se reparó Big Data clase 2
  conservando el hash de su lección y 38 imágenes; de 2,23 GB a 5,54 MB, sin APIs.
- 2026-09-24: unificación de la distribución bajo `ClaseViva-Portable.zip`, recompilada
  con el arreglo anterior. La eliminación de builds, ZIP y auditorías externas redundantes
  quedó pendiente: la herramienta bloqueó el comando antes de ejecutarlo.
  El código, tests, dependencias, cursos, `.env` y entorno local no se eliminan.

## Checkpoints recuperables

`history/checkpoints-20260914-20260924.zip` conserva seis entradas verificadas byte a byte:
patch fuente del 14/09, checkpoint de entrega, inventario/hash del antiguo portable,
patch focal del 24/09, informe de reparación y recibo de compactación.

Tamaño: 471.112 bytes. SHA-256:
`86ecd43bfd0a1c1c52c3184aac8473359c7bf91864a9278686fa2f6f243ac82e`.

Los patches permiten reconstruir cambios sobre su base; revisarlos antes de aplicar.
No contienen claves ni audios. No son copias de seguridad de cursos. Los WAV eliminados
tras la compactación validada no se recuperan mediante un rollback del código.

Las referencias a entregas externas antiguas en `PROJECT_STATE.md` y en los checkpoints
son históricas: esos nombres ya no deben usarse para distribuir, aunque siguen en disco
porque el borrado fue bloqueado. El registro de la nueva
entrega y su inventario se mantienen en `history/` después de verificar el ZIP.
