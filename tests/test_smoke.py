"""Suite de humo: SIN red, SIN GPU, SIN audio. Verifica importaciones y lógica pura.

Ejecutar:  .venv\\Scripts\\python.exe tests\\test_smoke.py   (exit 0 = OK)
"""
import importlib
import contextlib
import io
import os
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FALLOS = []


def check(nombre: str, cond: bool, detalle: str = "") -> None:
    if cond:
        print(f"  [OK] {nombre}")
    else:
        print(f"  [FALLO] {nombre} {detalle}")
        FALLOS.append(nombre)


def main() -> int:
    print("== test_smoke: importaciones ==")
    for mod in ["src.config", "src.estado", "src.imagen", "src.ocr",
                "src.deepseek_client", "src.escriba", "src.copiloto",
                "src.captura_audio", "src.captura_pantalla", "src.audio_sesiones",
                "src.transcriptor_vivo", "src.post_procesar", "src.compat_av",
                "src.loopback_proceso", "src.visual_idle", "src.panel", "src.auditar_contenido",
                "src.compactar_clase", "src.study_package"]:
        try:
            importlib.import_module(mod)
            check(f"import {mod}", True)
        except Exception as e:
            check(f"import {mod}", False, f"-> {type(e).__name__}: {e}")

    from src import config, estado, imagen, captura_audio, escriba, visual_idle
    from src.escriba import construir_bloque, extraer_links
    from src.ocr import parece_codigo
    from src import (
        audio_manifest,
        auditar_contenido,
        compactar_clase,
        post_procesar,
        study_package,
    )

    # Aislar todos los destinos mutables antes de ejecutar cualquier prueba.
    smoke_sandbox = tempfile.TemporaryDirectory(prefix="clase-viva-smoke-")
    smoke_root = Path(smoke_sandbox.name)
    config.CONTENIDO_DIR = smoke_root / "contenido"
    config.CLASES_DIR = config.CONTENIDO_DIR / config.CURSO_DEFAULT / "clases"
    config.RESUMENES_DIR = config.CONTENIDO_DIR / config.CURSO_DEFAULT / "resumenes"
    config.TAREAS_MD = config.CONTENIDO_DIR / config.CURSO_DEFAULT / "TAREAS.md"
    config.MEMORIA_CURSOS = smoke_root / "memoria_cursos.md"
    config.LOGS_DIR = smoke_root / "logs"
    config.PUNTERO_CLASE = smoke_root / "clase_actual.txt"
    config.LATIDO = config.LOGS_DIR / "panel_latido.txt"

    print("== test_smoke: máquina de estados (en carpeta temporal) ==")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        config.CONTENIDO_DIR = tmp / "contenido"  # aislar del proyecto real (multi-curso)
        config.PUNTERO_CLASE = tmp / "clase_actual.txt"
        d = estado.crear_clase("Prueba de Humo ÁÉÍ", curso="curso-test")
        check("curso derivado de la ruta", estado.curso_de(d) == "curso-test")
        check("crear_clase crea estructura", (d / "audio").is_dir() and (d / "img").is_dir())
        check("carpeta CLASE_NN_tema (formato corto)", d.name.startswith("CLASE_01_"),
              f"-> {d.name}")
        check("estado trae region/apuntes_auto",
              "region" in estado.leer_estado(d) and "apuntes_auto" in estado.leer_estado(d))
        st = estado.leer_estado(d)
        check("estado inicial grabando/parte1", st["estado"] == "grabando" and st["parte"] == 1)
        check("apuntes automaticos por defecto cada 5 min", st.get("apuntes_auto") == 5,
              f"-> {st.get('apuntes_auto')}")
        check("estado trae campos audio nuevos", "dispositivo" in st and "audio_modo" in st)
        check("estado trae diagnostico visual idle",
              "auto_pausa_motivo" in st and "visual_idle" in st)
        diag_mute = captura_audio.diagnostico_silencio_proceso(
            "chrome", "chrome:proceso(pid=1234)", rms=0.0, pico_sesion=0.0,
            chunks_silencio=max(2, config.SILENCIO_CHUNKS),
            chunks_umbral=config.SILENCIO_CHUNKS,
            silencio_rms=config.SILENCIO_RMS)
        check("diagnostico chrome muteado advierte",
              "Chrome parece silenciado o sin audio" in diag_mute["warning"]
              and "No se detecta audio de Chrome" in diag_mute["detalle"])
        diag_temprano = captura_audio.diagnostico_silencio_proceso(
            "chrome", "chrome:proceso(pid=1234)", rms=0.0, pico_sesion=0.0,
            chunks_silencio=1,
            chunks_umbral=2,
            silencio_rms=config.SILENCIO_RMS)
        check("diagnostico no advierte en el primer chunk silencioso",
              diag_temprano["warning"] == "")
        diag_otro = captura_audio.diagnostico_silencio_proceso(
            "chrome", "chrome:proceso(pid=1234)", rms=0.0, pico_sesion=0.2,
            chunks_silencio=max(2, config.SILENCIO_CHUNKS),
            chunks_umbral=config.SILENCIO_CHUNKS,
            silencio_rms=config.SILENCIO_RMS)
        check("diagnostico no advierte si sesion navegador tiene pico",
              diag_otro["warning"] == "")
        from PIL import Image as PILImage, ImageDraw
        img_oscura = PILImage.new("RGB", (320, 180), (4, 4, 6))
        diag_visual = visual_idle.analizar_imagen(img_oscura)
        check("visual-idle: pantalla oscura acelera auto-pausa",
              diag_visual["idle"]
              and visual_idle.umbral_silencio(4, True) <= config.VISUAL_IDLE_CHUNKS,
              f"-> {diag_visual}")
        img_slide = PILImage.new("RGB", (320, 180), (8, 8, 12))
        draw = ImageDraw.Draw(img_slide)
        for y in range(25, 150, 20):
            draw.rectangle((20, y, 280, y + 5), fill=(235, 235, 240))
        diag_slide = visual_idle.analizar_imagen(img_slide)
        check("visual-idle: slide oscura con contenido no se descarta",
              not diag_slide["idle"], f"-> {diag_slide}")
        check("clase_activa apunta bien", estado.clase_activa() == d)
        check("almacenando=True al inicio", estado.almacenando(st))
        st2 = estado.actualizar_estado(d, auto_pausa=True)
        check("auto_pausa detiene almacenamiento", not estado.almacenando(st2))
        estado.actualizar_estado(d, auto_pausa=False)
        estado.cambiar_estado(d, "pausado")
        check("cambiar_estado", estado.leer_estado(d)["estado"] == "pausado")
        time.sleep(0.05)
        estado.marcar(d, "fin_parte", nota="teoria")
        st["parte"] = 2
        estado.escribir_estado(d, st)
        time.sleep(0.05)
        estado.marcar(d, "fin")
        rangos = estado.rangos_partes(d)
        check("rangos_partes = 2 partes", len(rangos) == 2, f"-> {rangos}")
        check("rangos contiguos", rangos[0][2] == rangos[1][1])
        estado.anexar_jsonl(d / "x.jsonl", {"a": 1})
        check("jsonl ida y vuelta", estado.leer_jsonl(d / "x.jsonl") == [{"a": 1}])
        from src import panel
        responder_orig = panel.copiloto.responder
        registrar_orig = panel.copiloto.registrar_qa
        try:
            panel.copiloto.responder = lambda _d, q, **kw: f"respuesta a {q}"
            panel.copiloto.registrar_qa = lambda _d, q, r: None
            resp = panel.app.test_client().post(
                "/api/pregunta", json={"q": "directa", "captura": None})
            data = resp.get_json()
            check("panel pregunta directa acepta captura null",
                  resp.status_code == 200 and data.get("ok") is True
                  and data.get("respuesta") == "respuesta a directa",
                  f"-> status={resp.status_code}, data={data}")
            estado.actualizar_estado(
                d, estado="grabando", parte=2, apuntes_auto=5, resumir=False,
                resumen_estado="", resumen_msg="")
            resp_res = panel.app.test_client().post(
                "/api/comando", json={"cmd": "resumir"})
            data_res = resp_res.get_json()
            st_res = estado.leer_estado(d)
            check("boton resumir marca solicitud con auto fijo",
                  resp_res.status_code == 200 and data_res.get("ok") is True
                  and data_res.get("msg") == "Resumen solicitado"
                  and st_res.get("resumir") is True
                  and st_res.get("apuntes_auto") == 5
                  and st_res.get("resumen_estado") == "pendiente",
                  f"-> status={resp_res.status_code}, data={data_res}, st={st_res}")
            est_res = panel.app.test_client().get("/api/estado").get_json()
            check("api estado muestra resumen generando con auto fijo",
                  est_res.get("resumen_estado") == "pendiente"
                  and "generando resumen" in est_res.get("resumen_msg", "")
                  and est_res.get("apuntes_auto") == 5,
                  f"-> {est_res}")
            (d / "leccion_en_vivo.md").write_text(
                "# Vivo\n\n---\n\n## ⏱️ 00:00-00:02 · Parte 2\n\n"
                "### Resumen manual visible\n\nContenido del tramo.\n",
                encoding="utf-8")
            estado.actualizar_estado(d, resumir=False, resumen_estado="listo",
                                     resumen_msg="Resumen generado.")
            est_res2 = panel.app.test_client().get("/api/estado").get_json()
            check("resumen renderiza sin cambiar auto fijo",
                  "Resumen manual visible" in est_res2.get("leccion_html", "")
                  and est_res2.get("apuntes_auto") == 5,
                  f"-> {est_res2}")
            estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
                "t_ini": 10, "t_fin": 20, "parte": 2, "rango": "00:10-00:20",
                "cuerpo": "### Apuntes del tramo\nTema acumulado uno con definicion importante.",
                "imagenes": [],
            })
            estado.anexar_jsonl(d / "resumen_tramos.jsonl", {
                "t_ini": 20, "t_fin": 30, "parte": 2, "rango": "00:20-00:30",
                "cuerpo": "### Apuntes del tramo\nULTIMO_DETALLE_MARKER explicado con mas detalle.",
                "imagenes": [],
            })
            estado.anexar_jsonl(d / "transcript.jsonl", {
                "t": 31, "fin": 32, "parte": 2,
                "texto": "PENDIENTE_MARKER transcript posterior al ultimo resumen " * 8,
            })
            estado.actualizar_estado(d, stt_transcribed_until_wall=32)
            vista_viva = panel.app.test_client().get("/api/estado").get_json()
            check("leccion viva muestra ficha actual y estado pendiente simple",
                  "Pausa de estudio" in vista_viva.get("leccion_html", "")
                  and "ULTIMO_DETALLE_MARKER" in vista_viva.get("leccion_html", "")
                  and "Tramo en curso" in vista_viva.get("leccion_html", "")
                  and "PENDIENTE_MARKER" not in vista_viva.get("leccion_html", ""),
                  f"-> {vista_viva.get('leccion_html', '')[:600]}")
            estado.cambiar_estado(d, "pausado")
            estado.actualizar_estado(d, resumir=False, resumen_estado="", resumen_msg="")
            resp_res_pausa = panel.app.test_client().post(
                "/api/comando", json={"cmd": "resumir"})
            data_res_pausa = resp_res_pausa.get_json()
            check("boton resumir funciona en pausa",
                  resp_res_pausa.status_code == 200 and data_res_pausa.get("ok") is True
                  and estado.leer_estado(d).get("resumir") is True,
                  f"-> status={resp_res_pausa.status_code}, data={data_res_pausa}")
            estado.actualizar_estado(d, estado="grabando", resumir=False,
                                     resumen_estado="", resumen_msg="")
            estado.actualizar_estado(
                d, fuente="chrome", audio_modo="chrome:proceso(pid=1234)",
                audio_warning=diag_mute["warning"], audio_diag=diag_mute["detalle"])
            est_resp = panel.app.test_client().get("/api/estado")
            est_data = est_resp.get_json()
            check("api estado expone warning de chrome muteado",
                  est_resp.status_code == 200
                  and "Chrome parece silenciado" in est_data.get("audio_warning", "")
                  and "No se detecta audio de Chrome" in est_data.get("audio_hint", "")
                  and "aviso audio" in est_data.get("trans_diag", ""),
                  f"-> status={est_resp.status_code}, data={est_data}")
            estado.actualizar_estado(
                d, resumir=False, resumen_estado="parcial",
                resumen_msg="Resumen parcial; transcript conservado")
            parcial_data = panel.app.test_client().get("/api/estado").get_json()
            check("api estado expone resumen parcial sin presentarlo como generado",
                  parcial_data.get("resumen_estado") == "parcial"
                  and "parcial" in parcial_data.get("resumen_msg", "").lower()
                  and parcial_data.get("resumen_msg") != "Resumen generado.",
                  f"-> {parcial_data}")
            page_safe = panel.app.test_client().get("/").get_data(as_text=True)
            check("UI mantiene consolidación sin prometer limpieza destructiva",
                  "Consolidar lección final" in page_safe
                  and "Consolidar seguro (sin limpiar)" not in page_safe)
            check("UI copiloto usa textarea autoresizable",
                  '<textarea id="preg"' in page_safe
                  and "autoresizePregunta" in page_safe
                  and "shiftKey" in page_safe,
                  "textarea/autoresize no encontrado")
            check("UI no expone selector de intervalo de resumen",
                  'id="sela"' not in page_safe and "manual</option>" not in page_safe)
            check("UI mantiene dos columnas hasta ancho normal",
                  "@media(max-width:1180px)" in page_safe
                  and "@media(max-width:1760px)" not in page_safe)
            estado.cambiar_estado(d, "fin")
            consolidar_orig = post_procesar.consolidar_todo
            llamadas_consolidar = []
            panel.POST_LOG.clear()
            try:
                def fake_consolidar(d_arg, forzar=False, limpiar_al_final=False,
                                    modo_estudio=False, paquete_ligero=False):
                    llamadas_consolidar.append({
                        "d": d_arg, "forzar": forzar,
                        "limpiar_al_final": limpiar_al_final,
                        "modo_estudio": modo_estudio,
                        "paquete_ligero": paquete_ligero,
                    })
                    print("POST_FAKE_OK paquete ligero")

                post_procesar.consolidar_todo = fake_consolidar
                resp_cons = panel.app.test_client().post(
                    "/api/consolidar",
                    json={"confirmar_sin_evidencia": True},
                )
                limite = time.time() + 2
                while not llamadas_consolidar and time.time() < limite:
                    time.sleep(0.02)
                data_cons = resp_cons.get_json()
                check("endpoint consolidar UI crea paquete ligero por defecto",
                      resp_cons.status_code == 200 and data_cons.get("ok") is True
                      and llamadas_consolidar
                      and llamadas_consolidar[0]["limpiar_al_final"] is True
                      and llamadas_consolidar[0]["paquete_ligero"] is True
                      and any("paquete ligero" in ln.lower() for ln in panel.POST_LOG),
                      f"-> status={resp_cons.status_code}, data={data_cons}, "
                      f"llamadas={llamadas_consolidar}, log={panel.POST_LOG}")
            finally:
                post_procesar.consolidar_todo = consolidar_orig
                panel._POST_CORRIENDO = False
                panel._POST_ERROR = ""
                estado.actualizar_estado(d, estado="grabando", resumen_estado="",
                                         resumen_msg="")

            def snapshot_contenido_real():
                root = config.ROOT / "contenido"
                if not root.is_dir():
                    return []
                return sorted((p.name, p.is_dir(), p.stat().st_mtime_ns)
                              for p in root.iterdir())

            real_antes = snapshot_contenido_real()
            contenido_orig = config.CONTENIDO_DIR
            puntero_orig = config.PUNTERO_CLASE
            modo_orig = panel.MODO_APP
            mon_orig = panel._MONITORES
            disp_orig = panel._DISPOSITIVOS
            lanzar_orig = panel._lanzar_hijos
            env_orig = os.environ.get("CLASE_PANEL_MODO")
            stt_key_orig = os.environ.get("ASSEMBLYAI_API_KEY")
            try:
                os.environ["CLASE_PANEL_MODO"] = "app"
                # Preflight real, cliente ficticio y cero red: el smoke prueba
                # consentimiento/configuración sin consumir cuota.
                os.environ["ASSEMBLYAI_API_KEY"] = "fixture-smoke-no-network"
                panel.MODO_APP = os.environ.get("CLASE_PANEL_MODO") == "app"
                config.CONTENIDO_DIR = tmp / "contenido_vacio_panel"
                config.PUNTERO_CLASE = tmp / "clase_actual_vacia_panel.txt"
                panel._MONITORES = [{"idx": 1, "etiqueta": "Pantalla test"}]
                panel._DISPOSITIVOS = [{"id": "default", "etiqueta": "Audio test"}]
                info_resp = panel.app.test_client().get("/api/inicio_info")
                info = info_resp.get_json()
                check("panel app inicio_info funciona con contenido vacio",
                      info_resp.status_code == 200
                      and config.CURSO_DEFAULT in info.get("cursos", [])
                      and info.get("clases", {}).get(config.CURSO_DEFAULT) == []
                      and info.get("monitores") and info.get("dispositivos"),
                      f"-> status={info_resp.status_code}, data={info}")
                page_resp = panel.app.test_client().get("/")
                page = page_resp.get_data(as_text=True)
                check("panel app pagina contiene Inicio de clase completo",
                      page_resp.status_code == 200
                      and "Inicio de clase" in page
                      and 'id="icurso"' in page
                      and 'id="itema"' in page
                      and 'id="imon"' in page
                      and 'id="ifuente"' in page
                      and "Empezar" in page,
                      f"-> status={page_resp.status_code}")
                panel._lanzar_hijos = lambda: None
                iniciar_resp = panel.app.test_client().post(
                    "/api/iniciar",
                    json={"curso": config.CURSO_DEFAULT, "tipo": "nueva",
                          "tema": "Prueba UI", "monitor": "1",
                          "fuente": "chrome", "dispositivo": "default",
                          "stt_backend": "remote",
                          "stt_remote_consent": True})
                iniciar = iniciar_resp.get_json()
                creada = list((config.CONTENIDO_DIR / config.CURSO_DEFAULT / "clases")
                              .glob("CLASE_01_prueba-ui*"))
                check("panel app crea clase nueva con contenido vacio en temporal",
                      iniciar_resp.status_code == 200
                      and iniciar.get("ok") is True
                      and bool(creada)
                      and (creada[0] / "session_state.json").is_file(),
                      f"-> status={iniciar_resp.status_code}, data={iniciar}, creada={creada}")
                config.PUNTERO_CLASE = tmp / "clase_actual_vacia_panel_visor.txt"
                os.environ.pop("CLASE_PANEL_MODO", None)
                panel.MODO_APP = os.environ.get("CLASE_PANEL_MODO") == "app"
                visor_resp = panel.app.test_client().post(
                    "/api/iniciar", json={"tema": "no debe crear"})
                visor = visor_resp.get_json()
                check("panel visor rechaza iniciar con mensaje claro",
                      visor_resp.status_code == 200
                      and visor.get("ok") is False
                      and "Panel en modo visor" in visor.get("msg", "")
                      and "CLASE_PANEL_MODO=app" in visor.get("msg", ""),
                      f"-> status={visor_resp.status_code}, data={visor}")
                estado_visor = panel.app.test_client().get("/api/estado").get_json()
                check("panel visor expone aviso en estado",
                      estado_visor.get("estado") == "sin clase"
                      and estado_visor.get("modo_panel") == "visor"
                      and "Panel en modo visor" in estado_visor.get("modo_msg", ""),
                      f"-> {estado_visor}")
                check("panel pagina incluye aviso visible de modo visor",
                      "viewerNotice" in page
                      and "Panel en modo visor" in page
                      and "ClaseViva.vbs/ClaseViva.bat" in page)
            finally:
                if env_orig is None:
                    os.environ.pop("CLASE_PANEL_MODO", None)
                else:
                    os.environ["CLASE_PANEL_MODO"] = env_orig
                if stt_key_orig is None:
                    os.environ.pop("ASSEMBLYAI_API_KEY", None)
                else:
                    os.environ["ASSEMBLYAI_API_KEY"] = stt_key_orig
                config.CONTENIDO_DIR = contenido_orig
                config.PUNTERO_CLASE = puntero_orig
                panel.MODO_APP = modo_orig
                panel._MONITORES = mon_orig
                panel._DISPOSITIVOS = disp_orig
                panel._lanzar_hijos = lanzar_orig
            real_despues = snapshot_contenido_real()
            check("tests de inicio no tocan contenido real",
                  real_antes == real_despues,
                  f"-> antes={real_antes}, despues={real_despues}")
        finally:
            panel.copiloto.responder = responder_orig
            panel.copiloto.registrar_qa = registrar_orig

    print("== test_smoke: post-proceso v7 (lógica pura) ==")
    pub = post_procesar.clasificar_publicidad_probable(
        [{"t": 0, "fin": 95, "texto": "Compra ahora con descuento, suscríbete al canal e instala la app desde el link promocional."}],
        [{"cambio_visual": "no_relacionado"}],
        contexto_antes="La clase venía explicando gradiente descendente.",
        contexto_despues="La clase retoma gradiente descendente y función de pérdida.",
    )
    check("publicidad corta comercial se marca",
          pub["tipo"] == "publicidad_probable", f"-> {pub}")
    ventas = post_procesar.clasificar_publicidad_probable(
        [{"t": 0, "fin": 120, "texto": "La profesora invitada comenta ventas empresariales, pricing y modelo de negocio del proyecto final."}],
        [], contexto_antes="proyecto de IA", contexto_despues="proyecto de IA")
    check("intervención humana de ventas se conserva",
          ventas["tipo"] != "publicidad_probable", f"-> {ventas}")
    varias_voces = post_procesar.clasificar_publicidad_probable(
        [{"t": 0, "fin": 240, "texto": "El profesor explica el método y luego la profesora agrega un caso clínico con resultados."}],
        [], contexto_antes="", contexto_despues="")
    check("varias voces/profesores se conserva",
          varias_voces["tipo"] == "contenido_clase", f"-> {varias_voces}")
    check("eliminar Otras pantallas",
          "Otras pantallas" not in post_procesar.eliminar_otras_pantallas(
              "## Tema\ntexto\n\n#### 🖼️ Otras pantallas del tramo\n|x|\n|---|\n|img|\n\n## Sigue\nok"))

    tema_comercial = post_procesar.clasificar_publicidad_probable(
        [{"t": 0, "fin": 900, "texto": "La clase desarrolla estrategia comercial, ventas B2B, pricing, embudos y modelo de negocio como tema principal del curso."}],
        [], contexto_antes="inicio de la clase", contexto_despues="continuamos con estrategia comercial")
    check("tema comercial real de clase se conserva",
          tema_comercial["tipo"] == "contenido_clase", f"-> {tema_comercial}")
    web_md = post_procesar.fuentes_ampliadas_md(
        "mock",
        buscar_fn=lambda _q, _n: [{"titulo": "Fuente mock", "url": "https://example.edu/a",
                                   "resumen": "Resumen breve del buscador."}],
        extraer_fn=lambda _url: {"ok": True, "titulo": "Paper mock", "fuente": "example.edu",
                                 "ideas": ["Idea clave mock offline"],
                                 "complementa": "Contrasta la clase.",
                                 "url": "https://example.edu/a"})
    check("fuentes ampliadas sin red",
          "Fuentes ampliadas" in web_md and "Idea clave mock offline" in web_md
          and "https://example.edu/a" in web_md)
    web_fallo = post_procesar.fuentes_ampliadas_md(
        "mock",
        buscar_fn=lambda _q, _n: [{"titulo": "Fuente mock sin extraer", "url": "https://example.edu/b",
                                   "resumen": ""}],
        extraer_fn=lambda _url: {"ok": False, "error": "Timeout"})
    check("fuente web fallida deja nota",
          "no se pudo extraer contenido" in web_fallo.lower(), f"-> {web_fallo}")
    english_text = ("The machine learning lecture explains training data, loss function, "
                    "gradient descent, neural network models, features, accuracy, prediction "
                    "and test set evaluation. ") * 3
    check("idioma inglés por texto sin metadata",
          post_procesar.idioma_material([{"texto": english_text}]) == "en")
    mixed_text = ("The modelo de datos y la clase explican loss con ejemplos en español. ") * 3
    check("idioma dudoso no activa inglés automático",
          post_procesar.idioma_material([{"texto": mixed_text}]) != "en")

    from PIL import Image as PILImage

    def fijar_rangos_clase(d, base=1000.0, cortes=(90.0,), fin=180.0):
        st = estado.leer_estado(d)
        st.update({"inicio": base, "estado": "fin", "parte": len(cortes) + 1})
        estado.escribir_estado(d, st)
        (d / "marcas.jsonl").write_text("", encoding="utf-8")
        estado.anexar_jsonl(d / "marcas.jsonl",
                            {"t": base, "tipo": "inicio", "parte": 1, "nota": st["nombre"]})
        for i, corte in enumerate(cortes, 1):
            estado.anexar_jsonl(d / "marcas.jsonl",
                                {"t": base + corte, "tipo": "fin_parte", "parte": i,
                                 "nota": f"parte {i}"})
        estado.anexar_jsonl(d / "marcas.jsonl",
                            {"t": base + fin, "tipo": "fin", "parte": len(cortes) + 1,
                             "nota": "fin"})
        return base

    def guardar_img(d, nombre, color, *, pedagogica=False):
        imagen_fixture = PILImage.new("RGB", (240, 135), color)
        if pedagogica:
            # Una slide real tiene estructura espacial. Un rectángulo plano es un
            # fixture de idle/UI y el selector moderno debe rechazarlo.
            from PIL import ImageDraw
            draw = ImageDraw.Draw(imagen_fixture)
            draw.rectangle((18, 16, 222, 118), outline="navy", width=4)
            draw.line((34, 42, 198, 42), fill="navy", width=5)
            draw.line((34, 68, 174, 68), fill="navy", width=4)
            draw.line((34, 92, 210, 92), fill="navy", width=4)
        imagen_fixture.save(d / "img" / nombre)

    with tempfile.TemporaryDirectory() as tmp2:
        tmp2 = Path(tmp2)
        config.CONTENIDO_DIR = tmp2 / "contenido"
        config.PUNTERO_CLASE = tmp2 / "clase_actual.txt"
        d2 = estado.crear_clase("Post v7", curso="curso-v7")
        paths = post_procesar.rutas_curso(d2)
        check("estructura contenido/<curso>/clases",
              d2.parent == paths["clases_dir"] and paths["resumenes_dir"].name == "resumenes",
              f"-> d={d2}, paths={paths}")
        PILImage.new("RGB", (10, 10), "white").save(d2 / "img" / "usada.jpg")
        PILImage.new("RGB", (10, 10), "black").save(d2 / "img" / "sobra.jpg")
        (d2 / "leccion_final.md").write_text("# Final\n\n<img src=\"img/usada.jpg\" width=\"420\">\n",
                                             encoding="utf-8")
        (d2 / "transcript.jsonl").write_text("{\"t\":1,\"texto\":\"x\"}\n", encoding="utf-8")
        (d2 / "leccion_en_vivo.md").write_text("borrador", encoding="utf-8")
        (d2 / "img_log.jsonl").write_text("{\"t\":1,\"archivo\":\"usada.jpg\"}\n", encoding="utf-8")
        post_procesar.aplicar_modo_estudio(d2)
        zips = list((d2 / "_debug").glob("*.zip"))
        check("modo-estudio conserva leccion_final", (d2 / "leccion_final.md").is_file())
        check("modo-estudio conserva imagen referenciada", (d2 / "img" / "usada.jpg").is_file())
        check("modo-estudio limpia imagen no referenciada", not (d2 / "img" / "sobra.jpg").exists())
        check("modo-estudio archiva transcript", not (d2 / "transcript.jsonl").exists()
              and bool(zips))
        if zips:
            with zipfile.ZipFile(zips[0]) as z:
                names = set(z.namelist())
            check("debug zip contiene auxiliares esperados",
                  {"transcript.jsonl", "leccion_en_vivo.md", "img_log.jsonl",
                   "session_state.json"}.issubset(names),
                  f"-> {sorted(names)}")
        post_procesar.limpiar_debug(d2)
        check("limpiar-debug borra _debug solo al invocarse", not (d2 / "_debug").exists())

    with tempfile.TemporaryDirectory() as tmp3:
        tmp3 = Path(tmp3)
        config.CONTENIDO_DIR = tmp3 / "contenido"
        config.PUNTERO_CLASE = tmp3 / "clase_actual.txt"
        d3 = estado.crear_clase("Post cobertura", curso="curso-cobertura")
        base = fijar_rangos_clase(d3, base=2000.0, cortes=(90.0,), fin=190.0)
        guardar_img(d3, "cap_p1.jpg", "white", pedagogica=True)
        guardar_img(d3, "cap_sobra.jpg", "gray", pedagogica=True)
        guardar_img(d3, "cap_p2.jpg", "black", pedagogica=True)
        for obj in [
            {"t": base + 10, "fin": base + 15, "parte": 1,
             "texto": "P1_TRANSCRIPT_MARKER explica gradiente descendente y funcion de perdida."},
            {"t": base + 40, "fin": base + 45, "parte": 1,
             "texto": "P1_TRANSCRIPT_EXTRA conecta la diapositiva con el ejemplo numerico."},
            {"t": base + 110, "fin": base + 116, "parte": 2,
             "texto": "P2_TRANSCRIPT_MARKER desarrolla evaluacion, overfitting y tarea pendiente."},
            {"t": base + 150, "fin": base + 156, "parte": 2,
             "texto": "P2_TRANSCRIPT_EXTRA incluye TAREA_P2 y referencias a datasets."},
        ]:
            estado.anexar_jsonl(d3 / "transcript.jsonl", obj)
        for obj in [
            {"t": base + 20, "archivo": "cap_p1.jpg", "dhash": "0000000000000000",
             "ocr": "gradiente descendente funcion de perdida w = 1",
             "relacion": "clase"},
            {"t": base + 50, "archivo": "cap_sobra.jpg", "dhash": "00ff00ff00ff00ff",
             "ocr": "ejemplo numerico gradiente descendente w = 2",
             "relacion": "clase"},
            {"t": base + 130, "archivo": "cap_p2.jpg", "dhash": "ffffffffffffffff",
             "ocr": "evaluacion overfitting tarea dataset w = 3",
             "relacion": "clase"},
        ]:
            estado.anexar_jsonl(d3 / "img_log.jsonl", obj)

        llamadas = []
        preguntar_orig = post_procesar.preguntar
        fuentes_orig = post_procesar.fuentes_ampliadas_md

        def fake_preguntar(msgs, max_tokens=0, etiqueta="", **_kwargs):
            user_msg = msgs[-1]["content"] if msgs else ""
            llamadas.append((etiqueta, user_msg))
            if etiqueta.startswith("post-chunk-p1-"):
                return ("## Fundamentos cubiertos\nP1_TRANSCRIPT_MARKER queda desarrollado.\n"
                        "[[IMG:cap_p1.jpg|Diagrama inicial con relacion directa al transcript.]]\n"
                        "![descartada](img/cap_sobra.jpg)\n"
                        "[[IMG:inventada.jpg|Marcador inventado que debe descartarse.]]\n"
                        "#### Otras pantallas del tramo\nruido\nBUSCAR: gradient descent")
            if etiqueta.startswith("post-chunk-p2-"):
                return ("## Evaluación del modelo\nP2_TRANSCRIPT_MARKER queda desarrollado y TAREA_P2 aparece.\n"
                        "[[IMG:cap_p2.jpg|Captura asociada a evaluacion y tarea.]]\n"
                        "BUSCAR: model evaluation")
            if etiqueta == "post-merge-parte1":
                return ("## Fundamentos cubiertos\nP1_TRANSCRIPT_MARKER queda desarrollado.\n"
                        '<img src="img/cap_p1.jpg" width="420">')
            if etiqueta == "post-merge-parte2":
                return ("## Evaluación del modelo\nP2_TRANSCRIPT_MARKER queda desarrollado y TAREA_P2 aparece.\n"
                        '<img src="img/cap_p2.jpg" width="420">')
            if etiqueta == "post-resumen":
                return "Resumen con P1_TRANSCRIPT_MARKER y P2_TRANSCRIPT_MARKER."
            if etiqueta == "post-tareas":
                return "- [ ] TAREA_P2 -- revisar dataset y entregar notebook"
            return "OK"

        try:
            post_procesar.preguntar = fake_preguntar
            post_procesar.fuentes_ampliadas_md = (
                lambda _q: "\n#### Fuentes ampliadas\n- **Mock** (example.edu)\n"
                           "  - Ideas clave:\n    - MOCK_WEB_IDEA\n"
                           "  - Cómo complementa/corrige la clase: contraste offline\n"
                           "  - URL: <https://example.edu/mock>\n"
            )
            post_procesar.consolidar_todo(
                d3,
                forzar=True,
                limpiar_al_final=True,
                paquete_ligero=True,
            )
        finally:
            post_procesar.preguntar = preguntar_orig
            post_procesar.fuentes_ampliadas_md = fuentes_orig

        final3 = (d3 / "leccion_final.md").read_text(encoding="utf-8")
        p1_prompt = next((p for e, p in llamadas if e.startswith("post-chunk-p1-")), "")
        p2_prompt = next((p for e, p in llamadas if e.startswith("post-chunk-p2-")), "")
        check("prompt incluye transcript de parte 1",
              "P1_TRANSCRIPT_MARKER" in p1_prompt and "cap_p1.jpg" in p1_prompt)
        check("prompt incluye transcript de parte 2",
              "P2_TRANSCRIPT_MARKER" in p2_prompt and "cap_p2.jpg" in p2_prompt)
        check("leccion final integra rangos por tema",
              final3.count("Cobertura útil integrada:") == 1
              and not re.search(r"(?m)^# Parte \d+", final3)
              and "Fundamentos cubiertos" in final3
              and "Evaluación del modelo" in final3)
        check("leccion final ubica bloques de grabacion al inicio",
              "## ⏱️ Bloques de grabación" in final3
              and "| Parte 1 |" in final3 and "| Parte 2 |" in final3
              and final3.index("## ⏱️ Bloques de grabación")
              < final3.index("Resumen ejecutivo"))
        check("consolidacion de app deja paquete ligero reanudable",
              sorted(path.name for path in d3.iterdir())
              == ["img", "leccion_final.md"]
              and study_package.parse_metadata(final3) is not None,
              f"-> {sorted(path.name for path in d3.iterdir())}")
        check("leccion final conserva transcript de cada parte",
              "P1_TRANSCRIPT_MARKER" in final3 and "P2_TRANSCRIPT_MARKER" in final3)
        check("capturas relevantes quedan referenciadas",
              "img/cap_p1.jpg" in final3 and "img/cap_p2.jpg" in final3)
        check("slide seleccionada omitida por el LLM se conserva deterministicamente",
              (d3 / "img" / "cap_sobra.jpg").exists()
              and "img/cap_sobra.jpg" in final3)
        check("imagen inventada se descarta", "inventada.jpg" not in final3)
        check("Otras pantallas no sobrevive en final", "Otras pantallas" not in final3)
        check("tareas mockeadas se anexan", "TAREA_P2" in final3
              and "TAREA_P2" in (post_procesar.rutas_curso(d3)["tareas_md"]).read_text(encoding="utf-8"))
        check("fuentes ampliadas mockeadas aparecen", "MOCK_WEB_IDEA" in final3)

    with tempfile.TemporaryDirectory() as tmp4:
        tmp4 = Path(tmp4)
        config.CONTENIDO_DIR = tmp4 / "contenido"
        config.PUNTERO_CLASE = tmp4 / "clase_actual.txt"
        d4 = estado.crear_clase("Post fallback", curso="curso-fallback")
        base = fijar_rangos_clase(d4, base=3000.0, cortes=(), fin=120.0)
        for i in range(18):
            estado.anexar_jsonl(d4 / "transcript.jsonl",
                                {"t": base + i * 5 + 1, "fin": base + i * 5 + 3,
                                 "parte": 1,
                                 "texto": f"FALLBACK_RAW_MARKER_{i:02d} detalle tecnico " * 8})
        preguntar_orig = post_procesar.preguntar

        def fake_vacio(_msgs, max_tokens=0, etiqueta="", **_kwargs):
            if etiqueta.startswith("post-chunk") or etiqueta.startswith("post-merge") \
                    or etiqueta == "post-resumen":
                return "   "
            if etiqueta == "post-tareas":
                return "SIN_TAREAS"
            return ""

        try:
            post_procesar.preguntar = fake_vacio
            post_procesar.consolidar_todo(d4, forzar=True, limpiar_al_final=True)
        finally:
            post_procesar.preguntar = preguntar_orig
        final4 = (d4 / "leccion_final.md").read_text(encoding="utf-8")
        check("respuesta vacía preserva transcript crudo",
              "Desarrollo del tramo" in final4 and "FALLBACK_RAW_MARKER_00" in final4)
        check("leccion final no queda demasiado corta con transcript suficiente",
              len(final4) > 900, f"-> len={len(final4)}")

    with tempfile.TemporaryDirectory() as tmp5:
        tmp5 = Path(tmp5)
        config.CONTENIDO_DIR = tmp5 / "contenido"
        config.PUNTERO_CLASE = tmp5 / "clase_actual.txt"
        d5 = estado.crear_clase("English post", curso="curso-english")
        base = fijar_rangos_clase(d5, base=4000.0, cortes=(), fin=120.0)
        guardar_img(d5, "cap_en.jpg", "white")
        estado.anexar_jsonl(d5 / "img_log.jsonl",
                            {"t": base + 20, "archivo": "cap_en.jpg",
                             "dhash": "0000000000000000", "relacion": "clase"})
        for i in range(5):
            estado.anexar_jsonl(d5 / "transcript.jsonl",
                                {"t": base + i * 10 + 1, "fin": base + i * 10 + 5,
                                 "parte": 1, "texto": english_text})
        preguntar_orig = post_procesar.preguntar
        fuentes_orig = post_procesar.fuentes_ampliadas_md

        def fake_english(_msgs, max_tokens=0, etiqueta="", **_kwargs):
            if etiqueta.startswith("post-chunk-p1-"):
                return ("## Training section\nENGLISH_PART_MARKER explains model evaluation.\n"
                        "[[IMG:cap_en.jpg|Training curve tied to the English transcript.]]\n"
                        "BUSCAR: neural network loss")
            if etiqueta == "post-merge-parte1":
                return ("## Training section\nENGLISH_PART_MARKER explains model evaluation.\n"
                        '<img src="img/cap_en.jpg" width="420">')
            if etiqueta == "post-resumen":
                return "English class summary."
            if etiqueta == "post-tareas":
                return "SIN_TAREAS"
            if etiqueta.startswith("post-eng"):
                return "## English final\nENGLISH_VERSION_MARKER\n<img src=\"img/cap_en.jpg\" width=\"420\">"
            return "OK"

        try:
            post_procesar.preguntar = fake_english
            post_procesar.fuentes_ampliadas_md = lambda _q: ""
            post_procesar.consolidar_todo(
                d5, forzar=True, limpiar_al_final=True, generar_ingles=True)
        finally:
            post_procesar.preguntar = preguntar_orig
            post_procesar.fuentes_ampliadas_md = fuentes_orig
        eng = d5 / "leccion_final_eng.md"
        eng_txt = eng.read_text(encoding="utf-8") if eng.is_file() else ""
        check("leccion_final_eng se crea por inglés detectado", eng.is_file())
        check("leccion_final_eng conserva referencias img",
              "img/cap_en.jpg" in eng_txt and (d5 / "img" / "cap_en.jpg").is_file())
        check("leccion_final_eng no duplica carpeta img",
              not any(p.is_dir() and p.name != "img" and "img" in p.name.lower()
                      for p in d5.iterdir()))

    print("== test_smoke: post-proceso v7.2 (imagenes/cobertura) ==")

    def guardar_wav_test(path: Path, amp: int = 6000, seconds: float = 1.0, rate: int = 8000):
        import struct
        import wave
        n = int(rate * seconds)
        data = struct.pack(f"<{n}h", *([amp, -amp] * (n // 2)))
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(data)

    with tempfile.TemporaryDirectory() as tmp_wav:
        tmp_wav = Path(tmp_wav)
        config.CONTENIDO_DIR = tmp_wav / "contenido"
        config.PUNTERO_CLASE = tmp_wav / "clase_actual.txt"
        dwav = estado.crear_clase("Wav limpio", curso="curso-wav")
        wav_name = "chunk_000000050000_p1_v10.wav"
        guardar_wav_test(dwav / "audio" / wav_name)
        (dwav / "leccion_final.md").write_text("# Final\n", encoding="utf-8")
        estado.anexar_jsonl(dwav / "transcript.jsonl", {
            "t": 50_001, "fin": 50_002, "parte": 1,
            "chunk": wav_name, "texto": "Audio ya transcrito.",
        })
        post_procesar.limpiar(dwav, cobertura={
            "limpieza_segura": False,
            "wav_total": 1,
            "wav_sin_transcript_total": 0,
            "transcript_wav_faltante_total": 0,
            "wav_util_sin_transcript": [],
            "periodos_con_capturas_sin_audio": [],
        })
        check("limpieza borra WAV si todos los chunks tienen transcript",
              not (dwav / "audio").exists(), f"-> {list((dwav).iterdir())}")

    print("== test_smoke: contenido y ciclo de vida v8 ==")
    with tempfile.TemporaryDirectory() as tmp_life:
        tmp_life = Path(tmp_life)
        contenido_orig = config.CONTENIDO_DIR
        puntero_orig = config.PUNTERO_CLASE
        config.CONTENIDO_DIR = tmp_life / "contenido"
        config.PUNTERO_CLASE = tmp_life / "clase_actual.txt"
        (config.CONTENIDO_DIR / "clases" / "CLASE_OLD").mkdir(parents=True)
        (config.CONTENIDO_DIR / "resumenes").mkdir(parents=True)
        (config.CONTENIDO_DIR / "resumenes" / "old.md").write_text("old", encoding="utf-8")
        (config.CONTENIDO_DIR / "curso-real" / "clases").mkdir(parents=True)
        (config.CONTENIDO_DIR / "curso-real" / "resumenes").mkdir(parents=True)
        (config.CONTENIDO_DIR / "_debug").mkdir()
        cursos = config.cursos_existentes()
        rep_cont = auditar_contenido.auditar_contenido(
            config.CONTENIDO_DIR, tmp_life / "backups")
        plan_mig = auditar_contenido.migrar_legacy(
            config.CONTENIDO_DIR, tmp_life / "backups", aplicar=False,
            timestamp="20260707_010203")
        check("contenido: legacy no aparece como curso valido",
              "curso-real" in cursos and "clases" not in cursos
              and "resumenes" not in cursos and "_debug" not in cursos,
              f"-> cursos={cursos}")
        check("contenido: auditoria detecta carpetas root legacy",
              set(rep_cont["carpetas_legacy"]) == {"clases", "resumenes"}
              and rep_cont["clases_fuera_de_curso"] == ["CLASE_OLD"],
              f"-> {rep_cont}")
        check("contenido: migracion dry-run no mueve nada",
              (config.CONTENIDO_DIR / "clases").is_dir()
              and (config.CONTENIDO_DIR / "resumenes").is_dir()
              and plan_mig.get("aplicado") is False,
              f"-> {plan_mig}")
        check("contenido: plan de migracion apunta a backups fuera de contenido",
              plan_mig["backup_fuera_de_contenido"]
              and str(tmp_life / "backups") in plan_mig["backup_dir"],
              f"-> {plan_mig}")
        from src import panel
        estado_life = panel.app.test_client().get("/api/estado").get_json()
        check("UI: estado advierte legacy root",
              set(estado_life.get("contenido_legacy", [])) == {"clases", "resumenes"}
              and "legacy" in estado_life.get("contenido_legacy_msg", ""),
              f"-> {estado_life}")
        config.CONTENIDO_DIR = contenido_orig
        config.PUNTERO_CLASE = puntero_orig

    with tempfile.TemporaryDirectory() as tmp_comp:
        tmp_comp = Path(tmp_comp)
        config.CONTENIDO_DIR = tmp_comp / "contenido"
        config.PUNTERO_CLASE = tmp_comp / "clase_actual.txt"
        dsin = estado.crear_clase("Sin final", curso="curso-compact")
        plan_sin = compactar_clase.plan_compactacion(dsin)
        check("compactar: clase sin leccion_final no compacta",
              plan_sin["ok"] is False and "falta leccion_final" in plan_sin["motivo"],
              f"-> {plan_sin}")

        dcomp = estado.crear_clase("Compactar dry", curso="curso-compact")
        base_comp = fijar_rangos_clase(dcomp, base=30_000.0, cortes=(), fin=120.0)
        guardar_img(dcomp, "keep.jpg", "white")
        guardar_img(dcomp, "drop.jpg", "black")
        guardar_wav_test(dcomp / "audio" / f"chunk_{int(base_comp + 10):012d}_p1_v10.wav")
        estado.anexar_jsonl(dcomp / "transcript.jsonl",
                            {"t": base_comp + 11, "fin": base_comp + 14, "parte": 1,
                             "chunk": f"chunk_{int(base_comp + 10):012d}_p1_v10.wav",
                             "texto": "Segmento compactable."})
        estado.anexar_jsonl(dcomp / "img_log.jsonl",
                            {"t": base_comp + 12, "archivo": "keep.jpg",
                             "dhash": "1111111111111111", "ocr": "Slide"})
        (dcomp / "leccion_final.md").write_text(
            "# Compactar\n\n## Reporte de cobertura temporal\n\nok\n\n"
            "![keep](img/keep.jpg)\n", encoding="utf-8")
        plan_comp = compactar_clase.plan_compactacion(dcomp, timestamp="20260707_010203")
        mover_rutas = {x["ruta"] for x in plan_comp["mover"]}
        check("compactar: dry-run no borra ni mueve",
              (dcomp / "audio").is_dir() and (dcomp / "img" / "drop.jpg").is_file()
              and plan_comp["dry_run_default"],
              f"-> {plan_comp}")
        check("compactar: conserva solo imagenes referenciadas",
              "img/drop.jpg" in mover_rutas and "img/keep.jpg" not in mover_rutas
              and "img/keep.jpg" in plan_comp["conservar"],
              f"-> mover={mover_rutas}, conservar={plan_comp['conservar']}")
        check("compactar: genera manifest en plan",
              plan_comp["manifest"]["imagenes_conservadas"] == ["keep.jpg"]
              and plan_comp["manifest"]["version"] == compactar_clase.COMPACT_VERSION,
              f"-> {plan_comp['manifest']}")
        (dcomp / "leccion_final.md").write_text(
            "# Compactar\n\n## Reporte de cobertura temporal\n\nok\n\n"
            "![missing](img/missing.jpg)\n", encoding="utf-8")
        plan_missing = compactar_clase.plan_compactacion(dcomp)
        check("compactar: detecta imagenes faltantes referenciadas",
              "missing.jpg" in plan_missing["imagenes_faltantes"]
              and not plan_missing["puede_aplicar"],
              f"-> {plan_missing}")

        from src import panel
        panel_plan = panel.app.test_client().post("/api/compactar_dry_run", json={})
        panel_data = panel_plan.get_json()
        check("UI: endpoint compactar dry-run no mueve evidencia",
              panel_plan.status_code == 200 and panel_data.get("ok") is True
              and (dcomp / "audio").is_dir() and (dcomp / "img" / "drop.jpg").is_file(),
              f"-> status={panel_plan.status_code}, data={panel_data}")
        check("UI: pagina expone boton compactar dry-run",
              "Compactar clase (dry-run)" in panel.PAGINA)

    with tempfile.TemporaryDirectory() as tmp_ctx:
        tmp_ctx = Path(tmp_ctx)
        config.CONTENIDO_DIR = tmp_ctx / "contenido"
        config.PUNTERO_CLASE = tmp_ctx / "clase_actual.txt"
        dctx = estado.crear_clase("Continuar", curso="curso-contexto")
        base_ctx = time.time() - 400
        fijar_rangos_clase(dctx, base=base_ctx, cortes=(), fin=360.0)
        guardar_img(dctx, "prev.jpg", "white")
        final_prev = dctx / "leccion_final.md"
        final_prev.write_text(
            "# Clase previa\n\n## Resumen ejecutivo\n\nHebb 1947 y perceptron.\n\n"
            "## Tema previo\n\nContenido consolidado anterior.\n![prev](img/prev.jpg)\n",
            encoding="utf-8")
        prev_mtime = time.time() - 120
        os.utime(final_prev, (prev_mtime, prev_mtime))
        old_seg = {"t": prev_mtime - 60, "fin": prev_mtime - 55, "parte": 1,
                   "texto": "Transcript viejo que no debe reenviarse completo."}
        new_seg = {"t": prev_mtime + 20, "fin": prev_mtime + 25, "parte": 1,
                   "texto": "Transcript nuevo sobre backpropagation."}
        ctx = post_procesar.contexto_previo_compacto(dctx, max_chars=500)
        nuevos = post_procesar.segmentos_nuevos_desde_final(
            dctx, [old_seg, new_seg], {"origen": "vivo", "inicio": base_ctx},
            final_mtime=prev_mtime)
        snaps = post_procesar.crear_snapshot_previo(dctx, timestamp="20260707_010203")
        check("continuacion: clase compactada usa leccion_final como contexto previo",
              ctx["disponible"] and "Hebb 1947" in ctx["texto"]
              and ctx["chars_enviados"] <= ctx["chars_originales"],
              f"-> {ctx}")
        check("continuacion: procesa transcript nuevo sin transcript viejo completo",
              len(nuevos) == 1 and nuevos[0]["texto"].startswith("Transcript nuevo"),
              f"-> {nuevos}")
        check("continuacion: genera snapshot leccion_final_PREV",
              any(p.name.startswith("leccion_final_PREV_20260707_010203")
                  and p.is_file() for p in snaps),
              f"-> {snaps}")

        segs_opt = [
            {"t": base_ctx + 1, "fin": base_ctx + 2,
             "texto": "um okay Hebb 1947 propuso una regla de aprendizaje."},
            {"t": base_ctx + 3, "fin": base_ctx + 4,
             "texto": "okay Hebb 1947 propuso una regla de aprendizaje."},
            {"t": base_ctx + 5, "fin": base_ctx + 6,
             "texto": "La formula y = f(x) se conserva."},
        ]
        opt = post_procesar.optimizar_transcript_llm(
            segs_opt, {"origen": "vivo", "inicio": base_ctx})
        check("tokens: dedup transcript reduce repeticiones y conserva hitos",
              opt["segmentos_deduplicados"] >= 1
              and "Hebb 1947" in opt["texto"] and "y = f(x)" in opt["texto"],
              f"-> {opt}")
        ocr = post_procesar.reducir_ocr_slide({
            "ocr": "McCulloch Pitts 1943\nHebb 1947\nPerceptron\n" + "ruido " * 300
        }, max_chars=120)
        check("tokens: OCR reducido conserva titulo y bullets",
              "McCulloch Pitts 1943" in ocr["texto"]
              and "Hebb 1947" in ocr["texto"]
              and ocr["chars_enviados"] < ocr["chars_brutos"],
              f"-> {ocr}")
        rep_tok = post_procesar._reporte_optimizacion_tokens_md([
            {"transcript_chars_brutos": opt["chars_brutos"],
             "transcript_chars_enviados": opt["chars_enviados"],
             "segmentos_deduplicados": opt["segmentos_deduplicados"],
             "ocr_chars_brutos": ocr["chars_brutos"],
             "ocr_chars_enviados": ocr["chars_enviados"]}
        ], ctx)
        check("tokens: reporte de ahorro aparece",
              "Ahorro transcript estimado" in rep_tok
              and "Ahorro contexto previo" in rep_tok,
              f"-> {rep_tok}")
        key_cache = post_procesar.cache_llm_key(
            "prompt-v1", "texto con sk-SECRET-DE-PRUEBA", "fichas")
        check("tokens: cache LLM hash no incluye API key",
              "sk-" not in key_cache and len(key_cache) == 64,
              f"-> {key_cache}")

        slides_masivas = []
        for i in range(154):
            if i in {7, 42, 99}:
                ocr_txt = f"Deep Learning 1986\nBackpropagation\nformula y = f(Wx+b)\nURL https://example.org/{i}"
            elif i % 9 == 0:
                ocr_txt = "Decorative transition no useful text"
            else:
                ocr_txt = "History and resources same repeated agenda"
            slides_masivas.append({
                "archivo": f"mass_{i:03d}.jpg",
                "t_rel": float(i * 4),
                "ocr": ocr_txt,
                "transcript_ventana": "The professor explains backpropagation, neural networks and 1986 history.",
                "motivo": "slide_nueva",
                "grupo_total": 1,
            })
        seleccion_masiva = post_procesar.seleccionar_slides_por_tema(
            slides_masivas,
            "The professor explains backpropagation, neural networks and 1986 history.",
            presupuesto=18,
            tema_actual="history and resources",
            curso="prueba-deep-learning",
            parte=2)
        rep_sel_masiva = getattr(post_procesar.seleccionar_slides_por_tema,
                                 "ultimo_reporte", {})
        check("tokens: 154 slides sinteticas se rankean antes del LLM",
              len(seleccion_masiva) <= 18
              and rep_sel_masiva["slides_descartadas_presupuesto"] >= 130,
              f"-> n={len(seleccion_masiva)}, rep={rep_sel_masiva}")
        check("tokens: ranking conserva formulas, anos y titulos",
              any("formula" in s["ocr"] and "1986" in s["ocr"]
                  for s in seleccion_masiva),
              f"-> {[s['archivo'] for s in seleccion_masiva]}")
        decor = {
            "archivo": "decor.jpg", "ocr": "sin texto legible probablemente misma vista",
            "transcript_ventana": "", "sin_transcript_cercano": True,
            "motivo": "slide_nueva",
        }
        formula = {
            "archivo": "formula.jpg", "ocr": "Perceptron 1958\nw = w + eta x\nTAREA dataset",
            "transcript_ventana": "Perceptron 1958 formula and dataset task",
            "motivo": "slide_nueva",
        }
        score_decor = post_procesar.puntuar_slide_relevancia(decor)
        score_formula = post_procesar.puntuar_slide_relevancia(
            formula, formula["transcript_ventana"], "perceptron")
        check("tokens: slides decorativas sin transcript se penalizan",
              score_formula["score"] > score_decor["score"]
              and {"sin_transcript", "ocr_pobre"} & set(score_decor["penalizaciones"]),
              f"-> decor={score_decor}, formula={score_formula}")

        segs_tema = []
        for i in range(4):
            segs_tema.append({"t": i * 45, "fin": i * 45 + 30,
                              "texto": "Deep learning neural networks gradient descent loss function."})
        for i in range(6):
            segs_tema.append({"t": 240 + i * 45, "fin": 240 + i * 45 + 30,
                              "texto": "Welcome to class today we will study English literature poetry grammar writing."})
        segmentos_tema = post_procesar.segmentar_transcript_por_tema(
            segs_tema, [], "prueba-deep-learning", "deep learning history")
        check("segmentacion: detecta cambio sostenido a otra clase",
              any(s["clasificacion"] == "otra_clase_legitima" for s in segmentos_tema),
              f"-> {segmentos_tema}")
        segs_pub = [
            {"t": 0, "fin": 40,
             "texto": "Deep learning gradient descent and neural network training."},
            {"t": 240, "fin": 300,
             "texto": "Subscribe and use this discount coupon for free shipping sponsor offer."},
        ]
        segmentos_pub = post_procesar.segmentar_transcript_por_tema(
            segs_pub, [], "prueba-deep-learning", "deep learning")
        check("segmentacion: publicidad no se mezcla como leccion principal",
              any(s["clasificacion"] == "publicidad_ruido" for s in segmentos_pub),
              f"-> {segmentos_pub}")
        preguntar_orig_extra = post_procesar.preguntar

        def fake_extra(msgs, max_tokens=0, etiqueta="", **_kwargs):
            if etiqueta.startswith("post-extra"):
                return "## Lección adicional generada\nEnglish literature poetry grammar."
            return "SIN_TAREAS"

        try:
            post_procesar.preguntar = fake_extra
            extras = post_procesar.generar_lecciones_extra_detectadas(
                dctx, segmentos_tema, {"origen": "video", "inicio": 0})
        finally:
            post_procesar.preguntar = preguntar_orig_extra
        check("segmentacion: otra clase legitima genera archivo separado",
              extras and extras[0].is_file()
              and extras[0].name.startswith("01_leccion_")
              and "Lección adicional generada" in extras[0].read_text(encoding="utf-8")
              and "Transcripción base preservada" in extras[0].read_text(encoding="utf-8"),
              f"-> {extras}")

        estado.anexar_jsonl(dctx / "transcript.jsonl", old_seg)
        estado.anexar_jsonl(dctx / "transcript.jsonl", new_seg)
        preguntar_orig_ctx = post_procesar.preguntar
        fuentes_orig_ctx = post_procesar.fuentes_ampliadas_md

        def fake_ctx(msgs, max_tokens=0, etiqueta="", **_kwargs):
            user_msg = msgs[-1]["content"] if msgs else ""
            if etiqueta.startswith("post-chunk"):
                return "## Material nuevo\nBackpropagation nuevo explicado."
            if etiqueta == "post-merge-parte1":
                return "## Material nuevo integrado\nBackpropagation nuevo explicado."
            if etiqueta == "post-resumen":
                return "Resumen ejecutivo global actualizado."
            if etiqueta == "post-tareas":
                return "SIN_TAREAS"
            return user_msg[:400]

        try:
            post_procesar.preguntar = fake_ctx
            post_procesar.fuentes_ampliadas_md = lambda _q: ""
            post_procesar.consolidar_todo(dctx, forzar=True, limpiar_al_final=False)
        finally:
            post_procesar.preguntar = preguntar_orig_ctx
            post_procesar.fuentes_ampliadas_md = fuentes_orig_ctx
        final_ctx = (dctx / "leccion_final.md").read_text(encoding="utf-8")
        debug_ctx = (dctx / "_debug" / "postprocess_report.md").read_text(
            encoding="utf-8")
        check("continuacion: integra localmente la leccion previa completa",
              "Contenido consolidado anterior." in final_ctx
              and "img/prev.jpg" in final_ctx
              and "Leccion previa preservada integramente" not in final_ctx
              and "Material nuevo procesado" not in final_ctx
              and final_ctx.count("## 🧭 Resumen ejecutivo") == 1
              and "Leccion previa integrada completa" in debug_ctx
              and "Leccion previa integrada completa" not in final_ctx,
              f"-> {final_ctx[:1200]}")

        preguntar_orig_cache = post_procesar.preguntar
        env_cache_orig = os.environ.get("CLASE_POST_CACHE")
        llamadas_cache = []

        def fake_cache(msgs, max_tokens=0, etiqueta="", **_kwargs):
            llamadas_cache.append(etiqueta)
            return "RESPUESTA CACHEADA"

        try:
            os.environ["CLASE_POST_CACHE"] = "1"
            post_procesar.preguntar = fake_cache
            post_procesar._cache_stats_reset()
            msgs_cache = [{"role": "user", "content": "mismo prompt sintetico"}]
            r1 = post_procesar.preguntar_cacheado(
                dctx, msgs_cache, 100, "cache-test", "prompt-cache-v1")
            r2 = post_procesar.preguntar_cacheado(
                dctx, msgs_cache, 100, "cache-test", "prompt-cache-v1")
            stats_cache = post_procesar.cache_llm_stats()
        finally:
            post_procesar.preguntar = preguntar_orig_cache
            if env_cache_orig is None:
                os.environ.pop("CLASE_POST_CACHE", None)
            else:
                os.environ["CLASE_POST_CACHE"] = env_cache_orig
        check("tokens: CLASE_POST_CACHE produce hit en segunda llamada",
              r1 == r2 == "RESPUESTA CACHEADA"
              and len(llamadas_cache) == 1
              and stats_cache["cache_hits"] >= 1,
              f"-> llamadas={llamadas_cache}, stats={stats_cache}")

        (tmp_ctx / "PROJECT_STATE.md").write_text(
            "\n".join(f"- entrada {i}" for i in range(450)), encoding="utf-8")
        rep_docs = auditar_contenido.auditar_docs(tmp_ctx)
        check("docs: auditoria reporta estado grande sin modificar",
              rep_docs["dry_run_default"] and rep_docs["aplicado"] is False
              and any(d["archivo"] == "PROJECT_STATE.md" and d.get("lineas", 0) >= 450
                      and d.get("recomendacion") for d in rep_docs["docs"]),
              f"-> {rep_docs}")
        root_docs_orig = config.ROOT
        try:
            config.ROOT = tmp_ctx
            out_docs = io.StringIO()
            with contextlib.redirect_stdout(out_docs):
                rc_docs = auditar_contenido.main(["--docs"])
        finally:
            config.ROOT = root_docs_orig
        check("docs: CLI --docs imprime auditoria sin reporte contenido",
              rc_docs == 0 and "Auditoria docs" in out_docs.getvalue()
              and "Auditoria contenido" not in out_docs.getvalue(),
              f"-> rc={rc_docs}, out={out_docs.getvalue()[:200]}")

    with tempfile.TemporaryDirectory() as tmp_ui:
        tmp_ui = Path(tmp_ui)
        config.CONTENIDO_DIR = tmp_ui / "contenido"
        config.PUNTERO_CLASE = tmp_ui / "clase_actual.txt"
        dui = estado.crear_clase("Filtro interfaz", curso="curso-ui")
        base = fijar_rangos_clase(dui, base=15_000.0, cortes=(), fin=300.0)
        origen_ui = {"origen": "vivo", "inicio": base}
        guardar_img(dui, "panel.jpg", "white")
        guardar_img(dui, "slide_real.jpg", "black")
        estado.anexar_jsonl(dui / "img_log.jsonl",
                            {"t": base + 20, "archivo": "panel.jpg",
                             "dhash": "1111111111111111",
                             "ocr": "ClaseViva Consolidar lección final Copiloto localhost"})
        estado.anexar_jsonl(dui / "img_log.jsonl",
                            {"t": base + 40, "archivo": "slide_real.jpg",
                             "dhash": "2222222222222222",
                             "ocr": "McCulloch Pitts 1943 Hebb 1947 Perceptron"})
        segs_ui = [{"t": base + 42, "fin": base + 48, "parte": 1,
                    "texto": "El profesor explica el perceptron y los recursos historicos."}]
        slides_ui = post_procesar.construir_timeline_slides(
            dui, 0, 80, segs_ui, origen_ui)
        rep_ui = getattr(post_procesar.construir_timeline_slides, "ultimo_reporte", {})
        check("no-slide: captura ClaseViva se filtra si hay slide real",
              len(slides_ui) == 1 and slides_ui[0]["archivo"] == "slide_real.jpg"
              and rep_ui.get("capturas_interfaz_filtradas", 0) >= 1,
              f"-> slides={slides_ui}, reporte={rep_ui}")
        check("no-slide: slide real de deep learning se conserva",
              not slides_ui[0].get("no_slide")
              and "McCulloch" in slides_ui[0].get("ocr", ""),
              f"-> {slides_ui}")

        dsolo = estado.crear_clase("Solo interfaz", curso="curso-ui")
        base2 = fijar_rangos_clase(dsolo, base=16_000.0, cortes=(), fin=120.0)
        origen_solo = {"origen": "vivo", "inicio": base2}
        guardar_img(dsolo, "solo_panel.jpg", "gray")
        estado.anexar_jsonl(dsolo / "img_log.jsonl",
                            {"t": base2 + 20, "archivo": "solo_panel.jpg",
                             "dhash": "3333333333333333",
                             "ocr": "ClaseViva Controles de clase Transcripción Copiloto"})
        segs_solo = [{"t": base2 + 22, "fin": base2 + 30, "parte": 1,
                      "texto": "Transcript util del profesor sobre optimizacion y redes neuronales " * 4}]
        slides_solo = post_procesar.construir_timeline_slides(
            dsolo, 0, 80, segs_solo, origen_solo)
        check("no-slide: si solo hay UI y transcript util se conserva secundaria",
              len(slides_solo) == 1 and slides_solo[0].get("evidencia_secundaria")
              and slides_solo[0].get("no_slide"),
              f"-> {slides_solo}")

    dup_md = (
        '| <img src="img/cap_dup.jpg" width="420"> | curva de perdida del modelo |\n'
        '| <img src="img/cap_dup.jpg" width="420"> | arquitectura CNN con filtros convolucionales |\n'
        '| <img src="img/cap_ok.jpg" width="420"> | slide distinta |')
    dup_limpio, dup_rep = post_procesar.deduplicar_referencias_img(dup_md)
    check("duplicados: caption incompatible deja una sola imagen principal",
          dup_limpio.count("img/cap_dup.jpg") == 1
          and dup_rep["imagenes_duplicadas_detectadas"] == 1
          and dup_rep["duplicados_eliminados"] == 1
          and dup_rep["caption_incompatible_advertencias"],
          f"-> limpio={dup_limpio}, reporte={dup_rep}")
    ap_visual = post_procesar._apendice_slides_omitidas_md(
        '| <img src="img/slide_extra.jpg" width="420"> | idea clave omitida del merge |',
        {"slide_extra.jpg"}, parte=1)
    check("slides omitidas por merge vuelven como apendice visual",
          "Apendice visual de estudio" in ap_visual
          and 'img/slide_extra.jpg' in ap_visual
          and "idea clave omitida" in ap_visual,
          f"-> {ap_visual}")

    with tempfile.TemporaryDirectory() as tmp_cov:
        tmp_cov = Path(tmp_cov)
        config.CONTENIDO_DIR = tmp_cov / "contenido"
        config.PUNTERO_CLASE = tmp_cov / "clase_actual.txt"
        dcov = estado.crear_clase("Cobertura temporal", curso="curso-cov")
        base = fijar_rangos_clase(dcov, base=20_000.0, cortes=(), fin=140.0)
        origen_cov = {"origen": "vivo", "inicio": base}
        wavs_cov = [
            f"chunk_{int(base + 10):012d}_p1_v10.wav",
            f"chunk_{int(base + 40):012d}_p1_v10.wav",
            f"chunk_{int(base + 70):012d}_p1_v10.wav",
        ]
        for nombre in wavs_cov:
            guardar_wav_test(dcov / "audio" / nombre, seconds=1.0)
        for obj in [
            {"t": base + 11, "fin": base + 14, "parte": 1,
             "chunk": wavs_cov[0], "texto": "Primer chunk transcrito."},
            {"t": base + 41, "fin": base + 44, "parte": 1,
             "chunk": wavs_cov[1], "texto": "Segundo chunk transcrito."},
            {"t": base + 95, "fin": base + 98, "parte": 1,
             "chunk": f"chunk_{int(base + 95):012d}_p1_v10.wav",
             "texto": "Segmento que referencia WAV inexistente."},
        ]:
            estado.anexar_jsonl(dcov / "transcript.jsonl", obj)
        segs_cov = estado.leer_jsonl(dcov / "transcript.jsonl")
        temporal = post_procesar.analizar_cobertura_temporal(dcov, segs_cov, origen_cov)
        cobertura_audio = post_procesar.analizar_cobertura_audio(dcov, segs_cov, [],
                                                                 origen_cov)
        reporte_temporal = post_procesar._reporte_cobertura_temporal_md(temporal)
        check("cobertura temporal: detecta WAV sin transcript",
              temporal["wav_sin_transcript_total"] == 1
              and wavs_cov[2] in temporal["wav_sin_transcript"],
              f"-> {temporal}")
        check("cobertura temporal: detecta transcript con WAV inexistente",
              temporal["transcript_wav_faltante_total"] == 1
              and temporal["estado"] == "advertencia",
              f"-> {temporal}")
        check("cobertura temporal: reporte incluye nota de audio inexistente",
              "DeepSeek no puede reconstruir audio inexistente" in reporte_temporal,
              f"-> {reporte_temporal}")
        check("cobertura temporal pendiente bloquea limpieza segura",
              cobertura_audio["cobertura_estado"] == "dudosa"
              and not cobertura_audio["limpieza_segura"],
              f"-> {cobertura_audio}")
        check("cobertura temporal: v10 reporta velocidad 1x",
              temporal["velocidad_codigo"] == "v10"
              and temporal["velocidad_factor"] == 1.0
              and "velocidad 2x" in reporte_temporal,
              f"-> temporal={temporal}, reporte={reporte_temporal}")

    with tempfile.TemporaryDirectory() as tmp_v20:
        tmp_v20 = Path(tmp_v20)
        config.CONTENIDO_DIR = tmp_v20 / "contenido"
        config.PUNTERO_CLASE = tmp_v20 / "clase_actual.txt"
        dv20 = estado.crear_clase("Cobertura v20", curso="curso-cov")
        base = fijar_rangos_clase(dv20, base=21_000.0, cortes=(), fin=90.0)
        origen_v20 = {"origen": "vivo", "inicio": base}
        wav20 = f"chunk_{int(base + 10):012d}_p1_v20.wav"
        guardar_wav_test(dv20 / "audio" / wav20, seconds=2.0)
        estado.anexar_jsonl(dv20 / "transcript.jsonl",
                            {"t": base + 11, "fin": base + 14, "parte": 1,
                             "chunk": wav20, "texto": "Chunk a velocidad configurada 2x."})
        temporal20 = post_procesar.analizar_cobertura_temporal(
            dv20, estado.leer_jsonl(dv20 / "transcript.jsonl"), origen_v20)
        reporte20 = post_procesar._reporte_cobertura_temporal_md(temporal20)
        check("cobertura temporal: v20 estima equivalente de video x2",
              temporal20["velocidad_codigo"] == "v20"
              and temporal20["velocidad_factor"] == 2.0
              and temporal20["duracion_video_equivalente_seg"] >= 3.5
              and "ClaseViva registró velocidad 2x" in reporte20,
              f"-> temporal={temporal20}, reporte={reporte20}")

    origen_epoch = {"origen": "vivo", "inicio": 1_700_000_000.0}
    segs_epoch = [
        {"t": 1_700_000_010.0, "ini": 0.0, "fin": 2.0, "texto": "inicio"},
        {"t": 1_700_000_260.0, "ini": 0.0, "fin": 2.0, "texto": "retoma"},
    ]
    caps_epoch = [{"t": 1_700_000_120.0, "archivo": "cap.jpg"}]
    gaps_epoch = post_procesar.detectar_gaps_transcript(
        segs_epoch, caps_epoch, origen_epoch)
    segs_rel = [
        {"t": 10.0, "fin": 12.0, "texto": "inicio"},
        {"t": 260.0, "fin": 262.0, "texto": "retoma"},
    ]
    gaps_rel = post_procesar.detectar_gaps_transcript(
        segs_rel, [{"t": 120.0, "archivo": "cap.jpg"}], origen_epoch)
    check("timestamps epoch/relativo -> gap razonable",
          200 <= gaps_epoch["gap_max_segundos"] <= 260
          and 200 <= gaps_rel["gap_max_segundos"] <= 260,
          f"-> epoch={gaps_epoch}, rel={gaps_rel}")
    check("gap no se vuelve absurdo de cientos de miles de horas",
          gaps_epoch["gap_max_segundos"] < 3600
          and "495401" not in gaps_epoch["gap_max_fmt"],
          f"-> {gaps_epoch}")

    with tempfile.TemporaryDirectory() as tmp_sg:
        tmp_sg = Path(tmp_sg)
        config.CONTENIDO_DIR = tmp_sg / "contenido"
        config.PUNTERO_CLASE = tmp_sg / "clase_actual.txt"
        dsg = estado.crear_clase("Slide guided", curso="curso-slide")
        base = fijar_rangos_clase(dsg, base=10_000.0, cortes=(), fin=760.0)
        origen_sg = {"origen": "vivo", "inicio": base}
        colores = ["white", "red", "green", "blue", "yellow"]
        dhashes_distintos = [
            "0000000000000000", "ffffffffffffffff", "00ff00ff00ff00ff",
            "ff00ff00ff00ff00", "0f0f0f0f0f0f0f0f",
        ]
        for i, color in enumerate(colores, 1):
            nombre = f"sg_{i}.jpg"
            guardar_img(dsg, nombre, color)
            estado.anexar_jsonl(dsg / "img_log.jsonl",
                                {"t": base + i * 30, "archivo": nombre,
                                 "dhash": dhashes_distintos[i - 1],
                                 "ocr": f"Slide {i} titulo"})
            estado.anexar_jsonl(dsg / "transcript.jsonl",
                                {"t": base + i * 30 + 3, "fin": base + i * 30 + 8,
                                 "parte": 1,
                                 "texto": f"SLIDE_{i}_TRANSCRIPT explicación cercana " * 6})
        segs_sg = estado.leer_jsonl(dsg / "transcript.jsonl")
        slides5 = post_procesar.construir_timeline_slides(
            dsg, 0, 220, segs_sg, origen_sg)
        check("slide-guided: 5 slides distintas conservadas con transcript cercano",
              len(slides5) == 5
              and all(f"SLIDE_{i}_TRANSCRIPT" in slides5[i - 1]["transcript_ventana"]
                      for i in range(1, 6)),
              f"-> {slides5}")

        for i in range(30):
            nombre = f"dup_{i:02d}.jpg"
            guardar_img(dsg, nombre, "gray")
            estado.anexar_jsonl(dsg / "img_log.jsonl",
                                {"t": base + 250 + i, "archivo": nombre,
                                 "dhash": "aaaaaaaaaaaaaaaa", "ocr": "misma slide"})
        slides_dup = post_procesar.construir_timeline_slides(
            dsg, 240, 290, [], origen_sg)
        check("slide-guided: 30 capturas casi iguales deduplican a 1 slide",
              len(slides_dup) == 1 and slides_dup[0].get("grupo_total") == 30,
              f"-> {slides_dup}")

        for nombre, ocr_txt, t_rel in [
                ("ocr_a.jpg", "Alpha beta gamma delta epsilon zeta theta",
                 330),
                ("ocr_b.jpg", "Volta Galvani Frankenstein frog muscle current",
                 340)]:
            guardar_img(dsg, nombre, "purple")
            estado.anexar_jsonl(dsg / "img_log.jsonl",
                                {"t": base + t_rel, "archivo": nombre,
                                 "dhash": "bbbbbbbbbbbbbbbb", "ocr": ocr_txt})
        slides_ocr = post_procesar.construir_timeline_slides(
            dsg, 320, 360, [], origen_sg)
        check("slide-guided: OCR importante conserva nueva slide con dhash parecido",
              len(slides_ocr) == 2
              and "ocr_cambio_importante" in slides_ocr[1].get("motivo", ""),
              f"-> {slides_ocr}")

        guardar_img(dsg, "sin_audio.jpg", "black")
        estado.anexar_jsonl(dsg / "img_log.jsonl",
                            {"t": base + 600, "archivo": "sin_audio.jpg",
                             "dhash": "cccccccccccccccc", "ocr": "Diagrama sin voz cercana"})
        slide_sin_audio = post_procesar.construir_timeline_slides(
            dsg, 590, 610, [], origen_sg)
        fichas_sin_audio = post_procesar.fichas_slides(slide_sin_audio, origen_sg)
        check("slide-guided: slide sin transcript queda marcada sin inventar audio",
              len(slide_sin_audio) == 1
              and slide_sin_audio[0].get("sin_transcript_cercano") is True
              and "EVIDENCIA VISUAL SIN AUDIO" in fichas_sin_audio,
              f"-> slides={slide_sin_audio}, fichas={fichas_sin_audio}")

        check("slide-guided: 20 slides se dividen en tandas sin descarte silencioso",
              [len(x) for x in post_procesar._slide_batches([{} for _ in range(20)])]
              == [12, 8])
        omitidas = post_procesar._registrar_slides_omitidas_por_merge(
            dsg, 1,
            "## Tema\ntexto\n|  |  |\n|---|---|\n"
            "| <img src=\"img/keep.jpg\" width=\"420\"> | explicación con transcript |",
            {"keep.jpg"})
        debug_omitidas = (dsg / "_debug" / "slides_omitidas_por_merge.md").read_text(
            encoding="utf-8")
        check("slide-guided: merge que elimina imagen registra debug",
              omitidas["slides_omitidas_por_merge"] == 1
              and "img/keep.jpg" in debug_omitidas
              and "reubican" in debug_omitidas.lower(),
              f"-> omitidas={omitidas}, debug={debug_omitidas}")

        omitidas_ui = post_procesar._bloques_img_omitidas(
            "| <img src=\"img/panel.jpg\" width=\"420\"> | ClaseViva Copiloto localhost |\n"
            "| <img src=\"img/sin_transcript.jpg\" width=\"420\"> | caption débil |",
            {"panel.jpg", "sin_transcript.jpg"})
        check("slide-guided: omitida UI/no-slide y sin transcript van solo a debug",
              len(omitidas_ui) == 2
              and any(x["es_interfaz"] for x in omitidas_ui)
              and any(not x["tiene_transcript_asociado"] for x in omitidas_ui),
              f"-> {omitidas_ui}")

    with tempfile.TemporaryDirectory() as tmp_omit:
        tmp_omit = Path(tmp_omit)
        config.CONTENIDO_DIR = tmp_omit / "contenido"
        config.PUNTERO_CLASE = tmp_omit / "clase_actual.txt"
        domit = estado.crear_clase("Merge omite slides", curso="curso-slide")
        base = fijar_rangos_clase(domit, base=11_000.0, cortes=(), fin=160.0)
        for i, color in [(1, "white"), (2, "red")]:
            nombre = f"omit_{i}.jpg"
            guardar_img(domit, nombre, color, pedagogica=True)
            estado.anexar_jsonl(domit / "img_log.jsonl",
                                {"t": base + i * 25, "archivo": nombre,
                                 "dhash": f"{i:016x}",
                                 "ocr": (
                                     "Alpha beta gamma delta epsilon zeta theta"
                                     if i == 1 else
                                     "Volta Galvani Frankenstein frog muscle current")})
            evidencia = (
                "Alpha beta gamma delta epsilon zeta theta"
                if i == 1 else
                "Volta Galvani Frankenstein frog muscle current"
            )
            estado.anexar_jsonl(domit / "transcript.jsonl",
                                {"t": base + i * 25 + 2, "fin": base + i * 25 + 7,
                                 "parte": 1,
                                 "texto": (f"OMIT_{i}_TRANSCRIPT {evidencia} " * 8)})
        preguntar_orig = post_procesar.preguntar

        def fake_omite_imgs(msgs, max_tokens=0, etiqueta="", **_kwargs):
            user_msg = msgs[-1]["content"] if msgs else ""
            if etiqueta.startswith("post-chunk"):
                refs = re.findall(r"SLIDE \d+: ([^\s]+)", user_msg)
                return "\n".join(
                    ["## Chunk con slides"] +
                    [f"[[IMG:{ref}|explicación cercana basada en transcript]]"
                     for ref in refs])
            if etiqueta == "post-merge-parte1":
                return "## Tema integrado\nEl merge conserva texto, pero omite las imágenes."
            if etiqueta == "post-resumen":
                return "Resumen sin galería residual."
            if etiqueta == "post-tareas":
                return "SIN_TAREAS"
            return ""

        try:
            post_procesar.preguntar = fake_omite_imgs
            post_procesar.consolidar_todo(domit, forzar=True, limpiar_al_final=False)
        finally:
            post_procesar.preguntar = preguntar_orig
        final_omit = (domit / "leccion_final.md").read_text(encoding="utf-8")
        debug_omit = (domit / "_debug" / "slides_omitidas_por_merge.md").read_text(
            encoding="utf-8")
        post_report_omit = (domit / "_debug" / "postprocess_report.md").read_text(
            encoding="utf-8")
        check("slide-guided: desarrollo detallado conserva cada visual una vez",
              "Desarrollo exhaustivo por tramos" in final_omit
              and "Desarrollo visual complementario" not in final_omit
              and final_omit.count("img/omit_1.jpg") == 1
              and "img/omit_2.jpg" in final_omit,
              f"-> {final_omit[:1000]}")
        check("slide-guided: visuales canónicos del detalle no se marcan omitidos",
              "img/omit_1.jpg" not in debug_omit and "img/omit_2.jpg" not in debug_omit,
              f"-> {debug_omit}")
        check("slide-guided: reporte distingue merge panorámico y detalle canónico",
              "Slides omitidas por merge: 0" in post_report_omit
              and "Slides reintegradas cerca de su explicación: 0" in post_report_omit
              and "Slides auditables en debug: 0; solo debug/no valor: 0" in post_report_omit
              and "Slides omitidas por merge:" not in final_omit
              and "Desarrollo exhaustivo por tramos" in final_omit,
              f"-> {final_omit[:1200]}")

    with tempfile.TemporaryDirectory() as tmp_sg2:
        tmp_sg2 = Path(tmp_sg2)
        config.CONTENIDO_DIR = tmp_sg2 / "contenido"
        config.PUNTERO_CLASE = tmp_sg2 / "clase_actual.txt"
        dsg2 = estado.crear_clase("Slide final", curso="curso-slide")
        base = fijar_rangos_clase(dsg2, base=12_000.0, cortes=(), fin=260.0)
        transcript_sg2 = []
        for i in range(1, 6):
            nombre = f"sf_{i}.jpg"
            guardar_img(
                dsg2, nombre, ["white", "red", "green", "blue", "yellow"][i - 1],
                pedagogica=True)
            estado.anexar_jsonl(dsg2 / "img_log.jsonl",
                                {"t": base + i * 30, "archivo": nombre,
                                 "dhash": dhashes_distintos[i - 1],
                                 "ocr": (
                                     f"Modelo final gradiente perdida entrenamiento w = {i}")})
            texto = (f"SF_{i}_TRANSCRIPT modelo final gradiente perdida "
                     f"entrenamiento w = {i}. ")
            if i == 5:
                texto += "Cambio de video: Galvani Volta Frankenstein frog muscle. "
            transcript_sg2.append({
                "t": base + i * 30 + 2,
                "fin": base + i * 30 + 6,
                "parte": 1,
                "texto": texto * 8,
            })
        guardar_img(dsg2, "unused.jpg", "black")
        wav_sg2 = dsg2 / "audio" / f"chunk_{int(base + 30):012d}_p1_v10.wav"
        guardar_wav_test(wav_sg2)
        for row in transcript_sg2:
            row["chunk"] = wav_sg2.name
        audio_manifest.commit_transcript_chunk(
            dsg2,
            wav_sg2.stem,
            transcript_sg2,
            {"path": wav_sg2.name},
            materialize=False,
        )
        preguntar_orig = post_procesar.preguntar

        def fake_slide_post(msgs, max_tokens=0, etiqueta="", **_kwargs):
            user_msg = msgs[-1]["content"] if msgs else ""
            if etiqueta.startswith("post-chunk"):
                refs = re.findall(r"SLIDE \d+: ([^\s]+)", user_msg)
                cuerpo = ["## Reconstrucción por slides"]
                if "Galvani" in user_msg:
                    cuerpo.append("## Cambio de video o profesor: Galvani, Volta y Frankenstein")
                for ref in refs:
                    cuerpo.append(f"[[IMG:{ref}|explicación cercana basada en transcript]]")
                    cuerpo.append(f"Detalle asociado a {ref}.")
                return "\n".join(cuerpo)
            if etiqueta == "post-merge-parte1":
                return user_msg
            if etiqueta == "post-resumen":
                return "Resumen slide-guided."
            if etiqueta == "post-tareas":
                return "SIN_TAREAS"
            return ""

        try:
            post_procesar.preguntar = fake_slide_post
            post_procesar.consolidar_todo(dsg2, forzar=True, limpiar_al_final=False)
        finally:
            post_procesar.preguntar = preguntar_orig
        final_sg2 = (dsg2 / "leccion_final.md").read_text(encoding="utf-8")
        check("slide-guided: salida final incluye 5 imágenes con explicación cercana",
              all(f"img/sf_{i}.jpg" in final_sg2 for i in range(1, 6))
              and "explicación cercana basada en transcript" in final_sg2,
              f"-> {final_sg2[:800]}")
        check("slide-guided: cambio de video/profesor en transcript se conserva",
              "Galvani" in final_sg2 and "Frankenstein" in final_sg2,
              f"-> {final_sg2[:800]}")
        check("--sin-limpiar conserva WAV e imágenes no referenciadas",
              any((dsg2 / "audio").glob("*.wav"))
              and (dsg2 / "img" / "unused.jpg").is_file(),
              f"-> wav={list((dsg2 / 'audio').glob('*.wav'))}, "
              f"unused={(dsg2 / 'img' / 'unused.jpg').exists()}")

    with tempfile.TemporaryDirectory() as tmp6:
        tmp6 = Path(tmp6)
        config.CONTENIDO_DIR = tmp6 / "contenido"
        config.PUNTERO_CLASE = tmp6 / "clase_actual.txt"
        d6 = estado.crear_clase("Selector v72", curso="curso-v72")
        base = 5000.0
        fijar_rangos_clase(d6, base=base, cortes=(), fin=900.0)
        for i in range(100):
            guardar_img(d6, f"sim_{i:03d}.jpg", "white")
            estado.anexar_jsonl(d6 / "img_log.jsonl",
                                {"t": base + i, "archivo": f"sim_{i:03d}.jpg",
                                 "dhash": "0000000000000000"})
        segs_selector = [
            {"t": base + 20, "fin": base + 25, "parte": 1,
             "texto": "Definición importante de gradient descent, loss function y fórmula MSE para el examen."}
        ]
        caps_selector = estado.leer_jsonl(d6 / "img_log.jsonl")
        sel_sim = post_procesar.seleccionar_capturas_relevantes(
            d6, caps_selector, segs_selector, "vivo", max_imgs=24)
        check("100 capturas similares -> selector devuelve pocas",
              1 <= len(sel_sim) <= 3, f"-> {len(sel_sim)}")

        guardar_img(d6, "tecnica.jpg", "black")
        guardar_img(d6, "lejana.jpg", "blue")
        caps_prioridad = [
            {"t": base + 21, "archivo": "tecnica.jpg", "dhash": "ffffffffffffffff",
             "ocr": "MSE loss function gradient descent formula"},
            {"t": base + 500, "archivo": "lejana.jpg", "dhash": "00ff00ff00ff00ff",
             "ocr": "pantalla de pausa sin contenido"},
        ]
        sel_prio = post_procesar.seleccionar_capturas_relevantes(
            d6, caps_prioridad, segs_selector, "vivo", max_imgs=1)
        check("captura cercana a segmento tecnico se prioriza",
              sel_prio and sel_prio[0]["archivo"] == "tecnica.jpg", f"-> {sel_prio}")

        caps_silencio = []
        for i in range(30):
            nombre = f"sil_{i:02d}.jpg"
            guardar_img(d6, nombre, "gray")
            caps_silencio.append({"t": base + 1000 + i * 20, "archivo": nombre,
                                  "dhash": f"{i:016x}",
                                  "ocr": "Diapositiva con tabla de resultados y ecuaciones utiles" if i < 8 else ""})
        sel_sil = post_procesar.seleccionar_capturas_relevantes(
            d6, caps_silencio, [], "vivo", max_imgs=6)
        check("silencio largo con muchas capturas no envia todas",
              len(sel_sil) <= 6 and len(sel_sil) < len(caps_silencio),
              f"-> {len(sel_sil)}")

        sel_old = post_procesar.seleccionar_capturas_relevantes(
            d6, [{"t": base + 22, "archivo": "tecnica.jpg"}], segs_selector, "vivo",
            max_imgs=2)
        check("compatibilidad img_log antiguo sin campos nuevos",
              bool(sel_old) and sel_old[0]["archivo"] == "tecnica.jpg", f"-> {sel_old}")
        try:
            sel_missing = post_procesar.seleccionar_capturas_relevantes(
                d6, [{"archivo": "tecnica.jpg"}], [], "vivo", max_imgs=2)
            ok_missing = isinstance(sel_missing, list)
        except Exception:
            ok_missing = False
        check("fallback seguro si faltan OCR o timestamps", ok_missing)

        dvis = estado.crear_clase("Visual study", curso="curso-v72")
        base_vis = fijar_rangos_clase(dvis, base=6000.0, cortes=(), fin=240.0)
        for i, ocr_txt in enumerate([
                "Titulo",
                "Titulo\nprimer bullet",
                "Titulo\nprimer bullet\nsegundo bullet\nformula y = f(Wx+b)",
        ], 1):
            nombre = f"prog_{i}.jpg"
            guardar_img(dvis, nombre, "white")
            estado.anexar_jsonl(dvis / "img_log.jsonl",
                                {"t": base_vis + i * 6, "archivo": nombre,
                                 "dhash": "1111111111111111", "ocr": ocr_txt})
        slides_prog = post_procesar.construir_timeline_slides(
            dvis, 0, 40, [], {"origen": "vivo", "inicio": base_vis})
        check("slide progresiva conserva captura final con mayor texto",
              len(slides_prog) == 1
              and slides_prog[0]["archivo"] == "prog_3.jpg"
              and slides_prog[0].get("representante") == "captura_mas_completa_o_final",
              f"-> {slides_prog}")

        for nombre, t in [("stable_a.jpg", 70), ("stable_b.jpg", 95)]:
            guardar_img(dvis, nombre, "gray")
            estado.anexar_jsonl(dvis / "img_log.jsonl",
                                {"t": base_vis + t, "archivo": nombre,
                                 "dhash": "2222222222222222", "ocr": ""})
        slides_stable = post_procesar.construir_timeline_slides(
            dvis, 60, 120, [], {"origen": "vivo", "inicio": base_vis})
        sel_stable = post_procesar.seleccionar_slides_por_tema(
            slides_stable, [], presupuesto=1, tema_actual="visual theory")
        check("pantalla plana sin texto ni audio no se protege por mera estabilidad",
              sel_stable
              and not any(s.get("protegida_estudio") for s in sel_stable),
              f"-> slides={slides_stable}, sel={sel_stable}")

        from PIL import ImageDraw as PILImageDraw
        diagrama = PILImage.new("RGB", (320, 180), "white")
        draw_diag = PILImageDraw.Draw(diagrama)
        for x, y, label in [(50, 80, "x"), (160, 40, "h"), (160, 120, "h"), (270, 80, "y")]:
            draw_diag.ellipse((x - 18, y - 18, x + 18, y + 18), outline="black", width=4)
            draw_diag.text((x - 4, y - 8), label, fill="black")
        draw_diag.line((68, 80, 142, 40), fill="black", width=4)
        draw_diag.line((68, 80, 142, 120), fill="black", width=4)
        draw_diag.line((178, 40, 252, 80), fill="black", width=4)
        draw_diag.line((178, 120, 252, 80), fill="black", width=4)
        diagrama.save(dvis / "img" / "diagrama.jpg")
        estado.anexar_jsonl(dvis / "img_log.jsonl",
                            {"t": base_vis + 150, "archivo": "diagrama.jpg",
                             "dhash": "3333333333333333", "ocr": ""})
        segs_diagrama = [{
            "t": base_vis + 145, "fin": base_vis + 165, "parte": 1,
            "texto": "El profesor explica el diagrama de una red neuronal con capas "
                     "ocultas y conexiones entre nodos.",
        }]
        slides_diagrama = post_procesar.construir_timeline_slides(
            dvis, base_vis + 140, base_vis + 170, segs_diagrama,
            {"origen": "vivo", "inicio": base_vis})
        sel_diagrama = post_procesar.seleccionar_slides_por_tema(
            slides_diagrama, segs_diagrama, presupuesto=1, tema_actual="red neuronal")
        check("diagrama sin OCR se protege por clasificador visual local",
              sel_diagrama
              and sel_diagrama[0].get("visual_tipo") == "diagrama_o_grafico_probable"
              and sel_diagrama[0].get("protegida_estudio"),
              f"-> slides={slides_diagrama}, sel={sel_diagrama}")

        frames = []
        for i in range(40):
            frames.append({
                "archivo": f"frame_{i:02d}.jpg", "t_rel": float(i * 2),
                "ocr": "" if i % 5 else "video frame transition",
                "dhash": f"{i + 10:016x}",
                "transcript_ventana": "El profesor explica una animacion con movimiento.",
                "motivo": "frame_video",
                "grupo_total": 1,
            })
        sel_frames = post_procesar.seleccionar_slides_por_tema(
            frames, "El profesor explica una animacion con movimiento.",
            presupuesto=8, tema_actual="animacion")
        check("video o animación se resume en pocos frames informativos",
              1 <= len(sel_frames) <= 8 and len(sel_frames) < len(frames),
              f"-> {len(sel_frames)}")

    with tempfile.TemporaryDirectory() as tmp7:
        tmp7 = Path(tmp7)
        config.CONTENIDO_DIR = tmp7 / "contenido"
        config.PUNTERO_CLASE = tmp7 / "clase_actual.txt"
        d7 = estado.crear_clase("Cobertura v72", curso="curso-v72")
        base = fijar_rangos_clase(d7, base=7000.0, cortes=(), fin=800.0)
        for nombre, color in [("usada.jpg", "white"), ("gap.jpg", "gray"),
                              ("sobra_fuera.jpg", "black")]:
            guardar_img(d7, nombre, color)
        for obj in [
            {"t": base + 1, "fin": base + 5, "parte": 1,
             "texto": "Inicio de la clase con contexto."},
            {"t": base + 610, "fin": base + 615, "parte": 1,
             "texto": "La clase retoma con conclusiones."},
        ]:
            estado.anexar_jsonl(d7 / "transcript.jsonl", obj)
        for obj in [
            {"t": base + 20, "archivo": "usada.jpg", "dhash": "0000000000000001"},
            {"t": base + 300, "archivo": "gap.jpg", "dhash": "0000000000000002"},
            {"t": base + 620, "archivo": "sobra_fuera.jpg", "dhash": "0000000000000003"},
        ]:
            estado.anexar_jsonl(d7 / "img_log.jsonl", obj)
        guardar_wav_test(d7 / "audio" / f"chunk_{int(base + 300):012d}_p1_v10.wav", amp=7000)
        (d7 / "leccion_final.md").write_text(
            "# Final\n\n<img src=\"img/usada.jpg\" width=\"420\">\n", encoding="utf-8")
        cobertura = post_procesar.analizar_cobertura_audio(
            d7, estado.leer_jsonl(d7 / "transcript.jsonl"), "vivo",
            estado.leer_jsonl(d7 / "img_log.jsonl"))
        check("gap largo con WAV util advierte",
              cobertura["cobertura_estado"] == "dudosa"
              and not cobertura["limpieza_segura"]
              and cobertura["wav_con_rms_util"] == 1,
              f"-> {cobertura}")
        post_procesar.limpiar(d7, cobertura=cobertura)
        check("gap largo sin audio no limpia WAV automaticamente",
              any((d7 / "audio").glob("*.wav")))
        check("imagen de gap sin audio queda protegida",
              (d7 / "img" / "gap.jpg").is_file())
        check("imagen no referenciada fuera de gap se elimina",
              not (d7 / "img" / "sobra_fuera.jpg").exists())

    with tempfile.TemporaryDirectory() as tmp8:
        tmp8 = Path(tmp8)
        config.CONTENIDO_DIR = tmp8 / "contenido"
        config.PUNTERO_CLASE = tmp8 / "clase_actual.txt"
        d8 = estado.crear_clase("Map reduce v72", curso="curso-v72")
        base = fijar_rangos_clase(d8, base=9000.0, cortes=(), fin=1400.0)
        for t, tema in [(10, "TEMA_A neurona biologica"),
                        (470, "TEMA_B potencial de accion"),
                        (930, "TEMA_C perceptron y pesos")]:
            estado.anexar_jsonl(d8 / "transcript.jsonl",
                                {"t": base + t, "fin": base + t + 5,
                                 "parte": 1, "texto": tema * 8})
        preguntar_orig = post_procesar.preguntar
        old_min = post_procesar.POST_CHUNK_MIN_SEG
        old_max = post_procesar.POST_CHUNK_MAX_SEG
        old_chars = post_procesar.POST_CHUNK_MAX_CHARS
        llamadas_mr = []

        def fake_map_reduce(msgs, max_tokens=0, etiqueta="", **_kwargs):
            user_msg = msgs[-1]["content"] if msgs else ""
            llamadas_mr.append((etiqueta, user_msg))
            if etiqueta.startswith("post-chunk"):
                if "TEMA_A" in user_msg:
                    return "## Guion A\nTEMA_A detallado"
                if "TEMA_B" in user_msg:
                    return "## Guion B\nTEMA_B detallado"
                if "TEMA_C" in user_msg:
                    return "## Guion C\nTEMA_C detallado"
            if etiqueta == "post-merge-parte1":
                ok = all(x in user_msg for x in ("TEMA_A", "TEMA_B", "TEMA_C"))
                return "## Leccion integrada\nTEMA_A\nTEMA_B\nTEMA_C" if ok else ""
            if etiqueta == "post-resumen":
                return "Resumen integrado de TEMA_A, TEMA_B y TEMA_C."
            if etiqueta == "post-tareas":
                return "SIN_TAREAS"
            return ""

        try:
            post_procesar.preguntar = fake_map_reduce
            post_procesar.POST_CHUNK_MIN_SEG = 1
            post_procesar.POST_CHUNK_MAX_SEG = 300
            post_procesar.POST_CHUNK_MAX_CHARS = 10000
            post_procesar.consolidar_todo(d8, forzar=True, limpiar_al_final=True)
        finally:
            post_procesar.preguntar = preguntar_orig
            post_procesar.POST_CHUNK_MIN_SEG = old_min
            post_procesar.POST_CHUNK_MAX_SEG = old_max
            post_procesar.POST_CHUNK_MAX_CHARS = old_chars
        final8 = (d8 / "leccion_final.md").read_text(encoding="utf-8")
        check("map-reduce: 3 chunks -> 3 guiones -> merge con 3 temas",
              sum(1 for e, _p in llamadas_mr if e.startswith("post-chunk")) == 3
              and any(e == "post-merge-parte1" for e, _p in llamadas_mr)
              and all(x in final8 for x in ("TEMA_A", "TEMA_B", "TEMA_C")),
              f"-> llamadas={llamadas_mr}, final={final8[:300]}")

    print("== test_smoke: dhash ==")
    from PIL import Image
    horizontal = Image.new("L", (64, 64))
    horizontal.putdata([255 - x * 4 for y in range(64) for x in range(64)])  # claro->oscuro (bits=1)
    vertical = Image.new("L", (64, 64))
    vertical.putdata([y * 4 for y in range(64) for x in range(64)])          # filas constantes (bits=0)
    h1, h2 = imagen.dhash(horizontal), imagen.dhash(vertical)
    check("dhash consigo misma = 0", imagen.distancia(h1, imagen.dhash(horizontal)) == 0)
    check("dhash distinta > umbral", imagen.distancia(h1, h2) > 10,
          f"-> distancia {imagen.distancia(h1, h2)}")

    print("== test_smoke: escriba (lógica pura) ==")
    links = extraer_links("ver https://arxiv.org/abs/1706.03762, y https://arxiv.org/abs/1706.03762")
    check("extraer_links dedup", links == ["https://arxiv.org/abs/1706.03762"], f"-> {links}")
    bloque = construir_bloque("17:00–17:03", 1, ["cap_1.png"], "### 📌 Resumen\n- x",
                              ["https://unsa.edu.pe"])
    check("construir_bloque imagen", "![captura](img/cap_1.png)" in bloque)
    check("construir_bloque links extra", "<https://unsa.edu.pe>" in bloque)
    check("construir_bloque encabezado", "## ⏱️ 17:00–17:03 · Parte 1" in bloque)
    limpiar_buscar_orig = escriba._quitar_buscar_y_enriquecer
    detalle_orig = escriba.preguntar_detalle
    escriba_web_orig = os.environ.get("CLASE_ESCRIBA_WEB")
    try:
        escriba._quitar_buscar_y_enriquecer = (
            lambda cuerpo: cuerpo.split("BUSCAR:", 1)[0].strip())
        llamadas = []

        def fake_truncado(_msgs, max_tokens=0, etiqueta="", **_kwargs):
            llamadas.append(etiqueta)
            if len(llamadas) == 1:
                return {"texto": "### Apuntes\n- La corteza visual prim",
                        "finish_reason": "length", "error": None}
            return {"texto": (
                        "### Panorama acumulado de la clase\nLa corteza visual primaria "
                        "procesa información visual.\n\n"
                        "### Desarrollo detallado del tramo actual\nProcesa bordes y "
                        "orientación.\n\n"
                        "### Contexto acumulado para continuar\nLa orientación es una "
                        "característica visual.\nBUSCAR: primary visual cortex"),
                    "finish_reason": "stop", "error": None}

        escriba.preguntar_detalle = fake_truncado
        res_trunc = escriba.generar_resumen_en_vivo(
            "La corteza visual primaria procesa bordes y orientación. " * 30,
            "", "00:00-00:10")
        check("escriba finish_reason length pide continuación una vez",
              res_trunc["estado"] == "generado" and len(llamadas) == 2
              and "corteza visual primaria" in res_trunc["cuerpo"]
              and "orientación" in res_trunc["cuerpo"],
              f"-> llamadas={llamadas}, res={res_trunc}")

        llamadas = []

        def fake_cont_falla(_msgs, max_tokens=0, etiqueta="", **_kwargs):
            llamadas.append(etiqueta)
            if len(llamadas) == 1:
                return {"texto": "### Apuntes\n- Potencial de acción incom",
                        "finish_reason": "length", "error": None}
            return {"texto": None, "finish_reason": None, "error": "timeout"}

        escriba.preguntar_detalle = fake_cont_falla
        transcript_raw = "El potencial de acción tiene despolarización y repolarización."
        res_parcial = escriba.generar_resumen_en_vivo(transcript_raw * 8, "", "00:00-00:10")
        check("escriba continuación fallida preserva bloque parcial y transcript crudo",
              res_parcial["estado"] == "parcial"
              and "Resumen parcial" in res_parcial["cuerpo"]
              and "Transcripción cruda preservada" in res_parcial["cuerpo"]
              and "despolarización" in res_parcial["cuerpo"],
              f"-> {res_parcial}")

        llamadas = []
        os.environ["CLASE_ESCRIBA_WEB"] = "1"

        def fake_sin_buscar(_msgs, max_tokens=0, etiqueta="", **_kwargs):
            llamadas.append(etiqueta)
            if len(llamadas) == 1:
                return {"texto": "### Apuntes\nLa sinapsis química libera neurotransmisores.",
                        "finish_reason": "stop", "error": None}
            return {"texto": (
                        "### Panorama acumulado de la clase\nLa sinapsis química "
                        "transmite señales.\n\n"
                        "### Desarrollo detallado del tramo actual\nLibera "
                        "neurotransmisores en la hendidura sináptica.\n\n"
                        "### Contexto acumulado para continuar\nLos receptores reciben "
                        "la señal.\nBUSCAR: chemical synapse neurotransmitters"),
                    "finish_reason": "stop", "error": None}

        escriba.preguntar_detalle = fake_sin_buscar
        res_buscar = escriba.generar_resumen_en_vivo(
            "La sinapsis química libera neurotransmisores. " * 20, "", "00:00-00:10")
        check("escriba falta BUSCAR se considera incompleto y se corrige",
              res_buscar["estado"] == "generado" and len(llamadas) == 2
              and "sinapsis química" in res_buscar["cuerpo"],
              f"-> llamadas={llamadas}, res={res_buscar}")
        dres = estado.crear_clase("Resumen acumulado", curso="curso-test")
        estado.anexar_jsonl(dres / "resumen_tramos.jsonl", {
            "t_ini": 10, "t_fin": 20, "rango": "00:10-00:20", "parte": 1,
            "imagenes": ["cap1.jpg"], "cuerpo": "### Apuntes\nTema A\nBUSCAR: topic a",
        })
        estado.anexar_jsonl(dres / "resumen_tramos.jsonl", {
            "t_ini": 30, "t_fin": 40, "rango": "00:30-00:40", "parte": 2,
            "imagenes": [], "cuerpo": "### Apuntes\nTema B",
        })
        acum = escriba.resumenes_acumulados_md(dres, hasta=25, parte=1)
        check("escriba arma contexto acumulado por parte y tiempo",
              "Tema A" in acum and "cap1.jpg" in acum and "Tema B" not in acum
              and "BUSCAR:" not in acum,
              f"-> {acum}")
    finally:
        if escriba_web_orig is None:
            os.environ.pop("CLASE_ESCRIBA_WEB", None)
        else:
            os.environ["CLASE_ESCRIBA_WEB"] = escriba_web_orig
        escriba._quitar_buscar_y_enriquecer = limpiar_buscar_orig
        escriba.preguntar_detalle = detalle_orig

    print("== test_smoke: heurística de código ==")
    check("detecta código", parece_codigo("def f(x):\n  return x*2\nprint(f(2))"))
    check("no detecta prosa", not parece_codigo("la sigmoide es una función suave"))

    print("== test_smoke: transcriptor anti-bucles ==")
    from src import transcriptor_vivo
    check("cadena remota cubre x1/x2/x3 sin cambiar proveedor por velocidad",
          transcriptor_vivo.modelo_para_velocidad(1.0) == "remote-failover"
          and transcriptor_vivo.modelo_para_velocidad(2.0) == "remote-failover"
          and transcriptor_vivo.modelo_para_velocidad(3.0) == "remote-failover")
    diag_clase = transcriptor_vivo.diagnosticar_repeticion_lineas([
        {"texto": "Clase 3."}, {"texto": "Clase 4."}, {"texto": "Clase 5."},
    ])
    check("transcriptor detecta bucle Clase N",
          diag_clase["sospechoso"] and diag_clase["clase_n"] == 3,
          f"-> {diag_clase}")
    diag_repite = transcriptor_vivo.diagnosticar_repeticion_lineas([
        {"texto": "contenido útil sobre redes neuronales"},
        {"texto": "sí"}, {"texto": "sí"}, {"texto": "sí"},
    ])
    check("chunk con frase corta repetida queda sospechoso",
          diag_repite["sospechoso"] and "frase_corta_repetida" in diag_repite["razones"],
          f"-> {diag_repite}")
    check("metadata no_speech alto y baja confianza descarta segmento",
          transcriptor_vivo.segmento_debe_descartarse(
              "ruido probable", avg_logprob=-1.4, no_speech_prob=0.95,
              compression_ratio=1.0))
    check("metadata confiable no descarta segmento útil",
          not transcriptor_vivo.segmento_debe_descartarse(
              "El profesor explica gradiente descendente.",
              avg_logprob=-0.2, no_speech_prob=0.05, compression_ratio=1.1))
    llamadas_retry = []

    def base_loop():
        llamadas_retry.append("base")
        return [{"texto": "Clase 3."}, {"texto": "Clase 4."}, {"texto": "Clase 5."}]

    def retry_ok():
        llamadas_retry.append("retry")
        return [{"texto": "El profesor explica inteligencia artificial y deep learning."}]

    res_retry, diag_retry = transcriptor_vivo.transcribir_con_reintento(base_loop, retry_ok)
    check("respuesta sospechosa se filtra sin segunda llamada facturable",
          llamadas_retry == ["base"] and not diag_retry["retry"]
          and diag_retry["filtrado"] and not diag_retry["sospechoso"]
          and res_retry == [],
          f"-> llamadas={llamadas_retry}, diag={diag_retry}, res={res_retry}")
    segs_post_loop = [
        {"chunk": "chunk_x.wav", "texto": "Contenido útil inicial."},
        {"chunk": "chunk_x.wav", "texto": "Clase 3."},
        {"chunk": "chunk_x.wav", "texto": "Clase 4."},
        {"chunk": "chunk_x.wav", "texto": "Clase 5."},
    ]
    rep_post_loop = post_procesar.analizar_transcript_sospechoso(segs_post_loop)
    filtrado_post_loop = post_procesar.filtrar_transcript_sospechoso(segs_post_loop)
    check("postproceso separa transcript sospechoso",
          rep_post_loop["sospechoso"] and len(filtrado_post_loop) == 1
          and filtrado_post_loop[0]["texto"].startswith("Contenido útil"),
          f"-> rep={rep_post_loop}, filtrado={filtrado_post_loop}")

    print("== test_smoke: detección de silencio (RMS) ==")
    import struct
    import wave
    from src.captura_audio import calcular_rms, metricas_wav
    silencio = b"\x00\x00" * 2048
    voz = struct.pack("<2048h", *([6000, -6000] * 1024))
    check("silencio -> rms 0", calcular_rms(silencio) == 0.0)
    check("audio -> rms alto", calcular_rms(voz) > config.SILENCIO_RMS,
          f"-> rms={calcular_rms(voz):.0f}")
    with tempfile.TemporaryDirectory() as tmp_wav:
        wav_sil = Path(tmp_wav) / "silencio.wav"
        with wave.open(str(wav_sil), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 32000)
        met_sil = metricas_wav(wav_sil)
        check("parser metricas WAV silencioso",
              met_sil["duration_sec"] == 2.0 and met_sil["rms"] == 0.0
              and met_sil["silent_window_pct"] == 100.0
              and not met_sil["useful_audio"],
              f"-> {met_sil}")
        check("fixture WAV no escribe en contenido real",
              not str(wav_sil.resolve()).startswith(str(config.CONTENIDO_DIR.resolve())),
              f"-> {wav_sil}")

    print("== test_smoke: WSOLA y panel ==")
    import numpy as np
    from src.compat_av import cambiar_tempo_wsola, estirar_wsola
    from src.panel import a_html, leccion_vivo
    x = np.sin(np.linspace(0, 80, 16000, dtype=np.float32))
    y = estirar_wsola(x, 2.0)
    check("WSOLA estira cerca de 2x", 1.8 <= len(y) / len(x) <= 2.1,
          f"-> ratio={len(y) / len(x):.2f}")
    z = cambiar_tempo_wsola(x, 0.5)
    check("WSOLA comprime cerca de 0.5x", 0.4 <= len(z) / len(x) <= 0.6,
          f"-> ratio={len(z) / len(x):.2f}")
    check("WSOLA finito", bool(np.isfinite(y).all()))
    check("markdown chat renderiza negrita", "<strong>Detalle:" in a_html("**Detalle:** ok"))
    vivo = "## ⏱️ 10:00 · Parte 1\n![x](img/a.jpg)\nuno\n---\n## ⏱️ 10:02 · Parte 2\n<img src='x'>\ndos"
    limpio = leccion_vivo(vivo, 2)
    check("leccion_vivo filtra parte actual", "dos" in limpio and "uno" not in limpio)
    check("leccion_vivo elimina imagenes", "img/" not in limpio and "<img" not in limpio)

    print()
    if FALLOS:
        print(f"SMOKE_FALLO: {len(FALLOS)} pruebas fallaron: {FALLOS}")
        smoke_sandbox.cleanup()
        return 1
    print("SMOKE_OK: todas las pruebas pasaron")
    smoke_sandbox.cleanup()
    return 0


if __name__ == "__main__":
    try:
        _exit_code = main()
    finally:
        from src import ocr
        ocr.cerrar_motor()
    sys.exit(_exit_code)

