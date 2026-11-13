"""Главное окно GOST Marker: текст, предпросмотр и управление GRBL."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .config import (
    MachineConfig,
    PreviewConfig,
    Settings,
    TagConfig,
    TextConfig,
    load_settings,
    save_settings,
)
from .fonts.gost import FONTS, StrokeFont, get_font, glyph_catalog
from .gcode import GcodeResult, fmt, generate_gcode
from .grbl import (
    GrblClient,
    JobCancelled,
    available_ports,
)
from .layout import LayoutResult, bbox_size, layout_text
from .preview_svg import save_svg
from .tag import TagGeometry

BG = "#eef2f7"
CARD = "#ffffff"
TEXT = "#0f172a"
MUTED = "#64748b"
BORDER = "#dbe3ee"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_SOFT = "#dbeafe"
OK = "#15803d"
OK_SOFT = "#dcfce7"
WARN = "#b45309"
WARN_SOFT = "#fef3c7"
DANGER = "#b91c1c"
DANGER_SOFT = "#fee2e2"
GRID_1MM = "#eaf0f8"
GRID_10MM = "#d9e2ef"
FIELD_BORDER = "#94a3b8"
STROKE = "#1d4ed8"

UI_FONT = ("Segoe UI", 10)
UI_FONT_SMALL = ("Segoe UI", 9)
UI_FONT_BOLD = ("Segoe UI Semibold", 10)
MONO_FONT = ("Consolas", 9)

ALIGN_LABELS = {"left": "Слева", "center": "По центру", "right": "Справа"}
ALIGN_KEYS = {v: k for k, v in ALIGN_LABELS.items()}
VALIGN_LABELS = {"bottom": "Снизу", "middle": "По центру", "top": "Сверху"}
VALIGN_KEYS = {v: k for k, v in VALIGN_LABELS.items()}
TAG_LABELS = {"none": "Без бирки", "u136": "Бирка У-136"}
TAG_KEYS = {v: k for k, v in TAG_LABELS.items()}


def _fnum(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return ""
    return fmt(float(value), decimals)


def _parse_float(text: str) -> float | None:
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# Прокручиваемая область
# ---------------------------------------------------------------------------


class ScrollableFrame(ttk.Frame):
    """Вертикально прокручиваемый контейнер для длинных вкладок."""

    def __init__(self, master: tk.Misc, *, bg: str = BG) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_inner_configure(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def scroll_units(self, units: int) -> None:
        self.canvas.yview_scroll(units, "units")


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings: Settings = load_settings()
        self._event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._client = GrblClient(
            on_log=lambda message: self._event_queue.put(("log", message)),
            on_status=lambda status: self._event_queue.put(("status", status)),
        )

        self._layout: LayoutResult | None = None
        self._job: GcodeResult | None = None
        self._job_thread: threading.Thread | None = None
        self._connect_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._refresh_after: str | None = None
        self._dragging_job = False
        self._closing = False
        self._scrollers: list[ScrollableFrame] = []

        # Предпросмотр
        self._auto_fit = True
        self._scale = 4.0
        self._ox = 0.0
        self._oy = 0.0
        self._pan: tuple[float, float, float, float] | None = None
        self._drag: tuple[float, float, float, float] | None = None
        self._drag_offset: tuple[float, float] | None = None
        self._pen: tuple[float, float, float] | None = None
        self._last_drag_draw = 0.0

        self._init_vars()
        self._build_style()

        self.root.title("GOST Marker — маркировка бирок У-136")
        self.root.configure(bg=BG)
        try:
            self.root.geometry(self.settings.window_geometry or "1280x800")
        except tk.TclError:
            self.root.geometry("1280x800")
        self.root.minsize(1080, 680)

        self._build_ui()
        self.root.bind_all("<MouseWheel>", self._on_global_wheel, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._pump_events)
        self.root.after(200, self.refresh)

    # ------------------------------------------------------------------
    # Переменные интерфейса
    # ------------------------------------------------------------------

    def _init_vars(self) -> None:
        s = self.settings
        t = s.text
        m = s.machine
        g = s.tag
        p = s.preview

        self.var_font = tk.StringVar(value=t.font)
        self.var_size = tk.StringVar(value=_fnum(t.size_mm))
        self.var_x = tk.StringVar(value=_fnum(t.x_mm))
        self.var_y = tk.StringVar(value=_fnum(t.y_mm))
        self.var_align = tk.StringVar(value=ALIGN_LABELS.get(t.align, "По центру"))
        self.var_valign = tk.StringVar(value=VALIGN_LABELS.get(t.valign, "По центру"))
        self.var_letter = tk.StringVar(value=_fnum(t.letter_spacing_mm))
        self.var_line = tk.StringVar(value=_fnum(t.line_spacing_mm))
        self.var_word = tk.StringVar(value=_fnum(t.word_spacing_mm))
        self.var_wrap = tk.StringVar(value=_fnum(t.wrap_width_mm))

        self.var_tag_mode = tk.StringVar(value=TAG_LABELS.get(g.mode, "Бирка У-136"))
        self.var_tag_side = tk.StringVar(value=_fnum(g.side_mm))
        self.var_tag_margin = tk.StringVar(value=_fnum(g.margin_mm))
        self.var_tag_slots = tk.BooleanVar(value=g.show_slots)

        self.var_show_grid = tk.BooleanVar(value=p.show_grid)
        self.var_show_travel = tk.BooleanVar(value=p.show_travel)
        self.var_show_tag = tk.BooleanVar(value=p.show_tag)

        self.var_port = tk.StringVar(value=m.port)
        self.var_baud = tk.StringVar(value=str(m.baud))
        self.var_pen_up = tk.StringVar(value=_fnum(m.pen_up_z))
        self.var_pen_down = tk.StringVar(value=_fnum(m.pen_down_z))
        self.var_z_feed = tk.StringVar(value=_fnum(m.z_feed, 0))
        self.var_draw_feed = tk.StringVar(value=_fnum(m.draw_feed, 0))
        self.var_travel_feed = tk.StringVar(value=_fnum(m.travel_feed, 0))
        self.var_dwell_down = tk.StringVar(value=_fnum(m.dwell_down_s, 2))
        self.var_dwell_up = tk.StringVar(value=_fnum(m.dwell_up_s, 2))
        self.var_use_g0 = tk.BooleanVar(value=m.use_g0_travel)
        self.var_park = tk.BooleanVar(value=m.park_at_end)
        self.var_park_x = tk.StringVar(value=_fnum(m.park_x))
        self.var_park_y = tk.StringVar(value=_fnum(m.park_y))
        self.var_optimize = tk.BooleanVar(value=m.optimize_order)
        self.var_merge = tk.BooleanVar(value=m.merge_touching)

        self.var_jog_step = tk.StringVar(value="1")
        self.var_jog_feed = tk.StringVar(value="1500")

        self.var_state = tk.StringVar(value="Не подключено")
        self.var_pos = tk.StringVar(value="X —   Y —   Z —")
        self.var_info = tk.StringVar(value="Нажмите «Обновить», чтобы построить предпросмотр")
        self.var_warn = tk.StringVar(value="")
        self.var_progress_text = tk.StringVar(value="")
        self.var_progress = tk.DoubleVar(value=0.0)
        self.var_ports_hint = tk.StringVar(value="")
        self.var_zoom = tk.StringVar(value="")

    # ------------------------------------------------------------------
    # Стили
    # ------------------------------------------------------------------

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", font=UI_FONT, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=UI_FONT_SMALL)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=UI_FONT_SMALL)
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=UI_FONT_BOLD)
        style.configure("H1.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 17))
        style.configure("H2.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Info.TLabel", background=BG, foreground=TEXT, font=UI_FONT_SMALL)
        style.configure("Warn.TLabel", background=BG, foreground=WARN, font=UI_FONT_SMALL)
        style.configure("Danger.TLabel", background=BG, foreground=DANGER, font=UI_FONT_SMALL)
        style.configure("Ok.TLabel", background=BG, foreground=OK, font=UI_FONT_SMALL)
        style.configure("Pos.TLabel", background=BG, foreground=TEXT, font=MONO_FONT)

        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background="#dde5f0",
            foreground=MUTED,
            padding=(16, 8),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", ACCENT_DARK)],
        )

        style.configure("TButton", padding=(10, 6), borderwidth=0, background="#e6ecf5", foreground=TEXT)
        style.map(
            "TButton",
            background=[("active", "#d7e1ef"), ("disabled", "#edf1f7")],
            foreground=[("disabled", "#9aa7b8")],
        )
        style.configure(
            "Accent.TButton",
            padding=(12, 7),
            background=ACCENT,
            foreground="#ffffff",
            font=UI_FONT_BOLD,
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#a9c2f3")],
            foreground=[("disabled", "#eef4ff")],
        )
        style.configure(
            "Danger.TButton",
            padding=(10, 6),
            background=DANGER_SOFT,
            foreground=DANGER,
        )
        style.map("Danger.TButton", background=[("active", "#fecaca"), ("disabled", "#f5f5f5")])
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT)
        style.configure("TRadiobutton", background=CARD, foreground=TEXT)
        style.configure("TEntry", fieldbackground="#ffffff", bordercolor=BORDER, padding=4)
        style.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=BORDER, padding=4)
        style.configure("TCombobox", fieldbackground="#ffffff", padding=4)
        style.configure("TProgressbar", background=ACCENT, troughcolor="#e2e8f0", borderwidth=0)
        style.configure("Vertical.TScrollbar", background="#cbd5e1", troughcolor=BG, borderwidth=0)
        style.configure("Treeview", background=CARD, fieldbackground=CARD, rowheight=24, borderwidth=0)

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_header()
        self._build_statusbar()

        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=0, minsize=486)
        container.columnconfigure(1, weight=1)

        left = ttk.Frame(container, width=486)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.pack_propagate(False)
        right = ttk.Frame(container)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        self._build_left(left)
        self._build_preview(right)

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=16, pady=(12, 8))

        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left")
        tk.Label(
            titles,
            text="GOST Marker",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w")
        tk.Label(
            titles,
            text="Одноштриховые шрифты ГОСТ тип А / Б · маркер по оси Z · GRBL 1.1h",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        actions = tk.Frame(header, bg=BG)
        actions.pack(side="right")
        ttk.Button(actions, text="Таблица глифов", command=self._open_glyph_window).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(actions, text="Сохранить настройки", command=self._save_settings_clicked).pack(
            side="left", padx=(0, 12)
        )
        self.lbl_badge = tk.Label(
            actions,
            text="  Не подключено  ",
            bg="#e2e8f0",
            fg="#475569",
            font=("Segoe UI Semibold", 9),
            padx=6,
            pady=5,
        )
        self.lbl_badge.pack(side="left")

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg="#e3eaf4")
        bar.pack(side="bottom", fill="x")
        inner = tk.Frame(bar, bg="#e3eaf4")
        inner.pack(fill="x", padx=16, pady=5)

        tk.Label(
            inner,
            text="Позиция:",
            bg="#e3eaf4",
            fg=MUTED,
            font=UI_FONT_SMALL,
        ).pack(side="left")
        tk.Label(
            inner,
            textvariable=self.var_pos,
            bg="#e3eaf4",
            fg=TEXT,
            font=MONO_FONT,
        ).pack(side="left", padx=(6, 20))

        tk.Label(
            inner,
            textvariable=self.var_state,
            bg="#e3eaf4",
            fg=MUTED,
            font=UI_FONT_SMALL,
        ).pack(side="left")

        tk.Label(
            inner,
            textvariable=self.var_zoom,
            bg="#e3eaf4",
            fg=MUTED,
            font=UI_FONT_SMALL,
        ).pack(side="right")

    # ------------------------------------------------------------------
    # Левая колонка: вкладки
    # ------------------------------------------------------------------

    def _build_left(self, parent: ttk.Frame) -> None:
        self._build_job_footer(parent)

        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True)

        self.tab_text = ttk.Frame(self.notebook)
        self.tab_machine = ttk.Frame(self.notebook)
        self.tab_log = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_text, text="Текст")
        self.notebook.add(self.tab_machine, text="Станок")
        self.notebook.add(self.tab_log, text="Журнал")

        self._build_text_tab(self.tab_text)
        self._build_machine_tab(self.tab_machine)
        self._build_log_tab(self.tab_log)

    # ---- вспомогательные элементы ----

    def _card(self, parent: tk.Misc, title: str, subtitle: str = "") -> tk.Frame:
        card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", padx=10, pady=(8, 0))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=12, pady=10)

        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=CARD, fg=TEXT, font=UI_FONT_BOLD).pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, bg=CARD, fg=MUTED, font=UI_FONT_SMALL).pack(
                side="left", padx=(8, 0)
            )

        body = tk.Frame(inner, bg=CARD)
        body.pack(fill="x", pady=(8, 0))
        return body

    def _entry(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        var: tk.Variable,
        *,
        unit: str = "",
        width: int = 10,
        hint: str = "",
    ) -> ttk.Entry:
        tk.Label(parent, text=label, bg=CARD, fg=TEXT, font=UI_FONT).grid(
            row=row, column=0, sticky="w", pady=3
        )
        entry = ttk.Entry(parent, textvariable=var, width=width)
        entry.grid(row=row, column=1, sticky="we", padx=(10, 4), pady=3)
        entry.bind("<KeyRelease>", self._on_any_change)
        entry.bind("<FocusOut>", self._on_any_change)
        if unit:
            tk.Label(parent, text=unit, bg=CARD, fg=MUTED, font=UI_FONT_SMALL).grid(
                row=row, column=2, sticky="w", pady=3
            )
            if hint:
                tk.Label(parent, text=hint, bg=CARD, fg=MUTED, font=UI_FONT_SMALL).grid(
                    row=row, column=3, sticky="w", padx=(8, 0), pady=3
                )
        return entry

    def _spin(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        var: tk.StringVar,
        *,
        from_: float,
        to: float,
        increment: float = 0.5,
        unit: str = "",
        width: int = 8,
    ) -> ttk.Spinbox:
        tk.Label(parent, text=label, bg=CARD, fg=TEXT, font=UI_FONT).grid(
            row=row, column=0, sticky="w", pady=3
        )
        spin = ttk.Spinbox(
            parent,
            textvariable=var,
            from_=from_,
            to=to,
            increment=increment,
            width=width,
            command=self._schedule_refresh,
        )
        spin.grid(row=row, column=1, sticky="w", padx=(10, 4), pady=3)
        spin.bind("<KeyRelease>", self._on_any_change)
        spin.bind("<FocusOut>", self._on_any_change)
        if unit:
            tk.Label(parent, text=unit, bg=CARD, fg=MUTED, font=UI_FONT_SMALL).grid(
                row=row, column=2, sticky="w", pady=3
            )
        return spin

    def _check(
        self,
        parent: tk.Misc,
        text: str,
        var: tk.BooleanVar,
        *,
        card: bool = True,
    ) -> ttk.Checkbutton:
        cb = ttk.Checkbutton(
            parent,
            text=text,
            variable=var,
            command=self._schedule_refresh,
            style="Card.TCheckbutton" if card else "TCheckbutton",
        )
        return cb

    def _on_any_change(self, _event: tk.Event | None = None) -> None:
        self._schedule_refresh()

    # ---- нижняя панель выполнения ----

    def _build_job_footer(self, parent: ttk.Frame) -> None:
        footer = tk.Frame(parent, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(8, 0))

        card = tk.Frame(footer, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=10, pady=8)

        self.btn_print = ttk.Button(
            inner,
            text="Печать на станке",
            style="Accent.TButton",
            command=self._start_job,
        )
        self.btn_print.pack(fill="x")

        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        self.btn_pause = ttk.Button(
            row, text="Пауза", command=self._toggle_pause, state="disabled", width=9
        )
        self.btn_pause.pack(side="left")
        self.btn_stop = ttk.Button(
            row,
            text="Стоп",
            style="Danger.TButton",
            command=self._stop_job,
            state="disabled",
            width=6,
        )
        self.btn_stop.pack(side="left", padx=(6, 8))

        self.progress = ttk.Progressbar(
            row, variable=self.var_progress, maximum=100.0
        )
        self.progress.pack(side="left", fill="x", expand=True)
        tk.Label(
            row,
            textvariable=self.var_progress_text,
            bg=CARD,
            fg=MUTED,
            font=MONO_FONT,
            width=6,
            anchor="e",
        ).pack(side="left", padx=(6, 0))

    # ---- вкладка «Текст» ----

    def _build_text_tab(self, parent: ttk.Frame) -> None:
        scroller = ScrollableFrame(parent, bg=BG)
        scroller.pack(fill="both", expand=True)
        self._scrollers.append(scroller)
        body = scroller.inner

        # Текст
        card = self._card(body, "Текст", "многострочный, Enter — новая строка")
        self.text_editor = tk.Text(
            card,
            height=4,
            wrap="word",
            font=("Segoe UI", 11),
            bg="#ffffff",
            fg=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            padx=8,
            pady=6,
            undo=True,
        )
        self.text_editor.pack(fill="x")
        self.text_editor.insert("1.0", self.settings.text.text)
        self.text_editor.edit_modified(False)
        self.text_editor.bind("<<Modified>>", self._on_text_modified)

        examples = tk.Frame(card, bg=CARD)
        examples.pack(fill="x", pady=(8, 0))
        for label, sample in (
            ("ЩР-1 / 220 В", "ЩР-1\n220 В"),
            ("ПРИВЕТ / МИР", "ПРИВЕТ\nМИР 2026"),
            ("У-136", "У-136"),
            ("Очистить", ""),
        ):
            ttk.Button(
                examples,
                text=label,
                command=lambda s=sample: self._set_sample_text(s),
            ).pack(side="left", padx=(0, 5))

        tk.Label(
            card,
            text="Символы, которых нет в шрифте, помечаются в предпросмотре красным.",
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
        ).pack(anchor="w", pady=(8, 0))

        # Шрифт
        card = self._card(body, "Шрифт", "одноштриховой, по мотивам ГОСТ 2.304-81")
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x")
        ttk.Radiobutton(
            row,
            text="Тип А · узкий",
            value="A",
            variable=self.var_font,
            command=self._on_font_changed,
            style="TRadiobutton",
        ).pack(side="left")
        ttk.Radiobutton(
            row,
            text="Тип Б · широкий",
            value="B",
            variable=self.var_font,
            command=self._on_font_changed,
            style="TRadiobutton",
        ).pack(side="left", padx=(18, 0))

        size_row = tk.Frame(card, bg=CARD)
        size_row.pack(fill="x", pady=(8, 0))
        tk.Label(size_row, text="Размер, мм", bg=CARD, fg=TEXT, font=UI_FONT).pack(side="left")
        size_spin = ttk.Spinbox(
            size_row,
            textvariable=self.var_size,
            from_=1,
            to=60,
            increment=0.5,
            width=8,
            command=self._schedule_refresh,
        )
        size_spin.pack(side="left", padx=(10, 8))
        size_spin.bind("<KeyRelease>", self._on_any_change)
        size_spin.bind("<FocusOut>", self._on_any_change)
        tk.Label(
            card,
            text="Размер — высота прописных букв. Толщина линии на G-код не влияет.",
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=400,
        ).pack(fill="x", pady=(4, 0))

        # Раскладка
        card = self._card(body, "Раскладка", "пустое поле = интервал по ГОСТ")
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="Выравнивание", bg=CARD, fg=TEXT, font=UI_FONT).grid(
            row=0, column=0, sticky="w", pady=3
        )
        cmb_align = ttk.Combobox(
            grid,
            textvariable=self.var_align,
            values=list(ALIGN_LABELS.values()),
            state="readonly",
            width=14,
        )
        cmb_align.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=3)
        cmb_align.bind("<<ComboboxSelected>>", self._on_any_change)

        tk.Label(grid, text="По вертикали", bg=CARD, fg=TEXT, font=UI_FONT).grid(
            row=1, column=0, sticky="w", pady=3
        )
        cmb_valign = ttk.Combobox(
            grid,
            textvariable=self.var_valign,
            values=list(VALIGN_LABELS.values()),
            state="readonly",
            width=14,
        )
        cmb_valign.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=3)
        cmb_valign.bind("<<ComboboxSelected>>", self._on_any_change)

        spacing = tk.Frame(card, bg=CARD)
        spacing.pack(fill="x", pady=(6, 0))
        for label, var in (
            ("Межбуквенный", self.var_letter),
            ("Межстрочный", self.var_line),
            ("Пробел", self.var_word),
            ("Ширина переноса", self.var_wrap),
        ):
            cell = tk.Frame(spacing, bg=CARD)
            cell.pack(side="left", padx=(0, 10))
            tk.Label(cell, text=label, bg=CARD, fg=TEXT, font=UI_FONT_SMALL).pack(anchor="w")
            row = tk.Frame(cell, bg=CARD)
            row.pack(anchor="w", pady=(2, 0))
            entry = ttk.Entry(row, textvariable=var, width=7)
            entry.pack(side="left")
            entry.bind("<KeyRelease>", self._on_any_change)
            entry.bind("<FocusOut>", self._on_any_change)
            tk.Label(row, text="мм", bg=CARD, fg=MUTED, font=UI_FONT_SMALL).pack(
                side="left", padx=(3, 0)
            )
        tk.Label(
            card,
            text="Перенос по ширине: 0 или пусто — без автоматического переноса.",
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
        ).pack(anchor="w", pady=(6, 0))

        # Положение
        card = self._card(body, "Положение", "координаты в мм от начала станка")
        pos = tk.Frame(card, bg=CARD)
        pos.pack(fill="x")
        pos.columnconfigure(1, weight=1)
        self._entry(pos, 0, "X", self.var_x, unit="мм")
        self._entry(pos, 1, "Y", self.var_y, unit="мм")
        pos_buttons = tk.Frame(card, bg=CARD)
        pos_buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(pos_buttons, text="Центр бирки", command=self._center_in_tag).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(pos_buttons, text="Вписать в бирку", command=self._fit_to_tag).pack(
            side="left"
        )

        # Бирка
        card = self._card(body, "Бирка", "треугольник, основание по X")
        tag_grid = tk.Frame(card, bg=CARD)
        tag_grid.pack(fill="x")
        tag_grid.columnconfigure(1, weight=1)

        tk.Label(tag_grid, text="Тип", bg=CARD, fg=TEXT, font=UI_FONT).grid(
            row=0, column=0, sticky="w", pady=3
        )
        cmb_tag = ttk.Combobox(
            tag_grid,
            textvariable=self.var_tag_mode,
            values=list(TAG_LABELS.values()),
            state="readonly",
            width=16,
        )
        cmb_tag.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=3)
        cmb_tag.bind("<<ComboboxSelected>>", self._on_tag_mode_changed)

        self._entry(tag_grid, 1, "Сторона", self.var_tag_side, unit="мм", width=8)
        self._entry(tag_grid, 2, "Отступ", self.var_tag_margin, unit="мм", width=8)
        self._check(tag_grid, "Показывать отверстия", self.var_tag_slots).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        tk.Label(
            card,
            text="Бирка У-136: равносторонний треугольник, вершина вверх. "
            "Начало координат — левый нижний угол бирки. Сторону уточните по своей партии.",
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
            wraplength=410,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        # Экспорт
        card = self._card(body, "Экспорт")
        buttons = tk.Frame(card, bg=CARD)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Сохранить G-code", command=self._export_gcode).pack(
            side="left"
        )
        ttk.Button(buttons, text="Экспорт SVG", command=self._export_svg).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="Обновить", command=self.refresh).pack(
            side="left", padx=(6, 0)
        )
        tk.Label(
            card,
            text="Печать и прогресс — на нижней панели слева, они всегда доступны.",
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=410,
        ).pack(fill="x", pady=(6, 0))
        tk.Frame(body, bg=BG, height=12).pack(fill="x")

    # ---- вкладка «Станок» ----

    def _build_machine_tab(self, parent: ttk.Frame) -> None:
        scroller = ScrollableFrame(parent, bg=BG)
        scroller.pack(fill="both", expand=True)
        self._scrollers.append(scroller)
        body = scroller.inner

        card = self._card(body, "Соединение", "GRBL 1.1h, 115200")
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="Порт", bg=CARD, fg=TEXT, font=UI_FONT).grid(
            row=0, column=0, sticky="w", pady=3
        )
        ports = available_ports()
        values = [device for device, _ in ports]
        if self.var_port.get() and self.var_port.get() not in values:
            values.insert(0, self.var_port.get())
        self.cmb_port = ttk.Combobox(
            grid, textvariable=self.var_port, values=values, width=18
        )
        self.cmb_port.grid(row=0, column=1, sticky="we", padx=(10, 4), pady=3)
        ttk.Button(grid, text="Обновить", command=self._refresh_ports).grid(
            row=0, column=2, sticky="e", pady=3
        )

        tk.Label(grid, text="Скорость", bg=CARD, fg=TEXT, font=UI_FONT).grid(
            row=1, column=0, sticky="w", pady=3
        )
        self.cmb_baud = ttk.Combobox(
            grid,
            textvariable=self.var_baud,
            values=["115200", "250000", "57600", "9600"],
            width=12,
        )
        self.cmb_baud.grid(row=1, column=1, sticky="w", padx=(10, 4), pady=3)

        self.btn_connect = ttk.Button(
            card, text="Подключиться", style="Accent.TButton", command=self._toggle_connection
        )
        self.btn_connect.pack(fill="x", pady=(8, 0))

        self.lbl_ports_hint = tk.Label(
            card,
            textvariable=self.var_ports_hint,
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
            justify="left",
            anchor="w",
            wraplength=410,
        )
        self.lbl_ports_hint.pack(fill="x", pady=(6, 0))
        self._update_ports_hint(ports)

        # Состояние
        card = self._card(body, "Состояние станка")
        self.lbl_machine_state = tk.Label(
            card,
            textvariable=self.var_state,
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI Semibold", 13),
            anchor="w",
        )
        self.lbl_machine_state.pack(fill="x")
        tk.Label(
            card,
            textvariable=self.var_pos,
            bg=CARD,
            fg=MUTED,
            font=MONO_FONT,
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        # Перо
        card = self._card(body, "Перо (ось Z)", "маркер Edding 140S")
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self._entry(grid, 0, "Z поднято", self.var_pen_up, unit="мм")
        self._entry(grid, 1, "Z опущено", self.var_pen_down, unit="мм")
        self._entry(grid, 2, "Подача Z", self.var_z_feed, unit="мм/мин")
        tk.Label(
            card,
            text="Подберите Z так, чтобы маркер касался бумаги без заметного нажима.",
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
            wraplength=410,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        # Подачи
        card = self._card(body, "Подачи", "мм/мин")
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self._entry(grid, 0, "Рисование", self.var_draw_feed, unit="мм/мин")
        self._entry(grid, 1, "Холостой ход", self.var_travel_feed, unit="мм/мин")
        self._entry(grid, 2, "Пауза внизу", self.var_dwell_down, unit="с")
        self._entry(grid, 3, "Пауза вверху", self.var_dwell_up, unit="с")

        # Поведение
        card = self._card(body, "Поведение программы")
        behavior = tk.Frame(card, bg=CARD)
        behavior.pack(fill="x")
        self._check(behavior, "Холостые ходы через G0", self.var_use_g0).pack(anchor="w")
        self._check(behavior, "Оптимизировать порядок штрихов", self.var_optimize).pack(
            anchor="w"
        )
        self._check(behavior, "Склеивать стыкующиеся штрихи", self.var_merge).pack(anchor="w")
        self._check(behavior, "Парковать в конце", self.var_park).pack(anchor="w")

        park = tk.Frame(card, bg=CARD)
        park.pack(fill="x", pady=(4, 0))
        tk.Label(park, text="Парковка X", bg=CARD, fg=TEXT, font=UI_FONT_SMALL).pack(side="left")
        e = ttk.Entry(park, textvariable=self.var_park_x, width=6)
        e.pack(side="left", padx=(6, 12))
        e.bind("<KeyRelease>", self._on_any_change)
        tk.Label(park, text="Y", bg=CARD, fg=TEXT, font=UI_FONT_SMALL).pack(side="left")
        e = ttk.Entry(park, textvariable=self.var_park_y, width=6)
        e.pack(side="left", padx=(6, 0))
        e.bind("<KeyRelease>", self._on_any_change)

        # Jog
        card = self._card(body, "Ручное управление", "Jog через $J")
        jog_top = tk.Frame(card, bg=CARD)
        jog_top.pack(fill="x")
        tk.Label(jog_top, text="Шаг, мм", bg=CARD, fg=TEXT, font=UI_FONT).pack(side="left")
        ttk.Combobox(
            jog_top,
            textvariable=self.var_jog_step,
            values=["0.1", "0.5", "1", "5", "10", "50"],
            width=6,
        ).pack(side="left", padx=(8, 14))
        tk.Label(jog_top, text="Подача", bg=CARD, fg=TEXT, font=UI_FONT).pack(side="left")
        ttk.Combobox(
            jog_top,
            textvariable=self.var_jog_feed,
            values=["500", "1000", "1500", "3000"],
            width=8,
        ).pack(side="left", padx=(8, 0))

        jog = tk.Frame(card, bg=CARD)
        jog.pack(pady=(10, 0))
        btn = dict(width=7)
        ttk.Button(jog, text="Y+", command=lambda: self._jog("Y", 1), **btn).grid(
            row=0, column=1, padx=3, pady=3
        )
        ttk.Button(jog, text="X−", command=lambda: self._jog("X", -1), **btn).grid(
            row=1, column=0, padx=3, pady=3
        )
        ttk.Button(jog, text="X+", command=lambda: self._jog("X", 1), **btn).grid(
            row=1, column=2, padx=3, pady=3
        )
        ttk.Button(jog, text="Y−", command=lambda: self._jog("Y", -1), **btn).grid(
            row=2, column=1, padx=3, pady=3
        )
        ttk.Button(jog, text="Z+", command=lambda: self._jog("Z", 1), **btn).grid(
            row=1, column=3, padx=(18, 3), pady=3
        )
        ttk.Button(jog, text="Z−", command=lambda: self._jog("Z", -1), **btn).grid(
            row=2, column=3, padx=(18, 3), pady=3
        )
        tk.Label(jog, text="Z", bg=CARD, fg=MUTED, font=UI_FONT_SMALL).grid(
            row=0, column=3, padx=(18, 3)
        )

        service = tk.Frame(card, bg=CARD)
        service.pack(fill="x", pady=(10, 0))
        ttk.Button(service, text="X/Y = 0", command=self._zero_xy).pack(side="left")
        ttk.Button(service, text="Домой", command=self._home).pack(side="left", padx=(6, 0))
        ttk.Button(service, text="Разблокировать", command=self._unlock).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(
            service, text="Сброс", style="Danger.TButton", command=self._soft_reset
        ).pack(side="left", padx=(6, 0))

        tk.Frame(body, bg=BG, height=12).pack(fill="x")

    # ---- вкладка «Журнал» ----

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        top = tk.Frame(frame, bg=BG)
        top.pack(fill="x", pady=(0, 6))
        ttk.Button(top, text="Очистить", command=self._clear_log).pack(side="left")
        ttk.Button(top, text="Сохранить журнал…", command=self._save_log).pack(
            side="left", padx=(6, 0)
        )
        self.var_log_count = tk.StringVar(value="0 строк")
        tk.Label(
            top,
            textvariable=self.var_log_count,
            bg=BG,
            fg=MUTED,
            font=UI_FONT_SMALL,
        ).pack(side="right")

        wrap = tk.Frame(frame, bg=BORDER, padx=1, pady=1)
        wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            wrap,
            wrap="none",
            font=MONO_FONT,
            bg="#0f172a",
            fg="#dbe7f7",
            insertbackground="#dbe7f7",
            relief="flat",
            padx=8,
            pady=6,
            state="disabled",
        )
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self._log_lines: list[str] = []
        self.log_text.tag_configure("cmd", foreground="#93c5fd")
        self.log_text.tag_configure("err", foreground="#fca5a5")
        self.log_text.tag_configure("ok", foreground="#86efac")
        self.log_text.tag_configure("time", foreground="#64748b")

    # ---- правая колонка: предпросмотр ----

    def _build_preview(self, parent: ttk.Frame) -> None:
        panel = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(fill="both", expand=True)

        toolbar = tk.Frame(panel, bg=CARD)
        toolbar.pack(fill="x", padx=12, pady=(10, 6))

        tk.Label(toolbar, text="Предпросмотр", bg=CARD, fg=TEXT, font=UI_FONT_BOLD).pack(
            side="left"
        )
        ttk.Checkbutton(
            toolbar,
            text="Сетка",
            variable=self.var_show_grid,
            command=self._on_preview_option,
            style="Card.TCheckbutton",
        ).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(
            toolbar,
            text="Холостые ходы",
            variable=self.var_show_travel,
            command=self._on_preview_option,
            style="Card.TCheckbutton",
        ).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            toolbar,
            text="Бирка",
            variable=self.var_show_tag,
            command=self._on_preview_option,
            style="Card.TCheckbutton",
        ).pack(side="left", padx=(8, 0))

        ttk.Button(toolbar, text="Вписать", command=self._fit_view).pack(side="right")
        tk.Label(
            toolbar,
            textvariable=self.var_zoom,
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
        ).pack(side="right", padx=(0, 10))

        self.canvas = tk.Canvas(
            panel,
            bg="#fbfdff",
            highlightthickness=0,
            bd=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<ButtonPress-2>", self._on_pan_press)
        self.canvas.bind("<B2-Motion>", self._on_pan_drag)
        self.canvas.bind("<ButtonPress-3>", self._on_pan_press)
        self.canvas.bind("<B3-Motion>", self._on_pan_drag)
        self.canvas.bind("<MouseWheel>", self._on_zoom)
        self.canvas.bind("<Motion>", self._on_canvas_motion)

        footer = tk.Frame(panel, bg=CARD)
        footer.pack(fill="x", padx=12, pady=(0, 10))
        self.lbl_preview_info = tk.Label(
            footer,
            textvariable=self.var_info,
            bg=CARD,
            fg=MUTED,
            font=UI_FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=700,
        )
        self.lbl_preview_info.pack(fill="x")

        self.lbl_preview_warn = tk.Label(
            footer,
            textvariable=self.var_warn,
            bg=CARD,
            fg=WARN,
            font=UI_FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=700,
        )
        self.lbl_preview_warn.pack(fill="x", pady=(3, 0))

        tk.Label(
            panel,
            text="ЛКМ по тексту — перетаскивание · ЛКМ по полю — панорама · "
            "колесо — масштаб · ПКМ/средняя — панорама",
            bg=CARD,
            fg="#94a3b8",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 8))

    # ------------------------------------------------------------------
    # Чтение значений из интерфейса
    # ------------------------------------------------------------------

    def _get_float(
        self,
        var: tk.Variable,
        default: float,
        lo: float | None = None,
        hi: float | None = None,
    ) -> float:
        value = _parse_float(str(var.get()))
        if value is None:
            return default
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        return value

    def _read_text_settings(self) -> TextConfig:
        t = self.settings.text
        if hasattr(self, "text_editor"):
            t.text = self.text_editor.get("1.0", "end-1c")
        font = self.var_font.get().upper()
        t.font = font if font in FONTS else "A"
        t.size_mm = self._get_float(self.var_size, t.size_mm, 1.0, 60.0)
        t.x_mm = self._get_float(self.var_x, t.x_mm, -100.0, 400.0)
        t.y_mm = self._get_float(self.var_y, t.y_mm, -100.0, 400.0)
        t.align = ALIGN_KEYS.get(self.var_align.get(), t.align)
        t.valign = VALIGN_KEYS.get(self.var_valign.get(), t.valign)

        letter = _parse_float(self.var_letter.get())
        t.letter_spacing_mm = max(0.0, letter) if letter is not None else None
        line = _parse_float(self.var_line.get())
        t.line_spacing_mm = line if line is not None and line > 0 else None
        word = _parse_float(self.var_word.get())
        t.word_spacing_mm = max(0.0, word) if word is not None else None
        wrap = _parse_float(self.var_wrap.get())
        t.wrap_width_mm = wrap if wrap is not None and wrap > 0 else None
        return t

    def _read_tag_settings(self) -> TagConfig:
        g = self.settings.tag
        g.mode = TAG_KEYS.get(self.var_tag_mode.get(), g.mode)
        g.side_mm = self._get_float(self.var_tag_side, g.side_mm, 10.0, 200.0)
        g.margin_mm = self._get_float(self.var_tag_margin, g.margin_mm, 0.0, 30.0)
        g.show_slots = bool(self.var_tag_slots.get())
        return g

    def _read_preview_settings(self) -> PreviewConfig:
        p = self.settings.preview
        p.show_grid = bool(self.var_show_grid.get())
        p.show_travel = bool(self.var_show_travel.get())
        p.show_tag = bool(self.var_show_tag.get())
        return p

    def _read_machine_settings(self) -> MachineConfig:
        m = self.settings.machine
        m.port = self.var_port.get().strip()
        m.baud = int(self._get_float(self.var_baud, m.baud, 1200, 1000000))
        m.pen_up_z = self._get_float(self.var_pen_up, m.pen_up_z, -50.0, 50.0)
        m.pen_down_z = self._get_float(self.var_pen_down, m.pen_down_z, -50.0, 50.0)
        m.z_feed = self._get_float(self.var_z_feed, m.z_feed, 10.0, 20000.0)
        m.draw_feed = self._get_float(self.var_draw_feed, m.draw_feed, 10.0, 20000.0)
        m.travel_feed = self._get_float(self.var_travel_feed, m.travel_feed, 10.0, 20000.0)
        m.dwell_down_s = self._get_float(self.var_dwell_down, m.dwell_down_s, 0.0, 10.0)
        m.dwell_up_s = self._get_float(self.var_dwell_up, m.dwell_up_s, 0.0, 10.0)
        m.use_g0_travel = bool(self.var_use_g0.get())
        m.park_at_end = bool(self.var_park.get())
        m.park_x = self._get_float(self.var_park_x, m.park_x, -100.0, 400.0)
        m.park_y = self._get_float(self.var_park_y, m.park_y, -100.0, 400.0)
        m.optimize_order = bool(self.var_optimize.get())
        m.merge_touching = bool(self.var_merge.get())
        return m

    def _read_all_settings(self) -> Settings:
        self._read_text_settings()
        self._read_machine_settings()
        self._read_tag_settings()
        self._read_preview_settings()
        return self.settings

    def _schedule_refresh(self, _event: tk.Event | None = None, delay: int = 220) -> None:
        if self._closing:
            return
        if self._refresh_after is not None:
            try:
                self.root.after_cancel(self._refresh_after)
            except tk.TclError:
                pass
        self._refresh_after = self.root.after(delay, self._do_scheduled_refresh)

    def _do_scheduled_refresh(self) -> None:
        self._refresh_after = None
        self.refresh()

    # ------------------------------------------------------------------
    # Действия с текстом и биркой
    # ------------------------------------------------------------------

    def _on_text_modified(self, _event: tk.Event) -> None:
        if self.text_editor.edit_modified():
            self.text_editor.edit_modified(False)
            self._schedule_refresh()

    def _set_sample_text(self, text: str) -> None:
        self.text_editor.delete("1.0", "end")
        if text:
            self.text_editor.insert("1.0", text)
        self.text_editor.edit_modified(False)
        self.refresh()

    def _on_font_changed(self) -> None:
        self.refresh()

    def _on_tag_mode_changed(self, _event: tk.Event | None = None) -> None:
        self.refresh()

    def _on_preview_option(self) -> None:
        self._read_preview_settings()
        self._draw_preview()

    def _tag_geometry(self) -> TagGeometry | None:
        g = self._read_tag_settings()
        if g.mode != "u136":
            return None
        return TagGeometry(g.side_mm, g.margin_mm)

    def _center_in_tag(self) -> None:
        self._read_text_settings()
        tag = self._tag_geometry()
        if tag is None:
            self.var_warn.set("Бирка отключена: выберите «Бирка У-136».")
            return
        cx, cy = tag.centroid
        self.var_x.set(_fnum(cx))
        self.var_y.set(_fnum(cy))
        self.var_align.set(ALIGN_LABELS["center"])
        self.var_valign.set(VALIGN_LABELS["middle"])
        self.refresh()

    def _fit_to_tag(self) -> None:
        t = self._read_text_settings()
        tag = self._tag_geometry()
        if tag is None:
            self.var_warn.set("Бирка отключена: выберите «Бирка У-136».")
            return
        if not t.text.strip():
            self.var_warn.set("Введите текст.")
            return

        cx, cy = tag.centroid
        self.var_x.set(_fnum(cx))
        self.var_y.set(_fnum(cy))
        self.var_align.set(ALIGN_LABELS["center"])
        self.var_valign.set(VALIGN_LABELS["middle"])

        font = get_font(t.font)
        lo, hi = 1.0, 48.0
        best = lo

        def fits(size: float) -> bool:
            lay = layout_text(
                t.text,
                font=font,
                size_mm=size,
                x_mm=cx,
                y_mm=cy,
                align="center",
                valign="middle",
                letter_spacing_mm=t.letter_spacing_mm,
                line_spacing_mm=t.line_spacing_mm,
                word_spacing_mm=t.word_spacing_mm,
                max_width_mm=t.wrap_width_mm,
            )
            return bool(lay.bbox) and tag.bbox_inside(lay.bbox, safe=True)

        for _ in range(22):
            mid = (lo + hi) / 2.0
            if fits(mid):
                best = mid
                lo = mid
            else:
                hi = mid

        # Округление вниз, чтобы после форматирования размера не выйти за зону.
        best = max(1.0, int(best * 10.0) / 10.0)
        while best > 1.0 and not fits(best):
            best = max(1.0, best - 0.1)
        self.var_size.set(_fnum(best, 1))
        self.refresh()

    # ------------------------------------------------------------------
    # Предпросмотр: преобразования и отрисовка
    # ------------------------------------------------------------------

    def _canvas_size(self) -> tuple[int, int]:
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def _w2c(self, x: float, y: float) -> tuple[float, float]:
        fh = self.settings.preview.field_height_mm
        return self._ox + x * self._scale, self._oy + (fh - y) * self._scale

    def _c2w(self, cx: float, cy: float) -> tuple[float, float]:
        fh = self.settings.preview.field_height_mm
        return (cx - self._ox) / self._scale, fh - (cy - self._oy) / self._scale

    def _fit_view(self) -> None:
        self._read_preview_settings()
        fw = self.settings.preview.field_width_mm
        fh = self.settings.preview.field_height_mm
        cw, ch = self._canvas_size()
        margin = 30
        scale = min((cw - margin * 2) / fw, (ch - margin * 2) / fh)
        self._scale = max(0.4, scale)
        self._ox = (cw - fw * self._scale) / 2.0
        self._oy = (ch - fh * self._scale) / 2.0
        self._auto_fit = True
        self._draw_preview()

    def _on_canvas_configure(self, _event: tk.Event) -> None:
        if self._auto_fit:
            self._fit_view()
        else:
            self._draw_preview()

    def _on_zoom(self, event: tk.Event) -> None:
        if event.delta == 0:
            return
        self._read_preview_settings()
        factor = 1.12 ** (event.delta / 120.0)
        new_scale = max(0.3, min(60.0, self._scale * factor))
        if abs(new_scale - self._scale) < 1e-9:
            return
        wx, wy = self._c2w(event.x, event.y)
        self._scale = new_scale
        fh = self.settings.preview.field_height_mm
        self._ox = event.x - wx * self._scale
        self._oy = event.y - (fh - wy) * self._scale
        self._auto_fit = False
        self._draw_preview()

    def _on_pan_press(self, event: tk.Event) -> None:
        self._pan = (event.x, event.y, self._ox, self._oy)
        self._auto_fit = False
        self.canvas.configure(cursor="fleur")

    def _on_pan_drag(self, event: tk.Event) -> None:
        if not self._pan:
            return
        x0, y0, ox, oy = self._pan
        self._ox = ox + (event.x - x0)
        self._oy = oy + (event.y - y0)
        self._draw_preview()

    def _on_canvas_motion(self, event: tk.Event) -> None:
        if not self._drag and not self._pan:
            if self._hit_text(*self._c2w(event.x, event.y)):
                self.canvas.configure(cursor="fleur")
            else:
                self.canvas.configure(cursor="crosshair")

    def _hit_text(self, wx: float, wy: float) -> bool:
        bbox = self._layout.bbox if self._layout else None
        if not bbox:
            return False
        pad = 6.0 / max(self._scale, 0.01)
        return (
            bbox[0] - pad <= wx <= bbox[2] + pad
            and bbox[1] - pad <= wy <= bbox[3] + pad
        )

    def _on_canvas_press(self, event: tk.Event) -> None:
        wx, wy = self._c2w(event.x, event.y)
        if self._hit_text(wx, wy):
            t = self._read_text_settings()
            self._drag = (event.x, event.y, t.x_mm, t.y_mm)
            self.canvas.configure(cursor="fleur")
            return
        self._pan = (event.x, event.y, self._ox, self._oy)
        self._auto_fit = False
        self.canvas.configure(cursor="fleur")

    def _on_canvas_drag(self, event: tk.Event) -> None:
        if self._pan:
            self._on_pan_drag(event)
            return
        if not self._drag:
            return
        x0, y0, tx, ty = self._drag
        dx = (event.x - x0) / max(self._scale, 0.01)
        dy = -(event.y - y0) / max(self._scale, 0.01)
        self.var_x.set(_fnum(tx + dx, 2))
        self.var_y.set(_fnum(ty + dy, 2))
        now = time.monotonic()
        if now - self._last_drag_draw >= 0.05:
            self._last_drag_draw = now
            self._drag_offset = (dx, dy)
            self._draw_preview()

    def _on_canvas_release(self, _event: tk.Event) -> None:
        was_drag = self._drag is not None
        self._drag = None
        self._pan = None
        self._drag_offset = None
        self.canvas.configure(cursor="crosshair")
        if was_drag:
            self.refresh()

    def _draw_preview(self) -> None:
        c = self.canvas
        c.delete("all")
        cw, ch = self._canvas_size()
        if cw < 20 or ch < 20:
            return

        p = self._read_preview_settings()
        fw = max(1.0, p.field_width_mm)
        fh = max(1.0, p.field_height_mm)
        self.var_zoom.set(f"{self._scale:.1f} px/мм · {fw:.0f}×{fh:.0f} мм")

        fx0, fy0 = self._w2c(0, fh)
        fx1, fy1 = self._w2c(fw, 0)

        # Поле
        c.create_rectangle(fx0, fy0, fx1, fy1, fill="#ffffff", outline="")

        if p.show_grid:
            step = 1 if self._scale >= 6.0 else 10
            if step == 1:
                for x in range(0, int(fw) + 1):
                    cx, _ = self._w2c(x, 0)
                    c.create_line(cx, fy0, cx, fy1, fill=GRID_1MM, width=1)
                for y in range(0, int(fh) + 1):
                    _, cy = self._w2c(0, y)
                    c.create_line(fx0, cy, fx1, cy, fill=GRID_1MM, width=1)
            for x in range(0, int(fw) + 1, 10):
                cx, _ = self._w2c(x, 0)
                c.create_line(cx, fy0, cx, fy1, fill=GRID_10MM, width=1)
            for y in range(0, int(fh) + 1, 10):
                _, cy = self._w2c(0, y)
                c.create_line(fx0, cy, fx1, cy, fill=GRID_10MM, width=1)

            if self._scale >= 1.4:
                for x in range(0, int(fw) + 1, 10):
                    cx, _ = self._w2c(x, 0)
                    c.create_text(
                        cx, fy1 + 9, text=str(x), fill="#94a3b8", font=("Segoe UI", 8)
                    )
                for y in range(0, int(fh) + 1, 10):
                    _, cy = self._w2c(0, y)
                    c.create_text(
                        fx0 - 12,
                        cy,
                        text=str(y),
                        fill="#94a3b8",
                        font=("Segoe UI", 8),
                        anchor="e",
                    )

        c.create_rectangle(
            fx0, fy0, fx1, fy1, outline=FIELD_BORDER, width=1.4, fill=""
        )

        # Бирка
        tag = self._tag_geometry() if p.show_tag else None
        if tag is not None:
            self._draw_tag(c, tag, bool(self.var_tag_slots.get()))
            cx, cy = tag.centroid
            tx, ty = self._w2c(cx, tag.height * 0.82)
            c.create_text(
                tx,
                ty,
                text=f"У-136 · {tag.side:.0f} мм",
                fill="#b45309",
                font=("Segoe UI", 9),
            )

        offset = getattr(self, "_drag_offset", None) or (0.0, 0.0)
        dx, dy = offset

        # Холостые ходы
        if p.show_travel and self._job:
            for a, b in self._job.travels:
                ax, ay = self._w2c(a[0] + dx, a[1] + dy)
                bx, by = self._w2c(b[0] + dx, b[1] + dy)
                c.create_line(ax, ay, bx, by, fill="#cbd5e1", width=1, dash=(3, 3))

        # Штрихи
        if self._layout:
            font = get_font(self.settings.text.font)
            width_px = max(1.1, font.line_width * self.settings.text.size_mm * self._scale)
            width_px = min(width_px, 5.0)
            for path in self._layout.paths:
                if len(path) < 2:
                    continue
                coords: list[float] = []
                for x, y in path:
                    px, py = self._w2c(x + dx, y + dy)
                    coords.extend((px, py))
                c.create_line(
                    coords,
                    fill=STROKE,
                    width=width_px,
                    capstyle="round",
                    joinstyle="round",
                )

            # Габариты
            if self._layout.bbox:
                x0, y0, x1, y1 = self._layout.bbox
                bx0, by0 = self._w2c(x0 + dx, y1 + dy)
                bx1, by1 = self._w2c(x1 + dx, y0 + dy)
                c.create_rectangle(
                    bx0, by0, bx1, by1, outline="#10b981", dash=(4, 3), width=1.0
                )
                w_mm = x1 - x0
                h_mm = y1 - y0
                c.create_text(
                    bx0,
                    by0 - 7,
                    text=f"{w_mm:.1f} × {h_mm:.1f} мм",
                    fill="#059669",
                    font=("Segoe UI", 8),
                    anchor="w",
                )

        # Начало координат
        ox, oy = self._w2c(0, 0)
        c.create_line(ox, oy, ox + 34, oy, fill="#ef4444", width=1.2)
        c.create_line(ox, oy, ox, oy - 34, fill="#ef4444", width=1.2)
        c.create_text(ox + 40, oy, text="X", fill="#ef4444", font=("Segoe UI", 9))
        c.create_text(ox, oy - 40, text="Y", fill="#ef4444", font=("Segoe UI", 9))

        # Позиция маркера
        if self._pen:
            px, py = self._w2c(self._pen[0], self._pen[1])
            c.create_oval(px - 5, py - 5, px + 5, py + 5, outline="#0ea5e9", width=1.6)
            c.create_line(px - 9, py, px + 9, py, fill="#0ea5e9")
            c.create_line(px, py - 9, px, py + 9, fill="#0ea5e9")

        if self._layout and self._layout.missing:
            c.create_text(
                12,
                12,
                text="Нет в шрифте: " + " ".join(sorted(self._layout.missing)),
                fill=DANGER,
                font=("Segoe UI", 9),
                anchor="nw",
            )

    def _draw_tag(self, c: tk.Canvas, tag: TagGeometry, show_slots: bool) -> None:
        def poly(points):
            flat: list[float] = []
            for x, y in points:
                px, py = self._w2c(x, y)
                flat.extend((px, py))
            return flat

        c.create_polygon(
            poly(tag.vertices),
            fill="#fff7ed",
            outline="#f59e0b",
            width=1.8,
            joinstyle="round",
        )
        c.create_polygon(
            poly(tag.safe_vertices),
            fill="",
            outline="#fbbf24",
            dash=(4, 3),
            width=1.0,
        )
        if show_slots:
            for slot in tag.slots():
                c.create_polygon(
                    poly(slot), fill="#ffffff", outline="#f59e0b", width=1.0
                )

    # ------------------------------------------------------------------
    # Генерация и информация
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        if self._closing:
            return
        self._refresh_after = None
        try:
            s = self._read_all_settings()
            font = get_font(s.text.font)
            layout = layout_text(
                s.text.text,
                font=font,
                size_mm=s.text.size_mm,
                x_mm=s.text.x_mm,
                y_mm=s.text.y_mm,
                align=s.text.align,
                valign=s.text.valign,
                letter_spacing_mm=s.text.letter_spacing_mm,
                line_spacing_mm=s.text.line_spacing_mm,
                word_spacing_mm=s.text.word_spacing_mm,
                max_width_mm=s.text.wrap_width_mm,
            )
            job = generate_gcode(
                layout.paths,
                s.machine,
                start_xy=(0.0, 0.0),
                font_name=font.name,
                size_mm=s.text.size_mm,
            )
            self._layout = layout
            self._job = job
            self._drag_offset = None
            self._update_info(font, layout, job)
            self._draw_preview()
        except Exception as exc:  # noqa: BLE001 - показываем пользователю
            self._layout = None
            self._job = None
            self.var_info.set("Не удалось построить предпросмотр")
            self.var_warn.set(str(exc))
            self.lbl_preview_warn.configure(fg=DANGER)
            self._draw_preview()
            self._log(f"Ошибка предпросмотра: {exc}")

    def _update_info(self, font: StrokeFont, layout: LayoutResult, job: GcodeResult) -> None:
        stats = job.stats
        w_mm, h_mm = bbox_size(layout.bbox)
        k = font.key
        self.var_info.set(
            f"Тип {k} · {layout.line_count} стр. · {stats.stroke_count} штрихов · "
            f"контур {fmt(stats.draw_length_mm)} мм · холостые {fmt(stats.travel_length_mm)} мм\n"
            f"габарит текста {w_mm:.1f} × {h_mm:.1f} мм · оценка {_fmt_duration(stats.estimated_seconds)} · "
            f"G-code: {len(job.text.splitlines())} строк"
        )

        issues: list[str] = []
        severe = False
        if not self.settings.text.text.strip():
            issues.append("Введите текст.")
            severe = True
        if layout.missing:
            issues.append("Нет в шрифте: " + " ".join(sorted(layout.missing)))
            severe = True
        if stats.stroke_count == 0:
            issues.append("Нет штрихов для рисования.")
            severe = True

        if layout.bbox:
            x0, y0, x1, y1 = layout.bbox
            fw = self.settings.preview.field_width_mm
            fh = self.settings.preview.field_height_mm
            if x0 < -1e-6 or y0 < -1e-6 or x1 > fw + 1e-6 or y1 > fh + 1e-6:
                issues.append(
                    f"Текст выходит за поле {fw:.0f}×{fh:.0f} мм: "
                    f"X {x0:.1f}…{x1:.1f}, Y {y0:.1f}…{y1:.1f}."
                )
                severe = True

            tag = self._tag_geometry()
            if tag is not None and not tag.bbox_inside(layout.bbox, safe=True):
                issues.append(
                    "Габарит текста выходит за безопасную зону бирки У-136. "
                    "Уменьшите размер или нажмите «Вписать в бирку»."
                )

        if issues:
            self.var_warn.set("  ".join(issues))
            self.lbl_preview_warn.configure(fg=DANGER if severe else WARN)
        else:
            self.var_warn.set("")
            self.lbl_preview_warn.configure(fg=MUTED)

    def _current_font(self) -> StrokeFont:
        self._read_text_settings()
        return get_font(self.settings.text.font)

    # ------------------------------------------------------------------
    # Экспорт
    # ------------------------------------------------------------------

    def _export_gcode(self) -> None:
        self.refresh()
        if not self._job:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить G-code",
            defaultextension=".nc",
            initialfile="gost_marking.nc",
            filetypes=[
                ("G-code", "*.nc *.gcode *.tap"),
                ("Текстовые файлы", "*.txt"),
                ("Все файлы", "*.*"),
            ],
        )
        if not path:
            return
        try:
            Path(path).write_text(self._job.text, encoding="utf-8", newline="\n")
        except OSError as exc:
            messagebox.showerror("Ошибка сохранения", str(exc), parent=self.root)
            return
        self._log(f"G-code сохранён: {path}")
        messagebox.showinfo(
            "G-code сохранён",
            f"Файл:\n{path}\n\nСтрок: {len(self._job.text.splitlines())}",
            parent=self.root,
        )

    def _export_svg(self) -> None:
        self.refresh()
        if not self._layout:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Экспорт предпросмотра в SVG",
            defaultextension=".svg",
            initialfile="gost_preview.svg",
            filetypes=[("SVG", "*.svg"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            save_svg(
                path,
                self._layout,
                self._job,
                self._current_font(),
                self.settings.preview,
                self.settings.tag,
                title="GOST Marker — предпросмотр",
                size_mm=self.settings.text.size_mm,
            )
        except OSError as exc:
            messagebox.showerror("Ошибка экспорта", str(exc), parent=self.root)
            return
        self._log(f"SVG сохранён: {path}")

    # ------------------------------------------------------------------
    # Журнал
    # ------------------------------------------------------------------

    def _log(self, message: str, tag: str = "") -> None:
        if not hasattr(self, "log_text"):
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp}  {message}"
        self._log_lines.append(line)
        if len(self._log_lines) > 2000:
            drop = len(self._log_lines) - 1500
            self._log_lines = self._log_lines[drop:]
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", f"{drop + 1}.0")
            self.log_text.configure(state="disabled")

        if not tag:
            low = message.lower()
            if "error" in low or "ошибка" in low or "alarm" in low or "не удалось" in low:
                tag = "err"
            elif message.startswith(("→", "←")):
                tag = "cmd"
            elif "завершен" in low or "подключено" in low or "ok" == low.strip():
                tag = "ok"

        self.log_text.configure(state="normal")
        self.log_text.insert("end", stamp + "  ", ("time",))
        if tag:
            self.log_text.insert("end", message + "\n", (tag,))
        else:
            self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.var_log_count.set(f"{len(self._log_lines)} строк")

    def _clear_log(self) -> None:
        self._log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.var_log_count.set("0 строк")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить журнал",
            defaultextension=".log",
            initialfile=f"gost_marker_{datetime.now():%Y%m%d_%H%M}.log",
            filetypes=[("Журнал", "*.log"), ("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text("\n".join(self._log_lines) + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Ошибка сохранения", str(exc), parent=self.root)

    # ------------------------------------------------------------------
    # Станок: подключение и ручные команды
    # ------------------------------------------------------------------

    def _update_ports_hint(self, ports: list[tuple[str, str]]) -> None:
        if not ports:
            self.var_ports_hint.set(
                "Порты не найдены. Подключите плату по USB и нажмите «Обновить»."
            )
            return
        text = "; ".join(f"{device} — {desc}" for device, desc in ports)
        self.var_ports_hint.set("Найдено: " + text)

    def _refresh_ports(self) -> None:
        ports = available_ports()
        values = [device for device, _ in ports]
        current = self.var_port.get().strip()
        if current and current not in values:
            values.insert(0, current)
        self.cmb_port.configure(values=values)
        self._update_ports_hint(ports)
        self._log(f"Поиск портов: найдено {len(ports)}")

    def _require_connection(self) -> bool:
        if self._client.connected:
            return True
        messagebox.showwarning(
            "Нет соединения",
            "Сначала подключитесь к плате на вкладке «Станок».",
            parent=self.root,
        )
        return False

    def _set_connected(self, connected: bool) -> None:
        if connected:
            self.lbl_badge.configure(text="  Подключено  ", bg=OK_SOFT, fg=OK)
            self.btn_connect.configure(text="Отключиться", state="normal")
            self.var_state.set("Idle")
            self.lbl_machine_state.configure(fg=OK)
        else:
            self.lbl_badge.configure(text="  Не подключено  ", bg="#e2e8f0", fg="#475569")
            self.btn_connect.configure(text="Подключиться", state="normal")
            self.var_state.set("Не подключено")
            self.var_pos.set("X —   Y —   Z —")
            self.lbl_machine_state.configure(fg=MUTED)
            self._pen = None
            self._draw_preview()

    def _toggle_connection(self) -> None:
        if self._client.connected:
            if self._job_thread and self._job_thread.is_alive():
                messagebox.showwarning(
                    "Идёт выполнение",
                    "Дождитесь окончания задания или нажмите «Стоп».",
                    parent=self.root,
                )
                return
            self._client.close()
            self._set_connected(False)
            self._log("Соединение закрыто")
            return
        if self._connect_thread and self._connect_thread.is_alive():
            return
        port = self.var_port.get().strip()
        if not port:
            messagebox.showwarning(
                "Не выбран порт",
                "Выберите COM-порт платы (кнопка «Обновить» обновляет список).",
                parent=self.root,
            )
            return
        baud = int(self._get_float(self.var_baud, 115200, 1200, 1000000))
        self.btn_connect.configure(state="disabled", text="Подключение…")
        self.lbl_badge.configure(text="  Подключение…  ", bg=WARN_SOFT, fg=WARN)
        self._log(f"Подключение к {port} @ {baud}…")

        def work() -> None:
            try:
                self._client.connect(port, baud)
                self._event_queue.put(("connected", (port, baud)))
            except Exception as exc:  # noqa: BLE001
                self._event_queue.put(("connect_error", str(exc)))

        self._connect_thread = threading.Thread(
            target=work, name="grbl-connect", daemon=True
        )
        self._connect_thread.start()

    def _run_machine(self, action: Callable[[], None], label: str) -> None:
        if not self._require_connection():
            return

        def work() -> None:
            try:
                action()
                self._event_queue.put(("log", f"{label}: выполнено"))
            except Exception as exc:  # noqa: BLE001
                self._event_queue.put(("machine_error", f"{label}: {exc}"))

        threading.Thread(target=work, name="grbl-command", daemon=True).start()

    def _jog(self, axis: str, sign: int) -> None:
        step = self._get_float(self.var_jog_step, 1.0, 0.01, 500.0)
        feed = self._get_float(self.var_jog_feed, 1500.0, 10.0, 20000.0)
        self._run_machine(
            lambda: self._client.jog(axis, sign * step, feed),
            f"Jog {axis}{'+' if sign > 0 else '−'}{fmt(step)}",
        )

    def _zero_xy(self) -> None:
        self._run_machine(
            lambda: self._client.set_work_zero(True, True, False), "X/Y = 0"
        )

    def _home(self) -> None:
        self._run_machine(self._client.home, "Поиск нуля ($H)")

    def _unlock(self) -> None:
        self._run_machine(self._client.unlock, "Разблокировка ($X)")

    def _soft_reset(self) -> None:
        if not self._client.connected:
            self._set_connected(False)
            return
        try:
            self._client.soft_reset()
            self._log("Сброс контроллера")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Сброс не выполнен: {exc}", "err")

    # ------------------------------------------------------------------
    # Выполнение задания
    # ------------------------------------------------------------------

    def _set_job_ui(self, active: bool) -> None:
        self.btn_print.configure(state="disabled" if active else "normal")
        self.btn_pause.configure(state="normal" if active else "disabled")
        self.btn_stop.configure(state="normal" if active else "disabled")
        if not active:
            self.btn_pause.configure(text="Пауза")

    def _preflight(self) -> bool:
        self.refresh()
        if not self._job or not self._layout:
            messagebox.showwarning(
                "Нет программы", "Не удалось построить G-code.", parent=self.root
            )
            return False
        if self._job.stats.stroke_count == 0:
            messagebox.showwarning(
                "Нет штрихов", "В тексте нет символов для рисования.", parent=self.root
            )
            return False

        messages: list[str] = []
        bbox = self._layout.bbox
        if bbox:
            fw = self.settings.preview.field_width_mm
            fh = self.settings.preview.field_height_mm
            if bbox[0] < 0 or bbox[1] < 0 or bbox[2] > fw or bbox[3] > fh:
                messages.append(f"Текст выходит за поле {fw:.0f}×{fh:.0f} мм.")
            tag = self._tag_geometry()
            if tag is not None and not tag.bbox_inside(bbox, safe=True):
                messages.append("Текст выходит за безопасную зону бирки У-136.")
        if messages:
            text = "\n".join(messages) + "\n\nВсё равно отправить программу на станок?"
            return messagebox.askyesno("Проверка расположения", text, parent=self.root)
        return True

    def _start_job(self) -> None:
        if self._job_thread and self._job_thread.is_alive():
            return
        if not self._require_connection():
            return
        if not self._preflight():
            return

        self._stop_event.clear()
        self._pause_event.clear()
        self.var_progress.set(0.0)
        self.var_progress_text.set("0 %")
        self._set_job_ui(True)
        self._log("Отправка программы на станок…")
        lines = self._job.text.splitlines()

        def work() -> None:
            try:
                self._client.stream_job(
                    lines,
                    progress=lambda p: self._event_queue.put(("progress", p)),
                    stop_event=self._stop_event,
                    pause_event=self._pause_event,
                )
                self._event_queue.put(("job_done", None))
            except JobCancelled as exc:
                self._event_queue.put(("job_cancelled", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self._event_queue.put(("job_error", str(exc)))

        self._job_thread = threading.Thread(target=work, name="grbl-job", daemon=True)
        self._job_thread.start()

    def _toggle_pause(self) -> None:
        if not self._job_thread or not self._job_thread.is_alive():
            return
        if self._pause_event.is_set():
            self._pause_event.clear()
            self.btn_pause.configure(text="Пауза")
            self._log("Продолжение")
        else:
            self._pause_event.set()
            self.btn_pause.configure(text="Продолжить")
            self._log("Пауза")

    def _stop_job(self) -> None:
        if not self._job_thread or not self._job_thread.is_alive():
            return
        self._stop_event.set()
        self._log("Остановка задания…")

    # ------------------------------------------------------------------
    # Очередь событий из потоков
    # ------------------------------------------------------------------

    def _pump_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self._event_queue.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(80, self._pump_events)

    def _is_within(self, widget: tk.Misc | None, ancestor: tk.Misc) -> bool:
        current: tk.Misc | None = widget
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, "master", None)
        return False

    def _on_global_wheel(self, event: tk.Event) -> None:
        """Прокрутка длинных вкладок; поля и предпросмотр обрабатывают колесо сами."""
        widget = event.widget
        if isinstance(widget, (tk.Text, ttk.Combobox, ttk.Spinbox, tk.Spinbox)):
            return
        if self._is_within(widget, self.canvas):
            return
        if not event.delta:
            return
        for scroller in self._scrollers:
            if self._is_within(widget, scroller.canvas) or self._is_within(
                widget, scroller.inner
            ):
                scroller.scroll_units(int(-event.delta / 120))
                return

    def _handle_event(self, kind: str, payload: object) -> None:
        if kind == "log":
            self._log(str(payload))
        elif kind == "status":
            self._handle_status(payload)
        elif kind == "connected":
            port, baud = payload  # type: ignore[misc]
            self._set_connected(True)
            self._log(f"Подключено: {port} @ {baud}")
        elif kind == "connect_error":
            self._set_connected(False)
            self._log(f"Ошибка подключения: {payload}", "err")
            messagebox.showerror("Не удалось подключиться", str(payload), parent=self.root)
        elif kind == "progress":
            value = float(payload) * 100.0  # type: ignore[arg-type]
            self.var_progress.set(value)
            self.var_progress_text.set(f"{value:.0f} %")
        elif kind == "job_done":
            self.var_progress.set(100.0)
            self.var_progress_text.set("100 %")
            self._set_job_ui(False)
            self._log("Задание завершено")
        elif kind == "job_cancelled":
            self._set_job_ui(False)
            self.var_progress_text.set("Стоп")
            self._log("Задание остановлено", "err")
        elif kind == "job_error":
            self._set_job_ui(False)
            self.var_progress_text.set("Ошибка")
            self._log(f"Ошибка задания: {payload}", "err")
            messagebox.showerror("Ошибка выполнения", str(payload), parent=self.root)
        elif kind == "machine_error":
            self._log(str(payload), "err")
            messagebox.showerror("Ошибка станка", str(payload), parent=self.root)

    def _handle_status(self, status: object) -> None:
        state = getattr(status, "state", "Unknown")
        position = getattr(status, "position", None)
        feed = getattr(status, "feed", 0.0)
        if position:
            self._pen = (position[0], position[1], position[2])
            self.var_pos.set(
                f"X {position[0]:8.2f}   Y {position[1]:8.2f}   Z {position[2]:7.2f}"
            )
            self._draw_preview()
        suffix = f" · F {feed:.0f}" if feed else ""
        self.var_state.set(f"{state}{suffix}")
        low = state.lower()
        if low.startswith("alarm"):
            color = DANGER
        elif low.startswith(("hold", "door", "soft")):
            color = WARN
        elif low.startswith(("run", "jog")):
            color = ACCENT
        elif low.startswith("idle"):
            color = OK
        else:
            color = MUTED
        self.lbl_machine_state.configure(fg=color)

    # ------------------------------------------------------------------
    # Настройки и завершение
    # ------------------------------------------------------------------

    def _save_settings(self) -> bool:
        self._read_all_settings()
        try:
            self.settings.window_geometry = self.root.geometry()
        except tk.TclError:
            pass
        try:
            save_settings(self.settings)
        except OSError as exc:
            self._log(f"Не удалось сохранить настройки: {exc}", "err")
            messagebox.showerror("Настройки", str(exc), parent=self.root)
            return False
        return True

    def _save_settings_clicked(self) -> None:
        if self._save_settings():
            self._log("Настройки сохранены в settings.json")
            messagebox.showinfo(
                "Настройки",
                "Настройки сохранены в settings.json рядом с программой.",
                parent=self.root,
            )

    def _on_close(self) -> None:
        if self._job_thread and self._job_thread.is_alive():
            if not messagebox.askyesno(
                "Идёт выполнение",
                "Задание ещё выполняется. Остановить станок и закрыть программу?",
                parent=self.root,
            ):
                return
            self._stop_event.set()
            self._job_thread.join(timeout=2.5)

        self._save_settings()
        self._closing = True
        try:
            self._client.close()
        except Exception:
            pass
        self.root.destroy()

    # ------------------------------------------------------------------
    # Таблица глифов
    # ------------------------------------------------------------------

    def _open_glyph_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Таблица глифов ГОСТ")
        win.geometry("1000x680")
        win.configure(bg=CARD)
        win.transient(self.root)

        top = tk.Frame(win, bg=CARD)
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(
            top, text="Глифы шрифта", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 13)
        ).pack(side="left")
        var_key = tk.StringVar(value=self.settings.text.font)
        info = tk.Label(
            top, text="", bg=CARD, fg=MUTED, font=UI_FONT_SMALL
        )
        info.pack(side="left", padx=(12, 0))

        wrap = tk.Frame(win, bg=BORDER, padx=1, pady=1)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        canvas = tk.Canvas(wrap, bg="#ffffff", highlightthickness=0)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def draw(_event: tk.Event | None = None) -> None:
            font = get_font(var_key.get())
            info.configure(
                text=f"{font.name} · x-scale {font.x_scale:.2f} · "
                f"d/h {font.line_width:.3f} · глифов {len(font.glyphs)}"
            )
            canvas.delete("all")
            width = max(400, canvas.winfo_width())
            cell_w, cell_h = 66, 78
            cols = max(4, int((width - 20) // cell_w))
            items = glyph_catalog(font)
            rows = (len(items) + cols - 1) // cols
            canvas.configure(scrollregion=(0, 0, cols * cell_w + 20, rows * cell_h + 20))
            scale = 34.0
            for index, (char, glyph) in enumerate(items):
                col = index % cols
                row = index // cols
                x0 = 20 + col * cell_w
                y0 = 20 + row * cell_h
                baseline = y0 + 50
                for stroke in glyph.strokes:
                    coords: list[float] = []
                    for gx, gy in stroke:
                        coords.extend(
                            (
                                x0 + 8 + gx * font.x_scale * scale,
                                baseline - gy * scale,
                            )
                        )
                    if len(coords) >= 4:
                        canvas.create_line(
                            coords,
                            fill=STROKE,
                            width=1.5,
                            capstyle="round",
                            joinstyle="round",
                        )
                canvas.create_text(
                    x0 + cell_w / 2,
                    y0 + cell_h - 10,
                    text=char,
                    fill=MUTED,
                    font=("Segoe UI", 10),
                )

        def choose(key: str) -> None:
            var_key.set(key)
            draw()

        for key, label in (("A", "Тип А · узкий"), ("B", "Тип Б · широкий")):
            ttk.Radiobutton(
                top,
                text=label,
                value=key,
                variable=var_key,
                command=lambda k=key: choose(k),
                style="TRadiobutton",
            ).pack(side="left", padx=(12, 0))

        canvas.bind("<Configure>", draw)
        win.after(60, draw)




