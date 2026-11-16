# Optimización de imágenes — 12 de noviembre de 2026

> Actualización posterior del mismo día: la revisión de seis repositorios y 39 muestras permitió liberar otros 6.49 MB sin pérdida mediante jpegoptim. Las cifras de esta página documentan la primera pasada. Consulta `COMPARATIVA_CODECS_20261112.md` y `image-optimization-extra-20261112.json` para el estado final.

Se optimizaron las 2.703 imágenes de 13 carpetas `img`, en las dos ubicaciones solicitadas. Todos los JPEG conservan sus dimensiones, cada píxel, EXIF y perfiles de color ICC. No se redujo la calidad ni se cambiaron nombres o enlaces.

## Espacio ocupado por imágenes y HTML

| Ubicación | Imágenes | Antes | Después | Liberado |
|---|---:|---:|---:|---:|
| ClaseViva / contenido | 1,360 | 415.65 MB | 155.00 MB | 260.64 MB |
| Infraestructura | 1,343 | 280.43 MB | 246.11 MB | 34.32 MB |

Unidades: MB decimales (1 MB = 1.000.000 bytes). Se incluyen los cinco HTML nuevos de Infraestructura en el tamaño final. El recibo JSON almacena los bytes exactos y los hashes de cada imagen.

## Cambios aplicados

- JPEG optimizado con MozJPEG en el dominio DCT, sin volver a comprimir con pérdida. Se conservó el formato JPEG porque la conversión de muestra a WebP sin pérdida produjo archivos mayores.
- HTML con rutas relativas a `img/`, sin duplicar imágenes en Base64, con carga diferida. Se generaron cinco HTML ligeros en Infraestructura y se aligeró también la clase que solo conservaba su HTML.
- Se actualizó exclusivamente el inventario de tamaños y SHA-256 dentro del comentario de metadata de cada Markdown. Los 12 cuerpos de lección y el resto de su metadata permanecen idénticos.
- Las capturas futuras usan JPEG optimizado y progresivo con la calidad y resolución configuradas. La descarga autocontenida desde la aplicación sigue disponible y se genera en memoria, sin inflar el paquete guardado.

## Uso

Abre `leccion_final.html` normalmente. Para copiar o compartir el paquete ligero, conserva el HTML y la carpeta `img` juntos, en la misma ubicación relativa.

## Validación

- 2.703 JPEG verificados píxel a píxel durante la compresión; hashes de todos los archivos escritos comprobados de nuevo al finalizar.
- 12 cuerpos de lección idénticos a la copia de sus hashes anterior al proceso; inventarios válidos.
- 13 HTML sin imágenes Base64 ni rutas locales dependientes del servidor; todas las imágenes enlazadas existen.
- 154 pruebas aprobadas de compresión, restauración ante fallos, exportación, captura, materiales visuales y compatibilidad portable.
- Los respaldos temporales de cada clase se eliminaron después de validar; no se duplicó la biblioteca.

## Archivos por clase

| Clase | Imágenes | Imágenes antes | Imágenes después | HTML antes | HTML después |
|---|---:|---:|---:|---:|---:|
| cita-medica-angel/clases/CLASE_01_consulta-general-essalud | 6 | 1.06 MB | 0.91 MB | 1.45 MB | 0.03 MB |
| maestria-inteligencia-artificial/clases/CLASE_01_clase-1 | 251 | 36.02 MB | 31.15 MB | 48.52 MB | 0.50 MB |
| maestria-inteligencia-artificial/clases/CLASE_02_clase2 | 159 | 22.47 MB | 18.84 MB | 30.31 MB | 0.35 MB |
| maestria-inteligencia-artificial/clases/CLASE_03_clase-2 | 234 | 36.10 MB | 29.93 MB | 48.56 MB | 0.43 MB |
| maestria-inteligencia-artificial/clases/CLASE_04_clase-3 | 70 | 8.75 MB | 7.80 MB | 11.90 MB | 0.23 MB |
| maestria-inteligencia-artificial/clases/CLASE_05_clase-semana-4 | 123 | 17.91 MB | 15.98 MB | 24.25 MB | 0.38 MB |
| maestria-inteligencia-artificial/clases/CLASE_06_ultima-clase | 178 | 13.03 MB | 11.08 MB | 17.69 MB | 0.32 MB |
| visualizacion-de-datos/clases/CLASE_01_semana-2 | 339 | 41.52 MB | 36.34 MB | 56.10 MB | 0.74 MB |
| SEMANA_1/CLASE_APUNTE | 148 | 15.39 MB | 12.35 MB | 0.00 MB | 0.37 MB |
| SEMANA_2_HADOOP_MAP_REDUCE/0.APUNTES_CLASE | 38 | 5.34 MB | 4.55 MB | 0.00 MB | 0.22 MB |
| SEMANA_3_SPARK/0.APUNTES | 235 | 40.23 MB | 34.50 MB | 0.00 MB | 0.61 MB |
| SEMANA_4/0_APUNTES_TEORIA | 877 | 214.39 MB | 187.57 MB | 0.00 MB | 1.30 MB |
| SEMANA_5_FLINK/0_APUNTE_TEORIA | 45 | 5.07 MB | 4.39 MB | 0.00 MB | 0.25 MB |

## Repetir en otras bibliotecas

```powershell
python -m scripts.optimizar_imagenes --raiz contenido --raiz "D:\ruta\otro-curso" --aplicar --reporte history/imagenes.json
```

Sin `--aplicar` se muestra el inventario. La herramienta conserva los originales si una operación falla.

Referencias técnicas: [MozJPEG Lossless Optimization](https://github.com/wanadev/mozjpeg-lossless-optimization) y [libjpeg-turbo: transformaciones JPEG sin pérdida](https://github.com/libjpeg-turbo/libjpeg-turbo/blob/main/doc/usage.txt).
