"""Reusable Tk widgets for the workbench.

Imports ``src.ui.theme`` only — never ``run_backtest``.
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from tkinter import ttk

from src.ui.theme import (
    CHAT_TAGS, EMPTY_FG, FONTS, LOG_TAGS, PALETTE, S, TONE, style_text_widget,
)
from src.ui.raster import mix


class ScrollableFrame(ttk.Frame):
    """One blessed Canvas + scrollbar + inner-frame + mousewheel rig.

    Binds ``<MouseWheel>`` on ``<Enter>``, unbinds on ``<Leave>`` and
    ``destroy()`` so ``bind_all`` cannot leak across tab switches.
    """

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0,
                                bg=PALETTE["bg"], borderwidth=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(
            yscrollcommand=autohide_scrollbar(vsb, "Vertical.TScrollbar"))
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
        self.canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

    def _on_body_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _on_mousewheel(self, event):
        # Content shorter than the viewport must not scroll into blank space.
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
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


class FlowFrame(ttk.Frame):
    """Left-to-right layout that wraps onto extra rows when too narrow.

    ``add(widget)`` registers a child (created with this frame as parent).
    ``add_spacer()`` right-aligns everything after it while that tail still
    fits on the current row; otherwise the tail wraps like any other item.

    Children are positioned with ``place`` — a grid would share column
    widths across the wrapped rows and tear the rows apart.
    """

    _SPACER = object()

    def __init__(self, parent, *, gap: int = 6, row_gap: int = 6, **kwargs):
        super().__init__(parent, **kwargs)
        self._items: list = []
        self._gap = gap
        self._row_gap = row_gap
        self._last_width = -1
        self._child_sizes: dict = {}
        self._reflow_pending = False
        self._layout_width = 0
        self.bind("<Configure>", self._on_configure)

    def add(self, widget, *, gap: int | None = None):
        self._items.append((widget, self._gap if gap is None else gap))
        self._last_width = -1
        self._set_height(self._natural_height())
        # A child whose text changes later (status dot, Deploy↔Stop) resizes
        # itself; its neighbours must move or they overlap.
        widget.bind("<Configure>", self._on_child_configure, add="+")
        return widget

    def _on_child_configure(self, event) -> None:
        size = (event.width, event.height)
        if self._child_sizes.get(event.widget) == size:
            return
        self._child_sizes[event.widget] = size
        if not self._reflow_pending:
            self._reflow_pending = True
            self.after_idle(self._deferred_reflow)

    def _deferred_reflow(self) -> None:
        self._reflow_pending = False
        try:
            self.reflow()
        except tk.TclError:
            pass  # destroyed while the idle callback was queued

    def add_spacer(self) -> None:
        self._items.append((self._SPACER, 0))

    def add_separator(self):
        sep = ttk.Separator(self, orient=tk.VERTICAL)
        self._items.append((sep, self._gap))
        return sep

    def _natural_height(self) -> int:
        heights = [w.winfo_reqheight() for w, _ in self._items
                   if w is not self._SPACER and not isinstance(w, ttk.Separator)]
        return max(heights) if heights else 1

    def _set_height(self, height: int) -> None:
        try:
            if int(self.cget("height")) != height:
                self.configure(height=height)
        except (tk.TclError, ValueError):
            pass

    def _on_configure(self, event) -> None:
        # Gate on width: placing children changes the height, which fires
        # <Configure> again.
        if event.width == self._last_width:
            return
        self._last_width = event.width
        self.reflow(event.width)

    def reflow(self, width: int | None = None) -> None:
        if width is None:
            width = self.winfo_width()
        if width <= 1:
            # Not mapped yet (or withdrawn): reuse the last real width
            # rather than collapsing everything into a 1-px column.
            width = self._layout_width
        if width <= 1:
            return
        self._layout_width = width

        def size(widget):
            if isinstance(widget, ttk.Separator):
                return max(1, widget.winfo_reqwidth()), 0
            return widget.winfo_reqwidth(), widget.winfo_reqheight()

        # rows: list of (left_items, right_items); item = (widget, gap, w, h)
        rows = [([], [])]
        x = 0
        pending_spacer = False
        for index, (widget, gap) in enumerate(self._items):
            if widget is self._SPACER:
                tail = [(w, g) for w, g in self._items[index + 1:]
                        if w is not self._SPACER]
                tail_w = sum(size(w)[0] + S(g) for w, g in tail)
                pending_spacer = x + tail_w <= width
                continue
            w_px, h_px = size(widget)
            need = w_px + S(gap)
            left, right = rows[-1]
            if pending_spacer:
                right.append((widget, gap, w_px, h_px))
                continue
            if left and x + w_px > width:
                rows.append(([], []))
                left, right = rows[-1]
                x = 0
            left.append((widget, gap, w_px, h_px))
            x += need

        y = 0
        for left, right in rows:
            items = left + right
            row_h = max((h for _, _, _, h in items), default=0) or \
                self._natural_height()
            # A separator that would end or start a row is just noise.
            while left and isinstance(left[-1][0], ttk.Separator) and not right:
                left[-1][0].place_forget()
                left = left[:-1]
            while left and isinstance(left[0][0], ttk.Separator):
                left[0][0].place_forget()
                left = left[1:]
            cx = 0
            for widget, gap, w_px, h_px in left:
                self._place(widget, cx, y, w_px, h_px, row_h)
                cx += w_px + S(gap)
            if right:
                total = sum(w + S(g) for _, g, w, _ in right) - S(right[-1][1])
                cx = max(cx, width - total)
                for widget, gap, w_px, h_px in right:
                    self._place(widget, cx, y, w_px, h_px, row_h)
                    cx += w_px + S(gap)
            y += row_h + S(self._row_gap)
        self._set_height(max(1, y - S(self._row_gap)))

    @staticmethod
    def _place(widget, x, y, w_px, h_px, row_h) -> None:
        if isinstance(widget, ttk.Separator):
            inset = max(1, int(row_h * 0.22))
            widget.place(x=x, y=y + inset, width=w_px,
                         height=max(1, row_h - 2 * inset))
        else:
            widget.place(x=x, y=y + (row_h - h_px) // 2)


def work_area(widget) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the usable area of ``widget``'s monitor.

    Excludes the taskbar and follows the monitor the widget is actually on;
    falls back to the primary screen where the Win32 call is unavailable.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD),
                            ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
            monitor = user32.MonitorFromWindow(hwnd, 2)  # DEFAULTTONEAREST
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcWork
                if r.right > r.left and r.bottom > r.top:
                    return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def fit_dialog(w: int, h: int, parent_box: tuple[int, int, int, int],
               area: tuple[int, int, int, int], *, frame: int = 0,
               caption: int = 0) -> tuple[int, int, int, int]:
    """Pure geometry: size ``w``x``h`` centered over ``parent_box``
    (x, y, width, height), shrunk and shifted to stay inside ``area``.

    ``frame``/``caption`` reserve room for the native window border and
    title bar, which Tk's client-area geometry does not include.
    """
    left, top, right, bottom = area
    w = max(1, min(w, right - left - 2 * frame))
    h = max(1, min(h, bottom - top - caption - frame))
    px, py, pw, ph = parent_box
    x = px + (pw - w) // 2
    y = py + (ph - h) // 3
    x = max(left + frame, min(x, right - frame - w))
    y = max(top + caption, min(y, bottom - frame - h))
    return w, h, x, y


def place_dialog(win, parent, width: int | None = None,
                 height: int | None = None) -> None:
    """Size (logical px, optional) and center a Toplevel over its parent,
    never larger than — or outside — the parent monitor's work area.

    The process is DPI-aware, so a fixed logical size can exceed a small
    scaled screen (820x740 at 150 % is taller than a 1080p work area);
    dialog bodies scroll or expand, so shrinking is harmless, while
    off-screen commit buttons are not.
    """
    try:
        win.update_idletasks()
        w = S(width) if width else win.winfo_reqwidth()
        h = S(height) if height else win.winfo_reqheight()
        parent_box = (parent.winfo_rootx(), parent.winfo_rooty(),
                      parent.winfo_width(), parent.winfo_height())
        mapped = parent_box[2] > 1 and parent_box[3] > 1
        area = work_area(parent if mapped else win)
        if not mapped:  # headless / early: center on the screen instead
            parent_box = (area[0], area[1], area[2] - area[0], area[3] - area[1])
        w, h, x, y = fit_dialog(w, h, parent_box, area,
                                frame=S(8), caption=S(32))
        win.geometry(f"{w}x{h}+{x}+{y}")
    except tk.TclError:
        pass


def tooltip_origin(anchor: tuple[int, int, int, int],
                   tip: tuple[int, int],
                   screen: tuple[int, int, int, int],
                   gap: int = 6) -> tuple[int, int]:
    """Top-left for a tooltip under ``anchor`` (x, y, w, h).

    Flips above the control when the below slot would leave ``screen``
    (left, top, right, bottom). A status-strip button sits on the bottom
    edge; opening under it lands off the work area, and the window
    manager clamps the tip back onto the button. That Enter/Leave loop
    is the sluggish hit on Check for Updates (issue #139 item 3).
    """
    ax, ay, _aw, ah = anchor
    tw, th = tip
    left, top, right, bottom = screen
    x = ax
    y_below = ay + ah + gap
    if y_below + th > bottom:
        y = ay - gap - th
    else:
        y = y_below
    if y < top:
        y = top
    if x + tw > right:
        x = right - tw
    if x < left:
        x = left
    return x, y


def resize_insets_from_metrics(metrics) -> tuple[int, int, int]:
    """(left, right, bottom) client pixels a thick frame claims for resize.

    ``metrics`` is ``GetSystemMetrics``. Indexes: SM_CXFRAME 32,
    SM_CYFRAME 33, SM_CXPADDEDBORDER 92, SM_CXVSCROLL 2. The padded
    border is isotropic: the same SM_CXPADDEDBORDER value is added on
    both axes. The right inset is the corner grip — at least a
    scrollbar wide — so a button packed into the bottom-right is not
    HTBOTTOMRIGHT (a window drag). Values are already device pixels.
    """
    frame_x = max(0, int(metrics(32)))
    frame_y = max(0, int(metrics(33)))
    pad_x = max(0, int(metrics(92)))
    pad_y = pad_x
    grip = max(0, int(metrics(2)))
    edge_x = frame_x + pad_x
    edge_y = frame_y + pad_y
    return (edge_x, max(edge_x, grip), edge_y)


def window_resize_insets() -> tuple[int, int, int]:
    """Resize/drag insets for this process. Zeros off Windows.

    Headless and the GUI share the builder, so this must not require a
    mapped window. A withdrawn root still gets the same padding.
    """
    if sys.platform != "win32":
        return (0, 0, 0)
    try:
        import ctypes
        return resize_insets_from_metrics(
            ctypes.windll.user32.GetSystemMetrics)
    except Exception:
        return (0, 0, 0)


# Logical (96-DPI) padding of the status strip before the OS drag inset.
# Top/bottom are a step roomier than the old 3px so the ghost buttons
# have a real hit box once the dead resize band is added underneath.
_STATUS_STRIP_BASE = (16, 6, 8, 6)


def status_strip_padding(insets: tuple[int, int, int]) -> tuple[int, int, int, int]:
    """Padding that keeps the status-strip buttons out of the drag band.

    ``insets`` is ``window_resize_insets()``: (left, right, bottom)
    device pixels. Right and bottom are added to the logical base so
    History / Report Issue / Check for Updates are HTCLIENT (issue #139
    item 3). The left inset is not applied — the status message stays
    on the strip's left rhythm.
    """
    _left, right, bottom = insets
    base_l, base_t, base_r, base_b = S(*_STATUS_STRIP_BASE)
    return (
        base_l,
        base_t,
        base_r + max(0, int(right)),
        base_b + max(0, int(bottom)),
    )


def attach_tooltip(widget, text: str) -> None:
    """Lightweight hover tooltip on the raised surface."""
    state = {"tip": None}

    def show(_event=None):
        if state["tip"] is not None:
            return
        tip = None
        try:
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.configure(bg=PALETTE["border_strong"])
            tk.Label(
                tip, text=text,
                background=PALETTE["bg_raised"], foreground=PALETTE["text"],
                font=FONTS.get("small") or ("", 9),
                justify=tk.LEFT, padx=S(10), pady=S(6), bd=0,
            ).pack(padx=S(1), pady=S(1))
            tip.update_idletasks()
            area = work_area(widget)
            x, y = tooltip_origin(
                (widget.winfo_rootx(), widget.winfo_rooty(),
                 max(1, widget.winfo_width()), max(1, widget.winfo_height())),
                (tip.winfo_reqwidth(), tip.winfo_reqheight()),
                area, gap=S(6))
            tip.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            if tip is not None:
                try:
                    tip.destroy()
                except tk.TclError:
                    pass
            return
        state["tip"] = tip

    def hide(_event=None):
        if state["tip"] is not None:
            try:
                state["tip"].destroy()
            except tk.TclError:
                pass
            state["tip"] = None

    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<ButtonPress>", hide, add="+")


def autohide_scrollbar(scrollbar: ttk.Scrollbar, base_style: str):
    """Return a ``yscrollcommand`` that hides the thumb while content fits.

    Swaps the scrollbar between ``base_style`` and ``Hidden.<base_style>``
    (same footprint) rather than unmapping it — no relayout, no flicker.
    """
    state = {"hidden": None}

    def on_set(first, last):
        scrollbar.set(first, last)
        try:
            hidden = float(first) <= 0.0 and float(last) >= 1.0
        except (TypeError, ValueError):
            return
        if hidden != state["hidden"]:
            state["hidden"] = hidden
            try:
                scrollbar.configure(
                    style=("Hidden." + base_style) if hidden else base_style)
            except tk.TclError:
                pass

    return on_set


def scrolled_tree(parent, **tree_kw):
    """Treeview + auto-hiding pill scrollbar in one frame → (frame, tree)."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, **tree_kw)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=autohide_scrollbar(vsb, "Vertical.TScrollbar"))
    vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=S(2, 0))
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    return frame, tree


def themed_scrolled_text(parent, *, wrap=tk.WORD, font=None, inset: bool = True,
                         focus_ring: bool = False, scrollbar: bool = True,
                         **text_kw):
    """Borderless Text + pill scrollbar inside a rounded well.

    Returns ``(frame, text)``. ``scrolledtext.ScrolledText`` ships a raw
    ``tk.Scrollbar`` that stays system-light — this is its replacement.
    ``focus_ring`` lights the well's border while the text has focus.
    """
    frame = ttk.Frame(parent, style="Well.TFrame", padding=S(3))
    body = ttk.Frame(frame, style="WellBody.TFrame")
    body.pack(fill=tk.BOTH, expand=True)
    text_kw.setdefault("padx", S(10))
    text_kw.setdefault("pady", S(8))
    text = tk.Text(
        body, wrap=wrap,
        font=font or FONTS.get("mono") or ("Consolas", 10),
        **text_kw,
    )
    style_text_widget(text, inset=inset)
    if scrollbar:
        vsb = ttk.Scrollbar(body, orient="vertical", command=text.yview,
                            style="Inset.Vertical.TScrollbar")
        text.configure(yscrollcommand=autohide_scrollbar(
            vsb, "Inset.Vertical.TScrollbar"))
        if wrap == tk.NONE:
            hsb = ttk.Scrollbar(body, orient="horizontal", command=text.xview,
                                style="Inset.Horizontal.TScrollbar")
            text.configure(xscrollcommand=autohide_scrollbar(
                hsb, "Inset.Horizontal.TScrollbar"))
            hsb.pack(side=tk.BOTTOM, fill=tk.X)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    if focus_ring:
        text.bind("<FocusIn>", lambda _e: frame.state(["focus"]), add="+")
        text.bind("<FocusOut>", lambda _e: frame.state(["!focus"]), add="+")
    return frame, text


# Hint tag inside a chat composer. Not buffer content for Send: composer_value
# returns "" while this tag is present (issue #139 item 4).
_COMPOSER_TAG = "composer_ph"
_MODIFIER_KEYSYMS = frozenset({
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Meta_L", "Meta_R", "Super_L", "Super_R", "Caps_Lock", "Num_Lock",
    "ISO_Level3_Shift", "Mode_switch",
})
# Keys that move the caret or leave the field. They must not wipe the hint.
_NAV_KEYSYMS = frozenset({
    "Left", "Right", "Up", "Down", "Home", "End", "Next", "Prior",
    "Tab", "Escape", "Shift_L", "Shift_R",
})


def _composer_showing(text) -> bool:
    try:
        return bool(text.tag_ranges(_COMPOSER_TAG))
    except tk.TclError:
        return False


def _clear_composer_placeholder(text) -> None:
    if not _composer_showing(text):
        return
    try:
        text.delete("1.0", "end")
    except tk.TclError:
        pass


def composer_value(text) -> str:
    """User text in a composer. The hint is not a message."""
    if _composer_showing(text):
        return ""
    try:
        return text.get("1.0", "end-1c").strip()
    except tk.TclError:
        return ""


def refresh_composer_placeholder(text) -> None:
    """Show the hint when the composer is empty; leave real text alone.

    Safe on a withdrawn root (headless constructs the same widgets).
    """
    hint = getattr(text, "_composer_hint", "")
    if not hint:
        return
    try:
        if _composer_showing(text):
            text.mark_set("insert", "1.0")
            return
        if text.get("1.0", "end-1c").strip():
            return
        text.delete("1.0", "end")
        text.insert("1.0", hint, (_COMPOSER_TAG,))
        text.mark_set("insert", "1.0")
    except tk.TclError:
        pass


def note_composer_key(text, keysym: str, char: str = "", state: int = 0) -> None:
    """Drop the hint before a real edit. Plain Enter leaves it.

    ``<Return>`` sends via ``composer_value``, which ignores the hint, so
    wiping it first would flash an empty box on a no-op send. Shift+Enter
    is a newline: the hint has to go first or the newline is appended to
    the prompt.
    """
    if keysym in ("Return", "KP_Enter"):
        if int(state) & 0x1:
            _clear_composer_placeholder(text)
        return
    if keysym in _MODIFIER_KEYSYMS or keysym in _NAV_KEYSYMS:
        return
    if char or keysym in ("BackSpace", "Delete"):
        _clear_composer_placeholder(text)


def install_composer_placeholder(text, placeholder: str) -> None:
    """Gray hint on the first line of ``text``, where the caret is.

    An overlay centered in the transcript sits hundreds of pixels above
    this box on a tall pane (issue #139 item 4). The hint is a tagged
    run, not a floating label, so it lines up with the caret. Clicks,
    paste, and editing keys clear it; FocusOut restores it when empty.
    """
    text._composer_hint = placeholder
    try:
        text.tag_configure(_COMPOSER_TAG, foreground=PALETTE["text_dim"])
    except tk.TclError:
        return

    def on_key(event, widget=text):
        note_composer_key(
            widget, str(getattr(event, "keysym", "")),
            getattr(event, "char", "") or "",
            int(getattr(event, "state", 0) or 0))

    def on_click(_event=None, widget=text):
        # A click in the middle of the hint would edit the hint itself.
        _clear_composer_placeholder(widget)

    def on_paste(_event=None, widget=text):
        _clear_composer_placeholder(widget)

    def on_focus_out(_event=None, widget=text):
        refresh_composer_placeholder(widget)

    text.bind("<Key>", on_key, add="+")
    text.bind("<Button-1>", on_click, add="+")
    text.bind("<<Paste>>", on_paste, add="+")
    text.bind("<FocusOut>", on_focus_out, add="+")
    refresh_composer_placeholder(text)


class StatusDot(ttk.Label):
    """Colored ● plus optional label. States: ok / warn / err / off.

    ``surface`` picks the style family so the label background matches
    what it sits on: ``"bg"``, ``"raised"`` (status strip / top bar) or
    ``"card"``.
    """

    _STATE_TO_KEY = {
        "ok": "ok", "warn": "warn", "err": "err", "error": "err", "off": "off",
    }
    _STYLES = {
        "bg": {
            "ok": "Status.Ok.TLabel", "warn": "Status.Warn.TLabel",
            "err": "Status.Err.TLabel", "off": "Dim.TLabel",
        },
        "raised": {
            "ok": "StatusStrip.Ok.TLabel", "warn": "StatusStrip.Warn.TLabel",
            "err": "StatusStrip.Err.TLabel", "off": "StatusStrip.Dim.TLabel",
        },
        "card": {
            "ok": "Card.Status.Ok.TLabel", "warn": "Card.Status.Warn.TLabel",
            "err": "Card.Status.Err.TLabel", "off": "Card.Dim.TLabel",
        },
    }

    def __init__(self, parent, text: str = "", *, surface: str = "bg", **kwargs):
        self._label = text
        self._state = "off"
        self._surface = surface if surface in self._STYLES else "bg"
        if "style" not in kwargs:
            kwargs["style"] = self._style_for("off")
        super().__init__(parent, text=self._render(text), **kwargs)

    def _style_for(self, key: str) -> str:
        return self._STYLES[self._surface][key]

    @staticmethod
    def _render(label: str) -> str:
        return f"●  {label}" if label else "●"

    def set_state(self, state: str) -> None:
        key = self._STATE_TO_KEY.get(state, "off")
        self._state = key
        self.configure(text=self._render(self._label),
                       style=self._style_for(key))

    def set_label(self, text: str) -> None:
        self._label = text
        self.set_state(self._state)


class StatCard(ttk.Frame):
    """Raised card: dim caption, large value, optional dim sub-line.

    ``variable`` keeps the value live; ``signed=True`` tints it green/red
    from a leading ``+`` / ``-`` (P&L fields) without the caller having
    to know about colors.
    """

    def __init__(self, parent, caption: str, *, variable=None, value: str = "",
                 sub_variable=None, sub: str | None = None,
                 signed: bool = False, small: bool = False, **kwargs):
        kwargs.setdefault("style", "Card.TFrame")
        kwargs.setdefault("padding", S(14, 10, 14, 10))
        super().__init__(parent, **kwargs)
        ttk.Label(self, text=caption, style="Card.Dim.TLabel").pack(anchor="w")
        value_style = "CardValueSmall.TLabel" if small else "CardValue.TLabel"
        self._value = ttk.Label(self, style=value_style)
        if variable is not None:
            self._value.configure(textvariable=variable)
        else:
            self._value.configure(text=value)
        self._value.pack(anchor="w", pady=S(2, 0))
        self._sub = None
        if sub_variable is not None or sub is not None:
            self._sub = ttk.Label(self, style="Card.Dim.TLabel")
            if sub_variable is not None:
                self._sub.configure(textvariable=sub_variable)
            else:
                self._sub.configure(text=sub)
            self._sub.pack(anchor="w")
        self._variable = variable
        self._trace_id = None
        if signed and variable is not None:
            self._trace_id = variable.trace_add("write", self._retint)
            self.bind("<Destroy>", self._drop_trace, add="+")
            self._retint()

    @property
    def value_label(self) -> ttk.Label:
        return self._value

    def _drop_trace(self, event) -> None:
        # The variable usually outlives the card; a trace left behind would
        # fire into a destroyed label on the next write.
        if event.widget is self and self._trace_id is not None:
            try:
                self._variable.trace_remove("write", self._trace_id)
            except (tk.TclError, ValueError):
                pass
            self._trace_id = None

    def set_tone(self, tone: str | None) -> None:
        color = TONE.get(tone) if tone else None
        self._value.configure(foreground=color or PALETTE["text"])

    def _retint(self, *_args) -> None:
        try:
            raw = str(self._variable.get()).strip()
        except tk.TclError:
            return
        digits = raw.lstrip("+-").replace(",", "").strip()
        nonzero = any(ch in "123456789" for ch in digits)
        if raw.startswith("+") and nonzero:
            self.set_tone("good")
        elif raw.startswith("-") and nonzero:
            self.set_tone("bad")
        else:
            self.set_tone(None)


def enable_row_hover(tree: ttk.Treeview) -> None:
    """Subtle hover highlight for Treeview rows.

    Uses a transient ``hover`` tag that is always removed before another
    row gets it, so code reading item tags never sees a stale one.
    """
    state = {"iid": ""}
    tree.tag_configure("hover",
                       background=mix(PALETTE["bg_inset"], PALETTE["text"], 0.06))

    def clear():
        iid = state["iid"]
        state["iid"] = ""
        if iid:
            try:
                if tree.exists(iid):
                    tags = [t for t in tree.item(iid, "tags") if t != "hover"]
                    tree.item(iid, tags=tags)
            except tk.TclError:
                pass

    def on_motion(event):
        try:
            iid = tree.identify_row(event.y)
        except tk.TclError:
            return
        if iid == state["iid"]:
            return
        clear()
        if iid:
            try:
                tags = list(tree.item(iid, "tags"))
                tree.item(iid, tags=tags + ["hover"])
                state["iid"] = iid
            except tk.TclError:
                pass

    tree.bind("<Motion>", on_motion, add="+")
    tree.bind("<Leave>", lambda _e: clear(), add="+")


class TagTextLog(ttk.Frame):
    """Read-only Text well with tag palette, cap, filter, search, pause.

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
        self._filter_var = tk.StringVar(value="All")
        self._search_var = tk.StringVar(value="")
        self._pause_var = tk.BooleanVar(value=False)

        if show_toolbar:
            bar = ttk.Frame(self)
            bar.pack(fill=tk.X, pady=S(0, 8))
            ttk.Label(bar, text="篩選 Filter", style="Dim.TLabel").pack(
                side=tk.LEFT, padx=S(0, 6))
            values = ["All"]
            if "info" in levels:
                values.append("Info")
            if "debug" in levels:
                values.append("Debug")
            combo = ttk.Combobox(
                bar, textvariable=self._filter_var, values=values,
                state="readonly", width=8)
            combo.pack(side=tk.LEFT, padx=S(0, 14))
            combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())
            ttk.Label(bar, text="搜尋 Search", style="Dim.TLabel").pack(
                side=tk.LEFT, padx=S(0, 6))
            search = ttk.Entry(bar, textvariable=self._search_var, width=28)
            search.pack(side=tk.LEFT, padx=S(0, 14))
            search.bind("<KeyRelease>", lambda _e: self._apply_search())
            ttk.Checkbutton(
                bar, text="暫停捲動 Pause", variable=self._pause_var,
                command=self._on_pause_toggle,
            ).pack(side=tk.LEFT)
            ttk.Button(bar, text="清除 Clear", style="Ghost.TButton",
                       command=self.clear).pack(side=tk.RIGHT)

        wrap, self.text = themed_scrolled_text(
            self, wrap=tk.WORD, font=self._font, inset=True,
        )
        wrap.pack(fill=tk.BOTH, expand=True)
        self.text.configure(bg=self._bg, fg=self._fg, state=tk.DISABLED)
        self._placeholder = placeholder or ""
        self._ph_label = None
        if self._placeholder:
            # Overlay (not buffer text) so get()/save paths stay empty.
            self._ph_label = tk.Label(
                self.text, text=self._placeholder,
                bg=self._bg, fg=EMPTY_FG,
                font=FONTS.get("body") or ("", 10),
                justify=tk.CENTER, bd=0, highlightthickness=0,
            )
            self._show_placeholder()
        self.text.tag_configure(
            "search_hit", background=mix(self._bg, PALETTE["warn"], 0.45),
            foreground=PALETTE["text"])
        self.text.tag_configure("info", foreground=self._fg)
        self.text.tag_configure("debug", foreground=PALETTE["text_faint"])

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

    def clear(self) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)
        self._show_placeholder()

    def line_count(self) -> int:
        """Buffer lines, O(1): the index of the end sentinel, not a copy
        of the buffer (a busy bot appends thousands of lines)."""
        return int(self.text.index("end-1c").split(".")[0]) - 1

    def _trim(self) -> None:
        extra = self.line_count() - self.max_lines
        if extra > 0:
            self.text.delete("1.0", f"{extra + 1}.0")

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
        if self.text.compare("end-1c", "==", "1.0"):
            self._show_placeholder()
        else:
            self._hide_placeholder()

    def _apply_filter(self) -> None:
        choice = (self._filter_var.get() or "All").lower()
        # Info hides debug-tagged lines; Debug hides everything else.
        try:
            self.text.tag_configure("debug", elide=(choice == "info"))
            self.text.tag_configure("info", elide=(choice == "debug"))
        except tk.TclError:
            pass

    def _apply_search(self) -> None:
        self.text.tag_remove("search_hit", "1.0", tk.END)
        needle = self._search_var.get()
        if not needle:
            return
        start = "1.0"
        first = None
        while True:
            pos = self.text.search(needle, start, tk.END, nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(needle)}c"
            self.text.tag_add("search_hit", pos, end)
            if first is None:
                first = pos
            start = end
        if first is not None:
            self.text.see(first)

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
