"""Single owner of the Tk workbench look: palette, ttk theme, fonts, DPI.

The theme is a real ttk theme ("tairobot") whose widget chrome is drawn by
``src.ui.raster`` as anti-aliased 9-slice images at the display's actual
DPI scale — rounded buttons/fields/cards, pill scrollbars, underline tabs.
Recoloring a stock theme (clam) cannot produce that: its elements are
hard 1-px rectangles.

Rules:
* No imports from ``run_backtest`` (one-way dependency).
* ``init_theme`` must succeed on a withdrawn root. Pass ``minimal=True``
  for headless bots: no theme is created at all (dotted style names fall
  back to the stock layouts), so the live path does zero extra work.
* Pixel metrics go through ``S()`` — the process is DPI-aware, so a raw
  ``padx=8`` is 8 *physical* pixels (half size on a 200 % display).
"""

from __future__ import annotations

import base64
import sys
from typing import Any

from src.ui import raster
from src.ui.raster import mix

# ── Design tokens ────────────────────────────────────────────────────
# Cool-neutral dark. Three surface levels (inset < bg < raised), one
# accent, semantic colors reserved for status / P&L only.
PALETTE: dict[str, str] = {
    "bg": "#0f1217",
    "bg_raised": "#171b22",
    "bg_inset": "#0a0d11",
    "bg_hover": "#1f252e",
    "border": "#252b35",
    "border_strong": "#363e4b",
    "text": "#e6e9ef",
    "text_dim": "#9aa4b2",
    "text_faint": "#66707f",
    "accent": "#4c8dff",
    "accent_hover": "#6ba1ff",
    "accent_pressed": "#3b78e6",
    "on_accent": "#ffffff",
    "ok": "#35c27d",
    "warn": "#f0b429",
    "err": "#f25c5c",
    "info": "#7cb7ff",
    "up": "#26a69a",
    "down": "#ef5350",
}

EMPTY_FG = PALETTE["text_faint"]

# Report / regime card tones.
TONE: dict[str, str] = {
    "good": PALETTE["ok"],
    "bad": PALETTE["err"],
}

# Whole-row tints for dense tables: the same hues pulled toward the text
# color, so a 200-row trade list reads as data rather than as an alarm.
ROW_TONE: dict[str, str] = {
    "good": mix(PALETTE["text"], PALETTE["ok"], 0.62),
    "bad": mix(PALETTE["text"], PALETTE["err"], 0.62),
}

CHAT_TAGS: dict[str, dict[str, str]] = {
    "user": {"foreground": "#8ab4ff"},
    "assistant": {"foreground": PALETTE["text"]},
    "code": {"foreground": "#e2b27c"},
    "error": {"foreground": PALETTE["err"]},
    "system": {"foreground": "#7fbf8e"},
}

LOG_TAGS: dict[str, dict[str, str]] = {
    "entry": {"foreground": PALETTE["ok"]},
    "exit": {"foreground": PALETTE["err"]},
    "bar": {"foreground": PALETTE["info"]},
    "status": {"foreground": PALETTE["warn"]},
}

# Vote-liveness / connection dots: unknown / stale / fresh.
DOT_COLORS: dict[str, str] = {
    "off": PALETTE["text_faint"],
    "err": PALETTE["err"],
    "ok": PALETTE["ok"],
    "warn": PALETTE["warn"],
}

# Regime episode Treeview bands: tinted rows, never saturated fills.
EPISODE_TAGS: dict[str, dict[str, str]] = {
    "ep_long": {"background": mix(PALETTE["bg_inset"], PALETTE["ok"], 0.16),
                "foreground": PALETTE["ok"]},
    "ep_short": {"background": mix(PALETTE["bg_inset"], PALETTE["err"], 0.16),
                 "foreground": PALETTE["err"]},
    "ep_idle": {"background": mix(PALETTE["bg_inset"], PALETTE["text_dim"], 0.10),
                "foreground": PALETTE["text_dim"]},
    "ep_unknown": {"background": mix(PALETTE["bg_inset"], PALETTE["info"], 0.14),
                   "foreground": PALETTE["info"]},
    "day": {"foreground": PALETTE["text_faint"]},
}

STATUS_LEVEL_STYLES: dict[str, str] = {
    "info": "StatusStrip.Dim.TLabel",
    "ok": "StatusStrip.Ok.TLabel",
    "warn": "StatusStrip.Warn.TLabel",
    "error": "StatusStrip.Err.TLabel",
}

# Populated by init_theme (tkfont.Font objects need a live Tcl interpreter).
FONTS: dict[str, Any] = {}

THEME_NAME = "tairobot"

_SCALE = 1.0

_UI_FAMILIES = (
    "Segoe UI", "Microsoft JhengHei UI", "Microsoft JhengHei",
    "Noto Sans CJK TC", "Noto Sans", "DejaVu Sans",
)
_UI_SEMIBOLD_FAMILIES = ("Segoe UI Semibold",)
_MONO_FAMILIES = (
    "Cascadia Mono", "Consolas", "Noto Sans Mono CJK TC", "DejaVu Sans Mono",
)
_ICON_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")


# ── DPI ──────────────────────────────────────────────────────────────

def enable_dpi_awareness() -> bool:
    """Opt the process into system-DPI awareness (call before ``tk.Tk()``).

    Without it Windows renders the window at 96 DPI and bitmap-stretches
    it — everything is blurry on a scaled display. System-aware (1), not
    per-monitor (2): Tk 8.6 ignores WM_DPICHANGED, so per-monitor would
    leave the window mis-sized after a drag to another monitor, while
    system-aware lets Windows rescale it there.

    A frozen EXE whose manifest already declares awareness makes this call
    fail with E_ACCESSDENIED — that is fine.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        return False


def scale() -> float:
    return _SCALE


def S(*px):
    """Scale logical (96-DPI) pixels to device pixels.

    ``S(8)`` → int, ``S(8, 4)`` → tuple — drop-in for padx/pady pairs.
    Non-zero inputs never round down to 0.
    """
    def one(v):
        if not v:
            return 0
        r = int(round(v * _SCALE))
        if r == 0:
            return 1 if v > 0 else -1
        return r
    if len(px) == 1:
        return one(px[0])
    return tuple(one(v) for v in px)


# ── Raw-Tk helpers ───────────────────────────────────────────────────

def style_text_widget(widget, *, inset: bool = True) -> None:
    """Apply surface colors to a raw Tk Text. Borderless: the rounded
    edge comes from the ``Well.TFrame`` the text sits in."""
    p = PALETTE
    bg = p["bg_inset"] if inset else p["bg_raised"]
    widget.configure(
        bg=bg, fg=p["text"],
        insertbackground=p["text"],
        selectbackground=mix(bg, p["accent"], 0.45),
        selectforeground=p["text"],
        inactiveselectbackground=mix(bg, p["accent"], 0.25),
        highlightthickness=0,
        relief="flat", borderwidth=0,
        insertwidth=S(2),
    )


def apply_window_theme(win, *, caption: str | None = None) -> None:
    """Paint a Tk/Toplevel background and theme its native caption bar.

    ``caption`` is the title-bar color (Windows 11); it defaults to the
    window background so the bar melts into the content. The main window
    passes the raised top-bar color instead.
    """
    try:
        win.configure(bg=PALETTE["bg"])
    except Exception:
        pass
    _style_titlebar(win, caption or PALETTE["bg"])


def _style_titlebar(win, caption: str) -> None:
    """Dark caption (Win10 1809+) and exact caption color (Win11)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi

        def set_attr(attr: int, value: int) -> bool:
            v = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0

        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (19 on pre-20H1 builds).
        if not set_attr(20, 1):
            set_attr(19, 1)
        r, g, b = raster.hex_to_rgb(caption)
        set_attr(35, (b << 16) | (g << 8) | r)      # DWMWA_CAPTION_COLOR
        tr, tg, tb = raster.hex_to_rgb(PALETTE["text_dim"])
        set_attr(36, (tb << 16) | (tg << 8) | tr)   # DWMWA_TEXT_COLOR
    except Exception:
        pass


def set_app_icon(root) -> None:
    """Window/taskbar icon drawn by the rasterizer (no .ico asset)."""
    try:
        import tkinter as tk
        icons = []
        for size in (64, 32, 16):
            im = raster.Image(size, size)
            u = size / 32.0
            im.rounded_rect(0, 0, size, size, 7 * u, PALETTE["accent"])
            im.polyline([(7 * u, 22 * u), (13 * u, 15 * u), (18 * u, 19 * u),
                         (25 * u, 9 * u)], 2.8 * u, PALETTE["on_accent"])
            im.circle(25 * u, 9 * u, 2.6 * u, PALETTE["on_accent"])
            icons.append(tk.PhotoImage(
                master=root, data=base64.b64encode(im.to_png()), format="png"))
        root._tairobot_icons = icons  # keep alive
        root.iconphoto(True, *icons)
    except Exception:
        pass


def _pick_family(root, candidates: tuple[str, ...], default: str) -> str:
    from tkinter import font as tkfont
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return default


# ── Image factory ────────────────────────────────────────────────────

class _Images:
    """Builds and owns the PhotoImages for one Tcl interpreter.

    PhotoImages die with their last Python reference, so the instance is
    pinned on the root (``root._tairobot_images``).
    """

    # ttk tiles the center slice of an image element, so every 9-slice
    # source gets its center widened to this many logical pixels.
    CENTER = 40

    def __init__(self, root):
        self.root = root
        self._cache: dict[tuple, Any] = {}

    def _photo(self, key: tuple, build):
        """``build`` returns a raster.Image or an ``(rgba, w, h)`` tuple."""
        img = self._cache.get(key)
        if img is None:
            import tkinter as tk
            built = build()
            if isinstance(built, raster.Image):
                png = built.to_png()
            else:
                rgba, w, h = built
                png = raster.encode_png(w, h, rgba)
            img = tk.PhotoImage(master=self.root,
                                data=base64.b64encode(png), format="png")
            self._cache[key] = img
        return img

    def box(self, fill, border=None, *, radius=6, bw=1, pad=0):
        """9-slice source for a rounded box. Returns (image, slice_border)."""
        r = S(radius)
        b = S(bw) if border else 0
        p = S(pad) if pad else 0
        edge = r + b + p + 1
        size = edge * 2 + 1
        center = S(self.CENTER)

        def build():
            im = raster.Image(size, size)
            im.bordered_rect(p, p, size - p, size - p, r, fill, border, b)
            return raster.stretch_center(im.to_rgba(), size, size,
                                         edge, edge, center, center)

        return self._photo(("box", fill, border, r, b, p), build), edge

    @staticmethod
    def min_size(edge: int) -> dict:
        """element_create kwargs pinning an element's minimum size to its
        un-stretched source (ttk defaults to the full image size, which
        the widened center would inflate to ~100 px), with zero built-in
        padding (it defaults to the slice border)."""
        side = edge * 2 + 1
        return {"width": side, "height": side, "padding": 0}

    def underline_tab(self, fill, bar, *, radius=6):
        """Tab source: optional rounded fill + optional bottom accent bar."""
        r = S(radius)
        bar_h = S(2)
        gap = S(3)
        edge = r + 1
        w = edge * 2 + 1
        h = edge * 2 + 1 + gap + bar_h

        def build():
            im = raster.Image(w, h)
            if fill:
                im.rounded_rect(0, 0, w, h - gap - bar_h, r, fill)
            if bar:
                im.rounded_rect(edge - 1, h - bar_h, w - edge + 1, h,
                                bar_h / 2, bar)
            return raster.stretch_center(im.to_rgba(), w, h, edge, edge,
                                         S(self.CENTER), S(12))

        return (self._photo(("tab", fill, bar, r), build),
                (edge, edge, edge, edge + gap + bar_h))

    def bands(self, key: str, width: int, bands):
        """Flat horizontal bands (hairlines, fills) — no per-pixel work."""
        return self._photo(("bands", key, width, tuple(bands)),
                           lambda: raster.solid_rows(width, list(bands)))

    def pill_thumb(self, color, *, vertical=True):
        """Scrollbar thumb: a pill centered in a wider transparent hit area."""
        track = S(12)
        thick = S(6)
        long = S(30)
        inset = (track - thick) // 2

        def build():
            if vertical:
                im = raster.Image(track, long)
                im.rounded_rect(inset, S(2), inset + thick, long - S(2),
                                thick / 2, color)
            else:
                im = raster.Image(long, track)
                im.rounded_rect(S(2), inset, long - S(2), inset + thick,
                                thick / 2, color)
            return im

        cap = thick // 2 + S(2) + 1
        border = (0, cap, 0, cap) if vertical else (cap, 0, cap, 0)
        return self._photo(("thumb", color, vertical), build), border

    def blank(self, w, h):
        return self._photo(("blank", w, h), lambda: raster.Image(w, h))

    def solid(self, color, w=1, h=1):
        def build():
            im = raster.Image(w, h)
            im.rounded_rect(0, 0, w, h, 0, color)
            return im
        return self._photo(("solid", color, w, h), build)

    def check(self, box_fill, box_border, mark=None, *, radio=False,
              gap=8):
        """Check / radio indicator with a transparent right-hand gap."""
        size = S(18)
        total_w = size + S(gap)

        def build():
            im = raster.Image(total_w, size)
            if radio:
                c = size / 2
                im.circle(c, c, size / 2, box_border)
                im.circle(c, c, size / 2 - S(1.5), box_fill)
                if mark:
                    im.circle(c, c, size * 0.22, mark)
            else:
                im.bordered_rect(0, 0, size, size, S(4), box_fill,
                                 box_border, S(1.5))
                if mark:
                    im.polyline([(size * 0.26, size * 0.52),
                                 (size * 0.44, size * 0.70),
                                 (size * 0.76, size * 0.32)],
                                S(2), mark)
            return im

        return self._photo(("check", box_fill, box_border, mark, radio, gap),
                           build)

    def chevron(self, color, direction="down", *, box=16, pad_right=6):
        size = S(box)
        total_w = size + S(pad_right)

        def build():
            im = raster.Image(total_w, size)
            a, b, m = size * 0.30, size * 0.70, size * 0.5
            if direction == "down":
                pts = [(a, size * 0.40), (m, size * 0.62), (b, size * 0.40)]
            elif direction == "up":
                pts = [(a, size * 0.60), (m, size * 0.38), (b, size * 0.60)]
            else:  # right
                pts = [(size * 0.40, a), (size * 0.62, m), (size * 0.40, b)]
            im.polyline(pts, S(1.6), color)
            return im

        return self._photo(("chev", color, direction, box, pad_right), build)


# ── Theme construction ───────────────────────────────────────────────

def _define_fonts(root) -> None:
    """(Re)define the theme fonts under fixed names.

    Fixed names + ``delete_font = False`` matter: the ttk style database
    and the option database reference fonts BY NAME. Auto-named fonts are
    deleted when their Python object is dropped (``Font.__del__``), so a
    second ``init_theme`` — or a second root clearing ``FONTS`` — silently
    reset every themed widget to the default font. Names are per
    interpreter, so each root gets its own set under the same names.
    """
    from tkinter import font as tkfont
    ui = _pick_family(root, _UI_FAMILIES, "TkDefaultFont")
    semi = _pick_family(root, _UI_SEMIBOLD_FAMILIES, ui)
    mono = _pick_family(root, _MONO_FAMILIES, "TkFixedFont")
    icon = _pick_family(root, _ICON_FAMILIES, "")
    semi_weight = "normal" if semi != ui else "bold"

    specs = {
        "body": dict(family=ui, size=10, weight="normal"),
        "body_bold": dict(family=semi, size=10, weight=semi_weight),
        "small": dict(family=ui, size=9, weight="normal"),
        "caption": dict(family=ui, size=8, weight="normal"),
        "section": dict(family=semi, size=12, weight=semi_weight),
        "title": dict(family=semi, size=15, weight=semi_weight),
        "value": dict(family=semi, size=11, weight=semi_weight),
        "metric": dict(family=semi, size=17, weight=semi_weight),
        "mono": dict(family=mono, size=10, weight="normal"),
        "mono_small": dict(family=mono, size=9, weight="normal"),
        "mono_bold": dict(family=mono, size=10, weight="bold"),
    }
    if icon:
        specs["icon"] = dict(family=icon, size=11, weight="normal")

    existing = set(tkfont.names(root))
    FONTS.clear()
    for key, spec in specs.items():
        name = f"tairobot_{key}"
        if name in existing:
            font = tkfont.Font(root=root, name=name, exists=True)
            font.configure(**spec)
        else:
            font = tkfont.Font(root=root, name=name, **spec)
        font.delete_font = False
        FONTS[key] = font


def _button_variant(style, im: _Images, name: str, element: str, *,
                    fill, hover, pressed, border, fg,
                    disabled_fill, disabled_border, disabled_fg,
                    focus_border, padding) -> None:
    normal, edge = im.box(fill, border)
    style.element_create(
        element, "image", normal,
        ("disabled", im.box(disabled_fill, disabled_border)[0]),
        ("pressed", im.box(pressed, border)[0]),
        ("active", im.box(hover, border)[0]),
        ("focus", im.box(fill, focus_border)[0]),
        border=edge, sticky="nsew", **im.min_size(edge))
    style.layout(name, [
        (element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})])
    style.configure(name, padding=padding, foreground=fg,
                    anchor="center", font=FONTS["body"])
    style.map(name, foreground=[("disabled", disabled_fg)])


def _build_theme(root, style) -> None:
    p = PALETTE
    im = _Images(root)
    root._tairobot_images = im  # keep PhotoImages alive

    bg, raised, inset = p["bg"], p["bg_raised"], p["bg_inset"]
    text, dim, faint = p["text"], p["text_dim"], p["text_faint"]
    accent = p["accent"]
    btn_fill = mix(raised, text, 0.05)
    btn_hover = mix(raised, text, 0.11)
    btn_pressed = mix(raised, bg, 0.6)
    dis_fill = mix(bg, raised, 0.5)
    dis_fg = mix(bg, dim, 0.55)
    field_border = p["border_strong"]
    sel_bg = mix(inset, accent, 0.40)

    style.theme_create(THEME_NAME, parent="default")
    style.theme_use(THEME_NAME)

    style.configure(".", background=bg, foreground=text,
                    troughcolor=inset, focuscolor=accent,
                    selectbackground=sel_bg, selectforeground=text,
                    insertcolor=text, fieldbackground=inset,
                    font=FONTS["body"], borderwidth=0)

    # ── Frames / labels ──
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=text)
    style.configure("Dim.TLabel", foreground=dim)
    style.configure("Faint.TLabel", foreground=faint)
    style.configure("Small.Dim.TLabel", foreground=dim, font=FONTS["small"])
    style.configure("Section.TLabel", font=FONTS["section"])
    style.configure("Title.TLabel", font=FONTS["title"])
    style.configure("Empty.TLabel", foreground=EMPTY_FG)
    style.configure("Empty.Inset.TLabel", background=inset,
                    foreground=EMPTY_FG)
    for tone, color in (("Ok", p["ok"]), ("Warn", p["warn"]),
                        ("Err", p["err"]), ("Info", p["info"])):
        style.configure(f"Status.{tone}.TLabel", foreground=color)

    # Cards: a rounded raised surface. Children that sit on it use the
    # Card.* styles so their background matches the fill.
    card_img, card_edge = im.box(raised, p["border"], radius=8)
    style.element_create("Card.frame", "image", card_img,
                         border=card_edge, sticky="nsew",
                         **im.min_size(card_edge))
    style.layout("Card.TFrame", [("Card.frame", {"sticky": "nsew"})])
    style.configure("Card.TFrame", background=bg)
    style.configure("CardBody.TFrame", background=raised)
    style.configure("Card.TLabel", background=raised, foreground=text)
    style.configure("Card.Dim.TLabel", background=raised, foreground=dim,
                    font=FONTS["small"])
    style.configure("CardValue.TLabel", background=raised, foreground=text,
                    font=FONTS["metric"])
    style.configure("CardValueSmall.TLabel", background=raised,
                    foreground=text, font=FONTS["value"])
    for tone, color in (("Ok", p["ok"]), ("Warn", p["warn"]),
                        ("Err", p["err"])):
        style.configure(f"Card.Status.{tone}.TLabel", background=raised,
                        foreground=color)

    # Wells: rounded inset surface hosting a borderless tk.Text.
    well_img, well_edge = im.box(inset, p["border"], radius=8)
    style.element_create("Well.frame", "image", well_img,
                         ("focus", im.box(inset, accent, radius=8)[0]),
                         border=well_edge, sticky="nsew",
                         **im.min_size(well_edge))
    style.layout("Well.TFrame", [("Well.frame", {"sticky": "nsew"})])
    style.configure("Well.TFrame", background=bg)
    style.configure("WellBody.TFrame", background=inset)

    # Top bar / status strip: flat raised bands.
    style.configure("Bar.TFrame", background=raised)
    style.configure("Bar.TLabel", background=raised, foreground=text)
    style.configure("Bar.Dim.TLabel", background=raised, foreground=dim)
    style.configure("Bar.Title.TLabel", background=raised, foreground=text,
                    font=FONTS["section"])
    style.configure("StatusStrip.TFrame", background=raised)
    style.configure("StatusStrip.TLabel", background=raised, foreground=text)
    style.configure("StatusStrip.Dim.TLabel", background=raised,
                    foreground=dim)
    style.configure("StatusStrip.Ok.TLabel", background=raised,
                    foreground=p["ok"])
    style.configure("StatusStrip.Warn.TLabel", background=raised,
                    foreground=p["warn"])
    style.configure("StatusStrip.Err.TLabel", background=raised,
                    foreground=p["err"])

    # ── Buttons ──
    pad = S(14, 6, 14, 6)
    common = dict(disabled_fill=dis_fill, disabled_border=p["border"],
                  disabled_fg=dis_fg, padding=pad)
    _button_variant(style, im, "TButton", "Button.button",
                    fill=btn_fill, hover=btn_hover, pressed=btn_pressed,
                    border=p["border_strong"], fg=text,
                    focus_border=accent, **common)
    _button_variant(style, im, "Accent.TButton", "Accent.button",
                    fill=accent, hover=p["accent_hover"],
                    pressed=p["accent_pressed"], border=None,
                    fg=p["on_accent"], focus_border=p["on_accent"],
                    **common)
    _button_variant(style, im, "Success.TButton", "Success.button",
                    fill=mix(raised, p["ok"], 0.85),
                    hover=p["ok"], pressed=mix(bg, p["ok"], 0.7),
                    border=None, fg="#06130c",
                    focus_border=p["on_accent"], **common)
    _button_variant(style, im, "Danger.TButton", "Danger.button",
                    fill=mix(raised, p["err"], 0.85),
                    hover=p["err"], pressed=mix(bg, p["err"], 0.7),
                    border=None, fg="#1a0606",
                    focus_border=p["on_accent"], **common)
    _button_variant(style, im, "Ghost.TButton", "Ghost.button",
                    fill=None, hover=btn_hover, pressed=btn_pressed,
                    border=None, fg=dim, focus_border=accent,
                    disabled_fill=None, disabled_border=None,
                    disabled_fg=dis_fg, padding=S(10, 6, 10, 6))
    style.map("Ghost.TButton", foreground=[("disabled", dis_fg),
                                           ("active", text)])
    # Same chrome on other surfaces: only the corner fill changes.
    for prefix, surface in (("Card", raised), ("Bar", raised)):
        for variant in ("", "Accent.", "Success.", "Danger.", "Ghost."):
            style.configure(f"{prefix}.{variant}TButton", background=surface)
    style.layout("Bar.Ghost.TButton", style.layout("Ghost.TButton"))
    style.layout("Card.Ghost.TButton", style.layout("Ghost.TButton"))
    for prefix in ("Card", "Bar"):
        for variant in ("Accent", "Success", "Danger"):
            style.layout(f"{prefix}.{variant}.TButton",
                         style.layout(f"{variant}.TButton"))

    # ── Entry / Combobox ──
    field, fedge = im.box(inset, field_border)
    field_focus = im.box(inset, accent)[0]
    field_hover = im.box(inset, mix(field_border, text, 0.25))[0]
    field_dis = im.box(dis_fill, p["border"])[0]
    style.element_create("Entry.field", "image", field,
                         ("disabled", field_dis), ("focus", field_focus),
                         ("hover", field_hover),
                         border=fedge, sticky="nsew", **im.min_size(fedge))
    style.layout("TEntry", [
        ("Entry.field", {"sticky": "nsew", "children": [
            ("Entry.padding", {"sticky": "nsew", "children": [
                ("Entry.textarea", {"sticky": "nsew"})]})]})])
    style.configure("TEntry", padding=S(8, 5, 8, 5), foreground=text,
                    insertcolor=text, fieldbackground=inset)
    style.map("TEntry", foreground=[("disabled", dis_fg)])

    style.element_create("Combobox.field", "image", field,
                         ("disabled", field_dis), ("focus", field_focus),
                         ("hover", field_hover),
                         border=fedge, sticky="nsew", **im.min_size(fedge))
    style.element_create("Combobox.downarrow", "image",
                         im.chevron(dim, "down"),
                         ("disabled", im.chevron(dis_fg, "down")),
                         ("hover", im.chevron(text, "down")),
                         sticky="")
    style.layout("TCombobox", [
        ("Combobox.field", {"sticky": "nsew", "children": [
            ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"expand": "1", "sticky": "nsew",
                                  "children": [
                ("Combobox.textarea", {"sticky": "nsew"})]})]})])
    style.configure("TCombobox", padding=S(8, 5, 2, 5), foreground=text,
                    fieldbackground=inset, insertcolor=text)
    style.map("TCombobox",
              foreground=[("disabled", dis_fg)],
              selectbackground=[("readonly", inset), ("!focus", inset)],
              selectforeground=[("readonly", text), ("!focus", text)])
    style.configure("ComboboxPopdownFrame", background=p["border_strong"],
                    borderwidth=S(1), relief="flat")

    # ── Check / radio ──
    box_off = im.check(inset, field_border)
    style.element_create(
        "Checkbutton.indicator", "image", box_off,
        ("disabled", "selected", im.check(dis_fill, p["border"], dis_fg)),
        ("disabled", im.check(dis_fill, p["border"])),
        ("selected", "active", im.check(p["accent_hover"], p["accent_hover"],
                                        p["on_accent"])),
        ("selected", im.check(accent, accent, p["on_accent"])),
        ("active", im.check(inset, mix(field_border, text, 0.35))),
        sticky="w")
    style.layout("TCheckbutton", [
        ("Checkbutton.padding", {"sticky": "nsew", "children": [
            ("Checkbutton.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.label", {"side": "left", "sticky": "nsew"})]})])
    style.configure("TCheckbutton", padding=S(2, 3, 2, 3), foreground=text)
    style.map("TCheckbutton", foreground=[("disabled", dis_fg)])

    radio_off = im.check(inset, field_border, radio=True)
    style.element_create(
        "Radiobutton.indicator", "image", radio_off,
        ("disabled", "selected", im.check(dis_fill, p["border"], dis_fg,
                                          radio=True)),
        ("disabled", im.check(dis_fill, p["border"], radio=True)),
        ("selected", "active", im.check(p["accent_hover"], p["accent_hover"],
                                        p["on_accent"], radio=True)),
        ("selected", im.check(accent, accent, p["on_accent"], radio=True)),
        ("active", im.check(inset, mix(field_border, text, 0.35),
                            radio=True)),
        sticky="w")
    style.layout("TRadiobutton", [
        ("Radiobutton.padding", {"sticky": "nsew", "children": [
            ("Radiobutton.indicator", {"side": "left", "sticky": ""}),
            ("Radiobutton.label", {"side": "left", "sticky": "nsew"})]})])
    style.configure("TRadiobutton", padding=S(2, 3, 2, 3), foreground=text)
    style.map("TRadiobutton", foreground=[("disabled", dis_fg)])
    # Surface variants: identical chrome, only the corner fill (the
    # implicit background element) follows the surface the widget sits on.
    for prefix, surface in (("Card", raised), ("Bar", raised)):
        for base in ("TCheckbutton", "TRadiobutton", "TEntry", "TCombobox"):
            style.configure(f"{prefix}.{base}", background=surface)

    # ── Notebook: underline tabs over a hairline ──
    tab_normal, tab_border = im.underline_tab(None, None)
    style.element_create(
        "Notebook.tab", "image", tab_normal,
        ("selected", im.underline_tab(None, accent)[0]),
        ("active", im.underline_tab(btn_fill, None)[0]),
        border=tab_border, sticky="nsew", padding=0,
        width=tab_border[0] + tab_border[2] + 1,
        height=tab_border[1] + tab_border[3] + 1)
    style.layout("TNotebook.Tab", [
        ("Notebook.tab", {"sticky": "nsew", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nsew",
                                  "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})])
    style.configure("TNotebook.Tab", padding=S(14, 7, 14, 9),
                    foreground=dim, font=FONTS["body"])
    style.map("TNotebook.Tab", foreground=[("selected", text),
                                           ("active", text)])
    hair = S(1)
    # The client element spans the whole page area (hidden behind the page
    # frames), so keep it a plain zero-width border: no image tiling.
    style.configure("TNotebook", background=bg, borderwidth=0,
                    tabmargins=S(2, 2, 2, 0), padding=S(0, 8, 0, 0))

    # ── Scrollbars: arrowless pills ──
    for orient, vertical in (("Vertical", True), ("Horizontal", False)):
        thumb, tborder = im.pill_thumb(mix(bg, dim, 0.35), vertical=vertical)
        style.element_create(
            f"{orient}.Scrollbar.thumb", "image", thumb,
            ("pressed", im.pill_thumb(mix(bg, dim, 0.75),
                                      vertical=vertical)[0]),
            ("active", im.pill_thumb(mix(bg, dim, 0.55),
                                     vertical=vertical)[0]),
            border=tborder, sticky="nsew")
        trough = im.blank(S(12), S(12))
        style.element_create(f"{orient}.Scrollbar.trough", "image", trough,
                             sticky="nsew")
        style.layout(f"{orient}.TScrollbar", [
            (f"{orient}.Scrollbar.trough", {
                "sticky": "ns" if vertical else "ew", "children": [
                    (f"{orient}.Scrollbar.thumb",
                     {"expand": "1", "sticky": "nsew"})]})])
        style.configure(f"{orient}.TScrollbar", background=bg)
        style.layout(f"Inset.{orient}.TScrollbar",
                     style.layout(f"{orient}.TScrollbar"))
        style.configure(f"Inset.{orient}.TScrollbar", background=inset)
        # "Hidden" = same footprint, no thumb. Widgets swap to it when the
        # content fits, instead of unmapping the scrollbar (which re-wraps
        # the text and can oscillate).
        hidden = [(f"{orient}.Scrollbar.trough",
                   {"sticky": "ns" if vertical else "ew"})]
        style.layout(f"Hidden.{orient}.TScrollbar", hidden)
        style.configure(f"Hidden.{orient}.TScrollbar", background=bg)
        style.layout(f"Hidden.Inset.{orient}.TScrollbar", hidden)
        style.configure(f"Hidden.Inset.{orient}.TScrollbar", background=inset)

    # ── Treeview ──
    tree_field, tedge = im.box(inset, p["border"], radius=8)
    style.element_create("Treeview.field", "image", tree_field,
                         border=tedge, sticky="nsew", **im.min_size(tedge))
    style.layout("Treeview", [
        ("Treeview.field", {"sticky": "nsew", "children": [
            ("Treeview.padding", {"sticky": "nsew", "children": [
                ("Treeview.treearea", {"sticky": "nsew"})]})]})])
    style.configure("Treeview", background=inset, fieldbackground=inset,
                    foreground=text, rowheight=S(30), padding=S(6),
                    font=FONTS["body"])
    style.map("Treeview",
              background=[("selected", sel_bg)],
              foreground=[("selected", text)])
    head = im.bands("head", S(64), [(S(48), inset), (hair, p["border"])])
    style.element_create("Treeheading.cell", "image", head,
                         border=(0, 0, 0, hair), sticky="nsew",
                         width=S(8), height=S(8), padding=0)
    style.layout("Treeview.Heading", [
        ("Treeheading.cell", {"sticky": "nsew"}),
        ("Treeheading.border", {"sticky": "nsew", "children": [
            ("Treeheading.padding", {"sticky": "nsew", "children": [
                ("Treeheading.image", {"side": "right", "sticky": ""}),
                ("Treeheading.text", {"sticky": "we"})]})]})])
    style.configure("Treeview.Heading", background=inset, foreground=dim,
                    font=FONTS["small"], padding=S(8, 7, 8, 7),
                    borderwidth=0, relief="flat")
    style.map("Treeview.Heading", foreground=[("active", text)])
    style.element_create(
        "Treeitem.indicator", "image",
        im.chevron(dim, "right", box=14, pad_right=4),
        ("user2", im.blank(S(18), S(14))),
        ("user1", im.chevron(dim, "down", box=14, pad_right=4)),
        sticky="w")

    # ── Labelframe: outlined group, caption sits above the outline ──
    lf_img, lf_edge = im.box(None, p["border"], radius=8)
    style.element_create("Labelframe.border", "image", lf_img,
                         border=lf_edge, sticky="nsew",
                         **im.min_size(lf_edge))
    style.layout("TLabelframe", [("Labelframe.border", {"sticky": "nsew"})])
    style.configure("TLabelframe", background=bg, padding=S(10, 8, 10, 10),
                    labeloutside=True, labelmargins=S(4, 0, 0, 4))
    style.configure("TLabelframe.Label", background=bg, foreground=dim,
                    font=FONTS["small"])

    # ── Progressbar ──
    trough_img, pedge = im.box(inset, p["border"], radius=4)
    bar_img, bedge = im.box(accent, None, radius=4)
    style.element_create("Horizontal.Progressbar.trough", "image",
                         trough_img, border=pedge, sticky="nsew",
                         **im.min_size(pedge))
    style.element_create("Horizontal.Progressbar.pbar", "image", bar_img,
                         border=bedge, sticky="nsew", **im.min_size(bedge))
    style.layout("Horizontal.TProgressbar", [
        ("Horizontal.Progressbar.trough", {"sticky": "nsew", "children": [
            ("Horizontal.Progressbar.pbar",
             {"side": "left", "sticky": "ns"})]})])
    style.configure("Horizontal.TProgressbar", background=bg)

    # ── Separator / panes ──
    # Long thin sources + explicit min size: a 1×1 source would tile
    # once per pixel along the separator.
    hsep = im.bands("hsep", S(64), [(hair, p["border"])])
    vsep = im.bands("vsep", hair, [(S(64), p["border"])])
    style.element_create("Horizontal.Separator.separator", "image", hsep,
                         width=hair, height=hair, sticky="ew")
    style.element_create("Vertical.Separator.separator", "image", vsep,
                         width=hair, height=hair, sticky="ns")
    style.layout("TSeparator",
                 [("Horizontal.Separator.separator", {"sticky": "ew"})])
    style.layout("Horizontal.TSeparator",
                 [("Horizontal.Separator.separator", {"sticky": "ew"})])
    style.layout("Vertical.TSeparator",
                 [("Vertical.Separator.separator", {"sticky": "ns"})])
    style.configure("TPanedwindow", background=bg)
    style.configure("Sash", sashthickness=S(8), gripcount=0,
                    background=bg)

    # ── Raw Tk widgets that ttk styles never reach ──
    root.option_add("*TCombobox*Listbox.background", raised)
    root.option_add("*TCombobox*Listbox.foreground", text)
    root.option_add("*TCombobox*Listbox.selectBackground", sel_bg)
    root.option_add("*TCombobox*Listbox.selectForeground", text)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*TCombobox*Listbox.highlightThickness", 0)
    root.option_add("*TCombobox*Listbox.font", FONTS["body"])
    root.option_add("*Listbox.background", inset)
    root.option_add("*Listbox.foreground", text)
    root.option_add("*Listbox.selectBackground", sel_bg)
    root.option_add("*Listbox.selectForeground", text)
    # Class-specific on purpose: a bare "*Font" also lands on every ttk
    # widget's own -font option and silently defeats the style fonts.
    for cls in ("Label", "Button", "Entry", "Listbox", "Checkbutton",
                "Radiobutton", "Menu", "Message"):
        root.option_add(f"*{cls}.font", FONTS["body"])
    root.option_add("*Text.font", FONTS["mono"])
    root.option_add("*Canvas.background", bg)
    root.option_add("*Canvas.highlightThickness", 0)

    # A readonly combobox keeps its text "selected" after a pick, which
    # renders as a highlighted slab — clear it.
    def _clear_selection(event):
        try:
            event.widget.selection_clear()
        except Exception:
            pass
    root.bind_class("TCombobox", "<<ComboboxSelected>>", _clear_selection,
                    add="+")


def init_theme(root, *, minimal: bool = False) -> Any:
    """Install the workbench theme on ``root`` and return the ttk Style.

    ``minimal=True`` (headless bots): create nothing. Fonts stay empty
    (call sites fall back to tuples) and every dotted style name resolves
    to its stock base layout, so widget construction is unchanged from
    the unthemed app.
    """
    global _SCALE
    from tkinter import ttk

    style = ttk.Style(root)
    if minimal:
        return style

    try:
        dpi = float(root.winfo_fpixels("1i"))
        _SCALE = max(1.0, dpi / 96.0)
    except Exception:
        _SCALE = 1.0

    _define_fonts(root)
    if THEME_NAME in style.theme_names():
        style.theme_use(THEME_NAME)
    else:
        _build_theme(root, style)
    set_app_icon(root)
    apply_window_theme(root)
    return style
