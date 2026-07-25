from src import post_procesar


def _segments(start: float, text: str, count: int = 4) -> list[dict]:
    return [
        {"t": start + idx * 55.0, "fin": start + idx * 55.0 + 45.0,
         "texto": text}
        for idx in range(count)
    ]


def test_congreso_multiponente_relacionado_permanece_en_una_leccion():
    transcript = []
    transcript += _segments(
        0.0,
        "La primera ponente explica CRISPR Cas9, guide RNA y corte del ADN en edición genética.",
    )
    transcript += _segments(
        240.0,
        "Ahora pasamos al siguiente ponente: vectores AAV, nanopartículas lipídicas, "
        "entrega tisular y efectos off-target del tratamiento.",
    )
    transcript += _segments(
        480.0,
        "La tercera profesora aborda consentimiento informado, justicia distributiva, "
        "bioética clínica y regulación de terapias germinales.",
    )

    segments = post_procesar.segmentar_transcript_por_tema(
        transcript, [], "congreso-edicion-genetica", "Edición genética y terapias CRISPR")

    assert not any(
        item["clasificacion"] == "otra_clase_legitima" for item in segments)


def test_cambio_de_ponente_por_si_solo_no_crea_otra_leccion():
    transcript = _segments(
        0.0, "Ponente Ana: mecanismo molecular de edición del genoma con CRISPR.")
    transcript += _segments(
        240.0,
        "Now let's move to our next speaker. Ponente Luis: seguridad, acceso clínico y ética.")

    segments = post_procesar.segmentar_transcript_por_tema(
        transcript, [], "congreso-genomica", "Congreso de edición del genoma")

    assert not any(
        item["clasificacion"] == "otra_clase_legitima" for item in segments)


def test_bloque_sostenido_y_explicitamente_ajeno_si_separa_leccion():
    transcript = _segments(
        0.0, "Deep learning neural networks gradient descent loss optimization.")
    transcript += _segments(
        240.0,
        "Welcome to class. Today we will study English literature, poetry, grammar, "
        "narrative voice and essay writing.",
        count=6,
    )

    segments = post_procesar.segmentar_transcript_por_tema(
        transcript, [], "deep-learning", "Neural network training")

    assert any(
        item["clasificacion"] == "otra_clase_legitima" for item in segments)
