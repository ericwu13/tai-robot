"""Single owner of the Tk workbench palette, named styles, and fonts.

No imports from ``run_backtest`` (one-way dependency). ``init_theme``
is Tcl option-setting only and must succeed on a withdrawn root.
"""

from __future__ import annotations

from typing import Any

# Plan §7 token table. text_dim (#8a8a94) is ≥4.5:1 on bg / bg_raised /
# bg_inset (5.28 / 4.85 / 5.47) so the values stay as specified.
PALETTE: dict[str, str] = {
    "bg": "#16161a",
    "bg_raised": "#1e1e24",
    "bg_inset": "#121216",
    "border": "#2e2e36",
    "text": "#d6d6dc",
    "text_dim": "#8a8a94",
    "accent": "#4f8cff",
    "ok": "#2ecc8f",
    "warn": "#f0b429",
    "err": "#ef5350",
    "info": "#8ab4f8",
    "up": "#26a69a",
    "down": "#ef5350",
}

# Hover / pressed surfaces derived from the same tokens (not new hues).
_HOVER = "#2a2a32"          # raised + border blend
_DISABLED_BG = "#1a1a1e"    # sits on bg without vanishing
_DISABLED_FG = "#5c5c66"

# Report / regime card tones. Values follow the dark-theme pair (plan §7),
# not the old light-theme #0f6e56 / #a32d2d literals.
TONE: dict[str, str] = {
    "good": PALETTE["ok"],
    "bad": PALETTE["err"],
}

CHAT_TAGS: dict[str, dict[str, str]] = {
    "user": {"foreground": "#569cd6"},
    "assistant": {"foreground": "#d4d4d4"},
    "code": {"foreground": "#ce9178"},
    "error": {"foreground": "#f44747"},
    "system": {"foreground": "#6a9955"},
}

LOG_TAGS: dict[str, dict[str, str]] = {
    "entry": {"foreground": "#4caf50"},
    "exit": {"foreground": "#f44336"},
    "bar": {"foreground": "#90caf9"},
    "status": {"foreground": "#ffc107"},
}

# Vote-liveness dots: unknown / stale / fresh → dim / err / ok.
DOT_COLORS: dict[str, str] = {
    "off": PALETTE["text_dim"],
    "err": PALETTE["err"],
    "ok": PALETTE["ok"],
    "warn": PALETTE["warn"],
}

# Regime episode Treeview bands (dark-theme equivalents of the old
# light greens/reds/greys).
EPISODE_TAGS: dict[str, dict[str, str]] = {
    "ep_long": {"background": "#0d3b2e", "foreground": PALETTE["ok"]},
    "ep_short": {"background": "#3b1512", "foreground": PALETTE["err"]},
    "ep_idle": {"background": "#2a2a30", "foreground": PALETTE["text_dim"]},
    "ep_unknown": {"background": "#1e1e3a", "foreground": PALETTE["info"]},
    "day": {"foreground": PALETTE["text_dim"]},
}

# set_status styles sit on the raised status strip — using Dim.TLabel
# (bg token) punched a darker hole in the strip.
STATUS_LEVEL_STYLES: dict[str, str] = {
    "info": "StatusStrip.Dim.TLabel",
    "ok": "StatusStrip.Ok.TLabel",
    "warn": "StatusStrip.Warn.TLabel",
    "error": "StatusStrip.Err.TLabel",
}

# Populated by init_theme (tkfont.Font objects need a live Tcl interpreter).
FONTS: dict[str, Any] = {}

_THEME_INIT = False

_UI_FAMILIES = (
    "Segoe UI", "Microsoft JhengHei UI", "Microsoft JhengHei",
    "Noto Sans CJK TC", "Noto Sans", "DejaVu Sans", "sans-serif",
)
_MONO_FAMILIES = (
    "Consolas", "Cascadia Mono", "DejaVu Sans Mono", "monospace",
)


def _pick_family(root, candidates: tuple[str, ...]) -> str:
    from tkinter import font as tkfont
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


def _flat_colors(p: dict[str, str], fill: str) -> dict[str, str]:
    """Kill clam's system light/dark bevel (the white-flash source)."""
    return {
        "background": fill,
        "lightcolor": fill,
        "darkcolor": fill,
        "bordercolor": p["border"],
        "focuscolor": p["accent"],
    }


def style_text_widget(widget, *, inset: bool = True) -> None:
    """Apply inset-well colors to a raw Tk Text / Listbox."""
    p = PALETTE
    bg = p["bg_inset"] if inset else p["bg_raised"]
    widget.configure(
        bg=bg, fg=p["text"],
        insertbackground=p["text"],
        selectbackground=p["accent"],
        selectforeground=p["text"],
        highlightthickness=1,
        highlightbackground=p["border"],
        highlightcolor=p["accent"],
        relief="flat", borderwidth=0,
        insertwidth=2,
    )


def apply_window_theme(win) -> None:
    """Paint a Toplevel so it does not flash the system light chrome."""
    p = PALETTE
    try:
        win.configure(bg=p["bg"])
    except Exception:
        pass


def init_theme(root) -> Any:
    """Apply the clam-based dark theme to ``root`` and return the Style.

    Safe on a withdrawn / unmapped root — only Tcl option database and
    ``root.configure``. Scaling uses ``winfo_fpixels('1i')`` which does
    not require the window to be mapped.
    """
    global _THEME_INIT
    from tkinter import ttk
    from tkinter import font as tkfont

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    try:
        dpi = float(root.winfo_fpixels("1i"))
        if dpi > 0:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass

    p = PALETTE
    root.configure(bg=p["bg"])

    # Combobox popdown is a raw Tk Listbox — clam never themes it.
    # Without this the dropdown is a white flash on a dark window.
    root.option_add("*TCombobox*Listbox.background", p["bg_inset"])
    root.option_add("*TCombobox*Listbox.foreground", p["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", p["text"])
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*Listbox.background", p["bg_inset"])
    root.option_add("*Listbox.foreground", p["text"])
    root.option_add("*Listbox.selectBackground", p["accent"])
    root.option_add("*Listbox.selectForeground", p["text"])

    style.configure(".",
                    foreground=p["text"],
                    fieldbackground=p["bg_inset"],
                    troughcolor=p["bg_inset"],
                    **_flat_colors(p, p["bg"]))

    style.configure("TFrame", **_flat_colors(p, p["bg"]))
    style.configure("TLabel", background=p["bg"], foreground=p["text"],
                    lightcolor=p["bg"], darkcolor=p["bg"],
                    bordercolor=p["bg"])
    style.configure("TButton",
                    foreground=p["text"],
                    padding=(10, 5),
                    focusthickness=1,
                    **_flat_colors(p, p["bg_raised"]))
    style.map("TButton",
              background=[("pressed", p["bg_inset"]),
                          ("active", _HOVER),
                          ("disabled", _DISABLED_BG)],
              foreground=[("disabled", _DISABLED_FG)],
              lightcolor=[("pressed", p["bg_inset"]),
                          ("active", _HOVER),
                          ("disabled", _DISABLED_BG)],
              darkcolor=[("pressed", p["bg_inset"]),
                         ("active", _HOVER),
                         ("disabled", _DISABLED_BG)],
              bordercolor=[("focus", p["accent"]),
                           ("disabled", p["border"])],
              focuscolor=[("focus", p["accent"])])

    style.configure("TEntry",
                    fieldbackground=p["bg_inset"],
                    foreground=p["text"],
                    insertcolor=p["text"],
                    padding=(6, 4),
                    **_flat_colors(p, p["bg_inset"]))
    style.map("TEntry",
              fieldbackground=[("disabled", _DISABLED_BG),
                               ("focus", p["bg_inset"])],
              foreground=[("disabled", _DISABLED_FG)],
              bordercolor=[("focus", p["accent"])],
              lightcolor=[("focus", p["accent"])],
              darkcolor=[("focus", p["accent"])])

    style.configure("TCombobox",
                    fieldbackground=p["bg_inset"],
                    foreground=p["text"],
                    arrowcolor=p["text"],
                    padding=(6, 3),
                    **_flat_colors(p, p["bg_inset"]))
    style.map("TCombobox",
              fieldbackground=[("readonly", p["bg_inset"]),
                               ("disabled", _DISABLED_BG)],
              foreground=[("readonly", p["text"]),
                          ("disabled", _DISABLED_FG)],
              background=[("readonly", p["bg_inset"]),
                          ("disabled", _DISABLED_BG)],
              arrowcolor=[("disabled", _DISABLED_FG)],
              bordercolor=[("focus", p["accent"])],
              lightcolor=[("focus", p["accent"])],
              darkcolor=[("focus", p["accent"])])

    style.configure("TNotebook",
                    tabmargins=(4, 6, 4, 0),
                    **_flat_colors(p, p["bg"]))
    style.configure("TNotebook.Tab",
                    foreground=p["text_dim"],
                    padding=(12, 6),
                    **_flat_colors(p, p["bg_raised"]))
    style.map("TNotebook.Tab",
              background=[("selected", p["bg"]),
                          ("active", _HOVER)],
              foreground=[("selected", p["text"]),
                          ("active", p["text"])],
              lightcolor=[("selected", p["bg"]),
                          ("active", _HOVER)],
              darkcolor=[("selected", p["bg"]),
                         ("active", _HOVER)],
              bordercolor=[("selected", p["accent"])])

    style.configure("Treeview",
                    fieldbackground=p["bg_inset"],
                    foreground=p["text"],
                    rowheight=24,
                    **_flat_colors(p, p["bg_inset"]))
    style.configure("Treeview.Heading",
                    foreground=p["text"],
                    padding=(6, 4),
                    **_flat_colors(p, p["bg_raised"]))
    style.map("Treeview",
              background=[("selected", p["accent"])],
              foreground=[("selected", p["text"])])
    style.map("Treeview.Heading",
              background=[("active", _HOVER)],
              lightcolor=[("active", _HOVER)],
              darkcolor=[("active", _HOVER)])

    style.configure("TLabelframe",
                    relief="groove",
                    borderwidth=1,
                    **_flat_colors(p, p["bg"]))
    style.configure("TLabelframe.Label",
                    background=p["bg"],
                    foreground=p["text_dim"],
                    lightcolor=p["bg"],
                    darkcolor=p["bg"])

    style.configure("TPanedwindow", **_flat_colors(p, p["bg"]))
    style.configure("Sash", sashthickness=6, **_flat_colors(p, p["border"]))

    for name in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name,
                        troughcolor=p["bg_inset"],
                        arrowcolor=p["text_dim"],
                        arrowsize=13,
                        **_flat_colors(p, p["bg_raised"]))
        style.map(name,
                  background=[("active", _HOVER), ("disabled", p["bg"])],
                  arrowcolor=[("disabled", _DISABLED_FG)],
                  lightcolor=[("active", _HOVER)],
                  darkcolor=[("active", _HOVER)])

    style.configure("TCheckbutton",
                    background=p["bg"],
                    foreground=p["text"],
                    indicatorcolor=p["bg_inset"],
                    padding=2)
    style.map("TCheckbutton",
              background=[("active", p["bg"])],
              foreground=[("disabled", _DISABLED_FG)],
              indicatorcolor=[("selected", p["accent"]),
                              ("pressed", p["accent"])])
    style.configure("TRadiobutton",
                    background=p["bg"],
                    foreground=p["text"],
                    indicatorcolor=p["bg_inset"],
                    padding=2)
    style.map("TRadiobutton",
              background=[("active", p["bg"])],
              foreground=[("disabled", _DISABLED_FG)],
              indicatorcolor=[("selected", p["accent"]),
                              ("pressed", p["accent"])])

    style.configure("TProgressbar",
                    troughcolor=p["bg_inset"],
                    **_flat_colors(p, p["accent"]))
    style.configure("Horizontal.TProgressbar",
                    troughcolor=p["bg_inset"],
                    **_flat_colors(p, p["accent"]))

    # Cards sit on bg_raised — child labels must share that fill or they
    # punch a bg-colored hole through the card.
    style.configure("Card.TFrame", **_flat_colors(p, p["bg_raised"]))
    style.configure("Card.TLabel", background=p["bg_raised"],
                    foreground=p["text"])
    style.configure("Card.Dim.TLabel", background=p["bg_raised"],
                    foreground=p["text_dim"])
    style.configure("CardValue.TLabel", background=p["bg_raised"],
                    foreground=p["text"])

    style.configure("Status.Ok.TLabel", background=p["bg"],
                    foreground=p["ok"])
    style.configure("Status.Warn.TLabel", background=p["bg"],
                    foreground=p["warn"])
    style.configure("Status.Err.TLabel", background=p["bg"],
                    foreground=p["err"])
    style.configure("Dim.TLabel", background=p["bg"],
                    foreground=p["text_dim"])

    style.configure("StatusStrip.TFrame", **_flat_colors(p, p["bg_raised"]))
    style.configure("StatusStrip.TLabel", background=p["bg_raised"],
                    foreground=p["text"])
    style.configure("StatusStrip.Dim.TLabel", background=p["bg_raised"],
                    foreground=p["text_dim"])
    style.configure("StatusStrip.Ok.TLabel", background=p["bg_raised"],
                    foreground=p["ok"])
    style.configure("StatusStrip.Warn.TLabel", background=p["bg_raised"],
                    foreground=p["warn"])
    style.configure("StatusStrip.Err.TLabel", background=p["bg_raised"],
                    foreground=p["err"])

    ui = _pick_family(root, _UI_FAMILIES)
    mono = _pick_family(root, _MONO_FAMILIES)
    FONTS.clear()
    FONTS["title"] = tkfont.Font(root=root, family=ui, size=16, weight="bold")
    FONTS["section"] = tkfont.Font(root=root, family=ui, size=13, weight="bold")
    FONTS["value"] = tkfont.Font(root=root, family=ui, size=11, weight="bold")
    FONTS["body"] = tkfont.Font(root=root, family=ui, size=10)
    FONTS["mono"] = tkfont.Font(root=root, family=mono, size=10)
    FONTS["mono_small"] = tkfont.Font(root=root, family=mono, size=9)
    FONTS["mono_bold"] = tkfont.Font(root=root, family=mono, size=10, weight="bold")

    _THEME_INIT = True
    return style
