"""Пошаговая диагностика извлечения осевых линий одного глифа.

Печатает результат каждого этапа: контуры, середины пар, узлы графа с их
степенями, цепочки обхода и результат каждой сшивки. Нужен, чтобы видеть,
на каком именно шаге теряется топология, а не подбирать константы вслепую.

    python tools/diag_glyph.py 0x41D
    python tools/diag_glyph.py 0x425 --font b

Символ задаётся кодом в hex: кириллица в аргументах командной строки
Windows может быть искажена кодовой страницей консоли.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_centerlines as bc  # noqa: E402
from fontTools.ttLib import TTFont  # noqa: E402

FONTS = {"a": "GOST_A.TTF", "b": "GOST_B.ttf"}


def plot(points, cap: float, columns: int = 64) -> None:
    """ASCII-карта точек: видно форму, разрывы и выбросы."""
    if not points:
        print("    (пусто)")
        return
    xs = [p[0] / cap for p in points]
    ys = [p[1] / cap for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    span_x = max(x1 - x0, 1e-6)
    span_y = max(y1 - y0, 1e-6)
    rows = max(1, int(round(columns * span_y / span_x / 2.0)))
    grid = [[" "] * columns for _ in range(rows + 1)]
    for x, y in zip(xs, ys):
        cx = min(columns - 1, int((x - x0) / span_x * (columns - 1)))
        cy = rows - min(rows, int((y - y0) / span_y * rows))
        grid[cy][cx] = "#"
    for row in grid:
        print("    " + "".join(row))


def show(paths, cap: float) -> None:
    """Список ломаных с габаритами в единицах высоты прописных."""
    if not paths:
        print("    (пусто)")
        return
    for index, path in enumerate(paths):
        span_x = (max(p[0] for p in path) - min(p[0] for p in path)) / cap
        span_y = (max(p[1] for p in path) - min(p[1] for p in path)) / cap
        text = " ".join(f"({x / cap:.3f},{y / cap:.3f})" for x, y in path)
        print(f"    [{index}] {len(path)} тчк, размах ({span_x:.3f}, {span_y:.3f})")
        print(f"        {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика одного глифа")
    parser.add_argument("char", help="код символа в hex, например 0x41D")
    parser.add_argument("--font", default="a", choices=sorted(FONTS))
    args = parser.parse_args()

    code = int(args.char, 16)
    ttf_path = bc.FONT_DIR / FONTS[args.font]
    ttf = TTFont(str(ttf_path))
    cmap = ttf.getBestCmap()
    glyph_set = ttf.getGlyphSet()

    cap = bc.detect_cap_units(glyph_set, cmap)
    d = bc.detect_stroke_units(glyph_set, cmap, cap)
    name = cmap.get(code)
    print(
        f"{ttf_path.name}: глиф {name!r}, прописные {cap:.1f}, "
        f"штрих {d:.2f} ({d / cap:.4f})"
    )
    if not name:
        print("глиф отсутствует в cmap")
        return 1

    contours = bc.glyph_contours(glyph_set, name, target=0.15 * d)
    print(f"\n== контуры: {len(contours)}, точек {sum(len(c) for c in contours)}")
    for index, contour in enumerate(contours):
        x0, y0, x1, y1 = bc._bbox([contour])
        print(
            f"    [{index}] {len(contour)} тчк, "
            f"габарит ({x0 / cap:.3f},{y0 / cap:.3f})-({x1 / cap:.3f},{y1 / cap:.3f})"
        )

    dots, rest = bc._detect_dots(contours, d)
    print(f"\n== точечных контуров: {len(dots)}, рабочих контуров: {len(rest)}")

    midpoints, counts = bc._extract_midpoints(rest, d)
    print(f"\n== середин пар: {len(midpoints)}")
    plot([mid for _key, mid in midpoints], cap)

    nodes, adj = bc._build_graph(midpoints, counts, d)
    degrees: dict[int, int] = {}
    for index in range(len(nodes)):
        degree = len(adj[index])
        degrees[degree] = degrees.get(degree, 0) + 1
    print(f"\n== узлов: {len(nodes)}, степени: {sorted(degrees.items())}")
    for index in range(len(nodes)):
        if len(adj[index]) == 2:
            continue
        x, y = nodes[index]
        print(f"    узел {index}, степень {len(adj[index])}, ({x / cap:.3f},{y / cap:.3f})")
        for other in sorted(adj[index]):
            ox, oy = nodes[other]
            print(f"        -> {other} ({ox / cap:.3f},{oy / cap:.3f})")

    chains = bc._trace(nodes, adj)
    print(f"\n== цепочек обхода: {len(chains)}")

    eps = bc.RDP_EPS * cap
    strokes = []
    for chain in chains:
        path = bc._simplify(chain, eps, bc.CORNER_DEG)
        if len(path) >= 2 and bc._length(path) > 0.2 * d:
            strokes.append(path)
    print(f"\n== штрихов после упрощения: {len(strokes)}")
    show(strokes, cap)

    strokes = bc._close_gaps(strokes, d, math.cos(math.radians(bc.COLLINEAR_DEG)))
    print(f"\n== после сшивки коллинеарных: {len(strokes)}")
    show(strokes, cap)

    strokes = bc._merge_coincident(strokes, d)
    print(f"\n== после склейки общих узлов: {len(strokes)}")
    show(strokes, cap)

    strokes = bc._close_gaps(strokes, d, -1.0)
    print(f"\n== после сшивки митр: {len(strokes)}")
    show(strokes, cap)

    strokes = bc._merge_coincident(strokes, d)
    strokes = bc._snap_ends(strokes, d)
    print(f"\n== после привязки концов: {len(strokes)}")
    show(strokes, cap)

    strokes = bc._join_collinear(strokes, d)
    print(f"\n== после склейки коллинеарных концов: {len(strokes)}")
    show(strokes, cap)

    strokes = bc._trim_free_ends(strokes, d)
    print(f"\n== после подрезки свободных концов: {len(strokes)}")
    show(strokes, cap)

    print(f"\n== итог extract_strokes: {len(bc.extract_strokes(contours, d, cap))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
