"""Геометрические примитивы для одноштриховых шрифтов.

Все координаты глифов задаются в нормированной системе:
* высота прописных букв = 1.0;
* базовая линия = 0.0;
* высота строчных = 0.70;
* нижние выносные элементы = -0.22;
* ось X направлена вправо, ось Y — вверх.

Шрифт хранит только центральные линии штрихов. Толщина линии
при генерации G-кода не используется.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Point = tuple[float, float]
Stroke = list[Point]


def line(x1: float, y1: float, x2: float, y2: float) -> Stroke:
    """Отрезок прямой."""
    return [(float(x1), float(y1)), (float(x2), float(y2))]


def poly(*points: Point) -> Stroke:
    """Ломаная по заданным точкам."""
    return [(float(x), float(y)) for x, y in points]


def arc(
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    a0: float,
    a1: float,
    n: int = 12,
) -> Stroke:
    """Дуга эллипса.

    Углы задаются в градусах: 0 — вправо, 90 — вверх, 180 — влево,
    270 (или -90) — вниз.
    """
    if n < 2:
        n = 2
    pts: Stroke = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts


def ellipse(cx: float, cy: float, rx: float, ry: float, n: int = 28) -> Stroke:
    """Замкнутый эллипс."""
    return arc(cx, cy, rx, ry, 0.0, 360.0, n)


def smooth(points: Sequence[Point], n: int = 8) -> Stroke:
    """Сглаженная кривая Catmull-Rom через заданные точки.

    Используется для скруглений, где не хочется вручную подбирать
    параметры эллиптических дуг. Концы кривой фиксируются.
    """
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 3:
        return pts
    padded = [pts[0], *pts, pts[-1]]
    out: Stroke = []
    for i in range(len(padded) - 3):
        p0, p1, p2, p3 = padded[i], padded[i + 1], padded[i + 2], padded[i + 3]
        for j in range(n):
            t = j / n
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * (
                (2.0 * p1[0])
                + (-p0[0] + p2[0]) * t
                + (2.0 * p0[0] - 5.0 * p1[0] + 4.0 * p2[0] - p3[0]) * t2
                + (-p0[0] + 3.0 * p1[0] - 3.0 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                (2.0 * p1[1])
                + (-p0[1] + p2[1]) * t
                + (2.0 * p0[1] - 5.0 * p1[1] + 4.0 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3.0 * p1[1] - 3.0 * p2[1] + p3[1]) * t3
            )
            out.append((x, y))
    out.append(pts[-1])
    return out


def bowl_right(
    left_x: float,
    right_x: float,
    top_y: float,
    bottom_y: float,
    n: int = 10,
) -> Stroke:
    """Правая половина «чаши»: от верхней точки к нижней через правый бок."""
    cy = (top_y + bottom_y) / 2.0
    ry = abs(top_y - bottom_y) / 2.0
    rx = abs(right_x - left_x)
    return arc(left_x, cy, rx, ry, 90.0, -90.0, n)


def bowl_left(
    right_x: float,
    left_x: float,
    top_y: float,
    bottom_y: float,
    n: int = 10,
) -> Stroke:
    """Левая половина «чаши»: от верхней точки к нижней через левый бок."""
    cy = (top_y + bottom_y) / 2.0
    ry = abs(top_y - bottom_y) / 2.0
    rx = abs(right_x - left_x)
    return arc(right_x, cy, rx, ry, 90.0, 270.0, n)


def dot(cx: float, cy: float, r: float = 0.035) -> Stroke:
    """Точка в виде маленькой окружности (чтобы её было видно на плоттере)."""
    return ellipse(cx, cy, r, r, 8)


def clean_stroke(points: Iterable[Point], eps: float = 1e-9) -> Stroke:
    """Убирает подряд идущие дубликаты точек."""
    out: Stroke = []
    for p in points:
        if not out or abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append((float(p[0]), float(p[1])))
    return out
