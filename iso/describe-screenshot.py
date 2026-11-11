"""Describe a VM screenshot in words, for CI logs: is Walter's face on screen?

Usage: python3 iso/describe-screenshot.py shot.png
Prints the share of dark pixels and the most common bright colours. Walter's
blob is a large bright, saturated area on a near-black background; a text
console is mostly black with a little grey; a blank screen is all one colour.
"""
import sys
from collections import Counter

from PIL import Image

img = Image.open(sys.argv[1]).convert("RGB")
w, h = img.size
small = img.resize((min(w, 320), int(h * min(w, 320) / w)))
px = list(small.getdata())  # noqa: Pillow < 12 compatible
n = len(px)
dark = sum(1 for r, g, b in px if max(r, g, b) < 40) / n
bright = [(r // 32 * 32, g // 32 * 32, b // 32 * 32) for r, g, b in px if max(r, g, b) >= 40]
saturated = sum(1 for r, g, b in px if max(r, g, b) - min(r, g, b) > 60) / n
white = sum(1 for r, g, b in px if min(r, g, b) > 220) / n
print(f"{sys.argv[1]}: {w}x{h}, dark {dark:.0%}, colourful {saturated:.0%}, white {white:.0%}")
print("  top colours:", Counter(bright).most_common(5))
# Walter: a colourful blob on a mostly near-black background (the Debian
# installer is colourful too, but bright blue all over).
verdict = ("looks like Walter (big colourful blob)" if saturated > 0.03 and dark > 0.5
           else "looks like a text console" if dark > 0.8 and white < 0.01 and bright
           else "blank screen" if len(set(px)) < 4 else "unclear")
print("  verdict:", verdict)
if "--expect-walter" in sys.argv and not verdict.startswith("looks like Walter"):
    sys.exit(1)
