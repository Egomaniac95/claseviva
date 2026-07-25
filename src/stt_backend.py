"""STT remoto secuencial, recuperable y sin dependencias de inferencia local.

Los adaptadores reciben únicamente audio derivado en memoria. El WAV canónico,
su hash, el manifest y el commit continúan siendo autoridad local. Ningún
preflight hace red y ninguna excepción incorpora credenciales o cuerpos remotos.
"""
from __future__ import annotations

import io
import math
import os
import time
import uuid
import wave
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping

import httpx
import numpy as np


DEFAULT_BACKEND = "remote"
ALLOWED_BACKENDS = {"remote", "groq"}  # groq: alias de lectura para sesiones previas
PROVIDER_ORDER = ("assemblyai", "deepgram", "speechmatics", "groq")
PROVIDER_KEYS = {
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "speechmatics": "SPEECHMATICS_API_KEY",
    "groq": "GROQ_API_KEY",
}
ASSEMBLYAI_MODEL = "universal-3-5-pro"
DEEPGRAM_MODEL = "nova-3"
SPEECHMATICS_MODEL = "melia-1"
GROQ_MODEL = "whisper-large-v3"
PROVIDER_MODELS = {
    "assemblyai": ASSEMBLYAI_MODEL,
    "deepgram": DEEPGRAM_MODEL,
    "speechmatics": SPEECHMATICS_MODEL,
    "groq": GROQ_MODEL,
}
CHAIN_LABEL = "AssemblyAI → Deepgram → Speechmatics → Groq"


class RemoteSttError(RuntimeError):
    """Fallo normalizado y seguro para journal, failover y reinicio."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        model: str = "",
        status_code: int | None = None,
        retryable: bool = True,
        retry_after_seconds: float | None = None,
        error_class: str = "remote_error",
        advance_provider: bool = True,
        request_id: str = "",
        pending_job: bool = False,
        ambiguous_submission: bool = False,
        invalid_audio: bool = False,
    ) -> None:
        # ``message`` debe ser construido por este módulo, nunca con body/headers.
        super().__init__(str(message))
        self.provider = str(provider)
        self.model = str(model)
        self.status_code = status_code
        self.retryable = bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        self.error_class = str(error_class)
        self.advance_provider = bool(advance_provider)
        self.request_id = str(request_id or "")
        self.pending_job = bool(pending_job)
        self.ambiguous_submission = bool(ambiguous_submission)
        self.invalid_audio = bool(invalid_audio)


class RemoteJobPending(RemoteSttError):
    def __init__(self, provider: str, model: str, request_id: str,
                 *, retry_after_seconds: float = 5.0,
                 ambiguous_submission: bool = False) -> None:
        super().__init__(
            f"{provider}: trabajo remoto pendiente; se recuperará sin reenviar audio",
            provider=provider, model=model, request_id=request_id,
            retryable=True, retry_after_seconds=retry_after_seconds,
            error_class=("submission_unknown" if ambiguous_submission else "job_pending"),
            advance_provider=False, pending_job=True,
            ambiguous_submission=ambiguous_submission,
        )


class FailoverExhausted(RemoteSttError):
    def __init__(self, trace: list[dict], retry_after_seconds: float = 60.0) -> None:
        super().__init__(
            "los cuatro proveedores STT fallaron; WAV conservado como pendiente",
            provider="chain", model="remote-chain", retryable=True,
            retry_after_seconds=retry_after_seconds,
            error_class="chain_exhausted", advance_provider=False,
        )
        self.trace = [dict(item) for item in trace]


@dataclass(frozen=True)
class RemoteSegment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None


def configured_backend(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = str(env.get("CLASE_STT_BACKEND", DEFAULT_BACKEND)).strip().casefold()
    return "remote" if value in ALLOWED_BACKENDS else DEFAULT_BACKEND


def preflight(*, backend: str | None = None, remote_consent: bool,
              environ: Mapping[str, str] | None = None) -> dict:
    """Comprueba consentimiento y presencia de claves sin realizar red."""
    env = os.environ if environ is None else environ
    raw = str(backend or configured_backend(env)).strip().casefold()
    if raw == "local":
        return {
            "ok": False, "backend": raw,
            "message": "Whisper local fue retirado. Selecciona la cadena STT remota.",
        }
    if raw not in ALLOWED_BACKENDS:
        return {
            "ok": False, "backend": raw,
            "message": f"Backend STT desconocido: {raw}. Usa 'remote'.",
        }
    if not bool(remote_consent):
        return {
            "ok": False, "backend": "remote",
            "message": (
                "Autoriza el envío de WAV sellados a la cadena remota "
                "AssemblyAI → Deepgram → Speechmatics → Groq."),
        }
    available = [
        provider for provider in PROVIDER_ORDER
        if str(env.get(PROVIDER_KEYS[provider], "")).strip()
    ]
    if not available:
        names = ", ".join(PROVIDER_KEYS[p] for p in PROVIDER_ORDER)
        return {
            "ok": False, "backend": "remote", "providers_available": [],
            "message": f"Falta al menos una clave STT en .env: {names}.",
        }
    return {
        "ok": True, "backend": "remote-chain", "provider": "remote-chain",
        "model": "remote-failover", "providers_available": available,
        "providers_unavailable": [p for p in PROVIDER_ORDER if p not in available],
        "message": "Cadena STT configurada; el preflight no realizó llamadas de red.",
    }


def _audio_upload(audio) -> tuple[str, bytes]:
    if isinstance(audio, (str, os.PathLike)):
        with open(audio, "rb") as handle:
            return os.path.basename(os.fspath(audio)) or "audio.wav", handle.read()
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
    pcm = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype("<i2")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm.tobytes())
    return "chunk.wav", stream.getvalue()


def wav_has_useful_signal(audio, silence_rms: int = 120) -> tuple[bool, dict]:
    """Gate conservador equivalente al capturador, antes de normalizar/WSOLA."""
    _name, payload = _audio_upload(audio)
    try:
        with wave.open(io.BytesIO(payload), "rb") as wav:
            width, channels = wav.getsampwidth(), max(1, wav.getnchannels())
            raw = wav.readframes(wav.getnframes())
        if width != 2:
            return True, {"reason": "unsupported_width", "rms": None, "peak": None}
        samples = np.frombuffer(raw, dtype="<i2")
        usable = samples.size - samples.size % channels
        if usable <= 0:
            return False, {"reason": "empty", "rms": 0.0, "peak": 0}
        mono = samples[:usable].reshape(-1, channels).astype(np.float64).mean(axis=1)
        # Muestreo equivalente al cálculo barato del capturador.
        probe = mono[::15] if mono.size else mono
        rms = float(math.sqrt(float(np.mean(np.square(probe))))) if probe.size else 0.0
        peak = int(np.max(np.abs(probe))) if probe.size else 0
        useful = bool(
            rms >= silence_rms
            or (rms >= max(12.0, silence_rms * 0.25)
                and peak >= max(100, silence_rms * 2)))
        return useful, {
            "reason": "useful" if useful else "silence",
            "rms": round(rms, 3), "peak": peak,
        }
    except (EOFError, OSError, wave.Error, ValueError):
        # Audio inválido debe llegar al adaptador y quedar pendiente, no tratarse
        # como silencio exitoso ni multiplicarse por cuatro proveedores.
        return True, {"reason": "unreadable", "rms": None, "peak": None}


def audio_duration_seconds(audio) -> float:
    """Duración del derivado realmente entregado al proveedor."""
    if isinstance(audio, (str, os.PathLike)):
        try:
            with wave.open(os.fspath(audio), "rb") as wav:
                rate = int(wav.getframerate())
                return wav.getnframes() / rate if rate > 0 else 0.0
        except (EOFError, OSError, wave.Error):
            return 0.0
    return np.asarray(audio).reshape(-1).size / 16000.0


def _float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _retry_after(response) -> float | None:
    value = (getattr(response, "headers", {}) or {}).get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_for_http(provider: str, model: str, response,
                    *, accepted: bool = False, request_id: str = "") -> RemoteSttError:
    status = int(getattr(response, "status_code", 0) or 0)
    auth = status in {401, 403}
    retryable = status in {408, 409, 425, 429, 498, 500, 502, 503, 504}
    error_class = (
        "authorization" if auth else
        "quota_or_rate_limit" if status in {429, 498} else
        "server_error" if status >= 500 else "http_error")
    return RemoteSttError(
        f"{provider}: HTTP {status or 'desconocido'} ({error_class})",
        provider=provider, model=model, status_code=status or None,
        retryable=retryable, retry_after_seconds=_retry_after(response),
        error_class=error_class, advance_provider=not accepted,
        request_id=request_id, pending_job=accepted,
    )


def _request(client, method: str, url: str, *, provider: str, model: str,
             accepted: bool = False, request_id: str = "",
             post_may_create_billable_job: bool = True, **kwargs):
    method = method.upper()
    try:
        response = client.request(method, url, **kwargs)
    except (httpx.ConnectTimeout, httpx.PoolTimeout) as error:
        raise RemoteSttError(
            f"{provider}: timeout de conexión antes de aceptación",
            provider=provider, model=model, retryable=True,
            error_class="connect_timeout", advance_provider=True,
        ) from error
    except (httpx.ReadTimeout, httpx.WriteTimeout) as error:
        if accepted or (method == "POST" and post_may_create_billable_job):
            raise RemoteJobPending(
                provider, model, request_id,
                ambiguous_submission=not bool(request_id)) from error
        raise RemoteSttError(
            f"{provider}: timeout de transporte recuperable",
            provider=provider, model=model, retryable=True,
            error_class="transport_timeout", advance_provider=True,
        ) from error
    except (httpx.NetworkError, OSError) as error:
        # Un error de protocolo después de empezar a escribir un POST no prueba
        # que el proveedor lo haya rechazado. Avanzar aquí podría cobrar dos
        # transcripciones; se estaciona hasta reconciliar o decidir manualmente.
        if accepted or (method == "POST" and post_may_create_billable_job):
            raise RemoteJobPending(
                provider, model, request_id,
                ambiguous_submission=not bool(request_id)) from error
        raise RemoteSttError(
            f"{provider}: fallo de red antes de respuesta",
            provider=provider, model=model, retryable=True,
            error_class="network_error", advance_provider=True,
        ) from error
    except httpx.HTTPError as error:
        if accepted or (method == "POST" and post_may_create_billable_job):
            raise RemoteJobPending(
                provider, model, request_id,
                ambiguous_submission=not bool(request_id)) from error
        raise RemoteSttError(
            f"{provider}: fallo HTTP recuperable antes de aceptación",
            provider=provider, model=model, retryable=True,
            error_class="http_transport_error", advance_provider=True,
        ) from error
    if int(response.status_code) >= 400:
        raise _error_for_http(
            provider, model, response, accepted=accepted, request_id=request_id)
    return response


def _json(response, provider: str, model: str) -> dict:
    try:
        value = response.json()
    except (TypeError, ValueError) as error:
        raise RemoteSttError(
            f"{provider}: respuesta JSON inválida",
            provider=provider, model=model, retryable=False,
            error_class="invalid_response", advance_provider=True,
        ) from error
    if not isinstance(value, dict):
        raise RemoteSttError(
            f"{provider}: estructura de respuesta inválida",
            provider=provider, model=model, retryable=False,
            error_class="invalid_response", advance_provider=True,
        )
    return value


def _segments_from_words(words: Iterable[dict], *, milliseconds: bool = False,
                         fallback_text: str = "") -> list[RemoteSegment]:
    scale = 0.001 if milliseconds else 1.0
    out: list[RemoteSegment] = []
    bucket: list[str] = []
    start = end = 0.0
    for word in words or []:
        text = str(word.get("text") or word.get("word") or word.get("content") or "").strip()
        if not text:
            continue
        w_start = max(0.0, _float(word.get("start", word.get("start_time"))) * scale)
        w_end = max(w_start, _float(word.get("end", word.get("end_time")), w_start) * scale)
        if not bucket:
            start = w_start
        gap = w_start - end if bucket else 0.0
        if bucket and (gap > 0.9 or end - start >= 12.0):
            out.append(RemoteSegment(start, end, " ".join(bucket).strip()))
            bucket, start = [], w_start
        if text in {".", ",", ";", ":", "?", "!"} and bucket:
            bucket[-1] += text
        else:
            bucket.append(text)
        end = w_end
        if bucket and text.endswith((".", "?", "!")) and end - start >= 1.0:
            out.append(RemoteSegment(start, end, " ".join(bucket).strip()))
            bucket = []
    if bucket:
        out.append(RemoteSegment(start, end, " ".join(bucket).strip()))
    if not out and str(fallback_text).strip():
        out.append(RemoteSegment(0.0, 0.0, str(fallback_text).strip()))
    return out


class _HttpProvider:
    provider = ""
    model = ""
    whole_chunk = True
    cross_chunk_overlap = False

    def __init__(self, *, api_key: str, client=None, timeout_seconds: float = 45.0,
                 base_url: str = "") -> None:
        if not str(api_key or "").strip():
            raise ValueError(f"falta {PROVIDER_KEYS[self.provider]}")
        self.api_key = str(api_key)
        self.base_url = str(base_url).rstrip("/")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(float(timeout_seconds)), follow_redirects=False)
        self.last_request_id = ""


class AssemblyAIBackend(_HttpProvider):
    provider, model = "assemblyai", ASSEMBLYAI_MODEL

    def __init__(self, **kwargs):
        kwargs.setdefault("base_url", "https://api.assemblyai.com")
        super().__init__(**kwargs)

    def transcribe(self, audio, *, resume=None, on_state=None, **_kwargs):
        resume, on_state = dict(resume or {}), on_state or (lambda *_a, **_k: None)
        upload_url = str(resume.get("upload_url") or "")
        job_id = str(resume.get("request_id") or resume.get("transcript_id") or "")
        headers = {"Authorization": self.api_key}
        if (not job_id and upload_url
                and str(resume.get("state") or resume.get("status") or "")
                == "submission_unknown"):
            # El submit pudo ser aceptado aunque su respuesta se perdiera. La API
            # permite listar transcripts; no se repite el POST sin reconciliar.
            response = _request(
                self.client, "GET", f"{self.base_url}/v2/transcript?limit=100",
                provider=self.provider, model=self.model, accepted=True,
                headers=headers)
            listed = _json(response, self.provider, self.model)
            candidates = listed.get("transcripts") or listed.get("items") or []
            match = next((row for row in candidates
                          if str(row.get("audio_url") or "") == upload_url), None)
            job_id = str((match or {}).get("id") or "")
            if not job_id:
                raise RemoteJobPending(
                    self.provider, self.model, "",
                    ambiguous_submission=True)
            on_state("accepted", request_id=job_id, transcript_id=job_id,
                     upload_url=upload_url, reconciled=True)
        if not job_id:
            if not upload_url:
                _filename, payload = _audio_upload(audio)
                response = _request(
                    self.client, "POST", f"{self.base_url}/v2/upload",
                    provider=self.provider, model=self.model,
                    post_may_create_billable_job=False,
                    headers={**headers, "Content-Type": "application/octet-stream"},
                    content=payload)
                upload_url = str(_json(response, self.provider, self.model).get("upload_url") or "")
                if not upload_url:
                    raise RemoteSttError(
                        "assemblyai: upload sin identificador recuperable",
                        provider=self.provider, model=self.model, retryable=False,
                        error_class="invalid_response")
                on_state("uploaded", upload_url=upload_url)
            try:
                response = _request(
                    self.client, "POST", f"{self.base_url}/v2/transcript",
                    provider=self.provider, model=self.model,
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "audio_url": upload_url,
                        "speech_models": [self.model],
                        "language_detection": True,
                    })
            except RemoteJobPending:
                on_state("submission_unknown", upload_url=upload_url)
                raise
            data = _json(response, self.provider, self.model)
            job_id = str(data.get("id") or "")
            if not job_id:
                raise RemoteSttError(
                    "assemblyai: submit sin job id",
                    provider=self.provider, model=self.model, retryable=False,
                    error_class="invalid_response")
            on_state("accepted", request_id=job_id, transcript_id=job_id,
                     upload_url=upload_url)
        self.last_request_id = job_id
        deadline = time.monotonic() + max(1.0, _float(
            os.environ.get("CLASE_ASSEMBLYAI_POLL_BUDGET_SEG", "90"), 90))
        while True:
            response = _request(
                self.client, "GET", f"{self.base_url}/v2/transcript/{job_id}",
                provider=self.provider, model=self.model, accepted=True,
                request_id=job_id, headers=headers)
            data = _json(response, self.provider, self.model)
            status = str(data.get("status") or "").casefold()
            if status == "completed":
                segments = _segments_from_words(
                    data.get("words") or [], milliseconds=True,
                    fallback_text=str(data.get("text") or ""))
                return segments, SimpleNamespace(
                    language=str(data.get("language_code") or ""),
                    language_probability=1.0, request_id=job_id,
                    provider=self.provider,
                    model=str(data.get("speech_model_used") or self.model))
            if status == "error":
                raise RemoteSttError(
                    "assemblyai: job rechazado por el proveedor",
                    provider=self.provider, model=self.model, retryable=False,
                    error_class="job_rejected", advance_provider=True,
                    request_id=job_id)
            on_state("polling", request_id=job_id, transcript_id=job_id,
                     upload_url=upload_url)
            if time.monotonic() >= deadline:
                raise RemoteJobPending(self.provider, self.model, job_id)
            time.sleep(min(2.0, max(0.1, _float(
                os.environ.get("CLASE_STT_POLL_INTERVAL_SEG", "1"), 1))))


class DeepgramBackend(_HttpProvider):
    provider, model = "deepgram", DEEPGRAM_MODEL

    def __init__(self, **kwargs):
        kwargs.setdefault("base_url", "https://api.deepgram.com")
        super().__init__(**kwargs)

    def transcribe(self, audio, **_kwargs):
        _filename, payload = _audio_upload(audio)
        response = _request(
            self.client, "POST",
            f"{self.base_url}/v1/listen?model={self.model}&language=multi"
            "&smart_format=true&mip_opt_out=true",
            provider=self.provider, model=self.model,
            headers={"Authorization": f"Token {self.api_key}", "Content-Type": "audio/wav"},
            content=payload)
        data = _json(response, self.provider, self.model)
        metadata = data.get("metadata") or {}
        self.last_request_id = str(metadata.get("request_id") or "")
        channels = ((data.get("results") or {}).get("channels") or [])
        alternatives = ((channels[0] if channels else {}).get("alternatives") or [])
        alt = alternatives[0] if alternatives else {}
        utterances = (data.get("results") or {}).get("utterances") or []
        segments = [
            RemoteSegment(_float(row.get("start")), _float(row.get("end")),
                          str(row.get("transcript") or "").strip())
            for row in utterances if str(row.get("transcript") or "").strip()
        ]
        if not segments:
            segments = _segments_from_words(
                alt.get("words") or [], fallback_text=str(alt.get("transcript") or ""))
        return segments, SimpleNamespace(
            language="multi", language_probability=1.0,
            request_id=self.last_request_id, provider=self.provider, model=self.model)


class SpeechmaticsBackend(_HttpProvider):
    provider, model = "speechmatics", SPEECHMATICS_MODEL

    def __init__(self, **kwargs):
        kwargs.setdefault(
            "base_url", os.environ.get(
                "SPEECHMATICS_BASE_URL", "https://us1.asr.api.speechmatics.com"))
        super().__init__(**kwargs)

    def transcribe(self, audio, *, resume=None, on_state=None,
                   audio_sha256: str = "", sequence=None, **_kwargs):
        resume, on_state = dict(resume or {}), on_state or (lambda *_a, **_k: None)
        job_id = str(resume.get("request_id") or resume.get("job_id") or "")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        reference = str(
            resume.get("tracking_reference") or
            f"{sequence or 0}:{audio_sha256}"[:128])
        if (not job_id and reference
                and str(resume.get("state") or resume.get("status") or "")
                == "submission_unknown"):
            response = _request(
                self.client, "GET", f"{self.base_url}/v2/jobs/?limit=100",
                provider=self.provider, model=self.model, accepted=True,
                headers=headers)
            listed = _json(response, self.provider, self.model)
            jobs = listed.get("jobs") or []
            match = next((row for row in jobs
                          if str((row.get("tracking") or {}).get("reference") or "")
                          == reference), None)
            job_id = str((match or {}).get("id") or "")
            if not job_id:
                raise RemoteJobPending(
                    self.provider, self.model, "", ambiguous_submission=True)
            on_state("accepted", request_id=job_id, job_id=job_id,
                     tracking_reference=reference, reconciled=True)
        if not job_id:
            filename, payload = _audio_upload(audio)
            config = {
                "type": "transcription",
                "transcription_config": {"model": self.model, "language": "multi"},
                "tracking": {"reference": reference},
            }
            try:
                response = _request(
                    self.client, "POST", f"{self.base_url}/v2/jobs/?wait=60&format=json-v2",
                    provider=self.provider, model=self.model, headers=headers,
                    files={"data_file": (filename, payload, "audio/wav")},
                    data={"config": __import__("json").dumps(config, separators=(",", ":"))})
            except RemoteJobPending:
                on_state("submission_unknown", tracking_reference=reference)
                raise
            data = _json(response, self.provider, self.model)
            job_id = str(data.get("id") or (data.get("job") or {}).get("id") or "")
            if not job_id:
                raise RemoteSttError(
                    "speechmatics: submit sin job id",
                    provider=self.provider, model=self.model, retryable=False,
                    error_class="invalid_response")
            on_state("accepted", request_id=job_id, job_id=job_id,
                     tracking_reference=reference)
        self.last_request_id = job_id
        deadline = time.monotonic() + max(1.0, _float(
            os.environ.get("CLASE_SPEECHMATICS_POLL_BUDGET_SEG", "90"), 90))
        while True:
            status_response = _request(
                self.client, "GET", f"{self.base_url}/v2/jobs/{job_id}?wait=30",
                provider=self.provider, model=self.model, accepted=True,
                request_id=job_id, headers=headers)
            status_data = _json(status_response, self.provider, self.model)
            status = str((status_data.get("job") or status_data).get("status") or "").casefold()
            if status == "done":
                response = _request(
                    self.client, "GET",
                    f"{self.base_url}/v2/jobs/{job_id}/transcript?format=json-v2",
                    provider=self.provider, model=self.model, accepted=True,
                    request_id=job_id, headers=headers)
                data = _json(response, self.provider, self.model)
                words = []
                for row in data.get("results") or []:
                    alternatives = row.get("alternatives") or []
                    if not alternatives:
                        continue
                    words.append({
                        "content": alternatives[0].get("content", ""),
                        "start_time": row.get("start_time", 0),
                        "end_time": row.get("end_time", 0),
                    })
                return _segments_from_words(words), SimpleNamespace(
                    language="multi", language_probability=1.0,
                    request_id=job_id, provider=self.provider, model=self.model)
            if status in {"rejected", "deleted", "expired"}:
                raise RemoteSttError(
                    "speechmatics: job rechazado por el proveedor",
                    provider=self.provider, model=self.model, retryable=False,
                    error_class="job_rejected", request_id=job_id)
            on_state("polling", request_id=job_id, job_id=job_id)
            if time.monotonic() >= deadline:
                raise RemoteJobPending(self.provider, self.model, job_id)


class GroqWhisperBackend(_HttpProvider):
    provider, model = "groq", GROQ_MODEL

    def __init__(self, **kwargs):
        kwargs.setdefault("base_url", "https://api.groq.com/openai/v1")
        super().__init__(**kwargs)

    def transcribe(self, audio, **kwargs):
        filename, payload = _audio_upload(audio)
        data = {"model": self.model, "response_format": "verbose_json"}
        language = str(kwargs.get("language") or "").strip()
        prompt = str(kwargs.get("initial_prompt") or "").strip()
        if language and language.casefold() != "auto":
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        response = _request(
            self.client, "POST", f"{self.base_url}/audio/transcriptions",
            provider=self.provider, model=self.model,
            headers={"Authorization": f"Bearer {self.api_key}"},
            files={"file": (filename, payload, "audio/wav")}, data=data)
        result = _json(response, self.provider, self.model)
        self.last_request_id = str((result.get("x_groq") or {}).get("id") or "")
        segments = [
            RemoteSegment(
                max(0.0, _float(row.get("start"))),
                max(0.0, _float(row.get("end"))),
                str(row.get("text") or "").strip(),
                avg_logprob=(None if row.get("avg_logprob") is None else _float(row.get("avg_logprob"))),
                no_speech_prob=(None if row.get("no_speech_prob") is None else _float(row.get("no_speech_prob"))),
                compression_ratio=(None if row.get("compression_ratio") is None else _float(row.get("compression_ratio"))),
            )
            for row in result.get("segments") or []
            if str(row.get("text") or "").strip()
        ]
        if not segments:
            segments = _segments_from_words(
                result.get("words") or [], fallback_text=str(result.get("text") or ""))
        return segments, SimpleNamespace(
            language=str(result.get("language") or ""), language_probability=1.0,
            request_id=self.last_request_id, provider=self.provider, model=self.model)


PROVIDER_CLASSES = {
    "assemblyai": AssemblyAIBackend,
    "deepgram": DeepgramBackend,
    "speechmatics": SpeechmaticsBackend,
    "groq": GroqWhisperBackend,
}


class RemoteFailoverBackend:
    """Un único backend lógico; intenta proveedores en orden y nunca hace hedging."""

    provider = "remote-chain"
    model = "remote-failover"
    whole_chunk = True
    cross_chunk_overlap = False

    def __init__(self, providers: list[_HttpProvider], *,
                 max_attempts_per_provider: int = 2,
                 max_inline_retry_seconds: float = 2.0,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.providers = list(providers)
        self.max_attempts_per_provider = max(1, int(max_attempts_per_provider))
        self.max_inline_retry_seconds = max(0.0, float(max_inline_retry_seconds))
        self.sleeper = sleeper
        self.last_request_id = ""
        self.last_provider = ""
        self.last_model = ""
        self.last_language = ""
        self.last_submitted_audio_seconds = 0.0
        self.last_silence_diagnostics: dict = {}
        self._context: dict = {}
        self._circuits: dict[str, float] = {}

    def set_attempt_context(self, *, chunk_id: str, audio_sha256: str,
                            sequence=None, history=None, recorder=None) -> None:
        self._context = {
            "chunk_id": str(chunk_id), "audio_sha256": str(audio_sha256),
            "sequence": sequence, "history": list(history or []),
            "recorder": recorder,
        }
        now = time.time()
        for row in self._context["history"]:
            provider = str(row.get("provider") or "")
            if provider not in PROVIDER_ORDER:
                continue
            state = str(row.get("state") or row.get("status") or "")
            if state not in {"provider_failed", "cooldown"}:
                continue
            retry_at = _float(row.get("retry_after"), 0)
            if retry_at > now:
                self._circuits[provider] = max(
                    retry_at, self._circuits.get(provider, 0.0))

    def _record(self, state: str, *, attempt_id: str, provider: _HttpProvider,
                started_at: float, **extra) -> None:
        recorder = self._context.get("recorder")
        if not callable(recorder):
            return
        terminal = state in {
            "completed", "provider_failed", "provider_unavailable",
            "cooldown", "submission_unknown", "chain_exhausted",
        }
        error_class = str(extra.pop("error_class", "") or "")
        recorder(
            state=state, status=state, attempt_id=attempt_id,
            provider=provider.provider, model=provider.model,
            sequence=self._context.get("sequence"),
            input_sha256=self._context.get("audio_sha256", ""),
            audio_sha256=self._context.get("audio_sha256", ""),
            started_at=started_at,
            ended_at=time.time() if terminal else None,
            error_class=error_class,
            submitted_audio_seconds=round(
                self.last_submitted_audio_seconds, 3),
            **{k: v for k, v in extra.items() if v is not None},
        )

    def _resume(self, provider: _HttpProvider) -> dict:
        audio_hash = str(self._context.get("audio_sha256") or "")
        candidates = [
            row for row in self._context.get("history", [])
            if str(row.get("provider") or "") == provider.provider
            and (not audio_hash or str(
                row.get("audio_sha256") or row.get("input_sha256") or "") == audio_hash)
            and str(row.get("state") or row.get("status") or "") in {
                "uploaded", "accepted", "polling", "submission_unknown",
            }
        ]
        return dict(candidates[-1]) if candidates else {}

    def transcribe(self, audio, **kwargs):
        self.last_submitted_audio_seconds = audio_duration_seconds(audio)
        useful, silence_diag = wav_has_useful_signal(audio)
        self.last_silence_diagnostics = silence_diag
        if silence_diag.get("reason") == "unreadable":
            raise RemoteSttError(
                "audio canónico ilegible; no se envió a ningún proveedor",
                provider="local_gate", model="pcm-energy-v1",
                retryable=False, error_class="invalid_audio",
                advance_provider=False, invalid_audio=True)
        if not useful:
            self.last_provider, self.last_model, self.last_request_id = (
                "silence_gate", "pcm-energy-v1", "")
            return [], SimpleNamespace(
                language="", language_probability=1.0, request_id="",
                provider=self.last_provider, model=self.last_model,
                silence=True, silence_diagnostics=silence_diag)
        trace: list[dict] = []
        for provider in self.providers:
            resume = self._resume(provider)
            if (str(resume.get("state") or resume.get("status") or "")
                    == "submission_unknown"
                    and provider.provider in {"deepgram", "groq"}):
                # APIs síncronas sin idempotency/recovery: reenviar podría cobrar
                # dos veces. El WAV permanece pendiente para decisión explícita.
                raise RemoteJobPending(
                    provider.provider, provider.model,
                    str(resume.get("request_id") or ""),
                    ambiguous_submission=True)
            circuit_until = float(self._circuits.get(provider.provider, 0.0))
            if not resume and circuit_until > time.time():
                attempt_id = (
                    f"{self._context.get('chunk_id') or 'audio'}:{provider.provider}:"
                    f"cooldown:{uuid.uuid4().hex[:10]}")
                self._record(
                    "cooldown", attempt_id=attempt_id, provider=provider,
                    started_at=time.time(), retry_after=circuit_until,
                    error_class="circuit_open")
                trace.append({"provider": provider.provider,
                              "error_class": "circuit_open", "status_code": None})
                continue
            if circuit_until:
                self._circuits.pop(provider.provider, None)
            for number in range(1, self.max_attempts_per_provider + 1):
                attempt_id = str(resume.get("attempt_id") or (
                    f"{self._context.get('chunk_id') or 'audio'}:{provider.provider}:"
                    f"{number}:{uuid.uuid4().hex[:10]}"))
                started_at = time.time()
                self._record("started", attempt_id=attempt_id, provider=provider,
                             started_at=started_at, attempt_number=number)

                def on_state(state: str, **extra):
                    self._record(state, attempt_id=attempt_id, provider=provider,
                                 started_at=started_at, **extra)
                    resume.update(extra)
                    resume["state"] = state
                    resume["attempt_id"] = attempt_id

                try:
                    segments, info = provider.transcribe(
                        audio, resume=resume, on_state=on_state,
                        audio_sha256=self._context.get("audio_sha256", ""),
                        sequence=self._context.get("sequence"), **kwargs)
                    self.last_provider = provider.provider
                    self.last_model = str(getattr(info, "model", provider.model))
                    self.last_request_id = str(
                        getattr(info, "request_id", provider.last_request_id) or "")
                    self.last_language = str(getattr(info, "language", "") or "")
                    segments = list(segments)
                    self._circuits.pop(provider.provider, None)
                    self._record(
                        "completed", attempt_id=attempt_id, provider=provider,
                        started_at=started_at, request_id=self.last_request_id,
                        segments=len(segments))
                    return segments, info
                except RemoteJobPending as error:
                    state = "submission_unknown" if error.ambiguous_submission else "polling"
                    recovery = {
                        key: resume.get(key)
                        for key in (
                            "upload_url", "transcript_id", "job_id",
                            "tracking_reference")
                        if resume.get(key)
                    }
                    self._record(
                        state, attempt_id=attempt_id, provider=provider,
                        started_at=started_at, request_id=error.request_id,
                        error_class=error.error_class,
                        retry_after=time.time() + float(error.retry_after_seconds or 5.0),
                        **recovery)
                    raise
                except RemoteSttError as error:
                    if error.pending_job:
                        # El job ya existe. Conservar su identificador y reanudar
                        # polling; nunca degradarlo a fallo que permita re-submit.
                        self._record(
                            "polling", attempt_id=attempt_id,
                            provider=provider, started_at=started_at,
                            request_id=error.request_id,
                            error_class=error.error_class,
                            retry_after=time.time() + float(
                                error.retry_after_seconds or 5.0))
                        raise RemoteJobPending(
                            provider.provider, provider.model,
                            error.request_id,
                            retry_after_seconds=float(
                                error.retry_after_seconds or 5.0)) from error
                    trace.append({
                        "provider": provider.provider,
                        "status_code": error.status_code,
                        "error_class": error.error_class,
                    })
                    cooldown_until = None
                    if error.error_class == "authorization":
                        cooldown_until = time.time() + 24 * 3600
                    elif error.retryable and (
                            number >= self.max_attempts_per_provider
                            or float(error.retry_after_seconds or 0)
                            > self.max_inline_retry_seconds):
                        cooldown_until = time.time() + max(
                            15.0, float(error.retry_after_seconds or 0.0))
                    if cooldown_until is not None:
                        self._circuits[provider.provider] = cooldown_until
                    self._record(
                        "provider_failed", attempt_id=attempt_id,
                        provider=provider, started_at=started_at,
                        request_id=error.request_id,
                        status_code=error.status_code,
                        error_class=error.error_class,
                        retryable=error.retryable,
                        retry_after=cooldown_until)
                    if error.invalid_audio or not error.advance_provider:
                        raise
                    inline = float(error.retry_after_seconds or 0.0)
                    if (number < self.max_attempts_per_provider and error.retryable
                            and inline <= self.max_inline_retry_seconds):
                        if inline:
                            self.sleeper(inline)
                        continue
                    break
        # Un preflight correcto siempre construye al menos un proveedor; los que
        # carecen de clave no aparecen y quedan indicados en info de configuración.
        raise FailoverExhausted(trace)


def create_backend(*, backend: str | None = None, remote_consent: bool,
                   environ: Mapping[str, str] | None = None,
                   clients: Mapping[str, object] | None = None,
                   providers: list[_HttpProvider] | None = None,
                   sleeper: Callable[[float], None] = time.sleep,
                   **_legacy):
    env = os.environ if environ is None else environ
    check = preflight(backend=backend, remote_consent=remote_consent, environ=env)
    if not check["ok"]:
        raise RuntimeError(check["message"])
    clients = dict(clients or {})
    if providers is None:
        providers = []
        for provider_id in PROVIDER_ORDER:
            key = str(env.get(PROVIDER_KEYS[provider_id], "")).strip()
            if not key:
                continue
            cls = PROVIDER_CLASSES[provider_id]
            timeout = _float(env.get(
                f"CLASE_{provider_id.upper()}_STT_TIMEOUT_SEG", "45"), 45)
            base = str(env.get(f"{provider_id.upper()}_BASE_URL", "")).strip()
            kwargs = {"api_key": key, "client": clients.get(provider_id),
                      "timeout_seconds": timeout}
            if base:
                kwargs["base_url"] = base
            providers.append(cls(**kwargs))
    chain = RemoteFailoverBackend(
        providers,
        max_attempts_per_provider=int(env.get("CLASE_STT_PROVIDER_ATTEMPTS", "2")),
        max_inline_retry_seconds=_float(
            env.get("CLASE_STT_MAX_INLINE_RETRY_SEG", "2"), 2),
        sleeper=sleeper,
    )
    return chain, {
        "backend": "remote-chain", "provider": "remote-chain",
        "model": "remote-failover", "device": "remote", "gpu": "",
        "compute_type": "managed", "readiness": "ready",
        "fallback_reason": "", "remote_audio": True, "whole_chunk": True,
        "provider_order": [provider.provider for provider in providers],
        "providers_unavailable": check.get("providers_unavailable", []),
    }
