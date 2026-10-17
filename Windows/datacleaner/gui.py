"""CustomTkinter front end."""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

from .detect import FORMATS
from . import __version__, APP_NAME, APP_BYLINE
from .pipeline import run

# ================================================================ settings

# (key, label, default) for the options under the Settings button
OPTIONS = [
    ("recursive", "Include subfolders", False),
    ("merge", "Merge files (one Excel workbook for everything)", True),
    ("opju", "Also save Origin project (.opju)", False),
    ("contour", "  + EEM contour graphs (experimental)", False),
    ("pptx", "Export graphs to PowerPoint (.pptx)\n  (graphs can't be opened in Origin from PowerPoint yet)", False),
    ("interpolate", "Interpolate across different wavelength grids", False),
    ("average", "Average to integer wavelengths", False),
]


def _settings_path() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Omnes" / "settings.json"


def _load_settings() -> dict:
    s = {k: d for k, _, d in OPTIONS}
    s["format"] = "Auto-detect"
    try:
        s.update(json.loads(_settings_path().read_text()))
    except Exception:
        pass
    return s


def _save_settings(s: dict) -> None:
    try:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s, indent=1))
    except Exception:
        pass


# ================================================================ GUI

def main() -> None:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox

    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("dark-blue")
    root = ctk.CTk()
    root.title(f"{APP_NAME} v{__version__}")
    root.geometry("600x620")
    root.minsize(500, 520)
    try:  # works both from source and from a PyInstaller build
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        ico, png = base / "assets" / "datacleaner.ico", base / "assets" / "datacleaner.png"
        if sys.platform.startswith("win") and ico.exists():
            root.iconbitmap(str(ico))
        elif png.exists():
            import tkinter as tk
            root._icon = tk.PhotoImage(file=str(png))
            root.iconphoto(True, root._icon)
    except Exception:
        pass

    font = "Segoe UI"
    settings = _load_settings()
    labels = {f.label: k for k, f in FORMATS.items()}
    if settings.get("format") not in labels:
        settings["format"] = "Auto-detect"
    state: dict = {"folder": None, "files": None}

    frame = ctk.CTkFrame(root)
    frame.pack(fill="both", expand=True, padx=20, pady=20)

    # ---- header: name, byline, settings
    head = ctk.CTkFrame(frame, fg_color="transparent")
    head.pack(fill="x", padx=10, pady=(12, 0))
    ctk.CTkButton(head, text="⚙ Settings", width=100, font=(font, 13),
                  command=lambda: open_settings()).pack(side="right", anchor="n")
    title = ctk.CTkFrame(head, fg_color="transparent")
    title.pack(side="left", expand=True)
    ctk.CTkLabel(title, text=APP_NAME, font=(f"{font} Bold", 28)).pack()
    ctk.CTkLabel(title, text=APP_BYLINE, font=(font, 13), text_color="gray").pack()

    # ---- input choice
    pick = ctk.CTkFrame(frame, fg_color="transparent")
    pick.pack(pady=(16, 4))

    def chosen(text: str):
        source_label.configure(text=text)
        run_btn.configure(state="normal")

    def pick_folder():
        d = filedialog.askdirectory(title="Select a folder of data files")
        if d:
            state["folder"], state["files"] = Path(d), None
            chosen(d)

    def pick_files():
        fs = filedialog.askopenfilenames(title="Select data files")
        if fs:
            state["folder"], state["files"] = None, [Path(f) for f in fs]
            names = [Path(f).name for f in fs]
            shown = ", ".join(names[:4]) + (f" … (+{len(names) - 4} more)" if len(names) > 4 else "")
            chosen(f"{len(fs)} file(s) in {Path(fs[0]).parent}:\n{shown}")

    ctk.CTkButton(pick, text="Choose Folder…", command=pick_folder, width=150,
                  font=(font, 14)).pack(side="left", padx=6)
    ctk.CTkButton(pick, text="Choose Files…", command=pick_files, width=150,
                  font=(font, 14)).pack(side="left", padx=6)
    source_label = ctk.CTkLabel(frame, text="Nothing selected", font=(font, 12), wraplength=500)
    source_label.pack(pady=(2, 4))
    summary = ctk.CTkLabel(frame, text="", font=(font, 11), text_color="gray", wraplength=500)
    summary.pack(pady=(0, 4))

    def show_summary():
        on = [lbl.split("\n")[0].strip(" +") for k, lbl, _ in OPTIONS if settings.get(k)]
        fmt = "" if settings["format"] == "Auto-detect" else f"Format: {settings['format']} · "
        summary.configure(text=fmt + (" · ".join(on) if on else "No options on"))

    # ---- settings window
    def open_settings():
        win = ctk.CTkToplevel(root)
        win.title("Settings")
        win.geometry("440x430")
        win.resizable(False, False)
        win.transient(root)
        win.after(50, win.grab_set)
        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=16)
        ctk.CTkLabel(body, text="File format", font=(f"{font} Bold", 13)).pack(anchor="w")
        fmt_var = ctk.StringVar(value=settings["format"])
        ctk.CTkOptionMenu(body, variable=fmt_var, values=["Auto-detect", *labels],
                          font=(font, 13), width=300).pack(anchor="w", pady=(4, 12))
        ctk.CTkLabel(body, text="Options", font=(f"{font} Bold", 13)).pack(anchor="w")
        vars_ = {}
        for key, text, _ in OPTIONS:
            vars_[key] = ctk.BooleanVar(value=bool(settings.get(key)))
            ctk.CTkCheckBox(body, text=text, variable=vars_[key], font=(font, 13)).pack(anchor="w", pady=3)

        def done():
            settings.update({k: v.get() for k, v in vars_.items()})
            settings["format"] = fmt_var.get()
            _save_settings(settings)
            show_summary()
            win.destroy()

        ctk.CTkButton(body, text="Done", command=done, font=(f"{font} Bold", 13)).pack(pady=(14, 0))
        win.protocol("WM_DELETE_WINDOW", done)

    logbox = ctk.CTkTextbox(frame, height=220, font=("Consolas", 11))

    def log(msg: str):
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        root.update_idletasks()

    def choose_merged(default: Path) -> Optional[Path]:
        p = filedialog.asksaveasfilename(
            title="Save merged file as", initialdir=str(default.parent),
            initialfile=default.name, defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")])
        return Path(p) if p else None

    def go():
        logbox.delete("1.0", "end")
        run_btn.configure(state="disabled")
        s = settings
        try:
            written = run(state["folder"], s["merge"], s["interpolate"], labels.get(s["format"]), log,
                          s["average"], s["recursive"], choose_merged, s["opju"], s["contour"],
                          s["pptx"], files=state["files"])
            if written:
                where = written[0] if len(written) == 1 else written[0].parent
                messagebox.showinfo(APP_NAME, f"Wrote {len(written)} file(s) to:\n{where}")
        except Exception as e:
            log(traceback.format_exc())
            messagebox.showerror(APP_NAME, str(e))
        finally:
            run_btn.configure(state="normal")

    run_btn = ctk.CTkButton(frame, text="Run", command=go, state="disabled",
                            font=(f"{font} Bold", 14))
    run_btn.pack(pady=8)
    logbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    show_summary()
    root.mainloop()
