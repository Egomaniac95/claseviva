"""Lanzador del día de clase (multi-curso).

    .venv\\Scripts\\python.exe iniciar_clase.py                 # flujo interactivo completo
    .venv\\Scripts\\python.exe iniciar_clase.py --reanudar      # retomar la clase activa

Flujo interactivo: elegir CURSO (o crear uno nuevo) -> tipo de sesión:
  [1] CONTINUACIÓN directa de una clase existente (misma lección, parte siguiente)
  [2] Lección NUEVA del curso (semana siguiente; hereda el historial de resúmenes)
El historial de otros cursos (memoria_cursos.md + resúmenes) se carga como contexto del copiloto.

Arranca 5 procesos: audio, pantalla, transcriptor, escriba y panel web (localhost:8756),
y deja en primer plano la consola del copiloto. Todo se coordina por session_state.json.
"""
import argparse
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import config, copiloto  # noqa: E402
from src import estado as est  # noqa: E402

MODULOS = ["src.captura_audio", "src.captura_pantalla",
           "src.transcriptor_vivo", "src.escriba", "src.panel"]
PANEL_URL = "http://localhost:8756"


def rotar_logs() -> None:
    """Logs > 5 MB se truncan al iniciar (optimización de peso)."""
    config.ensure_dirs()
    for lg in config.LOGS_DIR.glob("*.log"):
        try:
            if lg.stat().st_size > 5 * 1024 * 1024:
                lg.write_text("", encoding="utf-8")
        except OSError:
            pass


def reanudar_clase(d: Path) -> dict:
    """Retoma una clase (lógica compartida en estado.reanudar)."""
    st = est.reanudar(d)
    print(f"▶ Reanudada: parte {st['parte']} de la misma lección "
          f"(la lección final se regenera con TODAS las partes).")
    return st


def elegir_clase_existente(curso: str) -> Path | None:
    clases = sorted(p for p in (config.dir_curso(curso) / "clases").iterdir()
                    if p.is_dir())
    if not clases:
        print("  (este curso aún no tiene clases; se creará una nueva)")
        return None
    print("  Clases del curso:")
    for i, c in enumerate(clases[-10:], 1):
        print(f"    {i}: {c.name}")
    r = input(f"  ¿Cuál continúas? [1-{len(clases[-10:])}, Enter=última]: ").strip()
    ultimas = clases[-10:]
    if r.isdigit() and 1 <= int(r) <= len(ultimas):
        return ultimas[int(r) - 1]
    return ultimas[-1]


def flujo_interactivo(args) -> Path:
    # 1) Curso
    cursos = config.cursos_existentes()
    print("\n=== 🎓 CURSOS ===")
    for i, c in enumerate(cursos, 1):
        print(f"  {i}: {c}")
    print("  N: nuevo curso")
    r = input(f"¿Qué curso? [1-{max(1, len(cursos))}, N, Enter=1]: ").strip().lower()
    if r == "n" or not cursos:
        curso = input("Nombre del nuevo curso: ").strip() or config.CURSO_DEFAULT
        print("  📚 Curso nuevo: el historial de tus otros cursos (memoria_cursos.md y "
              "resúmenes) se carga automáticamente como contexto del copiloto.")
    elif r.isdigit() and 1 <= int(r) <= len(cursos):
        curso = cursos[int(r) - 1]
    else:
        curso = cursos[0]

    # 2) Tipo de sesión
    print("\n[1] CONTINUACIÓN directa de una clase existente (misma lección, parte siguiente)")
    print("[2] Lección NUEVA de este curso (semana siguiente; hereda historial)")
    t = input("Tipo de sesión [1/2, Enter=2]: ").strip()
    if t == "1":
        curso_slug = est._slug(curso)
        d = elegir_clase_existente(curso_slug)
        if d is not None:
            reanudar_clase(d)
            return d
    tema = args.nombre or input("Tema de la clase: ").strip() or "clase"
    d = est.crear_clase(tema, semana=args.semana, curso=curso)
    print(f"Clase creada: {d}")
    return d


def main() -> int:
    config.stdout_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--nombre", help='tema de la clase (si se omite, se pregunta)')
    ap.add_argument("--semana", type=int, help="número de semana (default: autoincremental)")
    ap.add_argument("--reanudar", action="store_true",
                    help="retomar la clase activa (crash/cierre accidental o parte siguiente)")
    args = ap.parse_args()

    if not config.DEEPSEEK_API_KEY:
        print("FALTA .env con DEEPSEEK_API_KEY. Corre init.ps1 primero.")
        return 1
    rotar_logs()

    if args.reanudar:
        d = est.clase_activa()
        if d is None:
            print("No hay clase activa que reanudar.")
            return 1
        st = reanudar_clase(d)
        print(f"Reanudando: {st['nombre']} (parte {st['parte']})")
    else:
        activa = est.clase_activa()
        if activa is not None and est.leer_estado(activa)["estado"] != "fin":
            print(f"OJO: hay una clase activa sin cerrar en {activa}.\n"
                  f"Usa --reanudar para retomarla, o ciérrala antes con /fin.")
            return 1
        d = flujo_interactivo(args)

    # Multi-monitor: elegir dónde está la clase (34" principal, laptop, o dual)
    try:
        from src.captura_pantalla import monitores_disponibles
        mons = monitores_disponibles()
        if len(mons) > 1:
            print("\nMonitores detectados:")
            for i, m in enumerate(mons, 1):
                print(f"  {i}: {m['width']}x{m['height']} en ({m['left']},{m['top']})")
            eleccion = input(f"¿En cuál está la CLASE? [1-{len(mons)}, "
                             f"Enter={config.MONITOR}]: ").strip()
            if eleccion.isdigit() and 1 <= int(eleccion) <= len(mons):
                est.actualizar_estado(d, monitor=int(eleccion))
                print(f"  capturando el monitor {eleccion} (cámbialo en vivo con /monitor N)")
    except Exception as e:
        print(f"(selector de monitor no disponible: {e}; usando CLASE_MONITOR={config.MONITOR})")

    # Fuente del audio: navegador (Chrome/Edge, con o sin audífonos) o todo el sistema.
    try:
        r = input("¿De dónde sale el audio de la clase? [1] Chrome  [2] Edge  "
                  "[3] Todo el sistema (Enter=3): ").strip()
        fuente = {"1": "chrome", "2": "msedge"}.get(r, "sistema")
        est.actualizar_estado(d, fuente=fuente, enfoque=False, audio_modo="")
        from src.captura_audio import dispositivos_info
        dispositivos = dispositivos_info()
        if len(dispositivos) > 1:
            print("\nDispositivo fallback WASAPI:")
            for i, dev in enumerate(dispositivos, 1):
                print(f"  {i}: {dev['etiqueta']}")
            rdev = input("¿Cuál usar si process-loopback falla? [Enter=predeterminado]: ").strip()
            if rdev.isdigit() and 1 <= int(rdev) <= len(dispositivos):
                est.actualizar_estado(d, dispositivo=dispositivos[int(rdev) - 1]["id"])
        if fuente != "sistema":
            print(f"  fuente: {fuente} · se intentará capturar SOLO ese navegador por proceso.\n"
                  f"  Si Windows no lo permite, el capturador activará enfoque como fallback.\n"
                  f"  Si REPRODUCES una grabación: /velocidad 1.5 (o 2) y pon el reproductor igual.")
    except Exception:
        pass

    config.ensure_dirs()
    procesos = []
    for mod in MODULOS:
        log = open(config.LOGS_DIR / f"{mod.split('.')[-1]}.log", "a", encoding="utf-8")
        p = subprocess.Popen([sys.executable, "-m", mod], cwd=str(ROOT),
                             stdout=log, stderr=subprocess.STDOUT)
        procesos.append((mod, p, log))
        print(f"  ▶ {mod} (pid {p.pid}, log en logs/{mod.split('.')[-1]}.log)")
    def abrir_panel_cuando_liste():
        import socket
        for _ in range(24):  # hasta ~12 s a que Flask levante
            try:
                with socket.create_connection(("127.0.0.1", 8756), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.5)
        try:
            webbrowser.open(PANEL_URL)
        except Exception:
            pass
    import threading
    threading.Thread(target=abrir_panel_cuando_liste, daemon=True).start()

    print("\n" + "=" * 62)
    print(f" 🖥️ PANEL WEB: {PANEL_URL}  (interfaz amigable con todo)")
    print(" La lección en vivo crece en:", d / "leccion_en_vivo.md")
    print(" Consola: /pausa /seguir /parte /fin /estado /pantalla /monitor")
    print("          /fuente /velocidad /enfoque /costo — o pregunta lo que sea.")
    print("=" * 62)

    try:
        copiloto.main()
    finally:
        limite = time.time() + 25
        for mod, p, log in procesos:
            restante = max(1, limite - time.time())
            try:
                p.wait(timeout=restante)
            except subprocess.TimeoutExpired:
                print(f"  ⏹ {mod} no cerró a tiempo; terminando proceso.")
                p.terminate()
            log.close()
        print("\nTodos los procesos cerrados.")
        print("Siguiente paso: .venv\\Scripts\\python.exe -m src.post_procesar --todo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
