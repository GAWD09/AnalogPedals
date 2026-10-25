"""AnalogPedals UI - Tkinter GUI for the AULA WIN68 HE analog-keys mapper.

    python pedals_ui.py

Left   - every gamepad control: click Bind, press the physical key to full
         travel, release it and it binds. x unbinds. A key shown in red is
         bound to more than one control. Search filters the list.
Right  - live trigger cylinders, stick radars, response-curve plots with
         editable gamma / dead zone / saturation, axis-invert checkboxes.
Top    - profile (presets.json) dropdown, export, SAVE CONFIG (pedals.ini).
"""
import json
import math
import threading
import time
import traceback
import zlib
from collections import Counter
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox, filedialog

import analogpedals as A
from analogpedals import (CONTROLS, STICK_AXES, DEFAULTS, MappingEngine,
                          TravelReader, KeyNameLearner, KeyBlocker,
                          load_keynames, load_presets, save_presets,
                          apply_preset, snapshot, travel_frac, shape,
                          prime_pad)

# ---- palette (sampled from the reference design) ----------------------
BG = "#070b13"
CARD = "#0c111b"
PANEL2 = "#0a0f18"
BORDER = "#1a2231"
BORDER2 = "#263250"
FG = "#ffffff"
DIM = "#8591a5"
DIM2 = "#566176"
ACCENT = "#00e5ff"
BLUE = "#2f7bff"
GREEN = "#3ddc97"
RED = "#ff5c6c"
ORANGE = "#ffb020"
CYAN_BG = "#0b2a33"
F_UI = "Segoe UI"
F_MONO = "Consolas"

BIND_PEAK = 25          # 0.25 mm: a key must travel this far to be picked
REST = 5                # 0.05 mm: counts as released
RAW_RANGE = 400         # curve plots span 0..4.0 mm

BADGE = {
    "RT": "RT", "LT": "LT",
    "LSX_L": "L\u2190", "LSX_R": "L\u2192", "LSY_U": "L\u2191",
    "LSY_D": "L\u2193", "RSX_L": "R\u2190", "RSX_R": "R\u2192",
    "RSY_U": "R\u2191", "RSY_D": "R\u2193",
    "BTN_A": "A", "BTN_B": "B", "BTN_X": "X", "BTN_Y": "Y",
    "LB": "LB", "RB": "RB", "BACK": "BK", "START": "ST",
}


# ----------------------------------------------------------------------
# Drawing helpers
# ----------------------------------------------------------------------
def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lerp(c1, c2, t):
    a, b = _rgb(c1), _rgb(c2)
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t)
                                   for i in range(3))


def rrect(c, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def grad_rrect(c, x1, y1, x2, y2, r, c1, c2, horizontal=True, tags=None):
    """Gradient-filled rounded rectangle, drawn as 1px lines."""
    r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))

    def inset(d):
        return 0 if d >= r else r - math.sqrt(max(0.0, r * r - (r - d) ** 2))

    if horizontal:
        span = max(1, x2 - x1)
        for x in range(int(x1), int(x2) + 1):
            ins = inset(min(x - x1, x2 - x))
            if y2 - ins > y1 + ins:
                c.create_line(x, y1 + ins, x, y2 - ins, tags=tags,
                              fill=lerp(c1, c2, (x - x1) / span))
    else:
        span = max(1, y2 - y1)
        for y in range(int(y1), int(y2) + 1):
            ins = inset(min(y - y1, y2 - y))
            if x2 - ins > x1 + ins:
                c.create_line(x1 + ins, y, x2 - ins, y, tags=tags,
                              fill=lerp(c1, c2, (y - y1) / span))


class Card(tk.Canvas):
    """Rounded, bordered container. Put children in `.inner`."""

    def __init__(self, master, bg=BG, fill=CARD, outline=BORDER, r=14,
                 pad=16, width=10, height=10):
        super().__init__(master, bg=bg, highlightthickness=0, width=width,
                         height=height)
        self.fill, self.outline, self.r, self.pad = fill, outline, r, pad
        self.inner = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad, window=self.inner,
                                       anchor="nw")
        self.bind("<Configure>", self._on_conf)

    def _on_conf(self, e):
        self.delete("bg")
        rrect(self, 1, 1, e.width - 2, e.height - 2, self.r, fill=self.fill,
              outline=self.outline, tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(1, e.width - 2 * self.pad),
                           height=max(1, e.height - 2 * self.pad))


class RoundBtn(tk.Canvas):
    """Rounded button / pill (static pill when command is None)."""

    def __init__(self, master, text="", command=None, width=80, height=30,
                 r=8, fill=PANEL2, outline=BORDER, fg=FG, hover=None,
                 font=(F_UI, 9, "bold"), bg=CARD, grad=None):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0)
        self.w, self.h, self.r = width, height, r
        self.text, self.command = text, command
        self.fill, self.outline, self.fg = fill, outline, fg
        self.hover, self.font, self.grad = hover, font, grad
        self._hov = False
        if command:
            self.configure(cursor="hand2")
            self.bind("<Button-1>", lambda e: self.command())
        if command or hover:
            self.bind("<Enter>", lambda e: self._set_hover(True))
            self.bind("<Leave>", lambda e: self._set_hover(False))
        self.redraw()

    def _set_hover(self, on):
        self._hov = on
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.w, self.h
        if self.grad:
            c1, c2 = self.grad
            if self._hov:
                c1, c2 = lerp(c1, "#ffffff", 0.12), lerp(c2, "#ffffff", 0.12)
            grad_rrect(self, 0, 0, w - 1, h - 1, self.r, c1, c2)
        else:
            f = self.hover if (self._hov and self.hover) else self.fill
            rrect(self, 1, 1, w - 2, h - 2, self.r, fill=f,
                  outline=self.outline)
        self.create_text(w / 2, h / 2, text=self.text, fill=self.fg,
                         font=self.font)

    def config_btn(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.redraw()


class Logo(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, width=42, height=42, bg=BG,
                         highlightthickness=0)
        grad_rrect(self, 0, 0, 41, 41, 11, "#19d3ff", "#2f6bff")
        rrect(self, 9, 15, 33, 28, 6, fill="#0a1424", outline="")
        self.create_line(14, 21, 20, 21, fill=ACCENT, width=2)
        self.create_line(17, 18, 17, 24, fill=ACCENT, width=2)
        self.create_oval(24, 18, 27, 21, fill=ACCENT, outline="")
        self.create_oval(27, 22, 30, 25, fill=ACCENT, outline="")


class IconBox(tk.Canvas):
    def __init__(self, master, glyph, bg=CARD, size=36):
        super().__init__(master, width=size, height=size, bg=bg,
                         highlightthickness=0)
        rrect(self, 1, 1, size - 2, size - 2, 9, fill=CYAN_BG,
              outline="#12485a")
        self.create_text(size / 2, size / 2, text=glyph, fill=ACCENT,
                         font=(F_UI, 13))


# ----------------------------------------------------------------------
# Live visuals (each redraws only when its values change)
# ----------------------------------------------------------------------
class TriggerPanel(tk.Canvas):
    def __init__(self, master, name, sub, color, grad):
        super().__init__(master, bg=CARD, highlightthickness=0, width=120,
                         height=200)
        self.name, self.sub, self.color, self.grad = name, sub, color, grad
        self._key = None
        self._vals = (0.0, 0.0)
        self.bind("<Configure>", lambda e: self.redraw(force=True))

    def set(self, out, mm):
        self._vals = (out, mm)
        self.redraw()

    def redraw(self, force=False):
        out, mm = self._vals
        key = (round(out, 3), round(mm, 2), self.winfo_width(),
               self.winfo_height())
        if key == self._key and not force:
            return
        self._key = key
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40 or h < 120:
            return
        rrect(self, 1, 1, w - 2, h - 2, 12, fill=PANEL2, outline=BORDER)
        self.create_text(14, 16, text=self.name, fill=self.color, anchor="w",
                         font=(F_MONO, 9, "bold"))
        self.create_text(14, 32, text=self.sub, fill=self.color, anchor="nw",
                         font=(F_MONO, 8, "bold"))
        self.create_text(w - 14, 16, text=f"{out * 100:.1f}%", anchor="e",
                         fill=ACCENT if out > 0.005 else DIM,
                         font=(F_MONO, 9, "bold"))
        tw = 56
        x0, x1 = (w - tw) / 2, (w + tw) / 2
        y0, y1 = 56, h - 44
        rrect(self, x0, y0, x1, y1, 12, fill="#090d15", outline="#27334b")
        for f in (0.25, 0.5, 0.75):
            y = y1 - (y1 - y0) * f
            self.create_line(x0 + 12, y, x1 - 12, y, fill="#232e44")
        self.create_line(x0 + 4, y0 + 10, x1 - 4, y0 + 10, fill="#0d8aa0",
                         dash=(3, 2))
        self.create_line(x0 + 4, y1 - 10, x1 - 4, y1 - 10, fill="#a8404f",
                         dash=(3, 2))
        fh = (y1 - y0 - 10) * out
        if fh > 2:
            grad_rrect(self, x0 + 6, y1 - 5 - fh, x1 - 6, y1 - 5,
                       min(9, fh / 2), self.grad[0], self.grad[1],
                       horizontal=False)
        self.create_text(w / 2, h - 18, text=f"Travel: {mm:.2f} mm",
                         fill=FG, font=(F_MONO, 8, "bold"))


class RadarPanel(tk.Canvas):
    def __init__(self, master, name, dot):
        super().__init__(master, bg=CARD, highlightthickness=0, width=170,
                         height=200)
        self.name, self.dot = name, dot
        self._key = None
        self._xy = (0.0, 0.0)
        self._dead = 0.1
        self.bind("<Configure>", lambda e: self.redraw(force=True))

    def set(self, x, y, dead=0.1):
        self._xy, self._dead = (x, y), dead
        self.redraw()

    def redraw(self, force=False):
        x, y = self._xy
        key = (round(x, 3), round(y, 3), round(self._dead, 3),
               self.winfo_width(), self.winfo_height())
        if key == self._key and not force:
            return
        self._key = key
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 120:
            return
        rrect(self, 1, 1, w - 2, h - 2, 12, fill=PANEL2, outline=BORDER)
        self.create_text(14, 14, text=self.name, fill=FG, anchor="nw",
                         font=(F_UI, 9, "bold"))
        mag = min(1.0, math.hypot(x, y))
        live = mag > 0.01
        self.create_text(w - 12, 14, anchor="ne", justify="right",
                         text=f"X: {x:+.2f} | Y: {y:+.2f}",
                         fill=ACCENT if live else DIM2,
                         font=(F_MONO, 8, "bold"))
        cx, cy = w / 2, 74 + (h - 74 - 36) / 2
        R = max(20, min(w / 2 - 22, (h - 74 - 36) / 2))
        for k, col in ((1.0, "#1b6f8c"), (0.66, "#133c52"),
                       (0.33, "#133c52")):
            self.create_oval(cx - R * k, cy - R * k, cx + R * k, cy + R * k,
                             outline=col)
        self.create_line(cx - R, cy, cx + R, cy, fill="#16324a")
        self.create_line(cx, cy - R, cx, cy + R, fill="#16324a")
        d = R * self._dead
        self.create_oval(cx - d, cy - d, cx + d, cy + d, outline="#8a3a48",
                         dash=(2, 2))
        px, py = cx + x * R, cy - y * R
        self.create_oval(px - 10, py - 10, px + 10, py + 10,
                         fill=lerp(PANEL2, self.dot, 0.25), outline="")
        self.create_oval(px - 5, py - 5, px + 5, py + 5, fill=self.dot,
                         outline="#ffffff" if live else "")
        self.create_text(14, h - 16, anchor="w", fill=FG,
                         text=f"Magnitude: {mag * 100:.0f}%",
                         font=(F_MONO, 8, "bold"))
        self.create_text(w - 12, h - 16, anchor="e",
                         text="Normal" if live else "Idle",
                         fill=GREEN if live else DIM2,
                         font=(F_MONO, 8, "bold"))


class CurvePlot(tk.Canvas):
    def __init__(self, master, ylabel, xlabel, c1, c2):
        super().__init__(master, bg=CARD, highlightthickness=0, width=300,
                         height=160)
        self.ylabel, self.xlabel, self.c1, self.c2 = ylabel, xlabel, c1, c2
        self._key = None
        self._args = (360, 8, 1.0, 0)
        self.bind("<Configure>", lambda e: self.redraw(force=True))

    def set(self, full, dead, gamma, cur):
        self._args = (full, dead, gamma, cur)
        self.redraw()

    def redraw(self, force=False):
        full, dead, gamma, cur = self._args
        key = (full, dead, round(gamma, 3), cur, self.winfo_width(),
               self.winfo_height())
        if key == self._key and not force:
            return
        self._key = key
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 60:
            return
        rrect(self, 1, 1, w - 2, h - 2, 10, fill=PANEL2, outline=BORDER)
        px0, py0, px1, py1 = 10, 12, w - 10, h - 12

        def X(raw):
            return px0 + min(raw, RAW_RANGE) / RAW_RANGE * (px1 - px0)

        def Y(v):
            return py1 - v * (py1 - py0)

        xd, xf = X(dead), X(full)
        self.create_rectangle(px0, py0, xd, py1, fill="#1c1018", outline="")
        self.create_rectangle(xf, py0, px1, py1, fill="#08242b", outline="")
        for i in range(1, 4):
            self.create_line(px0, Y(i / 4), px1, Y(i / 4), fill="#111a28")
            self.create_line(X(RAW_RANGE * i / 4), py0, X(RAW_RANGE * i / 4),
                             py1, fill="#111a28")
        self.create_line(xd, py0, xd, py1, fill="#a8404f", dash=(3, 3))
        self.create_line(xf, py0, xf, py1, fill="#0d8aa0", dash=(3, 3))
        self.create_line(X(dead), py1, X(full), Y(1.0), fill="#27334b",
                         dash=(3, 3))
        n = 60
        prev = None
        for i in range(n + 1):
            raw = i * RAW_RANGE / n
            pt = (X(raw), Y(shape(travel_frac(raw, full, dead), gamma)))
            if prev:
                self.create_line(*prev, *pt, fill=lerp(self.c1, self.c2,
                                                       i / n), width=2)
            prev = pt
        cv = shape(travel_frac(cur, full, dead), gamma)
        cx, cy = X(cur), Y(cv)
        self.create_line(cx, cy, cx, py1, fill="#1b7f95", dash=(1, 3))
        self.create_line(px0, cy, cx, cy, fill="#1b7f95", dash=(1, 3))
        self.create_oval(cx - 6, cy - 6, cx + 6, cy + 6,
                         fill=lerp(PANEL2, ACCENT, 0.3), outline="")
        self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=FG,
                         outline=ACCENT, width=2)
        self.create_text(px0 + 6, py0 + 4, text=self.ylabel, anchor="nw",
                         fill=DIM2, font=(F_MONO, 7))
        self.create_text(px1 - 6, py1 - 4, text=self.xlabel, anchor="se",
                         fill=DIM2, font=(F_MONO, 7))


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------
class AnalogPedalsUI:
    def __init__(self, root):
        self.root = root
        self.engine = MappingEngine().load()
        self.presets = load_presets()
        self.reader = TravelReader()
        self.reader.start()

        self.builtin_names = {46: "W", 69: "S", 68: "A", 70: "D"}
        self.learner = KeyNameLearner(self.reader, load_keynames())
        self.blocker = KeyBlocker()

        self.pad = None
        self._pad_err = None
        self._loop_err = None
        self._state = {}
        self._worker_run = True
        self._await = None
        self._await_idx = None
        self._row = {}
        self._spins = {}
        self._invert_vars = {}
        self._chk_t = 0.0
        self._profile_name = tk.StringVar(value="Current config")

        root.title("ANALOGPEDALS")
        root.configure(bg=BG)
        root.geometry("1440x940+20+20")
        root.minsize(1240, 800)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)
        self._style()

        self._make_pad()
        self._build_header()
        self._build_body()
        self._build_status_bar()
        self._rebuild_profile_menu()
        self._sync_widgets()

        if self.engine.params.get("auto_learn_names", 1):
            self.learner.start()
        self._apply_block()

        self._worker = threading.Thread(target=self._pad_loop, daemon=True)
        self._worker.start()
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(33, self._tick)

        if self.pad is None:
            messagebox.showwarning(
                "Virtual controller unavailable",
                "Could not create the virtual Xbox 360 pad:\n\n"
                f"{self._pad_err}\n\nInstall ViGEmBus and reboot. You can "
                "still bind keys and watch the live readout.")

    # ------------------------------------------------------------------
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Vertical.TScrollbar", background="#1a2231",
                     troughcolor=CARD, bordercolor=CARD, arrowcolor=DIM,
                     relief="flat")

    def _make_pad(self):
        try:
            import vgamepad as vg
            self.pad = vg.VX360Gamepad()
            prime_pad(self.pad)
        except Exception as e:
            self.pad = None
            self._pad_err = str(e)

    def _pad_loop(self):
        """Mapping + pad writes run off the UI thread at ~500 Hz."""
        while self._worker_run:
            try:
                st = self.engine.tick(self.reader)
                self._state = st
                if self.pad is not None:
                    self.engine.apply_to_pad(self.pad, st)
            except Exception:
                self._loop_err = traceback.format_exc()
                traceback.print_exc()
                time.sleep(0.1)
            time.sleep(0.002)

    # ------------------------------------------------------------------
    # header
    # ------------------------------------------------------------------
    def _build_header(self):
        top = tk.Frame(self.root, bg=BG)
        top.grid(row=0, column=0, sticky="we")
        hdr = tk.Frame(top, bg=BG)
        hdr.pack(fill="x", padx=24, pady=(16, 14))
        Logo(hdr).pack(side="left")

        tb = tk.Frame(hdr, bg=BG)
        tb.pack(side="left", padx=(14, 0))
        row = tk.Frame(tb, bg=BG)
        row.pack(anchor="w")
        tk.Label(row, text="ANALOG", bg=BG, fg=FG,
                 font=(F_UI, 17, "bold")).pack(side="left")
        tk.Label(row, text="PEDALS", bg=BG, fg=ACCENT,
                 font=(F_UI, 17, "bold")).pack(side="left")
        RoundBtn(row, text="PRO V2.4", width=66, height=20, r=5,
                 fill="#082a33", outline="#0b4d5c", fg=ACCENT,
                 font=(F_MONO, 8, "bold"), bg=BG).pack(side="left",
                                                      padx=(12, 0))
        tk.Label(tb, text="Hall Effect Analog Gamepad Emulation", bg=BG,
                 fg=DIM, font=(F_UI, 9)).pack(anchor="w")

        # right side (packed right-to-left)
        RoundBtn(hdr, text="\u25a3  SAVE CONFIG", command=self._save_config,
                 width=150, height=38, r=9, fg="#04121c", bg=BG,
                 grad=("#00e5ff", "#2f7bff"),
                 font=(F_UI, 9, "bold")).pack(side="right")
        RoundBtn(hdr, text="\u21e9  Export", command=self._export,
                 width=96, height=38, r=9, fill=CARD, outline=BORDER2,
                 hover="#141c2b", bg=BG).pack(side="right", padx=12)

        pill = Card(hdr, bg=BG, fill=CARD, outline=BORDER, r=10, pad=6,
                    width=560, height=44)
        pill.pack(side="right")
        pin = pill.inner
        tk.Label(pin, text="\u2630", bg=CARD, fg=DIM,
                 font=(F_UI, 11)).pack(side="left", padx=(8, 6))
        tk.Label(pin, text="Profile:", bg=CARD, fg=DIM,
                 font=(F_UI, 9, "bold")).pack(side="left")
        self._profile_btn = tk.Menubutton(
            pin, textvariable=self._profile_name, bg=CARD, fg=FG,
            activebackground=CARD, activeforeground=ACCENT, relief="flat",
            bd=0, font=(F_UI, 9, "bold"), cursor="hand2",
            direction="below", indicatoron=True, anchor="w")
        self._profile_btn.pack(side="left", padx=(8, 0), fill="x",
                               expand=True)
        self._profile_menu = tk.Menu(self._profile_btn, tearoff=0, bg=CARD,
                                     fg=FG, activebackground=CYAN_BG,
                                     activeforeground=ACCENT, bd=0,
                                     font=(F_UI, 9))
        self._profile_btn.configure(menu=self._profile_menu)
        for glyph, cmd in (("\U0001f5d1", self._profile_delete),
                           ("\u29c9", self._profile_copy),
                           ("+", self._profile_add)):
            lb = tk.Label(pin, text=glyph, bg=CARD, fg=DIM, cursor="hand2",
                          font=("Segoe UI Symbol", 12))
            lb.pack(side="right", padx=6)
            lb.bind("<Button-1>", lambda e, c=cmd: c())
            lb.bind("<Enter>", lambda e, w=lb: w.config(fg=FG))
            lb.bind("<Leave>", lambda e, w=lb: w.config(fg=DIM))

        tk.Frame(top, bg=BORDER, height=1).pack(fill="x")

    # ------------------------------------------------------------------
    # body
    # ------------------------------------------------------------------
    def _card_header(self, parent, glyph, title, right=None):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(0, 10))
        tk.Label(row, text=glyph, bg=CARD, fg=ACCENT,
                 font=(F_UI, 11)).pack(side="left", padx=(0, 8))
        tk.Label(row, text=title, bg=CARD, fg=FG, justify="left",
                 font=(F_UI, 9, "bold")).pack(side="left")
        if right is not None:
            right(row)
        return row

    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=24, pady=(20, 8))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=4, minsize=440, uniform="b")
        body.grid_columnconfigure(1, weight=7, uniform="b")

        left = Card(body, r=14, pad=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self._build_left(left.inner)

        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=11)
        right.grid_rowconfigure(1, weight=8)

        top = tk.Frame(right, bg=BG)
        top.grid(row=0, column=0, sticky="nsew", pady=(0, 16))
        top.grid_rowconfigure(0, weight=1)
        top.grid_columnconfigure(0, weight=7)
        top.grid_columnconfigure(1, weight=10)
        c1 = Card(top)
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self._build_triggers(c1.inner)
        c2 = Card(top)
        c2.grid(row=0, column=1, sticky="nsew")
        self._build_sticks(c2.inner)

        bot = tk.Frame(right, bg=BG)
        bot.grid(row=1, column=0, sticky="nsew")
        bot.grid_rowconfigure(0, weight=1)
        bot.grid_columnconfigure(0, weight=1, uniform="c")
        bot.grid_columnconfigure(1, weight=1, uniform="c")
        c3 = Card(bot)
        c3.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self._build_pedal_curve(c3.inner)
        c4 = Card(bot)
        c4.grid(row=0, column=1, sticky="nsew")
        self._build_stick_curve(c4.inner)

    # ---- left card ----------------------------------------------------
    def _build_left(self, f):
        head = tk.Frame(f, bg=CARD)
        head.pack(fill="x", padx=14, pady=(12, 12))
        IconBox(head, "\u2328").pack(side="left")
        tb = tk.Frame(head, bg=CARD)
        tb.pack(side="left", padx=(12, 0))
        tk.Label(tb, text="Gamepad Mappings & Analog Travel", bg=CARD,
                 fg=FG, font=(F_UI, 11, "bold")).pack(anchor="w")
        tk.Label(tb, text="Hall effect continuous displacement sensor",
                 bg=CARD, fg=DIM, font=(F_UI, 8)).pack(anchor="w")

        sb = Card(head, bg=CARD, fill=PANEL2, outline=BORDER2, r=9, pad=6,
                  width=150, height=34)
        sb.pack(side="right")
        tk.Label(sb.inner, text="\u2315", bg=PANEL2, fg=DIM,
                 font=(F_UI, 11)).pack(side="left", padx=(4, 4))
        self._search_var = tk.StringVar(value="Search binds...")
        ent = tk.Entry(sb.inner, textvariable=self._search_var, bg=PANEL2,
                       fg=DIM, insertbackground=FG, relief="flat", bd=0,
                       highlightthickness=0, font=(F_UI, 9))
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<FocusIn>", self._search_in)
        ent.bind("<FocusOut>", self._search_out)
        ent.bind("<KeyRelease>", lambda e: self._apply_search())
        self._search_entry = ent

        strip = tk.Frame(f, bg=PANEL2)
        strip.pack(fill="x")
        strip.grid_columnconfigure(0, minsize=224)
        strip.grid_columnconfigure(1, minsize=92)
        strip.grid_columnconfigure(2, weight=1)
        for col, (txt, st) in enumerate((("GAMEPAD CONTROL", "w"),
                                         ("KEY / CODE", "w"),
                                         ("ACTION", "e"))):
            tk.Label(strip, text=txt, bg=PANEL2, fg=DIM,
                     font=(F_MONO, 8, "bold")).grid(
                row=0, column=col, sticky=st,
                padx=(14 if col == 0 else 0, 14 if col == 2 else 0), pady=8)
        tk.Frame(f, bg=BORDER, height=1).pack(fill="x")

        # footer first (pack bottom) so the table gets the rest
        foot = tk.Frame(f, bg=CARD)
        foot.pack(side="bottom", fill="x", padx=14, pady=(8, 10))
        r1 = tk.Frame(foot, bg=CARD)
        r1.pack(fill="x")
        tk.Label(r1, text="\u24d8", bg=CARD, fg=ACCENT,
                 font=(F_UI, 10)).pack(side="left")
        self._hint_var = tk.StringVar()
        self._hint(None)
        tk.Label(r1, textvariable=self._hint_var, bg=CARD, fg=DIM,
                 font=(F_UI, 8), wraplength=270, justify="left",
                 anchor="w").pack(side="left", padx=(6, 8), fill="x",
                                  expand=True)
        RoundBtn(r1, text="+  Add Axis/Button", command=self._add_custom,
                 width=140, height=30, r=8, fill="#141c2b",
                 outline=BORDER2, hover="#1d2940").pack(side="right")

        r2 = tk.Frame(foot, bg=CARD)
        r2.pack(fill="x", pady=(10, 0))
        self._block_var = tk.IntVar()
        self._learn_var = tk.IntVar()
        kb = "normal" if A._KB_OK else "disabled"
        for var, txt, cmd in ((self._block_var, "Block mapped keys",
                               self._on_block),
                              (self._learn_var, "Auto-learn names",
                               self._on_learn)):
            tk.Checkbutton(r2, text=txt, variable=var, command=cmd,
                           bg=CARD, fg=DIM, selectcolor=PANEL2,
                           activebackground=CARD, activeforeground=FG,
                           font=(F_UI, 8), state=kb, bd=0,
                           highlightthickness=0).pack(side="left",
                                                      padx=(0, 12))
        tk.Label(r2, text="Button threshold", bg=CARD, fg=DIM,
                 font=(F_UI, 8)).pack(side="left", padx=(8, 4))
        self._spin(r2, "thr", 0.05, 0.95, 0.05, "%.2f",
                   lambda v: self.engine.params.__setitem__(
                       "button_threshold", v),
                   lambda: self.engine.params.get("button_threshold", 0.35),
                   width=5).pack(side="left")
        tk.Frame(f, bg=BORDER, height=1).pack(side="bottom", fill="x")

        # scrollable table
        wrap = tk.Frame(f, bg=CARD)
        wrap.pack(fill="both", expand=True)
        self._tbl = tk.Canvas(wrap, bg=CARD, highlightthickness=0)
        sbar = ttk.Scrollbar(wrap, orient="vertical",
                             command=self._tbl.yview)
        self._tbl.configure(yscrollcommand=sbar.set)
        sbar.pack(side="right", fill="y")
        self._tbl.pack(side="left", fill="both", expand=True)
        self._tbl_in = tk.Frame(self._tbl, bg=CARD)
        win = self._tbl.create_window((0, 0), window=self._tbl_in,
                                      anchor="nw")
        self._tbl.bind("<Configure>", lambda e: self._tbl.itemconfigure(
            win, width=e.width))
        self._tbl_in.bind("<Configure>", lambda e: self._tbl.configure(
            scrollregion=self._tbl.bbox("all")))
        self._tbl.bind("<Enter>", lambda e: self._tbl.bind_all(
            "<MouseWheel>", self._on_wheel))
        self._tbl.bind("<Leave>", lambda e: self._tbl.unbind_all(
            "<MouseWheel>"))
        self._build_rows()

    def _build_rows(self):
        for ctrl, meta in CONTROLS.items():
            rf = tk.Frame(self._tbl_in, bg=CARD)
            rf.grid_columnconfigure(0, minsize=224)
            rf.grid_columnconfigure(1, minsize=92)
            rf.grid_columnconfigure(2, weight=1)
            f0 = tk.Frame(rf, bg=CARD)
            f0.grid(row=0, column=0, sticky="w", padx=(14, 0), pady=9)
            RoundBtn(f0, text=BADGE.get(ctrl, ctrl), width=34, height=28,
                     r=7, fill="#111a29", outline=BORDER2, fg=DIM,
                     font=(F_MONO, 8, "bold")).pack(side="left")
            tk.Label(f0, text=meta["label"], bg=CARD, fg=FG,
                     font=(F_UI, 9)).pack(side="left", padx=(12, 0))
            pill = RoundBtn(rf, text="\u2014", width=70, height=28, r=7,
                            fill="#0f1624", outline=BORDER2, fg=DIM,
                            font=(F_MONO, 9, "bold"))
            pill.grid(row=0, column=1, sticky="w")
            f2 = tk.Frame(rf, bg=CARD)
            f2.grid(row=0, column=2, sticky="e", padx=(0, 14))
            ub = tk.Label(f2, text="\u00d7", bg=CARD, fg=DIM2,
                          cursor="hand2", font=(F_UI, 12))
            ub.pack(side="right")
            ub.bind("<Button-1>", lambda e, c=ctrl: self._unbind(c))
            ub.bind("<Enter>", lambda e, w=ub: w.config(fg=RED))
            ub.bind("<Leave>", lambda e, w=ub: w.config(fg=DIM2))
            bb = RoundBtn(f2, text="Bind", width=64, height=28, r=7,
                          fill="#141c2b", outline=BORDER2, hover="#1d2940",
                          command=lambda c=ctrl: self._toggle_bind(c))
            bb.pack(side="right", padx=(0, 10))
            bar = tk.Canvas(rf, height=2, bg=CARD, highlightthickness=0)
            bar.grid(row=1, column=0, columnspan=3, sticky="we")
            rect = bar.create_rectangle(0, 0, 0, 2, outline="", fill=ACCENT)
            tk.Frame(rf, height=1, bg="#121926").grid(
                row=2, column=0, columnspan=3, sticky="we")
            rf.pack(fill="x")
            self._row[ctrl] = {"frame": rf, "pill": pill, "bind": bb,
                               "bar": bar, "rect": rect,
                               "label": meta["label"].lower()}

    def _on_wheel(self, e):
        self._tbl.yview_scroll(int(-e.delta / 120), "units")

    def _search_in(self, e):
        if self._search_var.get() == "Search binds...":
            self._search_var.set("")
            self._search_entry.config(fg=FG)

    def _search_out(self, e):
        if not self._search_var.get().strip():
            self._search_var.set("Search binds...")
            self._search_entry.config(fg=DIM)

    def _apply_search(self):
        q = self._search_var.get().strip().lower()
        if q == "search binds...":
            q = ""
        for w in self._row.values():
            w["frame"].pack_forget()
        for ctrl, w in self._row.items():
            hay = w["label"] + " " + ctrl.lower() + " " + \
                w["pill"].text.lower()
            if not q or q in hay:
                w["frame"].pack(fill="x")

    def _add_custom(self):
        messagebox.showinfo(
            "Add Axis/Button",
            "Custom axes and buttons aren't supported by the engine yet.\n"
            "All 18 Xbox 360 controls are listed on the left.")

    # ---- trigger / stick telemetry ------------------------------------
    def _build_triggers(self, f):
        def tag(row):
            self._active_tag = RoundBtn(
                row, text="Active", width=62, height=22, r=6,
                fill="#0a2a1e", outline="#145a3c", fg=GREEN,
                font=(F_MONO, 8, "bold"))
            self._active_tag.pack(side="right")

        self._card_header(f, "\u2261", "TRIGGER PRESSURE\nTELEMETRY", tag)
        pan = tk.Frame(f, bg=CARD)
        pan.pack(fill="both", expand=True)
        pan.grid_rowconfigure(0, weight=1)
        pan.grid_columnconfigure(0, weight=1, uniform="t")
        pan.grid_columnconfigure(1, weight=1, uniform="t")
        self.pan_lt = TriggerPanel(pan, "LT", "(Brake)", RED,
                                   ("#ff7a8a", "#ff3b5c"))
        self.pan_lt.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.pan_rt = TriggerPanel(pan, "RT", "(Throttle)", ACCENT,
                                   ("#19e6ff", "#2f6bff"))
        self.pan_rt.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._trig_foot = tk.Label(f, text="", bg=CARD, fg=DIM2,
                                   font=(F_MONO, 7), justify="center")
        self._trig_foot.pack(pady=(10, 0))

    def _build_sticks(self, f):
        def rt(row):
            tk.Label(row, text="Radial 2D\nSpace", bg=CARD, fg=DIM2,
                     font=(F_MONO, 7), justify="left").pack(side="right")

        self._card_header(f, "\u2295", "DUAL THUMBSTICK DEFLECTION\nVECTORS",
                          rt)
        pan = tk.Frame(f, bg=CARD)
        pan.pack(fill="both", expand=True)
        pan.grid_rowconfigure(0, weight=1)
        pan.grid_columnconfigure(0, weight=1, uniform="s")
        pan.grid_columnconfigure(1, weight=1, uniform="s")
        self.rad_l = RadarPanel(pan, "Left\nStick", "#4aa3ff")
        self.rad_l.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.rad_r = RadarPanel(pan, "Right\nStick", ORANGE)
        self.rad_r.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        tk.Frame(f, bg=BORDER, height=1).pack(fill="x", pady=(12, 0))
        inv = tk.Frame(f, bg=CARD)
        inv.pack(fill="x", pady=(8, 0))
        tk.Label(inv, text="Axis Invert:", bg=CARD, fg=DIM,
                 font=(F_MONO, 8, "bold")).pack(side="left")
        for axis, txt in reversed(list(zip(STICK_AXES,
                                           ("LS-X", "LS-Y", "RS-X",
                                            "RS-Y")))):
            key = "invert_" + axis.lower()
            var = tk.IntVar()
            self._invert_vars[key] = var
            tk.Checkbutton(inv, text=txt, variable=var, bg=CARD, fg=DIM,
                           selectcolor=PANEL2, activebackground=CARD,
                           activeforeground=FG, font=(F_MONO, 8, "bold"),
                           bd=0, highlightthickness=0,
                           command=lambda k=key: self._on_invert(k)).pack(
                side="right", padx=(8, 0))

    # ---- curve cards --------------------------------------------------
    def _gamma_right(self, name, setter, getter):
        def build(row):
            self._spin(row, name, 0.5, 2.5, 0.05, "%.2f", setter, getter,
                       width=5, fg=ACCENT).pack(side="right")
            tk.Label(row, text="Gamma:", bg=CARD, fg=DIM,
                     font=(F_MONO, 8)).pack(side="right", padx=(0, 8))
        return build

    def _build_pedal_curve(self, f):
        self._card_header(
            f, "\u2197", "TRIGGER CURVE (PEDALS\nRT/LT)",
            self._gamma_right(
                "g_pedal",
                lambda v: self.engine.params.__setitem__("gamma_pedal", v),
                lambda: self.engine.params.get("gamma_pedal", 1.3)))
        self.plot_pedal = CurvePlot(
            f, "Output Axis (0 \u2192 100%)",
            "Travel Depth (0.0 \u2192 4.0 mm)", "#19b8ff", "#19f0ff")
        self.plot_pedal.pack(fill="both", expand=True)
        foot = tk.Frame(f, bg=CARD)
        foot.pack(fill="x", pady=(10, 0))
        for color, txt, name, lo, hi, inc, setter, getter in (
                (RED, "Min Deadzone (mm):", "dead", 0.01, 0.80, 0.01,
                 self._set_dead,
                 lambda: self.engine.params.get("dead_zone_mm", 0.08)),
                (ACCENT, "Bottom-Out Saturation (mm):", "full", 2.0, 4.0, 0.05,
                 self._set_full,
                 lambda: self.engine.params.get("full_travel_mm", 3.6))):
            g = tk.Frame(foot, bg=CARD)
            g.pack(side="left", padx=(0, 12))
            tk.Frame(g, bg=color, width=10, height=3).pack(side="left",
                                                           padx=(0, 6))
            tk.Label(g, text=txt, bg=CARD, fg=DIM,
                     font=(F_MONO, 7)).pack(side="left", padx=(0, 6))
            self._spin(g, name, lo, hi, inc, "%.2f", setter, getter,
                       width=4, fg=FG).pack(side="left")

    def _build_stick_curve(self, f):
        self._card_header(
            f, "\u2248", "STICK CURVE (LS-X & LS-Y)",
            self._gamma_right("g_stick", self._set_stick_gamma,
                              lambda: self.engine.params.get("gamma_lsx",
                                                             1.15)))
        self.plot_stick = CurvePlot(
            f, "Gamepad Stick Emulation (%)", "Key Press Travel",
            "#7a5cff", "#19c8ff")
        self.plot_stick.pack(fill="both", expand=True)
        foot = tk.Frame(f, bg=CARD)
        foot.pack(fill="x", pady=(10, 0))
        tk.Label(foot, text="Progressive fine steering ramp-up", bg=CARD,
                 fg=DIM, font=(F_MONO, 7)).pack(side="left")
        tk.Label(foot, text="Dual Axis Linked", bg=CARD, fg="#4aa3ff",
                 font=(F_MONO, 7, "bold")).pack(side="right")

    # ---- spinbox helper -----------------------------------------------
    def _spin(self, parent, name, lo, hi, inc, fmt, setter, getter, width=5,
              fg=ACCENT):
        var = tk.StringVar()

        def commit(e=None):
            try:
                v = max(lo, min(hi, float(var.get())))
            except ValueError:
                v = getter()
            setter(v)
            var.set(fmt % getter())

        sp = tk.Spinbox(parent, from_=lo, to=hi, increment=inc,
                        textvariable=var, width=width, justify="center",
                        format=fmt, bg=PANEL2, fg=fg, insertbackground=fg,
                        relief="flat", bd=0, highlightthickness=1,
                        highlightbackground=BORDER2, highlightcolor=ACCENT,
                        buttonbackground=PANEL2, command=commit,
                        font=(F_MONO, 9, "bold"))
        sp.bind("<Return>", commit)
        sp.bind("<FocusOut>", commit)
        self._spins[name] = (var, getter, fmt)
        return sp

    def _set_dead(self, v):
        p = self.engine.params
        p["dead_zone_mm"] = min(v, p["full_travel_mm"] - 0.1)

    def _set_full(self, v):
        p = self.engine.params
        p["full_travel_mm"] = max(v, p["dead_zone_mm"] + 0.1)

    def _set_stick_gamma(self, v):
        for axis in STICK_AXES:
            self.engine.params["gamma_" + axis.lower()] = v

    # ---- status bar ---------------------------------------------------
    def _build_status_bar(self):
        tk.Frame(self.root, bg=BORDER, height=1).grid(row=2, column=0,
                                                      sticky="we")
        bar = tk.Frame(self.root, bg=BG)
        bar.grid(row=3, column=0, sticky="we", padx=24, pady=(10, 12))
        self._dot = tk.Canvas(bar, width=10, height=10, bg=BG,
                              highlightthickness=0)
        self._dot_id = self._dot.create_oval(1, 1, 9, 9, fill=GREEN,
                                             outline="")
        self._dot.pack(side="left", padx=(0, 8))
        self._status = tk.Label(bar, text="", bg=BG, fg=DIM,
                                font=(F_MONO, 8))
        self._status.pack(side="left")
        self._chk_lbl = tk.Label(bar, text="", bg=BG, fg=ACCENT,
                                 font=(F_MONO, 8, "bold"))
        self._chk_lbl.pack(side="right")
        tk.Label(bar, text="Profile Checksum:", bg=BG, fg=DIM,
                 font=(F_MONO, 8)).pack(side="right", padx=(0, 6))
        for txt in ("Calibration Wizard", "Documentation"):
            lb = tk.Label(bar, text=txt, bg=BG, fg=DIM2, cursor="hand2",
                          font=(F_MONO, 8))
            lb.pack(side="right", padx=(0, 16))
            lb.bind("<Button-1>", lambda e, t=txt: messagebox.showinfo(
                t, f"{t} isn't available yet."))
            lb.bind("<Enter>", lambda e, w=lb: w.config(fg=FG))
            lb.bind("<Leave>", lambda e, w=lb: w.config(fg=DIM2))

    # ------------------------------------------------------------------
    # state <-> widgets
    # ------------------------------------------------------------------
    def _names(self):
        return {**self.builtin_names, **self.learner.names}

    def _hint(self, text):
        self._hint_var.set(text or "Press any analog key to learn mapping "
                           "automatically. Click Bind, press the key, "
                           "release. Red = bound twice.")

    def _sync_widgets(self):
        p = self.engine.params
        for var, getter, fmt in self._spins.values():
            var.set(fmt % getter())
        for key, var in self._invert_vars.items():
            var.set(int(p.get(key, 0)))
        self._block_var.set(int(p.get("block_mapped_keys", 0)))
        self._learn_var.set(int(p.get("auto_learn_names", 1)))
        self._refresh_rows()

    def _refresh_rows(self):
        names = self._names()
        b = self.engine.bindings
        counts = Counter(v for v in b.values() if v is not None)
        for ctrl, w in self._row.items():
            idx = b.get(ctrl)
            if idx is None:
                text, color = "\u2014", DIM2
            else:
                n = names.get(idx)
                text = str(n).upper() if n else f"#{idx}"
                color = RED if counts[idx] > 1 else FG
            w["pill"].config_btn(text=text, fg=color)
            waiting = ctrl == self._await
            w["bind"].config_btn(
                text="Press\u2026" if waiting else "Bind",
                fill="#1d4a5c" if waiting else "#141c2b",
                fg=ACCENT if waiting else FG)

    def _on_invert(self, key):
        self.engine.params[key] = int(self._invert_vars[key].get())

    # ------------------------------------------------------------------
    # binding
    # ------------------------------------------------------------------
    def _toggle_bind(self, ctrl):
        if self._await == ctrl:
            self._cancel_bind()
            return
        self._await, self._await_idx = ctrl, None
        self._hint(f"Binding {CONTROLS[ctrl]['label']}: press the key to "
                   "full travel, then release it. Click again to cancel.")
        self._refresh_rows()

    def _cancel_bind(self):
        self._await = self._await_idx = None
        self._hint(None)
        self._refresh_rows()

    def _unbind(self, ctrl):
        self.engine.bindings[ctrl] = None
        if self._await == ctrl:
            self._cancel_bind()
        self._refresh_rows()
        self._apply_block()

    def _poll_bind(self):
        if not self._await:
            return
        r = self.reader
        if self._await_idx is None:
            with r.lock:
                best = max(r.values.items(), key=lambda kv: kv[1],
                           default=None)
            if best and best[1] >= BIND_PEAK:
                self._await_idx = best[0]
        elif r.get(self._await_idx) <= REST:
            ctrl, idx = self._await, self._await_idx
            self.engine.bindings[ctrl] = idx
            self._await = self._await_idx = None
            self._hint(f"Bound {CONTROLS[ctrl]['label']} to key #{idx}.")
            self._refresh_rows()
            self._apply_block()

    # ------------------------------------------------------------------
    # options
    # ------------------------------------------------------------------
    def _apply_block(self):
        if self.engine.params.get("block_mapped_keys"):
            failed = self.blocker.apply(self.engine.bindings, self._names())
            if failed:
                self._hint("Could not block: " + ", ".join(failed))
        else:
            self.blocker.stop()

    def _on_block(self):
        self.engine.params["block_mapped_keys"] = int(self._block_var.get())
        self._apply_block()

    def _on_learn(self):
        on = int(self._learn_var.get())
        self.engine.params["auto_learn_names"] = on
        if on:
            self.learner.start()
        else:
            self.learner.stop()

    # ------------------------------------------------------------------
    # profiles / files
    # ------------------------------------------------------------------
    def _rebuild_profile_menu(self):
        m = self._profile_menu
        m.delete(0, "end")
        for n in sorted(self.presets):
            m.add_command(label=n,
                          command=lambda n=n: self._profile_select(n))
        if self.presets:
            m.add_separator()
        m.add_command(label="Import profile\u2026", command=self._import)
        m.add_command(label="Reset curves to defaults",
                      command=self._reset_params)

    def _profile_select(self, name):
        if name not in self.presets:
            return
        apply_preset(self.engine, self.presets[name])
        self._profile_name.set(name)
        self._sync_widgets()
        self._apply_block()

    def _profile_add(self):
        name = simpledialog.askstring("New profile", "Profile name:",
                                      parent=self.root)
        name = (name or "").strip()
        if not name:
            return
        if name in self.presets and not messagebox.askyesno(
                "Overwrite", f"Profile '{name}' exists. Overwrite?"):
            return
        self.presets[name] = snapshot(self.engine)
        save_presets(self.presets)
        self._profile_name.set(name)
        self._rebuild_profile_menu()

    def _profile_copy(self):
        base = self._profile_name.get()
        new, i = f"{base} Copy", 2
        while new in self.presets:
            new, i = f"{base} Copy {i}", i + 1
        self.presets[new] = snapshot(self.engine)
        save_presets(self.presets)
        self._profile_name.set(new)
        self._rebuild_profile_menu()

    def _profile_delete(self):
        name = self._profile_name.get()
        if name not in self.presets:
            messagebox.showinfo("Profile", "Select a saved profile first.")
            return
        if messagebox.askyesno("Delete", f"Delete profile '{name}'?"):
            del self.presets[name]
            save_presets(self.presets)
            self._profile_name.set("Current config")
            self._rebuild_profile_menu()

    def _reset_params(self):
        if messagebox.askyesno("Reset", "Reset curves and sliders to "
                               "defaults? (Bindings are kept.)"):
            self.engine.params = dict(DEFAULTS)
            self._sync_widgets()

    def _save_config(self):
        try:
            self.engine.save()
            messagebox.showinfo("Saved", "Saved to pedals.ini")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save:\n{e}")

    def _export(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".pedals",
            filetypes=[("AnalogPedals profile", "*.pedals"),
                       ("All files", "*.*")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snapshot(self.engine), f, indent=2)

    def _import(self):
        path = filedialog.askopenfilename(
            filetypes=[("AnalogPedals profile", "*.pedals"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not (
                    "bindings" in data or "params" in data):
                raise ValueError("not a profile file")
            apply_preset(self.engine, data)
            self._sync_widgets()
            self._apply_block()
        except Exception as e:
            messagebox.showerror("Import failed", str(e))

    # ------------------------------------------------------------------
    # live update
    # ------------------------------------------------------------------
    def _raw(self, ctrl):
        return self.reader.get(self.engine.bindings.get(ctrl))

    def _tick(self):
        try:
            self._update_ui()
        except Exception:
            traceback.print_exc()
        self.root.after(33, self._tick)

    def _update_ui(self):
        st = dict(self._state)
        p = self.engine.params
        full = int(p["full_travel_mm"] * 100)
        dead = int(p["dead_zone_mm"] * 100)

        for ctrl, pan in (("LT", self.pan_lt), ("RT", self.pan_rt)):
            pan.set(st.get(ctrl, 0) / 255.0, self._raw(ctrl) / 100.0)
        self._trig_foot.config(
            text=f"Deadzone threshold: {p['dead_zone_mm']:.2f}mm | "
                 f"Saturation: {p['full_travel_mm']:.2f}mm")

        self.rad_l.set(st.get("LSX", 0.0), st.get("LSY", 0.0))
        self.rad_r.set(st.get("RSX", 0.0), st.get("RSY", 0.0))

        self.plot_pedal.set(full, dead, self.engine.gamma("pedal"),
                            max(self._raw("RT"), self._raw("LT")))
        stick_raw = max(self._raw(c) for c, m in CONTROLS.items()
                        if m["group"] == "stick")
        self.plot_stick.set(full, dead, self.engine.gamma("LSX"), stick_raw)

        for ctrl, w in self._row.items():
            raw = self._raw(ctrl)
            bw = w["bar"].winfo_width()
            w["bar"].coords(w["rect"], 0, 0,
                            int(min(1.0, raw / max(1, full)) * bw), 2)

        if p.get("auto_learn_names", 1) and self.learner.update():
            self._refresh_rows()
            self._apply_block()

        self._poll_bind()

        online = self.reader.dev is not None
        self._active_tag.config_btn(
            text="Active" if online else "Offline",
            fill="#0a2a1e" if online else "#2a0f14",
            outline="#145a3c" if online else "#5a1c26",
            fg=GREEN if online else RED)
        pad_ok = self.pad is not None
        self._dot.itemconfig(self._dot_id,
                             fill=GREEN if (pad_ok and online) else RED)
        bus = "Connected" if pad_ok else "Offline"
        kbd = "online" if online else "searching..."
        err = "  \u2022  mapping error (see console)" if self._loop_err \
            else ""
        self._status.config(
            text=f"Virtual Bus: ViGEmBus {bus}  \u2022  Keyboard: {kbd}  "
                 f"\u2022  Polling: {self.reader.fps:.0f} frames/s{err}",
            fg=RED if self._loop_err else DIM)

        now = time.time()
        if now - self._chk_t > 1.0:
            self._chk_t = now
            crc = zlib.crc32(json.dumps(snapshot(self.engine),
                                        sort_keys=True,
                                        default=str).encode())
            self._chk_lbl.config(text=f"#{crc >> 16:04X}-{crc & 0xFFFF:04X}")

    # ------------------------------------------------------------------
    def _close(self):
        self._worker_run = False
        try:
            self._worker.join(timeout=0.5)
        except Exception:
            pass
        try:
            self.engine.save()
        except Exception:
            pass
        self.reader.stop()
        self.blocker.stop()
        self.learner.stop()
        if self.pad is not None:
            try:
                self.pad.reset()
                self.pad.update()
            except Exception:
                pass
        try:
            d = A.hid.device()
            path = A.find_vendor_path()
            if path:
                d.open_path(path)
                d.write(bytes(A.P.build_close_trigger_test()))
                d.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    AnalogPedalsUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
