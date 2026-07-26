"""Cliente DeepSeek V4 Flash: reintentos, log de costos, y NUNCA crashea al llamador.

Uso:  from src.deepseek_client import preguntar
      texto = preguntar([{"role": "user", "content": "hola"}])   # -> str | None
Ping: .venv\\Scripts\\python.exe -m src.deepseek_client --ping
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config

_client = None
_USAGE_LOG_LOCK = threading.Lock()


@contextmanager
def _usage_file_lock(path: Path, timeout: float = 5.0):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + max(0.1, float(timeout))
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timeout bloqueando uso_api.jsonl")
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _cliente():
    global _client
    if _client is None:
        if not config.DEEPSEEK_API_KEY:
            raise RuntimeError("Falta DEEPSEEK_API_KEY en .env (copiar .env.example a .env)")
        from openai import OpenAI
        _client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=float(os.environ.get("CLASE_DEEPSEEK_TIMEOUT", "30")),
            max_retries=0,  # los reintentos los manejamos nosotros con backoff
        )
    return _client


def _ruta_relativa(p: Path) -> str:
    try:
        return p.relative_to(config.ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _contexto_uso_api() -> dict:
    """Metadata local para atribuir costo sin exponer secretos ni prompts."""
    try:
        from . import estado as est
        d = est.clase_activa()
        if d is None:
            return {}
        st = est.leer_estado(d)
        return {
            "curso": st.get("curso") or est.curso_de(d),
            "clase_id": st.get("clase_id") or d.name,
            "clase_path": _ruta_relativa(d),
            "parte": st.get("parte", 0),
        }
    except Exception:
        return {}


def _tokens_cache_prefijo(usage) -> dict:
    """Copia los contadores de caché por prefijo que reporte el proveedor.

    Solo registra lo que la API devuelve (nunca deriva valores): DeepSeek expone
    prompt_cache_hit_tokens/prompt_cache_miss_tokens y, en el formato OpenAI,
    prompt_tokens_details.cached_tokens. Sin estos campos el costo real del
    input (hit ~10x más barato) es inauditable.
    """
    fila: dict = {}
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    miss = getattr(usage, "prompt_cache_miss_tokens", None)
    if hit is None:
        detalles = getattr(usage, "prompt_tokens_details", None)
        hit = getattr(detalles, "cached_tokens", None)
    if isinstance(hit, int):
        fila["tokens_cache_hit"] = hit
    if isinstance(miss, int):
        fila["tokens_cache_miss"] = miss
    return fila


def _log_uso(respuesta, etiqueta: str, usage_id: str | None = None) -> None:
    try:
        u = respuesta.usage
        costo = (u.prompt_tokens * config.COSTO_ENTRADA_USD_M
                 + u.completion_tokens * config.COSTO_SALIDA_USD_M) / 1_000_000
        config.ensure_dirs()
        fila = {
            "t": time.time(), "etiqueta": etiqueta, "modelo": config.DEEPSEEK_MODEL,
            "tokens_entrada": u.prompt_tokens, "tokens_salida": u.completion_tokens,
            "costo_usd": round(costo, 6),
            "usage_id": str(
                usage_id or getattr(respuesta, "id", "") or uuid.uuid4().hex),
        }
        fila.update(_tokens_cache_prefijo(u))
        fila.update(_contexto_uso_api())
        path = config.LOGS_DIR / "uso_api.jsonl"
        with _USAGE_LOG_LOCK, _usage_file_lock(path):
            if path.is_file():
                for linea in path.read_text(encoding="utf-8").splitlines():
                    try:
                        existente = json.loads(linea)
                    except json.JSONDecodeError:
                        continue
                    if existente.get("usage_id") == fila["usage_id"]:
                        return
            raw = (json.dumps(fila, ensure_ascii=False) + "\n").encode("utf-8")
            with open(path, "ab") as archivo:
                archivo.write(raw)
                archivo.flush()
                os.fsync(archivo.fileno())
    except Exception:
        pass


def _timeout_efectivo(max_tokens: int, timeout: float | None) -> float:
    if timeout is None:
        return 120 if max_tokens >= 3000 else float(
            os.environ.get("CLASE_DEEPSEEK_TIMEOUT", "30"))
    return timeout


def _sanear_error(e: Exception) -> str:
    msg = f"{type(e).__name__}: {e}"
    key = config.DEEPSEEK_API_KEY or ""
    if key:
        msg = msg.replace(key, "[API_KEY]")
    return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", msg)


def preguntar_detalle(mensajes: list[dict], max_tokens: int = 2048,
                      temperature: float = 0.3, etiqueta: str = "",
                      timeout: float | None = None) -> dict:
    """Llama a la API y devuelve texto + finish_reason sin lanzar al llamador."""
    timeout = _timeout_efectivo(max_tokens, timeout)
    ultimo_error = ""
    usage_id = uuid.uuid4().hex
    for intento in range(3):
        try:
            r = _cliente().chat.completions.create(
                model=config.DEEPSEEK_MODEL,
                messages=mensajes,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
            )
            _log_uso(r, etiqueta, usage_id=usage_id)
            choice = r.choices[0]
            texto = choice.message.content if choice.message else None
            return {
                "texto": texto,
                "finish_reason": getattr(choice, "finish_reason", None),
                "error": None,
            }
        except Exception as e:
            ultimo_error = _sanear_error(e)
            espera = 2 * (2 ** intento)
            print(f"[deepseek] intento {intento + 1}/3 falló ({ultimo_error}); "
                  f"reintento en {espera}s", flush=True)
            time.sleep(espera)
    config.log_error("deepseek", f"3 intentos agotados para etiqueta={etiqueta!r}: "
                     f"{ultimo_error}")
    return {"texto": None, "finish_reason": None,
            "error": ultimo_error or "3 intentos agotados"}


def preguntar(mensajes: list[dict], max_tokens: int = 2048, temperature: float = 0.3,
              etiqueta: str = "", timeout: float | None = None) -> str | None:
    """Llama a la API con 3 intentos y backoff. Devuelve el texto o None (jamás lanza)."""
    return preguntar_detalle(
        mensajes, max_tokens=max_tokens, temperature=temperature,
        etiqueta=etiqueta, timeout=timeout).get("texto")


def costo_acumulado() -> float:
    total = 0.0
    p = config.LOGS_DIR / "uso_api.jsonl"
    if p.is_file():
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                total += json.loads(ln).get("costo_usd", 0.0)
            except Exception:
                continue
    return total


def main() -> int:
    config.stdout_utf8()
    if "--ping" in sys.argv:
        r = preguntar([{"role": "user", "content": "Responde únicamente: OK"}],
                      max_tokens=5, temperature=0.0, etiqueta="ping")
        if r is not None:
            print(f"PING_OK modelo={config.DEEPSEEK_MODEL} respuesta={r.strip()!r}")
            return 0
        print("PING_FALLO: la API no respondió (revisar key, saldo o red)")
        return 1
    print("Uso: python -m src.deepseek_client --ping")
    return 2


if __name__ == "__main__":
    sys.exit(main())
