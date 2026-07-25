from src import panel


def test_markdown_renderiza_imagen_local_y_bloquea_traversal_y_script():
    rendered = panel.a_html(
        '| Evidencia | Explicación |\n'
        '|---|---|\n'
        '| <img src="img/slide_01.jpg" width="760"> | Concepto seguro |\n\n'
        '<img src="/img/../session_state.json">\n'
        '<script>alert(1)</script>'
    )

    assert 'src="/img/slide_01.jpg"' in rendered
    assert "md-image-local" in rendered
    assert "session_state.json" not in rendered
    assert "<script>" not in rendered


def test_pagina_leccion_aplica_layout_visual_izquierda_texto_derecha():
    page = panel._pagina_leccion_html(
        '# Redes neuronales\n\n'
        '| 🖼️ Evidencia visual | 📖 Explicación para estudiar |\n'
        '|---|---|\n'
        '| <img src="img/network.jpg" width="760"> | **Idea:** capas conectadas |'
    )

    assert '<html lang="es">' in page
    assert "width:61%" in page
    assert "width:39%" in page
    assert 'src="/img/network.jpg"' in page
    assert 'href="/leccion_final.md"' in page


def test_rutas_leccion_ofrecen_html_y_markdown_sin_cambiar_archivo(
        monkeypatch, tmp_path):
    lesson = tmp_path / "leccion_final.md"
    original = '# Clase sintética\n\n<img src="img/a.jpg" width="760">\n'
    lesson.write_text(original, encoding="utf-8")
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "a.jpg").write_bytes(b"fixture")
    monkeypatch.setattr(panel, "_clase", lambda: tmp_path)
    client = panel.app.test_client()

    html_response = client.get("/leccion_final")
    md_response = client.get("/leccion_final.md")

    assert html_response.status_code == 200
    assert html_response.mimetype == "text/html"
    assert b"Clase sint" in html_response.data
    assert md_response.status_code == 200
    assert md_response.mimetype == "text/markdown"
    assert lesson.read_text(encoding="utf-8") == original


def test_matematica_latex_se_renderiza_offline_como_mathml():
    rendered = panel.a_html(
        r"La probabilidad es \(\hat{p}_1 = 0.60\)." "\n\n"
        r"\[\text{CCE} = -\sum_{i=1}^{K} y_i \cdot "
        r"\log(\hat{p}_i)\]"
    )

    assert "<math" in rendered
    assert "math-inline" in rendered
    assert "math-block" in rendered
    assert "\\text{CCE}" not in rendered
    assert "\\hat{p}" not in rendered
    assert "<script" not in rendered


def test_normalizador_repara_delimitadores_matematicos_ambiguos():
    rendered = panel.a_html(
        r"[ \text{CCE} = -\log(0.60) \approx 0.51 ] y (y_i) vale cero."
    )

    assert rendered.count("<math") == 2
    assert "[ \\text" not in rendered
    assert "(y_i)" not in rendered
