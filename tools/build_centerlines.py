"""Сборка одноштриховых центральных линий из эталонных TTF ГОСТ.

Запуск (нужны fonttools, pillow, scikit-image, numpy):
    python tools/build_centerlines.py

Скрипт читает gost_marker/GOST_A.TTF и gost_marker/GOST_B.ttf, извлекает
скелеты глифов и записывает gost_marker/fonts/centerlines.py.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from skimage.measure import label, regionprops
from skimage.morphology import skeletonize

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "gost_marker"
DEFAULT_OUTPUT = ROOT / "gost_marker" / "fonts" / "centerlines.py"

CAP_UNITS = 1400.0
RENDER_SIZE = 2048
PAD = 90
SPUR = 24.0
RDP_EPS = 1.2
# Максимальный излом, при котором два штриха в стыке считаются одним
# продолжающимся штрихом (0° — идеально прямая линия).
JOIN_TURN_DEG = 45.0
# Запасной размер окна (в пикселях рендера), если ширина штриха неизвестна.
JOIN_LOOK = 96
# Радиус, в котором концы штрихов считаются одним узлом скелета.
JUNCTION_RADIUS = 4.0

# Символы, которые в этих TTF отсутствуют или нарисованы неверно.
# Для них остаётся ручная реализация из gost.py.
SKIP_CHARS = set("×÷°–—")

CHARSET = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    " .,:;!?-()[]{}/\\\"'«»=+*%#№$<>|~^±"
)


def rdp(points, eps):
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) <= 2:
        return pts
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i]
        bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best_d = -1.0
        best_k = -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if norm < 1e-9:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dx * (py - ay) - dy * (px - ax)) / norm
            if d > best_d:
                best_d, best_k = d, k
        if best_d > eps:
            keep[best_k] = True
            stack.append((i, best_k))
            stack.append((best_k, j))
    return [p for p, k in zip(pts, keep) if k]


def smooth(path, passes=2):
    pts = [(float(x), float(y)) for x, y in path]
    for _ in range(passes):
        if len(pts) < 3:
            break
        out = [pts[0]]
        for i in range(1, len(pts) - 1):
            x = (pts[i - 1][0] + 2 * pts[i][0] + pts[i + 1][0]) / 4
            y = (pts[i - 1][1] + 2 * pts[i][1] + pts[i + 1][1]) / 4
            out.append((x, y))
        out.append(pts[-1])
        pts = out
    return pts


def plen(path):
    return sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))


def trace(sk, spur=SPUR):
    ys, xs = np.nonzero(sk)
    pix = {(int(x), int(y)) for x, y in zip(xs, ys)}
    if not pix:
        return []
    adj = {}
    for p in pix:
        x, y = p
        adj[p] = [
            (x + dx, y + dy)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (dx or dy) and (x + dx, y + dy) in pix
        ]
    deg = {p: len(v) for p, v in adj.items()}
    nodes = {p for p, d in deg.items() if d != 2}
    visited = set()

    def ek(a, b):
        return (a, b) if a <= b else (b, a)

    def walk(start, nxt):
        path = [start, nxt]
        visited.add(ek(start, nxt))
        prev, cur = start, nxt
        while cur != start and cur not in nodes:
            cand = [q for q in adj[cur] if q != prev]
            if not cand:
                break
            unvisited = [q for q in cand if ek(cur, q) not in visited]
            nxt2 = unvisited[0] if unvisited else cand[0]
            path.append(nxt2)
            visited.add(ek(cur, nxt2))
            prev, cur = cur, nxt2
            if len(path) > 20000:
                break
        return path

    paths = []
    for s in sorted(nodes):
        for nb in adj[s]:
            if ek(s, nb) not in visited:
                paths.append(walk(s, nb))
    for p in sorted(pix):
        for nb in adj[p]:
            if ek(p, nb) not in visited:
                paths.append(walk(p, nb))

    filtered = []
    for path in paths:
        length = plen(path)
        endpoint = deg[path[0]] == 1 or deg[path[-1]] == 1
        if len(path) < 3:
            continue
        if endpoint and length < spur:
            continue
        filtered.append(path)
    return filtered


def snap_junctions(paths, radius=JUNCTION_RADIUS):
    """Сводит близкие концы штрихов в одну точку.

    Скелет в Т-образном стыке часто заканчивает линии в соседних пикселях,
    поэтому без этого шага стык не выглядит общим узлом.
    """
    merged = [list(p) for p in paths]
    entries = []
    for i, path in enumerate(merged):
        if len(path) < 2:
            continue
        entries.append((i, 0))
        entries.append((i, 1))

    parent = list(range(len(entries)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a in range(len(entries)):
        ia, ea = entries[a]
        xa, ya = merged[ia][0 if ea == 0 else -1]
        for b in range(a + 1, len(entries)):
            ib, eb = entries[b]
            if ia == ib:
                continue
            xb, yb = merged[ib][0 if eb == 0 else -1]
            if math.hypot(xa - xb, ya - yb) <= radius:
                union(a, b)

    clusters: dict[int, list[int]] = {}
    for a in range(len(entries)):
        clusters.setdefault(find(a), []).append(a)

    for members in clusters.values():
        if len(members) < 2:
            continue
        points = []
        for a in members:
            i, end = entries[a]
            points.append(merged[i][0 if end == 0 else -1])
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        snapped = (round(cx), round(cy))
        for a in members:
            i, end = entries[a]
            if end == 0:
                merged[i][0] = snapped
            else:
                merged[i][-1] = snapped

    return merged


def merge_paths(paths):
    """Склеивает цепочки, у которых общий конец принадлежит ровно двум штрихам."""
    merged = [list(p) for p in paths]
    changed = True
    while changed:
        changed = False
        ends = {}
        for i, path in enumerate(merged):
            ends.setdefault(path[0], []).append((i, 0))
            ends.setdefault(path[-1], []).append((i, 1))
        for _point, entries in ends.items():
            if len(entries) != 2:
                continue
            (i, end_i), (j, end_j) = entries
            if i == j:
                continue
            a = merged[i]
            b = merged[j]
            if end_i == 0:
                a = a[::-1]
            if end_j == 1:
                b = b[::-1]
            merged[i] = a + b[1:]
            merged.pop(j)
            changed = True
            break
    out = []
    for path in merged:
        clean = []
        for p in path:
            if not clean or p != clean[-1]:
                clean.append(p)
        if len(clean) >= 2:
            out.append(clean)
    return out


def _outward_direction(path, end, look=JOIN_LOOK):
    """Единичный вектор из точки стыка внутрь штриха.

    Берётся направление не до соседнего пикселя, а до точки на расстоянии ``look``:
    рядом со стыком скелет изгибается, и мгновенное направление обманывает.
    """
    pts = path[: look + 1] if end == 0 else path[-look - 1 :][::-1]
    if len(pts) < 2:
        return 0.0, 0.0
    x0, y0 = pts[0]
    x1, y1 = pts[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = math.hypot(dx, dy)
    if norm <= 1e-6:
        return 0.0, 0.0
    return dx / norm, dy / norm


def join_through_junctions(paths, max_turn_deg=JOIN_TURN_DEG, look=JOIN_LOOK):
    """Склеивает штрихи, которые проходят через стык почти по прямой.

    Скелет буквы в Т-образном стыке всегда разрывает длинную линию:
    например, у «Н» левая и правая стойки распадаются на верхнюю и нижнюю
    половины. Такие половины лежат на одной прямой по разные стороны от
    узла, поэтому их можно рисовать без подъёма пера.
    """
    limit = math.cos(math.radians(max_turn_deg))
    merged = [list(p) for p in paths]

    while True:
        ends: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for i, path in enumerate(merged):
            if len(path) < 2:
                continue
            ends.setdefault(path[0], []).append((i, 0))
            ends.setdefault(path[-1], []).append((i, 1))

        best = None
        for entries in ends.values():
            if len(entries) < 2:
                continue
            for a in range(len(entries)):
                for b in range(a + 1, len(entries)):
                    i, end_i = entries[a]
                    j, end_j = entries[b]
                    if i == j:
                        continue
                    ox1, oy1 = _outward_direction(merged[i], end_i, look)
                    ox2, oy2 = _outward_direction(merged[j], end_j, look)
                    # Векторы «от стыка внутрь штриха» должны быть
                    # противоположны: тогда линия проходит стык насквозь.
                    dot = ox1 * ox2 + oy1 * oy2
                    if dot <= -limit and (best is None or dot < best[0]):
                        best = (dot, i, end_i, j, end_j)

        if best is None:
            break

        _dot, i, end_i, j, end_j = best
        first = merged[i]
        second = merged[j]
        if end_i == 0:
            first = first[::-1]
        if end_j == 1:
            second = second[::-1]
        merged[i] = first + second[1:]
        merged.pop(j)

    out = []
    for path in merged:
        clean = []
        for point in path:
            if not clean or point != clean[-1]:
                clean.append(point)
        if len(clean) >= 2:
            out.append(clean)
    return out


def circle(centroid, radius, n=10):
    cx, cy = centroid
    return [
        (
            cx + radius * math.cos(2.0 * math.pi * i / n),
            cy + radius * math.sin(2.0 * math.pi * i / n),
        )
        for i in range(n + 1)
    ]


def build_glyph(pil_font, ch, stroke_px, dot_radius_px):
    bbox = pil_font.getbbox(ch, anchor="ls")
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0:
        return None

    width = x1 - x0 + PAD * 2
    height = y1 - y0 + PAD * 2
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    draw.text((PAD - x0, PAD - y0), ch, font=pil_font, fill=255, anchor="ls")
    mask = np.array(image) > 127
    origin_x = PAD - x0
    baseline = PAD - y0

    labels = label(mask, connectivity=2)
    pixel_strokes = []
    for region in regionprops(labels):
        r0, c0, r1, c1 = region.bbox
        sub = labels[r0:r1, c0:c1] == region.label
        hh, ww = sub.shape
        area = int(sub.sum())
        is_dot = (
            ww < 2.0 * stroke_px
            and hh < 2.0 * stroke_px
            and area / max(1, ww * hh) > 0.55
        )
        if is_dot:
            cy, cx = region.centroid
            pixel_strokes.append(circle((cx, cy), dot_radius_px))
            continue

        sk = skeletonize(sub)
        if not sk.any():
            continue
        paths = join_through_junctions(
            merge_paths(snap_junctions(trace(sk))), look=stroke_px
        )
        for path in paths:
            if len(path) < 2:
                continue
            path = [(x + c0, y + r0) for x, y in path]
            path = smooth(path, passes=2)
            path = rdp(path, eps=RDP_EPS)
            if len(path) < 2 or plen(path) < 1.5:
                continue
            pixel_strokes.append(path)

    strokes = []
    for path in pixel_strokes:
        pts = [
            (round((x - origin_x) / CAP_UNITS, 4), round((baseline - y) / CAP_UNITS, 4))
            for x, y in path
        ]
        clean = []
        for p in pts:
            if not clean or p != clean[-1]:
                clean.append(p)
        if len(clean) >= 2:
            strokes.append(clean)
    return strokes


def render_font(ttf_path: Path, key: str):
    ttf = TTFont(str(ttf_path))
    cmap = ttf.getBestCmap()
    glyf = ttf["glyf"]
    hmtx = ttf["hmtx"]
    pil = ImageFont.truetype(str(ttf_path), RENDER_SIZE)

    stroke_px = 100 if key == "A" else 140
    dot_radius_px = 32 if key == "A" else 42

    glyphs: dict[str, tuple[float, list[list[tuple[float, float]]]]] = {}
    for ch in CHARSET:
        cp = ord(ch)
        if ch == " " or ch in SKIP_CHARS or cp not in cmap:
            continue
        name = cmap[cp]
        if name == ".notdef":
            continue
        strokes = build_glyph(pil, ch, stroke_px, dot_radius_px)
        if not strokes:
            continue
        advance = hmtx[name][0] / CAP_UNITS
        glyphs[ch] = (round(advance, 4), strokes)

    space_name = cmap.get(32)
    word_spacing = hmtx[space_name][0] / CAP_UNITS if space_name else 0.43
    x_name = cmap.get(ord("x"))
    x_height = glyf[x_name].yMax / CAP_UNITS if x_name else 0.70

    return {
        "ascent": round(ttf["hhea"].ascent / CAP_UNITS, 4),
        "descent": round(ttf["hhea"].descent / CAP_UNITS, 4),
        "x_height": round(x_height, 4),
        "word_spacing": round(word_spacing, 4),
        "line_spacing": 1.57 if key == "A" else 1.70,
        "letter_spacing": 0.0,
        "line_width": round(1.0 / 14.0 if key == "A" else 1.0 / 10.0, 6),
        "glyphs": glyphs,
    }


def format_data(data: dict) -> str:
    out = [
        '"""Центральные линии, извлечённые из GOST_A.TTF и GOST_B.ttf."""',
        "",
        "# Сгенерировано tools/build_centerlines.py. Не редактировать вручную.",
        "",
        "DATA = {",
    ]
    for key in ("A", "B"):
        item = data[key]
        out.append(f'    "{key}": {{')
        for field in (
            "ascent",
            "descent",
            "x_height",
            "word_spacing",
            "line_spacing",
            "letter_spacing",
            "line_width",
        ):
            out.append(f'        "{field}": {item[field]},')
        out.append('        "glyphs": {')
        for ch, (advance, strokes) in item["glyphs"].items():
            stroke_text = ", ".join(
                "[" + ", ".join(f"({x}, {y})" for x, y in stroke) + "]"
                for stroke in strokes
            )
            out.append(f'            {ch!r}: ({advance}, [{stroke_text}]),')
        out.append("        },")
        out.append("    },")
    out.append("}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font-a", default=str(FONT_DIR / "GOST_A.TTF"))
    parser.add_argument("--font-b", default=str(FONT_DIR / "GOST_B.ttf"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    data = {
        "A": render_font(Path(args.font_a), "A"),
        "B": render_font(Path(args.font_b), "B"),
    }
    output = Path(args.output)
    output.write_text(format_data(data), encoding="utf-8")
    for key in ("A", "B"):
        glyphs = data[key]["glyphs"]
        points = sum(len(s) for _, strokes in glyphs.values() for s in strokes)
        print(f"{key}: {len(glyphs)} глифов, {points} точек -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
