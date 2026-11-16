from types import SimpleNamespace

import numpy as np
import pytest

from src import audio_silence, audio_vad, post_procesar, stt_backend


def _fixture():
    # Five seconds of low noise between two voiced regions, 32 ms windows.
    audio = np.full(320 * 512, .001, dtype=np.float32)
    audio[:50 * 512] = .1
    audio[250 * 512:] = .1
    scores = np.full(320, .01)
    scores[:50] = scores[250:] = .95
    return audio, scores


def test_digital_pause_is_cut_with_margins_and_voice_samples_preserved():
    audio, scores = _fixture()
    audio[50 * 512:250 * 512] = 0
    packed, clock, diag = audio_vad.trim(audio, predictor=lambda _: scores)
    cut_start, cut_end = 50 * 512 + 6400, 250 * 512 - 6400
    expected = np.r_[audio[:cut_start], audio[cut_end:]]
    np.testing.assert_array_equal(packed, expected)
    assert clock.original_time(cut_start / 16000) == cut_end / 16000
    assert clock.original_time(cut_start / 16000, end=True) == cut_start / 16000
    assert diag["cuts"] == 1


def test_low_vad_probability_does_not_remove_nonzero_voice():
    audio, scores = _fixture()
    packed, _, diag = audio_vad.trim(audio, predictor=lambda _: scores)
    np.testing.assert_array_equal(packed, audio)
    assert diag["cuts"] == 0


def test_ambiguous_low_voice_and_short_words_are_kept():
    audio, scores = _fixture()
    scores[50:250] = .25  # ambiguous frames must not be cut
    packed, _, _ = audio_vad.trim(audio, predictor=lambda _: scores)
    np.testing.assert_array_equal(packed, audio)
    scores[50:250] = .01
    scores[140] = .55  # even one short word keeps its surrounding margins
    packed, clock, _ = audio_vad.trim(audio, predictor=lambda _: scores)
    assert any(start <= 140 * 512 < start + size for _, start, size in clock.spans)


@pytest.mark.parametrize("case", ["no_voice", "broken_runtime", "nan", "wrong_shape"])
def test_uncertain_detector_falls_back_to_digital_without_discarding_noisy_audio(case):
    audio, scores = _fixture()
    def predict(_):
        if case == "broken_runtime":
            raise RuntimeError("fixture")
        return {"no_voice": np.zeros(320), "nan": np.full(320, np.nan),
                "wrong_shape": np.ones(12)}[case]
    packed, _, details = audio_vad.trim(audio, predictor=predict)
    np.testing.assert_array_equal(packed, audio)
    assert details["fallback"] == audio_silence.VERSION


def test_recurrent_model_resets_for_each_recording_and_runs_on_cpu():
    pytest.importorskip("onnxruntime")
    audio = np.zeros(16000 * 3, dtype=np.float32)
    a = audio_vad.probabilities(audio)
    b = audio_vad.probabilities(audio)
    np.testing.assert_array_equal(a, b)
    assert a.shape == (94,)
    assert a.max() < .2
    assert audio_vad._session().get_providers() == ["CPUExecutionProvider"]


class Provider:
    provider = "speechmatics"
    model = "fixture"
    last_request_id = "existing-job"

    def transcribe(self, audio, **kwargs):
        self.received = audio.copy()
        return [stt_backend.RemoteSegment(0, len(audio) / 16000, "clase")], SimpleNamespace(
            model=self.model, request_id=self.last_request_id, language="es")


def test_restart_uses_persisted_map_even_without_detector_and_after_config_change(monkeypatch):
    audio, scores = _fixture()
    monkeypatch.setenv("CLASE_STT_TRIM_SILENCE", "1")
    monkeypatch.setenv("CLASE_STT_VAD", "silero")
    monkeypatch.setattr(audio_vad, "probabilities", lambda _: scores)
    events = []
    provider = Provider()
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(chunk_id="current", audio_sha256="sha",
                              recorder=lambda **e: events.append({"chunk_id": "current", **e}))
    chain.transcribe(audio)
    initial = provider.received.copy()
    assert events[0]["audio_transform_map"]["packed_sha256"]
    monkeypatch.setenv("CLASE_STT_VAD", "off")
    monkeypatch.setenv("CLASE_STT_TRIM_SILENCE", "0")
    monkeypatch.setattr(audio_vad, "probabilities", lambda _: pytest.fail("must not rerun VAD"))
    chain.set_attempt_context(chunk_id="current", audio_sha256="sha", history=events)
    chain.transcribe(audio)
    np.testing.assert_array_equal(provider.received, initial)


def test_changed_preprocessing_blocks_upload_of_pending_job():
    audio, scores = _fixture()
    packed, clock, _ = audio_vad.trim(audio, predictor=lambda _: scores)
    payload = audio_vad.serialize_map(clock, packed)
    chain = stt_backend.RemoteFailoverBackend([Provider()])
    chain.set_attempt_context(chunk_id="current", audio_sha256="sha", history=[{
        "chunk_id": "current", "audio_sha256": "sha", "audio_transform": audio_vad.VERSION,
        "audio_transform_map": payload, "state": "polling", "provider": "speechmatics",
        "request_id": "existing-job"}])
    with pytest.raises(stt_backend.RemoteSttError, match="preprocesamiento"):
        chain.transcribe(audio * 2)
    assert not hasattr(chain.providers[0], "received")


def test_pending_v1_vad_reuses_saved_bytes_instead_of_new_silence_rule(monkeypatch):
    audio, _ = _fixture()
    packed, clock = audio_vad._pack(audio, [(50 * 512 + 6400, 250 * 512 - 6400)])
    payload = audio_vad.serialize_map(clock, packed)
    monkeypatch.setattr(audio_vad, "probabilities", lambda _: pytest.fail("mapa histórico"))
    provider = Provider()
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(chunk_id="old", audio_sha256="sha", history=[{
        "chunk_id": "old", "audio_sha256": "sha",
        "audio_transform": audio_vad.LEGACY_VERSION,
        "audio_transform_map": payload, "status": "polling",
        "provider": "speechmatics", "request_id": "existing-job",
    }])
    chain.transcribe(audio)
    np.testing.assert_array_equal(provider.received, packed)


def test_coverage_uses_pcm_clock_instead_of_reserved_filename(tmp_path):
    path = tmp_path / "chunk_001700000000_p1_v10.wav"
    origin = {"inicio": 1700000000, "origen": "vivo"}
    assert post_procesar._wav_t_rel(path, origin, {"pcm_wall_start": 1700000351.25}) == 351.25
    assert post_procesar._wav_t_rel(path, origin) == 0
