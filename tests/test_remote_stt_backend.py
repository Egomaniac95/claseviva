from __future__ import annotations

import copy
import json
import wave
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from src import panel, stt_backend, transcriptor_vivo


def _tone(seconds: float = 0.2, amplitude: float = 0.1) -> np.ndarray:
    t = np.arange(max(1, round(16000 * seconds)), dtype=np.float32) / 16000
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


class _Provider:
    whole_chunk = True
    cross_chunk_overlap = False

    def __init__(self, name, result=None, error=None):
        self.provider = name
        self.model = f"{name}-model"
        self.result = result
        self.error = error
        self.calls = 0
        self.last_request_id = ""

    def transcribe(self, _audio, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        self.last_request_id = f"req-{self.provider}"
        rows = self.result if self.result is not None else [
            stt_backend.RemoteSegment(0, 1, f"texto {self.provider}")]
        return rows, SimpleNamespace(
            language="es", language_probability=1.0,
            request_id=self.last_request_id, provider=self.provider,
            model=self.model)


class _Response:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return copy.deepcopy(self._payload)


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_preflight_remoto_no_hace_red_y_local_esta_retirado():
    env = {"ASSEMBLYAI_API_KEY": "fixture"}
    ready = stt_backend.preflight(
        backend="remote", remote_consent=True, environ=env)
    no_consent = stt_backend.preflight(
        backend="remote", remote_consent=False, environ=env)
    local = stt_backend.preflight(
        backend="local", remote_consent=True, environ=env)

    assert ready["ok"] is True
    assert ready["providers_available"] == ["assemblyai"]
    assert no_consent["ok"] is False
    assert local["ok"] is False
    assert "retirado" in local["message"]


def test_gate_de_silencio_no_invoca_ningun_proveedor():
    providers = [_Provider(name) for name in stt_backend.PROVIDER_ORDER]
    chain = stt_backend.RemoteFailoverBackend(providers)

    segments, info = chain.transcribe(np.zeros(16000, dtype=np.float32))

    assert segments == []
    assert info.provider == "silence_gate"
    assert [item.calls for item in providers] == [0, 0, 0, 0]


def test_gate_conservador_no_descarta_voz_baja():
    useful, diagnostics = stt_backend.wav_has_useful_signal(_tone(amplitude=0.012))
    assert useful is True
    assert diagnostics["peak"] >= 240


@pytest.mark.parametrize("amplitude", [1 / 32768, 0.0001, 0.0007])
def test_gate_keeps_signal_below_old_rms_threshold(amplitude):
    useful, _ = stt_backend.wav_has_useful_signal(_tone(amplitude=amplitude))
    assert useful


def test_gate_checks_every_sample_and_preserves_opposite_channels(tmp_path):
    from src.compat_av import cargar_wav16k

    stereo = np.zeros((16000, 2), dtype="<i2")
    stereo[1:100, 0] = 15
    stereo[1:100, 1] = -15
    path = tmp_path / "opposite.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(stereo.tobytes())
    assert stt_backend.wav_has_useful_signal(path)[0]
    assert np.any(cargar_wav16k(path))
    isolated = np.zeros(16000, dtype=np.float32)
    isolated[1] = 1 / 32768
    assert stt_backend.wav_has_useful_signal(isolated)[0]


def test_failover_es_secuencial_y_commit_metadata_usa_ganador():
    first = _Provider("assemblyai", error=stt_backend.RemoteSttError(
        "safe", provider="assemblyai", model="a", status_code=429,
        retryable=True, retry_after_seconds=30, error_class="rate_limit"))
    second = _Provider("deepgram")
    third = _Provider("speechmatics")
    chain = stt_backend.RemoteFailoverBackend(
        [first, second, third], max_attempts_per_provider=2,
        max_inline_retry_seconds=2, sleeper=lambda _s: None)

    rows, _info = chain.transcribe(_tone())

    assert [first.calls, second.calls, third.calls] == [1, 1, 0]
    assert rows[0].text == "texto deepgram"
    assert chain.last_provider == "deepgram"
    assert chain.last_request_id == "req-deepgram"


def test_exito_primario_no_invoca_fallbacks():
    primary = _Provider("assemblyai")
    fallbacks = [_Provider(name) for name in stt_backend.PROVIDER_ORDER[1:]]
    chain = stt_backend.RemoteFailoverBackend([primary, *fallbacks])

    rows, _info = chain.transcribe(_tone())

    assert rows[0].text == "texto assemblyai"
    assert primary.calls == 1
    assert [provider.calls for provider in fallbacks] == [0, 0, 0]


@pytest.mark.parametrize("winner_index", range(4))
def test_credenciales_invalidas_fuerzan_cada_eslabon_en_orden(winner_index):
    """Reproduce sin red la validación real con claves invalidadas progresivamente."""
    providers = []
    for index, name in enumerate(stt_backend.PROVIDER_ORDER):
        error = None
        if index < winner_index:
            error = stt_backend.RemoteSttError(
                "credencial rechazada", provider=name, model=f"{name}-model",
                status_code=401, retryable=False, error_class="authorization")
        providers.append(_Provider(name, error=error))
    chain = stt_backend.RemoteFailoverBackend(
        providers, max_attempts_per_provider=1)

    rows, _info = chain.transcribe(_tone())

    expected_calls = [1 if index <= winner_index else 0 for index in range(4)]
    assert [provider.calls for provider in providers] == expected_calls
    assert chain.last_provider == stt_backend.PROVIDER_ORDER[winner_index]
    assert rows[0].text == f"texto {stt_backend.PROVIDER_ORDER[winner_index]}"


def test_reintentos_son_acotados_antes_de_avanzar():
    transient = stt_backend.RemoteSttError(
        "safe", provider="assemblyai", model="a", status_code=503,
        retryable=True, error_class="server_error")
    first, second = _Provider("assemblyai", error=transient), _Provider("deepgram")
    chain = stt_backend.RemoteFailoverBackend(
        [first, second], max_attempts_per_provider=2, sleeper=lambda _s: None)

    chain.transcribe(_tone())

    assert first.calls == 2
    assert second.calls == 1


def test_job_aceptado_pendiente_no_avanza_ni_reenvia():
    first = _Provider("assemblyai", error=stt_backend.RemoteJobPending(
        "assemblyai", "universal-3-5-pro", "job-7"))
    second = _Provider("deepgram")
    chain = stt_backend.RemoteFailoverBackend([first, second])

    with pytest.raises(stt_backend.RemoteJobPending) as caught:
        chain.transcribe(_tone())

    assert caught.value.request_id == "job-7"
    assert [first.calls, second.calls] == [1, 0]


def test_audio_invalido_no_se_multiplica_en_cuatro_apis():
    error = stt_backend.RemoteSttError(
        "safe", provider="assemblyai", model="a", status_code=422,
        retryable=False, error_class="invalid_audio", invalid_audio=True,
        advance_provider=False)
    providers = [_Provider("assemblyai", error=error), _Provider("deepgram")]
    chain = stt_backend.RemoteFailoverBackend(providers)

    with pytest.raises(stt_backend.RemoteSttError) as caught:
        chain.transcribe(_tone())

    assert caught.value.invalid_audio is True
    assert [item.calls for item in providers] == [1, 0]


def test_wav_ilegible_se_rechaza_localmente_sin_consumir_api(tmp_path):
    path = tmp_path / "corrupto.wav"
    path.write_bytes(b"esto no es un wav")
    providers = [_Provider(name) for name in stt_backend.PROVIDER_ORDER]
    chain = stt_backend.RemoteFailoverBackend(providers)

    with pytest.raises(stt_backend.RemoteSttError) as caught:
        chain.transcribe(path)

    assert caught.value.error_class == "invalid_audio"
    assert [provider.calls for provider in providers] == [0, 0, 0, 0]


def test_agotamiento_total_conserva_traza_sin_mensajes_remotos():
    providers = [
        _Provider(name, error=stt_backend.RemoteSttError(
            "secret=must-not-leak", provider=name, model="m", status_code=401,
            retryable=False, error_class="authorization"))
        for name in stt_backend.PROVIDER_ORDER
    ]
    chain = stt_backend.RemoteFailoverBackend(providers)

    with pytest.raises(stt_backend.FailoverExhausted) as caught:
        chain.transcribe(_tone())

    assert [row["provider"] for row in caught.value.trace] == list(
        stt_backend.PROVIDER_ORDER)
    assert "must-not-leak" not in str(caught.value)


def test_assemblyai_modelo_35_plural_y_job_recuperable():
    client = _Client([
        _Response({"upload_url": "https://fixture.invalid/audio"}),
        _Response({"id": "aai-job"}),
        _Response({
            "id": "aai-job", "status": "completed", "language_code": "es",
            "speech_model_used": "universal-3-5-pro", "text": "hola mundo",
            "words": [
                {"text": "hola", "start": 0, "end": 300},
                {"text": "mundo", "start": 320, "end": 700},
            ],
        }),
    ])
    states = []
    backend = stt_backend.AssemblyAIBackend(api_key="fixture", client=client)

    rows, info = backend.transcribe(
        _tone(), on_state=lambda state, **meta: states.append((state, meta)))

    submit = client.calls[1][2]["json"]
    assert submit["speech_models"] == ["universal-3-5-pro"]
    assert submit["language_detection"] is True
    assert [state for state, _ in states][:2] == ["uploaded", "accepted"]
    assert info.request_id == "aai-job"
    assert "hola mundo" == rows[0].text


def test_assemblyai_reanuda_job_sin_upload_ni_submit():
    client = _Client([_Response({
        "id": "existing", "status": "completed", "text": "recuperado",
        "words": [],
    })])
    backend = stt_backend.AssemblyAIBackend(api_key="fixture", client=client)

    rows, _ = backend.transcribe(_tone(), resume={"request_id": "existing"})

    assert len(client.calls) == 1
    assert client.calls[0][0:2] == (
        "GET", "https://api.assemblyai.com/v2/transcript/existing")
    assert rows[0].text == "recuperado"


def test_timeout_del_upload_no_facturable_de_assembly_avanza_al_fallback():
    request = httpx.Request("POST", "https://api.assemblyai.com/v2/upload")
    assembly = stt_backend.AssemblyAIBackend(
        api_key="fixture",
        client=_Client([httpx.ReadTimeout("safe fixture", request=request)]),
    )
    deepgram = _Provider("deepgram")
    chain = stt_backend.RemoteFailoverBackend(
        [assembly, deepgram], max_attempts_per_provider=1)

    rows, _info = chain.transcribe(_tone())

    assert rows[0].text == "texto deepgram"
    assert deepgram.calls == 1


def test_deepgram_nova3_multi_parsea_request_id():
    client = _Client([_Response({
        "metadata": {"request_id": "dg-1"},
        "results": {"channels": [{"alternatives": [{
            "transcript": "hola", "words": [
                {"word": "hola", "start": 0.1, "end": 0.4}],
        }]}]},
    })])
    backend = stt_backend.DeepgramBackend(api_key="fixture", client=client)

    rows, info = backend.transcribe(_tone())

    assert "model=nova-3&language=multi" in client.calls[0][1]
    assert info.request_id == "dg-1"
    assert rows[0].text == "hola"


def test_speechmatics_melia_multi_y_job_id():
    client = _Client([
        _Response({"id": "sm-1"}, status=201),
        _Response({"job": {"id": "sm-1", "status": "done"}}),
        _Response({"results": [{
            "type": "word", "start_time": 0.0, "end_time": 0.4,
            "alternatives": [{"content": "hola"}],
        }]}),
    ])
    backend = stt_backend.SpeechmaticsBackend(api_key="fixture", client=client)

    rows, info = backend.transcribe(_tone(), audio_sha256="abc", sequence=3)

    config = json.loads(client.calls[0][2]["data"]["config"])
    assert config["transcription_config"] == {"model": "melia-1", "language": "multi"}
    assert info.request_id == "sm-1"
    assert rows[0].text == "hola"


def test_groq_usa_large_v3_no_turbo():
    client = _Client([_Response({
        "text": "hello", "language": "en", "x_groq": {"id": "g-1"},
        "segments": [{"start": 0, "end": 0.5, "text": "hello"}],
    })])
    backend = stt_backend.GroqWhisperBackend(api_key="fixture", client=client)

    rows, _ = backend.transcribe(_tone())

    assert client.calls[0][2]["data"]["model"] == "whisper-large-v3"
    assert "turbo" not in backend.model
    assert rows[0].text == "hello"


def test_timeout_lectura_post_sin_idempotencia_queda_pendiente():
    request = httpx.Request("POST", "https://fixture.invalid")
    client = _Client([httpx.ReadTimeout("secret body", request=request)])
    backend = stt_backend.DeepgramBackend(api_key="fixture", client=client)

    with pytest.raises(stt_backend.RemoteJobPending) as caught:
        backend.transcribe(_tone())

    assert caught.value.ambiguous_submission is True
    assert "secret body" not in str(caught.value)


def test_timeout_escritura_post_sin_idempotencia_queda_pendiente():
    request = httpx.Request("POST", "https://fixture.invalid")
    client = _Client([httpx.WriteTimeout("secret body", request=request)])
    backend = stt_backend.GroqWhisperBackend(api_key="fixture", client=client)

    with pytest.raises(stt_backend.RemoteJobPending) as caught:
        backend.transcribe(_tone())

    assert caught.value.ambiguous_submission is True
    assert "secret body" not in str(caught.value)


def test_429_al_consultar_job_aceptado_conserva_job_id_para_reanudar():
    client = _Client([_Response({}, status=429, headers={"retry-after": "17"})])
    provider = stt_backend.AssemblyAIBackend(api_key="fixture", client=client)
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(
        chunk_id="chunk-a", audio_sha256="hash-a", sequence=1,
        history=[{
            "provider": "assemblyai", "state": "accepted",
            "audio_sha256": "hash-a", "attempt_id": "attempt-a",
            "request_id": "job-existing",
        }], recorder=None)

    with pytest.raises(stt_backend.RemoteJobPending) as caught:
        chain.transcribe(_tone())

    assert caught.value.request_id == "job-existing"
    assert client.calls[0][0:2] == (
        "GET", "https://api.assemblyai.com/v2/transcript/job-existing")


def test_timeout_ambiguo_sin_idempotencia_no_reenvia_en_reinicio():
    provider = _Provider("deepgram")
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(
        chunk_id="chunk-a", audio_sha256="hash-a", sequence=1,
        history=[{
            "provider": "deepgram", "state": "submission_unknown",
            "audio_sha256": "hash-a", "attempt_id": "attempt-a",
        }], recorder=None)

    with pytest.raises(stt_backend.RemoteJobPending):
        chain.transcribe(_tone())

    assert provider.calls == 0


def test_circuit_breaker_salta_429_y_luego_admite_probe():
    first = _Provider("assemblyai", error=stt_backend.RemoteSttError(
        "safe", provider="assemblyai", model="a", status_code=429,
        retryable=True, retry_after_seconds=30, error_class="rate_limit"))
    second = _Provider("deepgram")
    events = []
    chain = stt_backend.RemoteFailoverBackend([first, second])
    chain.set_attempt_context(
        chunk_id="a", audio_sha256="ha", sequence=1, history=[],
        recorder=lambda **row: events.append(row))
    chain.transcribe(_tone())
    chain.set_attempt_context(
        chunk_id="b", audio_sha256="hb", sequence=2, history=events,
        recorder=lambda **row: events.append(row))
    chain.transcribe(_tone())

    assert first.calls == 1
    assert second.calls == 2
    assert any(row["state"] == "cooldown" for row in events)

    chain._circuits["assemblyai"] = 0
    first.error = None
    chain.set_attempt_context(
        chunk_id="c", audio_sha256="hc", sequence=3, history=[], recorder=None)
    chain.transcribe(_tone())
    assert first.calls == 2
    assert chain.last_provider == "assemblyai"


def test_journal_callback_contiene_contrato_minimo_sin_claves():
    provider = _Provider("assemblyai")
    events = []
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(
        chunk_id="chunk-a", audio_sha256="sha-a", sequence=7, history=[],
        recorder=lambda **row: events.append(row))

    chain.transcribe(_tone())

    completed = next(row for row in events if row["state"] == "completed")
    for key in (
            "sequence", "attempt_id", "provider", "model", "audio_sha256",
            "request_id", "state", "error_class", "started_at", "ended_at"):
        assert key in completed
    assert "fixture" not in json.dumps(events)


def test_transcribir_x2_no_modifica_wav_canonico(tmp_path, monkeypatch):
    path = tmp_path / "chunk_100_p1_v20.wav"
    samples = (_tone(1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    before = path.read_bytes()
    seen = {}

    class Model(_Provider):
        def transcribe(self, audio, **kwargs):
            seen["samples"] = len(audio)
            return super().transcribe(audio, **kwargs)

    model = stt_backend.RemoteFailoverBackend([Model("assemblyai")])
    transcriptor_vivo.transcribir_chunk(model, path)

    assert path.read_bytes() == before
    assert seen["samples"] > samples.size
    assert model.last_submitted_audio_seconds > 1.8


def test_panel_no_expone_whisper_local_y_preflight_antes_de_crear(monkeypatch):
    assert 'value="local"' not in panel.PAGINA
    assert "Whisper local" not in panel.PAGINA
    monkeypatch.setattr(panel, "MODO_APP", True)
    monkeypatch.setattr(panel, "_clase", lambda: None)
    monkeypatch.setattr(
        panel.est, "crear_clase",
        lambda *_a, **_k: pytest.fail("no debe mutar contenido"))
    monkeypatch.setattr(panel.stt_backend, "preflight", lambda **_k: {
        "ok": False, "message": "falta clave"})

    response = panel.app.test_client().post("/api/iniciar", json={
        "tipo": "nueva", "tema": "fixture", "curso": "fixture",
        "stt_backend": "remote", "stt_remote_consent": True,
    })

    assert response.status_code == 409
    assert response.get_json()["reason"] == "stt_preflight_failed"


def _aislar_inicio_info(monkeypatch, tmp_path):
    """El endpoint de inicio recorre contenido real: se aísla en tmp_path."""
    monkeypatch.setattr(panel, "MODO_APP", False)
    monkeypatch.setattr(panel.config, "CONTENIDO_DIR", tmp_path / "contenido")
    monkeypatch.setattr(panel.config, "PUNTERO_CLASE", tmp_path / "puntero.txt")
    monkeypatch.setattr(panel, "_monitores", lambda: [])
    monkeypatch.setattr(panel, "_dispositivos", lambda: [])


def test_inicio_info_expone_default_de_consentimiento(monkeypatch, tmp_path):
    _aislar_inicio_info(monkeypatch, tmp_path)
    cliente = panel.app.test_client()

    monkeypatch.delenv("CLASE_STT_REMOTE_CONSENT", raising=False)
    apagado = cliente.get("/api/inicio_info").get_json()
    monkeypatch.setenv("CLASE_STT_REMOTE_CONSENT", "1")
    encendido = cliente.get("/api/inicio_info").get_json()

    # Instalación nueva sin la variable: sigue pidiendo el consentimiento.
    assert apagado["stt_remote_consent_default"] is False
    assert encendido["stt_remote_consent_default"] is True


def test_default_premarcado_no_sustituye_al_consentimiento_del_cliente(
        monkeypatch, tmp_path):
    """Con la variable activa, un POST sin la casilla debe seguir siendo 409."""
    _aislar_inicio_info(monkeypatch, tmp_path)
    monkeypatch.setenv("CLASE_STT_REMOTE_CONSENT", "1")
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "fixture")
    monkeypatch.setattr(panel, "MODO_APP", True)
    monkeypatch.setattr(panel, "_clase", lambda: None)
    monkeypatch.setattr(
        panel.est, "crear_clase",
        lambda *_a, **_k: pytest.fail("no debe mutar contenido"))
    cliente = panel.app.test_client()

    for cuerpo in (
            {"tipo": "nueva", "tema": "fixture", "curso": "fixture",
             "stt_backend": "remote"},
            {"tipo": "nueva", "tema": "fixture", "curso": "fixture",
             "stt_backend": "remote", "stt_remote_consent": False},
    ):
        respuesta = cliente.post("/api/iniciar", json=cuerpo)
        assert respuesta.status_code == 409
        assert respuesta.get_json()["reason"] == "stt_preflight_failed"


def test_ui_toma_el_default_del_servidor_y_no_lo_hardcodea():
    # La casilla no lleva `checked` en el HTML: el valor llega del endpoint,
    # de modo que el default sigue siendo configurable y auditable.
    assert '<input id="isttconsent" type="checkbox">' in panel.PAGINA
    assert "INFO.stt_remote_consent_default" in panel.PAGINA
    assert "stt_remote_consent_prefilled" in panel.PAGINA
