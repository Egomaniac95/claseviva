import pytest

from src import math_render


@pytest.mark.parametrize("code", [
    '```python\n# (y_i)\nexpression = r"\\(x_i\\)"\n```',
    '~~~python\n# (y_i)\nexpression = r"\\[x_i\\]"\n~~~',
    '````markdown\n```python\n# (y_i)\n```\nvalue = r"\\(x_i\\)"\n````',
    '~~~python\nvalue = r"\\(x_i\\)"\n~~~~',
    '```python\n# (y_i)\nvalue = r"\\(x_i\\)"',
    '``a ` literal, (y_i), \\(x_i\\)``',
])
def test_math_preserves_code_and_still_renders_surrounding_formula(code):
    before = "Antes: (a_i).\n\n"
    # Un fence sin cerrar conserva el resto como código, tal como Markdown.
    unclosed = code.startswith('```python') and not code.endswith('```')
    after = "" if unclosed else "\n\nDespués: (b_i)."
    document = before + code + after

    normalized = math_render.normalizar_matematica_markdown(document)
    assert normalized == r"Antes: \(a_i\)." + "\n\n" + code + (
        "" if unclosed else "\n\n" + r"Después: \(b_i\).")
    protected, replacements = math_render.protect_math(document)
    assert code in protected
    assert len(replacements) == (1 if unclosed else 2)
    assert all("<math" in value for value in replacements.values())


def test_unmatched_backtick_does_not_consume_code_in_later_paragraph():
    code = '`lemmatizador.lemmatize(palabra, pos="v")`'
    text = "Marca suelta ` sin cierre.\n\n" + code + "\n\nFórmula: (a_i)."
    protected, replacements = math_render.protect_math(text)
    assert code in protected
    assert len(replacements) == 1
