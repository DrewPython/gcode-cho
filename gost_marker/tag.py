"""Геометрия бирки У-136 (равносторонний треугольник)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .fonts.primitives import Point

BBox = tuple[float, float, float, float]
Polyline = list[Point]


def equilateral_height(side: float) -> float:
    return side * math.sqrt(3.0) / 2.0


@dataclass(frozen=True)
class TagGeometry:
    """Треугольная бирка, лежащая на столе.

    Основание — от (0, 0) до (side, 0), вершина — вверх.
    """

    side: float = 62.0
    margin: float = 2.0

    @property
    def height(self) -> float:
        return equilateral_height(self.side)

    @property
    def vertices(self) -> tuple[Point, Point, Point]:
        return (0.0, 0.0), (self.side, 0.0), (self.side / 2.0, self.height)

    @property
    def centroid(self) -> Point:
        return self.side / 2.0, self.height / 3.0

    @property
    def inradius(self) -> float:
        return self.height / 3.0

    @property
    def safe_vertices(self) -> tuple[Point, Point, Point]:
        """Треугольник, отступивший от краёв на margin."""
        cx, cy = self.centroid
        r = self.inradius
        if self.margin <= 0:
            return self.vertices
        scale = max(0.05, (r - self.margin) / r)
        return tuple((cx + (x - cx) * scale, cy + (y - cy) * scale) for x, y in self.vertices)  # type: ignore[return-value]

    def point_inside(self, x: float, y: float, safe: bool = False, eps: float = 1e-9) -> bool:
        verts = self.safe_vertices if safe else self.vertices
        return point_in_convex_polygon((x, y), verts, eps)

    def bbox_inside(self, bbox: BBox | None, safe: bool = True) -> bool:
        """Проверяет, что все четыре угла bbox лежат внутри треугольника."""
        if not bbox:
            return True
        x0, y0, x1, y1 = bbox
        corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
        return all(self.point_inside(x, y, safe=safe) for x, y in corners)

    def slots(self) -> list[list[Point]]:
        """Приблизительное расположение двух продолговатых отверстий.

        Нужны только для наглядности: реальная ориентация бирки на столе
        задаётся пользователем.
        """
        s = self.side
        slot_l = 0.18 * s
        slot_w = 3.5
        y = 0.09 * self.height
        x1 = 0.10 * s
        x2 = s - 0.10 * s - slot_l
        return [
            capsule_outline(x1, y, x1 + slot_l, y, slot_w / 2.0),
            capsule_outline(x2, y, x2 + slot_l, y, slot_w / 2.0),
        ]


def capsule_outline(x1: float, y1: float, x2: float, y2: float, r: float, n: int = 8) -> Polyline:
    """Контур капсулы (продолговатого отверстия)."""
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return []
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    pts: Polyline = []
    # полуокружность у второй точки (от -90° до +90°)
    for i in range(n + 1):
        a = -math.pi / 2.0 + math.pi * i / n
        pts.append((x2 + r * (math.cos(a) * ux + math.sin(a) * nx),
                    y2 + r * (math.cos(a) * uy + math.sin(a) * ny)))
    # полуокружность у первой точки (от +90° до +270°)
    for i in range(n + 1):
        a = math.pi / 2.0 + math.pi * i / n
        pts.append((x1 + r * (math.cos(a) * ux + math.sin(a) * nx),
                    y1 + r * (math.cos(a) * uy + math.sin(a) * ny)))
    pts.append(pts[0])
    return pts


def point_in_convex_polygon(p: Point, poly: tuple[Point, ...] | list[Point], eps: float = 1e-9) -> bool:
    """Точка внутри выпуклого многоугольника (обход вершин по часовой или против)."""
    sign = 0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        cross = (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1)
        if abs(cross) <= eps:
            continue
        s = 1 if cross > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True
