"""Быстрые проверки без запуска GUI и без станка."""

from __future__ import annotations

import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gost_marker.config import (  # noqa: E402
    MachineConfig,
    PreviewConfig,
    Settings,
    TagConfig,
    TextConfig,
    load_settings,
    save_settings,
)
from gost_marker.fonts.gost import get_font  # noqa: E402
from gost_marker.gcode import fmt, generate_gcode  # noqa: E402
from gost_marker.grbl import GrblClient, JobCancelled, parse_status  # noqa: E402
from gost_marker.layout import bbox_size, layout_text  # noqa: E402
from gost_marker.preview_svg import render_svg  # noqa: E402
from gost_marker.tag import TagGeometry  # noqa: E402


class _FakeGrbl(GrblClient):
    """Клиент без реального порта: подтверждает строки и записывает их."""

    def __init__(self, ack: bool = True) -> None:
        super().__init__()
        self.ack = ack
        self.written: list[str] = []
        self._connected = True
        self._serial = object()

    def _write_line(self, line: str) -> None:
        self.written.append(line)
        if self.ack:
            self._responses.put("ok")

    def _write_raw(self, data: bytes) -> None:
        pass

CYRILLIC = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
DIGITS = "0123456789"


class FontTests(unittest.TestCase):
    def test_cyrillic_and_digits_exist(self) -> None:
        for key in ("A", "B"):
            font = get_font(key)
            for char in CYRILLIC + DIGITS + ".-/":
                self.assertIsNotNone(font.glyph(char), f"{key}: {char}")

    def test_font_b_is_wider(self) -> None:
        a = get_font("A")
        b = get_font("B")
        self.assertGreater(b.glyph("Н").width, a.glyph("Н").width)
        self.assertGreater(b.line_width, a.line_width)

    def test_strokes_join_through_junctions(self) -> None:
        """Стойки «Н» должны быть цельными, а не разорванными перекладиной."""
        for key in ("A", "B"):
            glyph = get_font(key).glyph("Н")
            self.assertIsNotNone(glyph)
            self.assertEqual(len(glyph.strokes), 3, key)
            spans = [
                max(y for _x, y in stroke) - min(y for _x, y in stroke)
                for stroke in glyph.strokes
            ]
            self.assertGreater(max(spans), 0.8, key)

    def test_complex_junctions_are_clean(self) -> None:
        """В сложных стыках не должно быть палок и десятка обрывков."""
        for key in ("A", "B"):
            font = get_font(key)
            self.assertLessEqual(len(font.glyph("Ж").strokes), 4, key)
            self.assertLessEqual(len(font.glyph("К").strokes), 4, key)
            self.assertLessEqual(len(font.glyph("М").strokes), 3, key)
            self.assertLessEqual(len(font.glyph("8").strokes), 2, key)
            self.assertEqual(len(font.glyph("О").strokes), 1, key)

    def test_junction_glyphs_have_no_short_spurs(self) -> None:
        """Короткие усики в стыках — это артефакт, а не штрих."""
        for key in ("A", "B"):
            font = get_font(key)
            for char in "ЖКФ8МЕАУЯж":
                glyph = font.glyph(char)
                self.assertIsNotNone(glyph, f"{key}: {char}")
                for stroke in glyph.strokes:
                    length = sum(
                        math.dist(stroke[i], stroke[i + 1])
                        for i in range(len(stroke) - 1)
                    )
                    self.assertGreater(length, 0.08, f"{key}: {char}")

    def test_round_glyphs_are_closed(self) -> None:
        for key in ("A", "B"):
            font = get_font(key)
            for char in "О8":
                glyph = font.glyph(char)
                self.assertEqual(len(glyph.strokes), 1, f"{key}: {char}")
                stroke = glyph.strokes[0]
                self.assertLess(math.dist(stroke[0], stroke[-1]), 0.01, f"{key}: {char}")


class LayoutTests(unittest.TestCase):
    def test_multiline_layout(self) -> None:
        for key in ("A", "B"):
            layout = layout_text(
                "ЩР-1\n220 В",
                font=key,
                size_mm=8.0,
                x_mm=31.0,
                y_mm=22.0,
                align="center",
                valign="middle",
            )
            self.assertEqual(layout.line_count, 2)
            self.assertGreater(layout.stroke_count, 5)
            self.assertFalse(layout.missing)
            self.assertIsNotNone(layout.bbox)
            width, height = bbox_size(layout.bbox)
            self.assertGreater(width, 0.0)
            self.assertGreater(height, 0.0)

    def test_text_fits_safe_tag_zone(self) -> None:
        tag = TagGeometry(side=62.0, margin=2.0)
        cx, cy = tag.centroid
        layout = layout_text(
            "У-136",
            font="A",
            size_mm=6.0,
            x_mm=cx,
            y_mm=cy,
            align="center",
            valign="middle",
        )
        self.assertTrue(tag.bbox_inside(layout.bbox, safe=True))

    def test_default_settings_fit_tag(self) -> None:
        settings = Settings()
        tag = TagGeometry(settings.tag.side_mm, settings.tag.margin_mm)
        layout = layout_text(
            settings.text.text,
            font=settings.text.font,
            size_mm=settings.text.size_mm,
            x_mm=settings.text.x_mm,
            y_mm=settings.text.y_mm,
            align=settings.text.align,
            valign=settings.text.valign,
        )
        self.assertTrue(tag.bbox_inside(layout.bbox, safe=True))

    def test_wrap(self) -> None:
        layout = layout_text(
            "ПРИВЕТ МИР 2026",
            font="A",
            size_mm=8.0,
            x_mm=0.0,
            y_mm=0.0,
            align="left",
            max_width_mm=30.0,
        )
        self.assertGreater(layout.line_count, 1)

    def test_n_is_one_continuous_stroke(self) -> None:
        """«N» должна писаться одним проходом, без двух подъёмов пера."""
        for key in ("A", "B"):
            layout = layout_text("N", font=key, size_mm=6.3, x_mm=0.0, y_mm=0.0)
            self.assertEqual(len(layout.paths), 1, key)
            start = layout.paths[0][0]
            end = layout.paths[0][-1]
            # Концы должны быть на противоположных вертикалях: низ слева,
            # верх справа (или наоборот после разворота).
            self.assertGreater(abs(start[1] - end[1]), 0.8 * 6.3, key)

    def test_diacritics_are_not_glued_to_letter(self) -> None:
        """Короткий соединитель не должен приклеивать breve/точку к букве."""
        for key in ("A", "B"):
            breve = layout_text("Й", font=key, size_mm=6.3, x_mm=0.0, y_mm=0.0)
            self.assertEqual(len(breve.paths), 2, key)
            i_dot = layout_text("i", font=key, size_mm=6.3, x_mm=0.0, y_mm=0.0)
            self.assertEqual(len(i_dot.paths), 2, key)

    def test_gcode_n_has_single_pen_down(self) -> None:
        layout = layout_text("N", font="A", size_mm=6.3, x_mm=0.0, y_mm=0.0)
        job = generate_gcode(layout.paths, MachineConfig(), size_mm=6.3)
        self.assertEqual(job.stats.stroke_count, 1)
        self.assertEqual(job.text.count("G1 Z0"), 1)
        # начальный подъём, подъём после единственного штриха и финальный
        self.assertEqual(job.text.count("G0 Z5"), 3)


class GcodeTests(unittest.TestCase):
    def test_fmt_keeps_integer_zeros(self) -> None:
        self.assertEqual(fmt(1200.0, 0), "1200")
        self.assertEqual(fmt(300.0, 0), "300")
        self.assertEqual(fmt(0.0, 0), "0")
        self.assertEqual(fmt(1.5, 3), "1.5")

    def _job(self):
        layout = layout_text(
            "ЩР-1\n220 В",
            font="A",
            size_mm=8.0,
            x_mm=31.0,
            y_mm=22.0,
            align="center",
            valign="middle",
        )
        job = generate_gcode(
            layout.paths,
            MachineConfig(),
            font_name="ГОСТ тип А",
            size_mm=8.0,
        )
        return layout, job

    def test_gcode_structure(self) -> None:
        layout, job = self._job()
        self.assertIn("G21", job.text)
        self.assertIn("G90", job.text)
        self.assertIn("M2", job.text)
        self.assertIn("G1 Z0", job.text)
        self.assertGreater(job.stats.stroke_count, 0)
        self.assertGreater(job.stats.draw_length_mm, 0.0)
        self.assertNotIn("nan", job.text.lower())
        self.assertNotIn("inf", job.text.lower())

    def test_lines_fit_grbl_buffer(self) -> None:
        _, job = self._job()
        for line in job.text.splitlines():
            self.assertLess(len(line.encode("utf-8")), 120, line)

    def test_no_zero_length_strokes(self) -> None:
        _, job = self._job()
        for path in job.paths:
            self.assertGreaterEqual(len(path), 2)
            for i in range(len(path) - 1):
                self.assertNotEqual(path[i], path[i + 1])


class ExportTests(unittest.TestCase):
    def test_svg_contains_shapes(self) -> None:
        layout = layout_text(
            "ТЕСТ", font="B", size_mm=10.0, x_mm=20.0, y_mm=20.0, align="left"
        )
        job = generate_gcode(layout.paths, MachineConfig())
        svg = render_svg(
            layout,
            job,
            get_font("B"),
            PreviewConfig(),
            TagConfig(),
        )
        self.assertIn("<svg", svg)
        self.assertIn("<polyline", svg)
        self.assertIn("У-136", svg)


class GrblProtocolTests(unittest.TestCase):
    def test_parse_status(self) -> None:
        status = parse_status("<Idle|MPos:1.000,2.500,0.000|FS:0,0>")
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.state, "Idle")
        self.assertEqual(status.mpos, (1.0, 2.5, 0.0))
        self.assertTrue(status.is_idle)

        running = parse_status("<Run|WPos:10.0,20.0,5.0|FS:1200,0>")
        assert running is not None
        self.assertEqual(running.position, (10.0, 20.0, 5.0))
        self.assertAlmostEqual(running.feed, 1200.0)
        self.assertIsNone(parse_status("ok"))

    def test_stream_job_acknowledges_lines(self) -> None:
        client = _FakeGrbl(ack=True)
        progress: list[float] = []
        client.stream_job(
            ["; comment", "G21", "", "G90", "G0 X1 Y2"],
            progress=progress.append,
        )
        self.assertEqual(client.written, ["G21", "G90", "G0 X1 Y2"])
        self.assertEqual(progress[-1], 1.0)
        self.assertFalse(client._job_active)

    def test_stream_job_stop_raises(self) -> None:
        client = _FakeGrbl(ack=True)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(JobCancelled):
            client.stream_job(["G21", "G90"], stop_event=stop)


class SettingsTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        settings = Settings()
        settings.text.text = "ТЕСТ\n220 В"
        settings.text.font = "B"
        settings.text.size_mm = 7.5
        settings.machine.pen_up_z = 4.25
        settings.tag.side_mm = 55.0
        settings.preview.show_travel = False

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            save_settings(settings, path)
            loaded = load_settings(path)

        self.assertEqual(loaded.text.text, "ТЕСТ\n220 В")
        self.assertEqual(loaded.text.font, "B")
        self.assertAlmostEqual(loaded.text.size_mm, 7.5)
        self.assertAlmostEqual(loaded.machine.pen_up_z, 4.25)
        self.assertAlmostEqual(loaded.tag.side_mm, 55.0)
        self.assertFalse(loaded.preview.show_travel)

    def test_missing_file_returns_defaults(self) -> None:
        loaded = load_settings(Path(tempfile.gettempdir()) / "no_such_gost_settings.json")
        self.assertIsInstance(loaded, Settings)
        self.assertEqual(loaded.text.font, "A")


if __name__ == "__main__":
    unittest.main()
