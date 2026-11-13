"""Сборка одноштриховых осевых линий из эталонных TTF ГОСТ.

Осевые линии восстанавливаются аналитически из контуров глифа (glyf) —
без растеризации и без скелетизации. Штрих в этих шрифтах имеет постоянную
толщину d и является офсетом осевой линии на d/2, поэтому середина пары
противоположных точек контура всегда лежит на осевой линии. Такой подход
даёт точные вершины (митры), точные концы и не порождает «шипов» и
зигзагов в углах и пересечениях, которые появлялись при скелетизации
растрового отпечатка.

Толщина линии в результат не переносится: сохраняется только геометрия
осевой линии, поэтому штрих можно рисовать маркером любой толщины.

Запуск (нужен fonttools; для --check дополнительно pillow):
    python tools/build_centerlines.py
    python tools/build_centerlines.py --check
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from fontTools.pens.basePen import BasePen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "gost_marker"
DEFAULT_OUTPUT = ROOT / "gost_marker" / "fonts" / "centerlines.py"

# Параметры восстановления. Величины заданы в долях толщины штриха d или
# высоты прописных, поэтому не зависят от кегля и конкретного шрифта.
SAMPLE_STEP = 0.10     # шаг дискретизации контура, в долях d
PARTNER_TOL = 0.25     # допуск поиска партнёра на противоположной стороне
NORMAL_DEG = 25.0      # допустимое отклонение пары от нормали контура
WIDTH_TOL = 0.40       # извлечение обрывается за 0.10·d до края «туши»
DOT_BBOX = 1.70        # контур меньше этого размера считается точкой
# Вершина восстанавливается как пересечение продолжений соседних штрихов,
# и у пологого угла требуемое удлинение растёт как (d/2)/sin(θ/2). Поднимать
# предел нельзя: эта же величина служит радиусом поиска сшиваемых концов
# (см. _close_gaps), поэтому запас 4.0·d начинал сшивать концы через полглифа.
# Замерено: «Н» распадалась на 4 штриха вместо 3, «Х» — на 4 вместо 2,
# у «М» появлялся разрыв между диагоналями.
MITER_LIMIT = 2.0      # предельное удлинение штриха при восстановлении вершины
SNAP_LIMIT = 0.75      # привязка конца штриха к другому штриху
MERGE_TOL = 0.05       # концы ближе этого считаются общим узлом
JOIN_TURN_DEG = 12.0   # склейка почти коллинеарных штрихов в узле
COLLINEAR_DEG = 25.0   # порог «штрихи лежат на одной прямой» при сшивке
# Извлечение обрывается за 0.10·d до края «туши», поэтому свободный конец
# дополнительно подрезается на 0.40·d: в сумме ровно d/2, как у торца штриха.
TRIM_FREE_END = 0.40
RDP_EPS = 0.0025       # допуск упрощения в единицах высоты прописных
CORNER_DEG = 20.0      # угол, который упрощение не имеет права менять
CHECK_RENDER = 1400    # высота прописных в пикселях для --check
CHECK_IOU = 0.97       # порог IoU, ниже которого глиф считается дефектным

# Символы, которые в этих TTF отсутствуют или нарисованы неверно.
# Для них остаётся ручная реализация из gost.py.
SKIP_CHARS = set("×÷°–—")

CHARSET = (
    "".join(chr(c) for c in range(0x20, 0x7F))
    + "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    + "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    + "«»„“”‘’…№±⌀Ø"
)


# ---------------------------------------------------------------------------
# Контуры глифа
# ---------------------------------------------------------------------------


class _FlattenPen(BasePen):
    """Собирает контуры глифа в ломаные, разбивая кривые на отрезки."""

    def __init__(self, glyph_set, target: float = 8.0) -> None:
        super().__init__(glyph_set)
        self._target = max(1e-6, float(target))
        self.contours: list[list[tuple[float, float]]] = []
        self._current: list[tuple[float, float]] = []

    def _steps(self, span: float) -> int:
        return max(2, min(64, int(round(span / self._target))))

    def _moveTo(self, pt) -> None:
        self._flush()
        self._current = [(float(pt[0]), float(pt[1]))]

    def _lineTo(self, pt) -> None:
        if not self._current:
            self._current = [(float(pt[0]), float(pt[1]))]
            return
        self._current.append((float(pt[0]), float(pt[1])))

    def _qCurveToOne(self, p1, p2) -> None:
        if not self._current:
            return
        p0 = self._current[-1]
        n = self._steps(math.dist(p0, p1) + math.dist(p1, p2))
        for i in range(1, n + 1):
            t = i / n
            mt = 1.0 - t
            a, b, c = mt * mt, 2.0 * mt * t, t * t
            self._current.append(
                (a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1])
            )

    def _curveToOne(self, p1, p2, p3) -> None:
        if not self._current:
            return
        p0 = self._current[-1]
        n = self._steps(math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3))
        for i in range(1, n + 1):
            t = i / n
            mt = 1.0 - t
            a, b, c, d = mt**3, 3.0 * mt * mt * t, 3.0 * mt * t * t, t**3
            self._current.append(
                (
                    a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
                )
            )

    def _closePath(self) -> None:
        self._flush()

    def _endPath(self) -> None:
        self._flush()

    def _flush(self) -> None:
        points = self._current
        self._current = []
        if len(points) >= 3:
            self.contours.append(points)


def glyph_contours(glyph_set, name: str, target: float = 8.0):
    """Контуры глифа в единицах шрифта (компоненты разворачиваются)."""
    pen = _FlattenPen(glyph_set, target=target)
    recording = DecomposingRecordingPen(glyph_set)
    glyph_set[name].draw(recording)
    recording.replay(pen)
    return pen.contours


def _bbox(contours):
    xs = [p[0] for contour in contours for p in contour]
    ys = [p[1] for contour in contours for p in contour]
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


def _resample_closed(points, step: float):
    """Пересэмплирует замкнутый контур с равным шагом по длине."""
    pts: list[tuple[float, float]] = []
    for p in points:
        q = (float(p[0]), float(p[1]))
        if not pts or math.dist(pts[-1], q) > 1e-9:
            pts.append(q)
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) <= 1e-9:
        pts.pop()
    n = len(pts)
    if n < 3:
        return pts

    lengths = [math.dist(pts[i], pts[(i + 1) % n]) for i in range(n)]
    total = sum(lengths)
    if total <= 1e-9:
        return pts

    count = max(12, int(round(total / max(step, 1e-6))))
    out: list[tuple[float, float]] = []
    seg = 0
    walked = 0.0
    for k in range(count):
        want = k * total / count
        while seg < n and walked + lengths[seg] < want - 1e-12:
            walked += lengths[seg]
            seg += 1
        if seg >= n:
            out.append(pts[0])
            continue
        a = pts[seg]
        b = pts[(seg + 1) % n]
        t = 0.0 if lengths[seg] <= 1e-12 else (want - walked) / lengths[seg]
        t = min(1.0, max(0.0, t))
        out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


class _Grid:
    """Равномерная сетка для быстрого поиска ближайшей точки."""

    def __init__(self, points, cell: float) -> None:
        self.points = points
        self.cell = cell if cell > 1e-9 else 1.0
        self.buckets: dict[tuple[int, int], list[int]] = {}
        for index, (x, y) in enumerate(points):
            key = (int(math.floor(x / self.cell)), int(math.floor(y / self.cell)))
            self.buckets.setdefault(key, []).append(index)

    def nearest(self, x: float, y: float, radius: float) -> int:
        reach = max(1, int(math.ceil(radius / self.cell)))
        cx = int(math.floor(x / self.cell))
        cy = int(math.floor(y / self.cell))
        best = -1
        best_d2 = radius * radius
        for gx in range(cx - reach, cx + reach + 1):
            for gy in range(cy - reach, cy + reach + 1):
                for index in self.buckets.get((gx, gy), ()):
                    px, py = self.points[index]
                    d2 = (px - x) ** 2 + (py - y) ** 2
                    if d2 <= best_d2:
                        best_d2 = d2
                        best = index
        return best

    def query(self, x: float, y: float, radius: float) -> list[int]:
        """Все индексы точек, попавших в радиус."""
        reach = max(1, int(math.ceil(radius / self.cell)))
        cx = int(math.floor(x / self.cell))
        cy = int(math.floor(y / self.cell))
        limit2 = radius * radius
        found: list[int] = []
        for gx in range(cx - reach, cx + reach + 1):
            for gy in range(cy - reach, cy + reach + 1):
                for index in self.buckets.get((gx, gy), ()):
                    px, py = self.points[index]
                    if (px - x) ** 2 + (py - y) ** 2 <= limit2:
                        found.append(index)
        return found


# ---------------------------------------------------------------------------
# Геометрические помощники
# ---------------------------------------------------------------------------


def _length(path) -> float:
    return sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))


def _is_closed(path) -> bool:
    """Замкнутый контур (петля): начало и конец совпадают."""
    return len(path) >= 3 and math.dist(path[0], path[-1]) <= 1e-6


def _circle(center, radius: float, n: int = 12):
    cx, cy = center
    return [
        (
            cx + radius * math.cos(2.0 * math.pi * i / n),
            cy + radius * math.sin(2.0 * math.pi * i / n),
        )
        for i in range(n + 1)
    ]


def _closest_on_polyline(point, path):
    """Ближайшая точка на ломаной и расстояние до неё."""
    px, py = point
    best_q = (float(path[0][0]), float(path[0][1]))
    best_d = float("inf")
    for i in range(len(path) - 1):
        ax, ay = path[i]
        bx, by = path[i + 1]
        dx = bx - ax
        dy = by - ay
        seg2 = dx * dx + dy * dy
        if seg2 <= 1e-18:
            qx, qy = ax, ay
        else:
            t = ((px - ax) * dx + (py - ay) * dy) / seg2
            t = min(1.0, max(0.0, t))
            qx = ax + dx * t
            qy = ay + dy * t
        dist = math.hypot(px - qx, py - qy)
        if dist < best_d:
            best_d = dist
            best_q = (qx, qy)
    return best_q, best_d


def _dist_to_polyline(point, path) -> float:
    return _closest_on_polyline(point, path)[1]


def _outward(path, end: int, ref: float = 0.0) -> tuple[float, float]:
    """Единичный вектор из штриха наружу через указанный конец."""
    pts = list(path) if end == 1 else list(reversed(path))
    last = pts[-1]
    result = (0.0, 0.0)
    for p in reversed(pts[:-1]):
        dx = last[0] - p[0]
        dy = last[1] - p[1]
        norm = math.hypot(dx, dy)
        if norm <= 1e-9:
            continue
        result = (dx / norm, dy / norm)
        if norm >= ref:
            break
    return result


def _ray_intersection(p1, u1, p2, u2):
    den = u1[0] * u2[1] - u1[1] * u2[0]
    if abs(den) < 1e-9:
        return None
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    t = (dx * u2[1] - dy * u2[0]) / den
    s = (dx * u1[1] - dy * u1[0]) / den
    return t, s, (p1[0] + t * u1[0], p1[1] + t * u1[1])


def _trim_end(path, end: int, amount: float):
    """Укорачивает штрих с указанного конца на amount по длине."""
    # Обход всегда идёт от подрезаемого конца внутрь, поэтому для последнего
    # конца порядок точек обращается, а результат возвращается обратно.
    pts = list(reversed(path)) if end == 1 else list(path)
    if len(pts) < 2 or amount <= 0.0:
        return list(path)
    total = _length(pts)
    if total <= 1e-9:
        return list(path)
    amount = min(amount, 0.3 * total)

    acc = 0.0
    kept = None
    for i in range(len(pts) - 1):
        a = pts[i]
        b = pts[i + 1]
        seg = math.dist(a, b)
        if acc + seg < amount:
            acc += seg
            continue
        t = 0.0 if seg <= 1e-12 else (amount - acc) / seg
        t = min(1.0, max(0.0, t))
        start = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        kept = [start] + pts[i + 1 :]
        break
    if kept is None:
        kept = [pts[-1]]
    result = list(reversed(kept)) if end == 1 else kept
    return result if len(result) >= 2 else list(path)


def _rdp(points, eps: float):
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = points[i]
        bx, by = points[j]
        dx = bx - ax
        dy = by - ay
        norm = math.hypot(dx, dy)
        best = -1.0
        index = -1
        for k in range(i + 1, j):
            px, py = points[k]
            if norm < 1e-9:
                dist = math.hypot(px - ax, py - ay)
            else:
                dist = abs(dx * (py - ay) - dy * (px - ax)) / norm
            if dist > best:
                best = dist
                index = k
        if best > eps and index > 0:
            keep[index] = True
            stack.append((i, index))
            stack.append((index, j))
    return [p for p, flag in zip(points, keep) if flag]


def _simplify(points, eps: float, corner_deg: float):
    """Упрощение ломаной с сохранением острых углов."""
    if len(points) <= 2:
        return list(points)
    limit = math.cos(math.radians(corner_deg))
    fixed = {0, len(points) - 1}
    for i in range(1, len(points) - 1):
        ax, ay = points[i - 1]
        bx, by = points[i]
        cx, cy = points[i + 1]
        ux, uy = bx - ax, by - ay
        vx, vy = cx - bx, cy - by
        nu = math.hypot(ux, uy)
        nv = math.hypot(vx, vy)
        if nu < 1e-12 or nv < 1e-12:
            continue
        if (ux * vx + uy * vy) / (nu * nv) < limit:
            fixed.add(i)
    order = sorted(fixed)
    out: list[tuple[float, float]] = []
    for a, b in zip(order, order[1:]):
        seg = _rdp(points[a : b + 1], eps)
        out.extend(seg if not out else seg[1:])
    return out


# ---------------------------------------------------------------------------
# Восстановление топологии штрихов
# ---------------------------------------------------------------------------


def _close_gaps(paths, d: float, min_collinear: float):
    """Сшивает концы штрихов, продлевая их до пересечения направлений.

    Штрихи постоянной толщины обрываются там, где офсет перестаёт быть
    парным (углы и пересечения), поэтому вершины восстанавливаются как
    пересечение продолжений соседних участков.
    """
    merged = [list(p) for p in paths]
    limit = MITER_LIMIT * d
    ref = 0.3 * d
    # Каждая сшивка только удлиняет конец, поэтому число шагов ограничено.
    for _ in range(8 * len(merged) + 8):
        ends = []
        for i, path in enumerate(merged):
            if len(path) >= 2 and not _is_closed(path):
                ends.append((i, 0, path[0]))
                ends.append((i, 1, path[-1]))

        best = None
        for a in range(len(ends)):
            ia, ea, pa = ends[a]
            ua = _outward(merged[ia], ea, ref)
            if ua == (0.0, 0.0):
                continue
            for b in range(a + 1, len(ends)):
                ib, eb, pb = ends[b]
                if ia == ib:
                    continue
                if math.dist(pa, pb) > 2.0 * limit:
                    continue
                ub = _outward(merged[ib], eb, ref)
                if ub == (0.0, 0.0):
                    continue
                anti = -(ua[0] * ub[0] + ua[1] * ub[1])
                if anti < min_collinear:
                    continue
                offset = (pb[0] - pa[0], pb[1] - pa[1])
                straight = (
                    anti >= math.cos(math.radians(COLLINEAR_DEG))
                    and abs(ua[0] * offset[1] - ua[1] * offset[0]) <= MERGE_TOL * d
                )
                if straight:
                    # Концы лежат на одной прямой: направления параллельны и
                    # точки пересечения не существует. Вершина в этом случае
                    # находится между концами, поэтому зазор закрывается
                    # посередине. Без этой ветки штрих, разорванный строго
                    # вертикально или горизонтально, не сшивался никогда:
                    # _ray_intersection возвращал None при нулевом определителе.
                    half = 0.5 * math.dist(pa, pb)
                    t, s = half, half
                    point = (pa[0] + 0.5 * offset[0], pa[1] + 0.5 * offset[1])
                else:
                    hit = _ray_intersection(pa, ua, pb, ub)
                    if hit is None:
                        continue
                    t, s, point = hit
                if not (1e-9 < t <= limit and 1e-9 <= s <= limit):
                    continue
                cost = t + s + (1.0 - anti) * d
                if best is None or cost < best[0]:
                    best = (cost, ia, ea, ib, eb, point)

        if best is None:
            break
        _cost, ia, ea, ib, eb, point = best
        for index, end in ((ia, ea), (ib, eb)):
            if end == 0:
                merged[index][0] = point
            else:
                merged[index][-1] = point
    return merged


def _snap_ends(paths, d: float):
    """Привязывает концы штрихов к другим штрихам (Т-образные стыки)."""
    limit = SNAP_LIMIT * d
    result = [list(p) for p in paths]
    for i, path in enumerate(result):
        if len(path) < 2 or _is_closed(path):
            continue
        for end in (0, 1):
            point = path[-1] if end else path[0]
            best = None
            for j, other in enumerate(result):
                if j == i or len(other) < 2:
                    continue
                q, dist = _closest_on_polyline(point, other)
                if 1e-9 < dist <= limit and (best is None or dist < best[0]):
                    best = (dist, q)
            if best is not None:
                if end == 0:
                    path[0] = best[1]
                else:
                    path[-1] = best[1]
    return result


def _merge_coincident(paths, d: float):
    """Склеивает штрихи, у которых концы сходятся в общий узел."""
    merged = [list(p) for p in paths]
    tol = MERGE_TOL * d
    while True:
        ends = []
        for i, path in enumerate(merged):
            if len(path) >= 2 and not _is_closed(path):
                ends.append((i, 0, path[0]))
                ends.append((i, 1, path[-1]))

        best = None
        for a in range(len(ends)):
            ia, ea, pa = ends[a]
            for b in range(a + 1, len(ends)):
                ib, eb, pb = ends[b]
                if ia == ib:
                    continue
                dist = math.dist(pa, pb)
                if dist > tol:
                    continue
                if best is None or dist < best[0]:
                    point = ((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0)
                    best = (dist, ia, ea, ib, eb, point)

        if best is None:
            break
        _dist, ia, ea, ib, eb, point = best
        first = list(merged[ia] if ea == 1 else merged[ia][::-1])
        second = list(merged[ib] if eb == 0 else merged[ib][::-1])
        first[-1] = point
        second[0] = point
        merged[ia] = first + second[1:]
        merged.pop(ib)
    return [p for p in merged if len(p) >= 2]


def _join_collinear(paths, d: float):
    """Склеивает почти коллинеарные штрихи, сходящиеся в одном узле."""
    merged = [list(p) for p in paths]
    limit = 0.3 * d
    limit_cos = math.cos(math.radians(JOIN_TURN_DEG))
    ref = 0.3 * d
    while True:
        ends = []
        for i, path in enumerate(merged):
            if len(path) >= 2 and not _is_closed(path):
                ends.append((i, 0, path[0]))
                ends.append((i, 1, path[-1]))

        best = None
        for a in range(len(ends)):
            ia, ea, pa = ends[a]
            ua = _outward(merged[ia], ea, ref)
            if ua == (0.0, 0.0):
                continue
            for b in range(a + 1, len(ends)):
                ib, eb, pb = ends[b]
                if ia == ib:
                    continue
                if math.dist(pa, pb) > limit:
                    continue
                ub = _outward(merged[ib], eb, ref)
                if ub == (0.0, 0.0):
                    continue
                dot = -(ua[0] * ub[0] + ua[1] * ub[1])
                if dot < limit_cos:
                    continue
                if best is None or dot > best[0]:
                    best = (dot, ia, ea, ib, eb)

        if best is None:
            break
        _dot, ia, ea, ib, eb = best
        first = list(merged[ia] if ea == 1 else merged[ia][::-1])
        second = list(merged[ib] if eb == 0 else merged[ib][::-1])
        merged[ia] = first + second[1:]
        merged.pop(ib)
    return [p for p in merged if len(p) >= 2]


def _trim_free_ends(paths, d: float):
    """Подрезает свободные концы на d/2.

    Свободный конец штриха постоянной толщины — это торец, поэтому осевая
    линия заканчивается на d/2 раньше края «туши».
    """
    amount = TRIM_FREE_END * d
    result = []
    for i, path in enumerate(paths):
        path = list(path)
        others = [p for j, p in enumerate(paths) if j != i and len(p) >= 2]
        if not _is_closed(path):
            if not any(_dist_to_polyline(path[-1], other) <= 0.25 * d for other in others):
                path = _trim_end(path, 1, amount)
            if len(path) >= 2 and not any(
                _dist_to_polyline(path[0], other) <= 0.25 * d for other in others
            ):
                path = _trim_end(path, 0, amount)
        if len(path) >= 2:
            result.append(path)
    return result


# ---------------------------------------------------------------------------
# Извлечение осевых линий из контуров
# ---------------------------------------------------------------------------


def _detect_dots(contours, d: float):
    """Находит точечные знаки (точки, диэрезис) и их радиусы.

    Точка в штриховом шрифте — это отдельный компактный контур, а не штрих
    постоянной толщины, поэтому он распознаётся по габаритам.
    """
    dots = []
    rest = []
    for contour in contours:
        x0, y0, x1, y1 = _bbox([contour])
        width = x1 - x0
        height = y1 - y0
        big = max(width, height)
        small = max(min(width, height), 1e-9)
        if big <= DOT_BBOX * d and big / small <= 1.5:
            dots.append(_circle(((x0 + x1) / 2.0, (y0 + y1) / 2.0), big / 2.0))
        else:
            rest.append(contour)
    return dots, rest


def _extract_midpoints(contours, d: float):
    """Середины пар противоположных точек контура (лежат на осевой линии)."""
    step = max(1e-6, SAMPLE_STEP * d)
    sampled = []
    for contour in contours:
        points = _resample_closed(contour, step)
        if len(points) >= 8:
            sampled.append(points)
    if not sampled:
        return [], []

    flat: list[tuple[float, float]] = []
    owner: list[tuple[int, int]] = []
    normals: list[tuple[float, float]] = []
    for ci, points in enumerate(sampled):
        n = len(points)
        for i, point in enumerate(points):
            ax, ay = points[(i - 1) % n]
            bx, by = points[(i + 1) % n]
            tx, ty = bx - ax, by - ay
            norm = math.hypot(tx, ty)
            normals.append((-ty / norm, tx / norm) if norm > 1e-12 else (0.0, 0.0))
            flat.append(point)
            owner.append((ci, i))

    grid = _Grid(flat, cell=0.5 * d)
    tol = PARTNER_TOL * d
    inner = 0.5 * d - WIDTH_TOL * d
    aligned = math.cos(math.radians(NORMAL_DEG))

    midpoints = []
    for index, (px, py) in enumerate(flat):
        nx, ny = normals[index]
        if nx == 0.0 and ny == 0.0:
            continue
        ci, i = owner[index]

        best = -1
        best_err = tol
        for sign in (1.0, -1.0):
            idx = grid.nearest(px + sign * d * nx, py + sign * d * ny, tol)
            if idx < 0:
                continue
            oc, oi = owner[idx]
            if oc == ci and abs(oi - i) <= 3:
                continue
            qx, qy = flat[idx]
            span = math.dist((px, py), (qx, qy))
            if span <= 1e-9:
                continue
            # Пара противоположна только тогда, когда отрезок между точками
            # идёт вдоль нормали контура в обеих точках. Иначе у углов и в
            # местах пересечения в пару попадает точка соседнего участка
            # контура: её середина ложная, и из таких середин собираются
            # кольца и выбросы в стыках.
            ux, uy = (qx - px) / span, (qy - py) / span
            if abs(ux * nx + uy * ny) < aligned:
                continue
            ox, oy = normals[idx]
            if abs(ux * ox + uy * oy) < aligned:
                continue
            err = abs(span - d)
            if err < best_err:
                best_err = err
                best = idx
        if best < 0:
            continue

        qx, qy = flat[best]
        mx, my = (px + qx) / 2.0, (py + qy) / 2.0

        # Вдоль осевой линии толщина постоянна, поэтому рядом не должно
        # быть других участков контура ближе половины толщины. Так
        # отсекаются ложные середины у торцов, углов и пересечений.
        if grid.nearest(mx, my, inner) >= 0:
            continue

        midpoints.append(((ci, i), (mx, my)))
    return midpoints, [len(points) for points in sampled]


def _build_graph(midpoints, sampled_count, d: float):
    """Кластеризует середины в узлы и связывает соседние вдоль контура."""
    step = max(1e-6, SAMPLE_STEP * d)
    merge = 0.75 * step
    nodes: list[list[float]] = []
    node_of_key: dict[tuple[int, int], int] = {}
    grid = _Grid([], cell=max(merge, 1e-9))

    for key, mid in midpoints:
        idx = grid.nearest(mid[0], mid[1], merge)
        if idx < 0:
            idx = len(nodes)
            nodes.append([mid[0], mid[1]])
            grid.points.append(mid)
            gkey = (
                int(math.floor(mid[0] / grid.cell)),
                int(math.floor(mid[1] / grid.cell)),
            )
            grid.buckets.setdefault(gkey, []).append(idx)
        node_of_key[key] = idx

    adj: list[set[int]] = [set() for _ in nodes]
    for (ci, i), node in node_of_key.items():
        n = sampled_count[ci]
        nxt = node_of_key.get((ci, (i + 1) % n))
        if nxt is not None and nxt != node:
            adj[node].add(nxt)
            adj[nxt].add(node)
    return nodes, adj


def _trace(nodes, adj):
    """Обходит граф узлов, собирая цепочки (штрихи) и замкнутые контуры.

    В узле степени больше двух штрихи продолжаются насквозь: рёбра
    разбиваются на пары по наименьшему углу поворота. Иначе обход
    поворачивал в пересечении произвольно — «Х» распадалась на два «V»,
    а стойки «Н» приклеивались к перекладине.
    """
    edges: list[tuple[int, int]] = []
    for a in range(len(adj)):
        for b in adj[a]:
            if a < b:
                edges.append((a, b))
    if not edges:
        return []

    incident: dict[int, list[int]] = {}
    for index, (a, b) in enumerate(edges):
        incident.setdefault(a, []).append(index)
        incident.setdefault(b, []).append(index)

    def direction(node: int, edge: int) -> tuple[float, float] | None:
        other = edges[edge][1] if edges[edge][0] == node else edges[edge][0]
        dx = nodes[other][0] - nodes[node][0]
        dy = nodes[other][1] - nodes[node][1]
        norm = math.hypot(dx, dy)
        return None if norm <= 1e-12 else (dx / norm, dy / norm)

    # Сквозной штрих в узле — это пара рёбер с наиболее тупым углом между
    # направлениями наружу: они смотрят в противоположные стороны.
    partner: dict[tuple[int, int], int | None] = {}
    for node, items in incident.items():
        pool = list(items)
        while len(pool) >= 2:
            best = None
            for i in range(len(pool)):
                di = direction(node, pool[i])
                if di is None:
                    continue
                for j in range(i + 1, len(pool)):
                    dj = direction(node, pool[j])
                    if dj is None:
                        continue
                    dot = di[0] * dj[0] + di[1] * dj[1]
                    if best is None or dot < best[0]:
                        best = (dot, i, j)
            if best is None:
                break
            _dot, i, j = best
            partner[(node, pool[i])] = pool[j]
            partner[(node, pool[j])] = pool[i]
            pool = [edge for k, edge in enumerate(pool) if k != i and k != j]
        for edge in pool:
            partner[(node, edge)] = None

    used: set[int] = set()
    paths: list[list[int]] = []

    def walk(node: int, edge: int) -> list[int]:
        chain = [node]
        while True:
            used.add(edge)
            a, b = edges[edge]
            node = b if a == node else a
            chain.append(node)
            nxt = partner.get((node, edge))
            if nxt is None or nxt in used:
                break
            edge = nxt
        return chain

    # Сначала цепочки, начинающиеся в узле, где штрих обрывается, затем
    # оставшиеся кольца.
    for node, items in incident.items():
        for edge in items:
            if edge in used or partner.get((node, edge)) is not None:
                continue
            paths.append(walk(node, edge))
    for index in range(len(edges)):
        if index not in used:
            paths.append(walk(edges[index][0], index))

    return [[nodes[i] for i in chain] for chain in paths if len(chain) >= 2]


def extract_strokes(contours, d: float, cap_units: float):
    """Осевые линии глифа в единицах высоты прописных."""
    if not contours or d <= 0.0 or cap_units <= 0.0:
        return []

    dots, rest = _detect_dots(contours, d)

    strokes: list[list[tuple[float, float]]] = []
    if rest:
        midpoints, counts = _extract_midpoints(rest, d)
        if midpoints:
            nodes, adj = _build_graph(midpoints, counts, d)
            eps = RDP_EPS * cap_units
            for chain in _trace(nodes, adj):
                path = _simplify(chain, eps, CORNER_DEG)
                if len(path) >= 2 and _length(path) > 0.2 * d:
                    strokes.append(path)
            # Узел без связей — точечный знак, не распознанный по габаритам.
            for index, node in enumerate(nodes):
                if adj[index]:
                    continue
                radius = min(_dist_to_polyline(node, contour) for contour in rest)
                if 0.2 * d <= radius <= 1.2 * d:
                    dots.append(_circle(node, radius))

    if strokes:
        collinear = math.cos(math.radians(COLLINEAR_DEG))
        strokes = _close_gaps(strokes, d, collinear)
        strokes = _merge_coincident(strokes, d)
        strokes = _close_gaps(strokes, d, -1.0)
        strokes = _merge_coincident(strokes, d)
        strokes = _snap_ends(strokes, d)
        strokes = _join_collinear(strokes, d)
        strokes = _trim_free_ends(strokes, d)

    result: list[list[tuple[float, float]]] = []
    for path in dots + strokes:
        points: list[tuple[float, float]] = []
        for x, y in path:
            point = (_round(x / cap_units), _round(y / cap_units))
            if not points or point != points[-1]:
                points.append(point)
        if len(points) >= 2:
            result.append(points)
    return result


def _round(value: float) -> float:
    rounded = round(value, 4)
    return 0.0 if rounded == 0.0 else rounded


# ---------------------------------------------------------------------------
# Метрики шрифта
# ---------------------------------------------------------------------------


def _opposite_distance(contours, cap_units: float) -> float | None:
    """Наиболее частое расстояние между противоположными участками контура.

    У штриха постоянной толщины это расстояние равно d, поэтому мода
    распределения даёт толщину штриха и не зависит от того, как именно
    нарисован конкретный глиф. Замер «по левой стойке» для этого не годится:
    у разных гарнитур левый край образован разными элементами.
    """
    step = 0.04 * cap_units
    sampled = []
    for contour in contours:
        points = _resample_closed(contour, step)
        if len(points) >= 8:
            sampled.append(points)
    if not sampled:
        return None

    flat: list[tuple[float, float]] = []
    owner: list[tuple[int, int]] = []
    for ci, points in enumerate(sampled):
        for i, point in enumerate(points):
            flat.append(point)
            owner.append((ci, i))

    grid = _Grid(flat, cell=0.25 * cap_units)
    radius = 0.35 * cap_units
    values: list[float] = []
    for ci, points in enumerate(sampled):
        n = len(points)
        guard = max(4, int(n * 0.05))
        for i, point in enumerate(points):
            best = None
            for idx in grid.query(point[0], point[1], radius):
                oc, oi = owner[idx]
                if oc == ci:
                    gap = abs(oi - i)
                    if min(gap, n - gap) < guard:
                        continue
                dist = math.dist(point, flat[idx])
                if dist > 1e-9 and (best is None or dist < best):
                    best = dist
            if best is not None:
                values.append(best)
    if not values:
        return None

    width = 0.002 * cap_units
    bins: dict[int, list[float]] = {}
    for value in values:
        bins.setdefault(int(value / width), []).append(value)
    peak = max(bins, key=lambda key: len(bins[key]))
    cluster = sorted(bins[peak])
    return cluster[len(cluster) // 2]


def detect_cap_units(glyph_set, cmap) -> float:
    """Высота прописных в единицах шрифта."""
    heights = []
    for char in ("Н", "H", "Б", "Е"):
        name = cmap.get(ord(char))
        if not name:
            continue
        contours = glyph_contours(glyph_set, name, target=40.0)
        if not contours:
            continue
        _x0, _y0, _x1, y1 = _bbox(contours)
        if y1 > 0:
            heights.append(y1)
    if heights:
        heights.sort()
        return heights[len(heights) // 2]
    return 1.0


def detect_stroke_units(glyph_set, cmap, cap_units: float) -> float:
    """Толщина штриха d в единицах шрифта."""
    samples = []
    for char in ("Н", "H", "П", "Ш", "И", "N", "Б", "В", "О", "о"):
        name = cmap.get(ord(char))
        if not name:
            continue
        contours = glyph_contours(glyph_set, name, target=0.05 * cap_units)
        if not contours:
            continue
        value = _opposite_distance(contours, cap_units)
        if value is not None and 0.02 * cap_units < value < 0.2 * cap_units:
            samples.append(value)
    if not samples:
        return cap_units / 14.0
    samples.sort()
    return samples[len(samples) // 2]


def _glyph_x_height(glyph_set, cmap, cap_units: float) -> float:
    name = cmap.get(ord("x"))
    if name:
        contours = glyph_contours(glyph_set, name, target=40.0)
        if contours:
            _x0, _y0, _x1, y1 = _bbox(contours)
            if y1 > 0:
                return y1 / cap_units
    return 0.70


# ---------------------------------------------------------------------------
# Сборка данных шрифта
# ---------------------------------------------------------------------------


def render_font(ttf_path: Path, key: str, check: bool = False) -> dict:
    ttf = TTFont(str(ttf_path))
    cmap = ttf.getBestCmap()
    glyph_set = ttf.getGlyphSet()
    hmtx = ttf["hmtx"]

    cap_units = detect_cap_units(glyph_set, cmap)
    stroke_units = detect_stroke_units(glyph_set, cmap, cap_units)
    target = 0.15 * stroke_units

    glyphs: dict[str, tuple[float, list[list[tuple[float, float]]]]] = {}
    for char in CHARSET:
        cp = ord(char)
        if char.isspace() or char in SKIP_CHARS or cp not in cmap:
            continue
        name = cmap[cp]
        if name == ".notdef":
            continue
        contours = glyph_contours(glyph_set, name, target=target)
        strokes = extract_strokes(contours, stroke_units, cap_units)
        if not strokes:
            print(f"  предупреждение: {char!r} — осевые линии не найдены", file=sys.stderr)
            continue
        glyphs[char] = (round(hmtx[name][0] / cap_units, 4), strokes)

    space_name = cmap.get(0x20)
    word_spacing = hmtx[space_name][0] / cap_units if space_name else 0.43

    data = {
        "ascent": round(ttf["hhea"].ascent / cap_units, 4),
        "descent": round(ttf["hhea"].descent / cap_units, 4),
        "x_height": round(_glyph_x_height(glyph_set, cmap, cap_units), 4),
        "word_spacing": round(word_spacing, 4),
        "line_spacing": 1.57 if key == "A" else 1.70,
        "letter_spacing": 0.0,
        "line_width": round(stroke_units / cap_units, 6),
        "glyphs": glyphs,
    }

    print(
        f"{key}: {ttf_path.name} — высота прописных {cap_units:.1f} ед., "
        f"толщина штриха {stroke_units:.2f} ед. "
        f"({stroke_units / cap_units:.4f} от высоты прописных)"
    )
    if check:
        _report(ttf_path, data, cap_units)
    return data


def _to_px(x: float, y: float, left: float, top: float, render: float, margin: int):
    """Перевод координат глифа (высота прописных = 1) в пиксели канвы."""
    return (margin + (x - left) * render, margin + (top - y) * render)


def _report(ttf_path: Path, data: dict, cap_units: float) -> None:
    """Сравнивает восстановленные осевые линии с исходным глифом по IoU.

    Эталон строится заливкой самих контуров по правилу чётности, без
    растеризации шрифта, поэтому в сравнение не попадают хинтинг и
    округление кегля. Штрихи рисуются с круглым торцом — именно так маркер
    продолжает линию за конец осевой линии, — поэтому корректная осевая
    линия воспроизводит отпечаток глифа точно.
    """
    try:
        from PIL import Image, ImageChops, ImageDraw
    except ImportError:
        print("--check требует pillow", file=sys.stderr)
        return

    ttf = TTFont(str(ttf_path))
    cmap = ttf.getBestCmap()
    glyph_set = ttf.getGlyphSet()

    render = float(CHECK_RENDER)
    width_px = max(1, int(round(data["line_width"] * render)))
    margin = int(round(0.12 * render)) + width_px
    radius = width_px / 2.0

    results = []
    for char, (_advance, strokes) in data["glyphs"].items():
        name = cmap.get(ord(char))
        if not name:
            continue
        source = glyph_contours(glyph_set, name, target=0.05 * cap_units)
        if not source:
            continue

        # Канва и преобразование координат строятся по фактическому отпечатку
        # глифа: hhea.ascent/descent в этих шрифтах недостоверны, и при опоре
        # на них эталон мог оказаться целиком за пределами изображения.
        x0, y0, x1, y1 = _bbox(source)
        mine_x = [x for stroke in strokes for x, _y in stroke]
        mine_y = [y for stroke in strokes for _x, y in stroke]
        left = min(x0 / cap_units, min(mine_x))
        right = max(x1 / cap_units, max(mine_x))
        bottom = min(y0 / cap_units, min(mine_y))
        top = max(y1 / cap_units, max(mine_y))
        if right - left <= 0.0 or top - bottom <= 0.0:
            continue

        size = (
            int(round((right - left) * render)) + 2 * margin,
            int(round((top - bottom) * render)) + 2 * margin,
        )

        reference = Image.new("1", size, 0)
        for contour in source:
            layer = Image.new("1", size, 0)
            ImageDraw.Draw(layer).polygon(
                [
                    _to_px(x / cap_units, y / cap_units, left, top, render, margin)
                    for x, y in contour
                ],
                fill=1,
            )
            reference = ImageChops.logical_xor(reference, layer)
        reference = reference.convert("L")

        mine = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mine)
        for stroke in strokes:
            pts = [_to_px(x, y, left, top, render, margin) for x, y in stroke]
            if len(pts) >= 2:
                draw.line(pts, fill=255, width=width_px, joint="curve")
            for px, py in (pts[0], pts[-1]):
                draw.ellipse(
                    [px - radius, py - radius, px + radius, py + radius], fill=255
                )

        inter = sum(ImageChops.darker(reference, mine).histogram()[1:])
        union = sum(ImageChops.lighter(reference, mine).histogram()[1:])
        results.append((inter / union if union else 1.0, char))

    if not results:
        return
    results.sort()
    mean = sum(iou for iou, _ in results) / len(results)
    bad = [(char, iou) for iou, char in results if iou < CHECK_IOU]
    print(f"  IoU: среднее {mean:.4f}, минимум {results[0][0]:.4f} ({results[0][1]!r})")
    if bad:
        listing = ", ".join(f"{char!r} {iou:.3f}" for char, iou in bad)
        print(f"  ниже порога {CHECK_IOU}: {listing}", file=sys.stderr)


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
        for char, (advance, strokes) in item["glyphs"].items():
            stroke_text = ", ".join(
                "[" + ", ".join(f"({x}, {y})" for x, y in stroke) + "]"
                for stroke in strokes
            )
            out.append(f"            {char!r}: ({advance}, [{stroke_text}]),")
        out.append("        },")
        out.append("    },")
    out.append("}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка осевых линий из TTF ГОСТ")
    parser.add_argument("--font-a", default=str(FONT_DIR / "GOST_A.TTF"))
    parser.add_argument("--font-b", default=str(FONT_DIR / "GOST_B.ttf"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--check",
        action="store_true",
        help="сравнить результат с исходными глифами по IoU (нужен pillow)",
    )
    args = parser.parse_args()

    data = {
        "A": render_font(Path(args.font_a), "A", check=args.check),
        "B": render_font(Path(args.font_b), "B", check=args.check),
    }
    output = Path(args.output)
    output.write_text(format_data(data), encoding="utf-8")
    for key in ("A", "B"):
        glyphs = data[key]["glyphs"]
        points = sum(len(stroke) for _, strokes in glyphs.values() for stroke in strokes)
        print(f"{key}: {len(glyphs)} глифов, {points} точек -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
