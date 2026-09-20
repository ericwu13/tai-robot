"""Reusable Tk widgets for the workbench (Phase 1).

Imports ``src.ui.theme`` only — never ``run_backtest``.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from src.ui.theme import (
    CHAT_TAGS, EMPTY_FG, FONTS, LOG_TAGS, PALETTE, style_text_widget,
)


class ScrollableFrame(ttk.Frame):
    """One blessed Canvas + scrollbar + inner-frame + mousewheel rig.

    Binds ``<MouseWheel>`` on ``<Enter>``, unbinds on ``<Leave>`` and
    ``destroy()`` so ``bind_all`` cannot leak across tab switches.
    """

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        bg = PALETTE["bg"]
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg,
                                borderwidth=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body,
                                              anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)
        self.bind("<Destroy>", self._on_destroy)
        self._wheel_bound = False

    def _on_linux_wheel(self, event):
        # X11 / this preview host: Button-4 up, Button-5 down.
        self.canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

    def _on_body_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _on_mousewheel(self, event):
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta:
            self.canvas.yview_scroll(delta, "units")

    def _bind_wheel(self, _event=None):
        if self._wheel_bound:
            return
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_linux_wheel)
        self.canvas.bind_all("<Button-5>", self._on_linux_wheel)
        self._wheel_bound = True

    def _unbind_wheel(self, _event=None):
        if not self._wheel_bound:
            return
        try:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")
        except tk.TclError:
            pass
        self._wheel_bound = False

    def _on_destroy(self, event):
        if event.widget is self:
            self._unbind_wheel()


def attach_tooltip(widget, text: str) -> None:
    """Lightweight hover tooltip using the dark raised/border palette."""
    state = {"tip": None}

    def show(_event=None):
        if state["tip"] is not None:
            return
        try:
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + widget.winfo_height() + 4
        except tk.TclError:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tip, text=text,
            background=PALETTE["bg_raised"],
            foreground=PALETTE["text"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            relief=tk.SOLID, borderwidth=1,
            font=FONTS.get("mono_small") or ("", 9),
            justify=tk.LEFT, padx=6, pady=3,
        ).pack()
        state["tip"] = tip

    def hide(_event=None):
        if state["tip"] is not None:
            try:
                state["tip"].destroy()
            except tk.TclError:
                pass
            state["tip"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)


def themed_scrolled_text(parent, *, wrap=tk.WORD, font=None, inset: bool = True,
                         **text_kw):
    """Text + ttk.Scrollbar well. Returns ``(frame, text)``.

    ``scrolledtext.ScrolledText`` ships a raw ``tk.Scrollbar`` that stays
    the system light chrome — this helper is the dark-theme replacement.
    """
    frame = ttk.Frame(parent)
    text = tk.Text(
        frame, wrap=wrap,
        font=font or FONTS.get("mono") or ("Consolas", 10),
        padx=6, pady=4, **text_kw,
    )
    style_text_widget(text, inset=inset)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=vsb.set)
    if wrap == tk.NONE:
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(xscrollcommand=hsb.set)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    return frame, text


class StatusDot(ttk.Label):
    """Colored ● plus optional label. States: ok / warn / err / off.

    ``surface`` picks the fill so the dot does not punch a hole through
    a raised strip (``"raised"``) or a card (``"card"``). Default
    ``"bg"`` matches window / LabelFrame backgrounds.
    """

    _STATE_TO_KEY = {
        "ok": "ok",
        "warn": "warn",
        "err": "err",
        "error": "err",
        "off": "off",
    }
    _STYLES = {
        "bg": {
            "ok": "Status.Ok.TLabel",
            "warn": "Status.Warn.TLabel",
            "err": "Status.Err.TLabel",
            "off": "Dim.TLabel",
        },
        "raised": {
            "ok": "StatusStrip.Ok.TLabel",
            "warn": "StatusStrip.Warn.TLabel",
            "err": "StatusStrip.Err.TLabel",
            "off": "StatusStrip.Dim.TLabel",
        },
        "card": {
            "ok": "Status.Ok.TLabel",
            "warn": "Status.Warn.TLabel",
            "err": "Status.Err.TLabel",
            "off": "Card.Dim.TLabel",
        },
    }

    def __init__(self, parent, text: str = "", *, surface: str = "bg", **kwargs):
        self._label = text
        self._state = "off"
        self._surface = surface if surface in self._STYLES else "bg"
        if "style" not in kwargs:
            kwargs["style"] = self._style_for("off")
        super().__init__(parent, text=self._render("off", text), **kwargs)

    def _style_for(self, key: str) -> str:
        return self._STYLES[self._surface][key]

    @staticmethod
    def _render(state: str, label: str) -> str:
        return f"● {label}" if label else "●"

    def set_state(self, state: str) -> None:
        key = self._STATE_TO_KEY.get(state, "off")
        self._state = key
        self.configure(text=self._render(key, self._label),
                       style=self._style_for(key))

    def set_label(self, text: str) -> None:
        self._label = text
        self.set_state(self._state)


class TagTextLog(ttk.Frame):
    """Read-only dark Text well with tag palette, cap, filter, search, pause.

    ``append`` must be called on the Tk thread. Producers on other threads
    must enqueue through ``_ui_queue`` (documented by the off-thread raise).
    """

    def __init__(
        self,
        parent,
        *,
        tags: dict | None = None,
        levels: tuple[str, ...] = ("info", "debug"),
        max_lines: int = 5000,
        show_toolbar: bool = False,
        placeholder: str | None = None,
        bg: str | None = None,
        fg: str | None = None,
        font=None,
        **kwargs,
    ):
        super().__init__(parent, **kwargs)
        self.max_lines = int(max_lines)
        self._paused = False
        self._levels = levels
        self._bg = bg or PALETTE["bg_inset"]
        self._fg = fg or PALETTE["text"]
        self._font = font or FONTS.get("mono_small") or ("Consolas", 9)

        if show_toolbar:
            bar = ttk.Frame(self)
            bar.pack(fill=tk.X, padx=2, pady=(2, 0))
            ttk.Label(bar, text="Filter:", style="Dim.TLabel").pack(
                side=tk.LEFT, padx=(2, 4))
            self._filter_var = tk.StringVar(value="All")
            values = ["All"]
            if "info" in levels:
                values.append("Info")
            if "debug" in levels:
                values.append("Debug")
            combo = ttk.Combobox(
                bar, textvariable=self._filter_var, values=values,
                state="readonly", width=8)
            combo.pack(side=tk.LEFT, padx=(0, 8))
            combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())
            ttk.Label(bar, text="Search:", style="Dim.TLabel").pack(
                side=tk.LEFT, padx=(0, 4))
            self._search_var = tk.StringVar()
            search = ttk.Entry(bar, textvariable=self._search_var, width=24)
            search.pack(side=tk.LEFT, padx=(0, 8))
            search.bind("<KeyRelease>", lambda _e: self._apply_search())
            self._pause_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                bar, text="暫停 Pause", variable=self._pause_var,
                command=self._on_pause_toggle,
            ).pack(side=tk.LEFT)
        else:
            self._filter_var = tk.StringVar(value="All")
            self._search_var = tk.StringVar(value="")
            self._pause_var = tk.BooleanVar(value=False)

        wrap, self.text = themed_scrolled_text(
            self, wrap=tk.WORD, font=self._font, inset=True,
        )
        wrap.pack(fill=tk.BOTH, expand=True)
        self.text.configure(bg=self._bg, fg=self._fg, state=tk.DISABLED)
        self._placeholder = placeholder or ""
        self._ph_label = None
        if self._placeholder:
            # Overlay (not buffer text) so get()/save paths stay empty and
            # the well fill is not punched by a Dim.TLabel hole.
            self._ph_label = tk.Label(
                self.text, text=self._placeholder,
                bg=self._bg, fg=EMPTY_FG,
                font=FONTS.get("body") or ("", 10),
                justify=tk.CENTER, bd=0, highlightthickness=0,
            )
            self._show_placeholder()
        self.text.tag_configure("search_hit", background=PALETTE["accent"],
                                foreground=PALETTE["bg"])
        self.text.tag_configure("info", foreground=self._fg)
        self.text.tag_configure("debug", foreground=PALETTE["text_dim"])

        palette = dict(CHAT_TAGS)
        palette.update(LOG_TAGS)
        if tags:
            palette.update(tags)
        for name, opts in palette.items():
            self.text.tag_configure(name, **opts)

    # ── Tk-thread-only write path ──

    def append(self, line: str, tag: str = "info") -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "TagTextLog.append must be called from the Tk thread; "
                "producers must go through _ui_queue")
        if not line.endswith("\n"):
            line = line + "\n"
        self._hide_placeholder()
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, line, tag)
        self._trim()
        if not self._paused:
            self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)
        self._apply_filter()

    def clear(self) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        if self._ph_label is None:
            return
        try:
            self._ph_label.place(relx=0.5, rely=0.42, anchor=tk.CENTER)
        except tk.TclError:
            pass

    def _hide_placeholder(self) -> None:
        if self._ph_label is None:
            return
        try:
            self._ph_label.place_forget()
        except tk.TclError:
            pass

    def _maybe_restore_placeholder(self) -> None:
        if self._ph_label is None:
            return
        raw = self.text.get("1.0", "end-1c").strip()
        if not raw:
            self._show_placeholder()
        else:
            self._hide_placeholder()

    def _trim(self) -> None:
        raw = self.text.get("1.0", "end-1c")
        n = len(raw.splitlines())
        extra = n - self.max_lines
        if extra > 0:
            self.text.delete("1.0", f"{extra + 1}.0")

    def _apply_filter(self) -> None:
        choice = (self._filter_var.get() or "All").lower()
        # Info hides debug-tagged lines; All/Debug show everything.
        elide_debug = choice == "info"
        try:
            self.text.tag_configure("debug", elide=elide_debug)
        except tk.TclError:
            pass

    def _apply_search(self) -> None:
        self.text.tag_remove("search_hit", "1.0", tk.END)
        needle = self._search_var.get()
        if not needle:
            return
        start = "1.0"
        while True:
            pos = self.text.search(needle, start, tk.END, nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(needle)}c"
            self.text.tag_add("search_hit", pos, end)
            start = end

    def _on_pause_toggle(self) -> None:
        self._paused = bool(self._pause_var.get())
        if not self._paused:
            self.text.see(tk.END)

    # Proxy common Text methods so existing chat/log call sites keep working.
    def config(self, *args, **kwargs):
        return self.text.config(*args, **kwargs)

    configure = config

    def insert(self, *args, **kwargs):
        self._hide_placeholder()
        return self.text.insert(*args, **kwargs)

    def delete(self, *args, **kwargs):
        result = self.text.delete(*args, **kwargs)
        self._maybe_restore_placeholder()
        return result

    def get(self, *args, **kwargs):
        return self.text.get(*args, **kwargs)

    def see(self, *args, **kwargs):
        return self.text.see(*args, **kwargs)

    def tag_configure(self, *args, **kwargs):
        return self.text.tag_configure(*args, **kwargs)

    def mark_set(self, *args, **kwargs):
        return self.text.mark_set(*args, **kwargs)

    def mark_unset(self, *args, **kwargs):
        return self.text.mark_unset(*args, **kwargs)

    def mark_gravity(self, *args, **kwargs):
        return self.text.mark_gravity(*args, **kwargs)

    def index(self, *args, **kwargs):
        return self.text.index(*args, **kwargs)
