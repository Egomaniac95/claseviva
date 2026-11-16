from types import SimpleNamespace

import numpy as np
import pytest

from src import audio_silence, stt_backend


@pytest.fixture(autouse=True)
def digital_filter_contract(monkeypatch):
    monkeypatch.setenv("CLASE_STT_VAD", "digital")


def _audio():
    return np.concatenate((np.full(16000, .1, np.float32),
                           np.zeros(6 * 16000, np.float32),
                           np.full(16000, .1, np.float32)))


def test_long_pause_preserves_samples_margins_and_original_clock():
    original = _audio()
    packed, clock = audio_silence.trim_digital_silence(original)
    assert len(packed) == round(2.6 * 16000)
    assert clock.original_time(1) == 1
    assert clock.original_time(1.6) == 7
    assert clock.original_time(2.6, end=True) == 8
    # At a splice, starts belong to the following span and ends to the former.
    assert clock.original_time(1.3) == 6.7
    assert clock.original_time(1.3, end=True) == 1.3
    np.testing.assert_array_equal(original[:16000], packed[:16000])
    np.testing.assert_array_equal(original[-16000:], packed[-16000:])


@pytest.mark.parametrize("level", [1 / 32768, 2 / 32768, .0001, .002, .1])
def test_faint_signal_and_noise_are_never_removed(level):
    original = np.full(8 * 16000, level, np.float32)
    packed, clock = audio_silence.trim_digital_silence(original)
    np.testing.assert_array_equal(original, packed)
    assert clock.original_time(7) == 7


def test_legacy_digital_jobs_recover_the_old_threshold():
    original = np.r_[np.full(16000, .1), np.full(6 * 16000, 1 / 32768),
                     np.full(16000, .1)].astype(np.float32)
    packed, _ = audio_silence.trim_digital_silence(original, legacy=True)
    assert len(packed) == round(2.6 * 16000)
    current, _ = audio_silence.trim_digital_silence(original)
    np.testing.assert_array_equal(current, original)


def test_pending_legacy_transform_does_not_switch_to_new_cut_rules():
    original = np.r_[np.full(16000, .1), np.full(6 * 16000, 1 / 32768),
                     np.full(16000, .1)].astype(np.float32)
    provider = Provider()
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(chunk_id="old", audio_sha256="sha", history=[{
        "chunk_id": "old", "audio_sha256": "sha",
        "audio_transform": audio_silence.LEGACY_VERSION,
        "status": "polling", "provider": "assemblyai", "request_id": "fixture",
    }])
    chain.transcribe(original)
    assert provider.seconds == 2.6


def test_short_pause_is_untouched():
    original = np.r_[np.ones(16000), np.zeros(16000), np.ones(16000)].astype(np.float32)
    packed, _ = audio_silence.trim_digital_silence(original)
    np.testing.assert_array_equal(packed, original)


class Provider:
    provider = "assemblyai"
    model = "fixture"
    last_request_id = "fixture"

    def transcribe(self, audio, **kwargs):
        self.seconds = len(audio) / 16000
        return [stt_backend.RemoteSegment(1.6, 2.6, "segunda frase")], SimpleNamespace(
            model=self.model, request_id="fixture", language="es")


def test_remote_chain_restores_timestamps_and_records_actual_upload(monkeypatch):
    monkeypatch.setenv("CLASE_STT_TRIM_SILENCE", "1")
    provider = Provider()
    chain = stt_backend.RemoteFailoverBackend([provider])
    events = []
    chain.set_attempt_context(chunk_id="current", audio_sha256="sha", recorder=lambda **e: events.append(e))
    segments, _ = chain.transcribe(_audio())
    assert provider.seconds == 2.6
    assert chain.last_submitted_audio_seconds == 2.6
    assert segments[0].start == 7
    assert segments[0].end == 8
    assert all(e["audio_transform"] == audio_silence.VERSION for e in events)


def test_legacy_pending_job_keeps_original_upload_and_clock():
    provider = Provider()
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(chunk_id="current", audio_sha256="sha", history=[{
        "chunk_id": "current", "audio_sha256": "sha", "state": "polling",
        "provider": "assemblyai", "transcript_id": "existing",
    }])
    segments, _ = chain.transcribe(_audio())
    assert provider.seconds == 8
    assert segments[0].start == 1.6


def test_other_chunk_history_does_not_disable_new_optimization():
    provider = Provider()
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(chunk_id="current", audio_sha256="sha", history=[{
        "chunk_id": "previous", "audio_sha256": "other", "state": "completed",
    }])
    chain.transcribe(_audio())
    assert provider.seconds == 2.6


def test_pending_trimmed_job_keeps_transform_when_option_is_disabled(monkeypatch):
    monkeypatch.setenv("CLASE_STT_TRIM_SILENCE", "0")
    provider = Provider()
    chain = stt_backend.RemoteFailoverBackend([provider])
    chain.set_attempt_context(chunk_id="current", audio_sha256="sha", history=[{
        "chunk_id": "current", "audio_sha256": "sha", "state": "polling",
        "provider": "assemblyai", "transcript_id": "existing",
        "audio_transform": audio_silence.VERSION,
    }])
    segments, _ = chain.transcribe(_audio())
    assert provider.seconds == 2.6
    assert segments[0].start == 7


def test_option_disables_trimming_for_new_jobs(monkeypatch):
    monkeypatch.setenv("CLASE_STT_TRIM_SILENCE", "0")
    provider = Provider()
    chain = stt_backend.RemoteFailoverBackend([provider])
    segments, _ = chain.transcribe(_audio())
    assert provider.seconds == 8
    assert segments[0].start == 1.6
