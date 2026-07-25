"""dhash propio (64 bits) para detectar cambios de diapositiva. Sin scipy/ImageHash."""
from PIL import Image

TAM = 8  # 8x8 -> hash de 64 bits


def dhash(img: Image.Image, tam: int = TAM) -> int:
    g = img.convert("L").resize((tam + 1, tam), Image.LANCZOS)
    px = g.tobytes()
    bits = 0
    for fila in range(tam):
        for col in range(tam):
            izq = px[fila * (tam + 1) + col]
            der = px[fila * (tam + 1) + col + 1]
            bits = (bits << 1) | (1 if izq > der else 0)
    return bits


def distancia(h1: int, h2: int) -> int:
    """Distancia de Hamming entre dos hashes (0 = idénticas)."""
    return bin(h1 ^ h2).count("1")
