from src import transcript_quality


def _segment(index: int, text: str) -> dict:
    return {
        "t": float(index * 10),
        "fin": float(index * 10 + 8),
        "texto": text,
        "chunk": f"chunk_{index // 3}.wav",
    }


def test_replay_contiguo_se_excluye_solo_de_la_vista_derivada():
    original = [
        _segment(0, "We define the loss function for this model."),
        _segment(1, "The gradient tells us how the parameters should change."),
        _segment(2, "The learning rate controls the size of every update."),
        _segment(3, "Now we compare training data with validation data."),
        _segment(20, "We define the loss function for this model."),
        _segment(21, "The gradient tells us how the parameters should change."),
        _segment(22, "The learning rate controls the size of every update."),
        _segment(23, "Now we compare training data with validation data."),
    ]

    filtered, report = transcript_quality.filter_replayed_segments(original)

    assert [row["t"] for row in filtered] == [0.0, 10.0, 20.0, 30.0]
    assert report["replayed_segments"] == 4
    assert report["replayed_runs"] == 1
    assert report["runs"][0]["separation_seconds"] >= 60
    assert original[4]["texto"].startswith("We define")


def test_enfasis_aislado_no_se_confunde_con_replay():
    rows = [
        _segment(0, "The learning rate is important for convergence."),
        _segment(20, "The learning rate is important for convergence."),
        _segment(21, "A new example starts here with different evidence."),
    ]

    filtered, report = transcript_quality.filter_replayed_segments(rows)

    assert filtered == rows
    assert report["replayed_segments"] == 0


def test_comparacion_oficial_es_determinista_y_reporta_hash():
    rows = [
        _segment(0, "Neural networks adjust weights with gradient descent."),
        _segment(1, "The loss measures prediction error."),
    ]

    report = transcript_quality.compare_with_official_text(
        "Neural networks adjust weights using gradient descent. "
        "The loss measures prediction error.",
        rows,
    )

    assert report["official_words"] == 12
    assert report["captured_words"] == 12
    assert 0 < report["wer"] < 0.2
    assert len(report["official_sha256"]) == 64
