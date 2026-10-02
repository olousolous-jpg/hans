#!/usr/bin/env python3
"""Náhled shaderu Liquid Glass bez KWinu.

Vykreslí glass.frag (stejný soubor, jaký používá efekt) nad zkušebním
pozadím a uloží PNG. Hodí se k ladění vzhledu a ke kontrole, že se shader
přeloží (GLSL 1.40, stejně jako v KWinu).

    pip install moderngl pillow numpy
    python3 tools/preview.py                 # všechny scény do ./preview/
    python3 tools/preview.py --refraction 20 --chroma 0.6 --scene ring

Na stroji bez GPU stačí Mesa (llvmpipe) přes EGL.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
SHADERS = HERE.parent / "effect" / "src" / "shaders"
W, H = 1280, 800

# KWin si sdf.glsl doplní sám (#include), tady ho vložíme ručně
SDF_GLSL = """
float sdfRoundedBox(vec2 position, vec2 center, vec2 extents, vec4 radius) {
    vec2 p = position - center;
    float r = p.x > 0.0
        ? (p.y < 0.0 ? radius.y : radius.w)
        : (p.y < 0.0 ? radius.x : radius.z);
    vec2 q = abs(p) - extents + vec2(r);
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}
"""

VERT = """
#version 140
uniform vec2 viewSize;
in vec2 position;
in vec2 texcoord;
out vec2 uv;
out vec2 vertex;
void main() {
    vec2 ndc = vec2(position.x / viewSize.x * 2.0 - 1.0, 1.0 - position.y / viewSize.y * 2.0);
    gl_Position = vec4(ndc, 0.0, 1.0);
    uv = texcoord;
    vertex = position;
}
"""


def background() -> Image.Image:
    img = Image.new("RGB", (W, H))
    px = np.zeros((H, W, 3), dtype=np.float32)
    x = np.linspace(0, 1, W)[None, :, None]
    stops = np.array([[255, 78, 80], [249, 212, 35], [31, 162, 255], [168, 50, 121]], np.float32)
    t = x * 3
    i = np.clip(t.astype(int), 0, 2)
    f = t - i
    px[:] = stops[i[..., 0]] * (1 - f) + stops[i[..., 0] + 1] * f
    img = Image.fromarray(px.astype(np.uint8))
    d = ImageDraw.Draw(img)
    for k in range(0, H, 50):
        d.rectangle([0, k, W, k + 5], fill=(255, 255, 255) if (k // 50) % 2 else (0, 0, 0))
    for k in range(0, W, 50):
        d.rectangle([k, 0, k + 3, H], fill=(32, 32, 32))
    d.text((40, 300), "LIQUID GLASS", fill=(255, 255, 255), font=load_font(90, bold=True))
    return img


def load_font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def saturation_matrix(s: float) -> list[float]:
    r, g, b = (1 - s) * 0.2126, (1 - s) * 0.7152, (1 - s) * 0.0722
    q = np.array([[r + s, r, r, 0], [g, g + s, g, 0], [b, b, b + s, 0], [0, 0, 0, 1]], np.float32)
    return list(q.flatten(order="F"))


def rounded_mask(size, box, radius) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def render(args, scene: str, out: Path):
    import moderngl

    ctx = moderngl.create_standalone_context(backend="egl") if args.egl else moderngl.create_standalone_context()
    frag = (SHADERS / "glass.frag").read_text().replace('#include "sdf.glsl"', SDF_GLSL)
    prog = ctx.program(vertex_shader=VERT, fragment_shader=frag)

    sharp = background()
    blurred = sharp.filter(ImageFilter.GaussianBlur(args.blur))
    tex = ctx.texture((W, H), 3, blurred.transpose(Image.FLIP_TOP_BOTTOM).tobytes())
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.repeat_x = tex.repeat_y = False
    sharp_tex = ctx.texture((W, H), 3, sharp.transpose(Image.FLIP_TOP_BOTTOM).tobytes())
    sharp_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    sharp_tex.repeat_x = sharp_tex.repeat_y = False

    # okno uprostřed
    win = (360, 220, 360 + 560, 220 + 360)
    radius = 12
    if scene in ("ring", "wave"):
        g = args.ring
        shape = (win[0] - g, win[1] - g, win[2] + g, win[3] + g)
        shape_radius = radius + g
    else:
        shape = win
        shape_radius = radius
    x0, y0, x1, y1 = shape

    def u(px, py):
        return px / W, 1 - py / H

    verts = np.array([
        x0, y0, *u(x0, y0), x1, y1, *u(x1, y1), x0, y1, *u(x0, y1),
        x0, y0, *u(x0, y0), x1, y0, *u(x1, y0), x1, y1, *u(x1, y1),
    ], np.float32)
    vbo = ctx.buffer(verts.tobytes())
    vao = ctx.vertex_array(prog, [(vbo, "2f 2f", "position", "texcoord")])

    def setu(name, value):
        if name in prog:
            prog[name].value = value

    cursor = (win[0] + 30, win[1] + 10) if scene == "light" else (-9999, -9999)
    setu("viewSize", (W, H))
    setu("texUnit", 0)
    setu("colorMatrix", tuple(saturation_matrix(args.saturation)))
    setu("offset", 1.0)
    setu("halfpixel", (0.5 / W, 0.5 / H))
    setu("opacity", 1.0)
    setu("texSize", (W, H))
    setu("box", ((x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2))
    setu("cornerRadius", (shape_radius,) * 4)
    setu("clipEnabled", 1.0)
    setu("edgeWidth", float(args.edge))
    setu("refraction", float(args.refraction))
    setu("chroma", float(args.chroma))
    setu("specular", float(args.specular))
    setu("tint", (1.0, 1.0, 1.0, float(args.tint)))
    setu("lightPos", cursor)
    setu("lightOn", 1.0 if scene == "light" else 0.0)
    setu("motion", (18.0, 0.0) if scene == "motion" else (0.0, 0.0))
    setu("lightAngle", math.pi * 1.25)
    setu("sharpTex", 1)
    setu("frameBox", ((win[0] + win[2]) / 2, (win[1] + win[3]) / 2, (win[2] - win[0]) / 2, (win[3] - win[1]) / 2))
    setu("frameRadius", (radius,) * 4)
    setu("hasRing", 1.0 if scene in ("ring", "wave") else 0.0)
    setu("ringClarity", float(args.clarity))
    setu("time", 1.3)
    setu("waveAmp", 9.0 * float(args.wave) if scene == "wave" else 0.0)
    setu("waveDir", (1.0, 0.0))

    fbo = ctx.simple_framebuffer((W, H), components=4)
    fbo.use()
    fbo.clear(0, 0, 0, 0)
    tex.use(0)
    sharp_tex.use(1)
    vao.render()
    raw = np.frombuffer(fbo.read(components=4), np.uint8).reshape(H, W, 4)[::-1]

    # složení: ostré pozadí + sklo (premultiplied) + obsah okna
    base = np.asarray(sharp, np.float32) / 255
    glass = raw[..., :3].astype(np.float32) / 255
    a = raw[..., 3:4].astype(np.float32) / 255
    comp = glass + base * (1 - a)
    img = Image.fromarray((np.clip(comp, 0, 1) * 255).astype(np.uint8))

    d = ImageDraw.Draw(img, "RGBA")
    if scene in ("ring", "wave"):
        # neprůhledné okno přes střed rámečku
        m = rounded_mask((W, H), win, radius)
        img.paste(Image.new("RGB", (W, H), (236, 236, 238)), (0, 0), m)
        d = ImageDraw.Draw(img, "RGBA")
        d.text((win[0] + 30, win[1] + 30), "neprůhledné okno se skleněným rámečkem", fill=(20, 20, 20), font=load_font(20))
    else:
        # průhledné okno: 19 % bílé přes sklo
        overlay = Image.new("RGBA", (W, H), (255, 255, 255, 48))
        m = rounded_mask((W, H), win, radius)
        img.paste(overlay, (0, 0), Image.fromarray((np.asarray(m) * (48 / 255)).astype(np.uint8)))
        d.text((win[0] + 30, win[1] + 30), "průhledné okno (%s)" % scene, fill=(20, 20, 20), font=load_font(20))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print("uloženo:", out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scene", choices=["glass", "ring", "light", "motion", "wave", "all"], default="all")
    ap.add_argument("--out", default="preview")
    ap.add_argument("--blur", type=float, default=10)
    ap.add_argument("--saturation", type=float, default=1.6)
    ap.add_argument("--edge", type=float, default=26)
    ap.add_argument("--refraction", type=float, default=20)
    ap.add_argument("--chroma", type=float, default=0.35)
    ap.add_argument("--specular", type=float, default=0.55)
    ap.add_argument("--tint", type=float, default=0.08)
    ap.add_argument("--ring", type=float, default=6)
    ap.add_argument("--clarity", type=float, default=0.7, help="čirost rámečku 0..1")
    ap.add_argument("--wave", type=float, default=0.6, help="síla vlnění 0..1")
    ap.add_argument("--egl", action="store_true", help="EGL bez displeje (Mesa llvmpipe)")
    args = ap.parse_args()
    scenes = ["glass", "ring", "light", "motion", "wave"] if args.scene == "all" else [args.scene]
    for s in scenes:
        render(args, s, Path(args.out) / ("%s.png" % s))


if __name__ == "__main__":
    main()
