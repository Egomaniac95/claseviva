"""POST-PROCESO: convierte lo capturado en la lección final ordenada + memoria del curso.

Modos:
  python -m src.post_procesar --parte 1          # consolida SOLO la parte 1 (útil en recesos)
  python -m src.post_procesar --todo             # todas las partes + leccion_final.md + resumen
  python -m src.post_procesar --video ruta.mp4 --nombre "Semana 5"
                                                  # ingesta una grabación (p. ej. la de Meet
                                                  #  guardada en Calendar/Drive) sin haber
                                                  #  corrido el pipeline en vivo

Qué hace por parte: filtra transcript por rango de tiempo, lo trocea, pide a DeepSeek que
REORDENE en secciones temáticas (separando digresiones en 'Fuera de guion'), y le intercala
las diapositivas ÚNICAS de cada tramo. Conserva evidencia salvo --limpiar explícito.
"""
import argparse
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import unicodedata
import wave
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageFilter, ImageStat

from . import audio_manifest
from . import config
from . import costos_api
from . import estado as est
from . import imagen
from . import incremental_jsonl
from . import math_render
from . import study_package
from . import student_notes
from . import transcript_quality
from . import visual_materials
from .deepseek_client import preguntar
from .transcriptor_vivo import (
    diagnosticar_repeticion_lineas,
    filtrar_lineas_repetitivas,
)

MAX_CHARS_TROZO = int(os.environ.get("CLASE_POST_TROZO_CHARS", "40000"))
POST_CHUNK_MIN_SEG = int(os.environ.get("CLASE_POST_CHUNK_MIN_SEG", "480"))
POST_CHUNK_MAX_SEG = int(os.environ.get("CLASE_POST_CHUNK_MAX_SEG", "900"))
POST_CHUNK_MAX_CHARS = int(os.environ.get("CLASE_POST_CHUNK_CHARS",
                                           str(MAX_CHARS_TROZO)))
POST_MAX_TOKENS = int(os.environ.get("CLASE_POST_MAX_TOKENS", "12000"))
UMBRAL_DEDUP = 6
GAP_LARGO_SEG = 180
TRANSCRIPT_CERCA_WAV_SEG = 90
OCR_MAX_CHARS = 700
SLIDE_PRE_SEG = float(os.environ.get("CLASE_POST_SLIDE_PRE_SEG", "20"))
SLIDE_POST_SEG = float(os.environ.get("CLASE_POST_SLIDE_POST_SEG", "90"))
POST_VERSION = "post_v32_remote_stt_math_notes"
SLIDE_GUIDED_VERSION = "slide_guided_v30_grounded_visual_context"
MERGE_VERSION = "merge_v4_concept_map_and_math_contract"
SUMMARY_VERSION = "summary_v3_balanced_integrated_lesson"
OCR_CACHE_VERSION = "full_frame_v1"
_OCR_CACHE_BY_CLASS: dict[str, dict[tuple[str, str, str], str]] = {}


def _lineas_gasto_api(resumen: dict) -> list[str]:
    return [
        "[post] gasto API en esta sesión: "
        f"${float(resumen.get('clase_actual_usd', 0.0)):.4f} USD",
        "[post] gasto API en este curso hasta ahora: "
        f"${float(resumen.get('total_usd', 0.0)):.4f} USD",
    ]

TERMINOS_PUBLICIDAD = (
    "compra", "comprar", "descuento", "oferta", "promoción", "promocion",
    "suscríbete", "suscribete", "suscríbase", "suscribase", "dale like",
    "patrocinado", "sponsor", "código promocional", "codigo promocional",
    "link promocional", "instalar app", "instala la app", "descarga la app",
    "envío gratis", "envio gratis", "black friday", "cupón", "cupon",
    "aprovecha", "haz clic en el enlace", "mi canal", "activa la campana",
)

TERMINOS_CLASE = (
    "profesor", "profesora", "docente", "clase", "curso", "investigación",
    "investigacion", "paper", "artículo", "articulo", "webinar", "discusión",
    "discusion", "negocio", "ventas", "empresa", "caso", "modelo", "tarea",
    "examen", "proyecto", "método", "metodo", "experimento", "resultado",
)

PROMPT_REORDEN = (
    "Recibes la transcripción cronológica (con marcas de hora) de un tramo de una clase de "
    "maestría en IA dictada en español. El profesor a veces salta de tema y vuelve después. "
    "Tu trabajo: REORGANIZAR el contenido en Markdown de estudio:\n"
    "- '## <tema>' por cada tema principal (agrupa lo disperso del mismo tema).\n"
    "- Conserva TODA la información técnica: definiciones, fórmulas, ejemplos, cifras.\n"
    "- Si se dictó código: bloque ``` con el código reconstruido + explicación de su lógica.\n"
    "- '### 🔗 Recursos' con URLs/papers/libros mencionados.\n"
    "- '### 🔀 Fuera de guion' al final con las digresiones del PROFESOR (anécdotas, avisos).\n"
    "- ⚠️ NO descartes cambios de tema: la clase puede tener VARIOS profesores y VARIOS temas "
    "(anatomía, luego un estudio clínico, luego dolor lumbar, etc.). TODOS son contenido "
    "legítimo: desarróllalos por igual, cada uno con su '## <tema>'. Solo cuenta como "
    "contaminación el audio CLARAMENTE de otra fuente de entretenimiento sonando a la vez "
    "(outros de YouTube 'suscríbete/dale like/nos vemos en el próximo video', publicidad, "
    "música con letra). Ante la duda, INCLÚYELO. Lo inequívocamente ajeno va a "
    "'### 🗑️ Posible contaminación de audio (revisar)' con su hora. "
    "También corrige palabras mal transcritas por audio acelerado cuando el contexto lo "
    "haga obvio.\n"
    "- No inventes contenido ni agregues opiniones. Español, tono de apunte de clase."
)

PROMPT_RESUMEN = (
    "Redacta el RESUMEN EJECUTIVO DE CLASE (max. 500 palabras) en espanol a partir de la "
    "leccion_final ya integrada, no desde un chunk aislado. Estructura: 1) parrafo de que "
    "se trato; 2) lista '### Temas' con vinetas; 3) '### Ideas clave para el examen' "
    "(3-8 vinetas); 4) dudas o limitaciones si la cobertura lo indica. Este resumen servira "
    "de memoria para entender clases siguientes, se preciso con terminos tecnicos. NO ignores "
    "cambios de profesor, tema, webinar, discusion, negocios o investigaciones: si estan en "
    "la leccion integrada, son parte de la memoria del curso. Mantén términos técnicos "
    "bilingües cuando la clase los use en inglés, por ejemplo redes neuronales (neural "
    "networks), descenso de gradiente (gradient descent) o pérdida (loss). No repitas el "
    "titulo 'Resumen ejecutivo'; devuelve solo el contenido interno. Corrige ortografía, "
    "gramática y errores obvios del STT. Antes de fijar cifras, fórmulas o nombres, comprueba "
    "su coherencia con el desarrollo y la evidencia visual legible; si las fuentes discrepan, "
    "declara la discrepancia en vez de escoger silenciosamente. En una reanudacion, fusiona "
    "los temas ya publicados con el contenido nuevo: el resultado debe representar TODA la "
    "clase. No uses 'primera parte', 'segunda parte' ni el bloque de grabacion como estructura "
    "pedagogica, salvo que la propia clase declare una ruptura tematica real. Cierra todas "
    "las frases y listas; no abras un punto que no puedas completar."
)

PROMPT_TAREAS = (
    "De estos apuntes de clase, extrae TODAS las tareas o encargos dejados por el profesor: "
    "deberes, entregables, lecturas, 'para la próxima clase', formato de entrega (p. ej. PDF), "
    "y ARCHIVOS que el estudiante debe descargar (notebooks .ipynb, datasets, slides, papers). "
    "Devuelve SOLO Markdown con este formato:\n"
    "- [ ] <tarea> — <detalle; fecha límite si se mencionó; archivos necesarios>\n"
    "Si NO se dejó ninguna tarea, responde exactamente: SIN_TAREAS"
)


PROMPT_TEJIDO = (
    "Recibes la transcripción cronológica de un tramo de una clase de maestría en IA (en "
    "español, con marcas de hora) y FICHAS de las diapositivas/pantallas capturadas (archivo, "
    "hora, texto OCR). Las fichas ya fueron filtradas: son apoyo/evidencia, no la fuente "
    "dominante. Redacta el APUNTE DETALLADO, TEJIDO Y EXHAUSTIVO del tramo en Markdown:\n"
    "- Una línea marcada [INTERVENCIÓN DEL ESTUDIANTE] pertenece al usuario local, no "
    "al profesor. Intégrala donde da contexto, atribúyela con claridad y nunca mezcles "
    "ambas voces.\n"
    "- Objetivo principal: reconstruir TODO lo que enseñaron el profesor o profesores, con "
    "suficiente detalle para estudiar sin volver al video. NO hagas un resumen corto. "
    "Conserva temas, subtemas, definiciones, pasos, argumentos, ejemplos, casos, "
    "aplicaciones, advertencias, preguntas/respuestas relevantes, tareas, fórmulas, "
    "intuiciones, nombres de archivos, comandos y decisiones metodológicas.\n"
    "- Limpia SOLO el ruido del habla: repeticiones vacías, muletillas y frases truncas. Si el "
    "profesor se corrige, integra la versión corregida y explica la corrección cuando aporte "
    "valor. Si hay ambigüedad, deja una nota breve con la marca de hora.\n"
    "- ESTRUCTURA TEJIDA: '## <tema>' y párrafos de teoría; cuando el contenido corresponda a "
    "una diapositiva, inserta en su PROPIA línea el marcador con el formato EXACTO\n"
    "[[IMG:archivo.jpg|lectura guiada de 3-5 frases: qué se ve, cómo se interpreta, "
    "cómo se conecta con la explicación y por qué importa]]\n"
    "y DEBAJO continúa AHONDANDO en la teoría de ese tema (definiciones, intuición, fórmulas, "
    "ejemplos, por qué importa). Alterna así: teoría → imagen → más teoría → siguiente "
    "imagen → más teoría...\n"
    "- Usa SOLO archivos que estén en las fichas; no inventes marcadores. Cada marcador de "
    "imagen debe explicar una idea concreta de la clase. Si una pantalla no aporta una idea "
    "útil, no la insertes.\n"
    "- Basa la lección principalmente en la TRANSCRIPCIÓN. Usa las imágenes solo cuando "
    "aporten evidencia visual de una idea concreta; no conviertas OCR/capturas en fuente "
    "principal si el audio no lo respalda. No crees secciones tipo 'Otras pantallas'.\n"
    "- Enriquece con conocimiento técnico estable cuando ayude a completar un salto del "
    "profesor o corregir una omisión evidente, marcándolo como '**Ampliación técnica:**'. "
    "No afirmes datos actuales o externos no presentes en el contexto como si hubieran sido "
    "navegados en tiempo real.\n"
    "- Mantén los términos técnicos en inglés con su aclaración en español: "
    "**MSE** (*Mean Squared Error* — error cuadrático medio).\n"
    "- ⚠️ IMPORTANTE — NO descartes cambios de tema: la clase PUEDE tener VARIOS profesores, "
    "varias investigaciones, webinar, discusión, ventas, negocios o temas no relacionados. "
    "TODO cambio de tema, aunque sea abrupto o parezca inconexo, es contenido LEGÍTIMO de la "
    "clase y DEBES desarrollarlo con el mismo detalle, creando un nuevo '## <tema>'. NUNCA "
    "descartes contenido por 'no tener captura que lo respalde' — el audio manda.\n"
    "- Publicidad/ruido de YouTube: marca '### Posible pausa publicitaria' SOLO si coinciden "
    "varios criterios: tramo breve (<3 min), lenguaje comercial ('compra', 'descuento', "
    "'suscríbete', 'oferta', 'patrocinado', 'instalar app', 'link promocional'), cambio "
    "visual no relacionado, no se retoma como tema principal y aparece entre dos tramos "
    "coherentes de clase. Si hay duda, CONSÉRVALO como '### Intervención breve / posible "
    "digresión'. No elimines contenido conceptual.\n"
    "- Si la transcripción viene en inglés, redacta la lección final en español para estudio, "
    "conservando términos técnicos en inglés cuando ayude. Si el tramo es mixto, agrega la "
    "formulación bilingüe solo donde sea útil para estudiar.\n"
    "- Si el contexto indica audio faltante o transcripción insuficiente, dilo con claridad: "
    "'tramo sin audio/transcripción insuficiente'. Usa OCR/capturas solo como evidencia "
    "parcial y no reconstruyas toda la clase desde imágenes silenciosas.\n"
    "- Inserta SOLO imágenes seleccionadas con valor pedagógico y explica cada una. No "
    "impongas un máximo arbitrario: conserva todas las identidades visuales principales y "
    "las secuencias con progreso real; excluye duplicados, cursor/presentador y UI sin valor.\n"
    "- '### 🔗 Recursos' con URLs/papers mencionados; '### 🔀 Fuera de guion' con las "
    "digresiones del PROFESOR. No inventes contenido.\n"
    "AL FINAL, en una línea sola: BUSCAR: <3-5 palabras clave en inglés del tema central "
    "del tramo> (adjuntaremos artículos reales de la web)."
)
PROMPT_CHUNK_DETALLADO = (
    "Redacta apuntes detallados de este tramo de clase en Markdown. La fuente principal es "
    "la TRANSCRIPCION completa del rango; las imagenes y OCR son evidencia de apoyo. No "
    "atribuyas al profesor las líneas [INTERVENCIÓN DEL ESTUDIANTE]; son aportes locales "
    "que deben quedar identificados. "
    "resumas en exceso: conserva cronologia local, definiciones, pasos, ejemplos, analogias, "
    "nombres propios, cifras, formulas, codigo, preguntas, tareas y cambios de tema. "
    "Conserva cada detalle tecnico unico, pero colapsa replays, reformulaciones equivalentes "
    "y repeticiones sin informacion nueva. Usa marcadores de imagen solo "
    "cuando una ficha aporte evidencia real, con el formato exacto "
    "[[IMG:archivo.jpg|lectura guiada de 3-5 frases que explique la evidencia y su "
    "relación con el concepto]]. Si falta audio o hay un "
    "gap de cobertura, declaralo explicitamente y no reconstruyas una clase larga desde "
    "capturas silenciosas. No pegues bloques de transcript ni citas largas: parafrasea y "
    "corrige errores foneticos evidentes. Omite correos, telefonos, listas de participantes "
    "y nombres de estudiantes que no sean necesarios para comprender la leccion. No inventes "
    "contenido. Al final, opcionalmente, agrega una linea "
    "BUSCAR: <3-5 palabras clave en ingles> para fuentes ampliadas baratas."
)

PROMPT_SLIDE_GUIDED = (
    "Reconstruye SOLO este tramo como material de estudio detallado en Markdown. La fuente "
    "dominante y obligatoria es la TRANSCRIPCION: las slides/capturas solo ubican "
    "diapositivas, codigo, diagramas o evidencia visual. Para cada slide enviada, explica "
    "que muestra y que dijo el profesor en su transcript asociado. Formato preferido: "
    "imagen antes o junto a la explicacion, usando SOLO marcadores deterministas "
    "[[IMG:archivo.jpg|lectura guiada de 3-5 frases y aproximadamente 70-140 palabras: "
    "qué se ve, cómo debe leerse, cómo se conecta con la explicación y por qué importa]]. "
    "No inventes nombres de archivos. "
    "Agrupa marcadores equivalentes bajo un único concepto y conserva solo el estado que "
    "aporte información nueva; movimiento de presentador, cursor, resaltado o jitter de OCR "
    "no crea por sí solo una secuencia. No titules secciones como 'Slide N', 'Slides N, M', "
    "'Diapositiva N' o con nombres de archivo. El marcador debe empezar exactamente por "
    "'[[IMG:'; nunca uses blockquotes, admonitions como '> [!IMG:...]' ni otra sintaxis. "
    "Si el profesor se desvia dentro de una slide, agrega una subseccion 'Comentario o "
    "deriva del profesor'. Si cambia completamente de tema, profesor, webinar o video, "
    "crea una nueva seccion; no descartes cambios por parecer inconexos. Si una slide no "
    "tiene transcript cercano, marcala como EVIDENCIA VISUAL SIN AUDIO/TRANSCRIPCION "
    "CERCANA y no inventes clase desde la imagen. No crees galerias residuales tipo "
    "'otras pantallas'. Si aparecen Galvani, Volta, Frankenstein u otro video/profesor en "
    "el transcript, conservalo como nueva seccion; si no aparece en transcript, no lo "
    "reconstruyas desde memoria externa. Al final, opcionalmente agrega BUSCAR: <3-5 "
    "palabras clave en ingles>. No uses slides no incluidas en las fichas; no inventes "
    "visuales ni nombres de archivo. Usa cada slide pedagógicamente distinta como máximo "
    "una vez; no fuerces una sección por captura ni publiques estados de navegación "
    "equivalentes. La galería conserva la evidencia secundaria. Prioriza "
    "siempre el transcript. Desarrolla el contenido único del rango con profundidad: define "
    "cada concepto, explica el razonamiento y los pasos, recupera ejemplos y conecta causas "
    "con consecuencias. No repitas el panorama de tramos anteriores solo para dar contexto. "
    "Redacta con ortografía y gramática académicas: no copies errores "
    "fonéticos del STT ni basura del OCR. El OCR legible sí puede COMPLETAR nombres, fechas, "
    "fórmulas, listas o cifras visibles que el audio omitió; introdúcelo como 'La diapositiva "
    "añade…', sin atribuírselo a la voz del profesor. Si transcript y diapositiva contradicen "
    "una cifra o fórmula, conserva ambas con su procedencia y marca revisión. Ignora barras "
    "laterales, recomendaciones, controles, chat, cursor y texto periférico de una aplicación. "
    "Nunca uses como título o identidad un nombre que aparezca solo en ese texto periférico. "
    "No pegues bloques rotulados Transcript/Transcripcion ni citas largas del STT: integra "
    "su contenido mediante prosa academica corregida. No publiques correos, telefonos, "
    "listas de participantes ni identidades de estudiantes irrelevantes; sustituyelos por "
    "una referencia generica. Las afirmaciones empresariales, biograficas, estadisticas o "
    "actuales que no esten corroboradas deben quedar como 'Segun lo expuesto en clase' y "
    "pendientes de contraste, nunca como un hecho silenciosamente validado. Si el mensaje "
    "incluye fuentes externas, usalas solo para una 'Actualizacion externa' o una correccion "
    "explicita con URL; no las atribuyas al profesor. "
    "Usa iconos Unicode sobrios y pertinentes solo en encabezados o recuadros de estudio "
    "(por ejemplo 🧠 concepto, 🧮 fórmula, 💻 práctica, 🎯 repaso); no los uses "
    "como adorno repetitivo ni sustituyas explicaciones por iconos. Para cada marcador, "
    "escribe una lectura guiada autosuficiente y concreta: qué se ve, qué elementos debe "
    "seguir el estudiante, qué idea demuestra, cómo se conecta con el desarrollo y por qué "
    "importa. Evita captions de una sola oración. "
    "Integra la evidencia en prosa: nunca publiques rótulos como 'OCR de la diapositiva', "
    "'Lo que dijo el profesor', 'transcript asociado' o 'continuación sin diapositivas', "
    "'Qué muestra la diapositiva' o 'Qué dice el profesor', "
    "ni conviertas un marcador de imagen en encabezado. No copies OCR en bruto: corrige "
    "solo lo corroborado y descarta caracteres rotos, menús y texto periférico. No cierres "
    "el tramo con otro resumen; el panorama global se genera por separado. "
    "No afirmes que una persona trabaja 'actualmente' en una organización sin una fuente "
    "externa fechada; si procede del audio, atribúyelo al profesor y al momento de la grabación."
)

PROMPT_SLIDES_SUPLEMENTARIAS = (
    "Genera SOLO bloques visuales complementarios para las slides de esta tanda. No "
    "reescribas la leccion ni vuelvas a explicar todo el tramo. Para cada ficha devuelve "
    "exactamente una linea [[IMG:archivo.jpg|lectura visual fiel de 3-5 frases: evidencia, "
    "interpretación, conexión conceptual y utilidad para estudiar]] y, solo cuando haga "
    "falta, un parrafo breve inmediatamente "
    "debajo. Incluye todas las slides recibidas una sola vez, no inventes archivos y no "
    "agregues resumen, introduccion, guia de estudio, glosario ni transcripcion completa."
)

PROMPT_MERGE_LECCION = (
    "Recibes guiones textuales detallados de varios tramos de una clase. Construye el "
    "PANORAMA TEMATICO de una leccion clara y estudiable en Markdown. Despues de tu salida, "
    "el sistema anexara el desarrollo exhaustivo de cada tramo; por eso aqui debes reordenar "
    "por temas, explicar conexiones y eliminar duplicados sin volver a copiar cronologicamente "
    "cada parrafo. Conserva en el panorama toda informacion conceptual y tecnica necesaria "
    "para orientar la lectura de los desarrollos: "
    "definiciones, ejemplos, formulas, analogias, nombres, pasos, discusiones, tareas, "
    "preguntas, cambios de profesor, webinar, negocios, investigacion o discusion. "
    "Sintetiza EXCLUSIVAMENTE los guiones nuevos recibidos: no reconstruyas una portada, "
    "un resumen ejecutivo ni partes anteriores. Empieza directamente con encabezados `##`; "
    "no emitas ningun encabezado `#`, titulo de clase, metadata de generacion o bloque "
    "`Resumen ejecutivo`. Si una idea anterior solo aparece como contexto, no la repitas; "
    "mencionala en una frase puente unicamente cuando un guion nuevo la amplie o corrija. "
    "Organiza el aporte nuevo con encabezados tematicos reutilizables: el ensamblado final "
    "lo integrara en el panorama, la guia, el glosario, el desarrollo exhaustivo y la "
    "galeria globales. No uses 'Parte 1', 'Parte 2' o 'continuacion' como tema pedagogico. "
    "No emitas marcadores ni nombres de imagen: las evidencias visuales se insertaran "
    "una sola vez dentro de los desarrollos. Abre con `## 🗺️ Mapa conceptual de la sesión`: "
    "muestra relaciones mediante flechas `→`/`↔` y una tabla compacta con las columnas "
    "`Concepto | Problema que resuelve | Mecanismo | Ejemplo de clase`. Después crea un "
    "panorama por temas con definición, mecanismo paso a paso, ejemplo, conexión con el "
    "tema siguiente y por qué importa. Si hubo gaps o dudas de cobertura, anade una "
    "seccion de dudas/cobertura. "
    "La salida debe ser autocontenida y util para estudiar sin volver al video: explica "
    "definiciones, relaciones, procedimientos, ejemplos, formulas y por que importa cada "
    "idea, con profundidad proporcional al contenido unico; no la alargues con repeticiones, "
    "nombres internos de archivos, conteos de WAV/segmentos ni diagnosticos del pipeline. "
    "Corrige ortografía, gramática y puntuación del STT sin alterar el significado. No "
    "publiques frases truncas, OCR ilegible ni títulos técnicos por cada frame. Antes de "
    "redactar, cruza cifras, nombres, fórmulas y listas entre guiones, transcript asociado y "
    "texto visual: la diapositiva legible puede completar un detalle no captado por el STT, "
    "pero debe presentarse como evidencia visual; una contradicción debe quedar explícita. "
    "Distingue `Lo dicho en clase`, una `**Correccion contextual:**`, una "
    "`**Ampliacion tecnica:**` y una `**Actualizacion externa:**` respaldada por una fuente; "
    "nunca corrijas silenciosamente al profesor. El texto es la explicación principal; las "
    "imágenes se publicarán después como evidencia de una idea ya explicada. Usa iconos "
    "Unicode sobrios y pertinentes solo en encabezados o recuadros de estudio, nunca en cada "
    "párrafo. "
    "Escribe matemática inline solo como `\\(...\\)` y matemática en bloque solo como "
    "`\\[...\\]`; nunca uses `(y_i)` ni `[ fórmula ]` como delimitadores ambiguos. "
    "Un "
    "cambio de ponente no separa por si solo la leccion: organiza por ponente y subtema cuando "
    "todos pertenecen al contexto declarado. "
    "Incluye obligatoriamente una seccion '## Guia de estudio' con: conceptos que el alumno "
    "debe dominar, preguntas probables de examen y conexiones entre temas. Incluye una "
    "seccion '## Glosario bilingue y formulas' que preserve los terminos canonicos del "
    "profesor en ingles junto a su explicacion en espanol. Si aparecen, no omitas logistica "
    "del curso, politicas de colaboracion/IA, herramientas como PyTorch/autograd, historia "
    "del campo, costos computacionales/ambientales, arquitecturas, optimizacion, funciones "
    "de perdida, tensores/batches, generalizacion, transferencia y escalamiento. No inventes "
    "lo que no aparezca en transcript, pero si el transcript lo contiene aunque sea una vez, "
    "debe quedar recuperable en la leccion final. Cuando recibas FUENTES EXTERNAS, usa solo "
    "afirmaciones directamente respaldadas, cita su URL junto a la actualización y no "
    "presentes resultados del buscador como si fueran palabras del profesor. No copies "
    "bloques de transcript crudo ni datos personales de participantes. Las fuentes externas "
    "deben corregir o ampliar de forma explicita, sin borrar lo que se dijo en clase."
)

PROMPT_FINAL_ENG = (
    "You receive the Spanish study notes generated from a mostly English class. Produce "
    "a clear academic English version in Markdown. Preserve the same structure, tasks, "
    "technical detail, tables and every image reference exactly as written (do not duplicate "
    "or invent images). Remove Spanish-only wording unless it is part of a quote. Return "
    "Markdown only."
)
RE_MARCADOR = re.compile(
    r"\[\[IMG:([^\|\]\r\n]+?)(?:\|([^\r\n]*?))?\]\]"
)
RE_BUSCAR = re.compile(
    r"^[ \t]*(?:[^\w\r\n]+\s*)?\*{0,2}BUSCAR\s*:?\*{0,2}\s*:?\s*(.+)$",
    re.I | re.M,
)
RE_IMG_REF = re.compile(r"img/([\w.\-]+)")
RE_MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
RE_HTML_IMG = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.I)

TERMINOS_INTERFAZ_CLASEVIVA_FUERTES = (
    "claseviva", "localhost", "127.0.0.1", "consolidar seguro",
    "leccion en vivo", "post-proceso", "post proceso", "inicio de clase",
)
TERMINOS_INTERFAZ_CLASEVIVA_PANEL = (
    "controles de clase", "resumen", "copiloto", "consolidar",
    "transcripcion", "transcripción", "pausar", "seguir", "fin",
    "estado", "curso", "sesion", "sesión", "tema", "pantalla", "audio",
    "clase actual", "sin clase", "boton", "button", "chip",
)

EN_HINTS = {
    "the", "and", "of", "to", "in", "is", "are", "we", "this", "that", "with",
    "data", "model", "models", "learning", "training", "train", "test", "loss",
    "function", "gradient", "network", "neural", "dataset", "features",
    "classification", "regression", "accuracy", "prediction", "probability",
}
ES_HINTS = {
    "que", "para", "con", "una", "uno", "los", "las", "del", "como", "esta",
    "este", "clase", "profesor", "profesora", "modelo", "datos", "aprendizaje",
    "funcion", "perdida", "entrenamiento", "red", "redes", "ejemplo", "tema",
}


def rutas_curso(d: Path) -> dict[str, Path | str | bool]:
    """Rutas normalizadas contenido/<curso>/{clases,resumenes,CURSO.md,TAREAS.md}.

    Si la clase viene de una estructura antigua contenido/clases/<clase>, se conserva la
    lectura desde esa carpeta pero las salidas ligeras van al curso indicado en el estado
    (o al curso por defecto). No se migra ni borra legado automáticamente.
    """
    d = Path(d).resolve()
    st = est.leer_estado(d) if (d / "session_state.json").is_file() else {}
    content_root = Path(config.CONTENIDO_DIR).resolve()
    legacy = d.parent.name == "clases" and d.parent.parent == content_root
    curso = str(st.get("curso") or (config.CURSO_DEFAULT if legacy else est.curso_de(d)))
    curso = est._slug(curso)
    physical_course = d.parent.parent if d.parent.name == "clases" else None
    # Una copia A/B con la estructura canónica debe mantener TODAS sus salidas dentro de
    # esa copia. Resolver siempre por config.dir_curso escapaba silenciosamente al curso
    # real si session_state conservaba el mismo slug.
    if physical_course is not None and physical_course.name == curso:
        curso_dir = physical_course.resolve()
    else:
        curso_dir = config.dir_curso(curso).resolve()
    clases_dir = curso_dir / "clases"
    resumenes_dir = curso_dir / "resumenes"
    resumenes_dir.mkdir(parents=True, exist_ok=True)
    clases_dir.mkdir(parents=True, exist_ok=True)
    return {
        "curso": curso,
        "curso_dir": curso_dir,
        "clases_dir": clases_dir,
        "resumenes_dir": resumenes_dir,
        "curso_md": curso_dir / "CURSO.md",
        "tareas_md": curso_dir / "TAREAS.md",
        "legacy": legacy,
    }


def _notas_manuales_curso(texto: str) -> list[str]:
    notas = re.findall(r"(?m)^-\s*📝\s*\[[^\n]+\].*$", str(texto or ""))
    return list(dict.fromkeys(nota.strip() for nota in notas if nota.strip()))


def _quitar_bloques_clase_curso(texto: str, clase_id: str) -> str:
    patron = re.compile(
        rf"(?ms)^##[ \t]+{re.escape(str(clase_id))}"
        r"(?:[ \t]+\([^\n]*\))?[ \t]*\n.*?(?=^##[ \t]+|\Z)",
    )
    return patron.sub("", str(texto or ""))


def _contexto_curso_sin_clase_actual(texto: str, clase_id: str) -> str:
    notas = _notas_manuales_curso(texto)
    limpio = _quitar_bloques_clase_curso(texto, clase_id)
    limpio = re.sub(r"(?m)^-\s*📝\s*\[[^\n]+\].*\n?", "", limpio).strip()
    if notas:
        limpio = (limpio + "\n\n## Notas manuales del curso\n\n"
                  + "\n".join(notas)).strip()
    return re.sub(r"\n{3,}", "\n\n", limpio)


def _escribir_texto_atomico(path: Path, texto: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporal = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporal, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(texto)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporal, path)
    finally:
        temporal.unlink(missing_ok=True)


def _actualizar_bitacora_curso(
        d: Path, path: Path, clase_id: str, fecha: str,
        idioma: str, resumen: str) -> None:
    with est._bloqueo_estado(d):
        existente = path.read_text(encoding="utf-8") if path.is_file() else (
            "# Bitacora del curso\n\n"
            "> Se actualiza sola al consolidar cada clase; puedes anadir notas desde "
            "el panel.\n"
        )
        notas = _notas_manuales_curso(existente)
        base = _quitar_bloques_clase_curso(existente, clase_id)
        base = re.sub(r"(?m)^-\s*📝\s*\[[^\n]+\].*\n?", "", base).rstrip()
        bloque = (
            f"## {clase_id} ({fecha})\n"
            f"- Idioma detectado: {idioma}\n\n{resumen.strip()}"
        )
        piezas = [base, bloque]
        if notas:
            piezas.append("\n".join(notas))
        _escribir_texto_atomico(path, "\n\n".join(
            pieza.strip() for pieza in piezas if pieza.strip()) + "\n")


def _reporte_parte_path(d: Path, parte: int) -> Path:
    return Path(d) / "_debug" / f"postprocess_part_{int(parte):02d}.json"


def _guardar_reporte_parte(
        d: Path, parte: int, leccion: Path, reporte: dict) -> None:
    """Persiste métricas derivadas para reusar una parte sin fingir ceros."""
    if not leccion.is_file():
        return
    payload = {
        "schema_version": 1,
        "parte": int(parte),
        "leccion_sha256": hashlib.sha256(leccion.read_bytes()).hexdigest(),
        "reporte": reporte,
    }
    _escribir_texto_atomico(
        _reporte_parte_path(d, parte),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _cargar_reporte_parte(
        d: Path, parte: int, leccion: Path) -> dict | None:
    path = _reporte_parte_path(d, parte)
    if not path.is_file() or not leccion.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        esperado = hashlib.sha256(leccion.read_bytes()).hexdigest()
    except (OSError, ValueError, TypeError):
        return None
    reporte = payload.get("reporte")
    if (payload.get("schema_version") != 1
            or int(payload.get("parte", -1)) != int(parte)
            or payload.get("leccion_sha256") != esperado
            or not isinstance(reporte, dict)):
        return None
    return reporte


def _respuesta_vacia(r: str | None) -> bool:
    return r is None or not str(r).strip()


def _idioma_por_texto(txt: str) -> str:
    """Fallback conservador: solo marca EN si la mayoría es claramente inglesa."""
    tokens = re.findall(r"[a-z]+", txt.lower())
    if len(tokens) < 35:
        return "es"
    en = sum(1 for t in tokens if t in EN_HINTS)
    es = sum(1 for t in tokens if t in ES_HINTS)
    lower = txt.lower()
    for frase in ("machine learning", "training data", "loss function",
                  "neural network", "gradient descent", "test set"):
        if frase in lower:
            en += 3
    for frase in ("aprendizaje automatico", "funcion de perdida",
                  "descenso de gradiente", "conjunto de datos"):
        if frase in lower:
            es += 3
    if en >= 10 and en >= max(1, es) * 1.8:
        return "en"
    if en >= 6 and es >= 4:
        return "mixto"
    return "es"


def idioma_material(segs: list[dict], st: dict | None = None) -> str:
    """es | en | mixto según metadata; si falta, usa heurística textual conservadora."""
    st = st or {}
    for clave in ("idioma", "language", "lang"):
        valor = str(st.get(clave, "")).strip().lower()
        if valor in {"en", "eng", "english", "ingles", "inglés"}:
            return "en"
        if valor in {"es", "spa", "spanish", "espanol", "español"}:
            return "es"
    idiomas = [str(s.get("idioma", "")).lower() for s in segs if s.get("idioma")]
    if not idiomas:
        return _idioma_por_texto(_texto_segs(segs))
    en = sum(1 for x in idiomas if x.startswith("en"))
    ratio = en / max(1, len(idiomas))
    if ratio >= 0.60:
        return "en"
    if ratio >= 0.25:
        return "mixto"
    return "es"


def _texto_segs(segs: list[dict]) -> str:
    return " ".join(str(s.get("texto", "")) for s in segs).strip()


def _transcripcion_cruda_md(segs: list[dict], origen: str,
                            titulo: str = "## Apéndice: transcripción cruda preservada") -> str:
    lineas = [
        titulo,
        "",
        "> Conservada automáticamente para que la lección final no pierda cobertura "
        "y distinguir el registro literal de la reorganización pedagógica.",
        "",
    ]
    for s in segs:
        texto = _redactar_datos_personales_publicos(
            str(s.get("texto", ""))).strip()
        if texto:
            parte = s.get("parte", "?")
            lineas.append(f"- [{fmt_t(float(s.get('t', 0) or 0), origen)} | parte {parte}] {texto}")
    return "\n".join(lineas).strip() + "\n"


def _leccion_demasiado_corta(final_txt: str, segs: list[dict]) -> bool:
    trans = _texto_segs(segs)
    if len(trans) < 1000:
        return False
    visible = re.sub(r"<[^>]+>", " ", final_txt)
    visible = re.sub(r"\s+", " ", visible).strip()
    return len(visible) < max(900, int(len(trans) * 0.12))


def _palabras_clave(txt: str) -> set[str]:
    return {w for w in re.findall(r"[a-záéíóúñü]{5,}", txt.lower())
            if w not in {"entonces", "porque", "cuando", "vamos", "sobre", "donde",
                         "tiene", "puede", "hacer", "ahora", "parte", "clase"}}


def _sin_retomar(texto: str, despues: str) -> bool:
    if not despues.strip():
        return False
    claves = _palabras_clave(texto)
    if not claves:
        return False
    return len(claves & _palabras_clave(despues)) / max(1, len(claves)) < 0.15


def clasificar_publicidad_probable(segs: list[dict], capturas: list[dict] | None = None,
                                   contexto_antes: str = "",
                                   contexto_despues: str = "") -> dict:
    """Heurística conservadora: nunca marca publicidad por un solo criterio."""
    if not segs:
        return {"tipo": "contenido_clase", "criterios": []}
    ts = [float(s.get("t", s.get("ini", 0)) or 0) for s in segs]
    fs = [float(s.get("fin", s.get("t", 0)) or 0) for s in segs]
    dur = max(max(fs), max(ts)) - min(ts)
    corto = dur <= 0 or dur < 180
    texto = _texto_segs(segs).lower()
    hits_pub = [p for p in TERMINOS_PUBLICIDAD if p in texto]
    hits_clase = [p for p in TERMINOS_CLASE if p in texto]
    caps = capturas or []
    visual_no_rel = any(str(c.get("relacion", c.get("cambio_visual", c.get("visual", "")))).lower()
                        in {"no_relacionado", "publicidad", "sudden", "abrupto"}
                        or bool(c.get("visual_no_relacionado")) for c in caps)
    no_retomado = _sin_retomar(texto, contexto_despues) or any(
        bool(s.get("no_retomado")) for s in segs)
    entre_tramos = bool(contexto_antes.strip() and contexto_despues.strip()) or any(
        bool(s.get("entre_tramos")) for s in segs)
    criterios = []
    if corto:
        criterios.append("duracion<3min")
    if hits_pub:
        criterios.append("lenguaje_comercial:" + ",".join(hits_pub[:4]))
    if visual_no_rel:
        criterios.append("visual_no_relacionado")
    if no_retomado:
        criterios.append("no_se_retoma")
    if entre_tramos:
        criterios.append("entre_tramos_de_clase")
    probable = corto and bool(hits_pub) and (
        visual_no_rel or no_retomado or entre_tramos or len(hits_pub) >= 2)
    if probable and hits_clase and len(hits_pub) == 1 and not visual_no_rel:
        probable = False
    if probable:
        tipo = "publicidad_probable"
    elif corto and (hits_pub or visual_no_rel or no_retomado):
        tipo = "intervencion_breve_posible_digresion"
    else:
        tipo = "contenido_clase"
    return {"tipo": tipo, "criterios": criterios, "duracion_seg": round(max(0, dur), 1)}


def analisis_publicidad_md(info: dict) -> str:
    if info["tipo"] == "publicidad_probable":
        return ("Heurística local: PUBLICIDAD PROBABLE; marcar aparte, no mezclar con la "
                f"clase. Criterios: {', '.join(info['criterios'])}.")
    if info["tipo"] == "intervencion_breve_posible_digresion":
        return ("Heurística local: intervención breve o posible digresión; conservar si "
                f"aporta algo. Criterios: {', '.join(info['criterios'])}.")
    return "Heurística local: contenido de clase; conservar y desarrollar."


def eliminar_otras_pantallas(md: str) -> str:
    """Elimina secciones 'Otras pantallas...' que solo listan imágenes sin explicación."""
    out, saltando = [], False
    for linea in md.splitlines():
        if re.match(r"^\s{0,3}#{2,6}\s+.*Otras pantallas", linea, re.I):
            saltando = True
            continue
        if saltando and re.match(r"^\s{0,3}#{1,6}\s+\S", linea):
            saltando = False
        if not saltando:
            out.append(linea)
    return "\n".join(out).strip() + ("\n" if out else "")


def _origen_info(origen) -> tuple[str, float]:
    if isinstance(origen, dict):
        modo = str(origen.get("origen") or origen.get("modo") or "vivo")
        inicio = _to_float(origen.get("inicio", origen.get("t0", 0.0)))
        return modo, inicio
    if isinstance(origen, (int, float)):
        return "vivo", float(origen)
    return str(origen or "vivo"), 0.0


def _origen_intervalos(origen) -> list[tuple[float, float]]:
    if not isinstance(origen, dict):
        return []
    intervalos: list[tuple[float, float]] = []
    for item in origen.get("intervalos_utiles", []) or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            inicio, fin = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            continue
        if fin > inicio:
            intervalos.append((inicio, fin))
    return sorted(intervalos)


def _origen_postproceso(d: Path, st: dict | None = None) -> dict:
    """Origen vivo proyectado sobre captura util, sin contar pausas ni recesos."""
    estado_clase = dict(st or est.leer_estado(d))
    modo = str(estado_clase.get("origen", "vivo") or "vivo")
    inicio = _to_float(estado_clase.get("inicio", 0.0))
    origen = {"origen": modo, "inicio": inicio}
    if modo == "video":
        return origen
    marcas = est.leer_marcas(d)
    fines = [
        _to_float(marca.get("t"))
        for marca in marcas
        if marca.get("tipo") == "fin"
    ]
    ahora = max(fines) if fines else time.time()
    intervalos = est.intervalos_captura_util(estado_clase, marcas, ahora)
    if intervalos:
        origen["intervalos_utiles"] = [
            [float(tramo_inicio), float(tramo_fin)]
            for tramo_inicio, tramo_fin in intervalos
        ]
    return origen


def _parece_absoluto_de_clase(valor: float, inicio: float) -> bool:
    if inicio <= 0:
        return valor >= 1_000_000_000
    return (valor >= inicio - 5 and valor - inicio <= 7 * 24 * 3600) or (
        valor >= 1_000_000_000 and inicio > 0)


def _normalizar_valor_tiempo(valor, origen) -> float:
    v = _to_float(valor)
    modo, inicio = _origen_info(origen)
    if modo == "video":
        return max(0.0, v)
    if inicio and _parece_absoluto_de_clase(v, inicio):
        intervalos = _origen_intervalos(origen)
        if intervalos:
            acumulado = 0.0
            for tramo_inicio, tramo_fin in intervalos:
                if v <= tramo_inicio:
                    return max(0.0, acumulado)
                if v < tramo_fin:
                    return max(0.0, acumulado + v - tramo_inicio)
                acumulado += tramo_fin - tramo_inicio
            return max(0.0, acumulado)
        return max(0.0, v - inicio)
    return max(0.0, v)


def fmt_duracion(segundos: float | int | None) -> str:
    total = max(0, int(round(_to_float(segundos))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_t(t: float, origen) -> str:
    return fmt_duracion(_normalizar_valor_tiempo(t, origen))


def _env_int(nombre: str, defecto: int) -> int:
    try:
        return int(os.environ.get(nombre, str(defecto)))
    except ValueError:
        return defecto


def _env_float(nombre: str, defecto: float) -> float:
    try:
        return float(os.environ.get(nombre, str(defecto)))
    except ValueError:
        return defecto


def _post_img_max() -> int:
    return max(0, _env_int("CLASE_POST_IMG_MAX",
                           _env_int("CLASE_POST_MAX_IMGS", 24)))


def _post_img_min() -> int:
    return max(0, _env_int("CLASE_POST_IMG_MIN", 4))


def _post_img_per_min() -> float:
    return max(0.0, _env_float("CLASE_POST_IMG_PER_MIN", 1.0))


def _post_slides_max() -> int:
    return max(1, _env_int("CLASE_POST_SLIDES_MAX", 12))


def _post_slides_budget_min() -> int:
    return max(1, _env_int("CLASE_POST_SLIDES_BUDGET_MIN", 4))


def _post_slides_budget_max() -> int:
    return max(_post_slides_budget_min(), _env_int("CLASE_POST_SLIDES_BUDGET_MAX", 18))


def _post_slides_hard_max() -> int:
    return max(_post_slides_budget_max(), _env_int(
        "CLASE_POST_SLIDES_HARD_MAX_PER_CHUNK", 24))


def _post_slides_budget_per_min() -> float:
    return max(0.1, _env_float("CLASE_POST_SLIDES_BUDGET_PER_MIN", 0.9))


def _post_slides_budget_fraction() -> float:
    return min(1.0, max(0.05, _env_float("CLASE_POST_SLIDES_BUDGET_FRACTION", 0.12)))


def _post_web_max_consultas() -> int:
    """Una consulta por tramo representativo, con guardarraíl de costo/red."""
    return min(48, max(1, _env_int("CLASE_POST_WEB_MAX_CONSULTAS", 48)))


def _post_slides_keep_density() -> float:
    """Slides/minuto por debajo del cual se conserva una deck razonable completa."""
    return max(0.5, _env_float("CLASE_POST_SLIDES_KEEP_DENSITY", 1.4))


def _post_slide_group_max_gap() -> float:
    return max(15.0, _env_float("CLASE_POST_SLIDE_GROUP_MAX_GAP", 180.0))


def _umbral_visual_estable(d: Path | None = None) -> float:
    """Tiempo real mínimo para conservar una imagen poco textual como material."""
    base = max(5.0, _env_float("CLASE_POST_VISUAL_ESTABLE_SEG", 20.0))
    vel = 1.0
    if d is not None:
        try:
            st = est.leer_estado(d)
            vel = max(1.0, float(st.get("velocidad", 1.0) or 1.0))
        except Exception:
            vel = 1.0
    return max(5.0, base / vel)


def _post_separar_temas() -> bool:
    return os.environ.get("CLASE_POST_SEPARAR_TEMAS", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def _to_float(valor, defecto: float = 0.0) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


def normalizar_tiempo_segmento(seg: dict, origen) -> float:
    modo, _inicio = _origen_info(origen)
    raw = seg.get("t", seg.get("inicio", seg.get("start", seg.get("ini"))))
    if (
        modo != "video"
        and _origen_intervalos(origen)
        and raw is not None
        and _to_float(raw) >= 1_000_000_000
    ):
        return _normalizar_valor_tiempo(raw, origen)
    if "t_rel" in seg:
        return max(0.0, _to_float(seg.get("t_rel")))
    valor = raw if raw is not None else 0.0
    return _normalizar_valor_tiempo(valor, origen)


def normalizar_fin_segmento(seg: dict, origen) -> float:
    modo, inicio = _origen_info(origen)
    raw_fin = _to_float(seg.get("fin"))
    if (
        modo != "video"
        and _origen_intervalos(origen)
        and raw_fin >= 1_000_000_000
    ):
        return max(
            normalizar_tiempo_segmento(seg, origen),
            _normalizar_valor_tiempo(raw_fin, origen),
        )
    if "fin_rel" in seg:
        return max(0.0, _to_float(seg.get("fin_rel")))
    inicio_rel = normalizar_tiempo_segmento(seg, origen)
    if "fin" not in seg:
        return inicio_rel
    raw_ini = _to_float(seg.get("ini", 0.0))
    raw_t = _to_float(seg.get("t", seg.get("ini", 0.0)))
    if modo == "video":
        return max(inicio_rel, raw_fin)
    if raw_fin >= raw_t and raw_t > 0:
        return max(inicio_rel, _normalizar_valor_tiempo(raw_fin, origen))
    if raw_fin >= raw_ini and raw_ini > 0 and _parece_absoluto_de_clase(raw_t, inicio):
        return inicio_rel + max(0.0, raw_fin - raw_ini)
    fin_rel = _normalizar_valor_tiempo(raw_fin, origen)
    if fin_rel >= inicio_rel:
        return fin_rel
    if raw_fin > 0:
        return inicio_rel + max(0.0, raw_fin - raw_ini)
    return inicio_rel


def normalizar_tiempo_captura(cap: dict, origen) -> float:
    modo, _inicio = _origen_info(origen)
    raw = cap.get("t")
    if (
        modo != "video"
        and _origen_intervalos(origen)
        and raw is not None
        and _to_float(raw) >= 1_000_000_000
    ):
        return _normalizar_valor_tiempo(raw, origen)
    if "t_rel" in cap:
        return max(0.0, _to_float(cap.get("t_rel")))
    return _normalizar_valor_tiempo(raw or 0.0, origen)


def _seg_t(seg: dict, origen=None) -> float:
    if origen is not None:
        return normalizar_tiempo_segmento(seg, origen)
    return _to_float(seg.get("t_rel", seg.get("t", seg.get("ini", 0.0))))


def _seg_fin(seg: dict, origen=None) -> float:
    if origen is not None:
        return normalizar_fin_segmento(seg, origen)
    return _to_float(seg.get("fin_rel", seg.get("fin", seg.get("t_rel",
                     seg.get("t", seg.get("ini", 0.0))))))


def _cap_t(cap: dict, origen=None) -> float:
    if origen is not None:
        return normalizar_tiempo_captura(cap, origen)
    return _to_float(cap.get("t_rel", cap.get("t", 0.0)))


def _tiempo_en_rango(t: float, inicio: float, fin: float) -> bool:
    """El primer rango incluye t=0; los siguientes poseen el limite izquierdo anterior."""
    incluye_inicio = math.isclose(inicio, 0.0, abs_tol=1e-9)
    return (t >= inicio if incluye_inicio else t > inicio) and t <= fin


def _tipo_evento_visual(cap: dict) -> str:
    """Normaliza el esquema nuevo conservando filas legacy como capturas."""
    return str(cap.get("evento", "captura") or "captura").strip().lower()


def _es_captura_visual(cap: dict) -> bool:
    return _tipo_evento_visual(cap) not in {"heartbeat", "fin"}


def capturas_rango(d: Path, t_ini: float, t_fin: float, origen=None) -> list[dict]:
    """Capturas existentes del rango. Tolera img_log antiguo sin campos nuevos."""
    out = []
    for c in est.leer_jsonl(d / "img_log.jsonl"):
        if not _es_captura_visual(c):
            continue
        t = _cap_t(c, origen) if origen is not None else _cap_t(c)
        if _tiempo_en_rango(t, t_ini, t_fin) and (
                d / "img" / str(c.get("archivo", ""))).is_file():
            c2 = dict(c)
            c2["t_rel"] = t
            out.append(c2)
    return sorted(out, key=_cap_t)


def _wav_t(wav_path: Path) -> float | None:
    m = re.search(r"chunk_(\d+)", wav_path.name)
    if not m:
        m = re.search(r"(\d{8,})", wav_path.stem)
    return float(m.group(1)) if m else None


def _wav_t_rel(wav_path: Path, origen) -> float | None:
    t = _wav_t(wav_path)
    if t is None:
        return None
    return _normalizar_valor_tiempo(t, origen)


def _wav_rms(wav_path: Path, max_seconds: float = 15.0) -> float | None:
    """RMS local muestreado; no envia WAV a DeepSeek."""
    try:
        from .captura_audio import calcular_rms
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = max(1, wf.getframerate())
            tomar = min(frames, int(rate * max_seconds))
            data = wf.readframes(tomar)
        return calcular_rms(data)
    except Exception:
        return None


def _wav_metricas(wav_path: Path) -> dict:
    try:
        from .captura_audio import metricas_wav
        met = metricas_wav(wav_path)
        return {
            "rms": met.get("rms"),
            "peak": met.get("peak"),
            "useful_audio": bool(met.get("useful_audio")),
            "duration_sec": met.get("duration_sec"),
        }
    except Exception:
        rms = _wav_rms(wav_path)
        return {
            "rms": rms,
            "peak": None,
            "useful_audio": bool(rms is not None and rms > config.SILENCIO_RMS),
            "duration_sec": None,
        }


def _wav_metricas_manifest(row: dict | None) -> dict | None:
    """Reusa métricas selladas solo cuando el caller ya verificó los WAV."""
    if not isinstance(row, dict) or row.get("status") != "sealed":
        return None
    if not row.get("sha256") or row.get("rms") is None or row.get("peak") is None:
        return None
    try:
        from .captura_audio import pcm_tiene_senal_util
        rms = float(row["rms"])
        peak = int(row["peak"])
        duration = float(row.get("duration", row.get("duration_sec", 0)) or 0)
    except (TypeError, ValueError):
        return None
    return {
        "rms": rms,
        "peak": peak,
        "useful_audio": pcm_tiene_senal_util(rms, peak, config.SILENCIO_RMS),
        "duration_sec": duration,
    }
def _segmentos_ordenados(segs: list[dict], origen=None) -> list[dict]:
    out = []
    for s in segs:
        if not str(s.get("texto", "")).strip():
            continue
        if origen is None:
            out.append(s)
        else:
            t_rel = normalizar_tiempo_segmento(s, origen)
            fin_rel = max(t_rel, normalizar_fin_segmento(s, origen))
            out.append(dict(s, t_rel=t_rel, fin_rel=fin_rel))
    return sorted(out, key=_seg_t)


def _gaps_transcript(segs: list[dict], origen=None) -> list[dict]:
    ordenados = _segmentos_ordenados(segs, origen)
    gaps = []
    for a, b in zip(ordenados, ordenados[1:]):
        fin = _seg_fin(a)
        ini = _seg_t(b)
        if ini - fin > GAP_LARGO_SEG:
            dur = ini - fin
            gaps.append({"inicio": fin, "fin": ini, "duracion": dur,
                         "inicio_fmt": fmt_duracion(fin),
                         "fin_fmt": fmt_duracion(ini),
                         "duracion_fmt": fmt_duracion(dur)})
    return gaps


def _capturas_en_periodo(capturas: list[dict], inicio: float, fin: float,
                         origen=None) -> list[dict]:
    out = []
    for c in capturas:
        t = _cap_t(c, origen) if origen is not None else _cap_t(c)
        if inicio < t <= fin:
            out.append(dict(c, t_rel=t))
    return out


def _transcript_cerca(t: float | None, segs: list[dict],
                      ventana: float = TRANSCRIPT_CERCA_WAV_SEG) -> bool:
    if t is None or not segs:
        return False
    for s in segs:
        if abs(_seg_t(s) - t) <= ventana or (_seg_t(s) <= t <= _seg_fin(s) + ventana):
            return True
    return False


def _wav_duracion(wav_path: Path) -> float | None:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            rate = max(1, wf.getframerate())
            return wf.getnframes() / rate
    except Exception:
        return None


def _wav_velocidad(wav_path: Path) -> tuple[str, float] | None:
    m = re.search(r"_v(\d{2})(?:\.|_)", wav_path.name.lower())
    if not m:
        return None
    codigo = f"v{m.group(1)}"
    try:
        return codigo, int(m.group(1)) / 10.0
    except ValueError:
        return None


def _resumen_velocidad_wavs(wavs: list[Path], dur_total: float) -> dict:
    conteos: dict[str, int] = {}
    velocidades: dict[str, float] = {}
    for wav in wavs:
        info = _wav_velocidad(wav)
        if not info:
            continue
        codigo, vel = info
        conteos[codigo] = conteos.get(codigo, 0) + 1
        velocidades[codigo] = vel
    if not conteos:
        return {
            "velocidad_codigo": "",
            "velocidad_factor": 1.0,
            "velocidad_conteos": {},
            "velocidad_confianza": "sin_dato",
            "duracion_video_equivalente_seg": round(dur_total, 2),
            "duracion_video_equivalente_fmt": fmt_duracion(dur_total),
        }
    codigo = max(conteos, key=lambda k: conteos[k])
    factor = velocidades[codigo]
    confianza = "mayoria" if conteos[codigo] >= max(1, len(wavs) / 2) else "mixta"
    equivalente = dur_total * factor
    return {
        "velocidad_codigo": codigo,
        "velocidad_factor": factor,
        "velocidad_conteos": conteos,
        "velocidad_confianza": confianza,
        "duracion_video_equivalente_seg": round(equivalente, 2),
        "duracion_video_equivalente_fmt": fmt_duracion(equivalente),
    }


def _chunk_segmento(seg: dict) -> str:
    for k in ("chunk", "wav", "audio", "archivo_audio", "archivo"):
        valor = str(seg.get(k, "") or "").strip()
        if valor:
            return Path(valor.replace("\\", "/")).name
    return ""


def _comparar_wavs_transcript(d: Path, segs: list[dict]) -> dict:
    audio_dir = d / "audio"
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []
    wav_nombres = {p.name for p in wavs}
    chunks = []
    for s in segs:
        chunk = _chunk_segmento(s)
        if chunk:
            chunks.append(chunk)
    chunks_materializados = set(chunks)
    stt = audio_manifest.transcript_status_report(d, verify_files=True)
    paths = stt["chunk_paths"]
    chunks_completos = {
        Path(str(paths.get(chunk_id) or f"{chunk_id}.wav")).name
        for chunk_id in stt["completed_chunks"]
    }
    chunks_pendientes = {
        Path(str(paths.get(chunk_id) or f"{chunk_id}.wav")).name
        for chunk_id in stt["unresolved_chunks"]
    }
    commits_vacios = {
        Path(str(paths.get(chunk_id) or f"{chunk_id}.wav")).name
        for chunk_id in stt["empty_completed_chunks"]
    }
    return {
        "wavs": wavs,
        "wav_nombres": wav_nombres,
        "chunks_transcript": chunks_completos,
        "chunks_materializados": chunks_materializados,
        "wav_sin_transcript": sorted(wav_nombres & chunks_pendientes),
        "transcript_wav_faltante": sorted(
            c for c in chunks_materializados - wav_nombres if c.lower().endswith(".wav")),
        "commits_vacios": sorted(commits_vacios),
        "stt": stt,
    }


def _info_wav_basica(wav_path: Path | None, origen) -> dict | None:
    if wav_path is None:
        return None
    t_rel = _wav_t_rel(wav_path, origen)
    return {
        "nombre": wav_path.name,
        "t_rel": t_rel,
        "hora": fmt_t(t_rel, origen) if t_rel is not None else "(sin timestamp)",
        "last_write": time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(wav_path.stat().st_mtime)),
    }


def _segmento_resumen(seg: dict | None, origen) -> dict | None:
    if not seg:
        return None
    texto = re.sub(r"\s+", " ", str(seg.get("texto", "")).strip())
    t_rel = _seg_t(seg)
    return {
        "timestamp": fmt_t(t_rel, origen),
        "t_rel": round(t_rel, 2),
        "chunk": _chunk_segmento(seg) or "(sin chunk)",
        "texto": texto[:180],
    }


def _marca_t(marca: dict, defecto: float) -> float:
    try:
        return float(marca.get("t", defecto))
    except (TypeError, ValueError):
        return defecto


def _intervalos_almacenamiento_desde_marcas(st: dict, marcas: list[dict],
                                            fin_ref: float) -> list[tuple[float, float]]:
    try:
        inicio = float(st.get("inicio", fin_ref) or fin_ref)
    except (TypeError, ValueError):
        inicio = fin_ref
    intervalos: list[tuple[float, float]] = []
    activo = True
    inicio_activo = inicio

    def cerrar(t: float) -> None:
        nonlocal activo, inicio_activo
        if activo:
            fin = max(inicio_activo, t)
            if fin > inicio_activo:
                intervalos.append((inicio_activo, fin))
            activo = False

    def abrir(t: float) -> None:
        nonlocal activo, inicio_activo
        if not activo:
            activo = True
            inicio_activo = max(inicio, t)

    for marca in sorted(marcas, key=lambda m: _marca_t(m, inicio)):
        tipo = marca.get("tipo")
        t = max(inicio, _marca_t(marca, inicio))
        if tipo in {"pausa", "auto_pausa", "fin"}:
            cerrar(t)
        elif tipo in {"seguir", "auto_seguir", "reanudar_sesion", "inicio"}:
            abrir(t)
    if activo and (st.get("estado") == "fin" or
                   (st.get("estado") == "grabando" and not st.get("auto_pausa", False))):
        cerrar(fin_ref)
    return intervalos


def _intervalos_inactividad_desde_marcas(
        st: dict, marcas: list[dict], fin_ref: float) -> list[tuple[float, float]]:
    """Complemento de captura útil; una pausa declarada no equivale a pérdida PCM."""
    try:
        inicio = float(st.get("inicio", fin_ref) or fin_ref)
    except (TypeError, ValueError):
        inicio = fin_ref
    if fin_ref <= inicio:
        return []
    activos = sorted(_intervalos_almacenamiento_desde_marcas(
        st, marcas, fin_ref))
    inactivos: list[tuple[float, float]] = []
    cursor = inicio
    for desde, hasta in activos:
        desde = max(inicio, float(desde))
        hasta = min(fin_ref, float(hasta))
        if desde > cursor:
            inactivos.append((cursor, desde))
        cursor = max(cursor, hasta)
    if cursor < fin_ref:
        inactivos.append((cursor, fin_ref))
    return inactivos


def continuidad_audio_contextual(d: Path) -> dict:
    """Reporta continuidad física descontando solo pausas registradas y cerradas."""
    try:
        st = est.leer_estado(d)
        marcas = sorted(
            est.leer_marcas(d), key=lambda marca: _marca_t(
                marca, float(st.get("inicio", 0) or 0)))
        inicio = float(st.get("inicio", 0) or 0)
        fines = [_marca_t(marca, inicio) for marca in marcas
                 if marca.get("tipo") == "fin"]
        fin_ref = fines[-1] if fines else max(
            [_marca_t(marca, inicio) for marca in marcas] or [time.time()])
        inactivos = _intervalos_inactividad_desde_marcas(
            st, marcas, fin_ref)
    except (OSError, TypeError, ValueError):
        inactivos = []
    return audio_manifest.continuity_report(
        d, declared_inactive_intervals=inactivos)


def analizar_sesion_captura(d: Path) -> dict:
    if not (d / "session_state.json").is_file():
        return {}
    try:
        st = est.leer_estado(d)
        marcas = est.leer_marcas(d)
    except Exception:
        return {}
    try:
        inicio = float(st.get("inicio", 0) or 0)
    except (TypeError, ValueError):
        inicio = 0.0
    if not inicio:
        return {}
    marcas_ordenadas = sorted(marcas, key=lambda m: _marca_t(m, inicio))
    fines = [_marca_t(m, inicio) for m in marcas_ordenadas if m.get("tipo") == "fin"]
    fin_ref = fines[-1] if fines else max([_marca_t(m, inicio) for m in marcas_ordenadas] or [time.time()])
    intervalos = _intervalos_almacenamiento_desde_marcas(st, marcas_ordenadas, fin_ref)
    activo = sum(max(0.0, fin - ini) for ini, fin in intervalos)
    auto = [m for m in marcas_ordenadas if m.get("tipo") == "auto_pausa"]
    manual = [m for m in marcas_ordenadas if m.get("tipo") == "pausa"]
    primera_auto = _marca_t(auto[0], inicio) if auto else None
    ultima_auto = auto[-1] if auto else {}
    return {
        "estado": st.get("estado", ""),
        "auto_pausa_actual": bool(st.get("auto_pausa", False)),
        "auto_pausa_motivo": st.get("auto_pausa_motivo", ""),
        "inicio": inicio,
        "fin_ref": fin_ref,
        "duracion_reloj_seg": round(max(0.0, fin_ref - inicio), 2),
        "duracion_reloj_fmt": fmt_duracion(max(0.0, fin_ref - inicio)),
        "duracion_almacenando_seg": round(activo, 2),
        "duracion_almacenando_fmt": fmt_duracion(activo),
        "auto_pausa_total": len(auto),
        "pausa_manual_total": len(manual),
        "primera_auto_pausa_seg": round(max(0.0, primera_auto - inicio), 2)
        if primera_auto is not None else None,
        "primera_auto_pausa_fmt": fmt_duracion(max(0.0, primera_auto - inicio))
        if primera_auto is not None else "",
        "ultima_auto_pausa_nota": str(ultima_auto.get("nota", "")),
    }


def analizar_cobertura_temporal(d: Path, segs: list[dict], origen) -> dict:
    """Reporte determinístico: duración WAV, rango transcript y chunks pendientes."""
    segs_norm = _segmentos_ordenados(segs, origen)
    comp = _comparar_wavs_transcript(d, segs_norm)
    wavs: list[Path] = comp["wavs"]
    duraciones = [_wav_duracion(w) for w in wavs]
    dur_ok = [x for x in duraciones if x is not None]
    dur_fallos = len(duraciones) - len(dur_ok)
    if dur_ok:
        promedio = sum(dur_ok) / len(dur_ok)
        dur_total = sum(dur_ok) + dur_fallos * promedio
    else:
        dur_total = len(wavs) * 30.0
    velocidad = _resumen_velocidad_wavs(wavs, dur_total)
    continuidad = continuidad_audio_contextual(d)

    primero = segs_norm[0] if segs_norm else None
    ultimo = segs_norm[-1] if segs_norm else None
    dur_transcript = 0.0
    if primero and ultimo:
        dur_transcript = max(0.0, _seg_fin(ultimo) - _seg_t(primero))

    gaps = _gaps_transcript(segs_norm)
    gap_max = max([g["duracion"] for g in gaps], default=0.0)
    ultimo_wav = wavs[-1] if wavs else None
    ultimo_wav_nombre = ultimo_wav.name if ultimo_wav else ""
    transcript_llega_ultimo_wav = (
        bool(ultimo_wav_nombre and ultimo_wav_nombre in comp["chunks_transcript"])
        or not wavs
    )
    advertencias = []
    if not wavs:
        advertencias.append("no quedan WAV para reauditar duración real")
    if comp["wav_sin_transcript"]:
        advertencias.append("hay WAV con commit STT pendiente o inválido")
    if comp["commits_vacios"]:
        advertencias.append(
            "hay commits STT completos sin segmentos; no están pendientes")
    if comp["transcript_wav_faltante"]:
        advertencias.append("el transcript referencia WAV que no existen en audio/")
    if gap_max > GAP_LARGO_SEG:
        advertencias.append("hay gaps largos entre segmentos de transcript")
    if wavs and not transcript_llega_ultimo_wav:
        advertencias.append("el transcript no alcanza el último WAV capturado")
    if continuidad.get("status") == "warning":
        advertencias.append(
            "la continuidad PCM reporta déficit o gaps entre WAV; revisar audio_manifest")
    captura_sesion = analizar_sesion_captura(d)
    if captura_sesion.get("auto_pausa_total"):
        advertencias.append("la sesión tuvo auto-pausa; revisar si el video siguió reproduciéndose")
    if captura_sesion.get("auto_pausa_actual") and captura_sesion.get("estado") == "fin":
        advertencias.append("la clase terminó estando en auto-pausa")

    estado = "ok" if not advertencias else "advertencia"
    return {
        "estado": estado,
        "wav_total": len(wavs),
        "duracion_wav_seg": round(dur_total, 2),
        "duracion_wav_fmt": fmt_duracion(dur_total),
        "duracion_wav_estimada": bool(dur_fallos or (wavs and not dur_ok)),
        **velocidad,
        "primer_wav": _info_wav_basica(wavs[0], origen) if wavs else None,
        "ultimo_wav": _info_wav_basica(ultimo_wav, origen),
        "primer_segmento": _segmento_resumen(primero, origen),
        "ultimo_segmento": _segmento_resumen(ultimo, origen),
        "duracion_transcript_seg": round(dur_transcript, 2),
        "duracion_transcript_fmt": fmt_duracion(dur_transcript),
        "wav_sin_transcript": comp["wav_sin_transcript"][:10],
        "wav_sin_transcript_total": len(comp["wav_sin_transcript"]),
        "commits_vacios": comp["commits_vacios"][:10],
        "commits_vacios_total": len(comp["commits_vacios"]),
        "stt_invalidos_total": comp["stt"]["invalid_count"],
        "transcript_wav_faltante": comp["transcript_wav_faltante"][:10],
        "transcript_wav_faltante_total": len(comp["transcript_wav_faltante"]),
        "gap_max_segundos": round(gap_max, 2),
        "gap_max_fmt": fmt_duracion(gap_max),
        "transcript_llega_ultimo_wav": transcript_llega_ultimo_wav,
        "continuidad_audio": continuidad,
        "captura_sesion": captura_sesion,
        "advertencias": advertencias,
    }


def detectar_gaps_transcript(segs: list[dict], capturas: list[dict], origen) -> dict:
    segs_norm = _segmentos_ordenados(segs, origen)
    caps_norm = [dict(c, t_rel=normalizar_tiempo_captura(c, origen)) for c in capturas]
    gaps = _gaps_transcript(segs_norm)
    periodos = []
    for g in gaps:
        caps_gap = _capturas_en_periodo(caps_norm, g["inicio"], g["fin"])
        if caps_gap:
            periodos.append({
                **g,
                "capturas": [str(c.get("archivo", "")) for c in caps_gap if c.get("archivo")],
                "capturas_total": len(caps_gap),
            })

    if not segs_norm and caps_norm:
        ts = [_cap_t(c) for c in caps_norm if c.get("archivo")]
        if ts:
            inicio = min(ts)
            fin = max(ts)
            dur = max(0.0, fin - inicio)
            periodos.append({
                "inicio": inicio, "fin": fin, "duracion": dur,
                "inicio_fmt": fmt_duracion(inicio),
                "fin_fmt": fmt_duracion(fin),
                "duracion_fmt": fmt_duracion(dur),
                "capturas": [str(c.get("archivo", "")) for c in caps_norm if c.get("archivo")],
                "capturas_total": len(caps_norm),
            })

    rango = None
    if segs_norm:
        rango = {"inicio": _seg_t(segs_norm[0]),
                 "fin": max(_seg_fin(s) for s in segs_norm)}
    gap_max = max([g["duracion"] for g in gaps], default=0.0)
    return {
        "segmentos_total": len(segs_norm),
        "rango_transcript": rango,
        "gaps_sin_segmentos": gaps,
        "gap_max_segundos": round(gap_max, 2),
        "gap_max_fmt": fmt_duracion(gap_max),
        "periodos_con_capturas_sin_audio": periodos,
    }


def analizar_cobertura_audio(d: Path, segs: list[dict],
                             capturas: list[dict] | str | dict | int | float | None = None,
                             origen="vivo", *, manifest_verificado: bool = False) -> dict:
    """Audita cobertura antes de limpiar. No retranscribe ni envia WAV al LLM."""
    if isinstance(capturas, (str, dict, int, float)) and isinstance(origen, list):
        capturas, origen = origen, capturas
    segs = _segmentos_ordenados(segs, origen)
    capturas = capturas if capturas is not None else est.leer_jsonl(d / "img_log.jsonl")
    audio_dir = d / "audio"
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []
    comparacion_chunks = _comparar_wavs_transcript(d, segs)
    advertencias: list[str] = []
    wav_info = []
    wav_util_sin_transcript = []
    wav_silenciosos = 0
    wav_util = 0
    metricas_desde_manifest = 0
    metricas_desde_wav = 0
    manifest_rows = audio_manifest.latest_by_chunk(d) if manifest_verificado else {}

    for wav_path in wavs:
        metricas_wav_local = _wav_metricas_manifest(
            manifest_rows.get(wav_path.stem))
        if metricas_wav_local is None:
            metricas_wav_local = _wav_metricas(wav_path)
            metricas_desde_wav += 1
        else:
            metricas_desde_manifest += 1
        rms = metricas_wav_local.get("rms")
        peak = metricas_wav_local.get("peak")
        util = bool(metricas_wav_local.get("useful_audio"))
        t_wav = _wav_t_rel(wav_path, origen)
        info = {"archivo": wav_path.name, "t": t_wav,
                "t_original": _wav_t(wav_path), "rms": rms, "peak": peak,
                "useful_audio": util}
        wav_info.append(info)
        if rms is None:
            continue
        if util:
            wav_util += 1
            if not _transcript_cerca(t_wav, segs):
                wav_util_sin_transcript.append(info)
        else:
            wav_silenciosos += 1

    if not wavs:
        advertencias.append("sin WAV disponibles para auditar")
    if wav_silenciosos:
        advertencias.append("tramo sin audio detectado; solo se conserva evidencia visual parcial")

    gaps_info = detectar_gaps_transcript(segs, capturas, origen)
    gaps = gaps_info["gaps_sin_segmentos"]
    periodos = gaps_info["periodos_con_capturas_sin_audio"]

    if periodos:
        advertencias.append("hay periodos con muchas capturas y sin transcript cercano")
    if wav_util_sin_transcript:
        advertencias.append("hay WAV con RMS util sin transcript cercano; se difiere limpieza")
    if comparacion_chunks["wav_sin_transcript"]:
        advertencias.append("hay WAV con commit STT pendiente o inválido")
    if comparacion_chunks["commits_vacios"]:
        advertencias.append(
            "hay commits STT completos sin segmentos; no están pendientes")
    if comparacion_chunks["transcript_wav_faltante"]:
        advertencias.append("el transcript referencia WAV inexistentes; no se puede reauditar todo el audio")

    rango = gaps_info["rango_transcript"]
    gap_max = gaps_info["gap_max_segundos"]
    limpieza_segura = (
        not periodos and not wav_util_sin_transcript and
        not comparacion_chunks["wav_sin_transcript"] and
        not comparacion_chunks["transcript_wav_faltante"] and
        gap_max <= GAP_LARGO_SEG
    )
    if not segs:
        estado_cov = "sin_audio"
        limpieza_segura = False if capturas or wav_util else limpieza_segura
    elif wav_util_sin_transcript or comparacion_chunks["wav_sin_transcript"]:
        estado_cov = "dudosa"
    elif periodos:
        estado_cov = "parcial"
        limpieza_segura = False
    elif comparacion_chunks["transcript_wav_faltante"]:
        estado_cov = "parcial"
        limpieza_segura = False
    elif gap_max > GAP_LARGO_SEG:
        estado_cov = "parcial"
        limpieza_segura = False
    else:
        estado_cov = "ok"

    continuidad = continuidad_audio_contextual(d)
    if continuidad.get("status") == "warning":
        advertencias.append("continuidad PCM con deficit o gaps; se difiere limpieza")
        limpieza_segura = False
        if estado_cov == "ok":
            estado_cov = "dudosa"

    return {
        "wav_total": len(wavs),
        "wav_con_rms_util": wav_util,
        "metricas_wav_desde_manifest": metricas_desde_manifest,
        "metricas_wav_recalculadas": metricas_desde_wav,
        "wav_silenciosos": wav_silenciosos,
        "wav_info": wav_info,
        "wav_util_sin_transcript": wav_util_sin_transcript,
        "wav_sin_transcript": comparacion_chunks["wav_sin_transcript"][:10],
        "wav_sin_transcript_total": len(comparacion_chunks["wav_sin_transcript"]),
        "commits_vacios": comparacion_chunks["commits_vacios"][:10],
        "commits_vacios_total": len(comparacion_chunks["commits_vacios"]),
        "stt_invalidos_total": comparacion_chunks["stt"]["invalid_count"],
        "transcript_wav_faltante": comparacion_chunks["transcript_wav_faltante"][:10],
        "transcript_wav_faltante_total": len(comparacion_chunks["transcript_wav_faltante"]),
        "segmentos_total": len(segs),
        "rango_transcript": rango,
        "gaps_sin_segmentos": gaps,
        "gap_max_segundos": round(gap_max, 2),
        "gap_max_fmt": fmt_duracion(gap_max),
        "periodos_con_capturas_sin_audio": periodos,
        "cobertura_estado": estado_cov,
        "limpieza_segura": limpieza_segura,
        "continuidad_audio": continuidad,
        "advertencias": advertencias,
        "origen": origen,
    }


def _cobertura_md(cobertura: dict) -> str:
    lineas = [
        "## Cobertura de audio y evidencia",
        "",
        f"- Estado: **{cobertura.get('cobertura_estado', 'desconocida')}**.",
        f"- WAV auditados: {cobertura.get('wav_total', 0)}; WAV con RMS util: "
        f"{cobertura.get('wav_con_rms_util', 0)}.",
        f"- Segmentos transcript: {cobertura.get('segmentos_total', 0)}.",
        f"- Gap maximo sin transcript: {fmt_duracion(cobertura.get('gap_max_segundos', 0))}.",
    ]
    for adv in cobertura.get("advertencias", []):
        lineas.append(f"- Advertencia: {adv}.")
    if cobertura.get("wav_sin_transcript_total"):
        lineas.append("- WAV existentes sin transcript: "
                      f"{cobertura.get('wav_sin_transcript_total')} "
                      f"({', '.join(cobertura.get('wav_sin_transcript', []))}).")
    if cobertura.get("transcript_wav_faltante_total"):
        lineas.append("- Transcript que referencia WAV inexistentes: "
                      f"{cobertura.get('transcript_wav_faltante_total')} "
                      f"({', '.join(cobertura.get('transcript_wav_faltante', []))}).")
    if cobertura.get("periodos_con_capturas_sin_audio"):
        lineas.append("")
        lineas.append("> Tramo sin audio detectado; solo se conserva evidencia visual parcial.")
    if not cobertura.get("limpieza_segura", True):
        lineas.append("> Limpieza de evidencia diferida por cobertura dudosa.")
    return "\n".join(lineas).strip() + "\n"


def _cobertura_estudiante_md(cobertura: dict) -> str:
    """Resume límites útiles para estudiar sin publicar telemetría del pipeline."""
    state = str(cobertura.get("cobertura_estado", "") or "").casefold()
    warnings = list(cobertura.get("advertencias", []) or [])
    if state in {"ok", "completa", "segura"} and not warnings:
        return ""
    lines = [
        "## Límites de la grabación", "",
        "> La lección conserva el contenido recuperable, pero hay evidencia que conviene "
        "contrastar con el material oficial.", "",
    ]
    continuity = cobertura.get("continuidad_audio") or {}
    if continuity.get("status") == "warning" or any(
            "gap" in str(item).casefold() or "continuidad" in str(item).casefold()
            for item in warnings):
        lines.append("- Se detectó al menos una transición o discontinuidad temporal de audio.")
    if cobertura.get("wav_sin_transcript_total"):
        lines.append("- Existe audio conservado que todavía no tiene texto autoritativo.")
    if cobertura.get("periodos_con_capturas_sin_audio"):
        lines.append("- Algunas capturas no tienen explicación oral cercana verificable.")
    if len(lines) == 4:
        lines.append("- La cobertura fue marcada para revisión; consulta el transcript y el deck.")
    return "\n".join(lines).strip() + "\n"


def separar_publicacion_y_diagnostico(
        *, contenido: str, cobertura: dict, reportes: list[str]) -> tuple[str, str]:
    public_parts = [_cobertura_estudiante_md(cobertura), str(contenido or "").strip()]
    public = "\n\n---\n\n".join(part for part in public_parts if part.strip()).strip()
    technical = "\n\n---\n\n".join(
        str(report or "").strip() for report in reportes if str(report or "").strip()
    ).strip()
    return public + ("\n" if public else ""), technical + ("\n" if technical else "")


def _reporte_replays_md(report: dict) -> str:
    lines = [
        "## Reporte de replays del transcript", "",
        f"- Segmentos canónicos: {report.get('segments_total', 0)}.",
        f"- Segmentos excluidos solo de la vista derivada: "
        f"{report.get('replayed_segments', 0)}.",
        f"- Secuencias de replay: {report.get('replayed_runs', 0)}.",
        "- `transcript.jsonl` y commits STT: sin cambios.",
    ]
    for run in report.get("runs", []):
        lines.append(
            "- Replay derivado: índices "
            f"{run.get('later_start_index')}–{run.get('later_end_index')}; "
            f"separación {run.get('separation_seconds')} s; "
            f"{run.get('anchor_count')} anclajes; similitud mediana "
            f"{run.get('similarity_median')}.")
    return "\n".join(lines).strip() + "\n"


def _linea_wav_reporte(info: dict | None) -> str:
    if not info:
        return "(sin WAV)"
    return (f"{info.get('nombre')} | hora {info.get('hora')} | "
            f"LastWriteTime {info.get('last_write')}")


def _linea_segmento_reporte(info: dict | None) -> str:
    if not info:
        return "(sin segmento)"
    return (f"{info.get('timestamp')} | {info.get('chunk')} | "
            f"{info.get('texto')}")


def _lista_corta(items: list[str], total: int) -> str:
    if not total:
        return "0"
    extra = "" if total <= len(items) else f" (+{total - len(items)} más)"
    return f"{total}: {', '.join(items)}{extra}"


def _reporte_cobertura_temporal_md(reporte: dict) -> str:
    dur_wav = reporte.get("duracion_wav_fmt", "00:00")
    if reporte.get("duracion_wav_estimada"):
        dur_wav += " (estimada parcialmente)"
    factor = float(reporte.get("velocidad_factor", 1.0) or 1.0)
    codigo_vel = str(reporte.get("velocidad_codigo", "") or "sin dato")
    conteos_vel = reporte.get("velocidad_conteos", {}) or {}
    conteos_txt = ", ".join(f"{k}={v}" for k, v in sorted(conteos_vel.items())) or "sin sufijo vNN"
    captura = reporte.get("captura_sesion") or {}
    continuidad = reporte.get("continuidad_audio") or {}
    lineas = [
        "## Reporte de cobertura temporal",
        "",
        f"- WAV existentes: {reporte.get('wav_total', 0)}.",
        f"- Duración WAV estimada: {dur_wav}.",
        f"- Velocidad registrada por ClaseViva: {factor:g}x ({codigo_vel}; {conteos_txt}).",
        "- Duración real capturada: "
        f"{reporte.get('duracion_wav_fmt', '00:00')}.",
        "- Duración equivalente estimada de video: "
        f"{reporte.get('duracion_video_equivalente_fmt', reporte.get('duracion_wav_fmt', '00:00'))}.",
        f"- Primer WAV: {_linea_wav_reporte(reporte.get('primer_wav'))}.",
        f"- Último WAV: {_linea_wav_reporte(reporte.get('ultimo_wav'))}.",
        "- Primer segmento transcript: "
        f"{_linea_segmento_reporte(reporte.get('primer_segmento'))}.",
        "- Último segmento transcript: "
        f"{_linea_segmento_reporte(reporte.get('ultimo_segmento'))}.",
        f"- Duración transcript aproximada: {reporte.get('duracion_transcript_fmt', '00:00')}.",
        "- WAV sin transcript: "
        f"{_lista_corta(reporte.get('wav_sin_transcript', []), reporte.get('wav_sin_transcript_total', 0))}.",
        "- Transcript que referencia WAV inexistentes: "
        f"{_lista_corta(reporte.get('transcript_wav_faltante', []), reporte.get('transcript_wav_faltante_total', 0))}.",
        f"- Gap máximo entre segmentos: {reporte.get('gap_max_fmt', '00:00')}.",
        f"- Continuidad PCM: **{continuidad.get('status', 'desconocida')}** "
        f"({continuidad.get('source', 'sin manifest')}).",
        f"- Déficit PCM estimado: {continuidad.get('deficit_seconds', 0):.3f} s; "
        f"gaps entre WAV: {continuidad.get('wall_gap_total_seconds', 0):.3f} s "
        f"(p95 {continuidad.get('wall_gap_p95_seconds', 0):.3f} s; "
        f"máximo {continuidad.get('wall_gap_max_seconds', 0):.3f} s).",
        f"- Estado de cobertura: **{reporte.get('estado', 'advertencia')}**.",
    ]
    if captura:
        lineas.extend([
            f"- Duración de sesión en reloj: {captura.get('duracion_reloj_fmt', '00:00')}.",
            "- Tiempo efectivo almacenando: "
            f"{captura.get('duracion_almacenando_fmt', '00:00')}.",
            "- Auto-pausas registradas: "
            f"{captura.get('auto_pausa_total', 0)}"
            + (f" (primera a {captura.get('primera_auto_pausa_fmt')})"
               if captura.get("primera_auto_pausa_fmt") else "")
            + ".",
            f"- Pausas manuales registradas: {captura.get('pausa_manual_total', 0)}.",
        ])
        if captura.get("auto_pausa_actual"):
            motivo = captura.get("auto_pausa_motivo") or "sin motivo"
            lineas.append(f"- La clase finalizó con auto-pausa activa: {motivo}.")
        if captura.get("ultima_auto_pausa_nota"):
            lineas.append(f"- Última auto-pausa: {captura.get('ultima_auto_pausa_nota')}.")
    for adv in reporte.get("advertencias", []):
        lineas.append(f"- Advertencia: {adv}.")
    lineas.append("")
    lineas.append("> Si el video se reprodujo más tiempo que la duración WAV/transcript "
                  "reportada, esa parte no fue capturada o no quedó transcrita; "
                  "DeepSeek no puede reconstruir audio inexistente.")
    if codigo_vel == "v10":
        lineas.append("> Si el reproductor externo estuvo a x2 pero ClaseViva quedó en x1, "
                      "la duración equivalente del video puede ser aproximadamente el doble; "
                      "para reportes exactos, configurar también velocidad 2x en ClaseViva.")
    elif codigo_vel == "v20":
        lineas.append("> ClaseViva registró velocidad 2x: la duración equivalente de video se "
                      "estima como duración real WAV x 2 y el transcript fue ajustado a la "
                      "velocidad configurada.")
    if reporte.get("wav_sin_transcript_total"):
        lineas.append("> Hay chunks WAV pendientes de transcripción: espera o reinicia el "
                      "transcriptor/reanuda el pipeline antes de dar la cobertura por completa. "
                      "Si consolidas ahora, considéralo parcial.")
    return "\n".join(lineas).strip() + "\n"


def _ocr_existente(c: dict) -> str:
    for k in ("ocr", "texto_ocr", "ocr_texto", "texto_visible", "texto"):
        v = str(c.get(k, "") or "").strip()
        if v:
            return v
    return ""


def _ocr_cache_enabled() -> bool:
    return os.environ.get("CLASE_POST_OCR_CACHE", "1").strip().casefold() not in {
        "0", "false", "no", "off",
    }


def _ocr_cache_path(d: Path) -> Path:
    return Path(d) / "_debug" / "ocr_cache.jsonl"


def _ocr_cache_for_class(d: Path) -> dict[tuple[str, str, str], str]:
    key = str(Path(d).resolve())
    cached = _OCR_CACHE_BY_CLASS.get(key)
    if cached is not None:
        return cached
    cached = {}
    for row in est.leer_jsonl(_ocr_cache_path(d)):
        if row.get("version") != OCR_CACHE_VERSION:
            continue
        archivo = str(row.get("archivo", "") or "")
        sha256 = str(row.get("sha256", "") or "")
        scope = str(row.get("scope", "full") or "full")
        texto = str(row.get("texto", "") or "").strip()
        if archivo and sha256 and texto:
            cached[(archivo, sha256, scope)] = texto
    _OCR_CACHE_BY_CLASS[key] = cached
    return cached


def _run_ocr_image(path: Path) -> str:
    from . import ocr as _ocr
    return str(_ocr.texto_de_imagen(path) or "").strip()


def _ocr_completo_para_captura(d: Path, c: dict) -> str:
    texto = _ocr_existente(c)
    if texto:
        return texto
    image_path = Path(d) / "img" / str(c.get("archivo", "") or "")
    if not image_path.is_file():
        return ""
    sha256 = ""
    cache: dict[tuple[str, str, str], str] | None = None
    if _ocr_cache_enabled():
        try:
            sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
            cache = _ocr_cache_for_class(d)
            hit = cache.get((image_path.name, sha256, "full"))
            if hit is not None:
                return hit
        except OSError:
            sha256 = ""
            cache = None
    try:
        texto = _run_ocr_image(image_path)
    except Exception:
        texto = ""
    if texto and sha256 and cache is not None:
        row = {
            "version": OCR_CACHE_VERSION,
            "archivo": image_path.name,
            "sha256": sha256,
            "scope": "full",
            "texto": texto,
        }
        est.anexar_jsonl(_ocr_cache_path(d), row)
        cache[(image_path.name, sha256, "full")] = texto
    return texto


def _ocr_para_captura(d: Path, c: dict) -> str:
    """OCR reducido para prompts; identidad visual usa la variante completa."""
    texto = _ocr_completo_para_captura(d, c)
    return texto[:OCR_MAX_CHARS]


def _ocr_region_principal_media(d: Path, slide: dict) -> str:
    """Separa el contenido principal de recomendaciones laterales de un reproductor.

    La identidad conserva el OCR completo. Solo la ficha explicativa usa este OCR
    espacial, y únicamente cuando hay varias señales de interfaz multimedia. Así
    una miniatura recomendada no se atribuye al video que realmente se presentó.
    """
    completo = str(slide.get("ocr", "") or "").strip()
    normal = _normalizar_busqueda(completo)
    media_signals = sum(term in normal for term in (
        "youtube", "suscrib", "subscribe", "reproductor", "video", "views",
        "compartir", "guardar", "download",
    ))
    if media_signals < 2 and "youtube" not in normal:
        return completo
    ruta = d / "img" / str(slide.get("archivo", "") or "")
    if not ruta.is_file():
        return completo
    sha256 = ""
    cache: dict[tuple[str, str, str], str] | None = None
    if _ocr_cache_enabled():
        try:
            sha256 = hashlib.sha256(ruta.read_bytes()).hexdigest()
            cache = _ocr_cache_for_class(d)
        except OSError:
            sha256 = ""
            cache = None
    scopes = [
        (0.52, "media_primary_x52_y04_90"),
        (0.66, "media_primary_x66_y04_90"),
    ]
    cached_candidates = (
        [cache.get((ruta.name, sha256, scope)) for _, scope in scopes]
        if cache is not None else []
    )
    candidates = (
        [str(text) for text in cached_candidates]
        if cached_candidates and all(text is not None for text in cached_candidates)
        else []
    )
    try:
        if not candidates:
            from . import ocr as ocr_local
            with Image.open(ruta) as source:
                source.load()
                if source.width < source.height * 1.35:
                    return completo
                for fraction, scope in scopes:
                    hit = (
                        cache.get((ruta.name, sha256, scope))
                        if cache is not None else None
                    )
                    if hit is not None:
                        candidates.append(hit)
                        continue
                    crop = source.crop((
                        0, int(source.height * 0.04),
                        int(source.width * fraction), int(source.height * 0.90),
                    ))
                    text = str(ocr_local.texto_de_pil(crop) or "").strip()
                    candidates.append(text)
                    if text and sha256 and cache is not None:
                        est.anexar_jsonl(_ocr_cache_path(d), {
                            "version": OCR_CACHE_VERSION,
                            "archivo": ruta.name,
                            "sha256": sha256,
                            "scope": scope,
                            "texto": text,
                        })
                        cache[(ruta.name, sha256, scope)] = text
    except Exception:
        return completo
    candidates = [text for text in candidates if len(_tokens_relevantes(text)) >= 2]
    if not candidates:
        return completo
    transcript_tokens = _tokens_relevantes(
        str(slide.get("transcript_ventana", "") or ""))

    def score(text: str) -> tuple[float, int]:
        tokens = _tokens_relevantes(text)
        common = len(tokens & transcript_tokens)
        precision = common / max(1, len(tokens))
        ui_noise = sum(term in _normalizar_busqueda(text) for term in (
            "suscrib", "subscribe", "recomend", "views", "compartir", "guardar",
        ))
        if common == 0:
            # En clases bilingües puede no haber tokens compartidos. Como los recortes
            # son anidados, el más compacto reduce la probabilidad de incluir la barra.
            return (0.0, -len(tokens))
        # Entre recortes anidados prima la precisión: un recorte ancho no gana por
        # sumar recomendaciones laterales que casualmente comparten una palabra.
        return (common * 2.0 + precision * 60.0
                + min(1.5, len(tokens) / 12.0) - ui_noise * 3.0, -len(tokens))

    return max(candidates, key=score)


def _anotar_ocr_principal(d: Path, slides: list[dict]) -> list[dict]:
    result = []
    for raw in slides:
        slide = dict(raw)
        principal = _ocr_region_principal_media(d, slide)
        if principal and principal != str(slide.get("ocr", "") or "").strip():
            slide["ocr_principal"] = principal
            slide["ocr_principal_origen"] = "region_multimedia_principal"
        result.append(slide)
    return result


def _promover_ocr_principal(slides: list[dict]) -> list[dict]:
    """Hace que matching y roles consuman el área docente, conservando el OCR completo."""
    result = []
    for raw in slides:
        slide = dict(raw)
        principal = str(slide.get("ocr_principal", "") or "").strip()
        completo = str(slide.get("ocr", "") or "").strip()
        if principal and principal != completo:
            slide["ocr_completo"] = completo
            slide["ocr"] = principal
            slide["ocr_clasificacion_origen"] = "region_multimedia_principal"
        result.append(slide)
    return result


def _titulo_verificado_media(ocr_principal: str) -> str:
    """Extrae un título corto del área principal de un reproductor.

    Se exige un año visible porque aporta un límite textual estable y evita
    convertir controles o palabras sueltas de la interfaz en un título.
    """
    texto = re.sub(r"\s+", " ", str(ocr_principal or "")).strip()
    if not texto:
        return ""
    youtube = re.search(r"\byoutube\b\s*", texto, flags=re.I)
    if youtube:
        texto = texto[youtube.end():]
    match = re.match(r"(.{4,110}?\b(?:19|20)\d{2})\b", texto, flags=re.I)
    if not match:
        return ""
    titulo = match.group(1).strip(" -–—:|·")
    return titulo if len(_tokens_relevantes(titulo)) >= 3 else ""


def _corregir_identidad_media_periferica(
        markdown: str, slides: list[dict]) -> str:
    """Impide que una recomendación lateral nombre el material presentado.

    El gate combina OCR espacial, contradicción con transcript, una ancla de
    identidad y la estructura Markdown. Solo reescribe la sección cuyo título
    mezcla la ancla principal con una identidad exclusiva del área periférica.
    """
    texto = str(markdown or "")
    if not texto.strip():
        return texto
    for slide in slides:
        principal = str(slide.get("ocr_principal") or "").strip()
        completo = str(slide.get("ocr") or "").strip()
        if not principal or not completo or principal == completo:
            continue
        titulo = _titulo_verificado_media(principal)
        if not titulo:
            continue
        tokens_principal = _tokens_relevantes(principal)
        tokens_transcript = _tokens_relevantes(
            str(slide.get("transcript_ventana") or ""))
        perifericos = {
            token for token in (_tokens_relevantes(completo) - tokens_principal
                                - tokens_transcript)
            if len(token) >= 5 and token not in {
                "youtube", "video", "views", "subscribe", "suscribirse",
                "recomendado", "recomendacion", "sidebar", "crear",
            }
        }
        if not perifericos:
            continue
        titulo_tokens = _tokens_relevantes(titulo)
        anios = set(re.findall(r"\b(?:19|20)\d{2}\b", titulo))
        lineas = texto.splitlines()
        indice = 0
        while indice < len(lineas):
            heading = re.match(r"^(#{2,6})\s+(.+?)\s*$", lineas[indice])
            if not heading:
                indice += 1
                continue
            tokens_heading = _tokens_relevantes(heading.group(2))
            sospechosos = perifericos & tokens_heading
            comparte_ancla = bool(anios & set(re.findall(
                r"\b(?:19|20)\d{2}\b", heading.group(2))))
            comparte_titulo = len(titulo_tokens & tokens_heading) >= 2
            if not sospechosos or not (comparte_ancla or comparte_titulo):
                indice += 1
                continue
            nivel = len(heading.group(1))
            fin = indice + 1
            while fin < len(lineas):
                siguiente = re.match(r"^(#{1,6})\s+", lineas[fin])
                if siguiente and len(siguiente.group(1)) <= nivel:
                    break
                fin += 1
            bloque = lineas[indice + 1:fin]
            patron_sospechoso = re.compile(
                r"\b(?:" + "|".join(
                    re.escape(token) for token in sorted(
                        sospechosos, key=len, reverse=True))
                + r")(?:[\s-]*\d+)?\b",
                flags=re.I,
            )
            for pos, linea in enumerate(bloque):
                if patron_sospechoso.search(linea):
                    bloque[pos] = (
                        f"El profesor presentó el video *{titulo}*. La identificación "
                        "procede del área principal del reproductor; los textos "
                        "laterales no se usaron para nombrarlo."
                    )
                    break
            nota = (
                f"> **Título verificado en la captura principal:** *{titulo}*. "
                "Las recomendaciones y textos laterales se excluyeron de la identidad."
            )
            lineas[indice:fin] = [
                f"{heading.group(1)} Video presentado: *{titulo}*", "", nota, "",
                *bloque,
            ]
            texto = "\n".join(lineas)
            break
    return texto


RUIDO_VISUAL = {
    "loading", "cargando", "youtube", "suscribete", "suscríbete", "subscribe",
    "skip ad", "omitir anuncio", "anuncio", "publicidad", "miniatura", "thumbnail",
}
CLAVES_CONCEPTUALES = {
    "importante", "concepto", "definicion", "definición", "recuerda", "examen",
    "clave", "atencion", "atención", "ejemplo", "formula", "fórmula", "codigo",
    "código", "algoritmo", "modelo", "dataset", "loss", "gradient", "red neuronal",
    "machine learning", "deep learning", "clasificacion", "clasificación",
}


def _texto_ruido_visual(txt: str) -> bool:
    low = txt.lower()
    return any(p in low for p in RUIDO_VISUAL)


def _score_segmento(seg: dict) -> int:
    txt = str(seg.get("texto", ""))
    low = txt.lower()
    score = 0
    score += min(8, len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ_]{8,}", txt)) // 4)
    score += 3 * sum(1 for p in CLAVES_CONCEPTUALES if p in low)
    score += 3 if re.search(r"[=+\-*/^]|\b(if|for|while|def|class|return|import)\b", txt) else 0
    score += 2 if re.search(r"\d+(?:\.\d+)?\s*(%|ms|s|min|MB|GB|epoch|batch)", txt) else 0
    score += 2 if ":" in txt or ";" in txt else 0
    return score


def _hash_captura(d: Path, c: dict) -> int | None:
    if c.get("dhash"):
        try:
            return int(str(c["dhash"]), 16)
        except ValueError:
            pass
    ruta = d / "img" / str(c.get("archivo", ""))
    if not ruta.is_file():
        return None
    try:
        return imagen.dhash(Image.open(ruta))
    except Exception:
        return None


def _dedup_capturas(d: Path, capturas: list[dict]) -> list[dict]:
    unicas, hashes = [], []
    for c in sorted(capturas, key=_cap_t):
        if not c.get("archivo") or not (d / "img" / str(c.get("archivo"))).is_file():
            continue
        h = _hash_captura(d, c)
        if h is None:
            unicas.append(dict(c))
            continue
        if all(imagen.distancia(h, k) > UMBRAL_DEDUP for k in hashes):
            hashes.append(h)
            unicas.append(dict(c, dhash=f"{h:016x}"))
    return unicas


def _tokens_ocr(txt: str) -> set[str]:
    return {w for w in re.findall(r"[a-záéíóúñ0-9]{4,}", (txt or "").lower())
            if w not in {"para", "esta", "este", "with", "that", "from", "como"}}


def _riqueza_ocr(txt: str) -> int:
    lineas = [ln.strip() for ln in (txt or "").splitlines() if ln.strip()]
    return len(_tokens_ocr(txt)) * 3 + min(60, len(txt or "") // 12) + len(lineas) * 2


def _firma_formula_ocr(txt: str) -> tuple[str, ...]:
    normal = _normalizar_busqueda(txt or "")
    if not re.search(r"(?:==|!=|<=|>=|->|[=+*/^<>])|\b\d+(?:[.,]\d+)?\b", normal):
        return ()
    return tuple(re.findall(
        r"[a-z_][a-z0-9_]*|\d+(?:[.,]\d+)?|==|!=|<=|>=|->|[=+\-*/^<>]",
        normal,
    ))


def _ocr_cambio_importante(a: str, b: str) -> bool:
    if _normalizar_busqueda(a).strip() == _normalizar_busqueda(b).strip():
        return False
    firma_a = _firma_formula_ocr(a)
    firma_b = _firma_formula_ocr(b)
    # dHash no detecta de forma fiable el cambio de un digito u operador. Una
    # formula corta distinta es evidencia semantica suficiente para no colapsar.
    if firma_a and firma_b and firma_a != firma_b:
        return True
    ta = _tokens_ocr(a)
    tb = _tokens_ocr(b)
    if len(ta | tb) < 6:
        # Para archivos distintos, poco texto sigue siendo identidad: cambiar
        # "Agenda" por "Grading" no es ruido aunque compartan toda la plantilla.
        # Un conjunto contenido en el otro sí representa un build progresivo.
        if ta and tb and (ta <= tb or tb <= ta):
            return False
        return ta != tb or _normalizar_busqueda(a) != _normalizar_busqueda(b)
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    return jaccard < 0.45 and (len(ta - tb) >= 3 or len(tb - ta) >= 3)


def _preferir_representante_slide(prev_ocr: str, nuevo_ocr: str,
                                  prev_t: float, nuevo_t: float) -> bool:
    """En slides progresivas conserva la captura final o más rica del mismo estado."""
    prev_r = _riqueza_ocr(prev_ocr)
    nuevo_r = _riqueza_ocr(nuevo_ocr)
    if nuevo_ocr and not prev_ocr:
        return True
    if nuevo_r >= prev_r + 8:
        return True
    if len(nuevo_ocr or "") >= max(40, int(len(prev_ocr or "") * 1.25)):
        return True
    return nuevo_t > prev_t and nuevo_r >= prev_r and len(nuevo_ocr or "") >= len(prev_ocr or "")


STOPWORDS_TEMA = {
    "para", "como", "esta", "este", "estos", "estas", "with", "that", "from",
    "they", "have", "will", "your", "their", "sobre", "pero", "porque", "entonces",
    "clase", "curso", "tema", "slide", "slides", "profesor", "profesora",
}

SENALES_INICIO_CLASE = (
    "welcome to class", "welcome everyone", "today we will", "today we're going",
    "next lecture", "now let's move to", "let us move to", "buenos dias",
    "buenos días", "vamos a empezar", "empecemos", "la clase de hoy",
    "hoy vamos a", "continuamos con", "next class", "new lecture",
)

SENALES_NUEVA_CLASE_FUERTES = (
    "welcome to class", "welcome everyone", "new lecture", "next class",
    "buenos dias", "buenos días", "vamos a empezar", "empecemos",
    "la clase de hoy",
)

TERMINOS_INTERFERENCIA = (
    "football", "soccer", "basketball", "music video", "official video",
    "lyrics", "song", "deporte", "partido", "gol", "musica", "música",
)


def _tokens_relevantes(txt: str) -> set[str]:
    normal = _normalizar_busqueda(txt)
    return {
        w for w in re.findall(r"[a-z0-9][a-z0-9_\-]{2,}", normal)
        if w not in STOPWORDS_TEMA and not w.isdigit()
    }


def _sim_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _contiene_patron_tecnico(txt: str) -> bool:
    return bool(re.search(
        r"https?://|www\.|[A-Za-z_]\w*\s*=\s*[^\s,;]{1,80}|"
        r"(?:\b[A-Za-z_]\w*|\b\d+(?:\.\d+)?)\s*[+*/^]\s*"
        r"(?:[A-Za-z_]\w*|\d+(?:\.\d+)?)|"
        r"\[(?:\s*[-+]?(?:\d+(?:\.\d+)?|[A-Za-z_]\w*)\s*,){1,}|"
        r"\b(if|for|while|def|class|return|import|SELECT|FROM)\b|"
        r"\b\d{4}\b|\b\d+(?:\.\d+)?\s*(%|ms|s|min|MB|GB|epoch|batch)\b",
        txt or "", flags=re.I))


def _contiene_codigo_formula(txt: str) -> bool:
    """Señal estructural fuerte; URL, año o unidad aislada no bastan."""
    return bool(re.search(
        r"\b(?:import\s+[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*|"
        r"from\s+[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s+import\s+[A-Za-z_*]|"
        r"def\s+[A-Za-z_]\w*\s*\(|class\s+[A-Za-z_]\w*(?:\([^)]*\))?\s*:|"
        r"return\s+(?:[A-Za-z_]\w*|\[|\(|[-+]?\d)|print\s*\(|"
        r"pip\s+install|conda\s+(?:install|create)|SELECT\s+.+\s+FROM)|"
        r"\b[A-Za-z_]\w*\s*(?:==|<=|>=|=|->|→)\s*"
        r"(?:[-+]?\d|[A-Za-z_]|\[|\()|"
        r"(?:\b[A-Za-z_]\w*|\b\d+(?:\.\d+)?)\s*[+*/^]\s*"
        r"(?:[A-Za-z_]\w*|\d+(?:\.\d+)?)|"
        r"\[(?:\s*[-+]?(?:\d+(?:\.\d+)?|[A-Za-z_]\w*)\s*,){1,}|"
        r"\b(?:mse|mae|relu|softmax|sigmoid|argmax|model\.fit)\s*\(",
        txt or "", flags=re.I))


def _analizar_visual_local(d: Path, captura: dict, ocr_txt: str = "") -> dict:
    """Clasificador visual CPU barato para no depender de modelos GPU durante clase.

    Detecta evidencia docente sin OCR abundante, como diagramas o gráficos, usando
    densidad de bordes y contraste. No decide solo: se combina con transcript y duración.
    """
    ruta = d / "img" / str(captura.get("archivo", ""))
    if not ruta.is_file():
        return {"visual_tipo": "sin_archivo", "visual_score": 0.0, "visual_razones": []}
    try:
        with Image.open(ruta) as img:
            img = img.convert("RGB")
            ancho = 160
            alto = max(48, round(img.height * ancho / max(1, img.width)))
            thumb = img.resize((ancho, alto), Image.LANCZOS)
            gris = thumb.convert("L")
            hist = gris.histogram()
            total = max(1, sum(hist))
            mean = sum(i * c for i, c in enumerate(hist)) / total
            var = sum(((i - mean) ** 2) * c for i, c in enumerate(hist)) / total
            std = math.sqrt(var)
            dark_ratio = sum(hist[:48]) / total
            bright_ratio = sum(hist[210:]) / total
            edges = gris.filter(ImageFilter.FIND_EDGES)
            edge_mean = ImageStat.Stat(edges).mean[0]
            rgb_std = sum(ImageStat.Stat(thumb).stddev) / 3.0
    except Exception:
        return {"visual_tipo": "error_visual", "visual_score": 0.0, "visual_razones": []}

    normal = _normalizar_busqueda(ocr_txt)
    razones: list[str] = []
    score = 0.0
    tipo = "captura_generica"
    plano = std < 8 and edge_mean < 5 and rgb_std < 10
    oscuro_idle = dark_ratio > 0.75 and bright_ratio < 0.10 and edge_mean < 12

    if _texto_ruido_visual(ocr_txt):
        tipo = "ruido_visual"
        score -= 8
        razones.append("ruido_ocr")
    elif _contiene_codigo_formula(ocr_txt):
        tipo = "codigo_formula_tabla"
        score += 12
        razones.append("patron_tecnico")
    elif len(_tokens_ocr(ocr_txt)) >= 8:
        tipo = "slide_textual"
        score += 8
        razones.append("ocr_textual")
    elif not plano and not oscuro_idle and edge_mean >= 12 and std >= 18:
        tipo = "diagrama_o_grafico_probable"
        score += min(10.0, edge_mean / 2.2) + min(4.0, std / 18.0)
        razones.append("bordes_y_contraste")
        if rgb_std >= 24:
            score += 2
            razones.append("color_informativo")
    elif plano or oscuro_idle:
        tipo = "pantalla_plana_o_idle"
        score -= 6
        razones.append("baja_informacion_visual")

    if re.search(r"\b(diagrama|grafico|gráfico|arquitectura|pipeline|matriz|tabla|"
                 r"plot|chart|network|layer|capas?)\b", normal):
        score += 5
        razones.append("termino_visual_docente")

    return {
        "visual_tipo": tipo,
        "visual_score": round(score, 2),
        "visual_razones": razones,
        "visual_metricas": {
            "edge_mean": round(edge_mean, 2),
            "std": round(std, 2),
            "dark_ratio": round(dark_ratio, 3),
            "bright_ratio": round(bright_ratio, 3),
            "rgb_std": round(rgb_std, 2),
        },
    }


def _lineas_ocr_distintivas(ocr: str, transcript_ventana: str = "",
                            max_chars: int = 600) -> tuple[str, int, int]:
    bruto = len(ocr or "")
    transcript_tokens = _tokens_relevantes(transcript_ventana)
    lineas: list[str] = []
    vistas: list[str] = []
    for raw in (ocr or "").splitlines():
        ln = re.sub(r"\s+", " ", raw).strip(" -\t")
        if not ln:
            continue
        normal = _normalizar_busqueda(ln)
        if any(difflib.SequenceMatcher(None, normal, prev).ratio() >= 0.92
               for prev in vistas):
            continue
        vistas.append(normal)
        toks = _tokens_relevantes(ln)
        aporta = (
            not lineas
            or _contiene_patron_tecnico(ln)
            or bool(toks & transcript_tokens)
            or len(ln) >= 18
            or any(k in normal for k in ("tarea", "deadline", "assignment", "dataset"))
        )
        if aporta:
            lineas.append(ln)
        if sum(len(x) + 3 for x in lineas) >= max_chars:
            break
    if not lineas:
        return "", bruto, 0
    titulo = lineas[0]
    bullets = [f"- {ln}" for ln in lineas[1:10]]
    reducido = "\n".join([titulo] + bullets)
    if len(reducido) > max_chars:
        reducido = reducido[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."
    return reducido, bruto, len(reducido)


def _presupuesto_slides_segmento(slides: list[dict], segs: list[dict]) -> int:
    if not slides:
        return 0
    tiempos = [_cap_t(s) for s in slides]
    if segs:
        t_min = min(_seg_t(s) for s in segs)
        t_max = max(_seg_fin(s) for s in segs)
    else:
        t_min, t_max = min(tiempos), max(tiempos)
    dur_min = max(0.5, (t_max - t_min) / 60.0)
    densidad = len(slides) / dur_min
    keep_all_limite = max(_post_slides_max(), math.ceil(dur_min * _post_slides_keep_density()))
    if len(slides) <= keep_all_limite:
        return len(slides)
    por_tiempo = math.ceil(dur_min * _post_slides_budget_per_min())
    por_fraccion = math.ceil(len(slides) * _post_slides_budget_fraction())
    presupuesto = max(_post_slides_budget_min(), por_tiempo, por_fraccion)
    if densidad > _post_slides_keep_density() * 3:
        presupuesto = min(presupuesto, math.ceil(dur_min * _post_slides_budget_per_min() * 1.4))
    return min(len(slides), _post_slides_budget_max(), max(1, presupuesto))


def puntuar_slide_relevancia(slide: dict, transcript_ventana: str = "",
                             tema_actual: str = "", curso: str = "",
                             parte: int | str = "") -> dict:
    """Ranking local antes de enviar slides al LLM."""
    ocr_txt = str(slide.get("ocr", "") or "")
    normal_ocr = _normalizar_busqueda(ocr_txt)
    transcript_tokens = _tokens_relevantes(transcript_ventana)
    ocr_tokens = _tokens_relevantes(ocr_txt)
    tema_tokens = _tokens_relevantes(f"{tema_actual} {curso}")
    score = 0.0
    razones: list[str] = []
    penalizaciones: list[str] = []

    lineas = [ln.strip() for ln in ocr_txt.splitlines() if ln.strip()]
    if lineas and 4 <= len(lineas[0]) <= 90:
        score += 8
        razones.append("titulo")
    if len(lineas) >= 2:
        score += min(8, len(lineas) * 1.2)
        razones.append("bullets")
    if transcript_tokens and ocr_tokens:
        sim = _sim_jaccard(transcript_tokens, ocr_tokens)
        score += min(14, sim * 42)
        if sim >= 0.08:
            razones.append("alineada_con_transcript")
    if tema_tokens and ocr_tokens:
        sim_tema = _sim_jaccard(tema_tokens, ocr_tokens)
        score += min(6, sim_tema * 30)
        if sim_tema >= 0.08:
            razones.append("alineada_con_tema")
    if _contiene_patron_tecnico(ocr_txt):
        score += 10
        razones.append("tecnica")
    if re.search(r"\b(tarea|assignment|deadline|entrega|dataset|notebook)\b",
                 normal_ocr):
        score += 10
        razones.append("tarea")
    if _contiene_datos_personales_visuales(ocr_txt):
        score -= 60
        penalizaciones.append("listado_datos_personales")
    visual_tipo = str(slide.get("visual_tipo", "") or "")
    visual_score = float(slide.get("visual_score", 0) or 0)
    if visual_score > 0:
        score += min(10, visual_score)
        razones.extend(str(r) for r in slide.get("visual_razones", [])[:3])
    if visual_tipo in {"diagrama_o_grafico_probable", "codigo_formula_tabla"}:
        score += 6
        razones.append(visual_tipo)
    if "ocr_cambio_importante" in str(slide.get("motivo", "")):
        score += 8
        razones.append("ocr_nuevo")
    if slide.get("grupo_total", 1) > 1:
        score += min(4, math.log2(max(1, int(slide.get("grupo_total", 1)))))
        razones.append("pantalla_estable")
    if slide.get("sin_transcript_cercano"):
        score -= 10
        penalizaciones.append("sin_transcript")
    if slide.get("no_slide") or slide.get("es_interfaz_claseviva"):
        score -= 18
        penalizaciones.append("ui_no_slide")
    if _texto_ruido_visual(ocr_txt):
        score -= 12
        penalizaciones.append("ruido_visual")
    if len(ocr_tokens) < 3 and not transcript_tokens and visual_score < 6:
        score -= 8
        penalizaciones.append("pobre_sin_audio")
    if "sin texto legible" in normal_ocr or "probablemente" in normal_ocr:
        score -= 5
        penalizaciones.append("ocr_pobre")
    prev_ocr = str(slide.get("_prev_ocr", "") or "")
    if prev_ocr and ocr_txt:
        similitud_prev = difflib.SequenceMatcher(
            None, _normalizar_busqueda(prev_ocr), normal_ocr).ratio()
        if similitud_prev >= 0.90:
            score -= 12
            penalizaciones.append("ocr_repetido")
        elif similitud_prev <= 0.55:
            score += 5
            razones.append("novedad")

    return {
        "score": round(score, 2),
        "razones": razones,
        "penalizaciones": penalizaciones,
        "parte": parte,
    }


def _es_tarea_recurso_fuerte(texto: str) -> bool:
    """Distingue una instruccion accionable de una interfaz que solo nombra recursos."""
    normal = _normalizar_busqueda(texto)
    if re.search(
            r"\b(deadline|fecha limite|fecha de entrega|due date)\b",
            normal):
        return True
    if re.search(
            r"\b(tarea|assignment|actividad|cuestionario|examen)\s*"
            r"(?:(?:n(?:ro)?\.?\s*)?\d+\b|[:\-])", normal):
        return True
    consigna = re.search(
        r"\b(tarea|assignment|actividad|cuestionario|examen|evaluacion)\b", normal)
    entrega = re.search(r"\b(entregar|submit|presentar)\b", normal)
    return bool(consigna and entrega)


def motivos_slide_estudio(slide: dict) -> list[str]:
    """Razones fuertes para conservar una identidad aunque el presupuesto sea bajo.

    Permanecer visible o tener mucho OCR aumenta el *score*, pero no vuelve por sí solo
    imprescindible a una captura. Esa distinción evita publicar cada estado de una web o
    aplicación sin perder fórmulas, tareas, diagramas, material oficial ni láminas estables
    de poco texto.
    """
    if slide.get("no_slide") or slide.get("es_interfaz_claseviva"):
        return []
    ocr_txt = str(slide.get("ocr", "") or "")
    normal = _normalizar_busqueda(ocr_txt)
    prev_normal = _normalizar_busqueda(str(slide.get("_prev_ocr", "") or ""))
    ocr_repetido = bool(slide.get("_ocr_repetido_global")) or (
        bool(normal and prev_normal)
        and difflib.SequenceMatcher(None, prev_normal, normal).ratio() >= 0.90
    )
    atomos = set(re.findall(
        r"\b[A-Za-z_]\w*\s*(?:==|<=|>=|=|->|→)\s*[-+]?\d+(?:[.,]\d+)?\b",
        ocr_txt))
    atomos_previos = set(re.findall(
        r"\b[A-Za-z_]\w*\s*(?:==|<=|>=|=|->|→)\s*[-+]?\d+(?:[.,]\d+)?\b",
        str(slide.get("_prev_ocr", "") or "")))
    progreso_formula = bool(atomos and atomos_previos and atomos != atomos_previos)
    if (_texto_ruido_visual(ocr_txt)
            or _contiene_datos_personales_visuales(ocr_txt)):
        return []
    razones: list[str] = []
    if slide.get("material_oficial"):
        # El presupuesto limita frames opcionales, no páginas oficiales distintas.
        # Los duplicados de una misma página ya fueron colapsados por identidad.
        razones.append("material_oficial_emparejado")
    if slide.get("media_reference") and slide.get("visual_role") in {
            "principal", "secuencia"}:
        razones.append("demostracion_audiovisual")
    tarea = _es_tarea_recurso_fuerte(normal)
    estable = bool(slide.get("visual_estable"))
    pocos_tokens = len(_tokens_relevantes(ocr_txt)) <= 3
    if estable and pocos_tokens and not slide.get("sin_transcript_cercano"):
        razones.append("lamina_estable_poco_texto")
    identidad_formula = bool(atomos)
    if (identidad_formula and (not ocr_repetido or progreso_formula) and (
            estable or not slide.get("sin_transcript_cercano"))):
        razones.append("patron_tecnico")
    if tarea and not ocr_repetido:
        razones.append("tarea_recurso")
    visual_tipo = str(slide.get("visual_tipo", "") or "")
    visual_score = float(slide.get("visual_score", 0) or 0)
    visual_docente = (
        visual_tipo == "diagrama_o_grafico_probable"
        or (visual_tipo == "codigo_formula_tabla" and identidad_formula)
    )
    if visual_docente and visual_score >= 6 and (
            not slide.get("sin_transcript_cercano") or estable):
        razones.append(visual_tipo)
    if slide.get("representante") == "captura_mas_completa_o_final":
        razones.append("slide_progresiva_final")
    if (int(slide.get("grupo_total", 1)) >= 4
            and not slide.get("sin_transcript_cercano")
            and (identidad_formula or tarea or visual_docente)):
        razones.append("pantalla_estable_con_explicacion")
    return razones


def seleccionar_slides_por_tema(slides: list[dict], transcript_segmento,
                                presupuesto: int | None = None,
                                tema_actual: str = "", curso: str = "",
                                parte: int | str = "") -> list[dict]:
    if not slides:
        seleccionar_slides_por_tema.ultimo_reporte = {
            "slides_candidatas": 0, "presupuesto": 0, "slides_enviadas": 0,
            "slides_descartadas_presupuesto": 0,
        }
        return []
    segs = transcript_segmento if isinstance(transcript_segmento, list) else []
    texto = _texto_segs(segs) if segs else str(transcript_segmento or "")
    presupuesto = _presupuesto_slides_segmento(slides, segs) if presupuesto is None else int(presupuesto)
    presupuesto = max(0, min(presupuesto, len(slides)))
    puntuadas: list[dict] = []
    prev_ocr = ""
    ocr_vistos_norm: list[str] = []
    ocr_vistos_exactos: set[str] = set()
    for slide in slides:
        s = dict(slide)
        s["_prev_ocr"] = prev_ocr
        actual_norm = _normalizar_busqueda(str(s.get("ocr", "") or ""))
        repetido_global = False
        if actual_norm:
            repetido_global = (
                actual_norm in ocr_vistos_exactos
                or any(difflib.SequenceMatcher(None, visto, actual_norm).ratio() >= 0.90
                       for visto in ocr_vistos_norm[-80:])
            )
        s["_ocr_repetido_global"] = repetido_global
        transcript_ventana = str(s.get("transcript_ventana") or texto)
        rep = puntuar_slide_relevancia(s, transcript_ventana, tema_actual, curso, parte)
        s["score"] = rep["score"]
        s["score_detalle"] = rep
        protegida = motivos_slide_estudio(s)
        if protegida:
            s["protegida_estudio"] = True
            s["motivos_estudio"] = protegida
        if rep["razones"] or rep["penalizaciones"] or protegida:
            s["motivo"] = (
                f"{s.get('motivo', 'slide')}; score="
                f"{'/'.join(rep['razones'][:4]) or 'base'}"
                f"{'; penaliza=' + '/'.join(rep['penalizaciones'][:3]) if rep['penalizaciones'] else ''}"
                f"{'; estudio=' + '/'.join(protegida[:4]) if protegida else ''}"
            )
        puntuadas.append(s)
        if s.get("ocr"):
            prev_ocr = str(s.get("ocr", ""))
        if actual_norm and not repetido_global:
            ocr_vistos_norm.append(actual_norm)
            ocr_vistos_exactos.add(actual_norm)

    candidatas = [s for s in puntuadas if float(s.get("score", 0)) > 0]
    if not candidatas:
        candidatas = sorted(puntuadas, key=lambda x: float(x.get("score", 0)), reverse=True)[:1]
    protegidas = [s for s in puntuadas if s.get("protegida_estudio")]
    hard_max_base = min(len(slides), _post_slides_hard_max())
    # El techo controla frames opcionales, no evidencia marcada para estudio.
    # Los lotes posteriores mantienen acotado cada prompt aunque haya muchas
    # slides protegidas en una clase densa.
    hard_max = min(len(slides), max(hard_max_base, len(protegidas)))
    seleccion_dict: dict[str, dict] = {}
    for s in sorted(protegidas, key=lambda x: (-float(x.get("score", 0)), _cap_t(x))):
        seleccion_dict[str(s.get("archivo"))] = s
    cupo_base = min(hard_max, max(presupuesto, len(seleccion_dict)))
    if len(seleccion_dict) < cupo_base:
        for s in sorted(candidatas, key=lambda x: (-float(x.get("score", 0)), _cap_t(x))):
            seleccion_dict.setdefault(str(s.get("archivo")), s)
            if len(seleccion_dict) >= cupo_base:
                break
    if len(seleccion_dict) > hard_max:
        seleccion_dict = {
            str(s.get("archivo")): s
            for s in sorted(seleccion_dict.values(),
                            key=lambda x: (-float(x.get("score", 0)), _cap_t(x)))[:hard_max]
        }
    seleccion = sorted(seleccion_dict.values(), key=_cap_t)
    seleccionar_slides_por_tema.ultimo_reporte = {
        "slides_candidatas": len(slides),
        "presupuesto": presupuesto,
        "hard_max": hard_max,
        "hard_max_base": hard_max_base,
        "slides_protegidas_estudio": len(protegidas),
        "slides_enviadas": len(seleccion),
        "slides_descartadas_presupuesto": max(0, len(slides) - len(seleccion)),
        "score_min_enviado": min([float(s.get("score", 0)) for s in seleccion], default=0),
        "score_max_enviado": max([float(s.get("score", 0)) for s in seleccion], default=0),
    }
    return seleccion


def _slide_score(slide: dict, segs_cercanos: list[dict]) -> float:
    ocr_txt = str(slide.get("ocr", "") or "")
    score = 10.0
    score += min(10.0, len(ocr_txt) / 80)
    score += min(8.0, len(segs_cercanos) * 1.6)
    score += min(8.0, max(0.0, float(slide.get("visual_score", 0) or 0)))
    if _texto_ruido_visual(ocr_txt):
        score -= 6.0
    if (not segs_cercanos and len(ocr_txt) < 30
            and float(slide.get("visual_score", 0) or 0) < 6):
        score -= 4.0
    if "ocr_cambio_importante" in str(slide.get("motivo", "")):
        score += 4.0
    if slide.get("no_slide"):
        score -= 10.0
    return round(score, 2)


def _t_abs_desde_rel(t_rel: float, cap: dict, origen) -> float:
    modo, inicio = _origen_info(origen)
    if modo == "video":
        return t_rel
    raw = _to_float(cap.get("t", t_rel))
    if inicio:
        return raw if _parece_absoluto_de_clase(raw, inicio) else inicio + t_rel
    return raw


def _transcript_para_slide(slide: dict, idx: int, slides: list[dict],
                           segs: list[dict], t_ini: float, t_fin: float,
                           origen) -> tuple[list[dict], float, float]:
    intervalos = slide.get("intervalos_visuales") or []
    if intervalos:
        ventanas = [
            (max(t_ini, _to_float(it.get("inicio")) - SLIDE_PRE_SEG),
             min(t_fin, _to_float(it.get("fin"), _to_float(it.get("inicio")))
                 + SLIDE_POST_SEG))
            for it in intervalos
        ]
        cercanos = [
            s for s in segs
            if any(_seg_fin(s) >= inicio and _seg_t(s) <= fin
                   for inicio, fin in ventanas)
        ]
        return (cercanos, min(v[0] for v in ventanas),
                max(v[1] for v in ventanas))

    t = _cap_t(slide)
    inicio_grupo = _to_float(slide.get("t_inicio_rel", t), t)
    fin_grupo = _to_float(slide.get("t_fin_rel", t), t)
    inicio = max(t_ini, inicio_grupo - SLIDE_PRE_SEG)
    fin = min(t_fin, fin_grupo + SLIDE_POST_SEG)
    if idx + 1 < len(slides):
        sig = _cap_t(slides[idx + 1])
        if sig > t:
            fin = min(fin, max(t + 8, sig - 1))
    cercanos = [s for s in segs if _seg_fin(s) >= inicio and _seg_t(s) <= fin]
    return cercanos, inicio, fin


def construir_timeline_slides(
        d: Path, t_ini: float, t_fin: float, segmentos: list[dict], origen,
        filas_visuales: list[dict] | None = None) -> list[dict]:
    """Construye una línea visual compatible con capturas y heartbeats legacy/nuevos."""
    construir_timeline_slides.ultimo_reporte = {
        "capturas_interfaz_filtradas": 0,
        "capturas_interfaz_secundarias": 0,
        "heartbeats_procesados": 0,
        "cierres_conservadores_sin_fin": 0,
        "identidades_reaparecidas_global": 0,
    }
    a_rel = _normalizar_valor_tiempo(t_ini, origen)
    b_rel = _normalizar_valor_tiempo(t_fin, origen)
    filas_normalizadas = []
    if filas_visuales is None:
        # Post completo y evidencia legacy conservan la reconstruccion integral.
        # Escriba puede entregar una ventana incremental ya acotada.
        filas_visuales = list(incremental_jsonl.read_jsonl(d / "img_log.jsonl"))
        filas_visuales.extend(
            incremental_jsonl.read_jsonl(d / "visual_timeline.jsonl"))
    for orden, raw in enumerate(filas_visuales):
        archivo = str(raw.get("ref_archivo") or raw.get("archivo", "") or "")
        if not archivo or not (d / "img" / archivo).is_file():
            continue
        t_rel = normalizar_tiempo_captura(raw, origen)
        if t_rel <= b_rel:
            filas_normalizadas.append(dict(
                raw, archivo=archivo, t_rel=t_rel, _orden_visual=orden))

    prioridad_evento = {"captura": 0, "heartbeat": 1, "fin": 2}
    filas_normalizadas.sort(key=lambda cap: (
        _cap_t(cap), prioridad_evento.get(_tipo_evento_visual(cap), 0),
        int(cap.get("_orden_visual", 0))))
    esquema_nuevo = any(
        "evento" in cap and a_rel <= _cap_t(cap) <= b_rel
        for cap in filas_normalizadas
    )
    cadencias = []
    por_archivo_t: dict[str, float] = {}
    for cap in filas_normalizadas:
        if _tipo_evento_visual(cap) != "heartbeat":
            continue
        previo = por_archivo_t.get(cap["archivo"])
        if previo is not None and 0 < _cap_t(cap) - previo <= 30:
            cadencias.append(_cap_t(cap) - previo)
        por_archivo_t[cap["archivo"]] = _cap_t(cap)
    cadencia = sorted(cadencias)[len(cadencias) // 2] if cadencias else float(
        getattr(config, "CAPTURA_INTERVALO", 2.0) or 2.0)
    gracia_crash = max(2.0, min(15.0, cadencia * 2.5))

    # Reconstruye solo el estado activo previo al rango. No hace OCR de toda la
    # clase anterior, pero permite que una slide iniciada antes de ``t_ini``
    # conserve su visibilidad dentro del rango solicitado.
    definiciones: dict[str, dict] = {}
    activa_previa: str | None = None
    ultimo_previo = a_rel
    incluye_inicio = math.isclose(a_rel, 0.0, abs_tol=1e-9)
    for cap in filas_normalizadas:
        t_cap = _cap_t(cap)
        if t_cap > a_rel or (incluye_inicio and t_cap >= a_rel):
            break
        tipo = _tipo_evento_visual(cap)
        if tipo == "captura":
            definiciones[cap["archivo"]] = cap
            activa_previa = cap["archivo"]
            ultimo_previo = _cap_t(cap)
        elif tipo == "heartbeat":
            activa_previa = cap["archivo"]
            ultimo_previo = _cap_t(cap)
        elif tipo == "fin" and activa_previa == cap["archivo"]:
            activa_previa = None
            ultimo_previo = _cap_t(cap)

    eventos = []
    conocidas: set[str] = set()
    siguiente = next(
        (cap for cap in filas_normalizadas
         if _tiempo_en_rango(_cap_t(cap), a_rel, b_rel)), None)
    heartbeat_confirma_continuidad = bool(
        siguiente
        and _tipo_evento_visual(siguiente) == "heartbeat"
        and siguiente.get("archivo") == activa_previa
    )
    if (esquema_nuevo and activa_previa and activa_previa in definiciones
            and (a_rel - ultimo_previo <= gracia_crash
                 or heartbeat_confirma_continuidad)):
        eventos.append(dict(
            definiciones[activa_previa], evento="captura", t_rel=a_rel,
            _seed_estado_previo=True, _orden_visual=-1))
        conocidas.add(activa_previa)

    for cap in filas_normalizadas:
        t_cap = _cap_t(cap)
        if not _tiempo_en_rango(t_cap, a_rel, b_rel):
            continue
        tipo = _tipo_evento_visual(cap)
        if tipo == "captura":
            definiciones[cap["archivo"]] = cap
            conocidas.add(cap["archivo"])
        elif tipo == "heartbeat" and cap["archivo"] not in conocidas:
            definicion = definiciones.get(cap["archivo"])
            if definicion is not None:
                eventos.append(dict(
                    definicion, evento="captura", t_rel=t_cap,
                    _reanudada_por_heartbeat=True,
                    _orden_visual=int(cap.get("_orden_visual", 0)) - 1))
                conocidas.add(cap["archivo"])
        eventos.append(cap)
    eventos.sort(key=lambda cap: (
        _cap_t(cap), prioridad_evento.get(_tipo_evento_visual(cap), 0),
        int(cap.get("_orden_visual", 0))))

    slides: list[dict] = []
    slide_por_archivo: dict[str, int] = {}
    activa_idx: int | None = None
    ultimo_evento_activa = a_rel

    def cerrar_activa(t_rel: float) -> None:
        if activa_idx is None or not (0 <= activa_idx < len(slides)):
            return
        slide = slides[activa_idx]
        intervalos = slide.setdefault("intervalos_visuales", [])
        if not intervalos:
            inicio = _to_float(slide.get("t_inicio_rel", _cap_t(slide)))
            intervalos.append({"inicio": inicio, "fin": inicio})
        intervalo = intervalos[-1]
        intervalo["fin"] = max(_to_float(intervalo.get("inicio")),
                                _to_float(intervalo.get("fin")), t_rel)
        slide["t_fin_rel"] = max(
            _to_float(slide.get("t_fin_rel", _cap_t(slide))), t_rel)

    for cap in eventos:
        tipo_evento = _tipo_evento_visual(cap)
        t_cap = _cap_t(cap)
        if tipo_evento in {"heartbeat", "fin"}:
            ref_idx = slide_por_archivo.get(cap["archivo"])
            if ref_idx is not None and tipo_evento == "heartbeat" and ref_idx != activa_idx:
                cerrar_activa(t_cap)
                slides[ref_idx].setdefault("intervalos_visuales", []).append({
                    "inicio": t_cap, "fin": t_cap,
                })
                activa_idx = ref_idx
            if ref_idx is not None and ref_idx == activa_idx:
                cerrar_activa(t_cap)
                ultimo_evento_activa = t_cap
                if tipo_evento == "heartbeat":
                    construir_timeline_slides.ultimo_reporte["heartbeats_procesados"] += 1
                    slides[ref_idx]["heartbeats_total"] = int(
                        slides[ref_idx].get("heartbeats_total", 0)) + 1
            if tipo_evento == "fin" and ref_idx is not None and ref_idx == activa_idx:
                activa_idx = None
            continue

        activa_previa = activa_idx
        cerrar_activa(t_cap)
        h = _hash_captura(d, cap)
        # El OCR debe existir antes de decidir un merge por hash. De lo contrario,
        # dos slides con la misma plantilla pueden colapsarse sin defensa semántica.
        ocr_txt = _ocr_completo_para_captura(d, cap)
        similar_idx = None
        motivo = "slide_nueva"
        if h is not None:
            for i in range(len(slides) - 1, -1, -1):
                slide = slides[i]
                h_prev = slide.get("_hash_int")
                if h_prev is not None and imagen.distancia(h, h_prev) <= UMBRAL_DEDUP:
                    ocr_previo = str(slide.get("ocr", "") or "")
                    mismo_archivo = cap["archivo"] == slide.get("archivo")
                    if not mismo_archivo and (not ocr_txt.strip() or not ocr_previo.strip()):
                        motivo = "ocr_ausente_no_fusiona_por_hash"
                        continue
                    if _ocr_cambio_importante(ocr_previo, ocr_txt):
                        motivo = "ocr_cambio_importante"
                        continue
                    similar_idx = i
                    break
        elif ocr_txt:
            for i in range(len(slides) - 1, -1, -1):
                slide = slides[i]
                previo = str(slide.get("ocr", "") or "")
                similitud = difflib.SequenceMatcher(
                    None, _normalizar_busqueda(previo),
                    _normalizar_busqueda(ocr_txt)).ratio()
                if (slide.get("_hash_int") is None and previo and similitud >= 0.80
                        and not _ocr_cambio_importante(previo, ocr_txt)):
                    similar_idx = i
                    break

        if similar_idx is not None:
            slide = slides[similar_idx]
            aparicion = {
                "archivo": cap["archivo"],
                "t_rel": t_cap,
                "t_abs": _t_abs_desde_rel(t_cap, cap, origen),
            }
            evidencias = slide.setdefault("evidencias_aparicion", [])
            if not any(
                    str(item.get("archivo", "")) == aparicion["archivo"]
                    and math.isclose(_to_float(item.get("t_rel")), t_cap, abs_tol=1e-6)
                    for item in evidencias if isinstance(item, dict)):
                evidencias.append(aparicion)
            gap_identidad = t_cap - _to_float(
                slide.get("t_fin_rel", _cap_t(slide)))
            if gap_identidad > _post_slide_group_max_gap():
                construir_timeline_slides.ultimo_reporte[
                    "identidades_reaparecidas_global"] += 1
                if "identidad_visual_reaparecida" not in slide.get("motivo", ""):
                    slide["motivo"] = (
                        f"{slide.get('motivo', 'slide')}; identidad_visual_reaparecida")
            slide["grupo_total"] = int(slide.get("grupo_total", 1)) + 1
            slide["capturas_colapsadas"] = int(
                slide.get("capturas_colapsadas", 1)) + 1
            if activa_previa != similar_idx:
                slide.setdefault("intervalos_visuales", []).append({
                    "inicio": t_cap, "fin": t_cap,
                })
            if not slide.get("ocr") and ocr_txt:
                slide["ocr"] = ocr_txt
            if _preferir_representante_slide(
                    str(slide.get("ocr", "")), ocr_txt, _cap_t(slide), t_cap):
                inicio_prev = _to_float(slide.get("t_inicio_rel", _cap_t(slide)))
                fin_prev = _to_float(slide.get("t_fin_rel", t_cap))
                slide.update({
                    "archivo": cap["archivo"],
                    "t_rel": t_cap,
                    "t_abs": _t_abs_desde_rel(t_cap, cap, origen),
                    "parte": cap.get("parte", slide.get("parte", "")),
                    "dhash": f"{h:016x}" if h is not None else str(
                        cap.get("dhash", slide.get("dhash", ""))),
                    "_hash_int": h if h is not None else slide.get("_hash_int"),
                    "ocr": ocr_txt or slide.get("ocr", ""),
                    "representante": "captura_mas_completa_o_final",
                    "t_inicio_rel": inicio_prev,
                    "t_fin_rel": max(fin_prev, t_cap),
                })
                if "representante_mas_completo" not in slide.get("motivo", ""):
                    slide["motivo"] = (
                        f"{slide.get('motivo', 'slide')}; representante_mas_completo")
            slide_por_archivo[cap["archivo"]] = similar_idx
            activa_idx = similar_idx
            ultimo_evento_activa = t_cap
            continue

        visual = _analizar_visual_local(d, cap, ocr_txt)
        es_ui = es_captura_interfaz_claseviva(
            ocr_txt, cap["archivo"], contexto=str(cap.get("titulo", "")))
        motivo_slide = motivo
        if es_ui:
            motivo_slide = f"{motivo_slide}; captura_interfaz_claseviva"
        elif visual.get("visual_tipo") and visual.get("visual_tipo") not in {
                "captura_generica", "pantalla_plana_o_idle"}:
            motivo_slide = f"{motivo_slide}; visual={visual.get('visual_tipo')}"
        slides.append({
            "archivo": cap["archivo"],
            "t_rel": t_cap,
            "t_abs": _t_abs_desde_rel(t_cap, cap, origen),
            "parte": cap.get("parte", ""),
            "dhash": f"{h:016x}" if h is not None else str(cap.get("dhash", "")),
            "_hash_int": h,
            "ocr": ocr_txt,
            "t_inicio_rel": t_cap,
            "t_fin_rel": t_cap,
            "intervalos_visuales": [{"inicio": t_cap, "fin": t_cap}],
            "grupo_total": 1,
            "capturas_colapsadas": 1,
            "evidencias_aparicion": [{
                "archivo": cap["archivo"],
                "t_rel": t_cap,
                "t_abs": _t_abs_desde_rel(t_cap, cap, origen),
            }],
            "motivo": motivo_slide,
            "es_interfaz_claseviva": es_ui,
            "no_slide": es_ui,
            **visual,
        })
        activa_idx = len(slides) - 1
        slide_por_archivo[cap["archivo"]] = activa_idx
        ultimo_evento_activa = t_cap

    # En filas legacy no hay heartbeat. El siguiente cambio y el fin del rango son
    # entonces la mejor evidencia disponible del tiempo que permaneció la slide.
    if activa_idx is not None:
        cierre = b_rel if not esquema_nuevo else min(
            b_rel, ultimo_evento_activa + gracia_crash)
        cerrar_activa(cierre)
        if esquema_nuevo and cierre < b_rel:
            slide = slides[activa_idx]
            slide["motivo"] = (
                f"{slide.get('motivo', 'slide')}; cierre_conservador_sin_fin")
            construir_timeline_slides.ultimo_reporte[
                "cierres_conservadores_sin_fin"] += 1

    ui_slides = [s for s in slides if s.get("es_interfaz_claseviva")]
    slides_reales = [s for s in slides if not s.get("es_interfaz_claseviva")]
    transcript_util = len(_texto_segs(segmentos)) >= 80
    if ui_slides and slides_reales:
        filtradas = sum(int(s.get("grupo_total", 1)) for s in ui_slides)
        construir_timeline_slides.ultimo_reporte["capturas_interfaz_filtradas"] = filtradas
        slides = slides_reales
    elif ui_slides and not slides_reales:
        if transcript_util:
            secundarias = sum(int(s.get("grupo_total", 1)) for s in ui_slides)
            construir_timeline_slides.ultimo_reporte[
                "capturas_interfaz_secundarias"] = secundarias
            for slide in slides:
                slide["evidencia_secundaria"] = True
                slide["motivo"] = (
                    f"{slide.get('motivo', 'slide')}; evidencia_secundaria_no_slide")
        else:
            filtradas = sum(int(s.get("grupo_total", 1)) for s in ui_slides)
            construir_timeline_slides.ultimo_reporte["capturas_interfaz_filtradas"] = filtradas
            slides = []

    segs = _segmentos_ordenados(segmentos, origen)
    umbral_estable = _umbral_visual_estable(d)
    for i, slide in enumerate(slides):
        intervalos = slide.get("intervalos_visuales") or []
        if intervalos:
            inicio = min(_to_float(it.get("inicio")) for it in intervalos)
            fin = max(_to_float(it.get("fin"), _to_float(it.get("inicio")))
                      for it in intervalos)
            duracion = sum(max(0.0, _to_float(it.get("fin"))
                               - _to_float(it.get("inicio"))) for it in intervalos)
            slide["t_inicio_rel"] = inicio
            slide["t_fin_rel"] = fin
        else:
            inicio = _to_float(slide.get("t_inicio_rel", _cap_t(slide)))
            fin = max(inicio, _to_float(slide.get("t_fin_rel", _cap_t(slide))))
            duracion = fin - inicio
        slide["duracion_visual_seg"] = round(duracion, 2)
        slide["umbral_visual_estable_seg"] = round(umbral_estable, 2)
        if duracion >= umbral_estable:
            slide["visual_estable"] = True
            if "visual_estable" not in slide.get("motivo", ""):
                slide["motivo"] = f"{slide.get('motivo', 'slide')}; visual_estable"
        cercanos, v_ini, v_fin = _transcript_para_slide(slide, i, slides, segs,
                                                        a_rel, b_rel, origen)
        slide["ventana_ini"] = v_ini
        slide["ventana_fin"] = v_fin
        slide["transcript_ventana"] = "\n".join(_linea_segmento(s, origen)
                                                for s in cercanos)
        slide["sin_transcript_cercano"] = not bool(cercanos)
        if not cercanos:
            slide["motivo"] = f"{slide.get('motivo', 'slide')}; sin_transcript_cercano"
        slide["score"] = _slide_score(slide, cercanos)
    return slides


def asignar_slides_a_chunks(slides: list[dict], chunks: list[dict]) -> list[list[dict]]:
    """Asigna cada slide de una parte a un único chunk por solape temporal."""
    asignadas: list[list[dict]] = [[] for _ in chunks]
    if not chunks:
        return asignadas

    for slide in slides:
        intervalos = slide.get("intervalos_visuales") or [{
            "inicio": _to_float(slide.get("t_inicio_rel", _cap_t(slide))),
            "fin": _to_float(slide.get("t_fin_rel", _cap_t(slide))),
        }]
        solapes: list[float] = []
        distancias: list[float] = []
        centro_slide = sum(
            (_to_float(it.get("inicio")) + _to_float(
                it.get("fin"), _to_float(it.get("inicio")))) / 2.0
            for it in intervalos
        ) / max(1, len(intervalos))
        for chunk in chunks:
            ini = _to_float(chunk.get("t_ini"))
            fin = max(ini, _to_float(chunk.get("t_fin"), ini))
            solape = sum(max(
                0.0,
                min(fin, _to_float(it.get("fin"), _to_float(it.get("inicio"))))
                - max(ini, _to_float(it.get("inicio"))),
            ) for it in intervalos)
            solapes.append(solape)
            distancias.append(0.0 if ini <= centro_slide <= fin else min(
                abs(centro_slide - ini), abs(centro_slide - fin)))
        mejor = min(
            range(len(chunks)),
            key=lambda idx: (-solapes[idx], distancias[idx], idx),
        )
        asignadas[mejor].append(dict(slide, chunk_asignado=mejor + 1))

    for grupo in asignadas:
        grupo.sort(key=lambda s: min(
            (_to_float(it.get("inicio")) for it in s.get("intervalos_visuales", [])),
            default=_cap_t(s),
        ))
    return asignadas


def seleccionar_slides_globales_por_chunks(
        slides: list[dict], chunks: list[dict], *, tema_actual: str = "",
        curso: str = "", parte: int | str = "",
        ) -> tuple[list[list[dict]], list[list[dict]], dict]:
    """Asigna primero y rankea después; cada slide compite exactamente una vez."""
    asignadas = asignar_slides_a_chunks(slides, chunks)
    seleccionadas: list[list[dict]] = []
    reporte = {
        "slides_candidatas": len(slides),
        "slides_descartadas_presupuesto": 0,
        "slides_protegidas_estudio": 0,
        "slides_enviadas": 0,
        "candidatas_por_chunk": [len(grupo) for grupo in asignadas],
        "seleccionadas_por_chunk": [],
    }
    for chunk, candidatas in zip(chunks, asignadas):
        presupuesto = _presupuesto_slides_segmento(candidatas, chunk.get("segs", []))
        elegidas = seleccionar_slides_por_tema(
            candidatas, chunk.get("segs", []), presupuesto=presupuesto,
            tema_actual=tema_actual, curso=curso, parte=parte)
        rep = getattr(seleccionar_slides_por_tema, "ultimo_reporte", {})
        reporte["slides_descartadas_presupuesto"] += int(
            rep.get("slides_descartadas_presupuesto", 0))
        reporte["slides_protegidas_estudio"] += int(
            rep.get("slides_protegidas_estudio", 0))
        reporte["slides_enviadas"] += len(elegidas)
        reporte["seleccionadas_por_chunk"].append(len(elegidas))
        seleccionadas.append(elegidas)
    return asignadas, seleccionadas, reporte


def _slide_batches(slides: list[dict], max_por_lote: int | None = None) -> list[list[dict]]:
    max_por_lote = _post_slides_max() if max_por_lote is None else max(1, int(max_por_lote))
    if not slides:
        return [[]]
    return [slides[i:i + max_por_lote] for i in range(0, len(slides), max_por_lote)]


def fichas_slides(slides: list[dict], origen,
                  transcript_max_chars: int | None = None) -> str:
    fichas_slides.ultimo_reporte = {"ocr_chars_brutos": 0, "ocr_chars_enviados": 0}
    if not slides:
        return "(sin slides/capturas relevantes en este tramo)"
    fichas = []
    for i, slide in enumerate(slides, 1):
        if slide.get("evidencia_secundaria"):
            adv = "CAPTURA DE INTERFAZ / NO-SLIDE; evidencia visual secundaria"
        elif slide.get("sin_transcript_cercano"):
            adv = "EVIDENCIA VISUAL SIN AUDIO/TRANSCRIPCIÓN CERCANA"
        else:
            adv = "transcript cercano disponible"
        ocr_red = reducir_ocr_slide(
            slide, transcript_ventana=str(slide.get("transcript_ventana") or ""))
        fichas_slides.ultimo_reporte["ocr_chars_brutos"] += int(
            ocr_red.get("chars_brutos", 0))
        fichas_slides.ultimo_reporte["ocr_chars_enviados"] += int(
            ocr_red.get("chars_enviados", 0))
        transcript_asociado = str(
            slide.get("transcript_ventana") or "(sin transcript cercano)")
        if transcript_max_chars is not None and len(transcript_asociado) > transcript_max_chars:
            transcript_asociado = transcript_asociado[:transcript_max_chars].rsplit(
                " ", 1)[0].rstrip() + "..."
        material = slide.get("material_oficial") or {}
        procedencia = ""
        if material:
            procedencia = (
                "\nPROCEDENCIA VISUAL: página "
                f"{material.get('pagina')} de {material.get('fuente_nombre')}; "
                f"rol {slide.get('visual_role', 'principal')}; la captura de aparición "
                "se conserva en el manifest visual derivado."
            )
        fichas.append(
            f"SLIDE {i}: {slide['archivo']} (hora {fmt_t(_cap_t(slide), origen)}, "
            f"score {slide.get('score')}, motivo: {slide.get('motivo')})\n"
            f"Estado: {adv}\n"
            f"OCR REDUCIDO:\n{ocr_red.get('texto') or '(sin texto legible)'}\n"
            f"TRANSCRIPT ASOCIADO:\n"
            f"{transcript_asociado}{procedencia}"
        )
    return "\n\n".join(fichas)


def _fallback_slide_guided_md(slides: list[dict], segs_chunk: list[dict], origen,
                              incluir_transcripcion: bool = True) -> str:
    lineas = [
        "## Desarrollo del tramo",
        "",
        "> Reconstrucción basada en el transcript y la evidencia visual conservada.",
        "",
    ]
    if slides:
        for i, slide in enumerate(slides, 1):
            lineas.append(f"### Slide {i}: {slide['archivo']}")
            idea = (
                _caption_visual_determinista(slide, max_chars=900)
                or _ocr_visual_confiable(slide)
                or "Evidencia visual conservada para este tramo"
            ).replace("|", "/").replace("[", "(").replace("]", ")")
            if slide.get("sin_transcript_cercano"):
                idea += " (sin audio cercano verificado)"
            lineas.append(f"[[IMG:{slide['archivo']}|{idea}]]")
            lineas.append("")
    if incluir_transcripcion:
        lineas.append(_transcripcion_cruda_md(
            segs_chunk, origen, "### Transcripción cruda del tramo"))
    return "\n".join(lineas).strip() if len(lineas) > 4 else ""


def _fallback_merge_estudio(guiones: list[str]) -> str:
    """Publica los guiones locales sin envolverlos en metadatos del pipeline."""
    cuerpos: list[str] = []
    for guion in guiones:
        lineas = []
        for linea in str(guion or "").splitlines():
            if re.match(r"^# Chunk\s+", linea):
                continue
            if re.match(r"^> (?:Rango|Slides de esta tanda):", linea):
                continue
            lineas.append(linea)
        cuerpo = "\n".join(lineas).strip()
        if cuerpo:
            cuerpos.append(cuerpo)
    if not cuerpos:
        return ""
    return "## Desarrollo de la clase\n\n" + "\n\n".join(cuerpos)


def _sanear_panorama_parte(markdown: str) -> str:
    """Una parte aporta cuerpo tematico; la portada y el resumen son globales."""
    txt = str(markdown or "").lstrip("\ufeff").strip()
    txt = re.sub(r"(?m)^#[ \t]+[^\n]*\n?", "", txt)
    txt = re.sub(
        r"(?mi)^>[ \t]*(?:Fecha|Idioma|Generado por)[^\n]*\n?", "", txt,
    )
    while True:
        heading = _RE_RESUMEN_EJECUTIVO_HEADING.search(txt)
        if not heading:
            break
        resto = txt[heading.end():]
        fin = re.search(r"(?m)^\s*---\s*$|^##[ \t]+\S", resto)
        limite = heading.end() + (fin.end() if fin else len(resto))
        txt = txt[:heading.start()] + txt[limite:]
    txt = re.sub(r"(?m)^\s*---\s*$", "", txt)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


_EMAIL_PUBLICO_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w-])"
)
_BLOQUE_TRANSCRIPT_LLM_RE = re.compile(
    r"^[ \t]*\*{0,2}(?:transcript|transcripci[oó]n(?:\s+literal)?)"
    r"(?:\s*\([^\n)]*\))?\s*:?\s*\*{0,2}[ \t]*\n"
    r"(?:[ \t]*>[^\n]*(?:\n|$))+",
    re.I | re.M,
)
_IMG_ADMONITION_LLM_RE = re.compile(
    r"(?im)^[ \t]*>\s*\[!IMG:(?P<archivo>[^\]|\r\n]+)"
    r"(?:\|(?P<inline>[^\]\r\n]+))?\]\s*(?: {2})?\r?\n"
    r"(?P<cuerpo>(?:[ \t]*>\s?(?!\[!IMG:)[^\r\n]*(?:\r?\n|$))*)"
)


def _contiene_datos_personales_visuales(texto: str) -> bool:
    """Bloquea listados/rosters; una dirección aislada puede ser un recurso docente."""
    correos = {correo.casefold() for correo in _EMAIL_PUBLICO_RE.findall(texto or "")}
    return len(correos) >= 2


def _redactar_datos_personales_publicos(texto: str) -> str:
    return _EMAIL_PUBLICO_RE.sub("[correo omitido]", str(texto or ""))


def _normalizar_marcadores_imagen_llm(texto: str) -> str:
    """Convierte admonitions inventadas por el LLM al contrato [[IMG:...|...]]."""
    def reemplazar(match: re.Match) -> str:
        archivo = str(match.group("archivo") or "").strip()
        inline = str(match.group("inline") or "").strip()
        cuerpo = " ".join(
            re.sub(r"^[ \t]*>\s?", "", linea).strip()
            for linea in str(match.group("cuerpo") or "").splitlines()
            if re.sub(r"^[ \t]*>\s?", "", linea).strip()
        )
        caption = re.sub(r"\s+", " ", inline or cuerpo).strip()
        caption = caption.replace("|", " — ").replace("]]", "]")
        if not caption:
            caption = "Evidencia visual vinculada con la explicación del tramo."
        return f"[[IMG:{archivo}|{caption}]]\n"

    normalizado = re.sub(
        r"(?m)^([ \t]*\[\[IMG:[^\r\n]+?)(?<!\])\][ \t]*$",
        r"\1]]",
        str(texto or ""),
    )
    return _IMG_ADMONITION_LLM_RE.sub(reemplazar, normalizado)


def _sanear_contenido_llm_publico(texto: str) -> str:
    """Quita transcript duplicado, metadatos de prompt y PII antes de publicar."""
    limpio = _normalizar_marcadores_imagen_llm(texto)
    limpio = _redactar_datos_personales_publicos(limpio)
    limpio = _BLOQUE_TRANSCRIPT_LLM_RE.sub("", limpio)
    limpio = re.sub(
        r"(?im)^>\s*\*{0,2}(?:fuente principal|diapositivas|objetivo del tramo)"
        r"\*{0,2}\s*:.*(?:\n|$)",
        "",
        limpio,
    )
    limpio = re.sub(
        r"(?im)^\s*\*?\(sin diapositiva(?:[^)]*)\)\*?\s*(?:\n|$)",
        "",
        limpio,
    )
    limpio = re.sub(
        r"(?im)^\s*#{2,4}\s+(\[\[IMG:[^\n]+\]\])\s*$",
        r"\1",
        limpio,
    )
    limpio = re.sub(
        r"(?im)^\s*(?:[-*]\s*)?\*{0,2}OCR\s+(?:de\s+)?(?:la\s+)?"
        r"(?:diapositiva|captura|imagen)(?:\s+legible)?\*{0,2}\s*:.*(?:\n|$)",
        "",
        limpio,
    )
    limpio = re.sub(
        r"(?im)^\s*(?:[-*]\s*)?\*{0,2}(?:lo que dijo (?:el|la) (?:profesor|docente)|"
        r"transcripci[oó]n asociada|transcript asociado)"
        r"(?:\s*\([^\n)]*\))?\*{0,2}\s*:\s*",
        "",
        limpio,
    )
    limpio = re.sub(
        r"(?im)^\s*\*{0,2}qu[eé]\s+muestra(?:n)?\s+(?:la|las)\s+"
        r"(?:diapositiva|diapositivas|captura|capturas|imagen|imágenes)"
        r"\*{0,2}\s*:\s*",
        "**🔎 Lectura visual:** ",
        limpio,
    )
    limpio = re.sub(
        r"(?im)^\s*\*{0,2}qu[eé]\s+dice(?:n)?\s+(?:el|la|los|las)\s+"
        r"(?:profesor|profesora|docente|profesores|profesoras|docentes)"
        r"(?:\s*\([^\n)]*\))?\*{0,2}\s*:\s*",
        "**🧠 Explicación integrada:** ",
        limpio,
    )
    limpio = re.sub(
        r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}\s*[–—-]\s*"
        r"(?:\d{1,2}:)?\d{1,2}:\d{2}\]\s*",
        "",
        limpio,
    )
    limpio = re.sub(
        r"(?im)^\s*#{2,4}\s+Continuaci[oó]n\s+sin\s+diapositivas[^\n]*(?:\n|$)",
        "",
        limpio,
    )
    limpio = re.sub(
        r"(?ims)^\s*#{2,4}\s+Resumen\s+del\s+rango\s*\n.*?"
        r"(?=^#{1,4}\s|\Z)",
        "",
        limpio,
    )
    # En imágenes uint8, torchvision.transforms.ToTensor convierte [0,255]
    # a [0,1]. Corrige únicamente un comentario de código autocontradictorio.
    limpio = re.sub(
        r"(?m)(transforms\.ToTensor\(\)[^\n#]*#[^\n]*"
        r"\[0\s*,\s*)0(\])",
        r"\g<1>1\2",
        limpio,
    )
    limpio = re.sub(r"[ \t]+\n", "\n", limpio)
    return re.sub(r"\n{3,}", "\n\n", limpio).strip()


def _guion_sin_metadatos(guion: str) -> tuple[str, str, str]:
    """Devuelve clave, rango y cuerpo publicable de un guion de chunk."""
    lineas = str(guion or "").splitlines()
    cabecera = next((linea for linea in lineas if re.match(r"^# Chunk\s+", linea)), "")
    match = re.match(r"^# Chunk\s+(\d+\.\d{3})(?:_\d+)?", cabecera)
    clave = match.group(1) if match else f"sin-clave-{hashlib.sha256(str(guion).encode()).hexdigest()[:8]}"
    rango = ""
    cuerpo: list[str] = []
    for linea in lineas:
        if re.match(r"^# Chunk\s+", linea):
            continue
        if re.match(r"^> Rango:", linea):
            rango = re.sub(r"^> Rango:\s*", "", linea).replace("->", "–")
            continue
        if re.match(r"^> Slides de esta tanda:", linea):
            continue
        cuerpo.append(linea)
    return clave, rango.strip(), _sanear_contenido_llm_publico("\n".join(cuerpo))


def _guiones_textuales_para_merge(guiones: list[str]) -> str:
    """Conserva el aporte semántico de las imágenes sin enviar sus rutas al merge."""
    piezas: list[str] = []
    for guion in guiones:
        clave, rango, cuerpo = _guion_sin_metadatos(guion)
        salida: list[str] = []
        for linea in cuerpo.splitlines():
            if '<img src="img/' in linea:
                while salida and salida[-1].lstrip().startswith("|"):
                    salida.pop()
                celdas = [celda.strip() for celda in linea.strip().strip("|").split("|")]
                explicacion = celdas[-1] if len(celdas) >= 2 else ""
                explicacion = re.sub(r"<[^>]+>", " ", explicacion)
                explicacion = re.sub(r"\s+", " ", explicacion).strip()
                if explicacion:
                    salida.append(f"- Evidencia visual descrita: {explicacion}")
                continue
            if linea.lstrip().startswith("|") and (
                    "Evidencia visual" in linea or re.match(r"^\|\s*:?-+", linea)):
                continue
            salida.append(linea)
        texto = "\n".join(salida).strip()
        if texto:
            piezas.append(
                f"### Tramo {clave}{f' · {rango}' if rango else ''}\n\n{texto}")
    return "\n\n---\n\n".join(piezas)


def _subir_nivel_encabezados(markdown: str, niveles: int = 2) -> str:
    def repl(match):
        nivel = min(6, len(match.group(1)) + niveles)
        return "#" * nivel + " " + match.group(2)
    return re.sub(r"(?m)^(#{1,5})\s+(.+)$", repl, str(markdown or ""))


def _desarrollo_detallado_guiones_md(
        guiones: list[str], fuentes_por_chunk: dict[str, str] | None = None) -> str:
    """Publica los chunks disjuntos como unidades extensas sin metadatos internos."""
    grupos: dict[str, dict] = {}
    for guion in guiones:
        clave, rango, cuerpo = _guion_sin_metadatos(guion)
        grupo = grupos.setdefault(clave, {"rango": rango, "cuerpos": []})
        if rango and not grupo["rango"]:
            grupo["rango"] = rango
        if cuerpo:
            grupo["cuerpos"].append(cuerpo)
    if not grupos:
        return ""
    lineas = [
        "## 📚 Desarrollo exhaustivo por tramos", "",
        "> Esta capa conserva el detalle único de cada tramo. El panorama anterior organiza "
        "los conceptos; aquí se recuperan ejemplos, razonamientos, pasos y evidencias sin "
        "reducirlos a un resumen.", "",
    ]
    for numero, (clave, grupo) in enumerate(grupos.items(), 1):
        titulo_rango = f" · {grupo['rango']}" if grupo.get("rango") else ""
        lineas.extend([f"### 🧩 Tramo {numero}{titulo_rango}", ""])
        cuerpo = "\n\n".join(grupo["cuerpos"])
        cuerpo = re.sub(
            r"(?ms)^### 🖼️ Evidencia visual integrada\s*\n\s*> Estas evidencias "
            r"complementan.*?procedencia\.\s*\n", "", cuerpo)
        lineas.extend([_subir_nivel_encabezados(cuerpo, 2), ""])
        fuente = str((fuentes_por_chunk or {}).get(clave, "") or "").strip()
        if fuente:
            fuente = re.sub(r"^#### Fuentes ampliadas\s*", "", fuente).strip()
            lineas.extend([
                "#### 🌐 Contraste y ampliación externa del tramo", "",
                "> Estas fuentes no forman parte de la voz del profesor. Sirven para "
                "actualizar, comprobar o ampliar el contenido del tramo.", "",
                fuente, "",
            ])
    texto, _ = deduplicar_referencias_img("\n".join(lineas).strip())
    return texto


_QUERY_STOPWORDS = {
    "ahora", "aqui", "clase", "como", "con", "cuando", "donde", "esta", "este",
    "esto", "para", "pero", "porque", "profesor", "profesora", "parte", "puede",
    "sobre", "tambien", "tiene", "vamos", "entonces", "cada", "desde", "ellos", "ella",
    "that", "this", "with", "from", "have", "will", "your", "about", "into", "there",
    "what", "when", "where", "which", "would", "teacher", "class", "course", "part",
}


_WEB_QUERY_CONCEPTS = (
    ("NVIDIA DLI", 30, ("nvidia", "deep learning institute", " dli ")),
    ("Google Colab", 28, ("google colab", " colab ")),
    ("Jupyter Notebook", 24, ("jupyter notebook", " ipynb ")),
    ("IBM Deep Blue", 30, ("deep blue",)),
    ("Samy Bengio", 30, ("samy bengio", "sami bengio")),
    ("McCulloch Pitts neuron", 30, ("mcculloch", "macculloch", "pitts")),
    ("Frank Rosenblatt perceptron", 30, ("rosenblatt",)),
    ("Minsky Papert Perceptrons", 30, ("minsky", "papert")),
    ("NeurIPS", 30, ("neurips", "nips conference")),
    ("MNIST", 30, ("mnist",)),
    ("PyTorch", 28, ("pytorch", "torch")),
    ("backpropagation", 24, ("backpropagation", "retropropagacion")),
    ("gradient descent", 22, ("gradient descent", "descenso por gradiente")),
    ("loss function", 20, ("loss function", "funcion de perdida", "mse", "mae")),
    ("supervised learning", 18, ("supervised learning", "aprendizaje supervisado")),
    ("expert systems", 20, ("expert systems", "sistemas expertos")),
    ("Big Data", 18, ("big data",)),
    ("XOR perceptron", 24, (" xor ", "compuerta xor")),
    ("convolutional neural network", 20, ("convolutional", "convolucional")),
    ("GPU computing", 18, (" gpu ", "tarjeta grafica")),
    ("K nearest neighbors", 18, ("nearest neighbors", "knn", "vecinos cercanos")),
    ("neural network", 12, ("neural network", "red neuronal", "redes neuronales")),
    ("machine learning", 10, ("machine learning", "aprendizaje automatico")),
    ("deep learning", 12, ("deep learning", "aprendizaje profundo")),
    ("artificial intelligence", 6, ("artificial intelligence", "inteligencia artificial")),
)

_WEB_QUERY_PRIMARY_SITES = {
    "nvidia dli": "nvidia.com",
    "google colab": "research.google.com",
    "ibm deep blue": "ibm.com",
    "frank rosenblatt perceptron": "cornell.edu",
    "neurips": "neurips.cc",
    "mnist": "nist.gov",
    "pytorch": "pytorch.org",
}


def _consulta_web_fallback(texto: str, tema: str = "") -> str:
    """Deriva una consulta técnica estable; evita palabras frecuentes de conversación."""
    normal = f" {_normalizar_busqueda(f'{tema} {texto}')} "
    candidatos: list[tuple[int, int, str]] = []
    for canonico, peso, variantes in _WEB_QUERY_CONCEPTS:
        apariciones = sum(normal.count(variante) for variante in variantes)
        if not apariciones:
            continue
        primera = min(
            (normal.find(variante) for variante in variantes if variante in normal),
            default=len(normal),
        )
        candidatos.append((peso + min(20, apariciones * 3), -primera, canonico))
    candidatos.sort(reverse=True)
    conceptos: list[str] = []
    vistos: set[str] = set()
    for _score, _pos, concepto in candidatos:
        key = concepto.casefold()
        if key in vistos:
            continue
        vistos.add(key)
        conceptos.append(concepto)
        if len(conceptos) >= 2:
            break
    if not conceptos:
        conceptos = ["artificial intelligence", "deep learning"]
    sitio = _WEB_QUERY_PRIMARY_SITES.get(
        _normalizar_busqueda(conceptos[0]), "")
    sufijo = f" site:{sitio}" if sitio else " official documentation primary source"
    return (" ".join(conceptos) + sufijo)[:160]


_ICONOS_ENCABEZADO = (
    (("resumen", "panorama", "introduccion"), "🧭"),
    (("historia", "evolucion", "cronologia"), "🕰️"),
    (("demostracion", "video", "experimento"), "🎬"),
    (("busqueda", "navegacion", "referencia web", "recursos consultados"), "🔍"),
    (("laboratorio", "codigo", "pytorch", "practica"), "💻"),
    (("dataset", "datos", "muestra", "train", "test"), "🗂️"),
    (("comparacion", "diferencia", "frente a", "versus"), "⚖️"),
    (("proceso", "flujo", "entrenamiento", "pipeline"), "🔁"),
    (("ejemplo", "caso", "aplicacion"), "🧪"),
    (("formula", "glosario", "matemat"), "🧮"),
    (("guia", "estudio", "examen", "repaso"), "🎯"),
    (("fuente", "contraste", "actualizacion", "externa"), "🌐"),
    (("galeria", "visual", "evidencia"), "🖼️"),
    (("duda", "limitacion", "cobertura"), "🔎"),
    (("curso", "logistica", "presentacion"), "🎓"),
    (("inteligencia", "neur", "deep learning", "machine learning"), "🧠"),
)


def _adornar_encabezados_pedagogicos(markdown: str) -> str:
    """Añade señales visuales sobrias sin alterar títulos ya iconizados."""
    def repl(match):
        hashes, titulo = match.group(1), match.group(2).strip()
        if re.match(r"^[^\w\s#]{1,3}\s", titulo):
            return match.group(0)
        normal = _normalizar_busqueda(titulo)
        for palabras, icono in _ICONOS_ENCABEZADO:
            if any(palabra in normal for palabra in palabras):
                return f"{hashes} {icono} {titulo}"
        return match.group(0)
    return re.sub(r"(?m)^(#{2,4})\s+(.+)$", repl, str(markdown or ""))


def _bloques_img_omitidas(merge_input: str, faltantes: set[str]) -> list[dict]:
    lineas = merge_input.splitlines()
    omitidas: list[dict] = []
    vistos: set[str] = set()
    posiciones = []
    for i, linea in enumerate(lineas):
        for ref in faltantes:
            if ref not in vistos and f"img/{ref}" in linea:
                posiciones.append((i, ref, linea))
                vistos.add(ref)
    for i, ref, linea in sorted(posiciones):
        heading_idx = next(
            (idx for idx in range(i, -1, -1)
             if re.match(r"^#{2,3}\s+", lineas[idx])),
            max(0, i - 3),
        )
        tema = re.sub(r"^#{2,3}\s+", "", lineas[heading_idx]).strip()
        ini = max(heading_idx, i - 5)
        fin = min(len(lineas), i + 5)
        bloque = "\n".join(x for x in lineas[ini:fin] if x.strip())
        if not bloque:
            continue
        bloque_sin_rutas = RE_IMG_REF.sub("", bloque)
        omitidas.append({
            "archivo": ref,
            "bloque": bloque,
            "tema": tema,
            "caption": _caption_de_linea_img(linea),
            "tiene_transcript_asociado": bool(re.search(
                r"transcrip|transcript|explicaci", bloque_sin_rutas, re.I)),
            "es_interfaz": es_captura_interfaz_claseviva(linea, ref),
            "motivo": "omitida_por_merge",
        })
    return omitidas


def _debug_slides_omitidas_path(d: Path) -> Path:
    debug = d / "_debug"
    debug.mkdir(parents=True, exist_ok=True)
    return debug / "slides_omitidas_por_merge.md"


def _debug_slides_omitidas_header() -> str:
    return (
        "# Slides omitidas por merge\n\n"
        "> Estas slides aparecieron en guiones de chunks, pero el merge no las "
        "integro inicialmente. Las valiosas se reubican cerca de su explicacion; "
        "la interfaz y el material sin valor quedan solo aqui para auditoria.\n\n"
    )


def _registrar_slides_omitidas_por_merge(d: Path, parte: int, merge_input: str,
                                         faltantes: set[str]) -> dict:
    omitidas = _bloques_img_omitidas(merge_input, faltantes)
    if not omitidas:
        return {
            "slides_omitidas_por_merge": 0,
            "slides_omitidas_debug": 0,
        }
    destino = _debug_slides_omitidas_path(d)
    if not destino.is_file():
        destino.write_text(_debug_slides_omitidas_header(), encoding="utf-8")
    with open(destino, "a", encoding="utf-8") as f:
        f.write(f"\n## Parte {parte}: {len(omitidas)} slide(s) omitida(s)\n\n")
        for item in omitidas:
            f.write(f"### {item['archivo']}\n\n")
            f.write(f"- Motivo: {item['motivo']}.\n")
            f.write(f"- Caption detectado: {item.get('caption') or '(sin caption)'}.\n")
            f.write("- Transcript/contexto asociado detectado: "
                    f"{'sí' if item.get('tiene_transcript_asociado') else 'no'}.\n")
            f.write("- Captura UI/no-slide: "
                    f"{'sí' if item.get('es_interfaz') else 'no'}.\n\n")
            f.write("```markdown\n")
            f.write(str(item.get("bloque", "")).strip() + "\n")
            f.write("```\n\n")
    return {
        "slides_omitidas_por_merge": len(omitidas),
        "slides_omitidas_debug": len(omitidas),
    }


def _limpiar_contexto_slide_omitida(txt: str, max_chars: int = 520) -> str:
    txt = RE_HTML_IMG.sub("", RE_MD_IMG.sub("", str(txt or "")))
    txt = RE_IMG_REF.sub("", txt)
    txt = txt.replace("|---|---|", " ")
    txt = re.sub(r"[|`]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" -")
    if len(txt) > max_chars:
        txt = txt[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."
    return txt


def _sanear_metadatos_visuales_publicos(markdown: str) -> str:
    """Oculta metadatos internos sin romper referencias HTML a las capturas."""
    texto = _sanear_contenido_llm_publico(markdown)
    texto = re.sub(
        r"(?i)\b(?:el|la)\s+(?:sistema\s+de\s+)?OCR\s+"
        r"(?:captur[oó]|recuper[oó]|detect[oó]|ley[oó])\b"
        r"[^.!?\n]*(?:[.!?]+|$)\s*",
        "",
        texto,
    )
    texto = re.sub(
        r"(?im)^(#{2,6}\s+)(?:slide|slides|diapositiva|diapositivas|"
        r"captura|capturas)\s+\d+(?:\s*,\s*\d+)*"
        r"(?:\s+(?:y|and)\s+\d+)?\s*[:\-–—]\s*",
        r"\1", texto,
    )
    texto = re.sub(
        r"(?i)(?:\*{1,2})?\b(?:SLIDE|SLIDES|DIAPOSITIVA|DIAPOSITIVAS)"
        r"\s+\d+(?:\s*,\s*\d+)*(?:\s+(?:Y|AND)\s+\d+)?"
        r"(?:\s*[–—-]\s*\d+)?"
        r"(?:\*{1,2})?\s*[:\-–—]?\s*",
        "", texto,
    )
    # El lookbehind conserva el único lugar donde el nombre es funcional:
    # ``src=\"img/cap_...jpg\"`` o ``[[IMG:cap_...jpg|...]]``. En captions y
    # párrafos no aporta al estudio.
    texto = re.sub(
        r"(?<!img/)(?<!IMG:)\b(?:cap|region)_\d+"
        r"(?:_[\w-]+)*\.(?:jpe?g|png)\b",
        "", texto, flags=re.I,
    )
    texto = re.sub(
        r"(?i)\(\s*hora\s+[^)]*?(?:score|motivo)[^)]*\)", "", texto)
    texto = re.sub(
        r"(?i)\*{0,2}Relaci[oó]n con la explicaci[oó]n"
        r"(?:\s*\(parte\s+\d+\))?\s*:\*{0,2}",
        "**🧠 Conexión conceptual:**", texto,
    )
    texto = re.sub(
        r"(?i)\*{0,2}Transcripci[oó]n (?:com[uú]n )?asociada"
        r"(?:\s+a las slides?\s*\d+(?:\s*[–—-]\s*\d+)?)?\s*:\*{0,2}",
        "**Explicación del profesor:**", texto,
    )
    texto = re.sub(
        r"(?i)\*{0,2}OCR\s*:\*{0,2}",
        "**La diapositiva muestra:**", texto,
    )
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto


def _limpiar_texto_visual_publico(texto: str, max_chars: int = 520) -> str:
    """Normaliza una explicación visual sin publicar rutas, metadatos ni OCR encadenado."""
    texto = str(texto or "")
    texto = RE_HTML_IMG.sub("", RE_MD_IMG.sub("", texto))
    texto = RE_IMG_REF.sub("", texto)
    texto = re.sub(r"(?m)^#{1,6}\s+", "", texto)
    texto = re.sub(r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]", " ", texto)
    texto = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*·\s*Parte\s+\d+\b", " ", texto,
                   flags=re.I)
    texto = re.sub(
        r"(?i)\b(?:contexto registrado del profesor|ocr de apoyo|captura contexto local|"
        r"transcripci[oó]n asociada|relaci[oó]n con la explicaci[oó]n)\s*:\s*",
        " ", texto,
    )
    texto = re.sub(
        r"(?<!img/)(?<!IMG:)\b(?:cap|region)_\d+"
        r"(?:_[\w-]+)*\.(?:jpe?g|png)\b",
        " ", texto, flags=re.I,
    )
    # Conserva guiones bajos: pueden ser identificadores reales como learning_rate.
    texto = re.sub(r"[|`*]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip(" -:")
    if len(texto) > max_chars:
        texto = texto[:max_chars].rsplit(" ", 1)[0].rstrip() + "…"
    return texto


def _texto_visual_publicable(texto: str, *, min_tokens: int = 3) -> bool:
    """Gate conservador: evita que una cadena larga pero corrupta parezca una leyenda."""
    texto = _limpiar_texto_visual_publico(texto, 700)
    if (not texto or re.search(r"\b0{4,}\b", texto)
            or any(mark in texto for mark in ("�", "Ã", "Â", "ðŸ"))):
        return False
    tokens = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9_.+-]*", texto)
    if len(tokens) < min_tokens:
        return False
    letras = sum(ch.isalpha() for ch in texto)
    visibles = sum(not ch.isspace() for ch in texto)
    if visibles and letras / visibles < 0.48:
        return False
    raros = [
        token for token in tokens
        if len(token) >= 5 and any(ch.isdigit() for ch in token[1:-1])
    ]
    return len(raros) <= max(1, len(tokens) // 12)


def _ocr_con_casing_corrupto(texto: str) -> bool:
    """Detecta mezclas internas de mayúsculas típicas de OCR roto, no títulos normales."""
    tokens = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9_.+-]*", str(texto or ""))
    return any(
        len(token) >= 5
        and token[:1].islower()
        and any(char.isupper() for char in token[1:])
        for token in tokens
    )


_CROMO_APLICACION_VISUAL = (
    "archivo", "editar", "insertar", "herramientas", "ayuda", "comandos",
    "entorno de ejecucion", "ejecutar todas", "copiar en drive", "compartir",
    "conectar",
)

_INICIOS_FRAGMENTO_ORAL = (
    "aqui", "alli", "ahora", "bueno", "como les", "de explicar", "entonces",
    "eso", "esto", "lo voy", "nos", "si no hay mas preguntas", "ustedes",
    "vamos", "y ",
)

_SENALES_LOGISTICA_ORAL = (
    "no me escuchan", "me escuchan", "microfono", "programa de micro",
    "mas preguntas", "nos vemos", "link", "enlace", "p. m.", "pm",
    "hora de retorno", "reunion",
)


def _ocr_dominado_por_interfaz(texto: str) -> bool:
    """Detecta menús y controles de una aplicación dominando el texto recuperado."""
    normal = _normalizar_busqueda(texto)
    hits = sum(
        bool(re.search(rf"(?<!\w){re.escape(termino)}(?!\w)", normal))
        for termino in _CROMO_APLICACION_VISUAL
    )
    return hits >= 4


def _texto_visual_explicativo(
        texto: str, *, min_tokens: int = 3, caption_confiable: bool = False
) -> bool:
    """Exige una explicación autosuficiente, no una interfaz o frase oral aislada."""
    limpio = _limpiar_texto_visual_publico(texto, 700)
    if _ocr_dominado_por_interfaz(limpio):
        return False
    if not _texto_visual_publicable(limpio, min_tokens=min_tokens):
        return bool(
            _contiene_codigo_formula(limpio)
            and re.search(r"(?:==|<=|>=|=|→|->|\[[^\]]+\]|\([^)]*\))", limpio)
        )
    if caption_confiable:
        return True
    normal = _normalizar_busqueda(limpio).lstrip("¿¡\"'([{ ")
    senales_logistica = sum(
        termino in normal for termino in _SENALES_LOGISTICA_ORAL)
    tiene_contenido = (
        _contiene_codigo_formula(limpio)
        or any(_normalizar_busqueda(clave) in normal
               for clave in CLAVES_CONCEPTUALES)
    )
    if senales_logistica >= 2 and not tiene_contenido:
        return False
    return not any(
        normal == inicio.rstrip() or normal.startswith(inicio)
        for inicio in _INICIOS_FRAGMENTO_ORAL
    )


def _extracto_transcript_visual(slide: dict, max_chars: int = 520) -> str:
    texto = str(slide.get("transcript_ventana") or "")
    texto = re.sub(r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]", ". ", texto)
    texto = _limpiar_texto_visual_publico(texto, max_chars * 2)
    if not _texto_visual_publicable(texto, min_tokens=5):
        return ""
    frases = []
    for frase in re.split(r"(?<=[.!?])\s+", texto):
        frase = re.sub(r"\s+([,.;:!?])", r"\1", frase).strip(" -")
        normal = _normalizar_busqueda(frase).rstrip(".?!")
        tokens = re.findall(r"[A-Za-zÀ-ÿ]+", frase)
        inicio = frase.lstrip("¿¡\"'“”‘’([{ ")
        inicia_completa = bool(
            inicio and (inicio[0].isupper() or inicio[0].isdigit()))
        puntuacion_valida = bool(re.search(r"[.!?…][\"'”’)\]}]*$", frase))
        puntuacion_rota = bool(re.search(r"[,.;:!?]\s*[,.;:!?]", frase))
        termina_incompleta = bool(re.search(
            r"\b(?:a|al|con|de|del|en|hacia|para|por|que|sin|sobre|y|o|acerca de)$",
            normal))
        if (
            len(frase) >= 28
            and len(tokens) >= 6
            and inicia_completa
            and puntuacion_valida
            and not puntuacion_rota
            and not termina_incompleta
            and _texto_visual_explicativo(frase, min_tokens=6)
        ):
            frases.append(frase)
    salida = " ".join(frases[:3])
    if not salida:
        return ""
    if len(salida) > max_chars:
        salida = salida[:max_chars].rsplit(" ", 1)[0].rstrip() + "…"
    return salida


def _ocr_visual_confiable(slide: dict, max_chars: int = 260) -> str:
    material = slide.get("material_oficial") or {}
    if _contiene_datos_personales_visuales(
            str(slide.get("ocr_principal") or slide.get("ocr") or "")):
        return ""
    if slide.get("browsing_reference") or slide.get("media_reference"):
        # Navegadores y reproductores tienen extractores propios; publicar su OCR
        # completo volvería a introducir anuncios, recomendaciones y controles.
        return ""
    es_tecnico = False
    if material:
        candidato = str(material.get("texto") or "")
    else:
        candidato = str(slide.get("ocr_principal") or slide.get("ocr") or "")
        if _ocr_dominado_por_interfaz(candidato):
            return ""
        # El OCR narrativo no se publica en bruto. Fuera del material oficial solo se
        # rescatan fórmulas, código, tablas o tareas inequívocas.
        normal = _normalizar_busqueda(candidato)
        es_tarea = _es_tarea_recurso_fuerte(normal)
        raw_atoms = visual_materials._technical_atoms(candidato)
        transcript_tokens = _tokens_relevantes(
            str(slide.get("transcript_ventana", "") or ""))
        verified_atoms = []
        for atom in raw_atoms:
            atom_tokens = _tokens_relevantes(atom)
            if (
                re.search(r"\d", atom)
                or not atom_tokens
                or len(atom_tokens & transcript_tokens) >= min(2, len(atom_tokens))
            ):
                verified_atoms.append(atom)
        strong_code = bool(re.search(
            r"\b(?:import|from\s+\w+\s+import|def|class|return)\b|"
            r"\b[a-z_][a-z0-9_]*_[a-z0-9_]*\s*(?:==|<=|>=|=|->)|"
            r"\[(?:\s*[-+]?\d+(?:[.,]\d+)?\s*,){1,}"
            r"\s*[-+]?\d+(?:[.,]\d+)?\s*\]",
            candidato,
            flags=re.I,
        ))
        contiene_sintaxis = _contiene_codigo_formula(candidato)
        es_tecnico = bool(contiene_sintaxis and (verified_atoms or strong_code))
        sintaxis_no_verificada = bool(contiene_sintaxis and not es_tecnico)
        rol = str(slide.get("visual_role", "") or "")
        visual_tipo = str(slide.get("visual_tipo", "") or "")
        corroborada = visual_materials._ocr_corroborated_by_transcript(slide)
        ocr_tokens = _tokens_relevantes(candidato)
        transcript_tokens = _tokens_relevantes(
            str(slide.get("transcript_ventana", "") or ""))
        coincidencias_semanticas = sum(
            1 for token in ocr_tokens
            if any(
                token == other
                or (
                    len(token) >= 6
                    and len(other) >= 6
                    and token[:6] == other[:6]
                )
                for other in transcript_tokens
            )
        )
        corroborada = corroborada or coincidencias_semanticas >= 2
        claves_conceptuales = sum(
            1 for clave in CLAVES_CONCEPTUALES
            if _normalizar_busqueda(clave) in normal
        )
        narrativo_seguro = (
            rol in {"principal", "secuencia"}
            and visual_tipo in {
                "slide_textual", "diagrama_o_grafico_probable",
                "codigo_formula_tabla",
            }
            and float(slide.get("visual_score", 0) or 0) >= 6
            and not _texto_ruido_visual(candidato)
            and not _ocr_con_casing_corrupto(candidato)
            and not sintaxis_no_verificada
            and (
                corroborada
                or (
                    bool(slide.get("visual_estable"))
                    and claves_conceptuales >= 2
                )
            )
        )
        if not (es_tecnico or es_tarea or narrativo_seguro):
            return ""
    candidato = _limpiar_texto_visual_publico(candidato, max_chars)
    min_tokens = 2 if (material or es_tecnico) else 5
    if _texto_visual_publicable(candidato, min_tokens=min_tokens):
        return candidato
    if es_tecnico and re.search(r"(?:==|<=|>=|=|\[[^\]]+\]|\([^)]*\))", candidato):
        return candidato
    return ""


def _titulo_visual(slide: dict, max_chars: int = 150) -> str:
    material = slide.get("material_oficial") or {}
    text = _ocr_visual_confiable(slide, max_chars=max_chars)
    if not text:
        text = _extracto_transcript_visual(slide, max_chars=max_chars)
    if material:
        page = material.get("pagina", "?")
        return f"Página {page}: {text or 'diapositiva oficial'}"
    return text


def _consulta_web_publica(slide: dict, max_chars: int = 180) -> str:
    """Limpia la consulta y corrige OCR solo cuando el audio aporta una forma verificable."""
    query = str(
        slide.get("browsing_query")
        or slide.get("browsing_parent_query")
        or ""
    ).strip()
    if not query:
        query = visual_materials._extract_browsing_query(
            visual_materials._browsing_evidence_text(slide))
    query = _limpiar_texto_visual_publico(query, max_chars)
    if not query or not _texto_visual_publicable(query, min_tokens=1):
        return ""

    transcript_words = re.findall(
        r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9_.+-]*",
        str(slide.get("transcript_ventana", "") or ""),
    )
    transcript_by_normal: dict[str, str] = {}
    for word in transcript_words:
        normalized = _normalizar_busqueda(word)
        if len(normalized) >= 5 and normalized not in STOPWORDS_TEMA:
            transcript_by_normal.setdefault(normalized, word)
    proper_name_normals = {
        _normalizar_busqueda(match.group(1))
        for match in re.finditer(
            r"\b(?i:apellido|autor(?:a)?|cient[ií]fic[oa]|doctor(?:a)?|"
            r"investigador(?:a)?|nombre|profesor(?:a)?)\s+(?:de\s+)?"
            r"([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ.-]{4,})",
            str(slide.get("transcript_ventana", "") or ""),
        )
    }

    corrected: list[str] = []
    for word in re.findall(
            r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9_.+-]*", query):
        normalized = _normalizar_busqueda(word)
        replacement = word
        if len(normalized) >= 6:
            best_ratio = 0.0
            best_word = ""
            for candidate_normal, candidate_word in transcript_by_normal.items():
                if candidate_normal == normalized:
                    continue
                if (
                    abs(len(candidate_normal) - len(normalized)) > 1
                    or candidate_normal[:1] != normalized[:1]
                    or candidate_normal[-1:] != normalized[-1:]
                ):
                    continue
                ratio = difflib.SequenceMatcher(
                    None, normalized, candidate_normal).ratio()
                if ratio > best_ratio:
                    best_ratio, best_word = ratio, candidate_word
            best_normal = _normalizar_busqueda(best_word)
            threshold = 0.84 if best_normal in proper_name_normals else 0.90
            if best_ratio >= threshold:
                replacement = best_word
        corrected.append(replacement)

    if corrected and _normalizar_busqueda(corrected[0]) == "definicion":
        if len(corrected) >= 2 and _normalizar_busqueda(corrected[1]) not in {
                "de", "del"}:
            corrected.insert(1, "de")
        corrected[0] = "definición"
    cleaned = _limpiar_texto_visual_publico(" ".join(corrected), max_chars)
    return cleaned if _texto_visual_publicable(cleaned, min_tokens=1) else ""


def _tema_referencia_web(slide: dict, max_tokens: int = 10) -> str:
    query = _consulta_web_publica(slide)
    if query:
        return query
    raw = visual_materials._browsing_evidence_text(slide)
    transcript_normal = _normalizar_busqueda(
        str(slide.get("transcript_ventana", "") or ""))
    raw_tokens = re.findall(
        r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9_.+-]*", raw)
    upper = min(max_tokens, 6, len(raw_tokens))
    for size in range(upper, 1, -1):
        for start in range(0, len(raw_tokens) - size + 1):
            original = raw_tokens[start:start + size]
            normalized = [_normalizar_busqueda(token) for token in original]
            if any(
                    not token
                    or token in visual_materials._BROWSING_TOPIC_STOP
                    for token in normalized):
                continue
            phrase = " ".join(normalized)
            if not re.search(
                    rf"(?<!\w){re.escape(phrase)}(?!\w)", transcript_normal):
                continue
            topic = _limpiar_texto_visual_publico(" ".join(original), 180)
            if _texto_visual_publicable(topic, min_tokens=2):
                return topic
    return ""


def _caption_entorno_practico(slide: dict) -> str:
    """Describe estados de notebook por contrato semántico, no por OCR literal."""
    if (
        slide.get("visual_role") not in {"principal", "secuencia", "galeria"}
        or str(slide.get("visual_tipo", "") or "") != "codigo_formula_tabla"
    ):
        return ""
    normal = _normalizar_busqueda(
        f"{slide.get('ocr_principal', '')} {slide.get('ocr', '')} "
        f"{slide.get('transcript_ventana', '')}")
    technical_normal = re.sub(r"\s*_\s*", "_", normal)
    if (
        any(term in technical_normal for term in (
            "x_train", "x valid", "x_valid", "train set", "valid set",
            "training set", "validation set", "conjunto de entrenamiento",
            "conjunto de validacion",
        ))
        and any(term in technical_normal for term in (
            "evalu", "valid", "train", "entrenamiento"))
    ):
        return (
            "La captura conserva el paso práctico donde se separan los datos usados "
            "para ajustar el modelo de los reservados para validarlo. El entorno visual "
            "permite localizar las particiones de entrenamiento y validación y comprobar "
            "que no se evalúa el modelo con los mismos ejemplos que ya vio."
        )
    if (
        any(term in technical_normal for term in (
            "dataset", "train_set", "valid_set", "torchvision", "dataloader",
            "mnist"))
        and any(term in technical_normal for term in (
            "download", "descarg", "cargar", "load", "root", "library",
            "libreria", "torch"))
    ):
        return (
            "La captura documenta la carga del conjunto de datos desde la biblioteca "
            "utilizada en el laboratorio. Deben observarse la fuente del dataset, las "
            "opciones de descarga o partición y los objetos que después alimentarán el "
            "entrenamiento y la validación."
        )
    if (
        any(term in normal for term in (
            "pil.image", "pil image", "imagen", "image", "matriz", "matrix"))
        and any(term in normal for term in (
            "etiqueta", "label", "x y", "x, y", "28 por 28", "28 x 28",
            "28x28", "par"))
    ):
        return (
            "La captura inspecciona una muestra como par de entrada y etiqueta: la "
            "imagen se representa mediante una matriz de píxeles y la etiqueta conserva "
            "la clase correcta. Esta vista ayuda a distinguir el dato que recibe el "
            "modelo de la respuesta utilizada para supervisar su aprendizaje."
        )
    if (
        "tensor" in normal
        and any(term in normal for term in (
            "dimension", "canal", "channel", "shape", "matriz"))
    ):
        return (
            "La captura introduce la representación tensorial de los datos. El estudiante "
            "debe identificar dimensiones, forma y canales para entender cómo una imagen "
            "o lote se convierte en la estructura numérica procesada por la red."
        )
    return ""


def _caption_visual_determinista(slide: dict, max_chars: int = 420) -> str:
    """Publica solo señales compactas verificables cuando no existe leyenda corregida."""
    material = slide.get("material_oficial") or {}
    if material:
        return _titulo_visual(slide, max_chars=max_chars)
    if slide.get("browsing_reference"):
        query = _consulta_web_publica(slide)
        if query:
            if (
                not str(slide.get("browsing_query", "") or "").strip()
                and str(slide.get("browsing_parent_query", "") or "").strip()
            ):
                return (
                    f"La captura conserva el recurso que se abrió a partir de la "
                    f"consulta web «{query}». Permite revisar la fuente o el resultado "
                    "elegido por el profesor sin convertir menús, anuncios ni paneles "
                    "laterales en contenido de la lección."
                )
            return (
                f"La captura conserva una consulta web sobre «{query}» y la página de "
                "resultados que el profesor utilizó como apoyo durante la clase. "
                "Permite verificar qué referencia se abrió, pero el resultado del "
                "buscador no sustituye la explicación ni constituye por sí solo una "
                "fuente validada."
            )
        topic = _tema_referencia_web(slide)
        if topic:
            return (
                f"La captura documenta una página consultada sobre «{topic}». "
                "Permite reconstruir la referencia que el profesor abrió y el aspecto "
                "que estaba contrastando; los menús, anuncios y resultados laterales "
                "no forman parte de la explicación."
            )
        transcript = _extracto_transcript_visual(
            slide, max_chars=min(max_chars, 620))
        if transcript:
            return (
                "La captura conserva una página web consultada mientras el profesor "
                f"desarrollaba esta idea: {transcript} La imagen permite reconstruir "
                "qué recurso se abrió; los menús, anuncios y paneles laterales no forman "
                "parte del contenido docente."
            )
        return (
            "La captura conserva una referencia web abierta durante la clase. Su valor "
            "es documentar la navegación y el recurso consultado, no convertir menús, "
            "anuncios o resultados laterales en contenido docente."
        )
    if slide.get("media_reference"):
        title = _limpiar_texto_visual_publico(
            str(slide.get("media_title", "") or ""), 180)
        identity = f" «{title}»" if title else ""
        if slide.get("media_phase") == "ficha":
            return (
                f"La captura identifica el recurso audiovisual{identity} y conserva su "
                "página descriptiva como evidencia del material que se presentó. El "
                "título y la descripción principal forman parte del contexto; las "
                "recomendaciones, controles y métricas del reproductor no."
            )
        return (
            f"La captura conserva un estado interno de la demostración{identity}. "
            "Permite observar el montaje, la entrada mostrada y el resultado visible "
            "del recurso reproducido, sin tratar los controles del reproductor como "
            "parte del concepto."
        )
    ocr = str(slide.get("ocr_principal") or slide.get("ocr") or "")
    practical = _caption_entorno_practico(slide)
    if practical:
        return practical
    if _ocr_dominado_por_interfaz(ocr) or _contiene_datos_personales_visuales(ocr):
        return ""
    transcript_tokens = _tokens_relevantes(
        str(slide.get("transcript_ventana", "") or ""))
    numeric_arrays = re.findall(
        r"\[(?:\s*[-+]?\d+(?:[.,]\d+)?\s*,)*"
        r"\s*[-+]?\d+(?:[.,]\d+)?\s*\]",
        ocr,
    )
    if numeric_arrays:
        detalle = "; ".join(numeric_arrays[:4])
        return (
            "La captura conserva la estructura numérica de la entrada o de sus "
            f"dimensiones: {detalle}."
        )
    atomos = []
    for atom in sorted(visual_materials._technical_atoms(ocr)):
        atom_tokens = _tokens_relevantes(atom)
        supported = len(atom_tokens & transcript_tokens)
        if (
            re.search(r"\d", atom)
            or not atom_tokens
            or supported >= min(2, len(atom_tokens))
        ):
            atomos.append(atom)
    if atomos:
        detalle = "; ".join(atomos[:8])
        return (
            "La captura conserva un estado verificable de la fórmula o de sus "
            f"parámetros: {detalle}."
        )
    matches_asignacion = re.finditer(
        r"\b(?P<left>[A-Za-z_]\w*)\s*(?P<op>==|<=|>=|=|→|->)\s*"
        r"(?P<right>[A-Za-z_]\w*(?:\s*[+*/^]\s*"
        r"(?:[A-Za-z_]\w*|\d+(?:[.,]\d+)?))?)\b",
        ocr,
    )
    asignaciones = []
    for match in matches_asignacion:
        left = match.group("left")
        right = match.group("right")
        right_normal = _normalizar_busqueda(right).replace(" ", "")
        right_variables = re.findall(r"[a-z_]\w*", right_normal)
        estructura_matematica = bool(re.search(r"[+*/^]", right))
        identificador_codigo = "_" in left or "." in left
        variables_cortas = (
            len(left) == 1
            and len(right_normal) <= 12
            and (
                any(ch.isupper() for ch in right)
                or all(len(variable) <= 2 for variable in right_variables)
            )
        )
        if estructura_matematica or identificador_codigo or variables_cortas:
            asignaciones.append(match.group(0))
    if asignaciones:
        detalle = "; ".join(
            _limpiar_texto_visual_publico(item, 100)
            for item in asignaciones[:8]
        )
        return (
            "La captura conserva una relación simbólica verificable usada en la "
            f"explicación: {detalle}."
        )
    fragmentos_codigo = re.findall(
        r"\b(?:import\s+[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*|"
        r"from\s+[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s+import\s+[A-Za-z_*][\w*, ]*|"
        r"def\s+[A-Za-z_]\w*\s*\([^)]*\)|"
        r"class\s+[A-Za-z_]\w*(?:\([^)]*\))?\s*:|"
        r"(?:model\.fit|optimizer\.step|loss\.backward)\s*\([^)]*\))",
        ocr, flags=re.I,
    )
    if fragmentos_codigo:
        detalle = "; ".join(
            _limpiar_texto_visual_publico(item, 120)
            for item in fragmentos_codigo[:4]
        )
        return f"La captura conserva el fragmento técnico ejecutado: {detalle}."
    normal = _normalizar_busqueda(ocr)
    if _es_tarea_recurso_fuerte(normal):
        return (
            "La captura registra una actividad o entrega de la clase. El enunciado "
            "original se conserva en la imagen para verificar sus requisitos."
        )
    if slide.get("visual_role") not in {"principal", "secuencia", "galeria"}:
        return ""
    # El STT cercano aporta contexto temporal, pero no demuestra que describa
    # lo visible. Solo respalda un titulo breve que tambien se lee en la imagen.
    ocr_breve = _limpiar_texto_visual_publico(ocr, 180)
    tokens_ocr = _tokens_relevantes(ocr_breve)
    tokens_transcript = _tokens_relevantes(
        str(slide.get("transcript_ventana", "") or ""))
    role = str(slide.get("visual_role", "") or "")
    corroborada = len(tokens_ocr & tokens_transcript) >= min(
        2, len(tokens_ocr))
    ancla_principal = bool(
        role in {"principal", "secuencia"}
        and float(slide.get("visual_score", 0) or 0) >= 6
        and _texto_visual_publicable(ocr_breve, min_tokens=3)
        and not _contiene_codigo_formula(ocr_breve)
    )
    ancla_breve = bool(
        2 <= len(tokens_ocr) <= 14
        and (corroborada or ancla_principal)
        and not _texto_ruido_visual(ocr_breve)
        and not _ocr_con_casing_corrupto(ocr_breve)
        and str(slide.get("visual_tipo", "") or "") in {
            "slide_textual", "diagrama_o_grafico_probable",
            "codigo_formula_tabla",
        }
    )
    if ancla_breve:
        return (
            f"La captura presenta «{ocr_breve}». Este título o rótulo visible fue "
            "corroborado por la explicación del mismo tramo."
        )
    return ""


def _tipo_lectura_visual(slide: dict, texto: str) -> str:
    if slide.get("browsing_reference"):
        return "web"
    if slide.get("media_reference"):
        return "media"
    normal = _normalizar_busqueda(
        f"{texto} {slide.get('ocr_principal', '')} {slide.get('ocr', '')}")
    visual_tipo = str(slide.get("visual_tipo", "") or "")
    if re.search(
            r"\b(import|def|class|return|torch|torchvision|numpy|pandas|"
            r"model|optimizer|backward)\b", normal):
        return "codigo"
    if any(term in normal for term in (
            "dataset", "conjunto de datos", "muestra", "sample", "train", "test",
            "feature", "label", "etiqueta", "tabla", "matrix", "matriz")):
        return "datos"
    if _contiene_codigo_formula(normal):
        return "formula"
    if any(term in normal for term in (
            "historia", "history", "cronologia", "timeline", "evolucion",
            "evolution")) or len(re.findall(r"\b(?:18|19|20)\d{2}\b", normal)) >= 2:
        return "historia"
    if any(term in normal for term in (
            "comparacion", "comparison", "diferencia", "versus", " vs ",
            "ventaja", "desventaja")):
        return "comparacion"
    if any(term in normal for term in (
            "agenda", "objetivos", "objectives", "contenido del curso",
            "course outline")):
        return "agenda"
    if any(term in normal for term in (
            "entrada", "input", "salida", "output", "proceso", "workflow",
            "pipeline", "ciclo", "feedback")):
        return "proceso"
    if visual_tipo == "diagrama_o_grafico_probable":
        return "diagrama"
    return "lamina"


def _guia_lectura_visual(slide: dict, texto: str) -> tuple[str, str, str]:
    """Devuelve observación, interpretación y una acción concreta de estudio."""
    tipo = _tipo_lectura_visual(slide, texto)
    normal = _normalizar_busqueda(
        f"{texto} {slide.get('media_title', '')} "
        f"{slide.get('ocr_principal', '')} {slide.get('ocr', '')} "
        f"{slide.get('transcript_ventana', '')}"
    )
    if tipo == "media" and any(term in normal for term in (
            "lenet", "mnist", "handwritten", "digito manuscrito",
            "digitos manuscritos", "convolutional network")):
        return (
            "Localiza el dígito manuscrito usado como entrada, la clase o predicción "
            "mostrada por el sistema y los cambios de trazo, posición o grosor entre "
            "ejemplos. En la ficha del recurso, identifica además LeNet y el contexto "
            "histórico de la demostración.",
            "La secuencia muestra inferencia, no el proceso de entrenamiento: una red "
            "convolucional ya ajustada transforma los píxeles en características y luego "
            "elige una clase. Que reconozca escrituras distintas ilustra por qué aprender "
            "patrones de datos resulta más útil que programar reglas manuales para cada trazo.",
            "Reconstruye el recorrido entrada de 28 × 28 píxeles → extracción de "
            "características → puntuaciones por clase → dígito predicho, y distingue esa "
            "predicción de la etiqueta real usada durante el entrenamiento.",
        )
    guias = {
        "web": (
            "Distingue la consulta escrita, la fuente o resultado que se abrió y el "
            "fragmento que motivó la navegación; ignora anuncios y paneles laterales.",
            "La búsqueda documenta cómo el profesor contextualizó o contrastó una idea. "
            "Debe leerse junto con el desarrollo de la clase y las fuentes citadas.",
            "Explica qué duda intentaba resolver la consulta y qué parte de la lección "
            "quedó apoyada por el recurso abierto.",
        ),
        "media": (
            "Observa el material presentado, el estado visible de la demostración y la "
            "relación entre la entrada, el proceso mostrado y el resultado en pantalla.",
            "Una demostración aporta evidencia de comportamiento: no basta con reconocer "
            "el reproductor; hay que identificar qué cambió y qué afirmación ilustra.",
            "Describe el experimento en cuatro pasos: montaje, entrada, transformación y "
            "resultado observable; después enlázalo con el concepto explicado.",
        ),
        "codigo": (
            "Sigue el flujo del bloque: datos de entrada, operación ejecutada, formas o "
            "parámetros relevantes y salida que se inspecciona.",
            "El código traduce la explicación a operaciones verificables. Cada línea debe "
            "poder relacionarse con una decisión del procedimiento.",
            "Reconstruye qué recibe cada instrucción, qué transforma y qué resultado "
            "esperarías antes de ejecutarla.",
        ),
        "formula": (
            "Localiza símbolos, operadores, índices y valores; identifica qué magnitud "
            "depende de cuál y qué unidades o restricciones aparecen.",
            "La fórmula condensa una relación que el texto desarrolla. Leerla implica "
            "nombrar cada término y explicar el sentido de la operación.",
            "Define cada símbolo y razona qué cambiaría en el resultado al aumentar o "
            "disminuir uno de los parámetros.",
        ),
        "datos": (
            "Diferencia muestra, características, etiqueta, dimensiones y particiones; "
            "busca también variabilidad, balance o ejemplos atípicos.",
            "La visualización permite pasar de una colección abstracta a la estructura "
            "que realmente consume el modelo o procedimiento.",
            "Comprueba cuántas observaciones, variables y clases hay, y explica para qué "
            "se usa cada partición del conjunto de datos.",
        ),
        "historia": (
            "Ordena fechas, personas, hitos y limitaciones; no leas la cronología como "
            "una lista aislada de nombres.",
            "El valor pedagógico está en la cadena causal: qué problema existía, qué "
            "avance lo resolvió y qué consecuencia produjo.",
            "Resume la secuencia como causa → avance → consecuencia y ubica cada hito en "
            "esa relación.",
        ),
        "comparacion": (
            "Identifica los criterios compartidos y compara fila por fila o columna por "
            "columna, evitando fijarte solo en una diferencia llamativa.",
            "La comparación sirve para justificar una elección o revelar un límite; el "
            "criterio de comparación es tan importante como el resultado.",
            "Formula una conclusión por criterio y señala en qué escenario conviene cada "
            "alternativa.",
        ),
        "agenda": (
            "Lee la jerarquía de temas y el orden propuesto; distingue objetivos, bloques "
            "de contenido, práctica y evaluación.",
            "La agenda funciona como mapa de dependencias: los bloques iniciales preparan "
            "los conceptos que se aplicarán después.",
            "Explica qué conocimiento previo necesita cada bloque y qué capacidad debería "
            "quedar al finalizarlo.",
        ),
        "proceso": (
            "Recorre flechas y etapas desde la entrada hasta la salida; ubica bucles, "
            "decisiones, retroalimentación y criterios de parada.",
            "El diagrama representa una secuencia operativa. Su lectura correcta exige "
            "explicar qué produce cada etapa para la siguiente.",
            "Narra el proceso sin mirar la imagen y verifica que puedas justificar cada "
            "transición.",
        ),
        "diagrama": (
            "Identifica componentes, agrupaciones, direcciones y relaciones espaciales; "
            "observa qué elementos reciben mayor énfasis visual.",
            "La disposición convierte relaciones abstractas en una estructura. El texto "
            "de la lección aporta el significado que no se deduce solo de la geometría.",
            "Explica la función de cada componente y cómo cambiaría el sistema si uno se "
            "eliminara o modificara.",
        ),
        "lamina": (
            "Empieza por el título, sigue la jerarquía de ideas y relaciona cada ejemplo "
            "o cifra con la afirmación principal.",
            "La lámina organiza evidencia, pero la explicación cercana establece las "
            "relaciones causales y los matices necesarios para comprenderla.",
            "Reformula la idea central con tus palabras y usa los elementos visibles como "
            "pruebas o ejemplos de esa formulación.",
        ),
    }
    return guias[tipo]


def _caption_visual_generico(texto: str) -> bool:
    """Detecta placeholders que no explican qué aporta una captura concreta."""
    normal = _normalizar_busqueda(texto)
    if not normal:
        return False
    return any(placeholder in normal for placeholder in (
        "evidencia visual conservada para este tramo",
        "evidencia visual complementaria",
        "evidencia visual vinculada con la explicacion del tramo",
        "captura conservada como evidencia visual",
    ))


def _descomponer_lectura_guiada(texto: str) -> dict[str, str]:
    """Interpreta una guia ya estructurada para no anidar sus rotulos."""
    raw = re.sub(r"(?i)<br\s*/?>", "\n", str(texto or ""))
    raw = raw.replace("**", "")
    patron = re.compile(
        r"(?i)(?:lectura\s+guiada\s*(?:[-—–]\s*)?idea\s+central|"
        r"qu[eé]\s+observar|c[oó]mo\s+interpretarla|para\s+estudiar)\s*:"
    )
    matches = list(patron.finditer(raw))
    if not matches:
        return {}
    campos: dict[str, str] = {}
    for indice, match in enumerate(matches):
        etiqueta = _normalizar_busqueda(match.group(0))
        if "lectura guiada" in etiqueta:
            clave = "idea"
        elif "observar" in etiqueta:
            clave = "observacion"
        elif "interpretarla" in etiqueta:
            clave = "interpretacion"
        else:
            clave = "repaso"
        fin = matches[indice + 1].start() if indice + 1 < len(matches) else len(raw)
        valor = raw[match.end():fin]
        valor = re.sub(r"^[^\w¿¡]+", "", valor, flags=re.UNICODE).strip()
        limpio = _limpiar_texto_visual_publico(valor, 1000)
        if limpio:
            campos[clave] = limpio
    return campos


def _visual_card_md(slide: dict, *, archivo: str, caption: str = "",
                    origen=None, parte: int | str = "",
                    caption_confiable: bool = False,
                    permitir_transcript: bool = True,
                    permitir_detalle_ocr: bool = True) -> str:
    """Tarjeta 60/40: imagen dominante y lectura guiada, sin OCR crudo."""
    if _contiene_datos_personales_visuales(
            str(slide.get("ocr_principal") or slide.get("ocr") or "")):
        return ""
    if _caption_visual_generico(caption):
        caption = _caption_visual_determinista(slide, max_chars=900)
        caption_confiable = bool(caption)
    guia_estructurada = _descomponer_lectura_guiada(caption)
    lectura = _limpiar_texto_visual_publico(
        guia_estructurada.get("idea", caption), 1000)
    if not _texto_visual_explicativo(
            lectura, min_tokens=4, caption_confiable=caption_confiable):
        lectura = ""
    raw_ocr = _limpiar_texto_visual_publico(
        slide.get("ocr_principal") or slide.get("ocr") or "", 700)
    lectura_tokens = set(_tokens_relevantes(lectura))
    ocr_tokens = set(_tokens_relevantes(raw_ocr))
    solapamiento_ocr = (
        len(lectura_tokens & ocr_tokens) / max(1, min(len(lectura_tokens), len(ocr_tokens)))
        if lectura_tokens and ocr_tokens else 0.0)
    if (not caption_confiable and solapamiento_ocr >= 0.72 and len(ocr_tokens) >= 8
            and not _ocr_visual_confiable(slide)):
        lectura = ""
    transcript = (
        _extracto_transcript_visual(slide, max_chars=720)
        if permitir_transcript else ""
    )
    visible = (
        _ocr_visual_confiable(slide, max_chars=440)
        if permitir_detalle_ocr and not (caption_confiable and lectura) else ""
    )
    titulo = lectura or visible or transcript
    if not titulo:
        return ""
    # Una leyenda producida para la lección ya integra y corrige las señales crudas.
    # Repetir debajo la ventana STT vuelve a introducir errores de reconocimiento.
    idea = "" if caption_confiable and lectura else transcript
    if idea:
        idea_norm = _normalizar_busqueda(idea)
        titulo_norm = _normalizar_busqueda(titulo)
        if titulo_norm in idea_norm or idea_norm in titulo_norm:
            idea = ""
    observacion, interpretacion, repaso = _guia_lectura_visual(
        slide, f"{titulo} {visible} {idea}")
    observacion = guia_estructurada.get("observacion", observacion)
    interpretacion = guia_estructurada.get("interpretacion", interpretacion)
    repaso = guia_estructurada.get("repaso", repaso)
    if visible and _normalizar_busqueda(visible) not in {
            _normalizar_busqueda(titulo), _normalizar_busqueda(idea)}:
        observacion = visible
    momento = ""
    if origen is not None:
        momento = fmt_t(_cap_t(slide), origen)
    elif slide.get("t_rel") is not None:
        momento = fmt_t(float(slide.get("t_rel") or 0), {"origen": "video"})
    material = slide.get("material_oficial") or {}
    procedencia = (
        f"Material oficial, página {material.get('pagina', '?')}; captura de aparición "
        f"conservada en {visual_materials.MANIFEST_NAME}."
        if material else "Captura original conservada como evidencia."
    )
    meta = " · ".join(filter(None, [
        momento,
        f"Parte {parte}" if parte != "" else "",
        procedencia,
    ]))
    conexion = (
        f"<br><br>**🔗 Conexión con la explicación:** {idea}"
        if idea else ""
    )
    celda = (
        f"**🧭 Lectura guiada — idea central:** {titulo}"
        f"<br><br>**👁️ Qué observar:** {observacion}"
        f"{conexion}"
        f"<br><br>**🧠 Cómo interpretarla:** {interpretacion}"
        f"<br><br>**🎯 Para estudiar:** {repaso}<br><br>"
        f"<small>{meta}</small>"
    )
    celda = celda.replace("|", "/").replace("<script", "&lt;script")
    return (
        "| 🖼️ Evidencia visual | 📖 Explicación para estudiar |\n"
        "|:---|:---|\n"
        f'| <img src="img/{archivo}" width="760"> | {celda} |'
    )


def _caption_archivo_estudio(d: Path | None, archivo: str, fallback: str) -> str:
    if d is not None:
        manifest = d / visual_materials.MANIFEST_NAME
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                page = next((
                    item for item in data.get("paginas", [])
                    if str(item.get("archivo", "")) == archivo
                ), None)
                if page:
                    return _titulo_visual({"material_oficial": page})
            except (OSError, ValueError, TypeError):
                pass
    clean = _limpiar_texto_visual_publico(fallback, 220)
    if not _texto_visual_publicable(clean, min_tokens=3):
        clean = ""
    return clean or "Evidencia visual complementaria"


def _apendice_slides_omitidas_md(merge_input: str, faltantes: set[str],
                                 parte: int, d: Path | None = None) -> str:
    omitidas = _bloques_img_omitidas(merge_input, faltantes)
    if not omitidas:
        return ""
    lineas = [
        "## Apendice visual de estudio",
        "",
        "> Estas capturas fueron seleccionadas y explicadas en los chunks, pero el merge "
        "tematico no las coloco en el cuerpo principal. Se conservan para estudiar "
        "diapositiva por diapositiva.",
        "",
    ]
    for item in omitidas:
        archivo = str(item.get("archivo", "") or "").strip()
        if not archivo:
            continue
        if d is not None and not (d / "img" / archivo).is_file():
            continue
        caption = str(item.get("caption") or "").strip()
        caption = _caption_archivo_estudio(
            d, archivo, caption or str(item.get("bloque", "") or ""))
        lineas.extend([
            f"### Slide conservada: {archivo}",
            "",
            "| Captura | Contexto de estudio |",
            "|---|---|",
            f'| <img src="img/{archivo}" width="420"> | **Parte {parte}.** '
            f"{caption} |",
            "",
        ])
    return "\n".join(lineas).strip()


def _secciones_nivel_dos(cuerpo: str) -> tuple[str, list[dict]]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", cuerpo, re.MULTILINE))
    if not matches:
        return cuerpo, []
    prefijo = cuerpo[:matches[0].start()]
    secciones = []
    for idx, match in enumerate(matches):
        fin = matches[idx + 1].start() if idx + 1 < len(matches) else len(cuerpo)
        secciones.append({
            "titulo": match.group(1).strip(),
            "texto": cuerpo[match.start():fin].rstrip(),
            "extras": [],
        })
    return prefijo, secciones


def _bloque_visual_pedagogico(item: dict, parte: int, d: Path | None,
                              slide: dict | None = None, origen=None) -> str:
    archivo = str(item.get("archivo", "") or "").strip()
    slide = dict(slide or {})
    slide.setdefault("archivo", archivo)
    caption = _caption_archivo_estudio(
        d, archivo, str(item.get("caption") or ""))
    if caption == "Evidencia visual complementaria":
        contexto = _limpiar_texto_visual_publico(item.get("bloque", ""), 500)
        caption = contexto if _texto_visual_publicable(contexto, min_tokens=4) else ""
    return _visual_card_md(
        slide, archivo=archivo, caption=caption, origen=origen, parte=parte,
        caption_confiable=True, permitir_detalle_ocr=False)


def _integrar_visuales_omitidos_en_leccion(
        cuerpo_final: str, merge_input: str, faltantes: set[str], *,
        parte: int, d: Path | None = None, slides: list[dict] | None = None,
        origen=None) -> tuple[str, dict]:
    """Reubica visuales valiosos por afinidad temática y deja UI/no-valor en debug."""
    omitidas = _bloques_img_omitidas(merge_input, faltantes)
    prefijo, secciones = _secciones_nivel_dos(cuerpo_final)
    fallback: list[str] = []
    por_archivo = {
        str(slide.get("archivo", "")): slide for slide in (slides or [])
    }
    integradas = solo_debug = 0
    for item in omitidas:
        archivo = str(item.get("archivo", "") or "").strip()
        if not archivo or (d is not None and not (d / "img" / archivo).is_file()):
            solo_debug += 1
            continue
        if item.get("es_interfaz"):
            solo_debug += 1
            continue
        bloque = _bloque_visual_pedagogico(
            item, parte, d, por_archivo.get(archivo), origen)
        if not bloque:
            solo_debug += 1
            continue
        tokens_item = _tokens_relevantes(
            f"{item.get('tema', '')} {item.get('caption', '')} "
            f"{_limpiar_contexto_slide_omitida(item.get('bloque', ''), 500)}"
        )
        mejor_idx = None
        mejor_score = 0.0
        for idx, seccion in enumerate(secciones):
            tokens_seccion = _tokens_relevantes(
                f"{seccion['titulo']} {seccion['texto'][:1000]}")
            score = _sim_jaccard(tokens_item, tokens_seccion)
            tema_norm = _normalizar_busqueda(str(item.get("tema", "")))
            titulo_norm = _normalizar_busqueda(str(seccion["titulo"]))
            if tema_norm and (tema_norm in titulo_norm or titulo_norm in tema_norm):
                score += 1.0
            if score > mejor_score:
                mejor_score, mejor_idx = score, idx
        if mejor_idx is not None and mejor_score > 0.0:
            secciones[mejor_idx]["extras"].append(bloque)
        else:
            fallback.append(bloque)
        integradas += 1
    partes = [prefijo.rstrip()]
    for seccion in secciones:
        partes.append(seccion["texto"])
        partes.extend(seccion["extras"])
    if fallback:
        partes.extend([
            "## Desarrollo visual complementario\n\n"
            "> Visuales pedagógicamente distintos cuyo tema no pudo ubicarse con "
            "confianza en una sección existente.",
            *fallback,
        ])
    return "\n\n".join(p for p in partes if p.strip()).strip(), {
        "integradas": integradas,
        "solo_debug": solo_debug,
        "sin_seccion": len(fallback),
    }


def _integrar_cobertura_visual_obligatoria(
        cuerpo: str, d: Path, slides: list[dict], origen,
        parte: int | str = "") -> tuple[str, dict]:
    """Impide que una identidad principal desaparezca por caption, lote o merge."""
    esperadas: list[dict] = []
    vistas: set[str] = set()
    for slide in slides:
        archivo = str(slide.get("archivo", "") or "").strip()
        if (
            slide.get("visual_role") not in {"principal", "secuencia"}
            or not archivo
            or archivo in vistas
            or not (d / "img" / archivo).is_file()
            or _contiene_datos_personales_visuales(
                str(slide.get("ocr_principal") or slide.get("ocr") or ""))
        ):
            continue
        vistas.add(archivo)
        esperadas.append(slide)
    publicadas_antes = set(extraer_referencias_img(cuerpo))
    faltantes = [
        slide for slide in esperadas
        if str(slide.get("archivo", "")) not in publicadas_antes
    ]
    cards: list[str] = []
    fallos: list[str] = []
    for slide in faltantes:
        archivo = str(slide["archivo"])
        caption = _caption_visual_determinista(slide, max_chars=760)
        if not caption:
            fallos.append(archivo)
            continue
        card = _visual_card_md(
            slide,
            archivo=archivo,
            caption=caption,
            origen=origen,
            parte=parte,
            caption_confiable=True,
            permitir_transcript=False,
            permitir_detalle_ocr=True,
        )
        if card:
            cards.append(card)
        else:
            fallos.append(archivo)
    if cards:
        cuerpo = cuerpo.rstrip() + "\n\n" + "\n\n".join([
            "## 🧩 Diapositivas y secuencias adicionales",
            (
                "> Estas identidades pedagógicamente distintas no quedaron ubicadas por "
                "el tejido temático. Se incorporan aquí con lectura guiada para que ninguna "
                "diapositiva principal ni secuencia real se pierda."
            ),
            *cards,
        ])
    publicadas_despues = set(extraer_referencias_img(cuerpo))
    principales = {
        str(slide.get("archivo", "")) for slide in esperadas
        if slide.get("visual_role") == "principal"
    }
    secuencias = {
        str(slide.get("archivo", "")) for slide in esperadas
        if slide.get("visual_role") == "secuencia"
    }
    return cuerpo, {
        "esperadas": len(esperadas),
        "principales_esperadas": len(principales),
        "secuencias_esperadas": len(secuencias),
        "principales_publicadas": len(principales & publicadas_despues),
        "secuencias_publicadas": len(secuencias & publicadas_despues),
        "reintegradas_por_gate": len(
            (publicadas_despues - publicadas_antes)
            & {str(slide.get("archivo", "")) for slide in faltantes}
        ),
        "faltantes": sorted(
            {str(slide.get("archivo", "")) for slide in esperadas}
            - publicadas_despues
        ),
        "fallos_caption": sorted(set(fallos)),
    }


def _galeria_visual_complementaria_md(
        d: Path, slides: list[dict], origen, parte: int | str = "") -> str:
    """Publica evidencia relevante no principal, sin rescatar duplicados o idle frames."""
    relevantes = []
    solo_evidencia = 0
    vistos: set[str] = set()
    for slide in slides:
        archivo = str(slide.get("archivo", "") or "").strip()
        if (slide.get("visual_role") != "galeria" or not archivo
                or archivo in vistos or not (d / "img" / archivo).is_file()):
            continue
        vistos.add(archivo)
        if _contiene_datos_personales_visuales(
                str(slide.get("ocr_principal") or slide.get("ocr") or "")):
            solo_evidencia += 1
            continue
        razones = motivos_slide_estudio(slide)
        transcript = _extracto_transcript_visual(slide, max_chars=300)
        material = bool(slide.get("material_oficial"))
        ocr_tokens = _tokens_relevantes(str(slide.get("ocr", "") or ""))
        transcript_tokens = _tokens_relevantes(
            str(slide.get("transcript_ventana", "") or ""))
        comunes = len(ocr_tokens & transcript_tokens)
        cobertura_ocr = comunes / max(1, len(ocr_tokens))
        corroborada = comunes >= 3 and cobertura_ocr >= 0.35
        estable = bool(slide.get("visual_estable"))
        visual_score = float(slide.get("visual_score", 0) or 0)
        referencia_observada = bool(
            slide.get("browsing_reference") or slide.get("media_reference"))
        narrativa_legible = bool(_ocr_visual_confiable(slide, max_chars=420))
        narrativa_alineada = bool(
            narrativa_legible
            and visual_score >= 6
            and (
                corroborada
                or estable
                or int(slide.get("grupo_total", 1) or 1) >= 2
            )
        )
        estado_visual_distinto = bool(
            visual_score >= 6
            and str(slide.get("visual_tipo", "") or "") in {
                "slide_textual", "diagrama_o_grafico_probable",
                "codigo_formula_tabla",
            }
            and not _ocr_dominado_por_interfaz(
                str(slide.get("ocr_principal") or slide.get("ocr") or ""))
            and (
                corroborada
                or estable
                or int(slide.get("grupo_total", 1) or 1) >= 2
            )
        )
        # No es un techo: cada visual que supera el contrato semántico se publica.
        # El resto sigue trazable en img_log/timeline y conserva su archivo original.
        if (referencia_observada or material or razones or narrativa_alineada
                or estado_visual_distinto
                or (corroborada and transcript and estable and visual_score >= 6)):
            relevantes.append(slide)
        else:
            solo_evidencia += 1
    publicables: list[tuple[dict, str]] = []
    for slide in relevantes:
        archivo = str(slide["archivo"])
        caption = _caption_visual_determinista(slide)
        if not caption:
            solo_evidencia += 1
            continue
        card = _visual_card_md(
            slide, archivo=archivo, caption=caption,
            origen=origen, parte=parte, caption_confiable=True,
            permitir_transcript=False, permitir_detalle_ocr=True,
        )
        if card:
            publicables.append((slide, card))
        else:
            solo_evidencia += 1
    if not publicables:
        return ""
    def clave_cronologica(item):
        return (
            int(item[0].get("parte", parte) or 0),
            _cap_t(item[0]),
            int(item[0].get("_orden_visual", 0) or 0),
            str(item[0].get("archivo", "")),
        )
    destacadas = sorted([
        item for item in publicables
        if item[0].get("browsing_reference") or item[0].get("media_reference")
    ], key=clave_cronologica)
    complementarias = sorted(
        [item for item in publicables if item not in destacadas],
        key=clave_cronologica,
    )
    lineas: list[str] = []
    if destacadas:
        lineas.extend([
            "## 🔍 Recursos consultados y demostraciones observadas",
            "",
            "> Estas capturas documentan búsquedas, páginas y recursos audiovisuales "
            "que aparecieron realmente durante la clase. La lectura guiada separa el "
            "contenido docente de anuncios, controles y recomendaciones.",
            "",
        ])
        for _slide, card in destacadas:
            lineas.extend([card, ""])
    if complementarias:
        lineas.extend([
            "## 🖼️ Galería visual complementaria",
            "",
            "> Evidencia visual relevante que complementa la explicación. Los duplicados "
            "y frames sin valor permanecen trazables en el timeline, pero no se publican "
            "aquí.",
            "",
            "<details>",
            f"<summary>🖼️ Abrir galería complementaria "
            f"({len(complementarias)} visuales)</summary>",
            "",
        ])
    ventana_actual = None
    for slide, card in complementarias:
        inicio = int(max(0, _cap_t(slide)) // 600) * 600
        if inicio != ventana_actual:
            ventana_actual = inicio
            lineas.extend([
                f"### 🎞️ Evidencia de {fmt_t(inicio, {'origen': 'video'})} a "
                f"{fmt_t(inicio + 600, {'origen': 'video'})}", "",
            ])
        lineas.extend([
            card,
            "",
        ])
    if solo_evidencia:
        lineas.extend([
            f"> {solo_evidencia} frame(s) adicional(es) se conservaron como evidencia "
            "trazable, pero no se publicaron porque carecían de explicación cercana o "
            "valor pedagógico verificable.", "",
        ])
    if complementarias:
        lineas.extend(["</details>", ""])
    return "\n".join(lineas).strip()


def _galeria_material_oficial_no_observado_md(
        d: Path, pages: list[dict], *, observed_ids: set[str]) -> str:
    """Referencia todo el deck sin presentar páginas no observadas como voz del profesor."""
    pending = [
        page for page in pages
        if str(page.get("material_id", "")) not in observed_ids
        and str(page.get("archivo", ""))
        and (d / "img" / str(page.get("archivo", ""))).is_file()
    ]
    if not pending:
        return ""
    lines = [
        "## Galería del material oficial no observado", "",
        "> Estas páginas pertenecen al material oficial aportado, pero su aparición no fue "
        "verificada en la captura. Se incluyen como complemento y no como afirmación del "
        "profesor en esta grabación.", "",
    ]
    for page in sorted(pending, key=lambda item: (
            str(item.get("fuente_sha256", "")), int(item.get("pagina", 0) or 0))):
        archivo = str(page["archivo"])
        title = _titulo_visual({"material_oficial": page})
        lines.extend([
            f"### {title}", "",
            f'<img src="img/{archivo}" width="620">', "",
            "*Material oficial; aparición no verificada en la grabación.*", "",
        ])
    return "\n".join(lines).strip()


def _reporte_slides_md(reportes_partes: list[dict], capturas_todas: list[dict],
                       contenido_integrado: str, cobertura: dict) -> str:
    originales = len(capturas_todas)
    por_chunks = sum(int(r.get("capturas_originales", 0)) for r in reportes_partes)
    slides_dedup = sum(int(r.get("slides_deduplicadas", 0)) for r in reportes_partes)
    slides_candidatas = sum(int(r.get("slides_candidatas", 0)) for r in reportes_partes)
    slides_enviadas = sum(int(r.get("slides_enviadas", 0)) for r in reportes_partes)
    slides_descartadas = sum(int(r.get("slides_descartadas_presupuesto", 0))
                             for r in reportes_partes)
    slides_protegidas = sum(int(r.get("slides_protegidas_estudio", 0))
                            for r in reportes_partes)
    slides_usadas = sum(int(r.get("slides_usadas", 0)) for r in reportes_partes)
    slides_usadas_chunks = sum(int(r.get("slides_usadas_chunks", 0))
                               for r in reportes_partes)
    slides_integradas_final = sum(int(r.get("slides_integradas_final", 0))
                                  for r in reportes_partes)
    slides_omitidas_merge = sum(int(r.get("slides_omitidas_por_merge", 0))
                                for r in reportes_partes)
    slides_omitidas_debug = sum(int(r.get("slides_omitidas_debug", 0))
                                for r in reportes_partes)
    slides_reintegradas = sum(int(r.get("slides_reintegradas_por_tema", 0))
                              for r in reportes_partes)
    slides_solo_debug = sum(int(r.get("slides_solo_debug", 0))
                            for r in reportes_partes)
    interfaz_filtradas = sum(int(r.get("capturas_interfaz_filtradas", 0))
                             for r in reportes_partes)
    interfaz_secundarias = sum(int(r.get("capturas_interfaz_secundarias", 0))
                               for r in reportes_partes)
    duplicadas = sum(int(r.get("imagenes_duplicadas_detectadas", 0))
                     for r in reportes_partes)
    duplicadas_eliminadas = sum(int(r.get("duplicados_eliminados", 0))
                                for r in reportes_partes)
    llamadas_antes = sum(int(r.get("llamadas_llm_estimadas_antes", 0))
                         for r in reportes_partes)
    llamadas_reales = sum(int(r.get("llamadas_llm_reales", 0))
                          for r in reportes_partes)
    materiales = max([int(r.get("materiales_oficiales", 0))
                      for r in reportes_partes] or [0])
    paginas_oficiales = max([int(r.get("paginas_oficiales", 0))
                             for r in reportes_partes] or [0])
    paginas_usadas = sum(int(r.get("paginas_oficiales_usadas", 0))
                         for r in reportes_partes)
    estados_emparejados = sum(int(r.get("estados_emparejados_material", 0))
                              for r in reportes_partes)
    duplicados_material = sum(int(r.get("duplicados_material_colapsados", 0))
                              for r in reportes_partes)
    matches_ambiguos = sum(int(r.get("matches_material_rechazados", 0))
                           for r in reportes_partes)
    roles_principal = sum(int(r.get("visuales_principales", 0))
                          for r in reportes_partes)
    roles_secuencia = sum(int(r.get("visuales_secuencia", 0))
                          for r in reportes_partes)
    roles_galeria = sum(int(r.get("visuales_galeria", 0))
                        for r in reportes_partes)
    roles_sin_valor = sum(int(r.get("visuales_sin_valor", 0))
                          for r in reportes_partes)
    advertencias_caption: list[str] = []
    for r in reportes_partes:
        advertencias_caption.extend(r.get("caption_incompatible_advertencias", []))
    imgs_finales = len(set(extraer_referencias_img(contenido_integrado)))
    lineas = [
        "## Reporte transcript-first / slide-guided",
        "",
        f"- Capturas registradas en img_log: {originales}.",
        f"- Capturas vistas por chunks: {por_chunks}.",
        f"- Slides deduplicadas: {slides_dedup}.",
        f"- Slides candidatas tras ranking local: {slides_candidatas or slides_dedup}.",
        f"- Slides protegidas como material de estudio: {slides_protegidas}.",
        f"- Slides enviadas a DeepSeek: {slides_enviadas}.",
        f"- Slides descartadas por presupuesto adaptativo: {slides_descartadas}.",
        f"- Slides usadas por chunks: {slides_usadas_chunks or slides_usadas}.",
        f"- Slides integradas en la lección final: {slides_integradas_final or imgs_finales}.",
        f"- Slides omitidas por merge: {slides_omitidas_merge}.",
        f"- Slides reintegradas cerca de su explicación: {slides_reintegradas}.",
        f"- Slides auditables en debug: {slides_omitidas_debug}; solo debug/no valor: "
        f"{slides_solo_debug}.",
        f"- Material oficial: {materiales} fuente(s), {paginas_oficiales} página(s); "
        f"{estados_emparejados} estados emparejados con {paginas_usadas} páginas usadas.",
        f"- Duplicados de una misma página oficial colapsados: {duplicados_material}.",
        f"- Matches oficiales ambiguos rechazados conservando captura: {matches_ambiguos}.",
        f"- Roles visuales: principales {roles_principal}; secuencias {roles_secuencia}; "
        f"galería {roles_galeria}; sin valor publicado {roles_sin_valor}.",
        f"- Capturas filtradas por interfaz ClaseViva/no-slide: {interfaz_filtradas}.",
        f"- Capturas de interfaz conservadas como evidencia secundaria: {interfaz_secundarias}.",
        f"- Imágenes referenciadas en la lección integrada: {imgs_finales}.",
        f"- Imágenes duplicadas detectadas: {duplicadas}; duplicados eliminados: "
        f"{duplicadas_eliminadas}.",
        f"- WAV auditados: {cobertura.get('wav_total', 0)}.",
        f"- Llamadas DeepSeek estimadas sin ranking adaptativo: {llamadas_antes}.",
        f"- Llamadas DeepSeek reales para guiones/merge: {llamadas_reales}.",
    ]
    for adv in advertencias_caption[:10]:
        lineas.append(f"- Advertencia de caption incompatible: {adv}.")
    if cobertura.get("wav_total", 0) == 0:
        lineas.append("- No se puede reauditar audio: no quedan WAV disponibles.")
    lineas.append("- Se protegen slides progresivas finales, pantallas estables y visuales "
                  "explicados aunque tengan poco OCR; frames de video/animación se muestrean "
                  "por ranking local para evitar frame-a-frame.")
    lineas.append("- Las slides valiosas omitidas por el merge se reintegran cerca de su "
                  "explicación o en desarrollo visual complementario; la galería conserva "
                  "evidencia secundaria. Duplicados y frames sin valor siguen trazables en "
                  "manifests/timeline sin publicarse.")
    lineas.append("- DeepSeek es texto-a-texto: no recibe WAV ni ve imágenes directamente; "
                  "el transcript manda y las slides son evidencia contextual.")
    if originales and not slides_dedup:
        lineas.append("- Advertencia: hubo capturas, pero no quedaron slides deduplicadas útiles.")
    return "\n".join(lineas).strip() + "\n"


def _en_periodo_sin_audio(c: dict, cobertura: dict | None) -> bool:
    if not cobertura:
        return False
    t = _cap_t(c)
    return any(p.get("inicio", 0) < t <= p.get("fin", 0)
               for p in cobertura.get("periodos_con_capturas_sin_audio", []))


def seleccionar_capturas_relevantes(d: Path, capturas: list[dict], segs_trozo: list[dict],
                                    origen: str, max_imgs: int | None = None,
                                    cobertura: dict | None = None) -> list[dict]:
    """Selecciona pocas capturas utiles antes de construir fichas para DeepSeek."""
    max_imgs = _post_img_max() if max_imgs is None else max(0, int(max_imgs))
    if max_imgs <= 0 or not capturas:
        return []
    caps_norm = [dict(c, t_rel=normalizar_tiempo_captura(c, origen)) for c in capturas]
    unicas = _dedup_capturas(d, caps_norm)
    if not unicas:
        return []

    segs = _segmentos_ordenados(segs_trozo, origen)
    scores_seg = [(s, _score_segmento(s)) for s in segs]
    t_min = min([_seg_t(s) for s in segs], default=min(_cap_t(c) for c in unicas))
    t_max = max([_seg_fin(s) for s in segs], default=max(_cap_t(c) for c in unicas))
    dur_min = max(0.1, (t_max - t_min) / 60)
    presupuesto = min(max_imgs, max(_post_img_min(), math.ceil(dur_min * _post_img_per_min())))
    presupuesto = min(presupuesto, len(unicas))

    puntuadas = []
    for c in unicas:
        t = _cap_t(c)
        cercanos = [(abs(_seg_t(s) - t), score) for s, score in scores_seg]
        dist, score_seg = min(cercanos, default=(9999.0, 0))
        score = 0.0
        if dist <= 20:
            score += 8 + score_seg
        elif dist <= 60:
            score += 5 + score_seg * 0.7
        elif dist <= 120:
            score += 2 + score_seg * 0.3
        if _en_periodo_sin_audio(c, cobertura):
            score -= 5
        texto_ocr = _ocr_existente(c)
        if texto_ocr:
            score += min(8, len(texto_ocr) / 80)
            if _texto_ruido_visual(texto_ocr):
                score -= 8
        puntuadas.append((score, c))

    # Solo aplicar OCR local al pool plausible; evita OCR masivo en cientos de capturas.
    pool_n = min(len(puntuadas), max(max_imgs * 3, 32))
    pool = sorted(puntuadas, key=lambda x: (-x[0], _cap_t(x[1])))[:pool_n]
    enriquecidas = []
    for score, c in pool:
        c2 = dict(c)
        texto_ocr = _ocr_para_captura(d, c2)
        c2["ocr"] = texto_ocr
        if texto_ocr:
            score += min(10, len(texto_ocr) / 70)
            if _texto_ruido_visual(texto_ocr):
                score -= 10
        elif not segs:
            score -= 4
        if _en_periodo_sin_audio(c2, cobertura) and len(texto_ocr) < 60:
            score -= 6
        enriquecidas.append((score, c2))

    if not segs and not any(len(c.get("ocr", "")) >= 60 for _, c in enriquecidas):
        return []
    seleccion = [c for score, c in sorted(enriquecidas, key=lambda x: (-x[0], _cap_t(x[1])))
                 if score > 0][:presupuesto]
    return sorted(seleccion, key=_cap_t)


def fichas_imagenes(d: Path, unicas: list[dict], origen: str) -> str:
    fichas = []
    max_imgs = _post_img_max()
    for c in unicas[:max_imgs]:
        t = _ocr_para_captura(d, c)[:600]
        fichas.append(f"{c['archivo']} (hora {fmt_t(_cap_t(c), origen)})\n"
                      f"OCR: {t or '(sin texto legible)'}")
    return "\n\n".join(fichas) or "(sin capturas en este tramo)"


def _nombre_img_ref(ref: str) -> str:
    ref = ref.strip().replace("\\", "/")
    ref = ref.split("#", 1)[0].split("?", 1)[0]
    return ref.rsplit("/", 1)[-1]


def _normalizar_busqueda(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt or "")
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    return txt.lower()


def es_captura_interfaz_claseviva(ocr: str, archivo: str, contexto: str = "") -> bool:
    """Detecta capturas del panel local con alta confianza, no slides del profesor."""
    blob = _normalizar_busqueda(f"{archivo} {ocr} {contexto}")
    def contiene(termino: str) -> bool:
        return bool(re.search(rf"(?<!\w){re.escape(termino)}(?!\w)", blob))

    if any(contiene(t) for t in TERMINOS_INTERFAZ_CLASEVIVA_FUERTES):
        return True
    controles = {
        "controles de clase", "pausar", "seguir", "fin", "consolidar",
        "copiloto", "transcripcion", "post-proceso", "post proceso", "sin clase",
    }
    controles_hit = {t for t in controles if contiene(t)}
    if len(controles_hit) >= 2:
        return True
    tiene_control = bool(controles_hit)
    tiene_contexto_panel = any(contiene(t) for t in (
        "estado", "clase actual", "curso", "sesion", "tema", "pantalla", "audio"))
    return tiene_control and tiene_contexto_panel


def extraer_referencias_img(md: str) -> list[str]:
    """Extrae referencias únicas a imágenes locales en Markdown, HTML y [[IMG:...]]."""
    refs: list[str] = []
    for m in RE_MARCADOR.finditer(md or ""):
        refs.append(_nombre_img_ref(m.group(1)))
    for m in RE_MD_IMG.finditer(md or ""):
        refs.append(_nombre_img_ref(m.group(1)))
    for m in RE_HTML_IMG.finditer(md or ""):
        refs.append(_nombre_img_ref(m.group(1)))
    for ref in RE_IMG_REF.findall(md or ""):
        refs.append(_nombre_img_ref(ref))
    vistas: set[str] = set()
    unicas: list[str] = []
    for ref in refs:
        if ref and ref not in vistas:
            vistas.add(ref)
            unicas.append(ref)
    return unicas


def _caption_de_linea_img(linea: str) -> str:
    marker = RE_MARCADOR.search(linea)
    if marker:
        return (marker.group(2) or "").strip()
    tabla = re.search(r"\|\s*<img\b[^>]*>\s*\|\s*(.*?)\s*\|?\s*$", linea, re.I)
    if tabla:
        return re.sub(r"<[^>]+>", " ", tabla.group(1)).strip()
    md_img = re.search(r"!\[([^\]]*)\]", linea)
    return (md_img.group(1) if md_img else "").strip()


def _tokens_caption(txt: str) -> set[str]:
    stop = {
        "slide", "imagen", "captura", "hora", "evidencia", "visual", "profesor",
        "profesora", "clase", "tema", "esta", "este", "that", "this", "with",
        "para", "como", "desde", "sobre",
    }
    return {w for w in re.findall(r"[a-z0-9]{4,}", _normalizar_busqueda(txt))
            if w not in stop}


def _captions_compatibles(a: str, b: str) -> bool:
    na = _normalizar_busqueda(a).strip()
    nb = _normalizar_busqueda(b).strip()
    if not na or not nb or na in nb or nb in na:
        return True
    ta, tb = _tokens_caption(a), _tokens_caption(b)
    if len(ta) < 3 or len(tb) < 3:
        return True
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    return jaccard >= 0.35


def deduplicar_referencias_img(md: str) -> tuple[str, dict]:
    """Conserva una sola asociación principal por imagen y reporta conflictos."""
    vistas: dict[str, str] = {}
    lineas_out: list[str] = []
    reporte = {
        "imagenes_duplicadas_detectadas": 0,
        "duplicados_eliminados": 0,
        "caption_incompatible_advertencias": [],
    }
    for linea in (md or "").splitlines():
        refs = extraer_referencias_img(linea)
        duplicadas = [r for r in refs if r in vistas]
        if not duplicadas:
            caption = _caption_de_linea_img(linea)
            for ref in refs:
                vistas[ref] = caption
            lineas_out.append(linea)
            continue

        reporte["imagenes_duplicadas_detectadas"] += len(duplicadas)
        caption = _caption_de_linea_img(linea)
        for ref in duplicadas:
            previo = vistas.get(ref, "")
            if not _captions_compatibles(previo, caption):
                reporte["caption_incompatible_advertencias"].append(
                    f"{ref}: '{previo[:80]}' vs '{caption[:80]}'")
        reporte["duplicados_eliminados"] += 1
        nuevos = [r for r in refs if r not in vistas]
        if nuevos:
            for ref in nuevos:
                vistas[ref] = caption
            lineas_out.append(linea)

    return "\n".join(lineas_out), reporte


def _sumar_reporte_duplicados(destino: dict, reporte: dict) -> None:
    destino["imagenes_duplicadas_detectadas"] = (
        int(destino.get("imagenes_duplicadas_detectadas", 0)) +
        int(reporte.get("imagenes_duplicadas_detectadas", 0)))
    destino["duplicados_eliminados"] = (
        int(destino.get("duplicados_eliminados", 0)) +
        int(reporte.get("duplicados_eliminados", 0)))
    destino.setdefault("caption_incompatible_advertencias", []).extend(
        reporte.get("caption_incompatible_advertencias", []))


def _quitar_imagenes_crudas(md: str) -> str:
    """El LLM solo puede insertar capturas mediante [[IMG:archivo|idea]]."""
    md = RE_MD_IMG.sub("", md)

    def repl_html(m):
        src = m.group(1).strip()
        if src.startswith(("http://", "https://")):
            return f"[imagen externa]({src})"
        return ""

    return RE_HTML_IMG.sub(repl_html, md)


def _filtrar_imgs_autorizadas(md: str, permitidas: set[str]) -> str:
    def repl_html(m):
        return m.group(0) if _nombre_img_ref(m.group(1)) in permitidas else ""

    return RE_HTML_IMG.sub(repl_html, md)


def entretejer(cuerpo: str, d: Path, unicas: list[dict], origen: str) -> str:
    """Convierte los marcadores [[IMG:...]] en tabla imagen-izquierda/texto-derecha y
    descarta capturas sin explicación útil. Las imágenes no referenciadas quedan para la
    limpieza posterior."""
    por_nombre = {c["archivo"]: c for c in unicas}
    usadas: set[str] = set()
    cuerpo = _quitar_imagenes_crudas(cuerpo)

    def reemplazo(m):
        nombre = m.group(1).strip()
        texto = (m.group(2) or "").strip()
        if len(texto) < 8:
            return ""
        c = por_nombre.get(nombre)
        if c is None or not (d / "img" / nombre).is_file():
            return ""  # marcador inventado o imagen ya limpiada: se descarta
        usadas.add(nombre)
        return "\n" + _visual_card_md(
            c, archivo=nombre, caption=texto, origen=origen,
            parte=c.get("parte", ""), caption_confiable=True,
            permitir_detalle_ocr=False) + "\n"

    tejido = eliminar_otras_pantallas(RE_MARCADOR.sub(reemplazo, cuerpo))
    return _filtrar_imgs_autorizadas(tejido, usadas)


def _solo_marcadores_suplementarios(cuerpo: str,
                                    seleccionadas: list[dict]) -> str:
    """Impide que una tanda visual secundaria replique la leccion del chunk."""
    permitidas = {str(slide.get("archivo", "")) for slide in seleccionadas}
    lineas = []
    vistas: set[str] = set()
    for marker in RE_MARCADOR.finditer(cuerpo or ""):
        archivo = marker.group(1).strip()
        if archivo not in permitidas or archivo in vistas:
            continue
        caption = (marker.group(2) or "Evidencia visual complementaria").strip()
        lineas.append(f"[[IMG:{archivo}|{caption}]]")
        vistas.add(archivo)
    return "\n".join(lineas)


def asegurar_slides_seleccionadas_en_guion(
        cuerpo: str, d: Path, seleccionadas: list[dict], origen,
        parte: int | str = "") -> str:
    """Inserta evidencia seleccionada que el LLM omitio, sin duplicar la leccion."""
    presentes = set(extraer_referencias_img(cuerpo))
    faltantes = [
        slide for slide in seleccionadas
        if str(slide.get("archivo", "")) not in presentes
        and (d / "img" / str(slide.get("archivo", ""))).is_file()
    ]
    if not faltantes:
        return cuerpo

    cards: list[str] = []
    for slide in faltantes:
        archivo = str(slide["archivo"])
        contexto = _caption_visual_determinista(slide)
        if slide.get("sin_transcript_cercano"):
            contexto = (
                "Evidencia visual sin audio cercano verificado; se conserva sin "
                "reconstruir afirmaciones ausentes."
            )
        if not contexto:
            continue
        card = _visual_card_md(
            slide, archivo=archivo, caption=contexto,
            origen=origen, parte=parte, caption_confiable=True,
            permitir_transcript=False, permitir_detalle_ocr=False,
        )
        if card:
            cards.append(card)
    if not cards:
        return cuerpo
    lineas = [
        "### 🖼️ Evidencia visual integrada",
        "",
        "> Estas evidencias complementan la explicación del tramo. Cada tarjeta distingue "
        "lo visible, la idea explicada y su procedencia.",
        "",
    ]
    for card in cards:
        lineas.extend([card, ""])
    return cuerpo.rstrip() + "\n\n" + "\n".join(lineas).strip()


_WEB_RELEVANCE_GENERIC = {
    "official", "research", "documentation", "paper", "course", "class",
    "deep", "learning", "machine", "artificial", "intelligence", "neural",
    "network", "networks", "model", "models",
}


_WEB_OFFICIAL_DOMAINS = {
    "nvidia.com", "pytorch.org", "tensorflow.org", "ibm.com", "google.com",
    "research.google", "microsoft.com", "learn.microsoft.com", "nist.gov",
    "neurips.cc", "openreview.net", "jmlr.org", "acm.org", "ieee.org",
    "nature.com", "science.org", "arxiv.org", "doi.org",
}


def _puntuar_autoridad_web(url: str, consulta: str) -> int:
    """Prioriza material primario/oficial y descarta blogs de autoridad incierta."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return 0
    dominio = parsed.netloc.casefold().split(":", 1)[0]
    if dominio.startswith("www."):
        dominio = dominio[4:]
    ruta = parsed.path.casefold()
    if not dominio:
        return 0
    if dominio in {"academia.edu", "www.academia.edu", "researchgate.net",
                   "www.researchgate.net"}:
        return 0

    normal = _normalizar_busqueda(consulta)

    def es_dominio(valor: str) -> bool:
        return dominio == valor or dominio.endswith(f".{valor}")

    if es_dominio("colab.research.google.com") and "/github/" in ruta:
        partes = ruta.split("/github/", 1)[1].strip("/").split("/", 1)
        if not partes or partes[0] not in {"google", "tensorflow", "pytorch", "nvidia",
                                          "nvdli"}:
            return 0

    reglas_especificas = (
        (("nvidia", " dli "), ("nvidia.com",), 100),
        (("pytorch",), ("pytorch.org",), 100),
        (("ibm", "deep blue"), ("ibm.com",), 100),
        (("google colab", " colab "), ("google.com", "research.google"), 100),
        (("mnist",), ("nist.gov", "yann.lecun.com"), 95),
        (("rosenblatt", "perceptron"), ("cornell.edu",), 95),
        (("neurips",), ("neurips.cc",), 100),
    )
    consulta_espaciada = f" {normal} "
    for conceptos, dominios, puntaje in reglas_especificas:
        if any(c in consulta_espaciada for c in conceptos) and any(
                es_dominio(d) for d in dominios):
            return puntaje

    if es_dominio("github.com"):
        # GitHub solo es fuente oficial cuando pertenece al proyecto u organismo.
        organizacion = ruta.strip("/").split("/", 1)[0]
        if organizacion in {"nvdli", "nvidia", "pytorch", "tensorflow", "google",
                            "microsoft", "ibm"}:
            return 85
        return 0

    if any(es_dominio(dominio_oficial) for dominio_oficial in _WEB_OFFICIAL_DOMAINS):
        return 80
    if dominio.endswith(".gov") or dominio.endswith(".edu"):
        return 70
    if any(fragmento in dominio for fragmento in (
            "springer.com", "sciencedirect.com", "wiley.com", "oup.com",
            "cambridge.org", "mitpress.mit.edu", "proceedings.mlr.press")):
        return 65
    if es_dominio("wikipedia.org"):
        return 10
    return 0


def _concepto_principal_web(consulta: str) -> str:
    normal = _normalizar_busqueda(consulta)
    hallados: list[tuple[int, str]] = []
    for canonico, _peso, _variantes in _WEB_QUERY_CONCEPTS:
        clave = _normalizar_busqueda(canonico)
        posicion = normal.find(clave)
        if posicion >= 0:
            hallados.append((posicion, clave))
    if not hallados:
        return ""
    return min(hallados)[1]


def _tokens_concepto_principal_web(consulta: str) -> set[str]:
    """Extrae los términos distintivos del primer concepto canónico."""
    concepto = _concepto_principal_web(consulta)
    if not concepto:
        return set()
    genericos = _WEB_RELEVANCE_GENERIC | {"function", "computing"}
    return {
        token for token in re.findall(r"[a-z0-9]{3,}", concepto)
        if token not in genericos
    }


def _metadatos_cubren_concepto_principal(consulta: str, metadatos: str) -> bool:
    concepto = _concepto_principal_web(consulta)
    if not concepto:
        return True
    normal = f" {_normalizar_busqueda(metadatos)} "
    grupos_especiales = {
        "nvidia dli": (("nvidia",), (" dli ", "deep learning institute")),
        "google colab": (("google",), ("colab",)),
        "ibm deep blue": (("ibm",), ("deep blue",)),
        "mcculloch pitts neuron": (("mcculloch",), ("pitts",)),
        "frank rosenblatt perceptron": (("rosenblatt",), ("perceptron",)),
        "minsky papert perceptrons": (("minsky",), ("papert",)),
    }
    grupos = grupos_especiales.get(concepto)
    if grupos:
        return all(any(alternativa in normal for alternativa in grupo) for grupo in grupos)
    tokens = _tokens_concepto_principal_web(consulta)
    if not tokens:
        return True
    presentes = tokens & {
        token for token in re.findall(r"[a-z0-9]{3,}", normal)
    }
    requeridos = len(tokens) if len(tokens) <= 2 else 2
    return len(presentes) >= requeridos


def _ideas_web_relevantes(consulta: str, ideas: list, resumen: str = "") -> list[str]:
    """Evita inyectar productos o temas vecinos extraídos de portadas generales."""
    principales = _tokens_concepto_principal_web(consulta)
    query_tokens = {
        token for token in re.findall(
            r"[a-z0-9]{3,}", _normalizar_busqueda(consulta))
        if token not in _WEB_RELEVANCE_GENERIC | {"official", "documentation", "primary",
                                                   "source", "function", "computing"}
    }

    def relevante(texto: str) -> bool:
        tokens = set(re.findall(r"[a-z0-9]{3,}", _normalizar_busqueda(texto)))
        if principales:
            return _metadatos_cubren_concepto_principal(consulta, texto)
        requeridos = min(2, len(query_tokens))
        return bool(requeridos and len(query_tokens & tokens) >= requeridos)

    salida = [str(idea).strip() for idea in ideas if str(idea).strip() and relevante(idea)]
    if not salida and str(resumen or "").strip() and relevante(str(resumen)):
        salida.append(str(resumen).strip())
    return salida[:5]


def _recortar_texto_web(texto: str, max_chars: int = 220) -> str:
    limpio = re.sub(r"\s+", " ", str(texto or "")).strip()
    if len(limpio) <= max_chars:
        return limpio
    recorte = limpio[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return recorte + "…"


def _fuente_web_relevante(consulta: str, resultado: dict, info: dict) -> bool:
    def tokens(valor) -> set[str]:
        normal = _normalizar_busqueda(str(valor or ""))
        return {t for t in re.findall(r"[a-z0-9]{3,}", normal)}

    query_tokens = tokens(consulta)
    metadatos = " ".join([
        str(resultado.get("titulo", "")),
        str(resultado.get("resumen", "")),
        str(resultado.get("url", "")),
        str(info.get("titulo", "")),
        str(info.get("fuente", "")),
    ])
    texto = " ".join([
        metadatos,
        " ".join(str(x) for x in info.get("ideas", []) or []),
    ])
    doc_tokens = tokens(texto)
    if not _metadatos_cubren_concepto_principal(consulta, metadatos):
        return False
    comunes = query_tokens & doc_tokens
    fuertes = query_tokens - _WEB_RELEVANCE_GENERIC
    if fuertes:
        return bool(fuertes & doc_tokens) and (len(comunes) >= 2 or len(fuertes) == 1)
    return len(comunes) >= 2


def fuentes_ampliadas_md(consulta: str, n: int = 3, buscar_fn=None, extraer_fn=None,
                          cache_dir: Path | None = None) -> str:
    """Sección web conservadora y testeable; no rompe si la red o extracción fallan."""
    consulta = consulta.strip()
    if not consulta:
        return ""
    cache_path = None
    if cache_dir is not None and buscar_fn is None and extraer_fn is None:
        cache_dir = Path(cache_dir)
        key = hashlib.sha256(
            f"web_v5_relevant_ideas|{n}|{consulta.casefold()}".encode("utf-8")
        ).hexdigest()
        cache_path = cache_dir / f"{key}.md"
        if cache_path.is_file():
            try:
                return cache_path.read_text(encoding="utf-8")
            except OSError:
                pass
    try:
        if buscar_fn is None or extraer_fn is None:
            from . import web
            buscar_fn = buscar_fn or web.buscar
            extraer_fn = extraer_fn or web.extraer_url
        resultados = buscar_fn(consulta, max(8, n * 4))
    except Exception:
        return ""
    if not resultados:
        return ""
    lineas = ["\n#### Fuentes ampliadas\n"]
    aceptadas = 0
    candidatos = sorted(
        enumerate(resultados),
        key=lambda par: (-_puntuar_autoridad_web(par[1].get("url", ""), consulta),
                         par[0]),
    )
    for _indice, r in candidatos:
        if aceptadas >= n:
            break
        url = (r.get("url") or "").strip()
        if not url or _puntuar_autoridad_web(url, consulta) < 25:
            continue
        try:
            info = extraer_fn(url)
        except Exception:
            info = {"ok": False, "url": url, "error": "extraccion_fallida"}
        if not _fuente_web_relevante(consulta, r, info):
            continue
        aceptadas += 1
        ok = bool(info.get("ok"))
        titulo = r.get("titulo") or info.get("titulo") or "Recurso web"
        titulo = _recortar_texto_web(str(titulo), 140)
        fuente = (info.get("fuente") if ok else "") or re.sub(r"^www\.", "", url.split("/")[2]) \
            if "://" in url else "web"
        lineas.append(f"- **{titulo}** ({fuente})")
        if ok:
            ideas = _ideas_web_relevantes(
                consulta,
                list(info.get("ideas", []) or []),
                str(r.get("resumen", "") or ""),
            )
            lineas.append("  - Ideas clave:")
            for idea in ideas[:5]:
                lineas.append(f"    - {_recortar_texto_web(idea)}")
            if not ideas:
                lineas.append("    - La fuente no devolvió ideas textuales útiles.")
            comp = info.get("complementa") or (
                "Aporta contexto externo para contrastar o ampliar lo explicado en clase."
            )
            lineas.append(
                "  - Cómo complementa/corrige la clase: "
                f"{_recortar_texto_web(str(comp), 260)}")
        else:
            resumen = (r.get("resumen") or "").strip()
            lineas.append("  - Ideas clave:")
            if resumen:
                lineas.append(f"    - {_recortar_texto_web(resumen)}")
            else:
                lineas.append("    - No se pudo extraer contenido de la página.")
            lineas.append("  - Cómo complementa/corrige la clase: revisar como fuente externa; "
                          "no se pudo extraer contenido.")
        lineas.append(f"  - URL: <{url}>")
    if not aceptadas:
        return ""
    salida = "\n".join(lineas).strip() + "\n"
    if cache_path is not None and salida.strip():
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporal = cache_path.with_suffix(".tmp")
            temporal.write_text(salida, encoding="utf-8")
            os.replace(temporal, cache_path)
        except OSError:
            pass
    return salida


def fuentes_ampliadas_consultas(
        consultas: list[str], max_consultas: int | None = None,
        cache_dir: Path | None = None) -> str:
    max_consultas = (
        _post_web_max_consultas() if max_consultas is None else max(0, int(max_consultas)))
    if max_consultas <= 0:
        return ""
    vistas: set[str] = set()
    piezas = []
    for q in consultas:
        q = re.sub(r"\s+", " ", str(q).strip())[:120]
        key = q.lower()
        if not q or key in vistas:
            continue
        vistas.add(key)
        try:
            md = fuentes_ampliadas_md(q, cache_dir=cache_dir)
        except TypeError:
            md = fuentes_ampliadas_md(q)
        if md:
            piezas.append(md.replace("#### Fuentes ampliadas", f"### {q}", 1))
        if len(piezas) >= max_consultas:
            break
    if not piezas:
        return ""
    return "## Fuentes ampliadas\n\n" + "\n\n".join(piezas).strip() + "\n"



def imagenes_unicas(d: Path, t_ini: float, t_fin: float) -> list[dict]:
    """Capturas del rango sin duplicados (dhash). No toca los archivos originales."""
    return _dedup_capturas(d, capturas_rango(d, t_ini, t_fin))


def capturas_unicas(d: Path, t_ini: float, t_fin: float) -> list[dict]:
    """Alias explicito para auditoria/compatibilidad: dedup visual por dhash."""
    return imagenes_unicas(d, t_ini, t_fin)


def _linea_segmento(s: dict, origen) -> str:
    role = (
        "[INTERVENCIÓN DEL ESTUDIANTE] "
        if s.get("source_role") == "student_local_intervention" else ""
    )
    return f"[{fmt_t(_seg_t(s), origen)}] {role}{str(s.get('texto', '')).strip()}"


def _lineas_segmentos_compactas(segs: list[dict], origen,
                                gap_seg: float = 4.0,
                                max_chars: int = 900) -> list[str]:
    """Agrupa microsegmentos Whisper en parrafos sin perder texto ni tiempo inicial."""
    lineas: list[str] = []
    actual: list[str] = []
    inicio_actual: float | None = None
    fin_previo: float | None = None
    chars_actual = 0

    def cerrar() -> None:
        nonlocal actual, inicio_actual, chars_actual
        if not actual or inicio_actual is None:
            actual = []
            inicio_actual = None
            chars_actual = 0
            return
        texto = re.sub(r"\s+", " ", " ".join(actual)).strip()
        if texto:
            lineas.append(f"[{fmt_t(inicio_actual, origen)}] {texto}")
        actual = []
        inicio_actual = None
        chars_actual = 0

    for s in _segmentos_ordenados(segs, origen):
        texto = _texto_segmento_limpio(str(s.get("texto", "")))
        if texto and s.get("source_role") == "student_local_intervention":
            texto = "[INTERVENCIÓN DEL ESTUDIANTE] " + texto
        if not texto:
            continue
        ini = _seg_t(s)
        fin = _seg_fin(s)
        hay_gap = fin_previo is not None and ini - fin_previo > gap_seg
        muy_largo = chars_actual and chars_actual + len(texto) + 1 > max_chars
        if actual and (hay_gap or muy_largo):
            cerrar()
        if inicio_actual is None:
            inicio_actual = ini
        actual.append(texto)
        chars_actual += len(texto) + 1
        fin_previo = fin
    cerrar()
    return lineas


def dividir_transcript_chunks(segs: list[dict], origen, min_seg: int | None = None,
                              max_seg: int | None = None,
                              max_chars: int | None = None) -> list[dict]:
    """Divide transcript en chunks temporales, sin cortar dentro de un segmento."""
    min_seg = POST_CHUNK_MIN_SEG if min_seg is None else int(min_seg)
    max_seg = POST_CHUNK_MAX_SEG if max_seg is None else int(max_seg)
    max_chars = POST_CHUNK_MAX_CHARS if max_chars is None else int(max_chars)
    ordenados = _segmentos_ordenados(segs, origen)
    chunks: list[dict] = []
    actual: list[dict] = []
    lineas: list[str] = []
    chars = 0
    t_ini = None

    def cerrar():
        nonlocal actual, lineas, chars, t_ini
        if not actual:
            return
        t_fin = max(_seg_fin(s) for s in actual)
        chunks.append({
            "idx": len(chunks) + 1,
            "t_ini": t_ini if t_ini is not None else _seg_t(actual[0]),
            "t_fin": t_fin,
            "segs": actual,
            "texto": "\n".join(lineas),
            "chars": chars,
        })
        actual, lineas, chars, t_ini = [], [], 0, None

    for s in ordenados:
        linea = _linea_segmento(s, origen)
        inicio_s = _seg_t(s)
        fin_s = _seg_fin(s)
        if actual and t_ini is not None:
            dur = max(0.0, fin_s - t_ini)
            muy_denso = chars + len(linea) > max_chars and chars >= int(max_chars * 1.25)
            debe_cortar = ((chars + len(linea) > max_chars and dur >= min_seg)
                           or muy_denso or dur > max_seg)
            if debe_cortar:
                cerrar()
        if t_ini is None:
            t_ini = inicio_s
        actual.append(s)
        lineas.append(linea)
        chars += len(linea)
        if t_ini is not None and max(0.0, fin_s - t_ini) >= max_seg:
            cerrar()
    cerrar()
    return chunks


def _ventanas_transcript_tema(segs: list[dict], ventana_seg: int = 180) -> list[dict]:
    ordenados = sorted(segs, key=lambda s: _to_float(s.get("t", 0)))
    if not ordenados:
        return []
    ventanas: list[dict] = []
    actual: list[dict] = []
    ini = _to_float(ordenados[0].get("t", 0))
    for s in ordenados:
        t = _to_float(s.get("t", 0))
        if actual and t - ini >= ventana_seg:
            ventanas.append({"inicio": ini, "fin": _to_float(actual[-1].get("fin", actual[-1].get("t", ini))),
                             "segs": actual})
            actual = []
            ini = t
        actual.append(s)
    if actual:
        ventanas.append({"inicio": ini, "fin": _to_float(actual[-1].get("fin", actual[-1].get("t", ini))),
                         "segs": actual})
    return ventanas


def _clasificar_ventana_tema(txt: str, tokens: set[str], prev_tokens: set[str],
                             tema_tokens: set[str], duracion: float) -> tuple[str, list[str]]:
    normal = _normalizar_busqueda(txt)
    razones: list[str] = []
    sim_prev = _sim_jaccard(tokens, prev_tokens)
    sim_tema = _sim_jaccard(tokens, tema_tokens)
    inicio_clase = any(p in normal for p in SENALES_INICIO_CLASE)
    inicio_fuerte = any(p in normal for p in SENALES_NUEVA_CLASE_FUERTES)
    publicidad = any(p in normal for p in TERMINOS_PUBLICIDAD)
    interferencia = any(p in normal for p in TERMINOS_INTERFERENCIA)
    idioma = _idioma_por_texto(txt)
    if publicidad and sim_tema < 0.10 and duracion <= 240:
        return "publicidad_ruido", ["lenguaje_comercial"]
    if interferencia and sim_tema < 0.12:
        return "interferencia_audio", ["vocabulario_ajeno"]
    if inicio_clase:
        razones.append("frase_inicio_clase")
    if prev_tokens and sim_prev < 0.12:
        razones.append("cambio_vocabulario")
    if tema_tokens and sim_tema < 0.08:
        razones.append("desalineado_tema")
    if idioma == "en":
        razones.append("idioma_en")
    if duracion >= 120 and inicio_fuerte and ("cambio_vocabulario" in razones or
                                              "desalineado_tema" in razones):
        return "otra_clase_legitima", razones
    if duracion <= 150 and ("cambio_vocabulario" in razones or "desalineado_tema" in razones):
        return "digresion_del_profesor", razones
    return "principal", razones or ["continua_tema"]


def _fusionar_segmentos_tema(segmentos: list[dict]) -> list[dict]:
    fusionados: list[dict] = []
    for seg in segmentos:
        if fusionados and fusionados[-1]["clasificacion"] == seg["clasificacion"]:
            prev = fusionados[-1]
            prev["fin"] = seg["fin"]
            prev["duracion_seg"] = max(0.0, prev["fin"] - prev["inicio"])
            prev["segs"].extend(seg["segs"])
            prev["razones"] = sorted(set(prev.get("razones", []) + seg.get("razones", [])))
            prev["texto"] = _texto_segs(prev["segs"])
        else:
            fusionados.append(dict(seg))
    return fusionados


def segmentar_transcript_por_tema(transcript: list[dict], img_log: list[dict] | None = None,
                                  curso: str = "", tema: str = "") -> list[dict]:
    """Segmentación local conservadora por clase/tema/expositor sin depender del LLM."""
    ventanas = _ventanas_transcript_tema(transcript, ventana_seg=180)
    tema_tokens = _tokens_relevantes(f"{curso} {tema}")
    segmentos: list[dict] = []
    prev_tokens: set[str] = set()
    for idx, v in enumerate(ventanas, 1):
        txt = _texto_segs(v["segs"])
        tokens = _tokens_relevantes(txt)
        duracion = max(0.0, _to_float(v["fin"]) - _to_float(v["inicio"]))
        clasif, razones = _clasificar_ventana_tema(
            txt, tokens, prev_tokens, tema_tokens, duracion)
        if not tokens and img_log:
            clasif, razones = "silencio_visual", ["capturas_sin_transcript"]
        segmentos.append({
            "idx": idx,
            "inicio": v["inicio"],
            "fin": v["fin"],
            "duracion_seg": duracion,
            "clasificacion": clasif,
            "razones": razones,
            "idioma": _idioma_por_texto(txt),
            "tokens_top": sorted(tokens)[:24],
            "segs": list(v["segs"]),
            "texto": txt,
        })
        if tokens:
            prev_tokens = tokens
    fusionados = _fusionar_segmentos_tema(segmentos)
    segmentar_transcript_por_tema.ultimo_reporte = {
        "segmentos": len(fusionados),
        "otra_clase_legitima": sum(1 for s in fusionados
                                    if s["clasificacion"] == "otra_clase_legitima"),
        "publicidad_ruido": sum(1 for s in fusionados
                                 if s["clasificacion"] == "publicidad_ruido"),
        "interferencia_audio": sum(1 for s in fusionados
                                    if s["clasificacion"] == "interferencia_audio"),
    }
    return fusionados


def _slug_corto(txt: str, defecto: str = "tema") -> str:
    base = est._slug(txt)[:60].strip("-")
    return base or defecto


def _titulo_desde_segmento(seg: dict) -> str:
    texto = _normalizar_busqueda(str(seg.get("texto", "")))[:1200]
    palabras = [
        w for w in re.findall(r"[a-z0-9][a-z0-9_\-]{2,}", texto)
        if w not in STOPWORDS_TEMA and not w.isdigit()
    ]
    vistas: set[str] = set()
    claves: list[str] = []
    for w in palabras:
        if w not in vistas:
            claves.append(w)
            vistas.add(w)
        if len(claves) >= 6:
            break
    return " ".join(claves) or "otra clase"


def _material_suficiente_leccion_extra(seg: dict) -> bool:
    texto = str(seg.get("texto", "") or "")
    dur = _to_float(seg.get("duracion_seg", 0))
    n_segs = len(seg.get("segs", []) or [])
    return dur >= 150 or n_segs >= 4 or len(texto) >= 900


def generar_lecciones_extra_detectadas(d: Path, segmentos: list[dict], origen) -> list[Path]:
    extras = [s for s in segmentos if s.get("clasificacion") == "otra_clase_legitima"]
    generar_lecciones_extra_detectadas.ultimo_reporte = {
        "separadas": 0,
        "insuficientes": [],
        "keys_separadas": set(),
    }
    if not extras:
        return []
    out_dir = d / "lecciones_detectadas"
    out_dir.mkdir(exist_ok=True)
    creados: list[Path] = []
    keys_separadas: set[tuple[str, str]] = set()
    insuficientes: list[dict] = []
    for idx, seg in enumerate(extras, 1):
        titulo = _titulo_desde_segmento(seg)
        if not _material_suficiente_leccion_extra(seg):
            insuficientes.append({
                "titulo": titulo,
                "duracion_seg": seg.get("duracion_seg", 0),
                "segmentos": len(seg.get("segs", []) or []),
            })
            continue
        destino = out_dir / f"{idx:02d}_leccion_{_slug_corto(titulo)}.md"
        segs_extra = _segmentos_ordenados(seg.get("segs", []), origen)
        slides_extra = construir_timeline_slides(
            d, seg["inicio"], seg["fin"], segs_extra, origen)
        slides_extra = seleccionar_slides_por_tema(
            slides_extra, segs_extra, tema_actual=titulo,
            curso=est.curso_de(d), parte=f"extra-{idx}")
        # Una sola tanda recibe LLM. El resto se integra de forma determinista para
        # conservar evidencia protegida sin multiplicar costo ni contexto.
        slides_prompt = _slide_batches(slides_extra)[0]
        fichas = fichas_slides(slides_prompt, origen)
        transcript = "\n".join(_linea_segmento(s, origen) for s in segs_extra)
        cuerpo = preguntar_cacheado(
            d,
            [{"role": "system", "content": PROMPT_SLIDE_GUIDED},
             {"role": "user", "content": (
                 f"LECCION ADICIONAL DETECTADA: {titulo}\n"
                 f"RANGO: {fmt_t(seg['inicio'], origen)} -> {fmt_t(seg['fin'], origen)}\n\n"
                 f"TRANSCRIPCION:\n{transcript}\n\nSLIDES:\n{fichas}")}],
            max_tokens=POST_MAX_TOKENS,
            etiqueta=f"post-extra-{idx:02d}",
            prompt_version=f"{POST_VERSION}:leccion-extra")
        if _respuesta_vacia(cuerpo):
            cuerpo = _fallback_slide_guided_md(slides_prompt, segs_extra, origen)
        else:
            cuerpo, _consultas = _extraer_busquedas(str(cuerpo))
        cuerpo = entretejer(str(cuerpo).strip(), d, slides_prompt, origen)
        cuerpo = asegurar_slides_seleccionadas_en_guion(
            cuerpo, d, slides_extra, origen, parte=f"extra-{idx}")
        cuerpo, _reporte_dup = deduplicar_referencias_img(cuerpo)
        lineas = [
            f"# Lección adicional {idx:02d}: {titulo}",
            "",
            f"> Rango detectado: {fmt_t(seg['inicio'], origen)} -> "
            f"{fmt_t(seg['fin'], origen)}.",
            f"> Clasificación local: {seg['clasificacion']} "
            f"({', '.join(seg.get('razones', [])) or 'cambio sostenido de tema'}).",
            "",
            eliminar_otras_pantallas(cuerpo).strip(),
            "",
            "---",
            "",
            "## Transcripción base preservada",
            "",
            transcript,
        ]
        destino.write_text("\n".join(lineas).strip() + "\n", encoding="utf-8")
        creados.append(destino)
        for s in segs_extra:
            keys_separadas.add(_segment_key(s))
    generar_lecciones_extra_detectadas.ultimo_reporte = {
        "separadas": len(creados),
        "insuficientes": insuficientes,
        "keys_separadas": keys_separadas,
    }
    return creados


def _reporte_segmentacion_md(segmentos: list[dict], extras: list[Path]) -> str:
    rep_extra = getattr(generar_lecciones_extra_detectadas, "ultimo_reporte", {})
    insuf = rep_extra.get("insuficientes", []) or []
    lineas = [
        "## Reporte de segmentación local",
        "",
        f"- Segmentos temáticos detectados: {len(segmentos)}.",
        f"- Lecciones extra separadas: {len(extras)}.",
    ]
    for seg in segmentos[:12]:
        lineas.append(
            f"- Segmento {seg.get('idx')}: {seg.get('clasificacion')} "
            f"({fmt_duracion(seg.get('duracion_seg', 0))}; "
            f"{', '.join(seg.get('razones', [])) or 'sin señales'}).")
    for p in extras:
        lineas.append(f"- Archivo separado: `{p.relative_to(p.parent.parent)}`.")
    if extras:
        lineas.append("- El contenido separado no se mezcla en la lección principal.")
    if insuf:
        lineas.append("- Cambios de tema sin material suficiente para archivo separado:")
        for item in insuf[:8]:
            lineas.append(
                f"  - {item.get('titulo', 'tema adicional')} "
                f"({fmt_duracion(item.get('duracion_seg', 0))}, "
                f"{item.get('segmentos', 0)} segmentos): integrado como tema adicional.")
    return "\n".join(lineas).strip() + "\n"


def _segment_key(s: dict) -> tuple[str, str]:
    return (str(s.get("t", s.get("inicio", ""))), str(s.get("texto", ""))[:120])


def trocear(segs: list[dict], origen: str) -> list[tuple[str, float, float, list[dict]]]:
    """Compatibilidad: devuelve los chunks nuevos en el formato historico."""
    return [(c["texto"], c["t_ini"], c["t_fin"], c["segs"])
            for c in dividir_transcript_chunks(segs, origen)]


def _debug_chunks_dir(d: Path) -> Path:
    p = d / "_debug" / "chunks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _extraer_busquedas(md: str) -> tuple[str, list[str]]:
    consultas: list[str] = []

    def repl(m):
        consultas.append(m.group(1).strip()[:120])
        return ""

    def repl_bloque(m):
        raw_query = str(m.group("query") or "").strip()
        terminos = re.findall(r"`([^`\r\n]+)`", raw_query)
        consulta = (
            " ".join(terminos)
            if terminos
            else re.sub(r"^[\s>*_-]+|[\s>*_-]+$", "", raw_query)
        ).strip()[:120]
        if consulta:
            consultas.append(consulta)
        return ""

    limpio = re.sub(
        r"(?im)^#{1,6}\s*(?:[^\w\r\n]+\s*)?BUSCAR\s*:?\s*$\r?\n"
        r"(?:[ \t]*\r?\n)*(?P<query>[ \t]*(?!#)[^\r\n]+)(?:\r?\n|$)",
        repl_bloque,
        str(md or ""),
    )
    limpio = RE_BUSCAR.sub(repl, limpio).strip()
    return limpio, [q for q in consultas if q]


def _reporte_cobertura_textual(final_txt: str, segs: list[dict]) -> dict:
    trans = _texto_segs(segs)
    visible = re.sub(r"<[^>]+>", " ", final_txt)
    visible = re.sub(r"\s+", " ", visible).strip()
    ratio = len(visible) / max(1, len(trans))
    return {
        "transcript_chars": len(trans),
        "leccion_chars": len(visible),
        "ratio": round(ratio, 3),
        "demasiado_corta": _leccion_demasiado_corta(final_txt, segs),
    }


MULETILLAS_RE = re.compile(
    r"\b(?:um+|uh+|eh+|este+|okay|ok|right|you know|i mean)\b[,\s]*", re.I)
IMPORTANTE_RE = re.compile(
    r"(\b\d{4}\b|[=∑ΣΔ∂→<-]|[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)")


def _texto_segmento_limpio(txt: str) -> str:
    txt = re.sub(r"\s+", " ", txt or "").strip()
    if len(txt) <= 12:
        return txt
    limpio = MULETILLAS_RE.sub("", txt)
    limpio = re.sub(r"\s+([,.;:])", r"\1", limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio or txt


def _segmento_importante(txt: str) -> bool:
    if IMPORTANTE_RE.search(txt or ""):
        return True
    low = (txt or "").lower()
    claves = ("pregunta", "tarea", "deadline", "entrega", "formula", "teorema",
              "definicion", "definición", "paper", "dataset", "profesor", "profesora")
    return any(k in low for k in claves)


def optimizar_transcript_llm(segs: list[dict], origen) -> dict:
    """Reduce repeticiones antes de DeepSeek sin borrar contenido tecnico."""
    ordenados = _segmentos_ordenados(segs, origen)
    raw_lineas = [_linea_segmento(s, origen) for s in ordenados]
    segs_enviados: list[dict] = []
    recientes: list[str] = []
    dedup = 0
    for s in ordenados:
        texto = _texto_segmento_limpio(str(s.get("texto", "")))
        if not texto:
            continue
        normal = _normalizar_busqueda(texto)
        duplicado = normal in recientes
        if not duplicado:
            for prev in recientes[-8:]:
                if difflib.SequenceMatcher(None, normal, prev).ratio() >= 0.94:
                    duplicado = True
                    break
        if duplicado:
            dedup += 1
            continue
        recientes.append(normal)
        if len(recientes) > 24:
            recientes = recientes[-24:]
        limpio = dict(s)
        limpio["texto"] = texto
        segs_enviados.append(limpio)
    enviadas = _lineas_segmentos_compactas(segs_enviados, origen)
    if not enviadas and raw_lineas:
        enviadas = raw_lineas
    bruto = "\n".join(raw_lineas)
    enviado = "\n".join(enviadas)
    ahorro = 0.0 if not bruto else max(0.0, 100.0 * (1.0 - len(enviado) / max(1, len(bruto))))
    return {
        "texto": enviado,
        "chars_brutos": len(bruto),
        "chars_enviados": len(enviado),
        "porcentaje_ahorro": round(ahorro, 2),
        "segmentos_brutos": len(ordenados),
        "segmentos_enviados": len(segs_enviados),
        "lineas_enviadas": len(enviadas),
        "segmentos_deduplicados": dedup,
    }


def analizar_transcript_sospechoso(segs: list[dict]) -> dict:
    por_chunk: dict[str, list[dict]] = {}
    for s in segs:
        chunk = str(s.get("chunk") or "SIN_CHUNK")
        por_chunk.setdefault(chunk, []).append(s)
    chunks = []
    total_clase_n = 0
    total_repetitivos = 0
    for chunk, items in sorted(por_chunk.items()):
        diag = diagnosticar_repeticion_lineas(items, incluir_repeticion_interna=False)
        total_clase_n += int(diag.get("clase_n", 0))
        total_repetitivos += int(diag.get("repetitivos", 0))
        if diag.get("sospechoso"):
            chunks.append({
                "chunk": chunk,
                "segmentos": len(items),
                "clase_n": diag.get("clase_n", 0),
                "repetitivos": diag.get("repetitivos", 0),
                "razones": diag.get("razones", []),
                "ejemplos": [str(x.get("texto", "")) for x in items[:6]],
            })
    return {
        "sospechoso": bool(chunks),
        "chunks": chunks,
        "chunks_total": len(por_chunk),
        "chunks_sospechosos": len(chunks),
        "segmentos_total": len(segs),
        "clase_n_total": total_clase_n,
        "repetitivos_total": total_repetitivos,
    }


def filtrar_transcript_sospechoso(segs: list[dict]) -> list[dict]:
    por_chunk: dict[str, list[dict]] = {}
    orden_chunks: list[str] = []
    for s in segs:
        chunk = str(s.get("chunk") or "SIN_CHUNK")
        if chunk not in por_chunk:
            orden_chunks.append(chunk)
        por_chunk.setdefault(chunk, []).append(s)
    filtrados: list[dict] = []
    for chunk in orden_chunks:
        items = por_chunk[chunk]
        diag = diagnosticar_repeticion_lineas(items, incluir_repeticion_interna=False)
        if diag.get("sospechoso"):
            filtrados.extend(filtrar_lineas_repetitivas(items))
        else:
            filtrados.extend(items)
    return filtrados


def _guardar_transcript_sospechoso(d: Path, reporte: dict, segs: list[dict]) -> None:
    if not reporte.get("sospechoso"):
        return
    dbg = d / "_debug"
    dbg.mkdir(exist_ok=True)
    lineas = [
        "# Transcript sospechoso por bucles Whisper",
        "",
        f"- Segmentos totales: {reporte.get('segmentos_total', 0)}",
        f"- Chunks sospechosos: {reporte.get('chunks_sospechosos', 0)} / "
        f"{reporte.get('chunks_total', 0)}",
        f"- Segmentos `(El )Clase N`: {reporte.get('clase_n_total', 0)}",
        "",
        "| chunk | segmentos | Clase N | repetitivos | razones |",
        "|---|---:|---:|---:|---|",
    ]
    sospechosos = {c["chunk"] for c in reporte.get("chunks", [])}
    for c in reporte.get("chunks", []):
        lineas.append(
            f"| `{c['chunk']}` | {c['segmentos']} | {c['clase_n']} | "
            f"{c['repetitivos']} | {', '.join(c.get('razones', []))} |")
    lineas.append("")
    lineas.append("## Segmentos separados")
    for s in segs:
        if str(s.get("chunk") or "SIN_CHUNK") not in sospechosos:
            continue
        lineas.append("")
        lineas.append(f"### `{s.get('chunk', 'SIN_CHUNK')}` · {s.get('t', '')}")
        lineas.append(str(s.get("texto", "")))
    (dbg / "transcript_sospechoso.md").write_text(
        "\n".join(lineas).strip() + "\n", encoding="utf-8")


def _reporte_transcript_sospechoso_md(reporte: dict) -> str:
    if not reporte.get("sospechoso"):
        return ""
    lineas = [
        "## Transcript sospechoso filtrado",
        "",
        f"- Chunks sospechosos: {reporte.get('chunks_sospechosos', 0)} / "
        f"{reporte.get('chunks_total', 0)}.",
        f"- Segmentos `(El )Clase N`: {reporte.get('clase_n_total', 0)}.",
        "- Acción: los segmentos repetitivos se separaron en "
        "`_debug/transcript_sospechoso.md` y no se usaron como evidencia principal.",
    ]
    for c in reporte.get("chunks", [])[:8]:
        lineas.append(
            f"- `{c['chunk']}`: {c['clase_n']} Clase N; "
            f"razones={', '.join(c.get('razones', []))}.")
    return "\n".join(lineas).strip() + "\n"


def reducir_ocr_slide(slide, max_chars: int = 600,
                      transcript_ventana: str = "") -> dict:
    """Fichas compactas: titulo, bullets distintivos, formulas, URLs y tareas."""
    if isinstance(slide, dict):
        ocr = str(slide.get("ocr_principal") or slide.get("ocr") or "")
        transcript_ventana = transcript_ventana or str(slide.get("transcript_ventana") or "")
    else:
        ocr = str(slide or "")
    reducido, bruto, enviado = _lineas_ocr_distintivas(
        ocr, transcript_ventana, max_chars=max_chars)
    return {
        "texto": reducido,
        "chars_brutos": bruto,
        "chars_enviados": enviado,
        "porcentaje_ahorro": round(
            0.0 if bruto == 0 else max(0.0, 100.0 * (1.0 - enviado / bruto)), 2),
    }


def cache_llm_key(prompt_version: str, transcript_filtrado: str,
                  fichas: str = "", modelo: str | None = None) -> str:
    payload = {
        "prompt_version": prompt_version,
        "modelo": modelo or config.DEEPSEEK_MODEL,
        "transcript_sha256": hashlib.sha256(
            (transcript_filtrado or "").encode("utf-8")).hexdigest(),
        "fichas_sha256": hashlib.sha256((fichas or "").encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _cache_stats_reset() -> None:
    preguntar_cacheado.stats = {
        "cache_hits": 0,
        "cache_misses": 0,
        "llamadas_reales": 0,
        "llamadas_cacheables": 0,
        "llm_omitidas": 0,
    }


def _cache_stats_note(hit: bool, cache_on: bool) -> None:
    stats = getattr(preguntar_cacheado, "stats", None)
    if stats is None:
        _cache_stats_reset()
        stats = preguntar_cacheado.stats
    stats["llamadas_cacheables"] += 1
    if cache_on and hit:
        stats["cache_hits"] += 1
    else:
        stats["cache_misses"] += 1
        stats["llamadas_reales"] += 1


def cache_llm_stats() -> dict:
    stats = getattr(preguntar_cacheado, "stats", None)
    if stats is None:
        _cache_stats_reset()
        stats = preguntar_cacheado.stats
    return dict(stats)


def _post_llm_enabled() -> bool:
    return os.environ.get("CLASE_POST_LLM", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def preguntar_cacheado(d: Path, msgs: list[dict], max_tokens: int,
                       etiqueta: str, prompt_version: str) -> str | None:
    """Cache local opcional para reprocesar la misma tanda sin nueva llamada."""
    if not _post_llm_enabled():
        stats = getattr(preguntar_cacheado, "stats", None)
        if stats is None:
            _cache_stats_reset()
            stats = preguntar_cacheado.stats
        stats["llamadas_cacheables"] += 1
        stats["llm_omitidas"] += 1
        preguntar_cacheado.ultimo_reporte = {
            "cache": "bypass", "hit": False, "llm": "disabled",
        }
        return None
    cache_config = os.environ.get("CLASE_POST_CACHE", "1").strip().lower()
    if cache_config in {"0", "false", "no", "off"}:
        preguntar_cacheado.ultimo_reporte = {"cache": "off", "hit": False}
        _cache_stats_note(hit=False, cache_on=False)
        return preguntar(msgs, max_tokens=max_tokens, etiqueta=etiqueta)
    contenido = "\n\n".join(str(m.get("content", "")) for m in msgs)
    key = cache_llm_key(prompt_version, contenido)
    cache_dir = d / "_debug" / "cache_llm"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}.md"
    if path.is_file():
        preguntar_cacheado.ultimo_reporte = {"cache": "on", "hit": True, "key": key}
        _cache_stats_note(hit=True, cache_on=True)
        return path.read_text(encoding="utf-8")
    r = preguntar(msgs, max_tokens=max_tokens, etiqueta=etiqueta)
    if not _respuesta_vacia(r):
        path.write_text(str(r), encoding="utf-8")
    preguntar_cacheado.ultimo_reporte = {"cache": "on", "hit": False, "key": key}
    _cache_stats_note(hit=False, cache_on=True)
    return r


_RE_RESUMEN_EJECUTIVO_HEADING = re.compile(
    r"(?im)^##[ \t]+[^\n]*?\bResumen[ \t]+ejecutivo"
    r"(?:[ \t]+de[ \t]+clase)?[ \t]*$"
)


def _extraer_resumen_ejecutivo(markdown: str) -> str:
    txt = str(markdown or "")
    heading = _RE_RESUMEN_EJECUTIVO_HEADING.search(txt)
    if not heading:
        return ""
    resto = txt[heading.end():]
    fin = re.search(r"(?m)^\s*---\s*$|^#{1,2}[ \t]+\S", resto)
    return resto[:fin.start() if fin else None].strip()


def contexto_previo_compacto(d: Path, max_chars: int = 8000) -> dict:
    final = d / "leccion_final.md"
    if not final.is_file():
        return {"disponible": False, "texto": "", "resumen": "",
                "chars_originales": 0,
                "chars_enviados": 0, "porcentaje_ahorro": 0.0}
    txt = study_package.strip_metadata(final.read_text(encoding="utf-8"))
    resumen = _extraer_resumen_ejecutivo(txt)
    headings = "\n".join(re.findall(r"^#{1,3}\s+.+$", txt, flags=re.M)[:60])
    cola = txt[-max(1200, max_chars // 3):]
    piezas = [
        "CONTEXTO PREVIO CONSOLIDADO (compacto; no es transcript crudo):",
        "",
        "### Resumen previo",
        resumen or "(sin resumen ejecutivo extraible)",
        "",
        "### Indice previo",
        headings or "(sin indice extraible)",
        "",
        "### Ultimo tramo previo",
        cola.strip(),
    ]
    compacto = "\n".join(piezas).strip()
    if len(compacto) > max_chars:
        compacto = compacto[:max_chars].rsplit("\n", 1)[0].rstrip()
    if len(compacto) > len(txt):
        compacto = txt.strip()
    ahorro = max(0.0, 100.0 * (1.0 - len(compacto) / max(1, len(txt))))
    return {
        "disponible": True,
        "fuente": "leccion_final.md",
        "texto": compacto,
        "resumen": resumen,
        "texto_completo": txt,
        "chars_originales": len(txt),
        "chars_enviados": len(compacto),
        "porcentaje_ahorro": round(ahorro, 2),
    }


def _limpiar_resumen_tramo(txt: str, max_chars: int = 1600) -> str:
    txt = RE_MD_IMG.sub("", str(txt or ""))
    txt = RE_HTML_IMG.sub("", txt)
    txt = RE_BUSCAR.sub("", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars].rsplit("\n", 1)[0].rstrip()
    return txt


def resumenes_tramos_rango(d: Path, t_ini: float, t_fin: float, origen,
                           max_chars: int = 7000) -> str:
    registros = est.leer_jsonl(d / "resumen_tramos.jsonl")
    if not registros:
        return ""
    a = _normalizar_valor_tiempo(t_ini, origen)
    b = _normalizar_valor_tiempo(t_fin, origen)
    lineas = [
        "RESUMENES EN VIVO DEL RANGO (guia local previa, usar para estructura; "
        "verificar contra transcript y slides):",
        "",
    ]
    total = 0
    for r in registros:
        ri = _normalizar_valor_tiempo(r.get("t_ini", 0), origen)
        rf = _normalizar_valor_tiempo(r.get("t_fin", ri), origen)
        if rf < a or ri > b:
            continue
        cuerpo = _limpiar_resumen_tramo(str(r.get("cuerpo", "") or ""))
        if not cuerpo:
            continue
        capturas = ", ".join(str(x) for x in (r.get("imagenes") or [])[:5])
        meta = f"- {r.get('rango', fmt_t(ri, origen) + '-' + fmt_t(rf, origen))}"
        meta += f" | estado={r.get('estado', '')}"
        if capturas:
            meta += f" | imagenes={capturas}"
        bloque = meta + "\n" + cuerpo + "\n"
        if total + len(bloque) > max_chars:
            break
        lineas.append(bloque)
        total += len(bloque)
    return "\n".join(lineas).strip() if total else ""


def crear_snapshot_previo(d: Path, timestamp: str | None = None) -> list[Path]:
    ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
    creados: list[Path] = []
    for nombre in ("leccion_final.md", "clase_manifest.json"):
        p = d / nombre
        if not p.is_file():
            continue
        sufijo = "md" if p.suffix.lower() == ".md" else "json"
        destino = d / f"{p.stem}_PREV_{ts}.{sufijo}"
        shutil.copy2(p, destino)
        creados.append(destino)
    return creados


def segmentos_nuevos_desde_final(d: Path, segs: list[dict], origen,
                                 final_mtime: float | None = None) -> list[dict]:
    final = d / "leccion_final.md"
    if not final.is_file():
        return []
    umbral = final_mtime if final_mtime is not None else final.stat().st_mtime
    nuevos = []
    for s in segs:
        raw_t = _to_float(s.get("t", s.get("inicio", s.get("start", s.get("ini", 0)))))
        chunk = str(s.get("chunk", "") or "")
        wav = d / "audio" / chunk if chunk else None
        es_nuevo = raw_t >= 1_000_000_000 and raw_t > umbral
        if wav is not None and wav.is_file() and wav.stat().st_mtime > umbral:
            es_nuevo = True
        if es_nuevo:
            nuevos.append(s)
    return _segmentos_ordenados(nuevos, origen)


def capturas_nuevas_desde_final(
        d: Path, capturas: list[dict],
        final_mtime: float | None = None) -> list[dict]:
    final = d / "leccion_final.md"
    if not final.is_file():
        return []
    umbral = final_mtime if final_mtime is not None else final.stat().st_mtime
    nuevas: list[dict] = []
    for captura in capturas:
        raw_t = _to_float(captura.get("t"))
        archivo = str(
            captura.get("ref_archivo") or captura.get("archivo") or "")
        imagen = d / "img" / archivo if archivo else None
        es_nueva = raw_t >= 1_000_000_000 and raw_t > umbral
        if imagen is not None and imagen.is_file() and imagen.stat().st_mtime > umbral:
            es_nueva = True
        if es_nueva:
            nuevas.append(captura)
    return nuevas


def _partes_con_evidencia(
        evidencias: list[dict],
        rangos: list[tuple[int, float, float]],
        origen) -> set[int]:
    partes: set[int] = set()
    sin_parte: list[dict] = []
    for evidencia in evidencias:
        try:
            parte = int(evidencia.get("parte", 0) or 0)
        except (TypeError, ValueError):
            parte = 0
        if parte > 0:
            partes.add(parte)
        else:
            sin_parte.append(evidencia)
    for evidencia in sin_parte:
        raw = evidencia.get(
            "t", evidencia.get("inicio", evidencia.get("start",
            evidencia.get("ini", evidencia.get("t_rel", 0.0)))))
        t_rel = _normalizar_valor_tiempo(raw, origen)
        for parte, inicio, fin in rangos:
            if _tiempo_en_rango(
                    t_rel,
                    _normalizar_valor_tiempo(inicio, origen),
                    _normalizar_valor_tiempo(fin, origen)):
                partes.add(parte)
                break
    return partes


def _quitar_titulo_principal(md: str) -> str:
    lineas = md.splitlines()
    if lineas and lineas[0].startswith("# "):
        return "\n".join(lineas[1:]).lstrip()
    return md.strip()


def _cuerpo_leccion_previa_sin_portada(markdown: str) -> str:
    """Retira portada/resumen regenerables y conserva intacto el cuerpo de estudio."""
    txt = str(markdown or "").lstrip("\ufeff").strip()
    if not txt:
        return ""
    heading = _RE_RESUMEN_EJECUTIVO_HEADING.search(txt)
    if heading:
        separador = re.search(r"(?m)^\s*---\s*$", txt[heading.end():])
        if separador:
            inicio = heading.end() + separador.end()
            return txt[inicio:].strip()
        siguiente = re.search(
            r"(?m)^#{1,2}[ \t]+\S", txt[heading.end():])
        if siguiente:
            inicio = heading.end() + siguiente.start()
            return txt[inicio:].strip()
        txt = txt[:heading.start()].rstrip()
    txt = _quitar_titulo_principal(txt)
    txt = re.sub(
        r"(?m)^(?:>[^\n]*\n(?:\s*\n)?)+", "", txt, count=1,
    )
    return txt.strip()


def _integrar_leccion_previa_local(contexto: dict, cuerpo_nuevo: str) -> str:
    """Conserva el cuerpo previo completo sin incorporarlo a ningun prompt LLM."""
    previo = _cuerpo_leccion_previa_sin_portada(
        str(contexto.get("texto_completo", "") or ""))
    if not previo:
        return cuerpo_nuevo
    integrado = f"{previo.rstrip()}\n\n---\n\n{cuerpo_nuevo.lstrip()}"
    return deduplicar_referencias_img(integrado)[0].strip()


_RE_PARTE_PUBLICADA = re.compile(
    r"(?im)^#[ \t]+Parte[ \t]+(?P<parte>\d+)(?:[^\n]*)$"
)
_RE_RANGO_PUBLICADO = re.compile(
    r"(?im)^>[ \t]*Rango:[ \t]*"
    r"(?P<inicio>\d{1,3}:\d{2}(?::\d{2})?)[ \t]*(?:->|→)[ \t]*"
    r"(?P<fin>\d{1,3}:\d{2}(?::\d{2})?)[^\n]*\n?"
)
_RE_COBERTURA_INTEGRADA = re.compile(
    r"(?im)^>[ \t]*Cobertura[ \t]+útil[ \t]+integrada:[ \t]*"
    r"(?P<inicio>\d{1,3}:\d{2}(?::\d{2})?)[ \t]*(?:->|→)[ \t]*"
    r"(?P<fin>\d{1,3}:\d{2}(?::\d{2})?)[^\n]*\n?"
)
_RE_SECCION_MAYOR = re.compile(
    r"(?m)^(?P<nivel>#{1,2})[ \t]+(?P<titulo>[^\n]+?)[ \t]*$"
)
_RE_SUBSECCION_ESTUDIO = re.compile(
    r"(?m)^###[ \t]+(?P<titulo>[^\n]+?)[ \t]*$"
)


def _titulo_seccion_normalizado(titulo: str) -> str:
    txt = re.sub(r"[`*_~]+", " ", str(titulo or ""))
    txt = _normalizar_busqueda(txt)
    txt = re.sub(r"^\s*\d+(?:[.)]|\s)+", " ", txt)
    txt = re.sub(r"[^a-z0-9]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _clasificar_seccion_publica(titulo: str) -> str:
    normal = _titulo_seccion_normalizado(titulo)
    if "panorama tematico" in normal and "leccion" in normal:
        return "panorama"
    if "guia de estudio" in normal:
        return "guia"
    if "glosario" in normal and ("formula" in normal or "bilingue" in normal):
        return "glosario"
    if "desarrollo exhaustivo" in normal or normal == "desarrollo de la clase":
        return "desarrollo"
    if "diapositiva" in normal and (
            "adicional" in normal or "secuencia" in normal):
        return "visuales_adicionales"
    if "recurso" in normal and (
            "consultado" in normal or "demostracion" in normal):
        return "recursos"
    if "galeria visual" in normal:
        return "galeria"
    if "limite" in normal and ("grabacion" in normal or "cobertura" in normal):
        return "limites"
    if "notas del estudiante" in normal:
        return "notas"
    if "fuentes ampliadas" in normal:
        return "fuentes"
    if "preguntas hechas en vivo" in normal:
        return "preguntas"
    if "tareas y encargos" in normal:
        return "tareas"
    return "tema"


def _limpiar_bordes_seccion(markdown: str) -> str:
    txt = str(markdown or "").strip()
    txt = re.sub(r"^(?:[ \t]*---[ \t]*\n)+", "", txt)
    txt = re.sub(r"(?:\n[ \t]*---[ \t]*)+$", "", txt)
    return txt.strip()


def _fusionar_contenidos_sin_perdida(contenidos: list[str]) -> str:
    """Une aportes distintos y elimina solo bloques exactamente equivalentes."""
    salida: list[str] = []
    vistos: set[str] = set()
    for contenido in contenidos:
        limpio = _limpiar_bordes_seccion(contenido)
        if not limpio:
            continue
        clave = re.sub(r"\s+", " ", limpio).strip().casefold()
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append(limpio)
    return "\n\n".join(salida)


def _extraer_secciones_mayores(markdown: str) -> tuple[str, list[dict]]:
    txt = _limpiar_bordes_seccion(markdown)
    coincidencias = list(_RE_SECCION_MAYOR.finditer(txt))
    if not coincidencias:
        return txt, []
    preambulo = _limpiar_bordes_seccion(txt[:coincidencias[0].start()])
    secciones: list[dict] = []
    for indice, match in enumerate(coincidencias):
        fin = coincidencias[indice + 1].start() \
            if indice + 1 < len(coincidencias) else len(txt)
        secciones.append({
            "titulo": match.group("titulo").strip(),
            "nivel": len(match.group("nivel")),
            "cuerpo": _limpiar_bordes_seccion(txt[match.end():fin]),
        })
    return preambulo, secciones


def _clave_subseccion_estudio(titulo: str) -> str:
    normal = _titulo_seccion_normalizado(titulo)
    if "concepto" in normal and (
            "dominar" in normal or "clave" in normal):
        return "conceptos_clave"
    if "pregunta" in normal and (
            "examen" in normal or "comprension" in normal):
        return "preguntas_examen"
    if "conexion" in normal and "tema" in normal:
        return "conexiones_temas"
    return normal


def _fusionar_subsecciones_estudio(contenidos: list[str]) -> str:
    """Fusiona guías/glosarios por subsección sin resumir ni descartar detalles."""
    preambulos: list[str] = []
    orden: list[str] = []
    secciones: dict[str, dict] = {}
    for contenido in contenidos:
        txt = _limpiar_bordes_seccion(contenido)
        coincidencias = list(_RE_SUBSECCION_ESTUDIO.finditer(txt))
        if not coincidencias:
            if txt:
                preambulos.append(txt)
            continue
        preambulo = _limpiar_bordes_seccion(txt[:coincidencias[0].start()])
        if preambulo:
            preambulos.append(preambulo)
        for indice, match in enumerate(coincidencias):
            fin = coincidencias[indice + 1].start() \
                if indice + 1 < len(coincidencias) else len(txt)
            titulo = match.group("titulo").strip()
            clave = _clave_subseccion_estudio(titulo)
            if clave not in secciones:
                orden.append(clave)
                secciones[clave] = {"titulo": titulo, "cuerpos": []}
            secciones[clave]["cuerpos"].append(
                _limpiar_bordes_seccion(txt[match.end():fin]))
    bloques: list[str] = []
    preambulo_unido = _fusionar_contenidos_sin_perdida(preambulos)
    if preambulo_unido:
        bloques.append(preambulo_unido)
    for clave in orden:
        seccion = secciones[clave]
        cuerpo = _fusionar_contenidos_sin_perdida(seccion["cuerpos"])
        if clave == "preguntas_examen":
            numero = 0

            def renumerar_pregunta(match):
                nonlocal numero
                numero += 1
                return f"{match.group('indentacion')}{numero}. "

            cuerpo = re.sub(
                r"(?m)^(?P<indentacion>[ \t]*)\d+\.[ \t]+",
                renumerar_pregunta,
                cuerpo,
            )
        bloque = f"### {seccion['titulo']}"
        if cuerpo:
            bloque += f"\n\n{cuerpo}"
        bloques.append(bloque)
    return "\n\n".join(bloques)


def _renumerar_tramos_integrados(markdown: str) -> str:
    """Mantiene una única secuencia de tramos al unir varias capturas de la clase."""
    contador = 0
    patron = re.compile(
        r"(?im)^(?P<prefijo>###[ \t]+(?:🧩[ \t]+)?Tramo)[ \t]+\d+"
        r"(?P<sufijo>[ \t]*(?:·[^\n]*)?)$"
    )

    def reemplazar(match):
        nonlocal contador
        contador += 1
        return f"{match.group('prefijo')} {contador}{match.group('sufijo')}"

    return patron.sub(reemplazar, str(markdown or ""))


_TITULOS_SECCIONES_INTEGRADAS = {
    "guia": "## 🎯 Guía de estudio",
    "glosario": "## 🧮 Glosario bilingüe y fórmulas",
    "desarrollo": "## 📚 Desarrollo exhaustivo por tramos",
    "visuales_adicionales": "## 🧩 Diapositivas y secuencias adicionales",
    "recursos": "## 🔍 Recursos consultados y demostraciones observadas",
    "galeria": "## 🖼️ Galería visual complementaria",
    "limites": "## ⚠️ Límites de la grabación",
    "notas": "## 📝 Notas del estudiante",
    "fuentes": "## 🌐 Fuentes ampliadas",
    "preguntas": "## ❓ Preguntas hechas en vivo",
    "tareas": "## ✅ Tareas y encargos",
}
_ORDEN_SECCIONES_INTEGRADAS = (
    "guia", "glosario", "desarrollo", "visuales_adicionales", "recursos",
    "galeria", "limites", "notas", "fuentes", "preguntas", "tareas",
)


def _integrar_partes_por_secciones(markdown: str) -> str:
    """Convierte bloques de captura en una única lección organizada por secciones.

    Las partes siguen trazables en manifests y evidencia, pero no dividen el material
    de estudio. También admite una lección ya unificada seguida de otra continuación.
    """
    original = str(markdown or "").strip()
    partes = list(_RE_PARTE_PUBLICADA.finditer(original))
    if not partes:
        return original

    coberturas: list[tuple[int, str, str]] = []
    for patron in (_RE_COBERTURA_INTEGRADA, _RE_RANGO_PUBLICADO):
        for match in patron.finditer(original):
            coberturas.append((
                match.start(), match.group("inicio"), match.group("fin"),
            ))
    coberturas.sort(key=lambda item: item[0])
    inicio_cobertura = coberturas[0][1] if coberturas else ""
    fin_cobertura = coberturas[-1][2] if coberturas else ""

    limpio = _RE_COBERTURA_INTEGRADA.sub("", original)
    limpio = _RE_RANGO_PUBLICADO.sub("", limpio)
    partes = list(_RE_PARTE_PUBLICADA.finditer(limpio))
    fuentes: list[str] = []
    prefijo = _limpiar_bordes_seccion(limpio[:partes[0].start()])
    if prefijo:
        fuentes.append(prefijo)
    for indice, match in enumerate(partes):
        fin = partes[indice + 1].start() if indice + 1 < len(partes) else len(limpio)
        cuerpo = _limpiar_bordes_seccion(limpio[match.end():fin])
        if cuerpo:
            fuentes.append(cuerpo)

    preambulos: list[str] = []
    temas: list[dict] = []
    temas_por_clave: dict[str, dict] = {}
    especiales: dict[str, list[str]] = {
        clave: [] for clave in _ORDEN_SECCIONES_INTEGRADAS
    }
    for fuente in fuentes:
        preambulo, secciones = _extraer_secciones_mayores(fuente)
        if preambulo:
            preambulos.append(preambulo)
        for seccion in secciones:
            categoria = _clasificar_seccion_publica(seccion["titulo"])
            if categoria == "panorama":
                if seccion["cuerpo"]:
                    preambulos.append(seccion["cuerpo"])
                continue
            if categoria in especiales:
                especiales[categoria].append(seccion["cuerpo"])
                continue
            clave = _titulo_seccion_normalizado(seccion["titulo"])
            if clave and clave in temas_por_clave:
                temas_por_clave[clave]["cuerpos"].append(seccion["cuerpo"])
                continue
            item = {
                "titulo": seccion["titulo"],
                "cuerpos": [seccion["cuerpo"]],
            }
            temas.append(item)
            if clave:
                temas_por_clave[clave] = item

    salida = ["# Panorama temático de la lección"]
    if inicio_cobertura and fin_cobertura:
        salida.append(
            f"> Cobertura útil integrada: {inicio_cobertura} -> {fin_cobertura}. "
            "La estructura sigue los temas; las pausas y continuaciones de grabación "
            "no dividen el contenido."
        )
    preambulo_unido = _fusionar_contenidos_sin_perdida(preambulos)
    if preambulo_unido:
        salida.append(preambulo_unido)
    for tema in temas:
        cuerpo = _fusionar_contenidos_sin_perdida(tema["cuerpos"])
        bloque = f"## {tema['titulo']}"
        if cuerpo:
            bloque += f"\n\n{cuerpo}"
        salida.append(bloque)
    for categoria in _ORDEN_SECCIONES_INTEGRADAS:
        contenidos = especiales[categoria]
        if not contenidos:
            continue
        if categoria in {"guia", "glosario"}:
            cuerpo = _fusionar_subsecciones_estudio(contenidos)
        else:
            cuerpo = _fusionar_contenidos_sin_perdida(contenidos)
        bloque = _TITULOS_SECCIONES_INTEGRADAS[categoria]
        if cuerpo:
            bloque += f"\n\n{cuerpo}"
        salida.append(bloque)

    integrado = "\n\n".join(bloque.strip() for bloque in salida if bloque.strip())
    integrado = re.sub(
        r"(?i)[ \t]+·[ \t]+Parte[ \t]+\d+[ \t]+·[ \t]+",
        " · ",
        integrado,
    )
    integrado = _renumerar_tramos_integrados(integrado)
    integrado = re.sub(r"\n{3,}", "\n\n", integrado).strip()
    return deduplicar_referencias_img(integrado)[0].strip()


def _normalizar_rangos_publicados(
        markdown: str, rangos: list[tuple[int, float, float]], origen) -> str:
    salida = str(markdown or "")
    por_parte = {
        int(parte): (fmt_t(inicio, origen), fmt_t(fin, origen))
        for parte, inicio, fin in rangos
    }

    def reemplazar(match):
        parte = int(match.group("parte"))
        rango = por_parte.get(parte)
        if not rango:
            return match.group(0)
        return (
            f"{match.group('heading')}\n\n"
            f"> Rango: {rango[0]} -> {rango[1]}"
        )

    patron = re.compile(
        r"(?m)^(?P<heading># Parte (?P<parte>\d+)[^\n]*)"
        r"(?:\n[ \t]*)*\n>[ \t]*Rango:[^\n]*"
    )
    return patron.sub(reemplazar, salida)


def _reporte_continuidad_md(contexto: dict, total_segs: int, nuevos_segs: int,
                            snapshots: list[Path]) -> str:
    lineas = [
        "## Reporte de continuidad y contexto previo",
        "",
        f"- Contexto previo usado: {'sí' if contexto.get('disponible') else 'no'}.",
        f"- Fuente de contexto: {contexto.get('fuente', '(ninguna)')}.",
        f"- Chars lección previa: {contexto.get('chars_originales', 0)}.",
        f"- Chars contexto enviado a DeepSeek: {contexto.get('chars_enviados', 0)}.",
        f"- Ahorro estimado de contexto previo: {contexto.get('porcentaje_ahorro', 0)}%.",
        f"- Segmentos transcript totales disponibles: {total_segs}.",
        f"- Segmentos transcript nuevos procesados: {nuevos_segs}.",
        "- Transcript viejo reenviado completo: no.",
        "- Leccion previa integrada completa en la nueva final: si, localmente.",
    ]
    if snapshots:
        lineas.append("- Snapshot previo: " + ", ".join(p.name for p in snapshots) + ".")
    return "\n".join(lineas).strip() + "\n"


def _reporte_optimizacion_tokens_md(reportes_partes: list[dict],
                                    contexto: dict | None = None) -> str:
    bruto = sum(int(r.get("transcript_chars_brutos", 0)) for r in reportes_partes)
    enviado = sum(int(r.get("transcript_chars_enviados", 0)) for r in reportes_partes)
    seg_dedup = sum(int(r.get("segmentos_deduplicados", 0)) for r in reportes_partes)
    ocr_bruto = sum(int(r.get("ocr_chars_brutos", 0)) for r in reportes_partes)
    ocr_enviado = sum(int(r.get("ocr_chars_enviados", 0)) for r in reportes_partes)
    llamadas_antes = sum(int(r.get("llamadas_llm_estimadas_antes", 0))
                         for r in reportes_partes)
    llamadas_reales = sum(int(r.get("llamadas_llm_reales", 0))
                          for r in reportes_partes)
    cache_stats = cache_llm_stats()
    ahorro_trans = 0.0 if not bruto else max(0.0, 100.0 * (1.0 - enviado / bruto))
    ahorro_ocr = 0.0 if not ocr_bruto else max(0.0, 100.0 * (1.0 - ocr_enviado / ocr_bruto))
    lineas = [
        "## Reporte de compactación de contexto y tokens",
        "",
        f"- Transcript bruto enviado candidato: {bruto} chars.",
        f"- Transcript enviado a DeepSeek tras filtro: {enviado} chars.",
        f"- Ahorro transcript estimado: {ahorro_trans:.1f}%.",
        f"- Segmentos deduplicados por repetición: {seg_dedup}.",
        f"- OCR bruto de slides: {ocr_bruto} chars.",
        f"- OCR enviado en fichas: {ocr_enviado} chars.",
        f"- Ahorro OCR estimado: {ahorro_ocr:.1f}%.",
        f"- Llamadas estimadas sin ranking adaptativo: {llamadas_antes}.",
        f"- Llamadas reales guiones/merge tras ranking: {llamadas_reales}.",
        "- Cache LLM local: activa por defecto; desactivar con CLASE_POST_CACHE=0.",
        f"- Cache hits: {cache_stats.get('cache_hits', 0)}; misses: "
        f"{cache_stats.get('cache_misses', 0)}; llamadas reales cacheables: "
        f"{cache_stats.get('llamadas_reales', 0)}.",
        f"- Etapas LLM omitidas por CLASE_POST_LLM=0: "
        f"{cache_stats.get('llm_omitidas', 0)}.",
    ]
    if contexto and contexto.get("disponible"):
        lineas.extend([
            f"- Contexto previo original: {contexto.get('chars_originales', 0)} chars.",
            f"- Contexto previo enviado: {contexto.get('chars_enviados', 0)} chars.",
            f"- Ahorro contexto previo: {contexto.get('porcentaje_ahorro', 0)}%.",
        ])
    return "\n".join(lineas).strip() + "\n"


def _compactar_markdown_para_resumen(
        markdown: str, max_chars: int = 60000) -> str:
    """Muestra equilibrada por secciones; evita que un inicio largo oculte el final."""
    txt = str(markdown or "")
    txt = RE_MD_IMG.sub("", txt)
    txt = RE_HTML_IMG.sub("", txt)
    txt = re.sub(r"(?is)<details>.*?</details>", "", txt)
    txt = re.sub(r"(?m)^\|.*\|\s*$", "", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    if len(txt) <= max_chars:
        return txt
    preambulo, *secciones = re.split(r"(?m)(?=^##[ \t]+\S)", txt)
    if not secciones:
        return txt[:max_chars].rsplit(" ", 1)[0].rstrip()
    max_secciones = max(1, max_chars // 420)
    if len(secciones) > max_secciones:
        indices = {
            round(i * (len(secciones) - 1) / max(1, max_secciones - 1))
            for i in range(max_secciones)
        }
        secciones = [secciones[i] for i in sorted(indices)]
    presupuesto_preambulo = min(len(preambulo), max(600, max_chars // 12))
    restante = max(1, max_chars - presupuesto_preambulo - len(secciones) * 2)
    por_seccion = max(120, restante // max(1, len(secciones)))
    piezas = [preambulo[:presupuesto_preambulo].strip()]
    for seccion in secciones:
        fragmento = seccion[:por_seccion]
        if len(seccion) > por_seccion:
            fragmento = fragmento.rsplit(" ", 1)[0].rstrip()
        piezas.append(fragmento.strip())
    salida = "\n\n".join(pieza for pieza in piezas if pieza).strip()
    if len(salida) > max_chars:
        salida = salida[:max_chars].rsplit(" ", 1)[0].rstrip()
    return salida


def _resumen_ejecutivo_incompleto(resumen: str) -> bool:
    txt = _limpiar_resumen_ejecutivo(str(resumen or "")).rstrip()
    if not txt:
        return True
    if txt.count("```") % 2:
        return True
    ultima = next((linea.strip() for linea in reversed(txt.splitlines())
                   if linea.strip()), "")
    if re.fullmatch(r"(?:[-*+]|\d+[.)]?)", ultima):
        return True
    return ultima[-1:] not in ".!?…:;)]}\"'"


def _cerrar_resumen_incompleto(resumen: str) -> str:
    txt = _limpiar_resumen_ejecutivo(str(resumen or "")).rstrip()
    if not _resumen_ejecutivo_incompleto(txt):
        return txt
    cortes = [m.end() for m in re.finditer(r"[.!?…](?=\s|\Z)", txt)]
    if cortes:
        return txt[:cortes[-1]].rstrip()
    return txt


def _limpiar_resumen_ejecutivo(resumen: str) -> str:
    txt = str(resumen or "").strip()
    patron = re.compile(
        r"^\s{0,3}#{1,3}\s*(?:resumen\s+ejecutivo(?:\s+de\s+clase)?|executive\s+summary)\s*\n+",
        re.I,
    )
    while True:
        nuevo = patron.sub("", txt, count=1).strip()
        if nuevo == txt:
            break
        txt = nuevo
    return txt


def _fallback_resumen_ejecutivo(contexto_previo: dict) -> str:
    previous = str(contexto_previo.get("resumen") or "").strip()
    internal_markers = (
        "api no respondio", "api no respondió", "regenerar con --todo",
        "revisar la leccion integrada", "revisar la lección integrada",
        "deepseek no devolvió", "deepseek no devolvio",
    )
    if previous and not any(marker in previous.casefold() for marker in internal_markers):
        return previous
    return (
        "Consulta el desarrollo completo de la clase: el resumen ejecutivo no pudo "
        "actualizarse durante este postproceso."
    )


def consolidar_parte(d: Path, parte: int, t_ini: float, t_fin: float,
                     origen: str, nota: str = "", forzar: bool = False,
                     cobertura: dict | None = None,
                     segs_override: list[dict] | None = None,
                     contexto_previo: str = "",
                     materiales: list[str | Path] | None = None,
                     paginas_materiales: list[dict] | None = None,
                     permitir_rescate_en_vivo: bool = True) -> Path | None:
    consolidar_parte.ultimo_reporte = {
        "parte": parte, "chunks": 0, "guiones": 0, "busquedas": [],
        "capturas_originales": 0, "capturas_seleccionadas": 0, "capturas_usadas": 0,
        "capturas_interfaz_filtradas": 0, "capturas_interfaz_secundarias": 0,
        "imagenes_duplicadas_detectadas": 0, "duplicados_eliminados": 0,
        "caption_incompatible_advertencias": [],
        "slides_usadas_chunks": 0, "slides_integradas_final": 0,
        "slides_omitidas_por_merge": 0, "slides_omitidas_debug": 0,
        "slides_candidatas": 0, "slides_descartadas_presupuesto": 0,
        "slides_protegidas_estudio": 0,
        "llamadas_llm_estimadas_antes": 0, "llamadas_llm_reales": 0,
        "transcript_chars_brutos": 0, "transcript_chars_enviados": 0,
        "segmentos_deduplicados": 0, "ocr_chars_brutos": 0, "ocr_chars_enviados": 0,
        "materiales_oficiales": 0, "paginas_oficiales": 0,
        "estados_emparejados_material": 0, "duplicados_material_colapsados": 0,
        "matches_material_rechazados": 0,
        "visuales_principales": 0, "visuales_secuencia": 0,
        "visuales_galeria": 0, "visuales_sin_valor": 0,
        "visuales_principales_publicados": 0,
        "visuales_secuencia_publicados": 0,
        "visuales_faltantes_publicacion": [],
        "referencias_web_detectadas": 0, "referencias_web_publicadas": 0,
        "visuales_media_detectados": 0, "visuales_media_publicados": 0,
    }
    destino = d / f"leccion_parte{parte}.md"
    if destino.is_file() and not forzar:
        reporte_guardado = _cargar_reporte_parte(d, parte, destino)
        if reporte_guardado is not None:
            consolidar_parte.ultimo_reporte = reporte_guardado
        print(f"[post] {destino.name} ya existe (usa --forzar para regenerar). Saltando.")
        return destino
    a_rel = _normalizar_valor_tiempo(t_ini, origen)
    b_rel = _normalizar_valor_tiempo(t_fin, origen)
    fuente_segs = segs_override if segs_override is not None else est.leer_jsonl(
        d / "transcript.jsonl")
    segs = [
        s for s in fuente_segs
        if _tiempo_en_rango(normalizar_tiempo_segmento(s, origen), a_rel, b_rel)
    ]
    if not segs:
        if not permitir_rescate_en_vivo:
            print(
                f"[post] parte {parte}: sin segmentos nuevos; se conserva la "
                "publicacion previa sin rescatar leccion_en_vivo.md."
            )
            return None
        # Rescate: si no hay voz transcrita (p. ej. falló el transcriptor), usar los
        # bloques que el Escriba generó EN VIVO (suelen venir del OCR de pantalla).
        lev = d / "leccion_en_vivo.md"
        bloques = [b for b in lev.read_text(encoding="utf-8").split("\n---\n")
                   if f"· Parte {parte}" in b] if lev.is_file() else []
        if not bloques:
            print(f"[post] parte {parte}: sin transcripción ni bloques en vivo. Saltando.")
            return None
        print(f"[post] parte {parte}: SIN voz transcrita; rescatando {len(bloques)} "
              f"bloques del Escriba en vivo (OCR).")
        contenido = (f"# 🧩 Parte {parte}" + (f" — {nota}" if nota else "") +
                     "\n\n> ⚠️ Esta parte no tiene transcripción de voz (falló la captura); "
                     "se conservan las notas EN VIVO generadas desde la pantalla.\n\n---\n"
                     + "\n---\n".join(bloques))
        destino.write_text(contenido, encoding="utf-8")
        return destino

    tema = est.leer_estado(d).get("nombre", "")
    paths = rutas_curso(d)
    curso_md = paths["curso_md"]
    curso_ctx = ""
    if curso_md.is_file():  # excluye la salida previa de esta misma clase al regenerar
        contexto_curso = _contexto_curso_sin_clase_actual(
            curso_md.read_text(encoding="utf-8"), est.leer_estado(d).get("clase_id", ""))
        if contexto_curso.strip():
            curso_ctx = (
                "CONTEXTO DEL CURSO HASTA HOY (solo clases anteriores y notas del "
                "alumno):\n" + contexto_curso[-4000:] + "\n\n"
            )
    if contexto_previo.strip():
        curso_ctx += ("CONTEXTO PREVIO CONSOLIDADO DE ESTA CLASE (compacto; no transcript "
                      "viejo completo):\n" + contexto_previo.strip()[:9000] + "\n\n")
    titulo_nuevo = f"# Parte {parte}" + (f" - {nota}" if nota else "")
    cabecera = titulo_nuevo + f"\n\n> Rango: {fmt_t(a_rel, origen)} -> {fmt_t(b_rel, origen)}\n"
    chunks = dividir_transcript_chunks(segs, origen)
    debug_dir = _debug_chunks_dir(d)
    guiones: list[str] = []
    busquedas: list[str] = []
    consultas_por_chunk: dict[str, list[str]] = {}
    fuentes_por_chunk: dict[str, str] = {}
    consultas_realizadas = 0
    consultas_intentadas: set[str] = set()
    cache_web = d / "_debug" / "cache_web"
    total_originales = 0
    total_seleccionadas = 0
    total_usadas = 0
    total_slides_dedup = 0
    total_slides_enviadas = 0
    total_slides_candidatas = 0
    total_slides_descartadas = 0
    total_slides_protegidas = 0
    llamadas_estimadas_antes = 0
    llamadas_reales = 0
    total_interfaz_filtradas = 0
    total_interfaz_secundarias = 0
    reporte_duplicados = {
        "imagenes_duplicadas_detectadas": 0,
        "duplicados_eliminados": 0,
        "caption_incompatible_advertencias": [],
    }
    reporte_omitidas = {
        "slides_omitidas_por_merge": 0,
        "slides_omitidas_debug": 0,
        "slides_reintegradas_por_tema": 0,
        "slides_solo_debug": 0,
    }
    reporte_cobertura_visual = {
        "esperadas": 0,
        "principales_esperadas": 0,
        "secuencias_esperadas": 0,
        "principales_publicadas": 0,
        "secuencias_publicadas": 0,
        "reintegradas_por_gate": 0,
        "faltantes": [],
        "fallos_caption": [],
    }
    reporte_tokens = {
        "transcript_chars_brutos": 0,
        "transcript_chars_enviados": 0,
        "segmentos_deduplicados": 0,
        "ocr_chars_brutos": 0,
        "ocr_chars_enviados": 0,
    }

    timeline_ini = max(0.0, a_rel - SLIDE_PRE_SEG)
    capturas_parte = capturas_rango(d, timeline_ini, b_rel, origen)
    slides_timeline = construir_timeline_slides(d, timeline_ini, b_rel, segs, origen)
    slides_timeline = _promover_ocr_principal(
        _anotar_ocr_principal(d, slides_timeline))
    reporte_timeline = getattr(construir_timeline_slides, "ultimo_reporte", {})
    reporte_preparacion_material = {
        "materiales_validos": 0, "paginas_preparadas": len(paginas_materiales or []),
        "errores": [],
    }
    if paginas_materiales is None:
        rutas_material = visual_materials.resolver_materiales_oficiales(d, materiales)
        paginas_materiales, reporte_preparacion_material = (
            visual_materials.preparar_materiales_oficiales(
                rutas_material, d / "img",
                manifest_path=d / visual_materials.MANIFEST_NAME,
                reset_appearances=forzar,
            )
        )
    paginas_materiales = paginas_materiales or []
    slides_galeria: list[dict] = []
    slides_sin_valor: list[dict] = []
    reporte_material = {
        "habilitado": False, "estados_emparejados": 0,
        "paginas_oficiales_usadas": 0, "duplicados_colapsados": 0,
        "roles": {},
    }
    if paginas_materiales:
        slides_roles, reporte_material = visual_materials.integrar_material_oficial(
            d / "img", slides_timeline, paginas_materiales,
            manifest_path=d / visual_materials.MANIFEST_NAME,
        )
    else:
        # Los roles no dependen de tener un PDF/PPTX. En capturas puras esta
        # clasificación evita que movimiento de presentador, cursor, UI o vídeo
        # convierta cada frame en una supuesta diapositiva protegida.
        slides_roles = visual_materials.clasificar_roles_visuales(
            d / "img", slides_timeline)
        reporte_material["roles"] = {
            role: sum(slide.get("visual_role") == role for slide in slides_roles)
            for role in ("principal", "secuencia", "galeria", "sin_valor")
        }
    slides_parte = [
        slide for slide in slides_roles
        if slide.get("visual_role") in {"principal", "secuencia"}
    ]
    slides_galeria = [
        slide for slide in slides_roles if slide.get("visual_role") == "galeria"
    ]
    slides_sin_valor = [
        slide for slide in slides_roles if slide.get("visual_role") == "sin_valor"
    ]
    capturas_por_chunk = asignar_slides_a_chunks(capturas_parte, chunks)
    slides_asignadas, slides_seleccionadas, reporte_sel_global = (
        seleccionar_slides_globales_por_chunks(
            slides_parte, chunks, tema_actual=tema,
            curso=str(paths.get("curso", "")), parte=parte))
    total_originales = len(capturas_parte)
    total_slides_dedup = len(slides_roles)
    total_slides_candidatas = int(reporte_sel_global.get("slides_candidatas", 0))
    total_slides_descartadas = int(
        reporte_sel_global.get("slides_descartadas_presupuesto", 0))
    total_slides_protegidas = int(
        reporte_sel_global.get("slides_protegidas_estudio", 0))
    total_interfaz_filtradas = int(
        reporte_timeline.get("capturas_interfaz_filtradas", 0))
    total_interfaz_secundarias = int(
        reporte_timeline.get("capturas_interfaz_secundarias", 0))

    for i, ch in enumerate(chunks, 1):
        opt_trans = optimizar_transcript_llm(ch["segs"], origen)
        texto = opt_trans["texto"] or ch["texto"]
        reporte_tokens["transcript_chars_brutos"] += int(opt_trans["chars_brutos"])
        reporte_tokens["transcript_chars_enviados"] += int(opt_trans["chars_enviados"])
        reporte_tokens["segmentos_deduplicados"] += int(opt_trans["segmentos_deduplicados"])
        a = ch["t_ini"]
        b = ch["t_fin"]
        segs_chunk = ch["segs"]
        capturas_originales = capturas_por_chunk[i - 1]
        slides_candidatas = slides_asignadas[i - 1]
        slides_chunk = slides_seleccionadas[i - 1]
        lotes_slides = _slide_batches(slides_chunk)
        llamadas_estimadas_antes += (
            math.ceil(len(slides_candidatas) / max(1, _post_slides_max()))
            if slides_candidatas else 1)
        antes = chunks[i - 2]["texto"][-1600:] if i > 1 else ""
        despues = chunks[i]["texto"][:1600] if i < len(chunks) else ""
        cobertura_txt = ""
        if cobertura:
            cobertura_txt = (
                "ANALISIS LOCAL DE COBERTURA:\n"
                f"- Estado global: {cobertura.get('cobertura_estado')}\n"
                f"- Gap maximo sin transcript: {fmt_duracion(cobertura.get('gap_max_segundos', 0))}\n"
                f"- Advertencias: {'; '.join(cobertura.get('advertencias', [])) or '(ninguna)'}\n\n"
            )
        resumenes_vivos_txt = resumenes_tramos_rango(d, a, b, origen)
        modo_principal = "DeepSeek" if _post_llm_enabled() else "fallback local"
        clave_chunk = f"{parte}.{i:03d}"
        fuente_contexto_chunk = ""
        if (_post_llm_enabled()
                and consultas_realizadas < _post_web_max_consultas()):
            consulta_previa = _consulta_web_fallback(texto, tema)
            if consulta_previa:
                consultas_por_chunk.setdefault(clave_chunk, []).append(consulta_previa)
                busquedas.append(consulta_previa)
                consultas_intentadas.add(consulta_previa.casefold())
                try:
                    fuente_contexto_chunk = fuentes_ampliadas_md(
                        consulta_previa, n=2, cache_dir=cache_web)
                except TypeError:
                    fuente_contexto_chunk = fuentes_ampliadas_md(consulta_previa)
                consultas_realizadas += 1
                if fuente_contexto_chunk:
                    fuentes_por_chunk[clave_chunk] = fuente_contexto_chunk
        print(f"[post] parte {parte}: guion chunk {i}/{len(chunks)} "
              f"({fmt_t(a, origen)}-{fmt_t(b, origen)}, {len(texto)} chars, "
              f"{len(capturas_originales)} capturas originales -> "
              f"{len(slides_candidatas)} slides deduplicadas -> "
              f"{len(slides_chunk)} seleccionadas -> "
              f"1 tanda {modo_principal} + "
              f"{max(0, len(lotes_slides) - 1)} tanda(s) deterministica(s))...")
        for lote_idx, seleccionadas in enumerate(lotes_slides, 1):
            lote_principal = lote_idx == 1
            if lote_principal:
                consultas_segmento: list[str] = []
                info_pub = clasificar_publicidad_probable(
                    segs_chunk, seleccionadas, antes, despues)
                fichas_txt = fichas_slides(
                    seleccionadas, origen, transcript_max_chars=900)
                rep_fichas = getattr(fichas_slides, "ultimo_reporte", {})
                reporte_tokens["ocr_chars_brutos"] += int(
                    rep_fichas.get("ocr_chars_brutos", 0))
                reporte_tokens["ocr_chars_enviados"] += int(
                    rep_fichas.get("ocr_chars_enviados", 0))
                fuentes_contexto_txt = (
                    "FUENTES EXTERNAS PARA CONTRASTE DEL RANGO "
                    "(no son voz del profesor):\n"
                    f"{fuente_contexto_chunk}\n\n"
                    if fuente_contexto_chunk else ""
                )
                prompt_usuario = (
                    f"TEMA DE LA CLASE: {tema}\n\n{curso_ctx}"
                    f"{cobertura_txt}"
                    f"{resumenes_vivos_txt + chr(10) + chr(10) if resumenes_vivos_txt else ''}"
                    f"{fuentes_contexto_txt}"
                    f"ANALISIS LOCAL DE PUBLICIDAD/RUIDO:\n"
                    f"{analisis_publicidad_md(info_pub)}\n\n"
                    f"TRANSCRIPCION FILTRADA DEL RANGO:\n{texto}\n\n"
                    f"SLIDES ENVIADAS (tanda {lote_idx}/{len(lotes_slides)}):\n"
                    f"{fichas_txt}"
                )
                etiqueta = f"post-chunk-p{parte}-{i:03d}"
                cuerpo = preguntar_cacheado(
                    d,
                    [{"role": "system", "content": PROMPT_SLIDE_GUIDED},
                     {"role": "user", "content": prompt_usuario}],
                    max_tokens=POST_MAX_TOKENS, etiqueta=etiqueta,
                    prompt_version=(
                        f"{POST_VERSION}:{SLIDE_GUIDED_VERSION}"))
                call_report = getattr(preguntar_cacheado, "ultimo_reporte", {})
                if call_report.get("llm") != "disabled" and not call_report.get("hit"):
                    llamadas_reales += 1
                if _respuesta_vacia(cuerpo):
                    cuerpo = _fallback_slide_guided_md(
                        seleccionadas, segs_chunk, origen)
                else:
                    cuerpo, consultas = _extraer_busquedas(str(cuerpo))
                    consultas_segmento.extend(consultas)
                cuerpo = _sanear_contenido_llm_publico(str(cuerpo))
                if (not consultas_segmento and _post_llm_enabled()
                        and clave_chunk not in consultas_por_chunk):
                    consulta_fallback = _consulta_web_fallback(
                        f"{cuerpo or ''} {texto}", tema)
                    if consulta_fallback:
                        consultas_segmento.append(consulta_fallback)
                if consultas_segmento:
                    consultas_por_chunk.setdefault(clave_chunk, []).extend(
                        consultas_segmento[:1])
                    busquedas.extend(consultas_segmento[:1])
                tejido = entretejer(str(cuerpo).strip(), d, seleccionadas, origen)
                total_slides_enviadas += len(seleccionadas)
            else:
                tejido = ""
            tejido = asegurar_slides_seleccionadas_en_guion(
                tejido, d, seleccionadas, origen, parte)
            tejido, reporte_dup = deduplicar_referencias_img(tejido)
            _sumar_reporte_duplicados(reporte_duplicados, reporte_dup)
            if lote_principal and _leccion_demasiado_corta(tejido, segs_chunk) and \
                    "Transcripcion cruda" not in tejido and \
                    "Transcripción cruda" not in tejido:
                tejido = tejido.rstrip() + "\n\n" + _transcripcion_cruda_md(
                    segs_chunk, origen, "### Transcripcion cruda del tramo")
            usadas = set(extraer_referencias_img(tejido))
            total_seleccionadas += len(seleccionadas)
            total_usadas += len(usadas)
            modo_lote = modo_principal if lote_principal else "deterministica"
            print(f"[post] parte {parte}: tanda {lote_idx}/{len(lotes_slides)} "
                  f"({modo_lote}) -> {len(seleccionadas)} slides -> "
                  f"{len(usadas)} usadas")
            sufijo = f"{i:03d}" if len(lotes_slides) == 1 else f"{i:03d}_{lote_idx:02d}"
            chunk_md = (f"# Chunk {parte}.{sufijo}\n\n"
                        f"> Rango: {fmt_t(a, origen)} -> {fmt_t(b, origen)}\n"
                        f"> Slides de esta tanda: {len(seleccionadas)} de "
                        f"{len(slides_chunk)} deduplicadas.\n\n"
                        f"{tejido.strip()}\n")
            (debug_dir / f"leccion_chunk_{parte:02d}_{sufijo}.md").write_text(
                chunk_md, encoding="utf-8")
            guiones.append(chunk_md)

    merge_input = "\n\n---\n\n".join(guiones)
    merge_input_textual = _guiones_textuales_para_merge(guiones)
    fuentes_piezas: list[str] = []
    for clave_chunk, consultas_chunk in consultas_por_chunk.items():
        consulta = next((str(q).strip() for q in consultas_chunk if str(q).strip()), "")
        if not consulta:
            continue
        fuente_chunk = fuentes_por_chunk.get(clave_chunk, "")
        if not fuente_chunk and consultas_realizadas < _post_web_max_consultas():
            consulta_pendiente = next((
                str(q).strip() for q in consultas_chunk
                if str(q).strip()
                and str(q).strip().casefold() not in consultas_intentadas
            ), "")
            if consulta_pendiente:
                consulta = consulta_pendiente
                consultas_intentadas.add(consulta.casefold())
                try:
                    fuente_chunk = fuentes_ampliadas_md(
                        consulta, n=2, cache_dir=cache_web)
                except TypeError:
                    # Conserva la interfaz inyectable histórica de los harnesses locales.
                    fuente_chunk = fuentes_ampliadas_md(consulta)
                consultas_realizadas += 1
        if fuente_chunk:
            fuentes_por_chunk[clave_chunk] = fuente_chunk
            fuentes_piezas.append(
                fuente_chunk.replace(
                    "#### Fuentes ampliadas", f"### {consulta}", 1).strip())
    fuentes_para_merge = (
        "## Fuentes ampliadas\n\n" + "\n\n".join(fuentes_piezas) + "\n"
        if fuentes_piezas else ""
    )
    contexto_fuentes = (
        "\n\nFUENTES EXTERNAS RECUPERADAS (corte temporal "
        f"{time.strftime('%Y-%m-%d')}; no forman parte de la voz del profesor):\n"
        f"{fuentes_para_merge}\n"
        if fuentes_para_merge else ""
    )
    cuerpo_final = preguntar_cacheado(
        d,
        [{"role": "system", "content": PROMPT_MERGE_LECCION},
         {"role": "user", "content": f"TEMA DE LA CLASE: {tema}\n"
                                     f"PARTE NUEVA A CONSOLIDAR: {parte}\n\n"
                                     f"GUIONES TEXTUALES DETALLADOS DE TRAMOS:\n"
                                     f"{merge_input_textual}"
                                     f"{contexto_fuentes}"}],
        max_tokens=POST_MAX_TOKENS, etiqueta=f"post-merge-parte{parte}",
        prompt_version=f"{POST_VERSION}:{MERGE_VERSION}")
    merge_report = getattr(preguntar_cacheado, "ultimo_reporte", {})
    if merge_report.get("llm") != "disabled" and not merge_report.get("hit"):
        llamadas_reales += 1
    if _respuesta_vacia(cuerpo_final):
        cuerpo_final = (
            "## 🧭 Panorama de la parte\n\n"
            "El desarrollo exhaustivo por tramos conserva a continuación el contenido "
            "recuperado de la transcripción y sus evidencias visuales."
        )
    else:
        cuerpo_final = str(cuerpo_final)
    cuerpo_final = _sanear_panorama_parte(cuerpo_final)
    cuerpo_final, consultas_merge = _extraer_busquedas(cuerpo_final)
    busquedas.extend(consultas_merge)
    desarrollo_detallado = _desarrollo_detallado_guiones_md(
        guiones, fuentes_por_chunk)
    if desarrollo_detallado:
        cuerpo_final = cuerpo_final.rstrip() + "\n\n" + desarrollo_detallado
    refs_guiones = set(extraer_referencias_img(merge_input))
    refs_final = set(extraer_referencias_img(cuerpo_final))
    faltantes = refs_guiones - refs_final
    if faltantes:
        reporte_omitidas = _registrar_slides_omitidas_por_merge(
            d, parte, merge_input, faltantes)
        cuerpo_final, reintegracion = _integrar_visuales_omitidos_en_leccion(
            cuerpo_final, merge_input, faltantes, parte=parte, d=d,
            slides=slides_roles, origen=origen)
        reporte_omitidas.update({
            "slides_reintegradas_por_tema": reintegracion["integradas"],
            "slides_solo_debug": reintegracion["solo_debug"],
        })
    cuerpo_final, reporte_cobertura_visual = (
        _integrar_cobertura_visual_obligatoria(
            cuerpo_final, d, slides_roles, origen, parte)
    )
    galeria_visual = _galeria_visual_complementaria_md(
        d, slides_galeria, origen, parte)
    if galeria_visual:
        cuerpo_final = cuerpo_final.rstrip() + "\n\n" + galeria_visual
    cuerpo_final = _corregir_identidad_media_periferica(
        cuerpo_final, slides_roles)
    cuerpo_final = _sanear_metadatos_visuales_publicos(cuerpo_final)
    cuerpo_final = _adornar_encabezados_pedagogicos(cuerpo_final)
    cuerpo_final, reporte_dup_final = deduplicar_referencias_img(cuerpo_final)
    _sumar_reporte_duplicados(reporte_duplicados, reporte_dup_final)
    refs_publicadas = set(extraer_referencias_img(cuerpo_final))
    referencias_web = {
        str(slide.get("archivo", "")) for slide in slides_roles
        if slide.get("browsing_reference")
        and slide.get("visual_role") != "sin_valor"
    }
    visuales_media = {
        str(slide.get("archivo", "")) for slide in slides_roles
        if slide.get("media_reference")
        and slide.get("visual_role") != "sin_valor"
    }
    if _leccion_demasiado_corta(cuerpo_final, segs) and "Transcripcion cruda" not in cuerpo_final:
        cuerpo_final = cuerpo_final.rstrip() + "\n\n" + _transcripcion_cruda_md(segs, origen)

    destino.write_text(cabecera + "\n\n" + eliminar_otras_pantallas(cuerpo_final).strip()
                       + "\n", encoding="utf-8")
    consolidar_parte.ultimo_reporte = {
        "parte": parte, "chunks": len(chunks), "guiones": len(guiones),
        "busquedas": busquedas,
        # Las fuentes ya quedaron junto al tramo que justificó la consulta; no se
        # devuelven para volver a anexarlas al final de la lección integrada.
        "fuentes_ampliadas_md": "",
        "fuentes_ampliadas_incrustadas": bool(fuentes_por_chunk),
        "capturas_originales": total_originales,
        "capturas_seleccionadas": total_seleccionadas,
        "capturas_usadas": total_usadas,
        "slides_deduplicadas": total_slides_dedup,
        "slides_candidatas": total_slides_candidatas,
        "slides_enviadas": total_slides_enviadas,
        "slides_descartadas_presupuesto": total_slides_descartadas,
        "slides_protegidas_estudio": total_slides_protegidas,
        "slides_usadas": total_usadas,
        "slides_usadas_chunks": len(refs_guiones),
        "slides_integradas_final": len(set(extraer_referencias_img(cuerpo_final))),
        "slides_omitidas_por_merge": reporte_omitidas["slides_omitidas_por_merge"],
        "slides_omitidas_debug": reporte_omitidas["slides_omitidas_debug"],
        "slides_reintegradas_por_tema": reporte_omitidas.get(
            "slides_reintegradas_por_tema", 0),
        "slides_solo_debug": reporte_omitidas.get("slides_solo_debug", 0),
        "llamadas_llm_estimadas_antes": llamadas_estimadas_antes + 1,
        "llamadas_llm_reales": llamadas_reales,
        "capturas_interfaz_filtradas": total_interfaz_filtradas,
        "capturas_interfaz_secundarias": total_interfaz_secundarias,
        "imagenes_duplicadas_detectadas": reporte_duplicados["imagenes_duplicadas_detectadas"],
        "duplicados_eliminados": reporte_duplicados["duplicados_eliminados"],
        "caption_incompatible_advertencias": (
            reporte_duplicados["caption_incompatible_advertencias"]),
        "materiales_oficiales": len({
            str(page.get("fuente_sha256", "")) for page in paginas_materiales
            if page.get("fuente_sha256")
        }),
        "paginas_oficiales": len(paginas_materiales),
        "paginas_oficiales_usadas": int(
            reporte_material.get("paginas_oficiales_usadas", 0)),
        "estados_emparejados_material": int(
            reporte_material.get("estados_emparejados", 0)),
        "duplicados_material_colapsados": int(
            reporte_material.get("duplicados_colapsados", 0)),
        "matches_material_rechazados": int(
            reporte_material.get("matches_rechazados_secuencia", 0)),
        "detalle_matches_material_rechazados": list(
            reporte_material.get("detalle_matches_rechazados", [])),
        "visuales_principales": int(
            reporte_material.get("roles", {}).get("principal", 0)),
        "visuales_secuencia": int(
            reporte_material.get("roles", {}).get("secuencia", 0)),
        "visuales_galeria": len(slides_galeria),
        "visuales_sin_valor": len(slides_sin_valor),
        "visuales_principales_publicados": int(
            reporte_cobertura_visual.get("principales_publicadas", 0)),
        "visuales_secuencia_publicados": int(
            reporte_cobertura_visual.get("secuencias_publicadas", 0)),
        "visuales_reintegrados_gate_publicacion": int(
            reporte_cobertura_visual.get("reintegradas_por_gate", 0)),
        "visuales_faltantes_publicacion": list(
            reporte_cobertura_visual.get("faltantes", [])),
        "visuales_fallos_caption": list(
            reporte_cobertura_visual.get("fallos_caption", [])),
        "referencias_web_detectadas": len(referencias_web),
        "referencias_web_publicadas": len(referencias_web & refs_publicadas),
        "visuales_media_detectados": len(visuales_media),
        "visuales_media_publicados": len(visuales_media & refs_publicadas),
        "errores_material_oficial": list(
            reporte_preparacion_material.get("errores", [])),
        **reporte_tokens,
    }
    _guardar_reporte_parte(
        d, parte, destino, consolidar_parte.ultimo_reporte)
    print(f"[post] parte {parte}: {total_originales} capturas originales -> "
          f"{total_slides_dedup} slides deduplicadas -> "
          f"{total_slides_enviadas} enviadas -> {total_usadas} usadas "
          f"({total_slides_descartadas} descartadas por presupuesto)")
    if reporte_material.get("habilitado"):
        print(
            f"[post] material oficial: {reporte_material.get('estados_emparejados', 0)} "
            f"estados -> {reporte_material.get('paginas_oficiales_usadas', 0)} páginas; "
            f"{reporte_material.get('duplicados_colapsados', 0)} duplicados colapsados; "
            f"{reporte_material.get('matches_rechazados_secuencia', 0)} ambiguos rechazados; "
            f"roles {reporte_material.get('roles', {})}"
        )
    print(f"[post] chunks parte {parte}: {len(chunks)} creados -> {len(guiones)} guiones "
          f"-> merge final OK")
    print(f"[post] escrito {destino}")
    return destino

def extraer_tareas(d: Path, contenido: str, clase_id: str) -> str | None:
    """Detecta tareas dejadas en clase; las anexa a la lección y a contenido/TAREAS.md."""
    if not _post_llm_enabled():
        print("[post] tareas: extracción LLM omitida por CLASE_POST_LLM=0")
        return None
    r = preguntar([{"role": "system", "content": PROMPT_TAREAS},
                   {"role": "user", "content": contenido[:30000]}],
                  max_tokens=800, etiqueta="post-tareas")
    if _respuesta_vacia(r) or "SIN_TAREAS" in str(r):
        return None
    seccion = ("\n---\n\n## 📋 Tareas detectadas en esta clase\n"
               "> ⬇️ **Descarga AHORA los archivos mencionados** (notebooks/datasets/slides) "
               "desde el chat del Meet o Drive, antes de que caduquen los enlaces. "
               "Luego pídele al agente resolverlos/exportar a PDF.\n\n" + r.strip() + "\n")
    with open(d / "leccion_final.md", "a", encoding="utf-8") as f:
        f.write(seccion)
    with open(rutas_curso(d)["tareas_md"], "a", encoding="utf-8") as f:
        f.write(f"\n## {clase_id}\n{r.strip()}\n")
    return r


def _sanear_markdown_final(d: Path) -> None:
    img_dir = d / "img"
    existentes = {p.name for p in img_dir.iterdir() if p.is_file()} if img_dir.is_dir() else set()
    for md in list(d.glob("leccion_parte*.md")) + [d / "leccion_final.md",
                                                   d / "leccion_final_eng.md"]:
        if md.is_file():
            limpio = eliminar_otras_pantallas(md.read_text(encoding="utf-8"))
            limpio = RE_MD_IMG.sub("", limpio)
            limpio = _filtrar_imgs_autorizadas(limpio, existentes)
            md.write_text(limpio, encoding="utf-8")


def _referencias_img(d: Path) -> set[str]:
    referenciadas = set()
    docs = list(d.glob("leccion_parte*.md")) + [
        d / "leccion_final.md", d / "leccion_final_eng.md", d / "qa.md"
    ]
    lecciones_detectadas = d / "lecciones_detectadas"
    if lecciones_detectadas.is_dir():
        docs.extend(lecciones_detectadas.glob("*.md"))
    docs.extend(d.glob("leccion_final_PREV_*.md"))
    debug_dir = d / "_debug"
    if debug_dir.is_dir():
        docs.extend(debug_dir.glob("slides_omitidas_por_merge.md"))
        docs.extend((debug_dir / "chunks").glob("leccion_chunk*.md"))
    for md in docs:
        if md.is_file():
            referenciadas |= set(extraer_referencias_img(md.read_text(encoding="utf-8")))
    manifest_material = d / visual_materials.MANIFEST_NAME
    if manifest_material.is_file():
        try:
            data = json.loads(manifest_material.read_text(encoding="utf-8"))
            for pagina in data.get("paginas", []):
                archivo = str(pagina.get("archivo", "") or "").strip()
                if archivo:
                    referenciadas.add(archivo)
                for aparicion in pagina.get("apariciones", []):
                    evidencia = str(aparicion.get("archivo", "") or "").strip()
                    if evidencia:
                        referenciadas.add(evidencia)
        except (OSError, ValueError, TypeError, AttributeError):
            # Manifest derivado inválido: no se adivinan rutas ni se bloquea legacy.
            pass
    return referenciadas


def _imagenes_protegidas_por_cobertura(cobertura: dict | None) -> set[str]:
    if not cobertura:
        return set()
    protegidas = set()
    for p in cobertura.get("periodos_con_capturas_sin_audio", []):
        protegidas |= {str(x) for x in p.get("capturas", []) if x}
    return protegidas


def _wav_limpieza_segura_por_chunks(cobertura: dict | None) -> bool:
    if not cobertura:
        return False
    if int(cobertura.get("wav_total", 0) or 0) <= 0:
        return False
    return (
        int(cobertura.get("wav_sin_transcript_total", 0) or 0) == 0
        and int(cobertura.get("transcript_wav_faltante_total", 0) or 0) == 0
        and not cobertura.get("wav_util_sin_transcript")
        and (cobertura.get("continuidad_audio") or {}).get("status", "ok") != "warning"
    )


def _razones_compactacion_retenida(
        barrera: dict, cobertura: dict, *, sin_audio: bool) -> list[str]:
    reasons = list(
        barrera.get("blocking_reasons") or barrera.get("reasons") or [])
    if sin_audio:
        return reasons
    if int(cobertura.get("wav_sin_transcript_total", 0) or 0):
        reasons.append(
            f"wav_sin_commit={int(cobertura['wav_sin_transcript_total'])}")
    if int(cobertura.get("transcript_wav_faltante_total", 0) or 0):
        reasons.append(
            "transcript_con_wav_ausente="
            f"{int(cobertura['transcript_wav_faltante_total'])}")
    if cobertura.get("wav_util_sin_transcript"):
        reasons.append("audio_util_sin_transcript")
    continuidad = cobertura.get("continuidad_audio") or {}
    if continuidad.get("status") == "warning":
        if int(continuidad.get("explicit_gap_frames", 0) or 0):
            reasons.append("discontinuidad_pcm_explicita")
        if int(continuidad.get("data_discontinuity_packets", 0) or 0):
            reasons.append("paquetes_wasapi_discontinuos")
        if float(continuidad.get("wall_gap_max_seconds", 0) or 0) > 0.2:
            reasons.append(
                "gap_no_declarado="
                f"{float(continuidad.get('wall_gap_max_seconds') or 0):.3f}s")
        if int(continuidad.get("terminal_capture_failures", 0) or 0):
            reasons.append("fallo_terminal_de_captura")
    if not cobertura.get("limpieza_segura") and not reasons:
        reasons.append("cobertura_temporal_dudosa")
    return list(dict.fromkeys(str(reason) for reason in reasons if reason))


def limpiar(d: Path, cobertura: dict | None = None,
            forzar_limpieza_evidencia: bool = False) -> None:
    """Operación destructiva opt-in para evidencia no referenciada.

    Exige que la lección y el transcript existan; consolidar no la invoca por defecto.
    """
    if not (d / "leccion_final.md").is_file() or not (d / "transcript.jsonl").is_file():
        print("[limpiar] falta leccion_final.md o transcript.jsonl: NO se limpia nada.")
        return
    if cobertura is None:
        st = est.leer_estado(d) if (d / "session_state.json").is_file() else {}
        cobertura = analizar_cobertura_audio(
            d, est.leer_jsonl(d / "transcript.jsonl"),
            est.leer_jsonl(d / "img_log.jsonl"),
            {"origen": st.get("origen", "vivo"), "inicio": st.get("inicio", 0.0)})
    _sanear_markdown_final(d)
    referenciadas = _referencias_img(d)
    protegidas = _imagenes_protegidas_por_cobertura(cobertura)
    continuidad = cobertura.get("continuidad_audio") or continuidad_audio_contextual(d)
    cobertura["continuidad_audio"] = continuidad
    continuidad_segura = continuidad.get("status") != "warning"
    limpieza_segura = bool(cobertura.get("limpieza_segura", True)) or forzar_limpieza_evidencia
    limpieza_wav_segura = forzar_limpieza_evidencia or (
        continuidad_segura
        and (limpieza_segura or _wav_limpieza_segura_por_chunks(cobertura))
    )
    liberado, n_wav, n_img = 0, 0, 0
    audio_dir = d / "audio"
    if audio_dir.is_dir():
        if limpieza_wav_segura:
            if not limpieza_segura:
                print("[post] limpieza: WAV cubiertos por transcript; se eliminan aunque haya gaps de pausa/receso.")
            for wav in audio_dir.glob("*.wav"):
                liberado += wav.stat().st_size
                wav.unlink()
                n_wav += 1
            with os.scandir(audio_dir) as it:
                if not any(it):
                    audio_dir.rmdir()
        else:
            print("[post] limpieza: diferida por gap/cobertura/continuidad dudosa; "
                  "WAV conservados.")
    img_dir = d / "img"
    if img_dir.is_dir():
        for f in img_dir.iterdir():
            if f.is_file() and f.name not in referenciadas and (
                    limpieza_segura or f.name not in protegidas):
                liberado += f.stat().st_size
                f.unlink()
                n_img += 1
    protegidas_n = len(protegidas - referenciadas) if not limpieza_segura else 0
    print(f"[limpiar] liberados {liberado / 1024 / 1024:.0f} MB "
          f"({n_wav} WAV de audio + {n_img} imágenes no referenciadas). "
          f"Referenciadas: {len(referenciadas)}. Protegidas por gaps: {protegidas_n}.")


def _zip_dir(origen: Path, destino: Path) -> None:
    with zipfile.ZipFile(destino, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in origen.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(origen))


def aplicar_modo_estudio(d: Path) -> None:
    """Conserva el material de estudio y archiva auxiliares en _debug/*.zip."""
    if not (d / "leccion_final.md").is_file():
        print("[modo-estudio] falta leccion_final.md: no se archiva nada.")
        return
    limpiar(d)
    debug = d / "_debug"
    debug.mkdir(exist_ok=True)
    base_stage = debug / f"archivo_{time.strftime('%Y%m%d_%H%M%S')}"
    stage = base_stage
    suf = 2
    while stage.exists() or (debug / f"{stage.name}.zip").exists():
        stage = debug / f"{base_stage.name}_{suf}"
        suf += 1
    stage.mkdir()
    patrones = [
        "transcript.jsonl", "leccion_en_vivo.md", "qa.md", "marcas.jsonl",
        "img_log.jsonl", "visual_timeline.jsonl", "audio_manifest.jsonl",
        "transcript_chunks", "session_state.json",
        "ESCRIBA_ESTADO*", "*.tmp",
        "leccion_parte*.md", "audio",
    ]
    movidos = 0
    for patron in patrones:
        for p in d.glob(patron):
            if not p.exists() or p == debug or p.name.startswith("leccion_final"):
                continue
            destino = stage / p.name
            if p.is_dir():
                shutil.move(str(p), str(destino))
            else:
                destino.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(destino))
            movidos += 1
    if movidos:
        zip_path = debug / f"{stage.name}.zip"
        _zip_dir(stage, zip_path)
        shutil.rmtree(stage)
        print(f"[modo-estudio] auxiliares archivados en {zip_path}")
    else:
        stage.rmdir()
        print("[modo-estudio] no había auxiliares para archivar.")


def limpiar_debug(d: Path) -> None:
    borrados = 0
    for nombre in ("_debug", "_archivo"):
        p = d / nombre
        if p.is_dir():
            shutil.rmtree(p)
            borrados += 1
    print(f"[limpiar-debug] carpetas debug eliminadas: {borrados}")


def cerrar_curso(nombre_curso: str) -> None:
    """Fin de curso: deja el registro ligero en memoria_cursos.md (viaja con la app) y
    explica cómo clonar la app a un curso nuevo sin arrastrar el contenido pesado."""
    d_activa = est.clase_activa()
    if d_activa is not None:
        clases_dir = rutas_curso(d_activa)["clases_dir"]
    else:
        clases_dir = config.dir_curso(est._slug(nombre_curso)) / "clases"
    carpetas = sorted(p.name for p in clases_dir.iterdir() if p.is_dir()) \
        if clases_dir.is_dir() else []
    hoy = time.strftime("%Y-%m-%d")
    if not config.MEMORIA_CURSOS.is_file():
        config.MEMORIA_CURSOS.write_text(
            "# 🧠 Memoria de cursos (viaja con la app al copiarla)\n", encoding="utf-8")
    with open(config.MEMORIA_CURSOS, "a", encoding="utf-8") as f:
        f.write(f"\n## {nombre_curso}\n"
                f"- Cerrado: {hoy} · Clases procesadas: {len(carpetas)}\n"
                f"- Semanas: {', '.join(carpetas) or '(ninguna)'}\n"
                f"- El contenido quedó en: {clases_dir.parent}\n")
    print(f"[curso] registrado '{nombre_curso}' en memoria_cursos.md\n"
          "\nPara usar la app en un CURSO NUEVO:\n"
          "  1. Copia la carpeta clase-viva\\ a la carpeta del nuevo curso\n"
          "  2. En la COPIA: borra contenido\\, logs\\, .venv\\ y clase_actual.txt\n"
          "     (memoria_cursos.md SÍ se queda: es la memoria entre cursos)\n"
          "  3. En la copia: py -3.12 -m venv .venv && instalar requirements (FASE 1 del PLAN)\n"
          "  4. El contenido de ESTE curso queda aquí; muévelo/respáldalo si quieres liberar espacio\n")


def _trocear_texto(texto: str, n: int = 32000) -> list[str]:
    partes, actual = [], []
    total = 0
    for bloque in texto.split("\n\n"):
        actual.append(bloque)
        total += len(bloque)
        if total >= n:
            partes.append("\n\n".join(actual))
            actual, total = [], 0
    if actual:
        partes.append("\n\n".join(actual))
    return partes


def generar_leccion_ingles(d: Path, final_es: str, st: dict) -> Path:
    destino = d / "leccion_final_eng.md"
    piezas = [f"# {st.get('nombre', st.get('clase_id', 'Class notes'))}",
              "> Academic English version. Image references reuse the same `img/` folder.\n"]
    for i, trozo in enumerate(_trocear_texto(final_es), 1):
        r = preguntar([{"role": "system", "content": PROMPT_FINAL_ENG},
                       {"role": "user", "content": trozo}],
                      max_tokens=POST_MAX_TOKENS, etiqueta=f"post-eng-{i}")
        if _respuesta_vacia(r):
            r = ("## Generation note\n\nThe API did not return this English section. "
                 "The Spanish study version remains the source of truth.\n")
        piezas.append(eliminar_otras_pantallas(r.strip()))
    destino.write_text("\n\n".join(piezas) + "\n", encoding="utf-8")
    return destino


def verificar_barrera_final(d: Path, permitir_parcial: bool = False) -> dict:
    """Impide publicar una final mientras captura o commits STT sigan pendientes."""
    reporte = audio_manifest.completion_barrier_report(d)
    if reporte.get("ready") or permitir_parcial:
        return reporte
    razones = ", ".join(str(x) for x in reporte.get("reasons", [])) or "sin detalle"
    raise RuntimeError(
        "postproceso final bloqueado por evidencia incompleta "
        f"(status={reporte.get('status', 'unknown')}; {razones}). "
        "Espera cierre de captura y commits STT, o usa --parcial explicitamente."
    )


def _bloques_grabacion(
        rangos: list[tuple[int, float, float]], origen) -> list[dict]:
    bloques: list[dict] = []
    for parte, inicio, fin in rangos:
        inicio_util = _normalizar_valor_tiempo(inicio, origen)
        fin_util = max(inicio_util, _normalizar_valor_tiempo(fin, origen))
        bloques.append({
            "part": int(parte),
            "useful_start_sec": round(inicio_util, 3),
            "useful_end_sec": round(fin_util, 3),
            "wall_start": round(_to_float(inicio), 6),
            "wall_end": round(_to_float(fin), 6),
        })
    return bloques


def _procedencia_material_oficial_compacta(d: Path) -> list[dict]:
    manifest = d / visual_materials.MANIFEST_NAME
    if not manifest.is_file():
        return []
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    fuentes: dict[str, dict] = {}
    for pagina in payload.get("paginas", []) or []:
        sha = str(pagina.get("fuente_sha256", "") or "").strip()
        if not sha:
            continue
        item = fuentes.setdefault(sha, {
            "sha256": sha,
            "name": str(pagina.get("fuente_nombre", "") or ""),
            "type": str(pagina.get("tipo_fuente", "") or ""),
            "pages_observed": [],
        })
        if pagina.get("apariciones"):
            try:
                number = int(pagina.get("pagina", 0) or 0)
            except (TypeError, ValueError):
                number = 0
            if number > 0 and number not in item["pages_observed"]:
                item["pages_observed"].append(number)
    for item in fuentes.values():
        item["pages_observed"].sort()
    return list(fuentes.values())


def _metadata_paquete_estudio(
        d: Path, st: dict, rangos: list[tuple[int, float, float]], origen,
        markdown_publico: str) -> dict:
    bloques = _bloques_grabacion(rangos, origen)
    return {
        "kind": study_package.KIND,
        "schema_version": study_package.SCHEMA_VERSION,
        "class": {
            "class_id": str(st.get("clase_id") or d.name),
            "name": str(st.get("nombre") or d.name),
            "course": str(st.get("curso") or est.curso_de(d)),
            "week": int(st.get("semana") or 1),
            "origin": str(st.get("origen") or "vivo"),
            "started_at": _to_float(st.get("inicio")),
        },
        "recording_parts": bloques,
        "useful_duration_sec": (
            bloques[-1]["useful_end_sec"] if bloques else 0.0),
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "continuation_source": "leccion_final.md",
        "package_policy": "lesson_and_referenced_assets",
        "images": study_package.image_inventory(d, markdown_publico),
        "additional_lessons": study_package.additional_lesson_inventory(d),
        "official_materials": _procedencia_material_oficial_compacta(d),
    }


def consolidar_todo(d: Path, forzar: bool = False, limpiar_al_final: bool = False,
                    modo_estudio: bool = False,
                    paquete_ligero: bool = False,
                    permitir_parcial: bool = False,
                    materiales: list[str | Path] | None = None,
                    generar_ingles: bool | None = None) -> None:
    # En sesiones transaccionales ``transcript.jsonl`` es solo una vista:
    # reconstruirla localmente desde commits válidos garantiza que Post nunca
    # retranscriba ni dependa de red. Clases legacy sin journal conservan su
    # transcript histórico, que sigue siendo su única autoridad disponible.
    stt_report = audio_manifest.transcript_status_report(d, verify_files=True)
    if ((d / audio_manifest.JOURNAL_DIR).is_dir()
            or stt_report.get("source") == "transcript_chunks"):
        audio_manifest.materialize_transcript(d, verify_files=True)
    barrera_final = verificar_barrera_final(d, permitir_parcial=permitir_parcial)
    if not barrera_final.get("ready"):
        print("[post] ADVERTENCIA: consolidacion parcial explicita; "
              + "; ".join(str(x) for x in barrera_final.get("reasons", [])))
    _cache_stats_reset()
    st = est.leer_estado(d)
    if generar_ingles is None:
        generar_ingles = os.environ.get("CLASE_POST_ENGLISH", "0").strip().casefold() in {
            "1", "true", "yes", "on",
        }
    origen = _origen_postproceso(d, st)
    rutas_material = visual_materials.resolver_materiales_oficiales(d, materiales)
    paginas_materiales, reporte_preparacion_material = (
        visual_materials.preparar_materiales_oficiales(
            rutas_material, d / "img",
            manifest_path=d / visual_materials.MANIFEST_NAME,
            reset_appearances=forzar,
        )
    )
    if rutas_material:
        print(
            f"[post] material oficial: {len(rutas_material)} fuente(s), "
            f"{len(paginas_materiales)} página(s) limpias preparadas"
        )
        for error in reporte_preparacion_material.get("errores", []):
            print(f"[post] material oficial aviso: {error}")
    contexto_previo = contexto_previo_compacto(d)
    if forzar:
        _debug_slides_omitidas_path(d).write_text(
            _debug_slides_omitidas_header(), encoding="utf-8")
    # El transcript canónico conserva exclusivamente la fuente docente/remota.
    # Las intervenciones locales son una vista pedagógica separada y etiquetada.
    segs_todos = est.leer_jsonl(d / "transcript.jsonl")
    segs_locales = student_notes.local_intervention_segments(d)
    capturas_todas = est.leer_jsonl(d / "img_log.jsonl")
    segs_nuevos = segmentos_nuevos_desde_final(d, segs_todos, origen)
    capturas_nuevas = capturas_nuevas_desde_final(d, capturas_todas)
    es_continuacion = bool(
        contexto_previo.get("disponible")
        and (segs_nuevos or capturas_nuevas or segs_locales)
    )
    snapshots_previos: list[Path] = []
    segs_docentes_procesar = segs_nuevos if es_continuacion else segs_todos
    segs_procesar = sorted(
        [*segs_docentes_procesar, *segs_locales],
        key=lambda row: (
            normalizar_tiempo_segmento(row, origen),
            str(row.get("student_note_id", row.get("chunk", ""))),
        ),
    )
    if es_continuacion:
        snapshots_previos = crear_snapshot_previo(d)
        print(f"[post] continuacion detectada: {len(segs_nuevos)} segmentos docentes "
              f"y {len(segs_locales)} intervenciones locales nuevas; "
              "se usa leccion_final.md previa como contexto compacto")
    paths = rutas_curso(d)
    segs_procesar, reporte_replays = transcript_quality.filter_replayed_segments(
        segs_procesar)
    if reporte_replays.get("replayed_segments"):
        print(
            "[post] replay detectado: "
            f"{reporte_replays['replayed_segments']} segmentos en "
            f"{reporte_replays['replayed_runs']} secuencia(s) se excluyen solo de la "
            "lección derivada; transcript canónico intacto")
    reporte_transcript_sospechoso = analizar_transcript_sospechoso(segs_procesar)
    if reporte_transcript_sospechoso.get("sospechoso"):
        _guardar_transcript_sospechoso(d, reporte_transcript_sospechoso, segs_procesar)
        segs_filtrados = filtrar_transcript_sospechoso(segs_procesar)
        print(f"[post] transcript sospechoso: "
              f"{reporte_transcript_sospechoso['chunks_sospechosos']} chunks con bucles; "
              f"{len(segs_procesar) - len(segs_filtrados)} segmentos separados a _debug")
        segs_procesar = segs_filtrados
    segmentacion = segmentar_transcript_por_tema(
        segs_procesar, capturas_todas, str(paths.get("curso", "")), st.get("nombre", ""))
    extras_detectadas = generar_lecciones_extra_detectadas(d, segmentacion, origen) \
        if _post_separar_temas() else []
    if extras_detectadas:
        extra_keys = getattr(
            generar_lecciones_extra_detectadas, "ultimo_reporte", {}
        ).get("keys_separadas", set())
        filtrados = [s for s in segs_procesar if _segment_key(s) not in extra_keys]
        if filtrados:
            print(f"[post] segmentacion: {len(extras_detectadas)} leccion(es) extra "
                  "separada(s); no se mezclan en la leccion principal")
            segs_procesar = filtrados
    cobertura = analizar_cobertura_audio(
        d, segs_todos, capturas_todas, origen,
        manifest_verificado=bool(barrera_final.get("ready")),
    )
    cobertura_temporal = analizar_cobertura_temporal(d, segs_todos, origen)
    if not barrera_final.get("ready"):
        cobertura["advertencias"].append(
            "postproceso parcial explicito: captura o commits STT pendientes")
        cobertura["cobertura_estado"] = "parcial"
        cobertura["limpieza_segura"] = False
    if reporte_transcript_sospechoso.get("sospechoso"):
        cobertura["advertencias"].append(
            "transcript sospechoso por bucles Whisper; segmentos repetitivos separados")
        cobertura["cobertura_estado"] = "dudosa"
        cobertura["limpieza_segura"] = False
    rango = cobertura.get("rango_transcript") or {}
    minutos = max(0.0, _to_float(rango.get("fin")) - _to_float(rango.get("inicio"))) / 60
    print(f"[post] transcript: {len(segs_todos)} segmentos, {minutos:.1f} min cubiertos")
    print(f"[post] audio: {cobertura['wav_total']} WAV -> "
          f"{cobertura['segmentos_total']} segmentos -> gap maximo "
          f"{fmt_duracion(cobertura['gap_max_segundos'])} -> "
          f"cobertura {cobertura['cobertura_estado']}")
    print(f"[post] cobertura temporal: WAV {cobertura_temporal['wav_total']} -> "
          f"{cobertura_temporal['duracion_wav_fmt']} WAV, "
          f"{cobertura_temporal['duracion_transcript_fmt']} transcript -> "
          f"{cobertura_temporal['estado']}")
    for adv in cobertura.get("advertencias", []):
        print(f"[post] audio aviso: {adv}")
    for adv in cobertura_temporal.get("advertencias", []):
        print(f"[post] cobertura aviso: {adv}")

    notas = [m["nota"] for m in est.leer_marcas(d) if m["tipo"] == "fin_parte"]
    archivos = []
    reportes_partes = []
    busquedas: list[str] = []
    rangos_partes = est.rangos_partes(d)
    partes_nuevas = (
        _partes_con_evidencia(
            [*segs_nuevos, *segs_locales, *capturas_nuevas], rangos_partes, origen)
        if es_continuacion else set()
    )
    for parte, a, b in rangos_partes:
        if es_continuacion and parte not in partes_nuevas:
            print(
                f"[post] parte {parte}: sin evidencia nueva; se conserva sin "
                "regenerar."
            )
            continue
        nota = notas[parte - 1] if parte - 1 < len(notas) else ""
        p = consolidar_parte(d, parte, a, b, origen, nota, forzar, cobertura,
                             segs_override=segs_procesar,
                             contexto_previo=(contexto_previo.get("texto", "")
                                              if es_continuacion else ""),
                             paginas_materiales=paginas_materiales,
                             permitir_rescate_en_vivo=(
                                 not es_continuacion or parte in partes_nuevas))
        if p:
            archivos.append(p)
            rep = getattr(consolidar_parte, "ultimo_reporte", {})
            reportes_partes.append(rep)
            busquedas.extend(rep.get("busquedas", []))
    if not archivos:
        print("[post] nada que consolidar.")
        return

    chunks_creados = sum(int(r.get("chunks", 0)) for r in reportes_partes)
    guiones_creados = sum(int(r.get("guiones", 0)) for r in reportes_partes)
    slides_dedup = sum(int(r.get("slides_deduplicadas", 0)) for r in reportes_partes)
    slides_enviadas = sum(int(r.get("slides_enviadas", 0)) for r in reportes_partes)
    slides_usadas = sum(int(r.get("slides_usadas", 0)) for r in reportes_partes)
    print(f"[post] chunks: {chunks_creados} creados -> {guiones_creados} guiones "
          "-> merge final OK")
    print(f"[post] slides: {len(capturas_todas)} capturas registradas -> "
          f"{slides_dedup} deduplicadas -> {slides_enviadas} enviadas -> "
          f"{slides_usadas} usadas")

    contenido_partes = [eliminar_otras_pantallas(p.read_text(encoding="utf-8"))
                        for p in archivos]
    contenido_partes_txt = "\n\n".join(contenido_partes)
    contenido_partes_txt, reporte_dup_integrado = deduplicar_referencias_img(
        contenido_partes_txt)
    if reporte_dup_integrado.get("imagenes_duplicadas_detectadas"):
        reportes_partes.append({
            "parte": "integrada",
            "imagenes_duplicadas_detectadas": reporte_dup_integrado[
                "imagenes_duplicadas_detectadas"],
            "duplicados_eliminados": reporte_dup_integrado["duplicados_eliminados"],
            "caption_incompatible_advertencias": reporte_dup_integrado[
                "caption_incompatible_advertencias"],
        })
    observed_material_ids: set[str] = set()
    material_manifest = d / visual_materials.MANIFEST_NAME
    if material_manifest.is_file():
        try:
            manifest_data = json.loads(material_manifest.read_text(encoding="utf-8"))
            observed_material_ids = {
                str(page.get("material_id", ""))
                for page in manifest_data.get("paginas", [])
                if page.get("apariciones")
            }
        except (OSError, ValueError, TypeError):
            observed_material_ids = set()
    official_unobserved = _galeria_material_oficial_no_observado_md(
        d, paginas_materiales, observed_ids=observed_material_ids)
    if official_unobserved:
        contenido_partes_txt = (
            contenido_partes_txt.rstrip() + "\n\n" + official_unobserved)
    idioma = idioma_material(segs_procesar or segs_todos, st)
    fecha = time.strftime("%Y-%m-%d", time.localtime(st.get("inicio") or time.time()))
    qa = (d / "qa.md").read_text(encoding="utf-8") if (d / "qa.md").is_file() else ""
    fuentes_partes = [
        str(reporte.get("fuentes_ampliadas_md", "") or "").strip()
        for reporte in reportes_partes
        if str(reporte.get("fuentes_ampliadas_md", "") or "").strip()
    ]
    fuentes_incrustadas = any(
        bool(reporte.get("fuentes_ampliadas_incrustadas"))
        for reporte in reportes_partes)
    if fuentes_partes:
        cuerpos_fuente = [
            re.sub(r"^## Fuentes ampliadas\s*", "", fuente).strip()
            for fuente in fuentes_partes
        ]
        fuentes_md = "## Fuentes ampliadas\n\n" + "\n\n".join(cuerpos_fuente) + "\n"
    elif not fuentes_incrustadas:
        fuentes_md = fuentes_ampliadas_consultas(busquedas)
    else:
        fuentes_md = ""
    reporte_temporal = _reporte_cobertura_temporal_md(cobertura_temporal)
    reporte_transcript_sospechoso_md = _reporte_transcript_sospechoso_md(
        reporte_transcript_sospechoso)
    reporte_slides = _reporte_slides_md(reportes_partes, capturas_todas,
                                        contenido_partes_txt, cobertura)
    reporte_tokens = _reporte_optimizacion_tokens_md(
        reportes_partes, contexto_previo if es_continuacion else None)
    reporte_segmentos = _reporte_segmentacion_md(segmentacion, extras_detectadas)
    technical_reports = [
        _cobertura_md(cobertura), reporte_temporal,
        _reporte_replays_md(reporte_replays),
        reporte_transcript_sospechoso_md, reporte_segmentos,
        reporte_slides, reporte_tokens,
    ]
    public_content = contenido_partes_txt
    if es_continuacion:
        continuidad_md = _reporte_continuidad_md(
            contexto_previo, len(segs_todos), len(segs_nuevos), snapshots_previos)
        technical_reports.append(continuidad_md)
    notes_ready = student_notes.package_readiness_report(d)
    if paquete_ligero and not notes_ready["ok"]:
        reasons = ", ".join(
            str(item.get("reason")) for item in notes_ready["issues"][:8])
        raise RuntimeError(
            "notas incluidas sin contenido publicable; corrige, extrae o descarta "
            f"antes de compactar: {reasons}")
    notes_section = student_notes.lesson_section(
        d, publish_assets=paquete_ligero)
    if notes_section:
        public_content = public_content.rstrip() + "\n\n---\n\n" + notes_section
    additional_lessons = study_package.additional_lesson_inventory(d)
    if paquete_ligero and additional_lessons:
        lines = [
            "## 📚 Lecciones adicionales separadas",
            "",
            (
                "> Se detectó una ruptura temática sostenida. Estos materiales se "
                "conservan como lecciones independientes y no se mezclan con el "
                "desarrollo principal."
            ),
            "",
        ]
        for item in additional_lessons:
            legacy = d / "lecciones_detectadas" / str(item["name"])
            packaged = d / study_package.ADDITIONAL_LESSONS_DIR / str(
                item["package_name"])
            source = legacy if legacy.is_file() else packaged
            if not source.is_file():
                raise RuntimeError(
                    f"falta lección adicional antes de publicarla: {item['name']}")
            body = source.read_text(encoding="utf-8")
            if source == packaged:
                body = (
                    body.replace('src="../', 'src="img/')
                    .replace("src='../", "src='img/")
                    .replace("](../", "](img/")
                )
            body = re.sub(r"^\s*#\s+[^\n]+\n+", "", body, count=1).strip()
            lines.extend([
                f"### {Path(str(item['name'])).stem.replace('_', ' ')}",
                "",
                body,
                "",
            ])
        public_content = (
            public_content.rstrip()
            + "\n\n---\n\n"
            + "\n".join(lines)
        )
    if fuentes_md:
        public_content += "\n---\n\n" + fuentes_md
    if qa.strip():
        public_content += "\n---\n\n# Preguntas hechas en vivo\n" + qa
    cuerpo_integrado_txt, technical_report = separar_publicacion_y_diagnostico(
        contenido=public_content, cobertura=cobertura, reportes=technical_reports)
    cuerpo_integrado_txt = _sanear_metadatos_visuales_publicos(
        eliminar_otras_pantallas(cuerpo_integrado_txt))
    debug_report_path = d / "_debug" / "postprocess_report.md"
    debug_report_path.parent.mkdir(parents=True, exist_ok=True)
    debug_report_path.write_text(
        "# Diagnóstico técnico de postproceso\n\n"
        "> Este documento es para auditoría; no forma parte de la lección de estudio.\n\n"
        + technical_report,
        encoding="utf-8",
    )
    if es_continuacion:
        cuerpo_integrado_txt = _integrar_leccion_previa_local(
            contexto_previo, cuerpo_integrado_txt)
    cuerpo_integrado_txt = _normalizar_rangos_publicados(
        cuerpo_integrado_txt, rangos_partes, origen)
    cuerpo_integrado_txt = _integrar_partes_por_secciones(cuerpo_integrado_txt)

    print("[post] generando resumen ejecutivo desde leccion integrada...")
    resumen_input = (
        "LECCION INTEGRADA COMPLETA (muestra equilibrada de todas las partes):\n\n"
        + _compactar_markdown_para_resumen(
            cuerpo_integrado_txt, max_chars=60000)
    )
    resumen = preguntar_cacheado(
        d,
        [{"role": "system", "content": PROMPT_RESUMEN},
         {"role": "user", "content": resumen_input}],
        max_tokens=1600, etiqueta="post-resumen",
        prompt_version=f"{POST_VERSION}:{SUMMARY_VERSION}")
    if _respuesta_vacia(resumen):
        resumen = _fallback_resumen_ejecutivo(contexto_previo)
    resumen = _limpiar_resumen_ejecutivo(str(resumen))
    if _resumen_ejecutivo_incompleto(resumen):
        print("[post] resumen incompleto detectado; reintentando con cierre estricto...")
        reintento = preguntar_cacheado(
            d,
            [{
                "role": "system",
                "content": (
                    PROMPT_RESUMEN
                    + "\nLa respuesta anterior se corto. Entrega un resumen completo "
                    "de maximo 450 palabras y termina cada punto con una frase cerrada."
                ),
            }, {"role": "user", "content": resumen_input}],
            max_tokens=2200,
            etiqueta="post-resumen-reintento",
            prompt_version=f"{POST_VERSION}:{SUMMARY_VERSION}:cierre",
        )
        if not _respuesta_vacia(reintento):
            resumen = _limpiar_resumen_ejecutivo(str(reintento))
    resumen = _cerrar_resumen_incompleto(resumen)

    bloques_grabacion = _bloques_grabacion(rangos_partes, origen)
    mapa_grabacion = study_package.recording_map_markdown(bloques_grabacion)
    final = [
        f"# {st.get('nombre', st['clase_id'])}",
        f"> Fecha: {fecha} | Idioma detectado: {idioma} | Generado por clase-viva\n",
    ]
    if mapa_grabacion:
        final.append(mapa_grabacion)
    final.extend([
        "## Resumen ejecutivo\n", str(resumen).strip(), "\n---\n",
        cuerpo_integrado_txt,
    ])
    if not barrera_final.get("ready"):
        final.insert(
            2,
            "> **ESTADO PARCIAL:** captura o transcripcion aun pendiente; "
            "no usar como leccion final cerrada.\n",
        )
    final_publico = _adornar_encabezados_pedagogicos(
        eliminar_otras_pantallas("\n\n".join(final) + "\n"))
    if _leccion_demasiado_corta(final_publico, segs_procesar or segs_todos):
        final_publico = final_publico.rstrip() + "\n\n---\n\n" + _transcripcion_cruda_md(
            segs_procesar or segs_todos, origen)
    final_publico = math_render.normalizar_matematica_markdown(final_publico)
    metadata_paquete = _metadata_paquete_estudio(
        d, st, rangos_partes, origen, final_publico)
    final_txt = (
        study_package.metadata_comment(metadata_paquete)
        + "\n\n"
        + final_publico.lstrip()
    )
    study_package.write_text_atomic(d / "leccion_final.md", final_txt)
    cobertura_txt = _reporte_cobertura_textual(final_txt, segs_procesar or segs_todos)
    print(f"[post] leccion_final: {cobertura_txt['leccion_chars']} chars, "
          f"ratio {cobertura_txt['ratio']:.3f}, cobertura {cobertura['cobertura_estado']}")

    (paths["resumenes_dir"] / f"resumen_{st['clase_id']}.md").write_text(
        f"# {st.get('nombre', st['clase_id'])} ({fecha})\n\n"
        f"- Idioma detectado: {idioma}\n\n{resumen}\n", encoding="utf-8")

    cmd_path = paths["curso_md"]
    _actualizar_bitacora_curso(
        d, cmd_path, st["clase_id"], fecha, idioma, resumen)

    tareas = extraer_tareas(d, contenido_partes_txt, st["clase_id"])
    if tareas:
        print("\n" + "=" * 62)
        print("TAREAS DETECTADAS EN ESTA CLASE:")
        print(tareas.strip())
        print("DESCARGA los archivos mencionados (notebooks/datasets) AHORA")
        print("y pide al agente resolverlos / exportar a PDF.")
        print("=" * 62 + "\n")

    final_txt = (d / "leccion_final.md").read_text(encoding="utf-8")
    english_generated = bool(idioma == "en" and generar_ingles)
    if english_generated:
        print("[post] clase mayormente en ingles: generando leccion_final_eng.md...")
        generar_leccion_ingles(d, final_txt, st)
    elif idioma == "en":
        print("[post] versión inglesa omitida; usa --english si necesitas una copia adicional")

    if paquete_ligero:
        print(
            "[post] paquete ligero: validación pendiente; la evidencia se conserva "
            "hasta terminar memoria, tareas y contabilidad")
    elif limpiar_al_final and barrera_final.get("ready"):
        limpiar(d, cobertura=cobertura)
    elif limpiar_al_final:
        print("[post] limpieza: bloqueada porque la consolidacion es parcial")
    else:
        print("[post] limpieza: omitida (la conservacion de evidencia es el default)")
    if modo_estudio and limpiar_al_final and barrera_final.get("ready") \
            and not paquete_ligero:
        aplicar_modo_estudio(d)
    elif modo_estudio and not paquete_ligero:
        print("[modo-estudio] omitido: requiere --limpiar explicito.")

    print(f"[post] LISTO: {d / 'leccion_final.md'}")
    if english_generated:
        print(f"[post] version inglesa: {d / 'leccion_final_eng.md'}")
    print(f"[post] memoria del curso actualizada: "
          f"{paths['resumenes_dir'] / ('resumen_' + st['clase_id'] + '.md')}")
    gastos = costos_api.resumen_curso(
        str(paths["curso"]), ahora=time.time(), clase_actual=d)
    for linea in _lineas_gasto_api(gastos):
        print(linea)
    if paquete_ligero:
        sin_audio = (
            int(cobertura.get("wav_total", 0) or 0) == 0
            and int(cobertura.get("transcript_wav_faltante_total", 0) or 0) == 0
        )
        evidence_safe = bool(
            barrera_final.get("ready")
            and (
                cobertura.get("limpieza_segura")
                or _wav_limpieza_segura_por_chunks(cobertura)
                or sin_audio
            )
        )
        if evidence_safe:
            reporte_paquete = study_package.finalize_lightweight_package(
                d, evidence_safe=True)
            print(
                "[post] paquete ligero listo: leccion_final.md + img/; "
                f"{reporte_paquete['images_kept']} imágenes referenciadas conservadas; "
                f"{reporte_paquete['paths_removed']} rutas auxiliares eliminadas; "
                f"{reporte_paquete['bytes_removed'] / 1024 / 1024:.1f} MiB liberados")
        else:
            reasons = _razones_compactacion_retenida(
                barrera_final, cobertura, sin_audio=sin_audio)
            print(
                "[post] ADVERTENCIA: leccion_final.md está lista; compactación "
                "retenida y evidencia íntegra conservada: "
                + ", ".join(reasons))
    return

def evaluar_evidencia_frame_video(
        img: Image.Image, evidencias: list[dict], *,
        texto_ocr_fn=None, cambio_evidencia_fn=None) -> dict:
    """Decide si un frame aporta evidencia usando OCR completo antes de colapsar dHash."""
    if texto_ocr_fn is None or cambio_evidencia_fn is None:
        from . import ocr as ocr_local
        texto_ocr_fn = texto_ocr_fn or ocr_local.texto_de_pil
        cambio_evidencia_fn = cambio_evidencia_fn or ocr_local.cambio_evidencia
    texto_ocr = str(texto_ocr_fn(img) or "")
    dhash_int = imagen.dhash(img)
    dhash16_int = imagen.dhash(img, tam=16)
    cercanas = [
        evidencia for evidencia in evidencias
        if imagen.distancia(dhash_int, int(evidencia["dhash_int"])) <= config.UMBRAL_DHASH
    ]
    umbral_secundario = max(8, int(config.UMBRAL_DHASH) * 4)
    coincidente = next((
        evidencia for evidencia in reversed(cercanas)
        if not cambio_evidencia_fn(
            str(evidencia.get("ocr", "") or ""), texto_ocr)
        and imagen.distancia(
            dhash16_int, int(evidencia.get("dhash16_int", dhash16_int))
        ) <= umbral_secundario
    ), None)
    duplicada = coincidente is not None
    return {
        "guardar": not duplicada,
        "dhash_int": dhash_int,
        "dhash": f"{dhash_int:016x}",
        "dhash16_int": dhash16_int,
        "dhash16": f"{dhash16_int:064x}",
        "ocr": texto_ocr,
        "candidatas_similares": len(cercanas),
        "ref_archivo": str((coincidente or {}).get("archivo", "") or ""),
        "motivo": (
            "ocr_cambio_evidencia" if cercanas and not duplicada
            else "duplicada_ocr_dhash" if duplicada
            else "dhash_nuevo"
        ),
    }


def evento_timeline_frame_video(evidencia: dict, t: float, parte: int = 1) -> dict | None:
    """Materializa una reaparicion deduplicada sin duplicar el archivo de imagen."""
    ref = str(evidencia.get("ref_archivo", "") or "")
    if evidencia.get("guardar") or not ref:
        return None
    return {
        "t": float(t),
        "archivo": ref,
        "ref_archivo": ref,
        "parte": int(parte),
        "dhash": str(evidencia.get("dhash", "") or ""),
        "dhash16": str(evidencia.get("dhash16", "") or ""),
        "evento": "heartbeat",
        "motivo": "video_frame_referido",
    }


def idioma_whisper_ingesta_video(configurado: str | None = None) -> str | None:
    """Solo fuerza los idiomas soportados; `auto` conserva deteccion de Whisper."""
    valor = str(config.IDIOMA if configurado is None else configurado).strip().lower()
    return valor if valor in {"es", "en"} else None


def ingestar_video(ruta_video: str, nombre: str) -> Path:
    """Retirado: la única ingesta STT válida nace de WAV canónicos sellados."""
    raise RuntimeError(
        "La ingesta --video con Whisper local fue retirada. Reproduce el video "
        "mediante la captura normal para obtener WAV canónicos y STT remoto.")




def main() -> int:
    config.stdout_utf8()
    ap = argparse.ArgumentParser(description="Consolidación post-clase")
    ap.add_argument("--parte", type=int, help="consolidar solo esta parte")
    ap.add_argument("--todo", action="store_true", help="todas las partes + lección final")
    ap.add_argument("--forzar", action="store_true", help="regenerar aunque exista")
    ap.add_argument("--video", help="ingesta una grabación mp4/mkv (p. ej. la de Meet)")
    ap.add_argument("--nombre", default="", help="nombre de la clase (con --video)")
    ap.add_argument("--clase", help="ruta de la carpeta de clase (default: la activa)")
    ap.add_argument("--limpiar", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--sin-limpiar", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--modo-estudio", action="store_true",
                    help="archiva auxiliares en _debug/*.zip y conserva solo material de estudio")
    ap.add_argument(
        "--paquete-ligero", action="store_true",
        help="compatibilidad: el paquete ligero ya es el cierre predeterminado",
    )
    ap.add_argument(
        "--conservar-evidencia", action="store_true",
        help=("auditoría opt-in: conserva WAV, transcript, manifests y capturas; "
              "el cierre normal deja solo leccion_final.md e imágenes publicadas"),
    )
    ap.add_argument("--parcial", action="store_true",
                    help="genera salida marcada parcial aunque captura/STT no hayan cerrado")
    ap.add_argument(
        "--material", action="append", default=[], metavar="RUTA",
        help=("PDF, PPTX, imagen o directorio de slides oficiales; repetible. También se admite "
              "CLASE_POST_MATERIALES o materiales_oficiales.json en la clase."),
    )
    ap.add_argument(
        "--english", action="store_true",
        help=("genera además leccion_final_eng.md; es opt-in porque añade llamadas LLM y "
              "tiempo de postproceso"),
    )
    ap.add_argument("--limpiar-debug", action="store_true",
                    help="borra _debug/_archivo de la clase (definitivo; requiere opción explícita)")
    ap.add_argument("--cerrar-curso", metavar="NOMBRE",
                    help='fin de curso: registra en memoria_cursos.md, ej: --cerrar-curso "IA 2026-II"')
    args = ap.parse_args()

    if args.cerrar_curso:
        cerrar_curso(args.cerrar_curso)
        return 0

    if args.video:
        if not args.nombre:
            print("Con --video debes pasar --nombre \"Semana X - tema\"")
            return 2
        ingestar_video(args.video, args.nombre)
        return 0

    d = Path(args.clase) if args.clase else est.clase_activa()
    if args.limpiar_debug:
        if d is None or not d.is_dir():
            print("No hay carpeta de clase válida para --limpiar-debug.")
            return 1
        limpiar_debug(d)
        return 0

    if d is None or not (d / "session_state.json").is_file():
        print("No hay clase activa ni --clase válida.")
        return 1

    if args.parte:
        st = est.leer_estado(d)
        origen = _origen_postproceso(d, st)
        notas = [m["nota"] for m in est.leer_marcas(d) if m["tipo"] == "fin_parte"]
        for parte, a, b in est.rangos_partes(d):
            if parte == args.parte:
                nota = notas[parte - 1] if parte - 1 < len(notas) else ""
                consolidar_parte(
                    d, parte, a, b, origen, nota, args.forzar,
                    materiales=args.material,
                )
                return 0
        print(f"parte {args.parte} no encontrada")
        return 1

    paquete_ligero = not bool(args.conservar_evidencia or args.sin_limpiar)
    consolidar_todo(d, args.forzar,
                    limpiar_al_final=paquete_ligero,
                    modo_estudio=args.modo_estudio,
                    paquete_ligero=paquete_ligero,
                    permitir_parcial=args.parcial,
                    materiales=args.material,
                    generar_ingles=args.english)
    return 0


if __name__ == "__main__":
    try:
        _exit_code = main()
    finally:
        from . import ocr as _ocr
        _ocr.cerrar_motor()
    sys.exit(_exit_code)
