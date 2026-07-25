"""Normalización LaTeX y render offline a MathML para Markdown de estudio."""
from __future__ import annotations

import html
import re


_CODE_SPLIT = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")
_BLOCK = re.compile(r"\\\[([\s\S]*?)\\\]")
_INLINE = re.compile(r"\\\((.+?)\\\)")
_LATEX_SIGNAL = re.compile(
    r"(?:\\(?:text|sum|frac|log|hat|sqrt|cdot|times|approx|leq|geq|infty)\b|"
    r"[A-Za-z]\s*[_^]\s*(?:\{|[A-Za-z0-9])|[=]|[_^]\{)")


def _normalizar_fragmento(text: str) -> str:
    # Salida observada en una lección antigua: \(\)CCE ... \(\).
    text = re.sub(
        r"\\\(\s*\\\)\s*(.+?)\s*\\\(\s*\\\)",
        lambda match: r"\(" + match.group(1).strip() + r"\)",
        text,
    )

    # Python-Markdown no entiende ``[ \text{...} ]`` como bloque matemático.
    def bad_block(match: re.Match) -> str:
        body = match.group(1).strip()
        return r"\[" + body + r"\]" if _LATEX_SIGNAL.search(body) else match.group(0)

    text = re.sub(r"(?<!\\)\[\s*([^\[\]\n]{1,1000}?)\s*\]", bad_block, text)

    # Igual para expresiones inline como ``(y_i)``; la señal evita tocar prosa.
    def bad_inline(match: re.Match) -> str:
        body = match.group(1).strip()
        return r"\(" + body + r"\)" if _LATEX_SIGNAL.search(body) else match.group(0)

    return re.sub(r"(?<!\\)\(([^()\n]{1,300})\)", bad_inline, text)


def normalizar_matematica_markdown(text: str) -> str:
    """Estandariza delimitadores sin modificar bloques/código inline."""
    pieces = _CODE_SPLIT.split(str(text or ""))
    return "".join(
        piece if index % 2 else _normalizar_fragmento(piece)
        for index, piece in enumerate(pieces)
    )


def _mathml(latex: str, *, block: bool) -> str:
    try:
        from latex2mathml.converter import convert
    except ImportError as exc:
        raise RuntimeError(
            "Falta latex2mathml. Instala requirements.txt en .venv para "
            "renderizar fórmulas sin depender de Internet.") from exc
    try:
        rendered = str(convert(str(latex).strip()))
    except Exception:
        return (
            f'<code class="math-error">{html.escape(str(latex).strip())}</code>')
    css = "math-block" if block else "math-inline"
    rendered = re.sub(
        r"<math\b", f'<math class="{css}"', rendered, count=1)
    if block:
        rendered = re.sub(
            r'<math class="math-block"',
            '<math class="math-block" display="block"', rendered, count=1)
    return rendered


def protect_math(text: str) -> tuple[str, dict[str, str]]:
    """Sustituye LaTeX por tokens antes de Markdown/sanitización."""
    normalized = normalizar_matematica_markdown(text)
    replacements: dict[str, str] = {}
    counter = 0

    def token(latex: str, *, block: bool) -> str:
        nonlocal counter
        marker = f"CLASEVIVAMATH{counter:06d}TOKEN"
        counter += 1
        replacements[marker] = _mathml(latex, block=block)
        return marker

    def protect_piece(piece: str) -> str:
        piece = _BLOCK.sub(
            lambda match: token(match.group(1), block=True), piece)
        return _INLINE.sub(
            lambda match: token(match.group(1), block=False), piece)

    pieces = _CODE_SPLIT.split(normalized)
    protected = "".join(
        piece if index % 2 else protect_piece(piece)
        for index, piece in enumerate(pieces)
    )
    return protected, replacements


def restore_math(safe_html: str, replacements: dict[str, str]) -> str:
    rendered = str(safe_html or "")
    for marker, mathml in replacements.items():
        rendered = rendered.replace(marker, mathml)
    return rendered
