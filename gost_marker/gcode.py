"""Генерация G-кода для GRBL 1.1h и оценка времени выполнения."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .config import MachineConfig
from .fonts.primitives import Point, Stroke

EPS = 1e-7


def fmt(value: float, decimals: int = 3) -> str:
    """Компактное число без хвостовых нулей и без -0."""
    if abs(value) < 10 ** (-decimals) / 2:
        value = 0.0
    s = f"{value:.{decimals}f}"
    if decimals > 0:
        s = s.rstrip("0").rstrip(".")
    return s if s not in {"", "-0", "-"} else "0"


def distance(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def polyline_length(path: Sequence[Point]) -> float:
    return sum(distance(path[i], path[i + 1]) for i in range(len(path) - 1))


def _dedupe(path: Sequence[Point], eps: float = 0.002) -> Stroke:
    out: Stroke = []
    for p in path:
        if not out or distance(out[-1], p) > eps:
            out.append((float(p[0]), float(p[1])))
    return out


def simplify_collinear(path: Sequence[Point], tol: float = 0.002) -> Stroke:
    """Убирает почти совпадающие и лежащие на одной прямой точки."""
    pts = _dedupe(path, tol)
    if len(pts) <= 2:
        return pts
    out: Stroke = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[i], pts[i + 1]
        ab = distance(a, b)
        bc = distance(b, c)
        ac = distance(a, c)
        if ab < tol or bc < tol:
            continue
        # площадь треугольника; если высота меньше tol — точка не нужна
        area2 = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        if area2 / max(ac, EPS) <= tol:
            continue
        out.append(b)
    out.append(pts[-1])
    return out


def merge_touching(paths: Sequence[Sequence[Point]], eps: float = 0.01) -> list[Stroke]:
    """Склеивает штрихи, у которых конец одного совпадает с началом другого."""
    merged: list[Stroke] = []
    for path in paths:
        p = [(float(x), float(y)) for x, y in path]
        if len(p) < 2:
            continue
        if merged and distance(merged[-1][-1], p[0]) <= eps:
            # не дублируем точку стыка
            merged[-1].extend(p[1:])
        else:
            merged.append(list(p))
    return merged


def optimize_paths(paths: Sequence[Sequence[Point]], start: Point = (0.0, 0.0)) -> list[Stroke]:
    """Жадный обход ближайшего штриха; направление штриха можно менять.

    Это заметно уменьшает холостые перемещения и «капли» маркера.
    """
    remaining: list[Stroke] = [[(float(x), float(y)) for x, y in p] for p in paths if len(p) >= 2]
    if not remaining:
        return []
    # Для очень больших заданий O(n²) может быть заметным; ограничимся
    # разумным пределом, иначе сохраняем порядок чтения.
    if len(remaining) > 4000:
        return remaining

    ordered: list[Stroke] = []
    cur = start
    while remaining:
        best_i = 0
        best_rev = False
        best_d = float("inf")
        for i, p in enumerate(remaining):
            d0 = (p[0][0] - cur[0]) ** 2 + (p[0][1] - cur[1]) ** 2
            d1 = (p[-1][0] - cur[0]) ** 2 + (p[-1][1] - cur[1]) ** 2
            if d0 <= d1 and d0 < best_d:
                best_i, best_rev, best_d = i, False, d0
            elif d1 < best_d:
                best_i, best_rev, best_d = i, True, d1
        p = remaining.pop(best_i)
        if best_rev:
            p.reverse()
        ordered.append(p)
        cur = p[-1]
    return ordered


@dataclass
class JobStats:
    draw_length_mm: float = 0.0
    travel_length_mm: float = 0.0
    stroke_count: int = 0
    point_count: int = 0
    estimated_seconds: float = 0.0

    @property
    def total_length_mm(self) -> float:
        return self.draw_length_mm + self.travel_length_mm


@dataclass
class GcodeResult:
    text: str
    paths: list[Stroke] = field(default_factory=list)
    travels: list[tuple[Point, Point]] = field(default_factory=list)
    stats: JobStats = field(default_factory=JobStats)


def _estimate_seconds(
    draw_len: float,
    travel_len: float,
    stroke_count: int,
    machine: MachineConfig,
) -> float:
    draw_time = draw_len / max(machine.draw_feed, 1.0) * 60.0
    travel_time = travel_len / max(machine.travel_feed, 1.0) * 60.0
    dz = abs(machine.pen_up_z - machine.pen_down_z)
    z_time = dz / max(machine.z_feed, 1.0) * 60.0
    per_stroke = z_time + max(0.0, machine.dwell_down_s) + max(0.0, machine.dwell_up_s)
    return (draw_time + travel_time + stroke_count * per_stroke) * 1.05 + 2.0


def generate_gcode(
    paths: Sequence[Sequence[Point]],
    machine: MachineConfig,
    *,
    start_xy: Point = (0.0, 0.0),
    font_name: str = "",
    size_mm: float = 0.0,
    optimize: bool | None = None,
    merge: bool | None = None,
    include_header: bool = True,
) -> GcodeResult:
    """Создаёт текст программы для GRBL 1.1h.

    Маркер поднимается/опускается осью Z. Все перемещения — в мм,
    абсолютные координаты, подача в мм/мин.
    """
    if optimize is None:
        optimize = machine.optimize_order
    if merge is None:
        merge = machine.merge_touching

    raw = [simplify_collinear(p) for p in paths if len(p) >= 2]
    raw = [p for p in raw if len(p) >= 2]
    ordered = optimize_paths(raw, start_xy) if optimize else raw
    if merge:
        ordered = merge_touching(ordered, eps=0.01)

    draw_len = sum(polyline_length(p) for p in ordered)
    travels: list[tuple[Point, Point]] = []
    travel_len = 0.0
    cur = start_xy
    for p in ordered:
        if distance(cur, p[0]) > 1e-6:
            travels.append((cur, p[0]))
            travel_len += distance(cur, p[0])
        cur = p[-1]

    stats = JobStats(
        draw_length_mm=draw_len,
        travel_length_mm=travel_len,
        stroke_count=len(ordered),
        point_count=sum(len(p) for p in ordered),
        estimated_seconds=_estimate_seconds(draw_len, travel_len, len(ordered), machine),
    )

    lines: list[str] = []
    if include_header:
        lines.extend(
            [
                "; Generated by GOST Marker",
                "; GRBL 1.1h / mm / absolute / feed mm-min",
                f"; font: {font_name or 'GOST'}; size: {fmt(size_mm)} mm",
                f"; strokes: {stats.stroke_count}; draw: {fmt(stats.draw_length_mm)} mm; "
                f"travel: {fmt(stats.travel_length_mm)} mm",
                "; ----------------------------------------",
            ]
        )
    lines.extend(["G21", "G90", "G94", "G17", f"G0 Z{fmt(machine.pen_up_z)}"])

    pen_down = False
    draw_feed_set = False
    cur = start_xy

    for p in ordered:
        first = p[0]
        if distance(cur, first) > 1e-6:
            if machine.use_g0_travel:
                lines.append(f"G0 X{fmt(first[0])} Y{fmt(first[1])}")
            else:
                lines.append(f"G1 X{fmt(first[0])} Y{fmt(first[1])} F{fmt(machine.travel_feed, 1)}")
            cur = first

        if not pen_down:
            if abs(machine.pen_down_z - machine.pen_up_z) > 1e-9:
                lines.append(f"G1 Z{fmt(machine.pen_down_z)} F{fmt(machine.z_feed, 1)}")
            if machine.dwell_down_s > 0:
                lines.append(f"G4 P{fmt(machine.dwell_down_s)}")
            pen_down = True

        if not draw_feed_set:
            lines.append(f"G1 F{fmt(machine.draw_feed, 1)}")
            draw_feed_set = True

        # Первая точка уже достигнута холостым ходом.
        for x, y in p[1:]:
            lines.append(f"G1 X{fmt(x)} Y{fmt(y)}")
            cur = (x, y)

        if machine.pen_up_z != machine.pen_down_z:
            lines.append(f"G0 Z{fmt(machine.pen_up_z)}")
        if machine.dwell_up_s > 0:
            lines.append(f"G4 P{fmt(machine.dwell_up_s)}")
        pen_down = False

    lines.append(f"G0 Z{fmt(machine.pen_up_z)}")
    if machine.park_at_end:
        lines.append(f"G0 X{fmt(machine.park_x)} Y{fmt(machine.park_y)}")
    lines.append("M2")
    lines.append("")

    return GcodeResult(
        text="\n".join(lines),
        paths=ordered,
        travels=travels,
        stats=stats,
    )
