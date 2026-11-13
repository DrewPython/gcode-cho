"""Настройки приложения и сохранение в JSON."""

from __future__ import annotations

import json
import types
import typing
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints


@dataclass
class TextConfig:
    text: str = "ЩР-1\n220 В"
    font: str = "A"                 # A / B
    size_mm: float = 7.5
    x_mm: float = 31.0
    y_mm: float = 18.0
    align: str = "center"           # left / center / right
    valign: str = "middle"          # bottom / middle / top
    letter_spacing_mm: float | None = None   # None = по ГОСТ
    line_spacing_mm: float | None = None     # None = по ГОСТ
    word_spacing_mm: float | None = None     # None = по ГОСТ
    wrap_width_mm: float | None = None       # None/0 = без переноса


@dataclass
class MachineConfig:
    port: str = ""
    baud: int = 115200
    pen_up_z: float = 5.0
    pen_down_z: float = 0.0
    z_feed: float = 300.0
    draw_feed: float = 1200.0
    travel_feed: float = 3000.0
    dwell_down_s: float = 0.15
    dwell_up_s: float = 0.05
    use_g0_travel: bool = True
    park_at_end: bool = True
    park_x: float = 0.0
    park_y: float = 0.0
    optimize_order: bool = True
    merge_touching: bool = True


@dataclass
class TagConfig:
    mode: str = "u136"              # none / u136
    side_mm: float = 62.0
    margin_mm: float = 2.0
    show_slots: bool = True


@dataclass
class PreviewConfig:
    show_grid: bool = True
    show_travel: bool = True
    show_tag: bool = True
    field_width_mm: float = 200.0
    field_height_mm: float = 100.0


@dataclass
class Settings:
    text: TextConfig = field(default_factory=TextConfig)
    machine: MachineConfig = field(default_factory=MachineConfig)
    tag: TagConfig = field(default_factory=TagConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    window_geometry: str = "1280x800"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _coerce(value: Any, target_type: Any) -> Any:
    """Мягко приводит JSON-значения к типу поля dataclass."""
    if value is None:
        return None
    origin = typing.get_origin(target_type)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(target_type) if a is not type(None)]
        if args:
            target_type = args[0]
    if target_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "да"}
        return bool(value)
    if target_type is int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    if target_type is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if target_type is str:
        return str(value)
    return value


def _from_dict(cls: type, data: dict[str, Any]):
    kwargs: dict[str, Any] = {}
    hints = get_type_hints(cls)
    for f in fields(cls):
        if f.name not in data:
            continue
        raw = data[f.name]
        target_type = hints.get(f.name, f.type)
        if is_dataclass(target_type):
            if isinstance(raw, dict):
                kwargs[f.name] = _from_dict(target_type, raw)  # type: ignore[arg-type]
            continue
        coerced = _coerce(raw, target_type)
        if coerced is not None or f.default is None:
            kwargs[f.name] = coerced
    return cls(**kwargs)


def default_settings_path() -> Path:
    return Path(__file__).resolve().parent.parent / "settings.json"


def load_settings(path: Path | str | None = None) -> Settings:
    p = Path(path) if path else default_settings_path()
    if not p.exists():
        return Settings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    return _from_dict(Settings, data)


def save_settings(settings: Settings, path: Path | str | None = None) -> Path:
    p = Path(path) if path else default_settings_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)
    except OSError:
        p.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return p
