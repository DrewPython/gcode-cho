"""Экспорт предпросмотра в SVG (для проверки без запуска GUI)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from .config import MachineConfig, PreviewConfig, TagConfig
from .fonts.gost import StrokeFont
from .gcode import GcodeResult, fmt
from .layout import LayoutResult, paths_bbox
from .tag import TagGeometry


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_svg(
    layout: LayoutResult,
    job: GcodeResult | None,
    font: StrokeFont,
    preview: PreviewConfig,
    tag: TagConfig,
    *,
    title: str = "GOST Marker — предпросмотр",
    scale: float = 4.0,
    size_mm: float | None = None,
) -> str:
    """Возвращает SVG-строку с полем станка, биркой и траекториями."""
    fw = max(1.0, preview.field_width_mm)
    fh = max(1.0, preview.field_height_mm)
    margin = 26.0
    width = fw * scale + margin * 2
    height = fh * scale + margin * 2 + 70

    def X(x: float) -> float:
        return margin + x * scale

    def Y(y: float) -> float:
        return margin + (fh - y) * scale

    def pts(path: Iterable[tuple[float, float]]) -> str:
        return " ".join(f"{X(x):.2f},{Y(y):.2f}" for x, y in path)

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}">'
    )
    out.append("<defs>")
    out.append(
        '<pattern id="grid10" width="10" height="10" patternUnits="userSpaceOnUse">'
        '<path d="M 10 0 L 0 0 0 10" fill="none" stroke="#e8edf4" stroke-width="0.8"/></pattern>'
    )
    out.append(
        '<pattern id="grid1" width="1" height="1" patternUnits="userSpaceOnUse">'
        '<path d="M 1 0 L 0 0 0 1" fill="none" stroke="#f4f7fb" stroke-width="0.35"/></pattern>'
    )
    out.append("</defs>")
    out.append(f'<rect x="0" y="0" width="{width:.0f}" height="{height:.0f}" fill="#ffffff"/>')
    out.append(
        f'<text x="{margin:.0f}" y="20" font-family="Segoe UI, Arial, sans-serif" '
        f'font-size="15" font-weight="600" fill="#1f2937">{_esc(title)}</text>'
    )

    # поле
    out.append(
        f'<rect x="{X(0):.2f}" y="{Y(fh):.2f}" width="{fw*scale:.2f}" height="{fh*scale:.2f}" '
        f'fill="url(#grid1)" stroke="#cbd5e1" stroke-width="1"/>'
    )
    out.append(
        f'<rect x="{X(0):.2f}" y="{Y(fh):.2f}" width="{fw*scale:.2f}" height="{fh*scale:.2f}" '
        f'fill="url(#grid10)" stroke="none"/>'
    )

    # подписи сетки
    for x in range(0, int(fw) + 1, 10):
        out.append(
            f'<text x="{X(x):.1f}" y="{Y(0)+14:.1f}" font-family="Segoe UI, Arial" '
            f'font-size="9" fill="#94a3b8" text-anchor="middle">{x}</text>'
        )
    for y in range(0, int(fh) + 1, 10):
        out.append(
            f'<text x="{X(0)-6:.1f}" y="{Y(y)+3:.1f}" font-family="Segoe UI, Arial" '
            f'font-size="9" fill="#94a3b8" text-anchor="end">{y}</text>'
        )

    # бирка
    if preview.show_tag and tag.mode == "u136":
        tg = TagGeometry(tag.side_mm, tag.margin_mm)
        verts = tg.vertices
        out.append(
            f'<polygon points="{pts(verts)}" fill="#fff7ed" fill-opacity="0.65" '
            f'stroke="#f59e0b" stroke-width="1.6" stroke-linejoin="round"/>'
        )
        safe = tg.safe_vertices
        out.append(
            f'<polygon points="{pts(safe)}" fill="none" stroke="#fbbf24" '
            f'stroke-width="0.8" stroke-dasharray="4 3"/>'
        )
        if tag.show_slots:
            for slot in tg.slots():
                out.append(
                    f'<polygon points="{pts(slot)}" fill="#ffffff" stroke="#f59e0b" stroke-width="0.8"/>'
                )
        cx, cy = tg.centroid
        out.append(
            f'<text x="{X(cx):.1f}" y="{Y(cy)-tg.height*scale*0.32:.1f}" text-anchor="middle" '
            f'font-family="Segoe UI, Arial" font-size="10" fill="#b45309">У-136 · {fmt(tg.side)} мм</text>'
        )

    # холостые ходы
    if preview.show_travel and job:
        for a, b in job.travels:
            out.append(
                f'<line x1="{X(a[0]):.2f}" y1="{Y(a[1]):.2f}" x2="{X(b[0]):.2f}" y2="{Y(b[1]):.2f}" '
                f'stroke="#cbd5e1" stroke-width="0.9" stroke-dasharray="3 3"/>'
            )

    # штрихи
    # толщина из GOST: d = h * line_width
    if size_mm and size_mm > 0:
        line_px = max(0.9, font.line_width * size_mm * scale)
    else:
        cap_px = 0.0
        if layout.bbox:
            cap_px = (layout.bbox[3] - layout.bbox[1]) * scale
        line_px = max(0.9, cap_px * font.line_width)
    for path in layout.paths:
        out.append(
            f'<polyline points="{pts(path)}" fill="none" stroke="#1d4ed8" '
            f'stroke-width="{line_px:.2f}" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    # bbox
    if layout.bbox:
        x0, y0, x1, y1 = layout.bbox
        out.append(
            f'<rect x="{X(x0):.2f}" y="{Y(y1):.2f}" width="{(x1-x0)*scale:.2f}" height="{(y1-y0)*scale:.2f}" '
            f'fill="none" stroke="#10b981" stroke-width="1" stroke-dasharray="5 4"/>'
        )

    # начало координат
    out.append(
        f'<line x1="{X(0)-10:.1f}" y1="{Y(0):.1f}" x2="{X(0)+18:.1f}" y2="{Y(0):.1f}" stroke="#ef4444" stroke-width="1.2"/>'
    )
    out.append(
        f'<line x1="{X(0):.1f}" y1="{Y(0)+10:.1f}" x2="{X(0):.1f}" y2="{Y(0)-24:.1f}" stroke="#ef4444" stroke-width="1.2"/>'
    )
    out.append(
        f'<text x="{X(0)+22:.1f}" y="{Y(0)+4:.1f}" font-family="Segoe UI, Arial" '
        f'font-size="10" fill="#ef4444">X</text>'
    )
    out.append(
        f'<text x="{X(0)+4:.1f}" y="{Y(0)-28:.1f}" font-family="Segoe UI, Arial" '
        f'font-size="10" fill="#ef4444">Y</text>'
    )

    # подпись снизу
    if job:
        s = job.stats
        info = (
            f"Строк: {layout.line_count} · штрихов: {s.stroke_count} · "
            f"контур: {fmt(s.draw_length_mm)} мм · холостые: {fmt(s.travel_length_mm)} мм · "
            f"оценка: {int(s.estimated_seconds // 60)}:{int(s.estimated_seconds % 60):02d}"
        )
    else:
        info = f"Строк: {layout.line_count} · штрихов: {layout.stroke_count}"
    out.append(
        f'<text x="{margin:.0f}" y="{height-18:.0f}" font-family="Segoe UI, Arial" '
        f'font-size="11" fill="#475569">{_esc(info)}</text>'
    )

    if layout.missing:
        out.append(
            f'<text x="{margin:.0f}" y="{height-4:.0f}" font-family="Segoe UI, Arial" '
            f'font-size="11" fill="#dc2626">Нет в шрифте: {_esc(" ".join(sorted(layout.missing)))}</text>'
        )

    out.append("</svg>")
    return "\n".join(out)


def save_svg(
    path: Path | str,
    layout: LayoutResult,
    job: GcodeResult | None,
    font: StrokeFont,
    preview: PreviewConfig,
    tag: TagConfig,
    **kwargs,
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_svg(layout, job, font, preview, tag, **kwargs), encoding="utf-8")
    return p
