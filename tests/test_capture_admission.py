import pytest

from src import audio_manifest, estado


@pytest.mark.parametrize("change", [{"estado": "pausado"}, {"estado": "fin"},
                                    {"audio_requires_attention": True},
                                    {"parte": 2}, {"velocidad": 1.5}])
def test_stale_capture_read_cannot_publish_after_pause_finish_or_configuration_change(tmp_path, change):
    old = {"estado": "grabando", "parte": 1, "velocidad": 1., "fuente": "sistema"}
    estado.escribir_estado(tmp_path, {**old, **change})
    accepted = estado.admitir_captura(tmp_path, old, lambda: audio_manifest.append_event(
        tmp_path, {"chunk_id": "reserved", "status": "capturing"}))
    assert not accepted
    assert audio_manifest.read_events(tmp_path) == []


@pytest.mark.parametrize("auto_pause", [False, True])
def test_active_and_automatic_silence_monitoring_still_admit_capture(tmp_path, auto_pause):
    state = {"estado": "grabando", "parte": 1, "velocidad": 1., "fuente": "sistema",
             "auto_pausa": auto_pause}
    estado.escribir_estado(tmp_path, state)
    seen = []
    assert estado.admitir_captura(tmp_path, state, lambda: seen.append("capturing"))
    assert seen == ["capturing"]
