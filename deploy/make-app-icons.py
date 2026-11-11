#!/usr/bin/env python3
"""Draw the companion app's icons (Walter's glowing blob with two big eyes).

  .venv/bin/python deploy/make-app-icons.py      writes app/icons/*.png + icon.svg

numpy only (no PIL): signed-distance shapes with soft edges, written as PNG.
"""
import struct, zlib
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "app" / "icons"
BG = np.array([5, 6, 10], float)
BLOB = np.array([88, 196, 255], float)


def png(path, rgba):
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.write_bytes(data)


def draw(size, scale=0.72, rounded=True):
    y, x = np.mgrid[0:size, 0:size].astype(float)
    u, v = (x + 0.5) / size * 2 - 1, (y + 0.5) / size * 2 - 1     # -1..1
    px = 2.0 / size
    def soft(d):   # signed distance -> coverage
        return np.clip(0.5 - d / px, 0, 1)
    img = np.zeros((size, size, 3)) + BG
    alpha = np.ones((size, size))
    if rounded:   # rounded-square tile, transparent corners
        q = np.maximum(np.abs(u), np.abs(v)) - 1 + 0.36
        d = np.sqrt(np.maximum(np.abs(u) - 0.64, 0) ** 2 + np.maximum(np.abs(v) - 0.64, 0) ** 2) - 0.36
        alpha = soft(d)
    r = scale
    cy = 0.06 * scale
    # glow
    dist = np.sqrt(u ** 2 + ((v - cy) / 0.94) ** 2)
    glow = np.exp(-np.maximum(dist - r, 0) ** 2 / (2 * (0.16 * scale) ** 2)) * 0.55
    img = img + (BLOB - BG) * glow[..., None] * 0.6
    # blob body: slightly squashed circle, lighter at the top
    body = soft(dist - r)
    shade = np.clip(1.08 - 0.25 * (v - cy + r) / (2 * r), 0.75, 1.1)
    col = np.clip(BLOB[None, None, :] * shade[..., None], 0, 255)
    img = img * (1 - body[..., None]) + col * body[..., None]
    # eyes: tall white ovals with a dark pupil
    for ex in (-0.36 * scale, 0.36 * scale):
        ey = cy - 0.08 * scale
        ed = np.sqrt(((u - ex) / (0.2 * scale)) ** 2 + ((v - ey) / (0.3 * scale)) ** 2) - 1
        e = soft(ed * 0.2 * scale) * body
        img = img * (1 - e[..., None]) + np.array([250, 252, 255.0]) * e[..., None]
        pd = np.sqrt(((u - ex - 0.02 * scale) / (0.11 * scale)) ** 2 + ((v - ey + 0.03 * scale) / (0.16 * scale)) ** 2) - 1
        p = soft(pd * 0.11 * scale) * e
        img = img * (1 - p[..., None]) + np.array([12, 22, 40.0]) * p[..., None]
        hd = np.sqrt((u - ex + 0.035 * scale) ** 2 + (v - ey + 0.1 * scale) ** 2) - 0.035 * scale
        hl = soft(hd) * p
        img = img * (1 - hl[..., None]) + 255 * hl[..., None]
    rgba = np.dstack([np.clip(img, 0, 255), alpha * 255]).astype(np.uint8)
    return rgba


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    png(OUT / "icon-192.png", draw(192))
    png(OUT / "icon-512.png", draw(512))
    png(OUT / "icon-maskable-512.png", draw(512, scale=0.5, rounded=False))   # safe zone
    png(OUT / "apple-touch-icon.png", draw(180, scale=0.66, rounded=False))  # iOS rounds it itself
    png(OUT / "favicon-32.png", draw(32, scale=0.8))
    (OUT / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" rx="18" fill="#05060a"/>'
        '<ellipse cx="50" cy="53" rx="36" ry="34" fill="#58c4ff"/>'
        '<ellipse cx="38" cy="48" rx="6" ry="9" fill="#fafcff"/><ellipse cx="62" cy="48" rx="6" ry="9" fill="#fafcff"/>'
        '<ellipse cx="39" cy="47" rx="3" ry="4.6" fill="#0c1628"/><ellipse cx="63" cy="47" rx="3" ry="4.6" fill="#0c1628"/>'
        '</svg>\n')
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
