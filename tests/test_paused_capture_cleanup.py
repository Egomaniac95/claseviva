import pytest

from src import audio_manifest


@pytest.mark.parametrize("requested_at,reason,expected", [
    (138.04, "abandoned_capture_without_valid_wav", "ok"),
    (137.9, "abandoned_capture_without_valid_wav", "warning"),
    (138.04, "device_lost", "warning"),
])
def test_pause_shutdown_without_pcm_is_discounted_only_inside_closed_pause(
        tmp_path, requested_at, reason, expected):
    # PCM clock lags acquisition by eight seconds; silence completed 1 ms
    # before the pause mark. A new acquisition was abandoned during the pause.
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "before", "status": "capturing", "observed_at": 108.02,
    })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "before", "status": "discarded_silence",
        "observed_at": 138.019, "rate": 100, "frame_count": 3000,
        "expected_frames": 3000, "pcm_wall_start": 100., "pcm_wall_end": 130.,
        "stream_id": "old", "start_frame": 0, "end_frame": 3000,
    })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "abandoned", "status": "capturing", "observed_at": requested_at,
        "wall_start": requested_at,
    })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "abandoned", "status": "capture_failed", "observed_at": 250.1,
        "wall_start": requested_at, "reason": reason, "error_type": "CaptureProcessInterrupted",
    })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "after", "status": "capturing", "observed_at": 250.2,
    })
    audio_manifest.append_event(tmp_path, {
        "chunk_id": "after", "status": "sealed", "observed_at": 280.3,
        "rate": 100, "frame_count": 3000, "expected_frames": 3000,
        "pcm_wall_start": 250.3, "pcm_wall_end": 280.3,
        "stream_id": "new", "start_frame": 0, "end_frame": 3000,
    })
    report = audio_manifest.continuity_report(
        tmp_path, declared_inactive_intervals=[(138.02, 250.)])
    assert report["status"] == expected
    assert report["wall_gap_max_seconds"] == 0
    # The uncontextualized audit still records the original failure.
    assert audio_manifest.capture_failure_report(tmp_path)["terminal_count"] == 1
    assert audio_manifest.continuity_report(tmp_path)["status"] == "warning"
