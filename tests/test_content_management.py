from __future__ import annotations

import os
from pathlib import Path

import pytest

from src import config, content_management, panel, student_notes


def _course_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "contenido"
    course = root / "curso-fixture"
    class_one = course / "clases" / "CLASE_01_uno"
    class_two = course / "clases" / "CLASE_02_dos"
    (class_one / "audio").mkdir(parents=True)
    (class_two / "img").mkdir(parents=True)
    (course / "resumenes").mkdir()
    (class_one / "audio" / "chunk.wav").write_bytes(b"wav-fixture")
    (class_one / "leccion_final.md").write_text("# Uno\n", encoding="utf-8")
    (class_two / "leccion_final.md").write_text("# Dos\n", encoding="utf-8")
    (course / "resumenes" / "resumen_CLASE_01_uno.md").write_text(
        "resumen uno\n", encoding="utf-8")
    (course / "CURSO.md").write_text(
        "# Bitácora\n\n"
        "- 📝 [manual] conservar esta nota\n\n"
        "## CLASE_01_uno (fecha)\ncontenido uno\n\n"
        "## CLASE_02_dos (fecha)\ncontenido dos\n",
        encoding="utf-8",
    )
    (course / "TAREAS.md").write_text(
        "# Tareas\n\n## CLASE_01_uno\ntarea uno\n\n"
        "## CLASE_02_dos\ntarea dos\n",
        encoding="utf-8",
    )
    return root, class_one, class_two


def _trash_mover(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)

    def move(source: str) -> None:
        path = Path(source)
        os.replace(path, destination / path.name)

    return move


def test_preview_is_read_only_and_inventories_class_sidecars(tmp_path: Path) -> None:
    root, class_one, _class_two = _course_fixture(tmp_path)
    before = sorted(
        (path.relative_to(root).as_posix(), path.stat().st_size)
        for path in root.rglob("*") if path.is_file())

    plan = content_management.build_deletion_plan(
        root, kind="class", course="curso-fixture", class_id="CLASE_01_uno")

    after = sorted(
        (path.relative_to(root).as_posix(), path.stat().st_size)
        for path in root.rglob("*") if path.is_file())
    assert before == after
    assert Path(plan.target) == class_one
    assert plan.file_count == 3
    assert plan.byte_count > 0
    assert plan.confirmation == "CLASE_01_uno"
    assert [Path(path).name for path in plan.sidecars] == [
        "resumen_CLASE_01_uno.md"]
    assert {Path(path).name for path in plan.documents_updated} == {
        "CURSO.md", "TAREAS.md"}
    assert len(plan.fingerprint) == 64


@pytest.mark.parametrize(
    ("course", "class_id"),
    [
        ("../curso-fixture", "CLASE_01_uno"),
        ("curso-fixture", "../CLASE_01_uno"),
        ("curso-fixture", "no-existe"),
    ],
)
def test_preview_rejects_traversal_and_missing_targets(
        tmp_path: Path, course: str, class_id: str) -> None:
    root, _class_one, _class_two = _course_fixture(tmp_path)

    with pytest.raises(content_management.ContentSelectionError):
        content_management.build_deletion_plan(
            root, kind="class", course=course, class_id=class_id)


def test_class_move_is_transactional_and_preserves_other_class_and_manual_notes(
        tmp_path: Path) -> None:
    root, class_one, class_two = _course_fixture(tmp_path)
    plan = content_management.build_deletion_plan(
        root, kind="class", course="curso-fixture", class_id="CLASE_01_uno")
    recycle = tmp_path / "papelera"

    completed = content_management.execute_deletion(
        root,
        kind=plan.kind,
        course=plan.course,
        class_id=plan.class_id,
        confirmation=plan.confirmation,
        expected_fingerprint=plan.fingerprint,
        send_to_trash=_trash_mover(recycle),
    )

    assert completed == plan
    assert not class_one.exists()
    assert class_two.is_dir()
    assert not (root / "curso-fixture" / "resumenes"
                / "resumen_CLASE_01_uno.md").exists()
    course_text = (root / "curso-fixture" / "CURSO.md").read_text(encoding="utf-8")
    tasks_text = (root / "curso-fixture" / "TAREAS.md").read_text(encoding="utf-8")
    assert "CLASE_01_uno" not in course_text + tasks_text
    assert "conservar esta nota" in course_text
    assert "CLASE_02_dos" in course_text + tasks_text
    bundle = next(recycle.iterdir())
    assert (bundle / content_management.MANIFEST_NAME).is_file()
    assert (bundle / "clase" / "CLASE_01_uno" / "leccion_final.md").is_file()
    assert (bundle / "documentos_originales" / "CURSO.md").is_file()


def test_failed_trash_move_rolls_back_class_sidecars_and_documents(tmp_path: Path) -> None:
    root, class_one, _class_two = _course_fixture(tmp_path)
    course_doc = root / "curso-fixture" / "CURSO.md"
    before = course_doc.read_bytes()
    plan = content_management.build_deletion_plan(
        root, kind="class", course="curso-fixture", class_id="CLASE_01_uno")

    def fail(_source: str) -> None:
        raise OSError("papelera fixture no disponible")

    with pytest.raises(OSError, match="papelera fixture"):
        content_management.execute_deletion(
            root, kind=plan.kind, course=plan.course, class_id=plan.class_id,
            confirmation=plan.confirmation,
            expected_fingerprint=plan.fingerprint,
            send_to_trash=fail,
        )

    assert class_one.is_dir()
    assert (root / "curso-fixture" / "resumenes"
            / "resumen_CLASE_01_uno.md").is_file()
    assert course_doc.read_bytes() == before
    assert not list((root / "curso-fixture").glob(
        f"{content_management.STAGE_PREFIX}*"))


def test_restored_recycle_bundle_recovers_class_and_course_documents(tmp_path: Path) -> None:
    root, class_one, _class_two = _course_fixture(tmp_path)
    original_course = (root / "curso-fixture" / "CURSO.md").read_bytes()
    plan = content_management.build_deletion_plan(
        root, kind="class", course="curso-fixture", class_id="CLASE_01_uno")
    recycle = tmp_path / "papelera"
    content_management.execute_deletion(
        root, kind=plan.kind, course=plan.course, class_id=plan.class_id,
        confirmation=plan.confirmation,
        expected_fingerprint=plan.fingerprint,
        send_to_trash=_trash_mover(recycle),
    )
    bundle = next(recycle.iterdir())
    restored = root / "curso-fixture" / bundle.name
    os.replace(bundle, restored)

    recovered = content_management.recover_interrupted_deletions(root)

    assert recovered == ["CLASE_01_uno"]
    assert class_one.is_dir()
    assert (root / "curso-fixture" / "resumenes"
            / "resumen_CLASE_01_uno.md").is_file()
    assert (root / "curso-fixture" / "CURSO.md").read_bytes() == original_course
    assert not restored.exists()


def test_changed_content_invalidates_preview_before_trash(tmp_path: Path) -> None:
    root, class_one, _class_two = _course_fixture(tmp_path)
    plan = content_management.build_deletion_plan(
        root, kind="class", course="curso-fixture", class_id="CLASE_01_uno")
    (class_one / "new.txt").write_text("changed", encoding="utf-8")
    called = []

    with pytest.raises(content_management.StaleDeletionPreview):
        content_management.execute_deletion(
            root, kind=plan.kind, course=plan.course, class_id=plan.class_id,
            confirmation=plan.confirmation,
            expected_fingerprint=plan.fingerprint,
            send_to_trash=lambda source: called.append(source),
        )

    assert called == []
    assert class_one.is_dir()


def test_course_move_and_pointer_cleanup_are_confined(tmp_path: Path) -> None:
    root, class_one, _class_two = _course_fixture(tmp_path)
    other = root / "otro-curso" / "clases" / "CLASE_01_otra"
    other.mkdir(parents=True)
    plan = content_management.build_deletion_plan(
        root, kind="course", course="curso-fixture")
    recycle = tmp_path / "papelera"
    project = tmp_path / "project"
    pointer = project / "clase_actual.txt"
    project.mkdir()
    pointer.write_text(str(class_one), encoding="utf-8")

    content_management.execute_deletion(
        root, kind=plan.kind, course=plan.course,
        confirmation=plan.confirmation,
        expected_fingerprint=plan.fingerprint,
        send_to_trash=_trash_mover(recycle),
    )
    cleared = content_management.clear_pointer_if_targeted(
        pointer, project, plan.target)

    assert cleared is True
    assert not pointer.exists()
    assert other.is_dir()
    assert (recycle / "curso-fixture").is_dir()


def _isolate_panel(monkeypatch, tmp_path: Path) -> Path:
    root, _class_one, _class_two = _course_fixture(tmp_path)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "CONTENIDO_DIR", root)
    monkeypatch.setattr(config, "PUNTERO_CLASE", tmp_path / "clase_actual.txt")
    monkeypatch.setattr(panel, "MODO_APP", True)
    monkeypatch.setattr(panel, "_POST_CORRIENDO", False)
    monkeypatch.setattr(panel, "_LAST_SHUTDOWN_TICKET", None)
    monkeypatch.setattr(panel, "HIJOS", [])
    monkeypatch.setattr(config, "diagnosticar_instancia", lambda *_a, **_k: {
        "owner_valid": False})
    monkeypatch.setattr(student_notes, "transcription_in_progress", lambda: False)
    return root


def test_panel_preview_and_delete_course_use_exact_confirmation(
        monkeypatch, tmp_path: Path) -> None:
    root = _isolate_panel(monkeypatch, tmp_path)
    recycle = tmp_path / "papelera"
    import send2trash
    monkeypatch.setattr(send2trash, "send2trash", _trash_mover(recycle))
    client = panel.app.test_client()

    preview_response = client.post(
        "/api/contenido/vista_previa_eliminar",
        json={"kind": "course", "course": "curso-fixture"},
    )
    preview = preview_response.get_json()
    rejected = client.post(
        "/api/contenido/eliminar",
        json={
            "kind": "course", "course": "curso-fixture",
            "confirmation": "incorrecto", "fingerprint": preview["fingerprint"],
        },
    )
    assert preview_response.status_code == 200
    assert preview["destination"] == "Papelera del sistema"
    assert rejected.status_code == 400
    assert (root / "curso-fixture").is_dir()

    accepted = client.post(
        "/api/contenido/eliminar",
        json={
            "kind": "course", "course": "curso-fixture",
            "confirmation": "curso-fixture", "fingerprint": preview["fingerprint"],
        },
    )

    assert accepted.status_code == 200
    assert accepted.get_json()["ok"] is True
    assert (recycle / "curso-fixture").is_dir()


def test_panel_rejects_viewer_mode_and_live_children(monkeypatch, tmp_path: Path) -> None:
    _isolate_panel(monkeypatch, tmp_path)
    client = panel.app.test_client()
    monkeypatch.setattr(panel, "MODO_APP", False)

    viewer = client.post(
        "/api/contenido/vista_previa_eliminar",
        json={"kind": "course", "course": "curso-fixture"},
    )

    assert viewer.status_code == 403
    monkeypatch.setattr(panel, "MODO_APP", True)

    class Alive:
        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(panel, "HIJOS", [{"p": Alive()}])
    preview = client.post(
        "/api/contenido/vista_previa_eliminar",
        json={"kind": "course", "course": "curso-fixture"},
    ).get_json()
    blocked = client.post(
        "/api/contenido/eliminar",
        json={
            "kind": "course", "course": "curso-fixture",
            "confirmation": "curso-fixture", "fingerprint": preview["fingerprint"],
        },
    )

    assert blocked.status_code == 409
    assert "procesos" in blocked.get_json()["msg"]


def test_panel_page_exposes_reversible_content_controls() -> None:
    assert 'id="deleteClassButton"' in panel.PAGINA
    assert 'id="deleteCourseButton"' in panel.PAGINA
    assert "/api/contenido/vista_previa_eliminar" in panel.PAGINA
    assert "/api/contenido/eliminar" in panel.PAGINA
    assert "Papelera" in panel.PAGINA
