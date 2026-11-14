"""Раскладка многострочного текста в штрихи на плоскости станка."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .fonts.gost import StrokeFont, get_font
from .fonts.primitives import Point, Stroke, clean_stroke

BBox = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax

# Максимальный разрыв между концами штрихов одного глифа, который считается
# стыком и склеивается в один проход. Меньше одной толщины штриха: так «N»
# собирается в один штрих, а точка над «i» или breve «Й» не приклеиваются
# к букве.
CHAIN_TOL_STROKE = 1.1


def normalize_text(text: str) -> str:
    """Приводит вводимый текст к символам, которые есть в шрифте."""
    replacements = {
        "\r\n": "\n",
        "\r": "\n",
        "\t": "    ",
        "\u2212": "-",  # минус
        "\u2010": "-",
        "\u2011": "-",
        "\u00ad": "-",
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u00a0": "\u00a0",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def _advance(
    char: str,
    font: StrokeFont,
    size: float,
    letter_spacing: float,
    word_spacing: float,
) -> float:
    if char == " ":
        return word_spacing
    glyph = font.glyph(char)
    if glyph is None:
        return 0.5 * size
    return glyph.width * font.x_scale * size + letter_spacing


def measure_line(
    line: str,
    font: StrokeFont,
    size_mm: float,
    letter_spacing_mm: float | None = None,
    word_spacing_mm: float | None = None,
) -> float:
    """Ширина строки в мм (без крайнего межбуквенного интервала)."""
    if not line:
        return 0.0
    ls = font.default_letter_spacing * size_mm if letter_spacing_mm is None else letter_spacing_mm
    ws = font.default_word_spacing * size_mm if word_spacing_mm is None else word_spacing_mm
    total = 0.0
    for ch in line:
        total += _advance(ch, font, size_mm, ls, ws)
    if line[-1] != " ":
        total -= ls
    return max(0.0, total)


def _hard_break(
    word: str,
    font: StrokeFont,
    size: float,
    ls: float,
    ws: float,
    max_width: float,
) -> list[str]:
    parts: list[str] = []
    current = ""
    for ch in word:
        candidate = current + ch
        if current and measure_line(candidate, font, size, ls, ws) > max_width:
            parts.append(current)
            current = ch
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def wrap_text(
    text: str,
    font: StrokeFont,
    size_mm: float,
    max_width_mm: float | None,
    letter_spacing_mm: float | None = None,
    word_spacing_mm: float | None = None,
) -> list[str]:
    """Переносит текст по словам, сохраняя заданные переводы строк."""
    text = normalize_text(text)
    if not max_width_mm or max_width_mm <= 0:
        return text.split("\n")

    ls = font.default_letter_spacing * size_mm if letter_spacing_mm is None else letter_spacing_mm
    ws = font.default_word_spacing * size_mm if word_spacing_mm is None else word_spacing_mm
    result: list[str] = []

    for paragraph in text.split("\n"):
        if paragraph == "":
            result.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            if word == "":
                if current:
                    current += " "
                continue
            candidate = word if not current else current + " " + word
            if measure_line(candidate, font, size_mm, ls, ws) <= max_width_mm:
                current = candidate
                continue
            if current:
                result.append(current)
                current = ""
            if measure_line(word, font, size_mm, ls, ws) <= max_width_mm:
                current = word
            else:
                pieces = _hard_break(word, font, size_mm, ls, ws, max_width_mm)
                result.extend(pieces[:-1])
                current = pieces[-1] if pieces else ""
        result.append(current)
    return result


def _distance(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def chain_strokes(paths: Sequence[Sequence[Point]], tolerance: float) -> list[Stroke]:
    """Склеивает штрихи глифа в один проход, если их концы почти совпадают.

    В отличие от простого «конец предыдущего = начало следующего», здесь
    можно менять направление штрихов и склеивать любой конец с любым.
    Например, у «N» диагональ не дотягивается до верха стойки на доли
    толщины; короткий соединитель идёт по стойке, и буква пишется одним
    проходом без подъёма маркера.
    """
    chains: list[Stroke] = [
        [(float(x), float(y)) for x, y in path] for path in paths if len(path) >= 2
    ]
    if len(chains) < 2:
        return chains

    while True:
        best = None
        for i in range(len(chains)):
            for j in range(i + 1, len(chains)):
                for end_i in (0, 1):
                    for end_j in (0, 1):
                        pi = chains[i][0] if end_i == 0 else chains[i][-1]
                        pj = chains[j][0] if end_j == 0 else chains[j][-1]
                        distance = _distance(pi, pj)
                        if distance <= tolerance and (
                            best is None or distance < best[0]
                        ):
                            best = (distance, i, end_i, j, end_j)
        if best is None:
            break

        _distance_best, i, end_i, j, end_j = best
        first = chains[i]
        second = chains[j]
        if end_i == 0:
            first = first[::-1]
        if end_j == 1:
            second = second[::-1]

        # Если концы совпадают, точку стыка не дублируем. Если между ними
        # осталась щель — она станет коротким соединителем с опущенным пером.
        overlap = _distance(first[-1], second[0]) <= 1e-9
        chains[i] = first + (second[1:] if overlap else second)
        chains.pop(j)

    out: list[Stroke] = []
    for chain in chains:
        clean: Stroke = []
        for point in chain:
            if not clean or _distance(clean[-1], point) > 1e-9:
                clean.append(point)
        if len(clean) >= 2:
            out.append(clean)
    return out


@dataclass
class LayoutResult:
    paths: list[Stroke] = field(default_factory=list)
    bbox: BBox | None = None
    line_count: int = 0
    lines: list[str] = field(default_factory=list)
    line_widths: list[float] = field(default_factory=list)
    baselines: list[float] = field(default_factory=list)
    missing: set[str] = field(default_factory=set)
    text_width: float = 0.0
    text_height: float = 0.0

    @property
    def stroke_count(self) -> int:
        return len(self.paths)


def layout_text(
    text: str,
    *,
    font: StrokeFont | str = "A",
    size_mm: float = 10.0,
    x_mm: float = 5.0,
    y_mm: float = 5.0,
    align: str = "left",
    valign: str = "bottom",
    letter_spacing_mm: float | None = None,
    line_spacing_mm: float | None = None,
    word_spacing_mm: float | None = None,
    max_width_mm: float | None = None,
) -> LayoutResult:
    """Раскладывает текст в абсолютные координаты станка (мм).

    align: left / center / right
    valign: bottom / middle / top
    letter_spacing_mm = None -> по ГОСТ (2d)
    line_spacing_mm = None -> по ГОСТ (22d для A, 17d для B)
    """
    if isinstance(font, str):
        font = get_font(font)
    if size_mm <= 0:
        raise ValueError("Размер шрифта должен быть больше нуля")
    if align not in {"left", "center", "right"}:
        raise ValueError("align должен быть left/center/right")
    if valign not in {"bottom", "middle", "top"}:
        raise ValueError("valign должен быть bottom/middle/top")

    ls = font.default_letter_spacing * size_mm if letter_spacing_mm is None else letter_spacing_mm
    ws = font.default_word_spacing * size_mm if word_spacing_mm is None else word_spacing_mm
    line_step = font.default_line_spacing * size_mm if line_spacing_mm is None else line_spacing_mm
    if line_step <= 0:
        line_step = font.default_line_spacing * size_mm

    lines = wrap_text(text, font, size_mm, max_width_mm, ls, ws)
    if not lines:
        lines = [""]
    n = len(lines)

    line_widths = [measure_line(line, font, size_mm, ls, ws) for line in lines]
    text_width = max(line_widths, default=0.0)

    ascent = font.ascent * size_mm
    descent = font.descent * size_mm
    block_height = (n - 1) * line_step + (ascent - descent)

    if valign == "top":
        first_baseline = y_mm - ascent
    elif valign == "middle":
        top = y_mm + block_height / 2.0
        first_baseline = top - ascent
    else:  # bottom
        last_baseline = y_mm - descent
        first_baseline = last_baseline + (n - 1) * line_step

    baselines: list[float] = []
    result = LayoutResult(
        line_count=n,
        lines=lines,
        line_widths=line_widths,
        text_width=text_width,
        text_height=block_height,
    )

    for idx, line in enumerate(lines):
        baseline = first_baseline - idx * line_step
        baselines.append(baseline)
        if align == "left":
            cursor = x_mm
        elif align == "center":
            cursor = x_mm - line_widths[idx] / 2.0
        else:
            cursor = x_mm - line_widths[idx]

        for ch in line:
            if ch == " ":
                cursor += ws
                continue
            glyph = font.glyph(ch)
            if glyph is None:
                result.missing.add(ch)
                cursor += 0.5 * size_mm
                continue
            glyph_paths: list[Stroke] = []
            for stroke in glyph.strokes:
                pts: Stroke = []
                for gx, gy in stroke:
                    pts.append(
                        (
                            cursor + gx * font.x_scale * size_mm,
                            baseline + gy * size_mm,
                        )
                    )
                pts = clean_stroke(pts)
                if len(pts) >= 2:
                    glyph_paths.append(pts)
            chain_tol = max(0.02, CHAIN_TOL_STROKE * font.line_width * size_mm)
            result.paths.extend(chain_strokes(glyph_paths, chain_tol))
            cursor += glyph.width * font.x_scale * size_mm + ls
        # после последнего символа межбуквенный интервал не нужен,
        # но на ширину строки он уже не влияет

    result.baselines = baselines
    result.bbox = paths_bbox(result.paths)
    if result.bbox:
        result.text_height = result.bbox[3] - result.bbox[1]
    return result


def paths_bbox(paths: Iterable[Sequence[Point]]) -> BBox | None:
    xs: list[float] = []
    ys: list[float] = []
    for path in paths:
        for x, y in path:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def bbox_size(bbox: BBox | None) -> tuple[float, float]:
    if not bbox:
        return 0.0, 0.0
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def bbox_intersects(a: BBox | None, b: BBox | None) -> bool:
    if not a or not b:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])
