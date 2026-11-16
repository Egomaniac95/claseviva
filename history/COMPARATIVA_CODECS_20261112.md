# Comparación de repositorios y codecs — 12 de noviembre de 2026

> Actualización posterior: el usuario autorizó WebP 85 y se convirtió la biblioteca completa. Esta página conserva la comparación anterior. El estado final y la limpieza están en `WEBP_LIMPIEZA_PROYECTO_20261112.md`. Se retiraron variantes experimentales y se conserva solo la muestra original y WebP 85.

Sí existe margen adicional. La primera optimización priorizó conservar los JPEG y retirar la duplicación Base64 de los HTML. Esta revisión compara otras herramientas con los JPEG ya optimizados.

## Método y resultados

Muestra: 39 imágenes, tres por cada una de las 13 carpetas, seleccionadas por los percentiles 25, 50 y 90 de tamaño. Se probaron copias, manteniendo las dimensiones. La referencia suma 5.073.823 bytes después de la primera optimización con MozJPEG. Los porcentajes son ahorros adicionales sobre esa muestra; no son garantías para cualquier imagen ni para toda la biblioteca.

| Método | Peso de muestra | Cambio respecto al JPEG actual | Conservación | Tiempo mediano de codificación |
|---|---:|---:|---|---:|
| JPEG / jpegoptim | 4.991 MB | -1.64 % | Píxeles idénticos | 0.145 s |
| JPEG XL, esfuerzo 9 | 4.114 MB | -18.93 % | Reconstrucción exacta del JPEG original | 1.041 s |
| JPEG XL, esfuerzo 10 | 4.116 MB | -18.88 % | Reconstrucción exacta del JPEG original | 1.082 s |
| WebP sin pérdida | 14.786 MB | +191.41 % | Píxeles idénticos | 1.130 s |
| AVIF sin pérdida | 22.836 MB | +350.07 % | Píxeles idénticos | 2.207 s |
| WebP calidad 95 | 5.035 MB | -0.76 % | Con pérdida | 0.648 s |
| WebP calidad 85 | 2.935 MB | -42.16 % | Con pérdida | 0.513 s |
| AVIF calidad 90, 4:4:4 | 4.643 MB | -8.49 % | Con pérdida | 0.698 s |
| AVIF calidad 80, 4:4:4 | 3.461 MB | -31.79 % | Con pérdida | 0.888 s |
| AVIF calidad 70, 4:4:4 | 2.783 MB | -45.15 % | Con pérdida | 0.766 s |

Un cambio negativo significa menor tamaño. Las cifras no incluyen tiempos de lectura, cambios de metadata ni verificación posterior. Las medidas de PSNR disponibles en el JSON cuantifican diferencias; no prueban por sí solas que todo texto, fórmula o gráfico conserve la misma legibilidad. Los valores de calidad de WebP y AVIF no son equivalentes.

## Interpretación

- **jpegoptim** permite una reducción adicional pequeña manteniendo JPEG, resolución, píxeles y metadata de color. El optimizador de bibliotecas ahora puede comparar su resultado con MozJPEG y guardar solo el menor, tras verificarlo.
- **JPEG XL** es la alternativa sin pérdida más interesante para archivo: en las 39 muestras se recuperó el JPEG original byte por byte. El esfuerzo 10 no mejoró el total frente al esfuerzo 9 en este conjunto. La comparación de píxeles de esta variante se hizo sobre el JPEG reconstruido, que es el original exacto.
- **WebP y AVIF sin pérdida** aumentaron el tamaño de los JPEG existentes en las configuraciones probadas. Guardar en un formato sin pérdida los artefactos ya presentes en un JPEG puede costar más espacio que conservar sus coeficientes comprimidos. Este resultado no implica que WebP sin pérdida sea malo para capturas originales sin compresión JPEG previa.
- **WebP 85 y AVIF 70** redujeron aproximadamente un 42 % y un 45 %, respectivamente, pero modificaron píxeles. No deben describirse como matemáticamente sin pérdida. Su conveniencia visual requiere revisar texto pequeño y fórmulas a escala real.
- **jpegli** mejora la codificación de JPEG nuevos con cuantización adaptativa y otras técnicas. Revisé su código y documentación; no lo ejecuté ni usé sus resultados como mediciones de esta tabla. No sustituye la transformación reversible de un JPEG ya publicado.

## Compatibilidad comprobada

Prueba local con Microsoft Edge 157.0.4341.46, perfil temporal independiente y archivos abiertos desde disco: JPEG, PNG, WebP y AVIF cargaron correctamente; JPEG XL no cargó. Por eso no se convirtió la biblioteca de HTML y Markdown a JXL. No se extrapola esta prueba a todas las versiones de todos los navegadores.

Para conservar la lectura directa de HTML y Markdown con píxeles idénticos, la opción aplicada sigue siendo JPEG y selección entre optimizadores. JPEG XL ofrece una opción de archivo reversible, pero exigiría un visor o un proceso de reconstrucción compatible en este entorno.

## Repositorios revisados

- [mozilla/mozjpeg](https://github.com/mozilla/mozjpeg): encoder y optimización de escaneos JPEG; es el utilizado en la primera pasada.
- [tjko/jpegoptim](https://github.com/tjko/jpegoptim): optimización JPEG sin pérdida; versión probada [1.5.6](https://github.com/tjko/jpegoptim/releases/tag/v1.5.6).
- [libjxl/libjxl](https://github.com/libjxl/libjxl): JPEG XL y reconstrucción JPEG reversible; versión probada [0.13.0, publicada el 11/11/2026](https://github.com/libjxl/libjxl/releases/tag/v0.13.0).
- [webmproject/libwebp](https://github.com/webmproject/libwebp): modos WebP con y sin pérdida. Se utilizó el encoder disponible en Pillow.
- [AOMediaCodec/libavif](https://github.com/AOMediaCodec/libavif): AVIF y configuración de codificación sin pérdida; [1.4.2](https://github.com/AOMediaCodec/libavif/releases/tag/v1.4.2).
- [google/jpegli](https://github.com/google/jpegli): implementación JPEG con mejoras en cuantización y espacios de color.

## Evidencia y muestras

- `image-codec-benchmark-20261112.json`: bytes, igualdad de píxeles, PSNR y tiempos por imagen y formato.
- `image-codec-browser-20261112.json`: resultado de la prueba del navegador.
- `image-codec-tools-20261112.json`: versiones y hashes de los ejecutables probados.
- `image-codec-preview-20261112/`: una muestra original de Infraestructura y sus variantes para revisión manual. Las variantes con pérdida solo están en esa carpeta de comparación.
- Los ejecutables de investigación, archivos comprimidos descargados y el perfil de navegador temporal se retiraron después de las pruebas. Solo se conserva por separado el backend opcional de jpegoptim con su licencia.

## Reproducción

El script `scripts/benchmark_formatos_imagen.py` necesita los ejecutables oficiales `cjxl.exe`, `djxl.exe`, `avifenc.exe` y `jpegoptim.exe` en una carpeta pasada con `--tools`. No modifica las imágenes de origen.

```powershell
python -m scripts.benchmark_formatos_imagen --raiz contenido --raiz "D:\05_Maestria_UNSA\3.SEMESTRE_III\7.INFRAESTRUCTURA" --tools "D:\ruta\codecs" --output history/comparativa.json
```

## Mejora compatible aplicada a toda la biblioteca

Se verificaron de nuevo los 2.703 JPEG y se aplicó el menor resultado de MozJPEG y jpegoptim a las dos ubicaciones. Ahorro adicional real: **6.49 MB** (6,489,730 bytes). Ahorro acumulado desde el estado original: **301.46 MB** en imágenes y HTML.

| Ubicación | Ahorro adicional | Peso actual de imágenes + HTML |
|---|---:|---:|
| ClaseViva / contenido | 2.42 MB | 152.59 MB |
| Infraestructura | 4.07 MB | 242.03 MB |

Los 2.703 hashes de píxeles coinciden con la primera pasada, los hashes de los archivos escritos fueron comprobados otra vez, los 12 cuerpos de lección permanecen idénticos y los 13 HTML resuelven todas las imágenes. Pasaron 37 pruebas focalizadas tras añadir el backend opcional. El recibo por archivo está en `image-optimization-extra-20261112.json`.
