from src import audio_manifest, stt_usage, study_package


def _attempt(d, part, attempt="a", state="completed", **extra):
    audio_manifest.record_transcription_attempt(
        d, f"chunk_000000001000_p{part}_v10", state, attempt_id=attempt,
        provider="assemblyai", submitted_audio_seconds=20,
        original_audio_seconds=30, **extra)


def test_metrics_count_provider_request_once_and_ignore_worker_commit(tmp_path):
    _attempt(tmp_path, 1, state="started")
    _attempt(tmp_path, 1)
    _attempt(tmp_path, 1, attempt="worker", state="complete")
    stats = stt_usage.summarize(tmp_path)["parts"]["1"]["assemblyai"]
    assert stats["completed_requests"] == 1
    assert stats["submitted_seconds"] == 20
    assert stats["silence_seconds_saved"] == 10


def test_summary_survives_cleanup_and_reconsolidation_does_not_double_count(tmp_path):
    _attempt(tmp_path, 1)
    usage = stt_usage.summarize(tmp_path)
    payload = {"recording_parts": [{"part": 1}], "stt_usage": usage}
    (tmp_path / "leccion_final.md").write_text(study_package.metadata_comment(payload), encoding="utf-8")
    assert stt_usage.summarize(tmp_path) == usage
    audio_manifest.transcription_attempts_path(tmp_path).unlink()
    assert stt_usage.summarize(tmp_path) == usage
    _attempt(tmp_path, 2, attempt="b")
    continued = stt_usage.summarize(tmp_path)
    assert set(continued["parts"]) == {"1", "2"}
    assert continued["quota_remaining"] is None


def test_legacy_missing_duration_is_unknown_not_measured_silence(tmp_path):
    audio_manifest.record_transcription_attempt(
        tmp_path, "chunk_000000001000_p1_v10", "completed", attempt_id="legacy",
        provider="groq", submitted_audio_seconds=30)
    stats = stt_usage.summarize(tmp_path)["parts"]["1"]["groq"]
    assert stats["requests_without_original_duration"] == 1
    assert stats["silence_seconds_saved"] == 0
