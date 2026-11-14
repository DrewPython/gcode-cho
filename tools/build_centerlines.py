"""Сборка одноштриховых центральных линий из эталонных TTF ГОСТ.

Запуск (нужны fonttools, pillow, scikit-image, scipy, numpy):
    python tools/build_centerlines.py

Скрипт читает gost_marker/GOST_A.TTF и gost_marker/GOST_B.ttf, извлекает
осевые линии глифов и записывает gost_marker/fonts/centerlines.py.

TTF ГОСТ — это не обычный контурный шрифт, а одноштриховой шрифт, контуры
которого «раздуты» до постоянной толщины (тип А — d = h/14, тип Б — d = h/10).
Поэтому осевая линия — это медиальная ось залитой фигуры. Чтобы не ловить
пиксельную лестницу и не рвать стыки, глиф рендерится в увеличенном размере,
затем у скелета схлопываются маленькие петли, которые всегда возникают в
местах пересечения нескольких штрихов.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi
from scipy.spatial import cKDTree
from skimage.measure import label, regionprops
from skimage.morphology import disk, medial_axis

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "gost_marker"
DEFAULT_OUTPUT = ROOT / "gost_marker" / "fonts" / "centerlines.py"

# Разрешение растеризации: пикселей на единицу шрифта. 1.0 — как в старом
# варианте; 2.0 уже даёт гладкие кривые и устойчивые стыки.
DEFAULT_RENDER_SCALE = 2.0

# Отступ вокруг глифа в юнитах шрифта. Должен быть больше половины толщины
# штриха, иначе скелет «прилипает» к краю картинки.
PAD_UNITS = 150.0

# Минимальная длина короткого ответвления скелета (в юнитах шрифта).
SPUR_UNITS = 3.0
# Допуск упрощения ломаной (в юнитах шрифта).
RDP_EPS_UNITS = 1.2
# Сколько раз сглаживать пиксельную лестницу перед упрощением.
SMOOTH_PASSES = 2
# Максимальный излом, при котором два штриха считаются продолжением друг друга.
JOIN_TURN_DEG = 40.0
# Радиус, в котором концы штрихов считаются одним узлом.
JUNCTION_SNAP_UNITS = 2.0
# Радиус для оценки направления штриха у стыка (доля половины толщины).
JUNCTION_LOOK_R = 0.5

# Короткие ответвления и перемычки в стыках. Длина задана в толщинах штриха:
# ус — ответвление с одним свободным концом, перемычка — короткий штрих между
# двумя узлами (именно она раньше превращала «ж» в две буквы с палкой посередине).
TERMINAL_SPUR_STROKE = 0.7
BRIDGE_MAX_STROKE = 2.2
# Максимальный разрыв между свободными концами, который считается артефактом
# скелета и «сшивается» по направлению штрихов.
HEAL_GAP_STROKE = 0.9
# Если свободный конец почти касается другого штриха, притягиваем его к
# середине этого штриха (Т-образный стык без разрыва).
ATTACH_STROKE = 0.6
# После чистки перемычек близкие узлы (например, разрезанная надвое стойка
# «ж») снова сводятся в один.
POST_JUNCTION_SNAP_STROKE = 0.5

# Петли скелета в стыках: ищем пиксели, которые дальше от края, чем половина
# толщины штриха. Именно там скелет и образует «блямбы».
CORE_TOL_R = 0.06
CORE_MIN_AREA_R = 0.10
CORE_REMOVE_R = 0.03
CORE_GROUP_ITERS = 2

# Точки (., :, ё и т.п.) рисуем маленьким кружком, как в ручных глифах.
DOT_RADIUS_CAP = 0.035
DOT_MAX_RATIO = 1.9
DOT_MIN_FILL = 0.60

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


# ---------------------------------------------------------------------------
# Векторные утилиты
# ---------------------------------------------------------------------------


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


def _fit_line(points):
    """Главная ось набора точек: (центр, единичное направление)."""
    pts = np.asarray(points, dtype=float)
    center = pts.mean(axis=0)
    if len(pts) < 2:
        return center, np.array([1.0, 0.0])
    _u, _s, vt = np.linalg.svd(pts - center)
    return center, vt[0]


def _line_intersection(lines, anchor):
    """Точка пересечения нескольких прямых (наименьшие квадраты).

    ``anchor`` — запасная точка (обычно самый «глубокий» пиксель стыка).
    Регуляризация нужна, когда все прямые почти параллельны: тогда ответ
    выбирается ближайшим к anchor, а не улетает в бесконечность.
    """
    if not lines:
        return np.asarray(anchor, dtype=float)
    if len(lines) == 1:
        p, d = lines[0]
        return p + np.dot(np.asarray(anchor, dtype=float) - p, d) * d

    a_rows = []
    b_rows = []
    for p, d in lines:
        n = np.array([-d[1], d[0]])
        a_rows.append(n)
        b_rows.append(float(n @ p))
    a_mat = np.asarray(a_rows)
    b_vec = np.asarray(b_rows)

    lam = 0.05
    mat = a_mat.T @ a_mat + lam * np.eye(2)
    rhs = a_mat.T @ b_vec + lam * np.asarray(anchor, dtype=float)
    try:
        return np.linalg.solve(mat, rhs)
    except np.linalg.LinAlgError:  # pragma: no cover - защита от вырождения
        return np.asarray(anchor, dtype=float)


# ---------------------------------------------------------------------------
# Скелет и стыки
# ---------------------------------------------------------------------------


def trace(sk, spur):
    """Разбивает скелет на цепочки между узлами и концами."""
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
            if len(path) > 200000:
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
        for point in path:
            if not clean or point != clean[-1]:
                clean.append(point)
        if len(clean) >= 2:
            out.append(clean)
    return out


def _outward_direction(path, end, look):
    """Единичный вектор из точки стыка внутрь штриха."""
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


def join_through_junctions(paths, look, max_turn_deg=JOIN_TURN_DEG):
    """Склеивает штрихи, которые проходят через стык почти по прямой."""
    limit = math.cos(math.radians(max_turn_deg))
    merged = [list(p) for p in paths]
    look = max(1, int(look))

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


def _endpoint_counts(paths):
    counts: dict[tuple[float, float], int] = {}
    for path in paths:
        if len(path) < 2:
            continue
        counts[path[0]] = counts.get(path[0], 0) + 1
        counts[path[-1]] = counts.get(path[-1], 0) + 1
    return counts


def _replace_endpoints(paths, old, new):
    for path in paths:
        if path and path[0] in old:
            path[0] = new
        if path and path[-1] in old:
            path[-1] = new


def _branch_line(path, end, length):
    """Прямая, аппроксимирующая конец штриха; направление — наружу."""
    pts = path[:length] if end == 0 else path[-length:][::-1]
    skip = min(3, max(0, len(pts) // 3))
    fit_pts = pts[skip:] if len(pts) - skip >= 2 else pts
    center, direction = _fit_line(fit_pts)
    if len(pts) >= 2 and np.dot(direction, np.asarray(pts[-1]) - np.asarray(pts[0])) < 0:
        direction = -direction
    return center, direction


def collapse_endpoint_clusters(paths, radius, r_px):
    """Сводит близкие концы в один узел по пересечению направлений штрихов."""
    entries = []
    for i, path in enumerate(paths):
        if len(path) < 2:
            continue
        entries.append((i, 0, path[0]))
        entries.append((i, 1, path[-1]))
    if not entries:
        return paths

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
        _ia, _ea, pa = entries[a]
        for b in range(a + 1, len(entries)):
            _ib, _eb, pb = entries[b]
            if math.dist(pa, pb) <= radius:
                union(a, b)

    clusters: dict[int, list[int]] = {}
    for a in range(len(entries)):
        clusters.setdefault(find(a), []).append(a)

    merged = [list(p) for p in paths]
    look = max(8, int(0.4 * r_px))
    for members in clusters.values():
        if len(members) < 2:
            continue
        points = [entries[a][2] for a in members]
        anchor = np.array(
            [
                sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points),
            ]
        )
        lines = [
            _branch_line(paths[entries[a][0]], entries[a][1], look)
            for a in members
        ]
        joint = _line_intersection(lines, anchor)
        if np.linalg.norm(joint - anchor) > max(2.0 * r_px, 3.0 * radius):
            joint = anchor
        point = (float(joint[0]), float(joint[1]))
        for a in members:
            i, end, _p = entries[a]
            if end == 0:
                merged[i][0] = point
            else:
                merged[i][-1] = point
    return merged


def _heal_short_branches(paths, stroke_px, r_px):
    """Убирает усики/перемычки и сшивает разрывы в стыках.

    Медиальная ось в месте пересечения штрихов иногда оставляет короткий
    «хвост» или перемычку между двумя узлами. Если их не убрать, буква
    рисуется с крючком, а «ж» — с горизонтальной палкой посередине.
    """
    paths = [list(p) for p in paths]
    if not paths:
        return paths

    # 1. Усики: один конец свободен, второй сидит в узле.
    changed = True
    while changed:
        changed = False
        counts = _endpoint_counts(paths)
        for i, path in enumerate(paths):
            if len(path) < 2:
                paths.pop(i)
                changed = True
                break
            c0 = counts.get(path[0], 0)
            c1 = counts.get(path[-1], 0)
            one_free = (c0 == 1) != (c1 == 1)
            both_free = c0 == 1 and c1 == 1
            # Короткий обрывок (оба конца свободны) — тоже мусор скелета:
            # именно он цепляется за конец штриха и делает крючок на «Х».
            if (one_free or both_free) and plen(path) < TERMINAL_SPUR_STROKE * stroke_px:
                paths.pop(i)
                changed = True
                break

    # 2. Перемычки: короткий штрих между двумя узлами. Схлопываем узлы в один
    #    и выбрасываем саму перемычку.
    changed = True
    while changed:
        changed = False
        counts = _endpoint_counts(paths)
        for i, path in enumerate(paths):
            if len(path) < 3:
                continue
            p0, p1 = path[0], path[-1]
            if p0 == p1:
                continue
            if (
                counts.get(p0, 0) >= 2
                and counts.get(p1, 0) >= 2
                and plen(path) < BRIDGE_MAX_STROKE * stroke_px
            ):
                middle = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)
                _replace_endpoints(paths, {p0, p1}, middle)
                paths.pop(i)
                changed = True
                break

    # 3. Разрывы между свободными концами. Если концы близко, продолжаем
    #    линии штрихов до пересечения — так вершина «М» и «А» не сплющивается.
    counts = _endpoint_counts(paths)
    free = []
    for i, path in enumerate(paths):
        if len(path) < 2:
            continue
        for end in (0, 1):
            point = path[0] if end == 0 else path[-1]
            if counts.get(point, 0) == 1:
                free.append((i, end, point))

    heal_r = HEAL_GAP_STROKE * stroke_px
    look = max(8, int(0.4 * r_px))
    used = set()
    for a in range(len(free)):
        if a in used:
            continue
        ia, ea, pa = free[a]
        best = None
        for b in range(a + 1, len(free)):
            if b in used:
                continue
            ib, eb, pb = free[b]
            if ia == ib and ea == eb:
                continue
            distance = math.dist(pa, pb)
            if distance <= heal_r and (best is None or distance < best[0]):
                best = (distance, b, ib, eb, pb)
        if best is None:
            continue
        distance, b, ib, eb, pb = best
        used.add(a)
        used.add(b)

        if ia == ib:
            # Замыкаем почти закрытую петлю.
            joint = ((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0)
        else:
            line_a = _branch_line(paths[ia], ea, look)
            line_b = _branch_line(paths[ib], eb, look)
            anchor = np.array([(pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0])
            joint_point = _line_intersection([line_a, line_b], anchor)
            # Защита от почти параллельных штрихов: пересечение не должно
            # улетать далеко от самих концов.
            limit = 1.5 * max(distance, 1.0)
            if (
                np.linalg.norm(joint_point - np.asarray(pa)) <= limit
                and np.linalg.norm(joint_point - np.asarray(pb)) <= limit
            ):
                joint = (float(joint_point[0]), float(joint_point[1]))
            else:
                joint = (float(anchor[0]), float(anchor[1]))

        paths[ia][0 if ea == 0 else -1] = joint
        paths[ib][0 if eb == 0 else -1] = joint

    return paths


def _closest_on_segment(point, a, b):
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    norm2 = dx * dx + dy * dy
    if norm2 <= 1e-12:
        return a, math.dist(point, a)
    t = ((px - ax) * dx + (py - ay) * dy) / norm2
    t = max(0.0, min(1.0, t))
    q = (ax + t * dx, ay + t * dy)
    return q, math.dist(point, q)


def _attach_free_ends(paths, stroke_px):
    """Притягивает свободные концы к ближайшему другому штриху.

    Так конец перекладины «А» или средней палки «Я» садится ровно на
    осевую линию стойки, а не остаётся в стороне от неё.
    """
    counts = _endpoint_counts(paths)
    free = []
    for i, path in enumerate(paths):
        if len(path) < 2:
            continue
        for end in (0, 1):
            point = path[0] if end == 0 else path[-1]
            if counts.get(point, 0) == 1:
                free.append((i, end, point))

    attach_r = ATTACH_STROKE * stroke_px
    for i, end, point in free:
        best = None
        for j, other in enumerate(paths):
            if j == i or len(other) < 2:
                continue
            for k in range(len(other) - 1):
                q, distance = _closest_on_segment(point, other[k], other[k + 1])
                if distance <= attach_r and (best is None or distance < best[0]):
                    best = (distance, q)
        if best is not None:
            paths[i][0 if end == 0 else -1] = (float(best[1][0]), float(best[1][1]))
    return paths


def _snap_junctions_to_centerline(paths, dist, r_px):
    """Не даёт узлу уехать на край штриха.

    Пересечение аппроксимирующих прямых иногда попадает в тонкое место у
    края. Если узел оказался ближе к краю, чем половина толщины, двигаем его
    на ближайшую «толстую» точку — там и проходит середина штриха.
    """
    counts = _endpoint_counts(paths)
    height, width = dist.shape
    radius = max(2, int(1.5 * r_px))
    for point, count in counts.items():
        if count < 2:
            continue
        x, y = point
        xi = int(round(x))
        yi = int(round(y))
        if not (0 <= yi < height and 0 <= xi < width):
            continue
        if dist[yi, xi] >= 0.7 * r_px:
            continue
        best = None
        x0 = max(0, xi - radius)
        x1 = min(width, xi + radius + 1)
        y0 = max(0, yi - radius)
        y1 = min(height, yi + radius + 1)
        window = dist[y0:y1, x0:x1]
        yy, xx = np.nonzero(window >= 0.85 * r_px)
        if len(xx) == 0:
            continue
        dx = (xx + x0 - x) ** 2
        dy = (yy + y0 - y) ** 2
        k = int(np.argmin(dx + dy))
        target = (float(xx[k] + x0), float(yy[k] + y0))
        for path in paths:
            if path and path[0] == point:
                path[0] = target
            if path and path[-1] == point:
                path[-1] = target
    return paths


def circle(centroid, radius, n=12):
    cx, cy = centroid
    return [
        (
            cx + radius * math.cos(2.0 * math.pi * i / n),
            cy + radius * math.sin(2.0 * math.pi * i / n),
        )
        for i in range(n + 1)
    ]


def _collapse_junction_loops(sk, dist, stroke_px):
    """Убирает петли скелета в местах пересечения штрихов.

    В стыке трёх и более штрихов медиальная ось образует маленькую петлю
    вокруг «утолщения». Если такую петлю оставить, при трассировке получаются
    крючки и перемычки. Возвращает очищенный скелет и список стыков:
    (пиксели ядра, опорная точка, радиус удаления).
    """
    r_px = stroke_px / 2.0
    tol = max(1.5, CORE_TOL_R * r_px)
    core = sk & (dist > r_px + tol)
    if not core.any():
        return sk, []

    # Склеиваем соседние пиксели ядра, чтобы не обрабатывать один стык
    # десяток раз по кусочкам.
    grouped = ndi.binary_dilation(core, structure=np.ones((3, 3)), iterations=CORE_GROUP_ITERS)
    labels, count = ndi.label(grouped, structure=np.ones((3, 3)))
    remove_r = max(2, int(CORE_REMOVE_R * r_px))
    min_area = max(16, int(CORE_MIN_AREA_R * r_px))

    work = sk.copy()
    junctions = []
    for index in range(1, count + 1):
        comp = core & (labels == index)
        area = int(comp.sum())
        if area < min_area:
            continue
        ys, xs = np.nonzero(comp)
        # Самая «глубокая» точка стыка — запасной вариант для пересечения.
        k = int(np.argmax(dist[ys, xs]))
        anchor = np.array([float(xs[k]), float(ys[k])])
        work &= ~ndi.binary_dilation(comp, structure=disk(remove_r))
        junctions.append((np.column_stack([xs, ys]), anchor, remove_r))
    return work, junctions


def _dist_at(dist, point):
    x, y = point
    xi = int(round(x))
    yi = int(round(y))
    if 0 <= yi < dist.shape[0] and 0 <= xi < dist.shape[1]:
        return float(dist[yi, xi])
    return 0.0


def _trim_boundary_ends(paths, dist, r_px):
    """Срезает усики скелета, уходящие в острый угол на конце штриха.

    У диагональных штрихов, обрезанных по габариту буквы, медиальная ось
    иногда добегает до самого угла. Нам нужен не угол, а середина торца —
    точка, удалённая от края примерно на половину толщины.
    """
    out = []
    limit = max(2, int(2.0 * r_px))
    for path in paths:
        if len(path) < 3:
            out.append(path)
            continue
        lo = 0
        hi = len(path) - 1
        if _dist_at(dist, path[lo]) < 0.5 * r_px:
            for k in range(1, min(limit, len(path) - 2) + 1):
                if _dist_at(dist, path[k]) >= 0.85 * r_px:
                    lo = k
                    break
        if _dist_at(dist, path[hi]) < 0.5 * r_px:
            for k in range(1, min(limit, len(path) - 2) + 1):
                idx = len(path) - 1 - k
                if idx <= lo + 1:
                    break
                if _dist_at(dist, path[idx]) >= 0.85 * r_px:
                    hi = idx
                    break
        if hi - lo + 1 >= 2:
            out.append(path[lo : hi + 1])
    return out


def _build_component(sub, stroke_px, px_per_unit):
    """Возвращает осевые линии одной связной компоненты (в координатах sub)."""
    if not sub.any():
        return []
    r_px = stroke_px / 2.0
    sk, dist = medial_axis(sub, return_distance=True)
    sk, junctions = _collapse_junction_loops(sk, dist, stroke_px)
    if not sk.any():
        return []

    paths = [list(map(tuple, p)) for p in trace(sk, spur=SPUR_UNITS * px_per_unit)]
    paths = _trim_boundary_ends(paths, dist, r_px)

    near_r = max(int(0.03 * r_px) + 2 * px_per_unit + 1, 3)
    look = max(8, int(JUNCTION_LOOK_R * r_px))
    for points, anchor, _remove_r in junctions:
        tree = cKDTree(points)
        entries = []
        for pi, path in enumerate(paths):
            for end in (0, 1):
                px, py = path[0] if end == 0 else path[-1]
                if tree.query_ball_point([px, py], near_r):
                    entries.append((pi, end))
        if not entries:
            continue

        lines = []
        for pi, end in entries:
            path = paths[pi]
            length = min(len(path), look)
            pts = path[:length] if end == 0 else path[-length:][::-1]
            # Первые пиксели у стыка ещё «загнуты», для прямой они вредны.
            skip = min(3, max(0, len(pts) // 3))
            fit_pts = pts[skip:] if len(pts) - skip >= 2 else pts
            center, direction = _fit_line(fit_pts)
            if np.dot(direction, np.asarray(pts[-1]) - np.asarray(pts[0])) < 0:
                direction = -direction
            lines.append((center, direction))

        junction = _line_intersection(lines, anchor)
        if np.linalg.norm(junction - anchor) > max(2.0 * r_px, 3.0 * near_r):
            junction = anchor
        for pi, end in entries:
            if end == 0:
                paths[pi][0] = (float(junction[0]), float(junction[1]))
            else:
                paths[pi][-1] = (float(junction[0]), float(junction[1]))

    snap_r = JUNCTION_SNAP_UNITS * px_per_unit
    paths = collapse_endpoint_clusters(paths, snap_r, r_px)
    paths = _heal_short_branches(paths, stroke_px, r_px)
    paths = collapse_endpoint_clusters(
        paths, POST_JUNCTION_SNAP_STROKE * stroke_px, r_px
    )
    paths = _attach_free_ends(paths, stroke_px)
    paths = _snap_junctions_to_centerline(paths, dist, r_px)

    # Чистим дубликаты и откровенный мусор.
    cleaned = []
    for path in paths:
        out = []
        for point in path:
            if not out or point != out[-1]:
                out.append(point)
        if len(out) < 2:
            continue
        length = plen(out)
        closed = math.dist(out[0], out[-1]) <= snap_r
        if closed and length < 4.0 * r_px:
            # Маленькая петля — остаток узла, а не настоящая петля буквы.
            continue
        if length < 0.2 * r_px:
            continue
        cleaned.append(out)

    cleaned = merge_paths(cleaned)
    cleaned = join_through_junctions(cleaned, look=look)

    # Замыкаем большие петли, у которых скелет оставил крошечную щель на
    # талии «8» или на стыке чаши. Открытые C/U-образные штрихи не трогаем:
    # у них расстояние между концами заметно больше.
    close_gap = 2.5 * stroke_px
    close_min_length = 10.0 * stroke_px
    for path in cleaned:
        if len(path) < 3:
            continue
        if plen(path) > close_min_length and math.dist(path[0], path[-1]) < close_gap:
            middle = (
                (path[0][0] + path[-1][0]) / 2.0,
                (path[0][1] + path[-1][1]) / 2.0,
            )
            path[0] = middle
            path[-1] = middle

    result = []
    for path in cleaned:
        path = smooth(path, passes=SMOOTH_PASSES)
        path = rdp(path, eps=RDP_EPS_UNITS * px_per_unit)
        if len(path) < 2 or plen(path) < 0.4 * px_per_unit:
            continue
        result.append(path)
    return result


def build_glyph(pil_font, ch, cap_units, stroke_units, px_per_unit):
    """Строит центральные линии глифа в нормированных координатах."""
    bbox = pil_font.getbbox(ch, anchor="ls")
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0:
        return None

    pad = int(PAD_UNITS * px_per_unit)
    width = x1 - x0 + pad * 2
    height = y1 - y0 + pad * 2
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    draw.text((pad - x0, pad - y0), ch, font=pil_font, fill=255, anchor="ls")
    mask = np.array(image) > 127
    origin_x = pad - x0
    baseline = pad - y0
    if not mask.any():
        return None

    stroke_px = stroke_units * px_per_unit
    r_px = stroke_px / 2.0
    dot_radius = DOT_RADIUS_CAP * cap_units * px_per_unit

    labels = label(mask, connectivity=2)
    pixel_strokes: list[list[tuple[float, float]]] = []

    for region in regionprops(labels):
        r0, c0, r1, c1 = region.bbox
        ww = c1 - c0
        hh = r1 - r0
        area = int(region.area)

        # Точки и прочие компактные кляксы рисуем маленьким кружком.
        if (
            ww <= DOT_MAX_RATIO * stroke_px
            and hh <= DOT_MAX_RATIO * stroke_px
            and area / max(1, ww * hh) > DOT_MIN_FILL
        ):
            cy, cx = region.centroid
            pixel_strokes.append(circle((cx, cy), dot_radius))
            continue

        # Небольшой запас фона вокруг компоненты, чтобы distance transform
        # не считал край картинки контуром буквы.
        margin = 2
        cr0 = max(0, r0 - margin)
        cc0 = max(0, c0 - margin)
        cr1 = min(mask.shape[0], r1 + margin)
        cc1 = min(mask.shape[1], c1 + margin)
        sub = labels[cr0:cr1, cc0:cc1] == region.label

        for path in _build_component(sub, stroke_px, px_per_unit):
            pixel_strokes.append([(x + cc0, y + cr0) for x, y in path])

    strokes: list[list[tuple[float, float]]] = []
    for path in pixel_strokes:
        pts = [
            (
                round((x - origin_x) / px_per_unit / cap_units, 4),
                round((baseline - y) / px_per_unit / cap_units, 4),
            )
            for x, y in path
        ]
        clean = []
        for point in pts:
            if not clean or point != clean[-1]:
                clean.append(point)
        if len(clean) >= 2:
            strokes.append(clean)
    return strokes


def _cap_height(ttf: TTFont, cmap: dict[int, str]) -> float:
    """Высота прописных в юнитах шрифта (по глифу «Н»)."""
    name = cmap.get(ord("Н"))
    glyf = ttf["glyf"]
    if name and name in glyf:
        return float(glyf[name].yMax)
    try:
        cap = float(ttf["OS/2"].sCapHeight)
        if cap > 0:
            return cap
    except Exception:  # pragma: no cover - экзотические шрифты
        pass
    return float(ttf["head"].unitsPerEm) * 0.7


def render_font(ttf_path: Path, key: str, scale: float):
    ttf = TTFont(str(ttf_path))
    cmap = ttf.getBestCmap()
    glyf = ttf["glyf"]
    hmtx = ttf["hmtx"]
    upm = float(ttf["head"].unitsPerEm)
    size = max(64, int(round(upm * scale)))
    px_per_unit = size / upm
    pil = ImageFont.truetype(str(ttf_path), size)

    cap_units = _cap_height(ttf, cmap)
    # ГОСТ тип А: d = h / 14, тип Б: d = h / 10.
    stroke_units = cap_units / (14.0 if key == "A" else 10.0)

    glyphs: dict[str, tuple[float, list[list[tuple[float, float]]]]] = {}
    for index, ch in enumerate(CHARSET, start=1):
        cp = ord(ch)
        if ch == " " or ch in SKIP_CHARS or cp not in cmap:
            continue
        name = cmap[cp]
        if name == ".notdef":
            continue
        strokes = build_glyph(pil, ch, cap_units, stroke_units, px_per_unit)
        if not strokes:
            continue
        advance = hmtx[name][0] / cap_units
        glyphs[ch] = (round(advance, 4), strokes)
        if index % 10 == 0:
            print(f"  {key}: {index}/{len(CHARSET)}", flush=True)

    space_name = cmap.get(32)
    word_spacing = hmtx[space_name][0] / cap_units if space_name else 0.43
    x_name = cmap.get(ord("x"))
    x_height = glyf[x_name].yMax / cap_units if x_name else 0.70

    return {
        "ascent": round(ttf["hhea"].ascent / cap_units, 4),
        "descent": round(ttf["hhea"].descent / cap_units, 4),
        "x_height": round(x_height, 4),
        "word_spacing": round(word_spacing, 4),
        "line_spacing": 1.57 if key == "A" else 1.70,
        "letter_spacing": 0.0,
        "line_width": round(stroke_units / cap_units, 6),
        "glyphs": glyphs,
    }


def format_data(data: dict, scale: float) -> str:
    out = [
        '"""Центральные линии, извлечённые из GOST_A.TTF и GOST_B.ttf."""',
        "",
        f"# Сгенерировано tools/build_centerlines.py (медиальная ось, растр {scale:g}×).",
        "# Не редактировать вручную.",
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
    parser = argparse.ArgumentParser(description="Извлечение осевых линий из TTF ГОСТ")
    parser.add_argument("--font-a", default=str(FONT_DIR / "GOST_A.TTF"))
    parser.add_argument("--font-b", default=str(FONT_DIR / "GOST_B.ttf"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--scale",
        type=float,
        default=DEFAULT_RENDER_SCALE,
        help="разрешение растеризации, пикселей на юнит шрифта (по умолчанию 2.0)",
    )
    args = parser.parse_args()
    if args.scale <= 0.25:
        parser.error("--scale должен быть больше 0.25")

    data = {
        "A": render_font(Path(args.font_a), "A", args.scale),
        "B": render_font(Path(args.font_b), "B", args.scale),
    }
    output = Path(args.output)
    output.write_text(format_data(data, args.scale), encoding="utf-8")
    for key in ("A", "B"):
        glyphs = data[key]["glyphs"]
        points = sum(len(s) for _, strokes in glyphs.values() for s in strokes)
        print(f"{key}: {len(glyphs)} глифов, {points} точек -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
