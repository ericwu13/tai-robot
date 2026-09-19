"""Single owner of the Tk workbench palette, named styles, and fonts.

No imports from ``run_backtest`` (one-way dependency). ``init_theme``
is Tcl option-setting only and must succeed on a withdrawn root.
"""

from __future__ import annotations

from typing import Any

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

# Named ttk styles used by set_status / StatusDot / cards.
STATUS_LEVEL_STYLES: dict[str, str] = {
    "info": "Dim.TLabel",
    "ok": "Status.Ok.TLabel",
    "warn": "Status.Warn.TLabel",
    "error": "Status.Err.TLabel",
}

# Populated by init_theme (tkfont.Font objects need a live Tcl interpreter).
FONTS: dict[str, Any] = {}

_THEME_INIT = False


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

    style.configure(".", background=p["bg"], foreground=p["text"],
                    fieldbackground=p["bg_inset"], bordercolor=p["border"],
                    troughcolor=p["bg_inset"])
    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("TButton", background=p["bg_raised"], foreground=p["text"],
                    bordercolor=p["border"], focusthickness=1,
                    focuscolor=p["accent"])
    style.map("TButton",
              background=[("active", p["border"]), ("disabled", p["bg"])],
              foreground=[("disabled", p["text_dim"])])
    style.configure("TEntry", fieldbackground=p["bg_inset"],
                    foreground=p["text"], insertcolor=p["text"],
                    bordercolor=p["border"])
    style.configure("TCombobox", fieldbackground=p["bg_inset"],
                    foreground=p["text"], background=p["bg_raised"],
                    arrowcolor=p["text"])
    style.map("TCombobox",
              fieldbackground=[("readonly", p["bg_inset"])],
              foreground=[("readonly", p["text"])])
    style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
    style.configure("TNotebook.Tab", background=p["bg_raised"],
                    foreground=p["text_dim"], padding=(10, 4))
    style.map("TNotebook.Tab",
              background=[("selected", p["bg_inset"])],
              foreground=[("selected", p["accent"])])
    style.configure("Treeview", background=p["bg_inset"],
                    foreground=p["text"], fieldbackground=p["bg_inset"],
                    bordercolor=p["border"], rowheight=22)
    style.configure("Treeview.Heading", background=p["bg_raised"],
                    foreground=p["text"], bordercolor=p["border"])
    style.map("Treeview",
              background=[("selected", p["accent"])],
              foreground=[("selected", p["bg"])])
    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
    style.configure("TLabelframe.Label", background=p["bg"],
                    foreground=p["text_dim"])
    style.configure("TPanedwindow", background=p["bg"])
    style.configure("TScrollbar", background=p["bg_raised"],
                    troughcolor=p["bg_inset"], bordercolor=p["border"],
                    arrowcolor=p["text"])
    style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
    style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])

    style.configure("Card.TFrame", background=p["bg_raised"],
                    relief="solid", borderwidth=1)
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
    style.configure("StatusStrip.TFrame", background=p["bg_raised"])

    FONTS.clear()
    FONTS["title"] = tkfont.Font(root=root, family="Segoe UI", size=16, weight="bold")
    FONTS["section"] = tkfont.Font(root=root, family="Segoe UI", size=13, weight="bold")
    FONTS["value"] = tkfont.Font(root=root, family="Segoe UI", size=11, weight="bold")
    FONTS["body"] = tkfont.Font(root=root, family="Segoe UI", size=10)
    FONTS["mono"] = tkfont.Font(root=root, family="Consolas", size=10)
    FONTS["mono_small"] = tkfont.Font(root=root, family="Consolas", size=9)
    FONTS["mono_bold"] = tkfont.Font(root=root, family="Consolas", size=10, weight="bold")

    _THEME_INIT = True
    return style
