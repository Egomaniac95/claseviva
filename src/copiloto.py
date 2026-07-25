"""EL COPILOTO: consola interactiva durante la clase.

- Escribe cualquier pregunta -> DeepSeek responde usando: resúmenes de clases ANTERIORES
  + notas de HOY + transcripción de los últimos 10 minutos. Todo queda en qa.md.
- /pantalla [pregunta]  -> captura la pantalla AHORA, la pasa por OCR y pregunta sobre ella
  (ideal para "explícame este código").
- Comandos de control: /pausa /seguir /parte /fin /estado /costo /ayuda
"""
import re
import sys
import time
from pathlib import Path

from . import audio_sesiones
from . import config
from . import costos_api
from . import estado as est
from . import ocr
from .deepseek_client import preguntar

AYUDA = """
Comandos:
  /pausa            pausa captura de audio+pantalla+notas (receso)
  /seguir           reanuda tras una pausa
  /parte [nombre]   cierra la parte actual y abre la siguiente (ej: /parte Fin de teoría)
  /fin              termina la clase (cierra todos los procesos)
  /estado           muestra estado, parte, auto-pausa, monitor, fuente y velocidad
  /monitor [N]      lista los monitores / cambia a cuál se captura (ej: /monitor 2)
  /fuente X         de dónde sale el audio: chrome | edge | sistema
  /dispositivo [N]  lista/cambia el loopback WASAPI usado como fallback
  /velocidad N      viendo una GRABACIÓN acelerada: 1.5 o 2 (y pon el player igual); 1 = normal
  /enfoque on|off   silencia TODAS las demás apps (solo se oye/graba el navegador)
  /costo            gasto API de esta sesión y de su curso
  /pantalla [preg]  captura la pantalla actual + OCR y pregunta sobre ella
  /ayuda            esta ayuda
Cualquier otro texto = pregunta al copiloto sobre la clase (en vivo).
"""


def contexto_previo(d: Path) -> str:
    """Memoria: resúmenes del CURSO de esta clase + historial ligero de otros cursos."""
    piezas = []
    cmd = est.curso_md(d)
    if cmd.is_file():
        piezas.append("## 📒 Bitácora del curso hasta hoy (incluye notas del alumno)\n"
                      + cmd.read_text(encoding="utf-8")[-6000:])
    if config.MEMORIA_CURSOS.is_file():
        piezas.append("## 🧠 Historial de otros cursos (conceptos ya desarrollados)\n"
                      + config.MEMORIA_CURSOS.read_text(encoding="utf-8")[-4000:])
    for p in sorted(est.resumenes_de(d).glob("*.md")):
        piezas.append(f"## {p.stem}\n{p.read_text(encoding='utf-8')}")
    junto = "\n\n".join(piezas)
    return junto[-20000:] if junto else "(aún no hay clases anteriores registradas)"


def transcript_reciente(d: Path, minutos: int = 45, max_chars: int = 50000) -> str:
    corte = time.time() - minutos * 60
    segs = [s for s in est.leer_jsonl(d / "transcript.jsonl") if s["t"] >= corte]
    return " ".join(s["texto"] for s in segs)[-max_chars:]


def notas_hoy(d: Path) -> str:
    p = d / "leccion_en_vivo.md"
    return p.read_text(encoding="utf-8")[-12000:] if p.is_file() else ""


def responder(d: Path, pregunta: str, sistema_extra: str = "", extra_usuario: str = "",
              usar_web: bool = False) -> str:
    if usar_web or pregunta.lower().startswith("/web "):
        from . import web
        consulta = pregunta.removeprefix("/web ").removeprefix("/WEB ").strip()[:120]
        extra_usuario = web.bloque_contexto(consulta) + extra_usuario
        pregunta = consulta or pregunta
    sistema = (
        "Eres un copiloto de estudio EN VIVO para un estudiante universitario/de posgrado. "
        "El profesor está dictando AHORA (por Meet/Zoom); el curso puede ser de CUALQUIER "
        "materia (IA, medicina, etc.). REGLA PRINCIPAL: SIEMPRE responde la pregunta con "
        "detalle y utilidad, PASE LO QUE PASE. El estudiante a menudo pregunta por algo que "
        "el profesor AÚN NO ha dicho, o que anticipa, o que conecta con la clase: eso es "
        "válido y esperado.\n"
        "- Si la TRANSCRIPCIÓN o NOTAS cubren el tema, básate en ellas y cítalas.\n"
        "- Si NO lo cubren (todavía), responde IGUAL con tu conocimiento experto del tema, y "
        "avisa en UNA línea breve al inicio: '📚 (aún no lo dijo el profesor; te respondo por "
        "contexto)'. NUNCA te limites a decir que no aparece en la transcripción: eso no "
        "ayuda. Da la explicación completa igual.\n"
        "- La curiosidad del estudiante siempre se recompensa: aunque la pregunta parezca "
        "externa a la clase, busca una conexión plausible con el momento, tema, ejemplo o "
        "duda cognitiva que pudo dispararla. Si no encuentras conexión real, dilo en una "
        "línea y responde de todos modos con una explicación completa, práctica y útil.\n"
        "- Formato: respuesta directa en 2-4 líneas y luego el detalle. Español. Si hay "
        "RESULTADOS WEB, intégralos citando URLs. Para datos anatómicos/clínicos o técnicos "
        "concretos, sé preciso; no inventes cifras que no sepas.\n\n"
        f"=== RESÚMENES DE CLASES ANTERIORES ===\n{contexto_previo(d)}\n" + sistema_extra
    )
    usuario = (f"=== NOTAS DE HOY (últimas) ===\n{notas_hoy(d)}\n\n"
               f"=== TRANSCRIPCIÓN CANÓNICA RECIENTE (45 min) ===\n{transcript_reciente(d)}\n\n"
               + extra_usuario +
               f"=== PREGUNTA ===\n{pregunta}")
    r = preguntar([{"role": "system", "content": sistema},
                   {"role": "user", "content": usuario}],
                  max_tokens=1800, etiqueta="copiloto")
    return r if r is not None else "⚠️ La API no respondió (3 intentos). Reintenta en un momento."


def registrar_qa(d: Path, pregunta: str, respuesta: str) -> None:
    with open(d / "qa.md", "a", encoding="utf-8") as f:
        f.write(f"\n### ❓ [{config.hhmm(time.time())}] {pregunta}\n\n{respuesta}\n")


def capturar_para_pregunta(d: Path) -> str:
    """Captura la pantalla AHORA y solo guarda el JPEG. No hace OCR ni llama a DeepSeek."""
    from .captura_pantalla import capturar_ahora, guardar_liviana  # import tardío

    st = est.leer_estado(d)
    img = capturar_ahora(monitor=st.get("monitor"), region=st.get("region"))
    epoch = int(time.time() * 1000)
    nombre = f"consulta_{epoch:013d}.jpg"
    guardar_liviana(img, d / "img" / nombre)
    return nombre


def descartar_captura_previa(d: Path, nombre: str) -> None:
    """Elimina solo un preview efímero que aún no fue publicado en ``qa.md``."""
    raw = str(nombre or "").strip()
    safe_name = Path(raw).name
    if raw != safe_name or not re.fullmatch(r"consulta_\d{13}\.jpg", safe_name):
        raise ValueError("nombre de captura previa inválido")
    qa = d / "qa.md"
    if qa.is_file() and safe_name in qa.read_text(encoding="utf-8"):
        raise RuntimeError("la captura ya está referenciada y no puede descartarse")
    target = d / "img" / safe_name
    if not target.is_file():
        raise FileNotFoundError("captura previa no encontrada")
    target.unlink()


def responder_con_captura(d: Path, pregunta: str, nombre: str, usar_web: bool = False) -> str:
    """OCR + DeepSeek sobre una captura ya congelada por el usuario."""
    nombre = Path(nombre).name
    ruta = d / "img" / nombre
    if not ruta.is_file():
        return "⚠️ No encuentro la captura adjunta. Toma otra captura e intenta de nuevo."
    texto = ocr.texto_de_imagen(d / "img" / nombre)
    if not pregunta:
        pregunta = "Explica lo que se ve en pantalla (si hay código, explica su lógica paso a paso)."
    extra = f"=== OCR DE LA PANTALLA ACTUAL ===\n{texto[:6000] or '(OCR no disponible)'}\n\n"
    r = responder(d, pregunta, extra_usuario=extra, usar_web=usar_web)
    registrar_qa(d, f"[pantalla] {pregunta}", f"![captura](img/{nombre})\n\n{r}")
    return r


def pantalla_con_captura(d: Path, pregunta: str) -> tuple[str, str]:
    """Compatibilidad consola: captura ahora y la procesa inmediatamente."""
    nombre = capturar_para_pregunta(d)
    return responder_con_captura(d, pregunta, nombre), nombre


def cmd_pantalla(d: Path, pregunta: str) -> str:
    return pantalla_con_captura(d, pregunta)[0]


def main() -> int:
    config.stdout_utf8()
    d = est.clase_activa()
    if d is None:
        print("[copiloto] no hay clase activa. Usa iniciar_clase.py")
        return 1
    st = est.leer_estado(d)
    print(f"\n🎓 clase-viva · {st['nombre']} · parte {st['parte']} · escribe /ayuda para comandos")

    while True:
        try:
            linea = input("\n🎓> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n(usa /fin para terminar la clase, o /pausa para un receso)")
            continue
        if not linea:
            continue
        st = est.leer_estado(d)

        if linea == "/ayuda":
            print(AYUDA)
        elif linea == "/estado":
            n_seg = len(est.leer_jsonl(d / "transcript.jsonl"))
            n_img = len(est.leer_jsonl(d / "img_log.jsonl"))
            auto = " 🔇AUTO-PAUSA(silencio)" if st.get("auto_pausa") else ""
            mon = st.get("monitor") or f"{config.MONITOR} (default)"
            print(f"estado={st['estado']}{auto} parte={st['parte']} monitor={mon} "
                  f"fuente={st.get('fuente', 'sistema')} velocidad={st.get('velocidad', 1.0)}x "
                  f"enfoque={'ON' if st.get('enfoque') else 'off'} "
                  f"segmentos={n_seg} capturas={n_img} carpeta={d}")
        elif linea.startswith("/fuente"):
            arg = linea[len("/fuente"):].strip().lower().replace("edge", "msedge") \
                .replace("msmsedge", "msedge")
            if arg in ("chrome", "msedge", "sistema"):
                est.actualizar_estado(d, fuente=arg)
                extra = ""
                if arg != "sistema" and audio_sesiones.disponible():
                    extra = " (la auto-pausa ahora se decide por el navegador; /enfoque on disponible)"
                print(f"✅ fuente de audio: {arg}{extra}")
            else:
                print("usa: /fuente chrome | edge | sistema")
        elif linea.startswith("/dispositivo"):
            from .captura_audio import dispositivos_info
            arg = linea[len("/dispositivo"):].strip()
            opciones = dispositivos_info()
            if arg:
                ids = {o["id"]: o for o in opciones}
                if arg in ids:
                    est.actualizar_estado(d, dispositivo=arg)
                    print(f"✅ dispositivo fallback: {ids[arg]['etiqueta']}")
                else:
                    print("id no encontrado. Usa /dispositivo para listar.")
            else:
                actual = st.get("dispositivo", "")
                for o in opciones:
                    marca = " ← actual" if o["id"] == actual else ""
                    print(f"  {o['id'] or '(default)'}: {o['etiqueta']}{marca}")
                print("usa: /dispositivo ID")
        elif linea.startswith("/velocidad"):
            arg = linea[len("/velocidad"):].strip().lower().rstrip("x")
            try:
                v = float(arg)
                assert v in (1.0, 1.25, 1.5, 1.75, 2.0)
                est.actualizar_estado(d, velocidad=v)
                if v > 1:
                    print(f"✅ velocidad {v}x — pon el reproductor del navegador TAMBIÉN a {v}x. "
                          f"La transcripción compensa la aceleración automáticamente.")
                else:
                    print("✅ velocidad normal (1x) — modo clase en vivo.")
            except (ValueError, AssertionError):
                print("usa: /velocidad 1 | 1.25 | 1.5 | 1.75 | 2")
        elif linea.startswith("/enfoque"):
            arg = linea[len("/enfoque"):].strip().lower()
            fuente = st.get("fuente", "sistema")
            if fuente == "sistema":
                print("primero elige el navegador: /fuente chrome (o edge)")
            elif arg in ("on", "off"):
                n = audio_sesiones.enfoque(fuente, arg == "on")
                est.actualizar_estado(d, enfoque=(arg == "on"))
                if arg == "on":
                    print(f"🎯 enfoque ON: {n} apps silenciadas — solo se oye/graba {fuente}.")
                else:
                    print(f"🔊 enfoque OFF: {n} apps restauradas.")
            else:
                print("usa: /enfoque on | off")
        elif linea.startswith("/monitor"):
            from .captura_pantalla import monitores_disponibles  # import tardío
            mons = monitores_disponibles()
            arg = linea[len("/monitor"):].strip()
            if arg.isdigit() and 1 <= int(arg) <= len(mons):
                est.actualizar_estado(d, monitor=int(arg))
                print(f"✅ capturando ahora el monitor {arg}. Verifica con: /pantalla ¿qué se ve?")
            else:
                actual = st.get("monitor") or config.MONITOR
                for i, m in enumerate(mons, 1):
                    marca_m = " ← actual" if i == actual else ""
                    print(f"  {i}: {m['width']}x{m['height']} en ({m['left']},{m['top']}){marca_m}")
                print("usa: /monitor N")
        elif linea == "/costo":
            gastos = costos_api.resumen_curso(
                est.curso_de(d), ahora=time.time(), clase_actual=d)
            print(
                "gasto API en esta sesión: "
                f"${gastos['clase_actual_usd']:.4f} USD"
            )
            print(
                "gasto API en este curso hasta ahora: "
                f"${gastos['total_usd']:.4f} USD"
            )
        elif linea == "/pausa":
            resultado = est.transicionar_captura(d, "pausa")
            print(resultado["msg"] + " /seguir para reanudar.")
        elif linea == "/seguir":
            resultado = est.transicionar_captura(d, "seguir")
            print(resultado["msg"])
        elif linea.startswith("/parte"):
            nota = linea[len("/parte"):].strip()
            est.marcar(d, "fin_parte", nota=nota)
            st = est.mutar_estado(
                d,
                lambda actual: dict(
                    actual, parte=int(actual.get("parte", 1)) + 1),
            )
            print(f"✅ Parte {st['parte'] - 1} cerrada{f' ({nota})' if nota else ''}. "
                  f"Ahora estás en la parte {st['parte']}.\n"
                  f"   Puedes consolidarla ya (en OTRA terminal):\n"
                  f"   .venv\\Scripts\\python.exe -m src.post_procesar --parte {st['parte'] - 1}")
        elif linea == "/fin":
            if st.get("enfoque"):  # restaurar el audio de las demás apps antes de salir
                audio_sesiones.enfoque(st.get("fuente", "sistema"), False)
                print("🔊 enfoque OFF: audio de las demás apps restaurado.")
            est.transicionar_captura(d, "fin")
            print("🏁 Clase terminada. Los procesos se cerrarán solos.\n"
                  "   Consolida todo con: .venv\\Scripts\\python.exe -m src.post_procesar --todo")
            return 0
        elif linea.startswith("/pantalla"):
            print("📸 capturando y analizando...")
            print("\n" + cmd_pantalla(d, linea[len("/pantalla"):].strip()))
        elif linea.startswith("/"):
            print(f"comando desconocido: {linea} (usa /ayuda)")
        else:
            print("🤖 pensando...")
            r = responder(d, linea)
            print("\n" + r)
            registrar_qa(d, linea, r)


if __name__ == "__main__":
    sys.exit(main())
