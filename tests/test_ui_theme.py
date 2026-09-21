"""Workbench UI infrastructure — rasterizer, theme, widgets, headless construct.

Tk-dependent cases skip when the interpreter has no usable Tk (some CI
shells). The rasterizer, palette requirements and the labels seam are
pure and run everywhere.
"""

from __future__ import annotations

import struct
import threading
import zlib

import pytest

from src.live.headless import (
    TITLE_DATA_RANGE,
    TITLE_EXISTING_POSITION,
    TITLE_STRATEGY_CHANGE,
)
from src.ui import labels, raster
from src.ui.theme import (
    CHAT_TAGS,
    DOT_COLORS,
    EPISODE_TAGS,
    LOG_TAGS,
    PALETTE,
    ROW_TONE,
    STATUS_LEVEL_STYLES,
    THEME_NAME,
    TONE,
    S,
)


def _tk_probe():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


_TK_OK = _tk_probe()
_tk_skip = pytest.mark.skipif(not _TK_OK, reason="Tk unavailable")


def _new_root():
    """tk.Tk() with one retry: Windows intermittently fails to locate
    init.tcl when interpreters are created in quick succession."""
    import tkinter as tk
    try:
        return tk.Tk()
    except tk.TclError:
        return tk.Tk()


@pytest.fixture
def themed_root():
    from src.ui.theme import init_theme
    root = _new_root()
    root.withdraw()
    init_theme(root)
    try:
        yield root
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ── rasterizer (pure) ────────────────────────────────────────────────

def _decode_png(png: bytes):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, width, height, idat = 8, 0, 0, b""
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF, f"bad CRC in {tag}"
        if tag == b"IHDR":
            width, height, depth, color, *_ = struct.unpack(">IIBBBBB", data)
            assert (depth, color) == (8, 6), "must be RGBA8"
        elif tag == b"IDAT":
            idat += data
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    rows = [raw[y * stride + 1:(y + 1) * stride] for y in range(height)]
    assert all(raw[y * stride] == 0 for y in range(height)), "filter must be None"
    return width, height, rows


def _px(rows, x, y):
    return tuple(rows[y][x * 4:x * 4 + 4])


def test_png_roundtrip_is_valid_rgba():
    im = raster.Image(12, 9)
    im.rounded_rect(0, 0, 12, 9, 0, "#336699")
    w, h, rows = _decode_png(im.to_png())
    assert (w, h) == (12, 9)
    assert _px(rows, 5, 4) == (0x33, 0x66, 0x99, 255)


def test_rounded_rect_corners_are_transparent_and_antialiased():
    size = 40
    im = raster.Image(size, size)
    im.rounded_rect(0, 0, size, size, 12, "#ffffff")
    _, _, rows = _decode_png(im.to_png())
    assert _px(rows, 0, 0)[3] == 0, "outside the corner arc must be transparent"
    assert _px(rows, size // 2, size // 2)[3] == 255
    # Straight edges on integer coordinates stay pixel-exact.
    assert _px(rows, size // 2, 0)[3] == 255
    # The arc itself must carry partial coverage — that is the smoothing.
    alphas = {_px(rows, x, y)[3] for x in range(12) for y in range(12)}
    assert any(0 < a < 255 for a in alphas), "corner has no anti-aliased pixels"


def test_bordered_rect_has_ring_and_fill():
    im = raster.Image(30, 30)
    im.bordered_rect(0, 0, 30, 30, 6, "#101010", "#ff0000", 2)
    _, _, rows = _decode_png(im.to_png())
    assert _px(rows, 15, 0)[:3] == (255, 0, 0)       # top edge = border
    assert _px(rows, 15, 15)[:3] == (16, 16, 16)     # center = fill


def test_hollow_bordered_rect_center_is_transparent():
    im = raster.Image(30, 30)
    im.bordered_rect(0, 0, 30, 30, 6, None, "#ff0000", 2)
    _, _, rows = _decode_png(im.to_png())
    assert _px(rows, 15, 15)[3] == 0
    assert _px(rows, 15, 0)[3] == 255


def test_stretch_center_widens_without_touching_corners():
    im = raster.Image(9, 9)
    im.bordered_rect(0, 0, 9, 9, 3, "#202020", "#ffffff", 1)
    rgba = im.to_rgba()
    out, w, h = raster.stretch_center(rgba, 9, 9, 4, 4, 20, 10)
    assert (w, h) == (9 - 1 + 20, 9 - 1 + 10)
    assert len(out) == w * h * 4
    # Corner pixels are byte-identical to the source.
    assert out[:4] == rgba[:4]
    assert out[-4:] == rgba[-4:]


def test_solid_rows_bands():
    rgba, w, h = raster.solid_rows(4, [(2, "#010203"), (1, None)])
    assert (w, h) == (4, 3)
    assert rgba[:4] == bytes((1, 2, 3, 255))
    assert rgba[-4:] == b"\x00\x00\x00\x00"


def test_mix_endpoints():
    assert raster.mix("#000000", "#ffffff", 0.0) == "#000000"
    assert raster.mix("#000000", "#ffffff", 1.0) == "#ffffff"
    assert raster.mix("#000000", "#ffffff", 0.5) in ("#7f7f7f", "#808080")


# ── palette requirements (pure) ──────────────────────────────────────

def _luminance(color: str) -> float:
    def channel(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = raster.hex_to_rgb(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_palette_has_required_tokens():
    required = {"bg", "bg_raised", "bg_inset", "border", "text", "text_dim",
                "accent", "on_accent", "ok", "warn", "err", "info", "up", "down"}
    assert required <= set(PALETTE)
    for name, value in PALETTE.items():
        assert len(value) == 7 and value.startswith("#"), name
        raster.hex_to_rgb(value)
    assert TONE["good"] == PALETTE["ok"] and TONE["bad"] == PALETTE["err"]
    assert set(CHAT_TAGS) >= {"user", "assistant", "code", "error", "system"}
    assert set(LOG_TAGS) >= {"entry", "exit", "bar", "status"}
    assert DOT_COLORS["ok"] == PALETTE["ok"]
    assert "ep_long" in EPISODE_TAGS


@pytest.mark.parametrize("surface", ["bg", "bg_raised", "bg_inset"])
def test_text_contrast_meets_wcag_aa_on_every_surface(surface):
    """Body text ≥ 7:1, secondary text and status colors ≥ 4.5:1."""
    assert _contrast(PALETTE["text"], PALETTE[surface]) >= 7.0
    for token in ("text_dim", "ok", "warn", "err", "info"):
        ratio = _contrast(PALETTE[token], PALETTE[surface])
        assert ratio >= 4.5, f"{token} on {surface}: {ratio:.2f}:1"


def test_accent_button_label_is_readable():
    assert _contrast(PALETTE["on_accent"], PALETTE["accent"]) >= 3.0


def test_row_tones_stay_readable_and_distinct():
    for tone in ("good", "bad"):
        assert _contrast(ROW_TONE[tone], PALETTE["bg_inset"]) >= 4.5
    assert ROW_TONE["good"] != ROW_TONE["bad"]


def test_scale_helper_never_collapses_to_zero():
    assert S(0) == 0
    assert isinstance(S(8), int) and S(8) >= 8
    assert S(0.4) >= 1, "a non-zero metric must survive rounding"
    pair = S(4, 0)
    assert isinstance(pair, tuple) and pair[1] == 0 and pair[0] >= 4


# ── labels seam / package hygiene (pure) ─────────────────────────────

def test_labels_import_seam_titles_not_redefined():
    """Seam titles must be the headless.py objects, not a second literal."""
    assert labels.TITLE_STRATEGY_CHANGE is TITLE_STRATEGY_CHANGE
    assert labels.TITLE_EXISTING_POSITION is TITLE_EXISTING_POSITION
    assert labels.TITLE_DATA_RANGE is TITLE_DATA_RANGE
    assert labels.DEPLOY_BOT == "部署機器人 Deploy Bot"
    assert labels.READY == "就緒 Ready"
    assert "開始對話" in labels.CHAT_PLACEHOLDER
    assert "Start chatting" in labels.CHAT_PLACEHOLDER
    assert "尚無結果" in labels.REPORT_EMPTY


def test_tooltip_opens_above_a_bottom_edge_control():
    """Issue #139 item 3: a tooltip under a status-strip button falls off
    the work area. The window manager clamps it back onto the button,
    and the resulting Enter/Leave loop makes the hit feel sluggish."""
    from src.ui.widgets import tooltip_origin

    # Room below: open underneath, left-aligned with the control.
    assert tooltip_origin(
        (10, 10, 40, 20), (80, 30), (0, 0, 800, 600), gap=6) == (10, 36)

    # Flush with the bottom edge: open above, and do not cover the control.
    x, y = tooltip_origin(
        (100, 1000, 180, 28), (200, 36), (0, 0, 1920, 1040), gap=6)
    assert (x, y) == (100, 958)
    assert y + 36 <= 1000

    # Past the right edge: clamp inside the screen.
    x, y = tooltip_origin(
        (700, 10, 40, 20), (150, 30), (0, 0, 800, 600), gap=6)
    assert x == 650 and y == 36
    assert x + 150 <= 800


def test_status_strip_padding_clears_the_resize_drag_insets():
    """Issue #139 item 3: History / Report / Updates must sit above
    HTBOTTOM and left of the corner grip. Insets are device pixels; the
    logical rhythm still goes through S()."""
    from src.ui.widgets import status_strip_padding

    assert status_strip_padding((0, 0, 0)) == S(16, 6, 8, 6)
    assert status_strip_padding((-4, -1, -8)) == S(16, 6, 8, 6)
    left, top, right, bottom = status_strip_padding((9, 21, 13))
    assert (left, top) == (S(16), S(6))  # left inset is not applied
    assert right == S(8) + 21
    assert bottom == S(6) + 13


def test_resize_insets_from_metrics_cover_the_corner_grip():
    """The bottom-right grip is a scrollbar wide, larger than the frame."""
    from src.ui.widgets import resize_insets_from_metrics

    def metrics(index: int) -> int:
        return {32: 4, 33: 5, 92: 4, 93: 3, 2: 17}[index]

    # (left edge, right/corner, bottom edge)
    assert resize_insets_from_metrics(metrics) == (8, 17, 8)


def test_resize_insets_are_zero_off_windows(monkeypatch):
    import sys
    from src.ui.widgets import window_resize_insets

    monkeypatch.setattr(sys, "platform", "linux")
    assert window_resize_insets() == (0, 0, 0)


def test_issue_139_helpers_are_wired_into_the_chrome():
    """The geometry helpers have to be called. A pure function that
    nothing packs does not move the buttons."""
    import inspect
    import run_backtest as rb
    from src.ui import widgets

    strip = inspect.getsource(rb.BacktestApp._build_status_strip)
    assert "status_strip_padding" in strip
    assert "window_resize_insets" in strip
    chat = inspect.getsource(rb.BacktestApp._build_chat_panel)
    assert "install_composer_placeholder" in chat
    assert "_chat_composer" in chat
    # The transcript must not host the floating centered hint.
    assert "placeholder=CHAT_PLACEHOLDER" not in chat
    send = inspect.getsource(rb.BacktestApp._send_chat)
    assert "composer_value" in send
    tip = inspect.getsource(widgets.attach_tooltip)
    assert "tooltip_origin" in tip


def test_ui_package_does_not_import_run_backtest():
    import src.ui.labels as labels_mod
    import src.ui.raster as raster_mod
    import src.ui.theme as theme_mod
    import src.ui.widgets as widgets_mod
    for mod in (theme_mod, widgets_mod, labels_mod, raster_mod):
        src = open(mod.__file__, encoding="utf-8").read()
        assert "import run_backtest" not in src
        assert "from run_backtest" not in src
    raster_src = open(raster_mod.__file__, encoding="utf-8").read()
    assert "tkinter" not in raster_src, "rasterizer must stay display-free"
    assert "PIL" not in raster_src, "no Pillow dependency in the bundle"
    widgets_src = open(widgets_mod.__file__, encoding="utf-8").read()
    assert "import scrolledtext" not in widgets_src


def test_status_level_styles_map():
    assert STATUS_LEVEL_STYLES["info"] == "StatusStrip.Dim.TLabel"
    assert STATUS_LEVEL_STYLES["ok"] == "StatusStrip.Ok.TLabel"
    assert STATUS_LEVEL_STYLES["warn"] == "StatusStrip.Warn.TLabel"
    assert STATUS_LEVEL_STYLES["error"] == "StatusStrip.Err.TLabel"


def test_spec_lists_src_ui_hiddenimports():
    spec = open("tai_backtest.spec", encoding="utf-8").read()
    for name in ("src.ui", "src.ui.theme", "src.ui.widgets", "src.ui.labels",
                 "src.ui.raster"):
        assert f"'{name}'" in spec


# ── theme (Tk) ───────────────────────────────────────────────────────

@_tk_skip
def test_init_theme_installs_image_theme(themed_root):
    from tkinter import ttk
    from src.ui.theme import FONTS

    style = ttk.Style(themed_root)
    assert style.theme_use() == THEME_NAME
    for name in ("TButton", "Accent.TButton", "Success.TButton",
                 "Danger.TButton", "Ghost.TButton", "TEntry", "TCombobox",
                 "TCheckbutton", "TRadiobutton", "TNotebook.Tab", "Treeview",
                 "Card.TFrame", "Well.TFrame", "TLabelframe",
                 "Vertical.TScrollbar", "Hidden.Vertical.TScrollbar",
                 "Inset.Vertical.TScrollbar", "Horizontal.TProgressbar"):
        assert style.layout(name), f"{name} has no layout"
    assert style.lookup("Dim.TLabel", "foreground") == PALETTE["text_dim"]
    assert style.lookup("Card.TLabel", "background") == PALETTE["bg_raised"]
    assert style.lookup("Treeview", "fieldbackground") == PALETTE["bg_inset"]
    assert int(style.lookup("Treeview", "rowheight")) == S(30)
    assert set(FONTS) >= {"title", "section", "value", "metric", "body",
                          "small", "mono", "mono_small", "mono_bold"}


@_tk_skip
def test_style_fonts_are_not_shadowed_by_option_database(themed_root):
    """A bare ``*Font`` option lands on every ttk widget's -font and
    silently defeats the style fonts (titles rendered at body size)."""
    from tkinter import ttk
    from src.ui.theme import FONTS
    label = ttk.Label(themed_root, text="x", style="Title.TLabel")
    assert str(label.cget("font")) == ""
    assert ttk.Style(themed_root).lookup("Title.TLabel", "font") == str(FONTS["title"])


@_tk_skip
def test_init_theme_is_idempotent_and_survives_a_second_root(themed_root):
    """The style and option databases reference fonts BY NAME. Auto-named
    fonts were deleted when FONTS was cleared (Font.__del__), so a second
    init_theme — or a second root — silently reset every themed widget to
    the default font: no error, just smaller titles."""
    from tkinter import font as tkfont
    from tkinter import ttk
    from src.ui.theme import FONTS, init_theme

    def title_height(root):
        label = ttk.Label(root, text="Title", style="Title.TLabel")
        root.update_idletasks()
        return label.winfo_reqheight()

    before = title_height(themed_root)
    init_theme(themed_root)  # same interpreter: theme already exists
    style = ttk.Style(themed_root)
    assert style.theme_use() == THEME_NAME
    assert style.lookup("Title.TLabel", "font") in tkfont.names(themed_root)
    assert title_height(themed_root) == before

    other = _new_root()
    other.withdraw()
    try:
        init_theme(other)     # fresh interpreter: rebuilt from scratch
        assert ttk.Style(other).theme_use() == THEME_NAME
        ttk.Button(other, text="x", style="Accent.TButton")
        assert FONTS["body"].actual()["size"]
        # ...and the first root's typography must have survived it.
        assert style.lookup("Title.TLabel", "font") in tkfont.names(themed_root)
        assert title_height(themed_root) == before
    finally:
        other.destroy()


@_tk_skip
def test_nine_slice_sources_have_wide_centers(themed_root):
    """ttk tiles the center slice: a 1-px center means ~100k blended draws
    per repaint of a large Treeview (the UI effectively hangs)."""
    images = themed_root._tairobot_images
    img, edge = images.box(PALETTE["bg_inset"], PALETTE["border"], radius=8)
    assert img.width() - 2 * edge >= S(24)
    assert img.height() - 2 * edge >= S(24)


@_tk_skip
def test_minimal_mode_creates_no_theme_and_custom_styles_still_construct():
    """Headless bots: zero theme work, yet every dotted style name used by
    the builders must resolve to its stock base layout."""
    import tkinter as tk
    from tkinter import ttk
    from src.ui import theme

    root = _new_root()
    root.withdraw()
    try:
        before = ttk.Style(root).theme_use()
        theme.init_theme(root, minimal=True)
        assert ttk.Style(root).theme_use() == before
        assert THEME_NAME not in ttk.Style(root).theme_names()
        ttk.Button(root, text="x", style="Bar.Accent.TButton")
        ttk.Button(root, text="x", style="Card.Ghost.TButton")
        ttk.Frame(root, style="Card.TFrame")
        ttk.Frame(root, style="Well.TFrame")
        ttk.Label(root, text="x", style="Card.Status.Ok.TLabel")
        ttk.Entry(root, style="Bar.TEntry")
        ttk.Combobox(root, style="Card.TCombobox")
        ttk.Checkbutton(root, style="Card.TCheckbutton")
        ttk.Scrollbar(root, orient="vertical",
                      style="Hidden.Inset.Vertical.TScrollbar")
        root.update_idletasks()
    finally:
        root.destroy()


@_tk_skip
def test_headless_bot_app_constructs_unthemed_and_stays_hidden():
    """Headless invariant: every widget builds on a withdrawn root, no
    theme is installed, and no window ever flashes on screen."""
    import tkinter as tk
    from tkinter import ttk
    from src.live.headless_app import HeadlessBotApp

    root = _new_root()
    root.withdraw()
    try:
        app = HeadlessBotApp(root)
        assert app.root is root
        assert app._headless is True
        assert root.state() == "withdrawn"
        assert ttk.Style(root).theme_use() != THEME_NAME
        for attr in ("log_text", "live_log", "chat_display", "trade_tree",
                     "regime_tree", "btn_deploy", "btn_login", "btn_update",
                     "btn_report", "symbol_combo", "strategy_combo",
                     "mode_combo", "results_notebook", "_conn_dot"):
            assert hasattr(app, attr), attr
        app.set_status("hello", "info")
        assert app.status_var.get() == "hello"
        assert app._status_msg_label.cget("style") == "StatusStrip.Dim.TLabel"
        app.set_status("boom", "error")
        assert app._status_msg_label.cget("style") == "StatusStrip.Err.TLabel"
        assert len(app._status_history) >= 2
        app._set_conn_dot("ok")
        assert "Connected" in str(app._conn_dot.cget("text"))
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ── widgets (Tk) ─────────────────────────────────────────────────────

@_tk_skip
def test_tag_text_log_cap_trims_from_top(themed_root):
    from tkinter import ttk
    from src.ui.widgets import TagTextLog

    log = TagTextLog(themed_root, max_lines=5, show_toolbar=True)
    for i in range(8):
        log.append(f"line-{i}", "info")
    content = log.get("1.0", "end-1c").strip().splitlines()
    assert content == [f"line-{i}" for i in range(3, 8)]
    assert log.line_count() == 5
    assert str(log.text.cget("bg")) == PALETTE["bg_inset"]

    def scrollbars(widget):
        found = []
        for child in widget.winfo_children():
            if isinstance(child, ttk.Scrollbar):
                found.append(child)
            found.extend(scrollbars(child))
        return found
    assert scrollbars(log), "TagTextLog must use a ttk.Scrollbar"


@_tk_skip
def test_tag_text_log_trim_does_not_copy_the_buffer(themed_root):
    """Trimming once copied the whole buffer per appended line — O(n) work
    on every log line of a busy bot."""
    from src.ui.widgets import TagTextLog

    log = TagTextLog(themed_root, max_lines=50)
    calls = []
    real_get = log.text.get
    log.text.get = lambda *a, **k: (calls.append(a), real_get(*a, **k))[1]
    for i in range(120):
        log.append(f"line-{i}")
    assert not calls, "append/trim must not read the buffer back"
    assert log.line_count() == 50


@_tk_skip
def test_tag_text_log_placeholder_clears_on_first_message(themed_root):
    from src.ui.widgets import TagTextLog

    log = TagTextLog(themed_root, placeholder="開始對話… / Start chatting…")
    assert str(log._ph_label.place_info().get("anchor", "")) == "center"
    assert log.get("1.0", "end-1c").strip() == ""  # overlay, not buffer text
    log.append("hello", "info")
    assert log._ph_label.place_info() == {}
    log.clear()
    assert str(log._ph_label.place_info().get("anchor", "")) == "center"


@_tk_skip
def test_composer_placeholder_is_not_a_message(themed_root):
    """Issue #139 item 4: the hint sits on the caret line, and Send must
    not post it. A real character replaces the hint."""
    import tkinter as tk
    from src.ui.labels import CHAT_PLACEHOLDER
    from src.ui.widgets import (
        composer_value, install_composer_placeholder, note_composer_key,
        refresh_composer_placeholder, themed_scrolled_text,
    )

    _well, text = themed_scrolled_text(themed_root, height=4, scrollbar=False)
    install_composer_placeholder(text, CHAT_PLACEHOLDER)
    assert "開始對話" in text.get("1.0", "end-1c")
    assert composer_value(text) == ""

    # Enter leaves the hint in place (the send path ignores it).
    note_composer_key(text, "Return", "\r")
    assert composer_value(text) == ""
    assert "開始對話" in text.get("1.0", "end-1c")

    # Shift+Enter is a newline: the hint must not stay glued to it.
    note_composer_key(text, "Return", "\r", state=1)
    text.insert(tk.INSERT, "\n")
    assert "開始對話" not in text.get("1.0", "end-1c")
    text.delete("1.0", tk.END)
    refresh_composer_placeholder(text)
    assert composer_value(text) == ""

    note_composer_key(text, "a", "a")
    text.insert(tk.INSERT, "a")
    assert composer_value(text) == "a"

    text.delete("1.0", tk.END)
    refresh_composer_placeholder(text)
    assert composer_value(text) == ""
    assert "Start chatting" in text.get("1.0", "end-1c")


def _pad4(widget) -> tuple[int, int, int, int]:
    raw = widget.cget("padding")
    if isinstance(raw, str):
        parts = [int(float(p)) for p in raw.split()]
    else:
        parts = [int(p) for p in raw]
    if len(parts) == 1:
        return (parts[0], parts[0], parts[0], parts[0])
    if len(parts) == 2:
        return (parts[0], parts[1], parts[0], parts[1])
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2], parts[1])
    return (parts[0], parts[1], parts[2], parts[3])


@_tk_skip
def test_issue_139_bottom_chrome_and_chat_composer(monkeypatch):
    """Items 3–4 on a withdrawn root (the headless construct path).

    The status strip's right/bottom padding includes the resize/drag
    inset, and the chat hint lives in the input beside Send — not as a
    label floating in the transcript.
    """
    import run_backtest as rb
    from src.live.headless_app import HeadlessBotApp
    from src.ui.widgets import composer_value

    monkeypatch.setattr(rb, "window_resize_insets", lambda: (0, 21, 13))
    root = _new_root()
    root.withdraw()
    try:
        app = HeadlessBotApp(root)
        left, _top, right, bottom = _pad4(app._status_strip)
        assert right >= 21
        assert bottom >= 13
        assert left == S(16)
        for btn in (app.btn_update, app.btn_report):
            assert str(btn.cget("cursor")) == "hand2"

        assert app.chat_display._ph_label is None
        assert composer_value(app.chat_input) == ""
        assert "開始對話" in app.chat_input.get("1.0", "end-1c")
        assert app.btn_send.master is app._chat_composer
        assert app._chat_input_well.master is app._chat_composer
        assert str(app.btn_send.pack_info()["side"]) == "right"
        assert str(app._chat_input_well.pack_info()["side"]) == "left"
        # Generate stays a secondary action, not on the composer row.
        assert app.btn_generate.master is not app._chat_composer
    finally:
        try:
            root.destroy()
        except Exception:
            pass


@_tk_skip
def test_tag_text_log_append_from_non_main_thread_raises(themed_root):
    """Producers must go through _ui_queue — documented by this raise."""
    from src.ui.widgets import TagTextLog

    log = TagTextLog(themed_root)
    errors: list[BaseException] = []

    def worker():
        try:
            log.append("from-worker")
        except RuntimeError as exc:
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert errors and "Tk thread" in str(errors[0])


@_tk_skip
def test_status_dot_states(themed_root):
    from src.ui.widgets import StatusDot

    dot = StatusDot(themed_root, text="conn")
    dot.set_state("ok")
    assert "●" in str(dot.cget("text"))
    assert dot.cget("style") == "Status.Ok.TLabel"
    dot.set_state("err")
    assert dot.cget("style") == "Status.Err.TLabel"
    dot.set_state("off")
    assert dot.cget("style") == "Dim.TLabel"
    raised = StatusDot(themed_root, text="strip", surface="raised")
    raised.set_state("ok")
    assert raised.cget("style") == "StatusStrip.Ok.TLabel"
    card = StatusDot(themed_root, text="vote", surface="card")
    card.set_state("warn")
    assert card.cget("style") == "Card.Status.Warn.TLabel"
    card.set_label("renamed")
    assert "renamed" in str(card.cget("text"))


@_tk_skip
def test_flow_frame_wraps_and_right_aligns_tail(themed_root):
    from tkinter import ttk
    from src.ui.widgets import FlowFrame

    flow = FlowFrame(themed_root, gap=6, row_gap=6)
    left = [flow.add(ttk.Button(flow, text=f"button-{i}")) for i in range(4)]
    flow.add_spacer()
    tail = flow.add(ttk.Button(flow, text="tail"))
    themed_root.update_idletasks()
    total = sum(b.winfo_reqwidth() for b in left + [tail]) + S(6) * 5

    flow.reflow(total + S(200))                    # roomy: one row
    ys = {int(b.place_info()["y"]) for b in left + [tail]}
    assert len(ys) == 1
    tail_x = int(tail.place_info()["x"])
    assert tail_x + tail.winfo_reqwidth() == total + S(200), "tail hugs the right edge"

    flow.reflow(left[0].winfo_reqwidth() * 2 + S(20))  # narrow: must wrap
    rows = {int(b.place_info()["y"]) for b in left + [tail]}
    assert len(rows) >= 2
    for b in left + [tail]:
        x = int(b.place_info()["x"])
        assert x + b.winfo_reqwidth() <= left[0].winfo_reqwidth() * 2 + S(20), \
            "no child may be clipped past the frame edge"
    assert int(flow.cget("height")) > left[0].winfo_reqheight()


@_tk_skip
def test_flow_frame_reflows_when_a_child_grows(themed_root):
    """The connection dot's label changes length at runtime; neighbours
    must move instead of being overlapped."""
    from tkinter import ttk
    from src.ui.widgets import FlowFrame

    flow = FlowFrame(themed_root, gap=6)
    flow.pack(fill="x")
    first = flow.add(ttk.Label(flow, text="off"))
    second = flow.add(ttk.Label(flow, text="neighbour"))
    flow.reflow(S(800))
    themed_root.update()
    first.configure(text="a considerably longer connection label")
    themed_root.update()
    a, b = first.place_info(), second.place_info()
    right_edge = int(a["x"]) + first.winfo_reqwidth()
    moved_right = int(b["x"]) >= right_edge
    wrapped = int(b["y"]) >= int(a["y"]) + first.winfo_reqheight()
    assert moved_right or wrapped, "neighbour is overlapped by the grown child"


@_tk_skip
def test_stat_card_signed_tint(themed_root):
    import tkinter as tk
    from src.ui.widgets import StatCard

    var = tk.StringVar(master=themed_root, value="0")
    card = StatCard(themed_root, "損益 P&L", variable=var, signed=True)
    assert str(card.value_label.cget("foreground")) == PALETTE["text"]
    var.set("+12,400")
    assert str(card.value_label.cget("foreground")) == PALETTE["ok"]
    var.set("-3,150")
    assert str(card.value_label.cget("foreground")) == PALETTE["err"]
    var.set("+0")
    assert str(card.value_label.cget("foreground")) == PALETTE["text"]


@_tk_skip
def test_autohide_scrollbar_swaps_style_not_geometry(themed_root):
    from tkinter import ttk
    from src.ui.widgets import autohide_scrollbar

    vsb = ttk.Scrollbar(themed_root, orient="vertical")
    on_set = autohide_scrollbar(vsb, "Vertical.TScrollbar")
    on_set("0.0", "1.0")
    assert vsb.cget("style") == "Hidden.Vertical.TScrollbar"
    on_set("0.0", "0.4")
    assert vsb.cget("style") == "Vertical.TScrollbar"


@_tk_skip
def test_row_hover_tag_is_transient(themed_root):
    from src.ui.widgets import scrolled_tree, enable_row_hover

    frame, tree = scrolled_tree(themed_root, columns=("a",), show="headings")
    iid = tree.insert("", "end", values=(1,), tags=("win",))
    enable_row_hover(tree)
    tree.item(iid, tags=("win", "hover"))
    tree.event_generate("<Leave>")
    # The helper tracks its own hovered row; a foreign "hover" is left
    # alone, and the original tag is never dropped.
    assert "win" in tree.item(iid, "tags")


@_tk_skip
def test_stat_card_drops_its_trace_when_destroyed(themed_root):
    """The variable outlives the card; a leftover trace fires into a
    destroyed label and the next write raises TclError."""
    import tkinter as tk
    from src.ui.widgets import StatCard

    # Tk does not raise from a trace callback: it routes the exception to
    # report_callback_exception (a traceback on stderr, every write).
    errors = []
    themed_root.report_callback_exception = lambda *exc: errors.append(exc)
    var = tk.StringVar(master=themed_root, value="0")
    card = StatCard(themed_root, "損益 P&L", variable=var, signed=True)
    card.destroy()
    themed_root.update_idletasks()
    var.set("+5")
    assert not errors, f"trace fired into a destroyed card: {errors[0][1]!r}"


def test_fit_dialog_stays_inside_the_work_area():
    """820x740 logical at 150 % = 1230x1110 px: taller than a 1080p work
    area. The Deploy dialog's commit buttons sit at its bottom edge."""
    from src.ui.widgets import fit_dialog

    area = (0, 0, 1920, 1032)
    w, h, x, y = fit_dialog(1230, 1110, (0, 45, 1920, 963), area,
                            frame=12, caption=48)
    assert y >= area[1] + 48
    assert y + h <= area[3] - 12, "bottom edge (buttons) must be on screen"
    assert x >= area[0] + 12 and x + w <= area[2] - 12

    # A dialog that fits is centered and untouched in size.
    w, h, x, y = fit_dialog(600, 400, (0, 45, 1920, 963), area,
                            frame=12, caption=48)
    assert (w, h) == (600, 400) and x == (1920 - 600) // 2

    # Secondary monitor to the LEFT of the primary: negative coordinates.
    left = (-1920, 0, 0, 1040)
    w, h, x, y = fit_dialog(1230, 1110, (-1920, 45, 1920, 963), left,
                            frame=12, caption=48)
    assert left[0] <= x and x + w <= left[2]
    assert y + h <= left[3] - 12


@_tk_skip
def test_place_dialog_clamps_a_fixed_size_to_the_monitor(themed_root, monkeypatch):
    import tkinter as tk
    from src.ui import theme, widgets

    class Parent:                      # maximized 1080p window at 150 %
        def winfo_rootx(self): return 0
        def winfo_rooty(self): return 45
        def winfo_width(self): return 1920
        def winfo_height(self): return 963
        def winfo_id(self): return 0

    monkeypatch.setattr(theme, "_SCALE", 1.5)
    monkeypatch.setattr(widgets, "work_area", lambda _w: (0, 0, 1920, 1032),
                        raising=False)
    win = tk.Toplevel(themed_root)
    win.withdraw()
    # A withdrawn window does not report a requested geometry back, so
    # record what place_dialog asks for.
    requested = []
    real_geometry = win.geometry
    win.geometry = lambda spec=None: (requested.append(spec), real_geometry(spec))[1]
    widgets.place_dialog(win, Parent(), 820, 740)
    assert requested and requested[-1], "place_dialog must set a geometry"
    size, _, pos = requested[-1].partition("+")
    height = int(size.split("x")[1])
    y = int(pos.split("+")[1])
    assert y + height <= 1032, f"dialog bottom {y + height} is below the work area"


# ── Trades tab: the tree must always equal the computed rows ─────────

class _StubRunner:
    started_at = None

    def __init__(self, state):
        self.state = state

    def get_live_bars(self):
        return []

    def get_status(self):
        return {"state": "RUNNING", "bars_1m": 0, "bars_agg": 0}


def _trade(tag, pnl, real_exit=0):
    from src.backtest.broker import OrderSide, Trade
    return Trade(tag=tag, side=OrderSide.LONG, qty=1, entry_price=44000,
                 exit_price=44000 + pnl // 200, entry_bar_index=0,
                 exit_bar_index=3, pnl=pnl, entry_dt="2026-09-01 09:00",
                 exit_dt="2026-09-01 09:30", real_exit_price=real_exit,
                 source="real")


def _result(trades):
    from types import SimpleNamespace
    from src.backtest.metrics import calculate_metrics
    curve, total = [], 0
    for t in trades:
        total += t.pnl
        curve.append(total)
    return SimpleNamespace(
        trades=trades, equity_curve=curve, strategy_name="UiTest",
        metrics=calculate_metrics(trades, curve, initial_balance=1_000_000))


@pytest.fixture
def workbench():
    from src.live.headless_app import HeadlessBotApp
    root = _new_root()
    root.withdraw()
    try:
        yield HeadlessBotApp(root)
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _column(app, name):
    tree = app.trade_tree
    idx = list(tree["columns"]).index(name)
    return [tree.item(iid, "values")[idx] for iid in tree.get_children()]


@_tk_skip
def test_trades_tab_shows_a_real_exit_price_that_lands_after_the_row_was_drawn(workbench):
    """Issue #92: the trade row is drawn at the bar close with real exit
    "--"; the OnNewData deal row sets trades[-1].real_exit_price seconds
    later and asks for a refresh. The row must update."""
    from src.live.live_runner import LiveState
    app = workbench
    app._live_runner = _StubRunner(LiveState.RUNNING)
    trades = [_trade("A", 800, real_exit=44004), _trade("B", -400)]
    app._display_results(_result(trades))
    assert _column(app, "real_exit") == ["44,004", "--"]
    last = app.trade_tree.get_children()[-1]
    app.trade_tree.selection_set(last)

    trades[-1].real_exit_price = 44622            # guarded in-place write
    app._display_results(_result(trades))
    assert _column(app, "real_exit") == ["44,004", "44,622"]
    assert app.trade_tree.selection() == (last,), "refresh must keep the selection"


@_tk_skip
def test_trades_tab_never_keeps_rows_from_a_previous_result(workbench):
    """Backtest (3 trades) → deploy a resumed bot (5 trades): every row
    must belong to the bot. Then a shorter result must drop the surplus."""
    from src.live.live_runner import LiveState
    app = workbench
    app._display_results(_result([_trade("BT", 100) for _ in range(3)]))
    assert _column(app, "tag") == ["BT"] * 3

    app._live_runner = _StubRunner(LiveState.RUNNING)
    app._display_results(_result([_trade("LIVE", 100) for _ in range(5)]))
    assert _column(app, "tag") == ["LIVE"] * 5

    app._display_results(_result([_trade("BOT-B", 100) for _ in range(2)]))
    assert _column(app, "tag") == ["BOT-B"] * 2
    assert _column(app, "num") == ["1", "2"]


@_tk_skip
def test_trades_tab_sort_survives_a_live_update(workbench):
    from src.live.live_runner import LiveState
    app = workbench
    app._live_runner = _StubRunner(LiveState.RUNNING)
    trades = [_trade("A", 800), _trade("B", -400), _trade("C", 1200)]
    app._display_results(_result(trades))
    app._sort_trade_tree("pnl")                   # ascending
    assert _column(app, "tag") == ["B", "A", "C"]

    trades.append(_trade("D", -2000))             # new worst trade closes
    app._display_results(_result(trades))
    assert _column(app, "tag") == ["D", "B", "A", "C"]


@_tk_skip
def test_deploy_cannot_be_reentered_while_the_position_check_is_pending(workbench):
    """The GUI waits for the OI snapshot without blocking Tk, so the Deploy
    button stays clickable; a second deploy would build a second runner
    over the first."""
    app = workbench
    calls = []
    app._deploy_live = lambda: calls.append("deploy")
    app._deploy_pending = True
    app._toggle_live()
    assert calls == []
    app._deploy_pending = False
    app._toggle_live()
    assert calls == ["deploy"]


def test_async_oi_wait_always_clears_the_pending_flag():
    import inspect
    import run_backtest as rb
    src = inspect.getsource(rb.BacktestApp._deploy_live_from)
    poll = src[src.index("def _poll_oi"):]
    assert "finally:" in poll and "self._deploy_pending = False" in poll
    assert src.index("self._deploy_pending = True") < src.index("return True")


# ── source contracts: fail against the pre-fix code ──────────────────

def test_phase1_run_backtest_source_contracts():
    import inspect
    import run_backtest as rb

    src = open(rb.__file__, encoding="utf-8").read()
    assert "def _attach_tooltip(" not in src
    assert "def _on_strategy_changed(" not in src
    assert "def _reflow_toolbar(" not in src, "FlowFrame replaces the hand-rolled reflow"
    tv_src = inspect.getsource(rb.BacktestApp._fetch_tradingview_live)
    assert "self.root.update()" not in tv_src
    assert "threading.Thread" in tv_src
    deploy_src = inspect.getsource(rb.BacktestApp._deploy_live_from)
    assert "self.root.update_idletasks" not in deploy_src
    assert "time.sleep(" not in deploy_src
    assert "_oi_wait_must_block" in deploy_src
    assert TITLE_EXISTING_POSITION in deploy_src
    assert TITLE_STRATEGY_CHANGE in deploy_src
    assert "return False" in deploy_src
    assert "messagebox." not in deploy_src
    assert "def set_status(" in src
    assert "transient_start" in inspect.getsource(rb.BacktestApp._append_chat)
    assert "transient_start" in inspect.getsource(rb.BacktestApp._remove_last_system_line)
    assert "_sync_trade_tree" in inspect.getsource(rb.BacktestApp._display_results)
    assert "_rendered_trade_count" not in src, "bare-count watermark must stay gone"
    assert "_flush_report_render" in inspect.getsource(rb.BacktestApp._render_report_view)
    assert "log_ui" in inspect.getsource(rb._route_to_log_widget)
    assert "def _deploy_live_continue(" in src
    # Order-confirm dialog stays non-modal.
    confirm_src = inspect.getsource(rb.BacktestApp._show_order_confirm_dialog)
    assert "dlg.grab_set" not in confirm_src
    assert "countdown" in confirm_src


def test_startup_orders_dpi_awareness_before_any_window():
    """COM init and tk.Tk() both create windows; DPI awareness can only be
    set while the process owns none."""
    import inspect
    import run_backtest as rb

    main_src = inspect.getsource(rb.main)
    assert main_src.index("enable_dpi_awareness()") < main_src.index("_init_com()")
    assert main_src.index("_init_com()") < main_src.index("tk.Tk()")


def test_headless_gets_minimal_theme_and_no_window_reveal():
    import inspect
    import run_backtest as rb

    init_src = inspect.getsource(rb.BacktestApp.__init__)
    assert "minimal=self._headless" in init_src
    reveal = init_src[init_src.index("if not self._headless:"):]
    assert "deiconify" in reveal and "zoomed" in reveal
    assert init_src.count("deiconify") == 1, "only the GUI path may reveal the window"


def test_exception_lambdas_capture_by_default_arg():
    """Python 3.13 deletes ``e`` when the except block ends; a bare
    ``lambda: ...e...`` scheduled with root.after raises NameError later."""
    import ast
    import run_backtest as rb

    tree = ast.parse(open(rb.__file__, encoding="utf-8").read())
    offenders = []
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
        if not handler.name:
            continue
        for lam in (n for n in ast.walk(handler) if isinstance(n, ast.Lambda)):
            bound = {a.arg for a in lam.args.args + lam.args.kwonlyargs}
            uses = {n.id for n in ast.walk(lam.body) if isinstance(n, ast.Name)}
            if handler.name in uses and handler.name not in bound:
                offenders.append(lam.lineno)
    assert not offenders, f"lambdas closing over a deleted except-variable: {offenders}"
