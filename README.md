# ClaseViva

**A local-first class capture system that turns a live lecture into a complete, study-ready lesson — not a summary.**

ClaseViva records a class's audio and screen while it happens, transcribes it through a
failover chain of speech-to-text providers, takes structured notes every five minutes,
and afterwards consolidates **everything that was said** — developments, questions,
examples and code — into a single self-contained Markdown/HTML lesson, with the full
transcript embedded and verified inside it.

Built and used in production by its author for his own Data Science master's classes
since July 2026: 13 classes processed end to end.

> 🇪🇸 *Versión en español más abajo · Manual técnico completo en [MANUAL_TECNICO.md](MANUAL_TECNICO.md)*

![Python](https://img.shields.io/badge/python-3.12-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![Tests](https://img.shields.io/badge/tests-952%20passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

## Why it exists

Most transcription tools hand you a summary and throw away the source. ClaseViva was
designed around the opposite principle: **the final lesson must contain everything that
was said in class, structured and enriched for detailed study — and the raw transcript
must survive inside the lesson itself**, so the paraphrase never becomes the only record.

## How it works

```text
Audio (30 s chunks) ──► STT failover chain ──► incremental transcript
Screen captures ──────► visual evidence (WebP, hash-verified)
                              │
        live notes every 5 min (DeepSeek Flash, no tools)
                              │
        Consolidate ──► detailed per-segment development
                        + full transcript embedded in the lesson
                        + coverage check per segment (≥ 35 % of prose,
                          one retry, visible warning if still short)
                        + transactional compaction with rollback
                              │
              leccion_final.md / leccion_final.html + img/
```

- **STT failover, sequential by design:** AssemblyAI Universal-3.5 Pro → Deepgram
  Nova-3 → Speechmatics Melia-1 → Groq Whisper Large v3. A valid commit is never
  re-transcribed or re-billed; a 402 suspends a provider for an hour while the next
  one keeps working.
- **Local voice activity:** Silero VAD runs on CPU (no PyTorch). Since the November
  2026 fidelity pass, only *digitally silent* audio (all-zero samples) is discarded —
  quiet speech is always kept and sent.
- **One LLM, deliberately:** DeepSeek Flash via `chat.completions`, no tools,
  `thinking=False`. Web context is fetched directly (Bing RSS / DuckDuckGo + page
  download) at zero tool cost — never through paid search endpoints.
- **OCR is local and free** (Windows OCR, RapidOCR fallback).

## Engineering highlights

- **Append-only audio manifest with SHA-256** per chunk; atomic writes
  (tmp → fsync → replace); per-process state locks. An abrupt shutdown loses at most
  the unsealed 30-second chunk, and consolidation resumes from valid commits.
- **Transactional cleanup:** the full transcript is embedded in the final lesson
  (readable appendix + base64 payload with segment count and SHA-256) and *validated*
  before any working file is deleted; deletion itself is staged with a manifest and
  rollback, and interrupted cleanups are recovered automatically.
- **Code survives verbatim:** code fences are protected with hash tokens through the
  merge/sanitize pipeline, so capitalization, indentation and comments reach the
  lesson byte-for-byte.
- **Cost accounting that reconciles:** every API call is logged with token counts
  (including prefix-cache hits) and a versioned price estimate. Reconciled against
  DeepSeek's official usage export for 2026-11-14: **116 calls, hit/miss/output
  tokens identical to the token**, estimate within ~7 % of the invoiced amount.

## Measured cost (author's real usage, Jul–Nov 2026)

| Metric | Value |
|---|---:|
| LLM calls logged | 705 |
| Total LLM spend | **US$ 1.57** |
| Average per month | ≈ US$ 0.42 |
| Most expensive class day (6.2 h class, 116 calls) | US$ 0.27 estimated / ≈ US$ 0.26 invoiced |
| Speech-to-text | US$ 0 (free tiers of all four providers) |

## Tests

952 tests passing (plus one hypothesis property test that runs without its 5-second
deadline). The suite covers the audio manifest, STT failover, consolidation,
coverage checks, transactional cleanup and cost accounting.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

## Quick start (from source, Windows, Python 3.12)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env   # fill in your API keys
.\ClaseViva.vbs               # opens the local panel at http://127.0.0.1:8756
```

A portable Windows build (`ClaseViva.exe`, no Python required) is described in
[WINDOWS_PORTABLE.md](WINDOWS_PORTABLE.md).

## Known limitations (declared, not hidden)

- Batch by design: 30-second audio chunks and 5-minute note intervals — the live
  lesson is a structured summary; the *final* lesson is the complete record.
- A single sequential STT worker: one pathological chunk can delay the queue
  (measured impact on real classes: median transcription 6.1 s per chunk, p95 12.4 s).
- Two large modules (`panel.py`, `post_procesar.py`) remain from the app's growth;
  prompt extraction is planned. Style lint is being cleaned up progressively
  (critical rules pass; no correctness findings).

Full audit history and design contracts live in `PROJECT_STATE.md` and `history/`.

## License

MIT — see [LICENSE](LICENSE).

---

# ClaseViva (español)

**Un sistema local de captura de clases que convierte una clase en vivo en una lección
completa y lista para estudiar — no en un resumen.**

ClaseViva graba el audio y la pantalla de la clase mientras ocurre, la transcribe con
una cadena de proveedores STT con failover secuencial, toma apuntes estructurados cada
cinco minutos y, al terminar, consolida **todo lo dicho** —desarrollos, preguntas,
ejemplos y código— en una lección Markdown/HTML autocontenida, con la transcripción
íntegra incrustada y verificada dentro de ella.

- **Principio de diseño:** la paráfrasis nunca es el único registro; la fuente
  (transcripción completa) viaja dentro de la lección final.
- **Durabilidad:** manifiesto de audio append-only con SHA-256, escrituras atómicas,
  limpieza transaccional con rollback y recuperación de consolidaciones interrumpidas.
- **Costo medido real:** US$ 1,57 en 705 llamadas (jul–nov 2026); STT a costo cero
  en cuotas gratuitas; contabilidad de tokens conciliada al detalle contra la
  facturación oficial del proveedor.
- **Calidad verificada:** 952 pruebas; el manual técnico completo, los contratos de
  diseño y el historial de auditorías están en el repositorio.

Instalación, flujo de datos y módulos clave: [MANUAL_TECNICO.md](MANUAL_TECNICO.md).

---

*Created by Angel Christopher Ramirez Gomero — molecular biologist & data science
master's student, building at the intersection of the genetic code and the other kind
of code. ([Doble Código](https://github.com/egomaniac95))*
