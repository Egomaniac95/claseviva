from scripts import benchmark_sealed_publication as benchmark


def test_benchmark_sealed_compara_mismo_wav_y_mejora_latencias():
    result = benchmark.run_benchmark(
        chunks=3,
        rate=100,
        channels=1,
        canonical_seconds=30.0,
        capture_block_seconds=30.0,
        stt_poll_seconds=2.0,
        transcribe_seconds=3.0,
        commit_seconds=0.01,
    )

    assert result["ok"] is True
    assert all(result["invariant"].values())
    assert result["legacy"]["wav_sha256"] == result["immediate"]["wav_sha256"]
    assert result["legacy"]["completed_commits"] == 3
    assert result["immediate"]["completed_commits"] == 3
    assert result["legacy"]["audio_to_sealed"]["p95_seconds"] >= 30.0
    assert result["immediate"]["audio_to_sealed"]["p95_seconds"] < 1.0
    assert result["immediate"]["audio_to_commit"]["p95_seconds"] \
        < result["legacy"]["audio_to_commit"]["p95_seconds"]
