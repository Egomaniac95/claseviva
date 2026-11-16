"""Lanzador de la APP (doble clic vía ClaseViva.vbs / ClaseViva.bat, sin consola).

- Instancia única: si el panel ya corre en :8756, solo abre el navegador y termina.
- Arranca el panel en MODO APP (asistente de inicio en la interfaz, dueño de los
  procesos capturadores, botón Cerrar app) y abre el navegador cuando responde.
"""
from src.desktop_app import cli as main


if __name__ == "__main__":
    raise SystemExit(main())
