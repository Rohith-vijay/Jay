"""Draws Jay's icon: an armoured hexagonal emblem with a glowing reactor core, an angular
visor slit and circuit traces. Writes assets/jay.ico (all Windows sizes) and assets/jay.png."""
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 1024                                   # drawn large, then scaled down for crisp edges
OUT = Path(__file__).resolve().parent.parent / "assets"
CYAN = (70, 225, 255)


def hexagon(cx, cy, r, rot=0.0):
    return [(cx + r * math.cos(rot + k * math.pi / 3), cy + r * math.sin(rot + k * math.pi / 3)) for k in range(6)]


def glow(layer: Image.Image, radius: float, strength: int = 2) -> Image.Image:
    out = layer.copy()
    for _ in range(strength):
        out = Image.alpha_composite(out, layer.filter(ImageFilter.GaussianBlur(radius)))
    return out


def gradient_fill(size, top, bottom):
    g = Image.new("RGBA", size)
    d = ImageDraw.Draw(g)
    for y in range(size[1]):
        t = y / (size[1] - 1)
        d.line([(0, y), (size[0], y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    return g


def draw() -> Image.Image:
    c = S / 2
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # armoured hex plate with a metallic gradient and bevel
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).polygon(hexagon(c, c, S * .47, math.pi / 6), fill=255)
    plate = gradient_fill((S, S), (38, 52, 66), (6, 12, 20))
    img.paste(plate, (0, 0), mask)
    d = ImageDraw.Draw(img)
    d.polygon(hexagon(c, c, S * .47, math.pi / 6), outline=(120, 150, 170, 255), width=10)
    d.polygon(hexagon(c, c, S * .43, math.pi / 6), outline=(20, 30, 40, 255), width=14)
    d.polygon(hexagon(c, c, S * .405, math.pi / 6), outline=CYAN + (150,), width=4)

    # circuit traces running from the core to the plate edge
    traces = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    t = ImageDraw.Draw(traces)
    for k in range(6):
        a = math.pi / 6 + k * math.pi / 3
        x1, y1 = c + math.cos(a) * S * .25, c + math.sin(a) * S * .25
        x2, y2 = c + math.cos(a) * S * .33, c + math.sin(a) * S * .33
        bend = a + (0.22 if k % 2 else -0.22)
        x3, y3 = c + math.cos(bend) * S * .38, c + math.sin(bend) * S * .38
        t.line([(x1, y1), (x2, y2), (x3, y3)], fill=CYAN + (220,), width=7)
        t.ellipse((x3 - 11, y3 - 11, x3 + 11, y3 + 11), fill=CYAN + (255,))
    img = Image.alpha_composite(img, glow(traces, 10, 1))

    # outer segmented ring
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    r = ImageDraw.Draw(ring)
    box = (c - S * .26, c - S * .26, c + S * .26, c + S * .26)
    for k in range(8):
        start = k * 45 + 6
        r.arc(box, start, start + 33, fill=CYAN + (255,), width=20)
    r.ellipse((c - S * .205, c - S * .205, c + S * .205, c + S * .205), outline=CYAN + (180,), width=6)
    img = Image.alpha_composite(img, glow(ring, 18, 2))

    # reactor core
    core = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    k = ImageDraw.Draw(core)
    for i, (rad, alpha) in enumerate([(.18, 70), (.15, 130), (.12, 200)]):
        k.ellipse((c - S * rad, c - S * rad, c + S * rad, c + S * rad), fill=(40, 190, 255, alpha))
    k.ellipse((c - S * .085, c - S * .085, c + S * .085, c + S * .085), fill=(225, 252, 255, 255))
    img = Image.alpha_composite(img, glow(core, 34, 2))

    # angular visor slit across the core
    visor = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    v = ImageDraw.Draw(visor)
    w, h = S * .3, S * .028
    v.polygon([(c - w, c - h * .4), (c - w * .35, c - h), (c + w * .35, c - h), (c + w, c - h * .4),
               (c + w * .35, c + h), (c - w * .35, c + h)], fill=(4, 10, 16, 235))
    v.polygon([(c - w * .32, c - h * .25), (c + w * .32, c - h * .25), (c + w * .32, c + h * .25),
               (c - w * .32, c + h * .25)], fill=(255, 255, 255, 255))
    img = Image.alpha_composite(img, glow(visor, 8, 1))

    # keep everything inside the hexagon, plus a soft outer glow
    inside = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    inside.paste(img, (0, 0), mask)
    edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(edge).polygon(hexagon(c, c, S * .47, math.pi / 6), outline=CYAN + (200,), width=12)
    halo = edge.filter(ImageFilter.GaussianBlur(14))
    return Image.alpha_composite(Image.alpha_composite(halo, inside), edge)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    art = draw()
    art.resize((512, 512), Image.LANCZOS).save(OUT / "jay.png")
    big = art.resize((256, 256), Image.LANCZOS)
    big.save(OUT / "jay.ico", sizes=[(256, 256), (128, 128), (96, 96), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
    print("wrote", OUT / "jay.ico")
    _ = ImageChops  # (kept for future effects)
