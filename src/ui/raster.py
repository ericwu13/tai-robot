"""Pure-Python anti-aliased shape rasterizer + PNG encoder.

The theme draws its rounded widget chrome (buttons, fields, cards, tabs,
scrollbar thumbs, check marks) at runtime, at the display's real DPI
scale, so edges stay crisp on 4K panels. Doing it here instead of
shipping bitmap assets keeps the repo free of binary files and adds no
dependency (no Pillow) to the PyInstaller bundle.

Shapes are signed-distance fields: coverage = clamp(0.5 - distance), which
gives exact 1-px anti-aliasing on curves and pixel-perfect straight edges
whenever the geometry sits on integer coordinates. Images are tiny
(9-slice sources, a few hundred pixels), so plain Python loops are fine.

No Tk import — everything here is testable without a display.
"""

from __future__ import annotations

import math
import struct
import zlib

Color = tuple[int, int, int]


def hex_to_rgb(color: str) -> Color:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(a: str, b: str, t: float) -> str:
    """Blend two hex colors; t=0 → a, t=1 → b."""
    ra, ga, ba = hex_to_rgb(a)
    rb, gb, bb = hex_to_rgb(b)
    return "#{:02x}{:02x}{:02x}".format(
        round(ra + (rb - ra) * t),
        round(ga + (gb - ga) * t),
        round(ba + (bb - ba) * t),
    )


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode straight-alpha RGBA8 pixels as a PNG byte string."""
    if len(rgba) != width * height * 4:
        raise ValueError("rgba buffer does not match width*height*4")

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: None
        raw += rgba[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def stretch_center(rgba: bytes, width: int, height: int, col: int, row: int,
                   nx: int, ny: int) -> tuple[bytes, int, int]:
    """Repeat pixel column ``col`` ``nx`` times and pixel row ``row`` ``ny`` times.

    ttk *tiles* the center slice of a 9-slice image element. A 1-px center
    means tens of thousands of alpha-blended draws per repaint of a large
    widget (a Treeview field effectively hangs the UI), so sources are
    rasterized small and then widened here — byte replication, no SDF work.
    """
    nx = max(1, int(nx))
    ny = max(1, int(ny))
    stride = width * 4
    rows = []
    for y in range(height):
        line = rgba[y * stride:(y + 1) * stride]
        rows.append(line[:col * 4] + line[col * 4:(col + 1) * 4] * nx
                    + line[(col + 1) * 4:])
    out = rows[:row] + [rows[row]] * ny + rows[row + 1:]
    return b"".join(out), width - 1 + nx, height - 1 + ny


def solid_rows(width: int, bands: list[tuple[int, str | None]]) -> tuple[bytes, int, int]:
    """Stack of solid horizontal bands: ``[(rows, color_or_None), ...]``.

    ``None`` is fully transparent. Used for hairlines and flat fills where
    rasterizing per pixel would be wasted work.
    """
    out = bytearray()
    height = 0
    for rows, color in bands:
        if rows <= 0:
            continue
        if color is None:
            px = b"\x00\x00\x00\x00"
        else:
            r, g, b = hex_to_rgb(color)
            px = bytes((r, g, b, 255))
        out += px * width * rows
        height += rows
    return bytes(out), width, height


class Image:
    """Float RGBA canvas (premultiplied) with SDF painting primitives."""

    def __init__(self, width: int, height: int):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._px = [0.0] * (self.width * self.height * 4)

    # ── painting ──

    def _paint(self, sdf, color: str, alpha: float = 1.0) -> None:
        r, g, b = (v / 255.0 for v in hex_to_rgb(color))
        px = self._px
        w = self.width
        for y in range(self.height):
            cy = y + 0.5
            row = y * w * 4
            for x in range(w):
                cov = 0.5 - sdf(x + 0.5, cy)
                if cov <= 0.0:
                    continue
                if cov > 1.0:
                    cov = 1.0
                a = cov * alpha
                i = row + x * 4
                inv = 1.0 - a
                px[i] = r * a + px[i] * inv
                px[i + 1] = g * a + px[i + 1] * inv
                px[i + 2] = b * a + px[i + 2] * inv
                px[i + 3] = a + px[i + 3] * inv

    def rounded_rect(self, x0: float, y0: float, x1: float, y1: float,
                     radius: float, color: str, alpha: float = 1.0) -> None:
        hw = (x1 - x0) / 2.0
        hh = (y1 - y0) / 2.0
        if hw <= 0 or hh <= 0:
            return
        cx = x0 + hw
        cy = y0 + hh
        r = max(0.0, min(radius, hw, hh))

        def sdf(px, py):
            qx = abs(px - cx) - hw + r
            qy = abs(py - cy) - hh + r
            ox = qx if qx > 0.0 else 0.0
            oy = qy if qy > 0.0 else 0.0
            inside = qx if qx > qy else qy
            if inside > 0.0:
                inside = 0.0
            return math.hypot(ox, oy) + inside - r

        self._paint(sdf, color, alpha)

    def bordered_rect(self, x0: float, y0: float, x1: float, y1: float,
                      radius: float, fill: str | None, border: str | None,
                      border_width: float = 1.0) -> None:
        """Rounded rect with an inner-aligned border ring."""
        if border and border_width > 0:
            self.rounded_rect(x0, y0, x1, y1, radius, border)
            if fill:
                bw = border_width
                self.rounded_rect(x0 + bw, y0 + bw, x1 - bw, y1 - bw,
                                  max(0.0, radius - bw), fill)
            else:
                self._punch(x0 + border_width, y0 + border_width,
                            x1 - border_width, y1 - border_width,
                            max(0.0, radius - border_width))
        elif fill:
            self.rounded_rect(x0, y0, x1, y1, radius, fill)

    def _punch(self, x0, y0, x1, y1, radius) -> None:
        """Erase a rounded rect (used for hollow border rings)."""
        hw = (x1 - x0) / 2.0
        hh = (y1 - y0) / 2.0
        if hw <= 0 or hh <= 0:
            return
        cx = x0 + hw
        cy = y0 + hh
        r = max(0.0, min(radius, hw, hh))
        px = self._px
        w = self.width
        for y in range(self.height):
            py = y + 0.5
            for x in range(w):
                qx = abs(x + 0.5 - cx) - hw + r
                qy = abs(py - cy) - hh + r
                ox = qx if qx > 0.0 else 0.0
                oy = qy if qy > 0.0 else 0.0
                inside = qx if qx > qy else qy
                if inside > 0.0:
                    inside = 0.0
                cov = 0.5 - (math.hypot(ox, oy) + inside - r)
                if cov <= 0.0:
                    continue
                if cov > 1.0:
                    cov = 1.0
                keep = 1.0 - cov
                i = (y * w + x) * 4
                px[i] *= keep
                px[i + 1] *= keep
                px[i + 2] *= keep
                px[i + 3] *= keep

    def circle(self, cx: float, cy: float, radius: float, color: str,
               alpha: float = 1.0) -> None:
        self._paint(lambda x, y: math.hypot(x - cx, y - cy) - radius,
                    color, alpha)

    def polyline(self, points: list[tuple[float, float]], thickness: float,
                 color: str, alpha: float = 1.0) -> None:
        """Round-capped stroked polyline (union of capsules — no double
        blending at the joints)."""
        if len(points) < 2:
            return
        half = thickness / 2.0
        segs = list(zip(points[:-1], points[1:]))

        def sdf(px, py):
            best = 1e9
            for (ax, ay), (bx, by) in segs:
                dx, dy = bx - ax, by - ay
                ln = dx * dx + dy * dy
                t = 0.0 if ln == 0 else ((px - ax) * dx + (py - ay) * dy) / ln
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
                if d < best:
                    best = d
            return best - half

        self._paint(sdf, color, alpha)

    # ── output ──

    def to_rgba(self) -> bytes:
        out = bytearray(self.width * self.height * 4)
        px = self._px
        for i in range(0, len(px), 4):
            a = px[i + 3]
            if a <= 0.0:
                continue
            inv = 1.0 / a
            out[i] = min(255, max(0, round(px[i] * inv * 255)))
            out[i + 1] = min(255, max(0, round(px[i + 1] * inv * 255)))
            out[i + 2] = min(255, max(0, round(px[i + 2] * inv * 255)))
            out[i + 3] = min(255, max(0, round(a * 255)))
        return bytes(out)

    def to_png(self) -> bytes:
        return encode_png(self.width, self.height, self.to_rgba())
