from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import numpy as np
import pytest

from src import stt_backend


class Provider:
    def __init__(self, name, error=None):
        self.provider = name
        self.model = "fixture"
        self.last_request_id = "done"
        self.calls = 0
        self.error = error

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return [stt_backend.RemoteSegment(0, 1, "explicación")], SimpleNamespace(
            model=self.model, request_id=self.last_request_id, language="es")


def _history(state="polling"):
    return [{"chunk_id": "chunk_p1", "audio_sha256": "sha", "attempt_id": "job-attempt",
             "provider": "speechmatics", "state": state, "request_id": "existing-job"}]


def test_pending_fallback_is_resumed_before_an_available_primary():
    providers = [Provider(name) for name in ("assemblyai", "deepgram", "speechmatics")]
    chain = stt_backend.RemoteFailoverBackend(providers)
    chain.set_attempt_context(chunk_id="chunk_p1", audio_sha256="sha", history=_history())
    chain.transcribe(np.full(16000, .1))
    assert [p.calls for p in providers] == [0, 0, 1]


def test_manual_consolidation_can_retry_unknown_sync_submission_without_empty_commit():
    first, unknown = Provider("assemblyai"), Provider("deepgram")
    events = []
    chain = stt_backend.RemoteFailoverBackend([first, unknown])
    history = [{**_history("submission_unknown")[0], "provider": "deepgram", "request_id": ""}]
    chain.set_attempt_context(chunk_id="chunk_p1", audio_sha256="sha", history=history,
                              recorder=lambda **row: events.append(row), manual_retry=True)
    segments, _ = chain.transcribe(np.full(16000, .1))
    assert first.calls == 1 and unknown.calls == 0 and segments
    assert any(row["status"] == "abandoned_by_manual_retry" for row in events)


def test_manual_retry_still_recovers_known_async_job_before_new_submission():
    first, pending = Provider("assemblyai"), Provider("speechmatics")
    chain = stt_backend.RemoteFailoverBackend([first, pending])
    chain.set_attempt_context(chunk_id="chunk_p1", audio_sha256="sha", history=_history(), manual_retry=True)
    chain.transcribe(np.full(16000, .1))
    assert pending.calls == 1 and first.calls == 0


def test_manual_retry_rechecks_authorization_after_firewall_changes(monkeypatch):
    monkeypatch.setattr(stt_backend.time, "time", lambda: 1000.)
    provider = Provider("assemblyai")
    chain = stt_backend.RemoteFailoverBackend([provider])
    history = [{"provider": "assemblyai", "state": "provider_failed", "status_code": 403,
                "error_class": "authorization", "retry_after": 87400.}]
    chain.set_attempt_context(chunk_id="chunk", audio_sha256="sha", history=history, manual_retry=True)
    chain.transcribe(np.full(16000, .1))
    assert provider.calls == 1


def test_manual_retry_still_respects_exhausted_quota(monkeypatch):
    monkeypatch.setattr(stt_backend.time, "time", lambda: 1000.)
    first, fallback = Provider("assemblyai"), Provider("groq")
    chain = stt_backend.RemoteFailoverBackend([first, fallback])
    history = [{"provider": "assemblyai", "state": "provider_failed", "status_code": 402,
                "error_class": "quota_exhausted", "retry_after": 4600.}]
    chain.set_attempt_context(chunk_id="chunk", audio_sha256="sha", history=history, manual_retry=True)
    chain.transcribe(np.full(16000, .1))
    assert first.calls == 0 and fallback.calls == 1


def test_pending_fallback_timeout_still_does_not_submit_to_primary():
    first = Provider("assemblyai")
    pending = Provider("speechmatics", stt_backend.RemoteJobPending("speechmatics", "fixture", "existing-job"))
    chain = stt_backend.RemoteFailoverBackend([first, pending])
    chain.set_attempt_context(chunk_id="chunk_p1", audio_sha256="sha", history=_history())
    with pytest.raises(stt_backend.RemoteJobPending):
        chain.transcribe(np.full(16000, .1))
    assert first.calls == 0


def test_terminal_job_does_not_resurrect_earlier_polling():
    provider = Provider("speechmatics")
    chain = stt_backend.RemoteFailoverBackend([provider])
    rows = _history() + [{**_history()[0], "state": "provider_failed"}]
    chain.set_attempt_context(chunk_id="chunk_p1", audio_sha256="sha", history=rows)
    assert chain._resume(provider) == {}


def test_crash_after_started_preserves_existing_job_identifier():
    provider = Provider("speechmatics")
    chain = stt_backend.RemoteFailoverBackend([provider])
    started = {k: v for k, v in _history()[0].items() if k != "request_id"}
    started["state"] = "started"
    chain.set_attempt_context(chunk_id="chunk_p1", audio_sha256="sha", history=_history() + [started])
    assert chain._resume(provider)["request_id"] == "existing-job"


def test_remote_completion_before_local_commit_recovers_existing_job():
    first, pending = Provider("assemblyai"), Provider("speechmatics")
    chain = stt_backend.RemoteFailoverBackend([first, pending])
    chain.set_attempt_context(chunk_id="chunk_p1", audio_sha256="sha", history=_history("completed"))
    assert chain._resume(pending)["request_id"] == "existing-job"
    chain.transcribe(np.full(16000, .1))
    assert first.calls == 0
    assert pending.calls == 1


@pytest.mark.parametrize("provider_name", ["deepgram", "groq"])
def test_sync_completion_without_local_commit_never_resubmits_automatically(provider_name):
    first, previous = Provider("assemblyai"), Provider(provider_name)
    chain = stt_backend.RemoteFailoverBackend([first, previous])
    rows = [{**_history("completed")[0], "provider": provider_name}]
    chain.set_attempt_context(chunk_id="chunk_p1", audio_sha256="sha", history=rows)
    with pytest.raises(stt_backend.RemoteJobPending) as caught:
        chain.transcribe(np.full(16000, .1))
    assert caught.value.ambiguous_submission
    assert first.calls == previous.calls == 0


def test_exhausted_credit_is_not_retried_for_every_chunk(monkeypatch):
    monkeypatch.setattr(stt_backend.time, "time", lambda: 1000.)
    response = SimpleNamespace(status_code=402, headers={})
    first = Provider("deepgram", stt_backend._error_for_http("deepgram", "fixture", response))
    second = Provider("groq")
    chain = stt_backend.RemoteFailoverBackend([first, second])
    chain.transcribe(np.full(16000, .1))
    chain.transcribe(np.full(16000, .1))
    assert first.calls == 1
    assert second.calls == 2
    assert chain._circuits["deepgram"] == 4600.


def test_rate_limit_without_header_waits_before_trying_again():
    error = stt_backend._error_for_http("groq", "fixture", SimpleNamespace(status_code=429, headers={}))
    assert error.retry_after_seconds == 60


def test_retry_after_understands_http_date(monkeypatch):
    now = 1700000000.
    monkeypatch.setattr(stt_backend.time, "time", lambda: now)
    header = format_datetime(datetime.fromtimestamp(now + 90, timezone.utc), usegmt=True)
    assert stt_backend._retry_after(SimpleNamespace(headers={"retry-after": header})) == 90.
