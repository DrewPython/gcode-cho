"""Точка входа приложения GOST Marker."""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
import traceback
from tkinter import messagebox

from gost_marker.ui import App


def enable_dpi_awareness() -> None:
    """Убирает размытие интерфейса на мониторах с масштабированием."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> int:
    enable_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Не удалось создать окно Tkinter: {exc}")
        return 1

    try:
        App(root)
    except Exception:
        traceback.print_exc()
        messagebox.showerror(
            "GOST Marker",
            "Не удалось запустить приложение.\n\n" + traceback.format_exc(limit=3),
        )
        root.destroy()
        return 1

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
