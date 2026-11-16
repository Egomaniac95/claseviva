# Silero VAD incluido en ClaseViva

Modelo oficial de [Silero VAD v6.2.3](https://github.com/snakers4/silero-vad/tree/v6.2.3),
licencia MIT adjunta. Fuente del binario:
https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/src/silero_vad/data/silero_vad.onnx

Tamaño: 2.327.524 bytes. SHA-256:
`1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`.

El runtime verifica esa huella antes de cargarlo. Inferencia ONNX en CPU, un hilo,
16 kHz, ventanas de 512 muestras y estado independiente por bloque. No descarga
modelos al arrancar ni requiere Torch, CUDA o cuentas externas.

La política conservadora propia de ClaseViva conserva probabilidades ambiguas,
recorta solo intervalos de al menos dos segundos con probabilidad inferior a 0,20
y deja 400 ms a cada lado. Sin voz detectada con confianza o ante error se utiliza
el filtro digital anterior. Los mapas y hashes del audio enviado se persisten
antes del primer envío para recuperar trabajos pendientes sin recalcular el VAD.
