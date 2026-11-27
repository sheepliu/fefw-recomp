# SPDX-License-Identifier: GPL-3.0-or-later
"""Draws the game's Steam library art from the project's own icon (make_icon.py): its "FW" monogram and colours, and a woven lattice
drawn here. No outside artwork is used. Needs Pillow.

    python launcher/icon/make_steam_art.py

Writes the PNGs into launcher/src-tauri/steam-art/, which the launcher carries and puts beside the shortcut it adds to Steam
(src-tauri/src/art.rs). The letters are taken from icon.png, so no font is needed.
"""
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import make_icon as mi

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), 'src-tauri', 'steam-art')
INNER, OUTER = (58, 44, 112), (14, 12, 32)
GOLD = (214, 170, 92)


def monogram():
    """The letters of icon.png: the gold pixels inside its frame."""
    crop = Image.open(os.path.join(HERE, 'icon.png')).convert('RGB').crop((140, 320, 910, 725))
    r, _, b = crop.split()
    alpha = ImageChops.subtract(r, b).point(lambda v: max(0, min(255, (v - 5) * 255 // 70)))
    out = crop.convert('RGBA')
    out.putalpha(alpha)
    return out.crop(alpha.point(lambda v: 255 if v > 40 else 0).getbbox())


def background(w, h, radius=0.75):
    side = max(w, h)
    top = (side - h) // 2
    return mi.radial(side, INNER, OUTER, center=(0.5, 0.5), radius=radius).crop((0, top, w, top + h)).convert('RGBA')


def weave(w, h, step, width, strength, clear):
    """Two families of diagonal threads passing over and under each other, faded out around `clear` (x, y, rx, ry: shares of the
    picture) so that what is in front reads."""
    k = 2  # drawn at twice the size, for smooth edges
    wk, hk, st, wd = w * k, h * k, step * k, width * k
    layer = Image.new('L', (wk, hk), 0)
    d = ImageDraw.Draw(layer)
    n = (wk + hk) // st + 2
    for i in range(-n, n):
        d.line([(i * st, 0), (i * st + hk, hk)], fill=255, width=wd)
    for j in range(0, n * 2):
        d.line([(j * st, 0), (j * st - hk, hk)], fill=0, width=wd * 3)
        d.line([(j * st, 0), (j * st - hk, hk)], fill=255, width=wd)
    for i in range(-n, n):  # at every other crossing the first family is on top again
        for j in range(0, n * 2):
            if (i + j) % 2:
                continue
            x, y = (i + j) * st / 2, (j - i) * st / 2
            if -st < x < wk + st and -st < y < hk + st:
                r = wd * 2.4
                d.line([(x - r, y - r), (x + r, y + r)], fill=0, width=wd * 3)
                d.line([(x - r * 1.5, y - r * 1.5), (x + r * 1.5, y + r * 1.5)], fill=255, width=wd)
    layer = layer.resize((w, h), Image.LANCZOS)
    fade = Image.new('L', (160, 160))
    px = fade.load()
    cx, cy, rx, ry = clear
    for yy in range(160):
        for xx in range(160):
            t = (((xx / 159 - cx) / rx) ** 2 + ((yy / 159 - cy) / ry) ** 2) ** 0.5
            t = max(0.0, min(1.0, (t - 0.55) / 0.9))
            px[xx, yy] = int(255 * strength * t * t * (3 - 2 * t))
    gold = Image.new('RGBA', (w, h), GOLD + (255,))
    gold.putalpha(ImageChops.multiply(layer, fade.resize((w, h), Image.BICUBIC)))
    return gold


def frame(img, inset, line, radius):
    mask = Image.new('L', img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((inset, inset, img.width - 1 - inset, img.height - 1 - inset), radius=radius,
                                           outline=150, width=line)
    gold = Image.new('RGBA', img.size, GOLD + (255,))
    gold.putalpha(mask)
    return Image.alpha_composite(img, gold)


def place(img, mono, width, cx, cy, shadow):
    """The monogram, `width` wide and centred on (cx, cy), over a soft shadow."""
    m = mono.resize((width, round(mono.height * width / mono.width)), Image.LANCZOS)
    x, y = round(cx - m.width / 2), round(cy - m.height / 2)
    alpha = Image.new('L', img.size, 0)
    alpha.paste(m.getchannel('A'), (x, y + shadow // 2))
    dark = Image.new('RGBA', img.size, (0, 0, 0, 0))
    dark.putalpha(alpha.filter(ImageFilter.GaussianBlur(shadow)).point(lambda v: v * 180 // 255))
    img = Image.alpha_composite(img, dark)
    img.alpha_composite(m, (x, y))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    mono = monogram()

    def save(img, name):
        img.save(os.path.join(OUT, name), optimize=True)

    portrait = Image.alpha_composite(background(600, 900), weave(600, 900, 74, 3, 0.55, (0.5, 0.5, 0.55, 0.36)))
    save(place(frame(portrait, 22, 3, 28), mono, 440, 300, 450, 14), 'portrait.png')
    wide = Image.alpha_composite(background(920, 430), weave(920, 430, 62, 3, 0.55, (0.5, 0.5, 0.42, 0.62)))
    save(place(frame(wide, 18, 3, 24), mono, 450, 460, 215, 14), 'wide.png')
    # the hero is the background alone: Steam lays the logo over it
    save(Image.alpha_composite(background(1920, 620, radius=0.62), weave(1920, 620, 86, 3, 0.42, (0.5, 0.5, 0.3, 0.55))), 'hero.png')
    save(place(Image.new('RGBA', (900, 520), (0, 0, 0, 0)), mono, 840, 450, 255, 12), 'logo.png')
    save(Image.open(os.path.join(HERE, 'icon.png')).resize((256, 256), Image.LANCZOS), 'icon.png')
    print('wrote', OUT)


if __name__ == '__main__':
    main()
